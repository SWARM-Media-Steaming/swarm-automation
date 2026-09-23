#!/usr/bin/env python3

from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest import mock

import diagnose
import test_swarm_issue_worker as fixtures
from diagnostic_store import DiagnosticProblem, DiagnosticRepository
from swarm_issue_worker import ProviderUsage


class DiagnoseTests(unittest.TestCase):
    setUp = fixtures.WorkerTestCase.setUp
    tearDown = fixtures.WorkerTestCase.tearDown
    git = fixtures.WorkerTestCase.git
    _worker_argv = fixtures.WorkerTestCase._worker_argv

    def repos_file(self, label: str = "SWARM-Media-Steaming/swarm") -> Path:
        entry = {
            "label": label,
            "workspace_dir": str(self.repo),
            "state_dir": str(self.state),
            "base_branch": "main",
            "remote_name": "origin",
            "integration_branch": "ai-main",
            "worker_args": self._worker_argv(auto=False) + ["--github-repository", label],
        }
        path = self.root / "repos.json"
        path.write_text(json.dumps([entry]))
        return path

    def write_log(self, path: Path, lines: list[str]) -> None:
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def clean_log(self, label: str) -> list[str]:
        return [f"[{label}] Selected oldest unprocessed assigned issue: #1 Example",
                f"[{label}] no issue to work right now."]

    def error_log(self, label: str, error: str) -> list[str]:
        return [f"[{label}] Grok is working. ...",
                f"[{label}] ERROR: {error}",
                f"[{label}] worker exited with status 1; will retry."]

    def run_diagnose(self, repos_file: Path, log_lines: list[str], **kwargs) -> dict:
        app_log = self.root / "automation.log"
        self.write_log(app_log, log_lines)
        return diagnose.diagnose(
            repos_file=repos_file, app_log=app_log, cron_log=app_log,
            extra_argv=[], **kwargs,
        )

    # ---- gathering / short-circuits ------------------------------------

    def test_no_problems_when_the_log_is_clean(self) -> None:
        label = "SWARM-Media-Steaming/swarm"
        with mock.patch("diagnose.select_provider") as select_provider:
            result = self.run_diagnose(self.repos_file(label), self.clean_log(label))
        self.assertEqual(result["problems"], [])
        self.assertTrue(result["ai_available"])
        select_provider.assert_not_called()

    def test_a_later_success_clears_an_earlier_error(self) -> None:
        label = "SWARM-Media-Steaming/swarm"
        lines = self.error_log(label, "Out-of-scope findings named unknown adversarial suites")
        lines += [f"[{label}] Committed completed issue #1 work as " + "a" * 40 + "."]
        with mock.patch("diagnose.select_provider") as select_provider:
            result = self.run_diagnose(self.repos_file(label), lines)
        self.assertEqual(result["problems"], [])
        select_provider.assert_not_called()

    def test_canned_pattern_resolves_without_any_provider_call(self) -> None:
        label = "SWARM-Media-Steaming/swarm"
        lines = self.error_log(label, "Grok exhausted usage before returning a resumable session ID; "
                                       "quota_paused")
        with mock.patch("diagnose.select_provider") as select_provider:
            result = self.run_diagnose(self.repos_file(label), lines)
        self.assertEqual(len(result["problems"]), 1)
        problem = result["problems"][0]
        self.assertEqual(problem["source"], "canned")
        self.assertFalse(problem["is_bug"])
        self.assertTrue(problem["actionable_items"])
        self.assertIsNone(problem["provider"])
        select_provider.assert_not_called()

    def test_cache_short_circuit_skips_the_provider_call_on_a_repeat_run(self) -> None:
        label = "SWARM-Media-Steaming/swarm"
        error = "Out-of-scope findings named unknown adversarial suites"
        lines = self.error_log(label, error)
        repos = self.repos_file(label)
        signature = diagnose.problem_signature(label, [f"[{label}] ERROR: {error}"])
        # Point the store at execution_history_db via the same worker_args the
        # real diagnose() call will construct, so it reads/writes the same file.
        from swarm_issue_worker import Config, build_parser
        args = build_parser().parse_args(self._worker_argv(auto=False))
        config = Config.from_args(args)
        store = DiagnosticRepository(config.execution_history_db)
        store.insert(
            "prior-run", diagnose.iso_timestamp(),
            DiagnosticProblem(
                repository=label, signature=signature, source="ai", provider="claude", model="claude-sonnet-5",
                explanation="Cached explanation.", confidence="medium",
                actionable_items=("Do the thing",), is_bug=False,
            ),
        )
        with mock.patch("diagnose.run_provider_oneshot") as oneshot, \
             mock.patch("diagnose.select_provider") as select_provider:
            result = self.run_diagnose(repos, lines)
        oneshot.assert_not_called()
        select_provider.assert_not_called()
        self.assertEqual(len(result["problems"]), 1)
        problem = result["problems"][0]
        self.assertEqual(problem["source"], "cache")
        self.assertEqual(problem["explanation"], "Cached explanation.")

    # ---- provider capacity -----------------------------------------------

    def test_no_provider_capacity_returns_unavailable_with_evidence(self) -> None:
        label = "SWARM-Media-Steaming/swarm"
        lines = self.error_log(label, "Something entirely novel broke")
        with mock.patch.object(
            __import__("swarm_issue_worker").Worker, "provider_usage",
            return_value=ProviderUsage(0, None),
        ):
            result = self.run_diagnose(self.repos_file(label), lines)
        self.assertFalse(result["ai_available"])
        self.assertIsNotNone(result["unavailable_reason"])
        self.assertEqual(len(result["problems"]), 1)
        problem = result["problems"][0]
        self.assertEqual(problem["source"], "unavailable")
        self.assertTrue(problem["evidence"])
        self.assertIsNone(problem["provider"])

    # ---- synthesis ---------------------------------------------------------

    def test_multi_problem_synthesis_maps_each_result_back_to_its_repository(self) -> None:
        label_a, label_b = "SWARM-Media-Steaming/swarm", "SWARM-Media-Steaming/swarm-feedback"
        entries = []
        for label in (label_a, label_b):
            entries.append({
                "label": label, "workspace_dir": str(self.repo), "state_dir": str(self.state),
                "base_branch": "main", "remote_name": "origin", "integration_branch": "ai-main",
                "worker_args": self._worker_argv(auto=False) + ["--github-repository", label],
            })
        repos = self.root / "repos.json"
        repos.write_text(json.dumps(entries))
        log_lines = (
            self.error_log(label_a, "Novel failure A")
            + self.error_log(label_b, "Novel failure B")
        )
        payload = json.dumps({
            "problems": [
                {"repository": label_a, "explanation": "A broke because X.", "confidence": "high",
                 "actionable_items": ["Fix X"], "is_bug": True,
                 "suggested_issue_title": "X is broken", "suggested_issue_body": "Standalone body A."},
                {"repository": label_b, "explanation": "B is a transient blip.", "confidence": "low",
                 "actionable_items": ["Retry"], "is_bug": False,
                 "suggested_issue_title": "", "suggested_issue_body": ""},
            ]
        })
        with mock.patch.object(
            __import__("swarm_issue_worker").Worker, "provider_usage",
            return_value=ProviderUsage(0, 80),
        ), mock.patch.object(
            __import__("swarm_issue_worker").Worker, "run_router", return_value="{}",
        ), mock.patch.object(
            __import__("swarm_issue_worker").Worker, "resolve_router_response",
            return_value={"provider": "claude", "selected_model": "claude-sonnet-5", "reasoning_effort": "low"},
        ), mock.patch("diagnose.run_provider_oneshot", return_value=payload) as oneshot:
            result = self.run_diagnose(repos, log_lines)
        oneshot.assert_called_once()
        self.assertTrue(result["ai_available"])
        by_repo = {p["repository"]: p for p in result["problems"]}
        self.assertEqual(by_repo[label_a]["explanation"], "A broke because X.")
        self.assertTrue(by_repo[label_a]["is_bug"])
        self.assertEqual(by_repo[label_a]["provider"], "Claude")
        self.assertFalse(by_repo[label_b]["is_bug"])

    def test_malformed_synthesis_response_falls_back_to_unavailable(self) -> None:
        label = "SWARM-Media-Steaming/swarm"
        lines = self.error_log(label, "Something entirely novel broke")
        with mock.patch.object(
            __import__("swarm_issue_worker").Worker, "provider_usage",
            return_value=ProviderUsage(0, 80),
        ), mock.patch.object(
            __import__("swarm_issue_worker").Worker, "run_router", return_value="{}",
        ), mock.patch.object(
            __import__("swarm_issue_worker").Worker, "resolve_router_response",
            return_value={"provider": "claude", "selected_model": "claude-sonnet-5", "reasoning_effort": "low"},
        ), mock.patch("diagnose.run_provider_oneshot", return_value="not json"):
            result = self.run_diagnose(self.repos_file(label), lines)
        self.assertFalse(result["ai_available"])
        self.assertEqual(result["problems"][0]["source"], "unavailable")

    # ---- network probe gating ----------------------------------------------

    def test_network_probe_only_runs_for_network_shaped_errors(self) -> None:
        label = "SWARM-Media-Steaming/swarm"
        # select_provider returning (None, {}) short-circuits straight to the
        # "unavailable" path, so this test only exercises the gathering step.
        with mock.patch("diagnose.run_network_probe") as probe, \
             mock.patch("diagnose.select_provider", return_value=(None, {})):
            self.run_diagnose(self.repos_file(label), self.error_log(label, "Something entirely novel broke"))
        probe.assert_not_called()

        with mock.patch("diagnose.run_network_probe", return_value="probe output") as probe, \
             mock.patch("diagnose.select_provider", return_value=(None, {})):
            result = self.run_diagnose(
                self.repos_file(label), self.error_log(label, "git@github.com: Permission denied (publickey).")
            )
        probe.assert_called_once()
        sources = [entry["source"] for entry in result["problems"][0]["evidence"]]
        self.assertIn("network probe", sources)

    # ---- app-level catch-all ------------------------------------------------

    def test_unbracketed_errors_are_attributed_to_the_app_itself(self) -> None:
        label = "SWARM-Media-Steaming/swarm"
        lines = [
            f"[{label}] Grok is working. ...",
            f"[{label}] no issue to work right now.",
            "ERROR: Could not fetch updates for the workspace: something unexpected.",
        ]
        with mock.patch("diagnose.select_provider", return_value=(None, {})):
            result = self.run_diagnose(self.repos_file(label), lines)
        self.assertEqual(len(result["problems"]), 1)
        self.assertEqual(result["problems"][0]["repository"], diagnose.APP_REPOSITORY)

    def test_bracketed_repo_errors_are_not_double_counted_as_app_errors(self) -> None:
        label = "SWARM-Media-Steaming/swarm"
        lines = self.error_log(label, "Something entirely novel broke")
        with mock.patch("diagnose.select_provider", return_value=(None, {})):
            result = self.run_diagnose(self.repos_file(label), lines)
        self.assertEqual(len(result["problems"]), 1)
        self.assertEqual(result["problems"][0]["repository"], label)


