"""Native session lifecycle and measured caching regressions (#381)."""
import dataclasses
import datetime as dt
import json
import subprocess
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

from ai_execution_history import ExecutionHistoryRepository
from model_pricing import CostEstimate
from prompt_sessions import SESSION_MAX_IDLE_SECONDS, resume_failure, valid_session_id
from swarm_issue_worker import Config, IssueContext, ProviderChoice, Worker, build_parser
from token_usage import (NormalizedUsage, cache_metrics, invocation_usage, normalize_usage,
                         render_ai_usage_markdown)
from usage_report import UsageFilters, build_usage_report, cache_routing_evidence
from test_usage_report import _event


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "ai/codex/issue-381")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "Test")
        (self.repo / "source.py").write_text("value = 1\n")
        self.git("add", ".")
        self.git("commit", "-qm", "initial")
        config = Config.from_args(build_parser().parse_args([
            "--repo-dir", str(self.repo), "--state-dir", str(self.root / "state"),
            "--github-repository", "acme/project", "--no-dynamic-model-routing",
            "--no-require-bot-auth", "--github-apps-config", str(self.root / "no-auth.json"),
        ]))
        self.worker = Worker(config)
        self.worker.issue = IssueContext(381, "Caching", "Required behavior", [], "https://example.invalid/381")
        self.worker.choice = ProviderChoice("Claude", "claude-sonnet-5", "high", str(uuid.uuid4()))
        self.worker.save_new_state(self.worker.issue, self.worker.choice, self.git("rev-parse", "HEAD"))
        self.calls = []

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], check=True,
                              capture_output=True, text=True).stdout.strip()

    def runner(self, prompt, env, activity="working"):
        self.calls.append((self.worker.choice.session_id, self.worker.choice.resume, prompt))
        self.worker.update_state(session_started=True)
        self.worker._last_ai_raw_output = json.dumps({"type": "result", "usage": {
            "input_tokens": 10, "output_tokens": 20, "cache_read_input_tokens": 90,
            "cache_creation_input_tokens": 0}, "total_cost_usd": 0.004})
        return 0

    def run_turn(self):
        with mock.patch.object(self.worker, "_run_claude", side_effect=self.runner), \
             mock.patch.object(self.worker, "provider_environment", return_value={}), \
             mock.patch.object(self.worker, "adversarial_prompt", return_value="current findings and patch"):
            return self.worker.run_ai("current requirements")

    def seed(self):
        self.worker.prepare_cli_session()
        self.worker.update_state(session_started=True)
        self.worker.remember_cli_session(True)
        return self.worker.choice.session_id

    def test_primary_reuses_only_confirmed_session_and_records_it(self):
        self.run_turn()
        self.run_turn()
        self.assertEqual(self.calls[0][0], self.calls[1][0])
        self.assertEqual([c[1] for c in self.calls], [False, True])
        events = self.worker.read_state()["token_usage_events"]
        self.assertEqual([e["session_reused"] for e in events], [False, True])
        self.assertEqual(events[1]["reported_cost"], 0.004)
        self.assertEqual(events[1]["cache_input_tokens"], 100)
        self.assertEqual(events[1]["agent_run_id"], self.calls[1][0])
        self.assertGreaterEqual(events[1]["duration_ms"], 0)

    def test_fix_rounds_reuse_but_uat_security_and_implementation_are_independent(self):
        primary = self.seed()
        for stage in ("adversarial", "adversarial_security"):
            self.worker.update_state(**{stage: {"active": True, "phase": "fix", "epoch": 1, "round": 1}})
            self.worker.fresh_cli_session()
            fixer = self.seed()
            self.assertNotEqual(fixer, primary)
            self.worker.fresh_cli_session()
            self.worker.prepare_cli_session()
            self.assertTrue(self.worker.choice.resume)
            self.assertEqual(self.worker.choice.session_id, fixer)
            self.worker.update_state(**{stage: {"active": True, "phase": "test", "epoch": 1, "round": 1}})
            self.worker.fresh_cli_session()
            tester = self.seed()
            self.assertNotIn(tester, (primary, fixer))
            self.worker.fresh_cli_session()  # new assessment, even at same round after rejected report
            self.worker.prepare_cli_session()
            self.assertFalse(self.worker.choice.resume)
            self.assertNotEqual(self.worker.choice.session_id, tester)
            self.worker.update_state(**{stage: {"active": False}})

    def test_interrupted_review_resumes_only_its_own_phase(self):
        self.worker.update_state(adversarial={"active": True, "phase": "test", "round": 0})
        tester = self.seed()
        self.worker.choice.resume = True
        self.worker.prepare_cli_session()
        self.assertEqual(self.worker.choice.session_id, tester)
        self.assertTrue(self.worker.choice.resume)
        self.worker.update_state(adversarial={"active": True, "phase": "test", "round": 1})
        self.assertTrue(self.worker.prepare_cli_session())
        self.assertFalse(self.worker.choice.resume)

    def test_rejected_assessment_cannot_resume_its_discarded_session(self):
        for stage in ("adversarial", "adversarial_security"):
            with self.subTest(stage=stage):
                self.worker.update_state(**{stage: {
                    "active": True, "phase": "test", "epoch": 1, "round": 0,
                }})
                rejected = self.seed()
                self.worker.update_state(**{stage: {
                    "active": True, "phase": "test", "epoch": 1, "round": 0,
                    "retry_rejection": {"reason": "invalid tester result", "attempts": 1},
                }})
                self.worker.choice.resume = True
                self.worker.choice.session_id = rejected
                rebuilt = self.worker.prepare_cli_session()
                self.assertTrue(rebuilt)
                self.assertFalse(self.worker.choice.resume)
                self.assertNotEqual(self.worker.choice.session_id, rejected)
                retry = self.seed()
                self.assertNotEqual(retry, rejected)
                self.worker.choice.resume = True
                self.worker.prepare_cli_session()
                self.assertTrue(self.worker.choice.resume)
                self.assertEqual(self.worker.choice.session_id, retry)
                self.worker.update_state(**{stage: {"active": False}})

    def test_model_effort_issue_repository_and_instruction_changes_invalidate(self):
        for change in ("model", "effort", "issue", "repository", "instructions"):
            with self.subTest(change=change):
                previous = self.seed()
                self.worker.choice.resume = True
                if change == "model":
                    self.worker.choice.model = "other-model"
                elif change == "effort":
                    self.worker.choice.effort = "max"
                elif change == "issue":
                    self.worker.issue.number += 1
                elif change == "repository":
                    self.worker.config = dataclasses.replace(self.worker.config, github_repository="other/repo")
                else:
                    (self.repo / "AGENTS.md").write_text("New mandatory requirement")
                self.assertTrue(self.worker.prepare_cli_session())
                self.assertFalse(self.worker.choice.resume)
                self.assertNotEqual(previous, self.worker.choice.session_id)

    def test_expired_corrupt_unconfirmed_and_reset_sessions_are_fresh(self):
        for mutation in ("old", "invalid", "unconfirmed", "future", "reset"):
            with self.subTest(mutation=mutation):
                self.seed()
                state = self.worker.read_state()
                entry = state["cli_sessions"]["primary"]
                if mutation == "old":
                    entry["updated_at"] = time.time() - SESSION_MAX_IDLE_SECONDS - 1
                elif mutation == "invalid":
                    entry["id"] = "--last"
                elif mutation == "unconfirmed":
                    entry["started"] = False
                elif mutation == "future":
                    entry["updated_at"] = "NaN"
                else:
                    entry["head"] = "0" * 40
                self.worker.write_state(state)
                self.worker.choice.resume = True
                self.assertTrue(self.worker.prepare_cli_session())
                self.assertFalse(self.worker.choice.resume)

    def test_failed_resume_retries_once_fresh_with_full_context(self):
        self.seed()
        calls = []
        def fail_then_succeed(prompt, env, activity):
            calls.append((self.worker.choice.resume, prompt))
            if len(calls) == 1:
                self.worker._last_ai_raw_output = "No conversation found with session ID"
                return 1
            return self.runner(prompt, env, activity)
        with mock.patch.object(self.worker, "_run_claude", side_effect=fail_then_succeed), \
             mock.patch.object(self.worker, "provider_environment", return_value={}), \
             mock.patch.object(self.worker, "build_prompt", return_value="full current issue"):
            self.assertEqual(self.worker.run_ai("delta"), 0)
        self.assertEqual([c[0] for c in calls], [True, False])
        self.assertIn("full current issue", calls[1][1])
        self.assertEqual(len(self.worker.read_state()["token_usage_events"]), 2)

    def test_legacy_resume_gets_fresh_context_and_no_cross_issue_lookup(self):
        self.worker.choice.resume = True
        self.worker.update_state(session_started=True)
        self.assertTrue(self.worker.prepare_cli_session())
        self.assertFalse(self.worker.choice.resume)

    def test_compaction_does_not_force_fresh_session(self):
        previous = self.seed()
        self.worker._last_ai_raw_output = '{"type":"system","subtype":"compact_boundary"}'
        self.worker.remember_cli_session(True)
        self.worker.prepare_cli_session()
        self.assertEqual(self.worker.choice.session_id, previous)
        self.assertTrue(self.worker.choice.resume)

    def test_successful_compaction_diagnostics_are_not_resume_failures(self):
        for text in (
            json.dumps({"type": "system", "subtype": "compact_boundary"}),
            json.dumps({
                "type": "system", "subtype": "compact_boundary",
                "message": "context window was full; compacted successfully",
            }),
            "Compacted conversation to 12% of the context window",
            "Successfully compacted the conversation after the context window was full.",
        ):
            with self.subTest(text=text[:60]):
                self.assertFalse(resume_failure(text))
        self.assertTrue(resume_failure("prompt is too long"))
        self.assertTrue(resume_failure(json.dumps({
            "type": "error", "error": {"code": "context_length_exceeded"},
        })))

    def test_independent_pass_cannot_resume_the_implementer(self):
        implementer = self.seed()
        self.worker._independent_ai_pass = True
        self.worker.choice.resume = True
        self.worker.choice.session_id = implementer
        try:
            rebuilt = self.worker.prepare_cli_session()
            self.assertFalse(rebuilt)
            self.assertFalse(self.worker.choice.resume)
            self.assertNotEqual(self.worker.choice.session_id, implementer)
            self.assertTrue(valid_session_id(self.worker.choice.session_id))
            self.worker.remember_cli_session(True)
        finally:
            self.worker._independent_ai_pass = False
        stored = self.worker.read_state().get("cli_sessions") or {}
        self.assertEqual(stored.get("primary", {}).get("id"), implementer)
        self.assertNotIn("documentation", stored)

    def test_session_metadata_contains_no_source_or_prompt(self):
        self.run_turn()
        metadata = json.dumps(self.worker.read_state()["cli_sessions"])
        self.assertNotIn("Required behavior", metadata)
        self.assertNotIn("current requirements", metadata)
        self.assertNotIn("value = 1", metadata)


