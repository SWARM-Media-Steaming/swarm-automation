"""Worker integration for mandatory repository-aware issue complexity (#369)."""
from __future__ import annotations

import hashlib
import json
from typing import Any

from repository_complexity import (AI_KEYS, ComplexityStore, RepositoryProfiler, compact_context,
                                  component_for, deterministic_prediction, dumps, evaluation_prompt,
                                  format_analysis, interpret_prediction, relevant_components,
                                  unavailable_profile)


class ComplexityWorkerMixin:
    def complexity_store(self):
        if getattr(self, "_complexity_store", None) is None:
            self._complexity_store = ComplexityStore(self.config.execution_history_db)
        return self._complexity_store

    def refresh_complexity_profile(self) -> dict[str, Any]:
        """Runs on every worker poll, including empty queues; cached unless ref/age changes."""
        from swarm_issue_worker import log
        try:
            profile = RepositoryProfiler(self.complexity_store(), self.config.repo_dir,
                self.config.github_repository, git=self.config.git_bin,
                remote=self.config.remote_name, base=self.config.base_branch).refresh()
        except Exception as error:
            log(f"WARNING: Repository complexity metrics unavailable ({type(error).__name__}); continuing with partial evidence.")
            profile = unavailable_profile()
            try:
                previous = self.complexity_store().latest(self.config.github_repository)
                if previous:
                    profile = dict(previous, unavailable=previous["unavailable"] + ["refresh_failed_stale_profile"])
            except Exception:
                pass
        self._complexity_profile = profile
        return profile

    def prepare_issue_complexity(self, host) -> dict[str, Any]:
        from swarm_issue_worker import log
        try:
            return self._prepare_issue_complexity(host)
        except Exception as error:
            log(f"WARNING: Complexity evaluation degraded ({type(error).__name__}); continuing with deterministic defaults.")
            self._issue_complexity = deterministic_prediction(unavailable_profile("evaluation_unavailable"),
                self.issue.title or "", self.issue.body or "")
            return self._issue_complexity

    def _prepare_issue_complexity(self, host) -> dict[str, Any]:
        from swarm_issue_worker import log
        if getattr(self, "_issue_complexity", None) is not None:
            return self._issue_complexity
        if self.in_progress_file.exists():
            saved = self.read_state().get("routing_decision") or {}
            if isinstance(saved, dict) and isinstance(saved.get("complexity_analysis"), dict):
                # An attempt keeps its original algorithm/profile and prediction on resume.
                self._issue_complexity = saved["complexity_analysis"]
                return self._issue_complexity
        profile = getattr(self, "_complexity_profile", None) or self.refresh_complexity_profile()
        components = relevant_components(profile, self.issue.title, self.issue.body)
        history = []
        try:
            history = self.complexity_store().similar(self.config.github_repository, self.issue.title,
                                                     components, self.issue.number)
        except Exception:
            pass
        self._complexity_history = history
        baseline = deterministic_prediction(profile, self.issue.title, self.issue.body, history)
        baseline["likely_files"] = []
        try:
            if profile.get("id"):
                baseline["likely_files"] = self.complexity_store().likely_files(profile["id"], components)
        except Exception:
            pass
        architecture = ""
        try:
            service = self.knowledge_service()
            if service is not None:
                pack = service.build_context_pack(repository=self.config.github_repository,
                    issue_title=self.issue.title, issue_body=self.issue.body, issue_number=self.issue.number,
                    execution_id=self.history.execution_id, files=[], token_limit=1000)
                architecture = pack.render()
        except Exception:
            baseline["unavailable"].append("architecture_context")
        context = compact_context(profile, baseline, self.issue.title, self.issue.body, history, architecture)
        prediction = None
        failures = []
        if self.config.jev.enabled:
            try:
                from decision_engine import DecisionType
                result = self.evaluate_decision(DecisionType.REPOSITORY_COMPLEXITY.value,
                                                {"complexity_context": context})
                self.record_complexity_jev_usage(result)
                if result.get("source") == "jev" and not result.get("fallbackUsed"):
                    response = {"vector": {key: result["scores"][key]*100 for key in AI_KEYS}}
                    response["vector"]["confidence"] = result["confidence"]
                    prediction = interpret_prediction(baseline, response, "jev", history)
                else:
                    failures.append("jev_unavailable_or_low_confidence")
            except Exception:
                failures.append("jev_evaluation_failed")
        if prediction is None:
            try:
                from dynamic_router import parse_router_payload
                raw = self.run_router(host, evaluation_prompt(context), (), prompt_type="complexity")
                prediction = interpret_prediction(baseline, parse_router_payload(raw), "ai", history)
            except Exception:
                failures.append("ai_evaluation_failed")
                prediction = baseline
        prediction["evaluation_failures"] = failures
        prediction["deterministic_baseline"] = {key: baseline[key] for key in ("vector", "scope", "requirements")}
        prediction["evaluator"] = ({"provider": "jev", "model": self.config.jev.model}
                                   if prediction["source"] == "jev" else
                                   {"provider": host.key, "model": host.router_model, "effort": host.router_effort}
                                   if prediction["source"] == "ai" else {"provider": "local", "model": None})
        fingerprint = hashlib.sha256((self.issue.title + "\n" + self.issue.body).encode()).hexdigest()
        prediction["input_fingerprint"] = hashlib.sha256(dumps(context).encode()).hexdigest()
        try:
            prediction["evaluation_id"] = self.complexity_store().record_prediction(
                self.config.github_repository, self.issue.number, fingerprint, prediction)
        except Exception as error:
            log(f"WARNING: Complexity prediction persistence unavailable ({type(error).__name__}).")
        self._issue_complexity = prediction
        return prediction

    def record_complexity_jev_usage(self, result: dict[str, Any]) -> None:
        """Reuse invocation telemetry, retaining Jev's reported price when available."""
        from swarm_issue_worker import iso_timestamp
        from token_usage import NormalizedUsage
        usage = result.get("metadata", {}).get("usage") or {}
        # A low-confidence call still spent tokens; the decision engine retains its recommendation.
        if not usage:
            usage = result.get("metadata", {}).get("jevRecommendation", {}).get("metadata", {}).get("usage") or {}
        values = {key: usage.get(key) for key in ("input_tokens", "output_tokens")}
        tokens = [value for value in values.values() if value is not None]
        self._record_usage_event(agent_type="router", prompt_type="complexity", provider_key="jev",
            provider_name="Jev", model=str(result.get("model") or self.config.jev.model), effort="",
            attempt_number=1, usage=NormalizedUsage(**values, total_tokens=sum(tokens) if tokens else None),
            started_at=iso_timestamp(), success=result.get("source") == "jev",
            error_type=str(result.get("errorType") or ""))
        cost = result.get("estimatedCost")
        if isinstance(cost, (int, float)) and cost >= 0:
            events = self.current_token_usage_events()
            if events and events[-1].get("provider") == "Jev":
                events[-1].update(estimated_cost=cost, pricing_status="reported", pricing_source="jev", pricing_version="reported")
                self.token_usage_events = events
                if self.in_progress_file.exists():
                    self.update_state(token_usage_events=events)

    def enforce_issue_complexity(self, decision, candidates):
        from dynamic_router import apply_complexity_requirements
        from swarm_issue_worker import log
        prediction = getattr(self, "_issue_complexity", None)
        if not prediction:
            return decision
        try:
            return apply_complexity_requirements(decision, prediction, candidates,
                allow_usage_credit_models=self.config.allow_usage_credit_models,
                history=getattr(self, "_complexity_history", ()),
                keep_provider=self.rework_kept_provider(decision))
        except Exception as error:
            # Catalog failures must not stop work; never claim the floor was met.
            log(f"WARNING: Complexity-aware routing unavailable ({type(error).__name__}); requirements retained for audit.")
            return dict(decision, complexity_analysis=prediction, complexity_requirements_unmet=True)

    def rework_kept_provider(self, decision) -> str:
        """The previous tool when the router deliberately kept it for a follow-up."""
        previous = (self.issue.previous_ai or "").lower()
        if (self.issue.work_type == "followup" and previous and decision.get("provider") == previous
                and not decision.get("provider_override_reason")):
            return previous
        return ""

    def persist_complexity_routing(self) -> None:
        from swarm_issue_worker import log
        prediction = (self.routing or {}).get("complexity_analysis") or getattr(self, "_issue_complexity", None)
        if prediction and prediction.get("evaluation_id"):
            try:
                self.complexity_store().routing(prediction["evaluation_id"], self.routing or {}, self.history.execution_id)
            except Exception as error:
                log(f"WARNING: Complexity routing persistence unavailable ({type(error).__name__}).")

    def complexity_prompt_note(self) -> str:
        prediction = getattr(self, "_issue_complexity", None)
        if not prediction and self.in_progress_file.exists():
            prediction = (self.read_state().get("routing_decision") or {}).get("complexity_analysis")
        if not prediction:
            return ""
        return "\nRepository-aware task assessment (predictions; existing validation gates remain authoritative):\n" + dumps(
            {key: prediction[key] for key in ("vector", "scope", "requirements")}) + "\n"

    def finish_complexity_outcome(self, status: str, files_changed=()) -> None:
        from swarm_issue_worker import log
        if status in {"quota_paused", "awaiting_input"}:
            return
        state = self.read_state() if self.in_progress_file.exists() else {}
        prediction = getattr(self, "_issue_complexity", None) or (state.get("routing_decision") or {}).get("complexity_analysis")
        if not prediction or not prediction.get("evaluation_id"):
            return
        try:
            events = self.current_token_usage_events()
            unique = {event["id"]: event for event in events if event.get("id")}
            events = list(unique.values())
            components = set(component_for(path) for path in files_changed)
            profile = getattr(self, "_complexity_profile", {})
            if profile.get("id"):
                mapping = self.complexity_store().files(profile["id"])
                components = {mapping.get(path, {}).get("component", component_for(path)) for path in files_changed}
            stages = {}
            repairs = 0
            for key, label in (("adversarial", "uat"), ("adversarial_security", "cyber")):
                loop = state.get(key) or {}
                rounds = loop.get("rounds") or []
                count = len(rounds)
                repairs += sum(int(row.get("round_number", 0)) > 0 for row in rounds)
                # Structured round reports, never raw finding text.
                findings = sum(max(len(row.get("findings") or row.get("in_scope_findings") or []),
                                   int(row.get("tests_failing_after") or 0))
                               for row in rounds if isinstance(row, dict))
                stages[label] = {"rounds": count, "findings": findings, "status": loop.get("status")}
            actual = {"files_changed": len(set(files_changed)), "modules_changed": len(components),
                      "services_changed": sum(name.startswith(("services/", "apps/")) for name in components),
                      "components": sorted(components), "worker_rounds": sum(event.get("agent_type") == "primary" for event in events),
                      "repair_rounds": repairs, "adversarial": stages,
                      "models": sorted({str(event.get("provider"))+"/"+str(event.get("model")) for event in events}),
                      "usage_records": events}
            for key in ("input_tokens", "output_tokens", "cached_input_tokens", "reasoning_tokens", "total_tokens", "estimated_cost"):
                values = [event[key] for event in events if event.get(key) is not None]
                actual[key] = sum(values) if values else None
            if any((state.get(key) or {}).get("outcome") == "cap_hit" for key in ("adversarial", "adversarial_security")) and status == "completed":
                status = "best_effort"
            actual["final_status"] = status
            self.complexity_store().outcome(prediction["evaluation_id"], status, actual)
        except Exception as error:
            log(f"WARNING: Complexity outcome persistence unavailable ({type(error).__name__}).")