class FileDiagnosticIssueTests(unittest.TestCase):
    setUp = fixtures.WorkerTestCase.setUp
    tearDown = fixtures.WorkerTestCase.tearDown
    git = fixtures.WorkerTestCase.git
    _worker_argv = fixtures.WorkerTestCase._worker_argv

    def store(self) -> DiagnosticRepository:
        return DiagnosticRepository(self.worker.config.execution_history_db)

    def test_files_an_unassigned_issue_in_the_swarm_automation_repo(self) -> None:
        store = self.store()
        problem_id = store.insert(
            "run-1", diagnose.iso_timestamp(),
            DiagnosticProblem(
                repository="SWARM-Media-Steaming/swarm", signature="sig-1", source="ai",
                explanation="A real bug.", confidence="high", is_bug=True,
                suggested_issue_title="Real bug", suggested_issue_body="Standalone body.",
            ),
        )
        with mock.patch.object(self.worker.github, "gh") as gh:
            gh.side_effect = lambda args, *rest: (
                "[]" if args[:2] == ["issue", "list"] else "https://example.invalid/issues/9"
            )
            result = diagnose.file_diagnostic_issue(store, problem_id, self.worker)
        self.assertEqual(result, {"filed_issue_url": "https://example.invalid/issues/9", "already_filed": False})
        create = next(c for c in gh.call_args_list if c.args[0][:2] == ["issue", "create"])
        arguments = create.args[0]
        self.assertEqual(
            arguments[arguments.index("--repo") + 1], "SWARM-Media-Steaming/swarm-automation"
        )
        self.assertNotIn("--assignee", arguments)
        record = store.get(problem_id)
        assert record is not None
        self.assertEqual(record["filed_issue_url"], "https://example.invalid/issues/9")

    def test_refuses_to_file_a_problem_that_was_not_flagged_as_a_bug(self) -> None:
        store = self.store()
        problem_id = store.insert(
            "run-1", diagnose.iso_timestamp(),
            DiagnosticProblem(repository="swarm", signature="sig-2", source="canned", is_bug=False),
        )
        with self.assertRaises(ValueError):
            diagnose.file_diagnostic_issue(store, problem_id, self.worker)

    def test_dedups_on_signature_instead_of_filing_twice(self) -> None:
        store = self.store()
        problem_id = store.insert(
            "run-1", diagnose.iso_timestamp(),
            DiagnosticProblem(
                repository="SWARM-Media-Steaming/swarm", signature="dup-sig", source="ai", is_bug=True,
                suggested_issue_title="Dup", suggested_issue_body="Body",
            ),
        )
        with mock.patch.object(self.worker.github, "gh") as gh:
            gh.return_value = json.dumps([
                {"url": "https://example.invalid/issues/5",
                 "body": "<!-- swarm-diagnose:signature:dup-sig -->\nAlready filed."}
            ])
            result = diagnose.file_diagnostic_issue(store, problem_id, self.worker)
        self.assertEqual(result, {"filed_issue_url": "https://example.invalid/issues/5", "already_filed": True})
        self.assertFalse(any(c.args[0][:2] == ["issue", "create"] for c in gh.call_args_list))


if __name__ == "__main__":
    unittest.main()