class CacheTelemetryTests(unittest.TestCase):
    def test_provider_denominators_and_write_premium(self):
        price = CostEstimate(1, "priced", input_rate_per_million=2,
                             cached_input_rate_per_million=.2, cache_write_rate_per_million=2.5)
        claude = NormalizedUsage(input_tokens=100, cache_read_tokens=800, cache_write_tokens=100)
        codex = NormalizedUsage(input_tokens=1000, cache_read_tokens=800, cached_tokens_included_in_input=True)
        self.assertEqual(cache_metrics(claude, price)["cache_input_tokens"], 1000)
        self.assertEqual(cache_metrics(codex, price)["cache_input_tokens"], 1000)
        self.assertAlmostEqual(cache_metrics(claude, price)["cache_savings_estimate"], .00139)
        self.assertIsNone(cache_metrics(NormalizedUsage(input_tokens=1), price)["cache_input_tokens"])
        self.assertIsNone(cache_metrics(claude, CostEstimate(None, "unknown_model"))["cache_savings_estimate"])

    def test_codex_cumulative_counts_are_differenced_and_turn_usage_takes_priority(self):
        raw = json.dumps({"type": "token_count", "info": {"total_token_usage": {
            "input_tokens": 150, "output_tokens": 20, "cached_input_tokens": 120}}})
        usage = normalize_usage("codex", raw)
        baseline = {"input_tokens": 100, "output_tokens": 10, "cache_read_tokens": 80,
                    "cached_input_tokens": 80, "total_tokens": 110}
        delta = invocation_usage(usage, True, baseline)
        self.assertEqual((delta.input_tokens, delta.total_tokens, delta.cache_read_tokens), (50, 60, 40))
        self.assertIsNone(invocation_usage(usage, True, None).input_tokens)
        self.assertIsNone(invocation_usage(usage, True, {"input_tokens": 200}).input_tokens)
        raw = json.dumps({"type": "turn.completed", "usage": {"input_tokens": 5}}) + "\n" + raw
        self.assertEqual(normalize_usage("codex", raw).input_tokens, 5)

    def test_damaged_cumulative_baseline_stays_unavailable(self):
        usage = NormalizedUsage(input_tokens=150, output_tokens=20, usage_scope="session")
        for baseline in ("corrupt", [1], 7, {"input_tokens": "100"}):
            with self.subTest(baseline=baseline):
                delta = invocation_usage(usage, True, baseline)
                self.assertIsNone(delta.input_tokens)

    def test_invalid_statistics_stay_unavailable(self):
        for invalid in (-1, float("inf"), float("nan"), True, "bad", 1.5):
            usage = normalize_usage("claude", json.dumps({"type": "result", "usage": {"input_tokens": invalid},
                                                         "total_cost_usd": invalid}))
            self.assertIsNone(usage.input_tokens)
            if invalid != 1.5:
                self.assertIsNone(usage.reported_cost)
        self.assertFalse(valid_session_id("--last"))
        self.assertFalse(resume_failure("Rate limit exceeded"))
        self.assertTrue(resume_failure(
            "Error: thread/resume failed: no rollout found for thread id "
            "11111111-2222-4333-8444-555555555555 (code -32600)"
        ))

    def test_migration_roundtrip_aggregation_and_history_compatibility(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = ExecutionHistoryRepository(Path(directory) / "history.db")
            legacy = _event("old", sequence=1)
            current = _event("new", sequence=2, session_reused=True, session_role="primary", agent_run_id=str(uuid.uuid4()),
                             cache_input_tokens=1000, cache_read_tokens=800, cache_savings_estimate=.002,
                             reported_cost=.003)
            repository.record_token_usage_batch("run", "acme/project", 381, [legacy, current])
            with repository.connect() as database:
                report = build_usage_report(database, filters=UsageFilters())
            summary = report["summary"]
            self.assertEqual(summary["cacheHitEfficiency"], .8)
            self.assertEqual(summary["sessionReuseReported"], 1)
            self.assertEqual(summary["reportedCost"], .003)
            records = repository.token_usage_for_execution("run")
            self.assertIsNone(records[0]["session_reused"])
            self.assertEqual(records[1]["cache_input_tokens"], 1000)
            rendered = render_ai_usage_markdown([legacy, current])
            self.assertIn("80.0%", rendered)
            self.assertIn("not realized billing savings", rendered)
            # Reopening applies the additive migration idempotently.
            self.assertEqual(len(ExecutionHistoryRepository(repository.database_path).token_usage_for_execution("run")), 2)

    def test_router_requires_recent_matching_successful_measured_history(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = ExecutionHistoryRepository(Path(directory) / "history.db")
            now = dt.datetime.now(dt.timezone.utc).isoformat()
            for n in range(20):
                repository.record_token_usage_batch("run", "acme/project", n % 5 + 1, [
                    _event(str(n), session_role="primary", cache_input_tokens=1000,
                           cache_read_tokens=800, estimated_cost=.008, cache_savings_estimate=.002,
                           started_at=now)])
                with repository.connect() as database:
                    evidence = cache_routing_evidence(database, "acme/project", "primary")
                self.assertEqual(bool(evidence), n == 19)
            self.assertAlmostEqual(evidence[0]["api_cost_discount"], .2)
            with repository.connect() as database:
                self.assertEqual(cache_routing_evidence(database, "other/repo", "primary"), [])
                self.assertEqual(cache_routing_evidence(database, "acme/project", "adversarial:test"), [])
                database.execute("UPDATE ai_token_usage SET success = 0 WHERE id IN ('0', '1')")
                self.assertEqual(cache_routing_evidence(database, "acme/project", "primary"), [])


if __name__ == "__main__":
    unittest.main()
