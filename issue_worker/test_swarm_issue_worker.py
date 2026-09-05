#!/usr/bin/env python3

from __future__ import annotations

import contextlib
import dataclasses
import datetime as dt
import io
import json
import os
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import github_app_auth as auth_module
import install_swarm_issue_cron as runner_module
import setup_github_bots as setup_module
from swarm_issue_worker import (
    Config,
    ISSUE_COMPLETED_EXIT_CODE,
    IssueContext,
    PROVIDER_UNAVAILABLE_EXIT_CODE,
    ProviderChoice,
    ProviderUsage,
    Worker,
    WorkerError,
    build_parser,
    extract_completion_metadata,
    extract_followup_metadata,
    is_worker_comment,
    priority_rank,
)


class WorkerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="swarm-worker-test.")
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo"
        self.state = self.root / "state"
        self.repo.mkdir()
        self.state.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "SWARM worker test")
        self.git("config", "user.email", "worker-test@example.invalid")
        (self.repo / "tracked.txt").write_text("base\n", encoding="utf-8")
        self.git("add", "tracked.txt")
        self.git("commit", "-q", "-m", "base")
        self.base_sha = self.git("rev-parse", "HEAD")
        self.remote = self.root / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", str(self.remote)], check=True)
        self.git("remote", "add", "origin", str(self.remote))
        self.git("push", "-q", "-u", "origin", "main")
        # The AI integration branch the worker cuts issue branches from.
        self.git("branch", "ai-main", "main")
        self.git("push", "-q", "origin", "ai-main")
        args = build_parser().parse_args(self._worker_argv(auto=False))
        self.worker = Worker(Config.from_args(args))

    def _worker_argv(self, auto: bool) -> list[str]:
        return [
            "--repo-dir", str(self.repo), "--state-dir", str(self.state),
            "--gh-bin", "/usr/bin/false", "--claude-bin", "", "--codex-bin", "", "--grok-bin", "",
            "--branch-prefix", "ai", "--integration-branch", "ai-main", "--base-branch", "main",
            # Isolate from any real ~/.config/swarm/github-apps.json on the host
            # so pushes stay local and no GitHub API calls are made.
            "--github-apps-config", str(self.root / "no-github-apps.json"),
            "--auto-approve" if auto else "--no-auto-approve",
            "--auto-merge" if auto else "--no-auto-merge",
            "--no-require-bot-auth",
        ]

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def git(self, *args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(self.repo), *args], text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
        ).stdout.strip()

    def pr_worker(self) -> Worker:
        return Worker(Config.from_args(build_parser().parse_args(self._worker_argv(auto=True))))

    def paused_state(self, issue_number: int = 101) -> dict[str, object]:
        return {
            "issue_number": issue_number, "issue_title": "Paused work",
            "issue_url": f"https://example.invalid/issues/{issue_number}", "base_sha": self.base_sha,
            "branch_name": f"ai/claude/issue-{issue_number}",
            "work_type": "initial", "previous_commit_sha": "", "previous_completion_comment": None,
            "followup_comments": [], "trigger_comment_id": None, "ai_tool": "Claude",
            "model": "test-model", "effort": "high", "session_id": f"session-{issue_number}",
            "session_started": True,
            "session_comment_id": 0, "status": "quota_paused", "quota_pause_count": 1,
            "quota_paused_at": "2026-08-25T10:00:00-05:00", "attempt_start_sha": self.base_sha,
        }

    def test_followup_accepts_human_and_bot_completion_authors(self) -> None:
        comments = [
            {"id": 100, "created_at": "2026-08-25T10:00:00Z", "user": {"login": "swarm-codex-bot[bot]"},
             "body": "<!-- swarm-issue-worker:commit:" + "1" * 40 + " -->\nCompleted by **Codex**."},
            {"id": 101, "created_at": "2026-08-25T10:01:00Z", "user": {"login": "swarm-codex-bot[bot]"},
             "body": "<!-- swarm-issue-worker:quota-paused:issue:71;pause:1;session:test -->\nWork paused."},
            {"id": 102, "created_at": "2026-08-25T10:02:00Z", "user": {"login": "DotNetRockStar"},
             "body": "Please add a disk-usage graph."},
            {"id": 103, "created_at": "2026-08-25T10:03:00Z", "user": {"login": "someone-else"},
             "body": "Untrusted request."},
        ]
        completion_authors = {"DotNetRockStar", "swarm-codex-bot[bot]"}
        followup = extract_followup_metadata(comments, {"DotNetRockStar"}, completion_authors)
        assert followup is not None
        self.assertEqual(followup["trigger_comment_id"], 102)
        self.assertEqual(len(followup["followup_comments"]), 1)
        self.assertEqual(followup["previous_ai"], "Codex")
        completion = extract_completion_metadata(comments, completion_authors)
        assert completion is not None
        self.assertEqual(completion["commit_sha"], "1" * 40)
        self.assertIsNone(extract_completion_metadata(comments, {"someone-else"}))

    def test_environment_only_summary_advances_followup_cursor(self) -> None:
        comments = [
            {"id": 100, "user": {"login": "swarm-claude-bot[bot]"},
             "body": "<!-- swarm-issue-worker:commit:" + "1" * 40 + " -->\nCompleted by **Claude**."},
            {"id": 101, "created_at": "2026-09-04T12:08:47Z",
             "user": {"login": "github-actions[bot]"}, "body": "CI failed."},
            {"id": 102, "user": {"login": "swarm-claude-bot[bot]"},
             "body": "<!-- swarm-issue-worker:environment-only:issue:226;provider:claude;through-comment:101 -->\nReviewed by **Claude** with no code changes."},
        ]
        completion_authors = {"swarm-claude-bot"}
        self.assertIsNone(
            extract_followup_metadata(comments, {"github-actions"}, completion_authors)
        )

        comments.append(
            {"id": 103, "created_at": "2026-09-04T13:00:00Z",
             "user": {"login": "github-actions[bot]"}, "body": "A different CI failure."}
        )
        followup = extract_followup_metadata(comments, {"github-actions"}, completion_authors)
        assert followup is not None
        self.assertEqual(followup["trigger_comment_id"], 103)
        self.assertEqual([item["id"] for item in followup["followup_comments"]], [103])

    def test_followup_author_matches_bot_login_without_suffix(self) -> None:
        # Operators list the CI bot as ``github-actions`` but the API reports it
        # as ``github-actions[bot]`` (and casing may differ) -- either form is
        # honored as a trusted follow-up author.
        comments = [
            {"id": 200, "created_at": "2026-08-25T10:00:00Z", "user": {"login": "swarm-codex-bot[bot]"},
             "body": "<!-- swarm-issue-worker:commit:" + "2" * 40 + " -->\nCompleted by **Codex**."},
            {"id": 201, "created_at": "2026-08-25T10:05:00Z", "user": {"login": "github-actions[bot]"},
             "body": "CI/CD failed on the latest commit; see the workflow run."},
        ]
        followup = extract_followup_metadata(
            comments, {"GitHub-Actions", "DotNetRockStar"}, {"swarm-codex-bot"}
        )
        assert followup is not None
        self.assertEqual(followup["trigger_comment_id"], 201)
        self.assertEqual(followup["followup_comments"][0]["author"], "github-actions[bot]")

    def test_new_issue_prefers_the_provider_with_most_usage_remaining(self) -> None:
        # Fresh issue: the least-drained provider goes first regardless of the
        # preferred-provider setting, so no account is exhausted before the rest.
        fresh = self.worker.choose_provider(
            "", {"Claude": 90.0, "Codex": 40.0, "Grok": 55.0}
        )
        assert fresh is not None
        self.assertEqual(fresh.name, "Claude")
        self.assertTrue(fresh.session_id, "Claude sessions are created up front")

        drained_claude = self.worker.choose_provider(
            "", {"Claude": 12.0, "Codex": 12.0, "Grok": 80.0}
        )
        assert drained_claude is not None
        self.assertEqual(drained_claude.name, "Grok")

    def test_equal_headroom_falls_back_to_the_preferred_provider(self) -> None:
        choice = self.worker.choose_provider(
            "", {"Claude": 50.0, "Codex": 50.0, "Grok": 50.0}
        )
        assert choice is not None
        self.assertEqual(choice.name, "Claude")

    def test_followup_rotates_away_from_the_previous_provider(self) -> None:
        headroom = {"Claude": 90.0, "Codex": 40.0, "Grok": 55.0}

        # Follow-up: the provider that did the previous pass is pushed to the
        # back even when it has the most headroom, so a different enabled
        # provider reviews; the rest keep their most-usage-first order.
        after_claude = self.worker.choose_provider("Claude", headroom)
        assert after_claude is not None
        self.assertEqual(after_claude.name, "Grok")

        after_grok = self.worker.choose_provider("Grok", headroom)
        assert after_grok is not None
        self.assertEqual(after_grok.name, "Claude")

        # Last resort: the previous provider is still used when it is the only
        # one with capacity.
        only_claude = self.worker.choose_provider("Claude", {"Claude": 90.0})
        assert only_claude is not None
        self.assertEqual(only_claude.name, "Claude")

    def test_prompt_policy_toggles_add_issue_instructions(self) -> None:
        self.worker.config = dataclasses.replace(
            self.worker.config,
            require_issue_tests=True,
            allow_environment_only_summary=True,
        )
        self.worker.issue = IssueContext(141, "Policy prompt", "Body", [], "https://example.invalid/141")
        self.worker.choice = ProviderChoice("Codex", "test-model", "high", "")
        self.worker.save_new_state(self.worker.issue, self.worker.choice, self.base_sha)

        prompt = self.worker.build_prompt(False, "", False)

        self.assertIn("add or update UAT and integration tests", prompt)
        self.assertIn("SWARM_ENVIRONMENT_ONLY", prompt)
        self.assertIn("do not write code", prompt)

    def test_missing_ready_label_is_created_and_retried(self) -> None:
        pending = {
            "issue_number": 144,
            "ready_for_testing_label_added": False,
        }
        missing = WorkerError("failed to update issue: 'Ready For Testing' not found")
        with mock.patch.object(
            self.worker.github,
            "gh",
            side_effect=[missing, "", ""],
        ) as github:
            result = self.worker.add_pending_label(pending)

        self.assertTrue(result["ready_for_testing_label_added"])
        self.assertEqual(github.call_count, 3)
        self.assertEqual(github.call_args_list[0].args[0][0:2], ["issue", "edit"])
        self.assertEqual(github.call_args_list[1].args[0][0:2], ["label", "create"])
        self.assertEqual(github.call_args_list[2].args[0][0:2], ["issue", "edit"])

    def test_environment_only_marker_finishes_without_commit(self) -> None:
        self.worker.config = dataclasses.replace(
            self.worker.config,
            allow_environment_only_summary=True,
        )
        self.worker.issue = IssueContext(142, "Env issue", "Body", [], "https://example.invalid/142")

        def fake_run_ai(_prompt: str) -> int:
            self.worker.ai_output_file.write_text(
                "## Summary\nThis needs a missing local service.\nSWARM_ENVIRONMENT_ONLY\n",
                encoding="utf-8",
            )
            return 0

        with (
            mock.patch.object(self.worker, "provider_usage", return_value=ProviderUsage(0, 100.0)),
            mock.patch.object(self.worker, "post_started_comment"),
            mock.patch.object(self.worker, "run_ai", side_effect=fake_run_ai),
            mock.patch.object(self.worker, "comments", return_value=[]),
            mock.patch.object(self.worker.github, "gh", return_value="") as github,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            status = self.worker.run_selected_issue()

        self.assertEqual(status, ISSUE_COMPLETED_EXIT_CODE)
        self.assertIn(142, self.worker.completed_numbers())
        self.assertFalse(self.worker.in_progress_file.exists())
        body = github.call_args.args[2]
        self.assertIn("environment-only", body)
        self.assertNotIn("SWARM_ENVIRONMENT_ONLY", body)

    def test_environment_only_followup_marker_records_trigger_comment(self) -> None:
        self.worker.issue = IssueContext(
            226, "Environment follow-up", "Body", [], "https://example.invalid/226",
            work_type="followup", trigger_comment_id=5540212567,
        )
        self.worker.choice = ProviderChoice("Claude", "test-model", "high", "session-226")
        self.worker.save_new_state(self.worker.issue, self.worker.choice, self.base_sha)
        with (
            mock.patch.object(self.worker, "usage_snapshot", return_value=None),
            mock.patch.object(self.worker, "comments", return_value=[]),
            mock.patch.object(self.worker.github, "gh", return_value="") as github,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.worker.finalize_environment_only("## Summary\nNo code change.")

        body = github.call_args.args[2]
        self.assertIn("through-comment:5540212567", body)

    def test_quota_notice_is_deduplicated_across_pause_counts(self) -> None:
        self.worker.issue = IssueContext(226, "Paused", "", [], "https://example.invalid/226")
        self.worker.choice = ProviderChoice("Claude", "test-model", "high", "session-226")
        state = self.paused_state(226)
        state.update({"quota_pause_count": 2, "quota_comment_posted": False})
        self.worker.write_state(state)
        existing = [{
            "body": "<!-- swarm-issue-worker:quota-paused:issue:226;pause:1;session:session-226 -->\nWork paused."
        }]
        with (
            mock.patch.object(self.worker, "comments", return_value=existing),
            mock.patch.object(self.worker.github, "gh", return_value="") as github,
        ):
            self.worker.post_quota_comment()

        github.assert_not_called()
        self.assertTrue(self.worker.read_state()["quota_comment_posted"])

    def test_saved_issue_can_handoff_to_another_provider(self) -> None:
        self.worker.issue = IssueContext(143, "Handoff", "Body", [], "https://example.invalid/143")
        original = ProviderChoice("Claude", "claude-test", "high", "claude-session")
        self.worker.choice = original
        self.worker.save_new_state(self.worker.issue, original, self.base_sha)
        self.worker.update_state(session_started=True)
        self.git("switch", "-q", "-c", "ai/claude/issue-143")

        def capacity(provider: str) -> int:
            return 0 if provider.lower() == "codex" else 1

        def fake_handoff_run(_prompt: str) -> int:
            self.worker.ai_output_file.write_text("## Summary\nDone.\n", encoding="utf-8")
            return 0

        def fake_handoff_commit(_run_start: str) -> str:
            (self.repo / "handoff.txt").write_text("continued\n", encoding="utf-8")
            self.git("add", "handoff.txt")
            self.git("commit", "-q", "-m", "[codex] Continue handoff (#143)")
            return self.git("rev-parse", "HEAD")

        with (
            mock.patch.object(self.worker, "provider_capacity", side_effect=capacity),
            mock.patch.object(self.worker, "prepare_repository", return_value=(self.base_sha, False, "", False)),
            mock.patch.object(self.worker, "post_started_comment"),
            mock.patch.object(self.worker, "run_ai", side_effect=fake_handoff_run),
            mock.patch.object(self.worker, "commit_completed_work", side_effect=fake_handoff_commit),
            mock.patch.object(self.worker, "ensure_issue_reference", side_effect=lambda sha, _recovered: sha),
            mock.patch.object(self.worker, "validate_new_commit_messages"),
            mock.patch.object(self.worker, "finalize_issue"),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(self.worker.run_selected_issue(), ISSUE_COMPLETED_EXIT_CODE)

        state = self.worker.read_state()
        self.assertEqual(self.worker.choice.name, "Codex")
        self.assertEqual(state["ai_tool"], "Codex")
        self.assertEqual(state["branch_name"], "ai/claude/issue-143")
        self.assertFalse(state["session_started"])

    def test_grok_capacity_reflects_install_and_sign_in(self) -> None:
        # Empty --grok-bin in setUp -> not installed -> unavailable.
        self.assertEqual(self.worker.grok_capacity(), 2)

    def test_codex_capacity_retries_one_transient_failure(self) -> None:
        failed = subprocess.CompletedProcess(
            ["codex-rate-limits"], 1, stdout="", stderr="temporary app-server timeout"
        )
        succeeded = subprocess.CompletedProcess(
            ["codex-rate-limits"],
            0,
            stdout=json.dumps(
                {
                    "primary": {"usedPercent": 55},
                    "secondary": {"usedPercent": 29},
                    "rateLimitReachedType": None,
                    "spendControlReached": False,
                }
            ),
            stderr="",
        )
        with (
            mock.patch.object(self.worker, "provider_bin", return_value="/test/codex"),
            mock.patch("swarm_issue_worker.command_available", return_value=True),
            mock.patch("swarm_issue_worker.run_command", side_effect=[failed, succeeded]) as run,
            mock.patch("swarm_issue_worker.time.sleep"),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(self.worker.codex_capacity(), 0)
        self.assertEqual(run.call_count, 2)
        self.assertIn("--timeout", run.call_args.args[0])

    def test_queued_issue_without_provider_capacity_has_distinct_status(self) -> None:
        self.worker.issue = IssueContext(137, "Queued work", "", [], "https://example.invalid/137")
        with (
            mock.patch.object(self.worker, "provider_usage", return_value=ProviderUsage(1, 3.0)),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            status = self.worker.run_selected_issue()
        self.assertEqual(status, PROVIDER_UNAVAILABLE_EXIT_CODE)

    def test_grok_issue_branch_name(self) -> None:
        worker = self.pr_worker()
        worker.choice = ProviderChoice("Grok", "grok-4.6", "high", "session")
        worker.issue = IssueContext(7, "t", "", [], "https://example.invalid/7")
        worker.issue.work_type = "followup"
        worker.issue.trigger_comment_id = 3
        self.assertEqual(worker.expected_branch(), "ai/xai/issue-7")

    def test_previous_ai_regex_parses_grok(self) -> None:
        from swarm_issue_worker import PREVIOUS_AI_RE

        self.assertEqual(
            PREVIOUS_AI_RE.search("Reworked by **Grok**.").group(1), "Grok"
        )

    def test_choice_from_state_does_not_resume_a_session_that_never_started(self) -> None:
        # prepare_repository persists a freshly-generated Claude session_id
        # before run_ai ever invokes `claude` with it (e.g. so a retry after
        # post_started_comment fails still knows which ID to assign). A
        # crash in that window must not make the next attempt think there's
        # a real session to --resume — regression test for that exact bug.
        state = {
            "ai_tool": "Claude", "model": "test-model", "effort": "high",
            "session_id": "never-actually-started",
        }
        choice = self.worker.choice_from_state(state)
        self.assertEqual(choice.session_id, "never-actually-started")
        self.assertFalse(choice.resume)

    def test_choice_from_state_resumes_once_session_started_is_recorded(self) -> None:
        state = {
            "ai_tool": "Claude", "model": "test-model", "effort": "high",
            "session_id": "genuinely-running", "session_started": True,
        }
        choice = self.worker.choice_from_state(state)
        self.assertTrue(choice.resume)

    @staticmethod
    def issue_payload(number: int, labels: tuple[str, ...] = ()) -> dict[str, object]:
        return {
            "number": number,
            "title": f"Issue {number}",
            "body": "",
            "labels": [{"name": name} for name in labels],
            "assignees": [{"login": "DotNetRockStar"}],
            "html_url": f"https://example.invalid/{number}",
            "created_at": f"2026-08-{number % 28 + 1:02d}T00:00:00Z",
        }

    def test_assigned_issues_are_sorted_by_number_not_api_or_timestamp_order(self) -> None:
        issues = [self.issue_payload(55), self.issue_payload(50), self.issue_payload(53)]
        with mock.patch.object(self.worker.github, "api_list", return_value=issues):
            selected = self.worker.assigned_issues()
        self.assertEqual([int(issue["number"]) for issue in selected], [50, 53, 55])

    def test_priority_rank_reads_common_label_spellings(self) -> None:
        self.assertEqual(priority_rank(["priority: urgent"]), 0)
        self.assertEqual(priority_rank(["Priority/High"]), 1)
        self.assertEqual(priority_rank(["medium"]), 2)
        self.assertEqual(priority_rank(["P3"]), 3)
        # No recognized priority label -> treated as Low.
        self.assertEqual(priority_rank(["bug", "enhancement"]), 3)
        self.assertEqual(priority_rank([]), 3)
        # Strongest label wins when several are present.
        self.assertEqual(priority_rank(["low", "priority: high", "medium"]), 1)

    def test_higher_priority_issue_is_selected_before_lower_numbered_one(self) -> None:
        issues = [
            self.issue_payload(20, labels=("priority: medium",)),
            self.issue_payload(90, labels=("priority: urgent",)),
            self.issue_payload(100, labels=("priority: high",)),
        ]
        with (
            mock.patch.object(self.worker, "assigned_issues", return_value=issues),
            mock.patch.object(self.worker, "comments", return_value=[]),
        ):
            selected = self.worker.select_issue()
        assert selected is not None
        self.assertEqual(selected.number, 90)

    def test_unprioritized_issue_loses_to_prioritized_higher_number(self) -> None:
        issues = [
            self.issue_payload(10),
            self.issue_payload(200, labels=("priority: high",)),
        ]
        with (
            mock.patch.object(self.worker, "assigned_issues", return_value=issues),
            mock.patch.object(self.worker, "comments", return_value=[]),
        ):
            selected = self.worker.select_issue()
        assert selected is not None
        self.assertEqual(selected.number, 200)

    def test_equal_priority_issues_keep_lowest_number_first(self) -> None:
        issues = [
            self.issue_payload(75, labels=("priority: low",)),
            self.issue_payload(40, labels=("priority: low",)),
        ]
        with (
            mock.patch.object(self.worker, "assigned_issues", return_value=issues),
            mock.patch.object(self.worker, "comments", return_value=[]),
        ):
            selected = self.worker.select_issue()
        assert selected is not None
        self.assertEqual(selected.number, 40)

    def test_lower_fresh_issue_beats_higher_followup_issue(self) -> None:
        self.worker.completed_file.write_text("55\n", encoding="utf-8")
        issues = [self.issue_payload(55), self.issue_payload(50)]
        followup_comments = [
            {
                "id": 100,
                "created_at": "2026-08-20T00:00:00Z",
                "user": {"login": "DotNetRockStar"},
                "body": "<!-- swarm-issue-worker:commit:" + "1" * 40 + " -->\nCompleted by **Codex**.",
            },
            {
                "id": 101,
                "created_at": "2026-08-21T00:00:00Z",
                "user": {"login": "DotNetRockStar"},
                "body": "Please revisit this.",
            },
        ]
        with (
            mock.patch.object(self.worker, "assigned_issues", return_value=issues),
            mock.patch.object(
                self.worker,
                "comments",
                side_effect=lambda number: followup_comments if number == 55 else [],
            ),
        ):
            selected = self.worker.select_issue()
        assert selected is not None
        self.assertEqual(selected.number, 50)
        self.assertEqual(selected.work_type, "initial")

    def test_lower_followup_issue_beats_higher_fresh_issue(self) -> None:
        self.worker.completed_file.write_text("50\n", encoding="utf-8")
        issues = [self.issue_payload(55), self.issue_payload(50)]
        followup_comments = [
            {
                "id": 200,
                "created_at": "2026-08-20T00:00:00Z",
                "user": {"login": "DotNetRockStar"},
                "body": "<!-- swarm-issue-worker:commit:" + "2" * 40 + " -->\nCompleted by **Claude**.",
            },
            {
                "id": 201,
                "created_at": "2026-08-21T00:00:00Z",
                "user": {"login": "DotNetRockStar"},
                "body": "Please revisit this first.",
            },
        ]
        with (
            mock.patch.object(self.worker, "assigned_issues", return_value=issues),
            mock.patch.object(
                self.worker,
                "comments",
                side_effect=lambda number: followup_comments if number == 50 else [],
            ),
        ):
            selected = self.worker.select_issue()
        assert selected is not None
        self.assertEqual(selected.number, 50)
        self.assertEqual(selected.work_type, "followup")

    def test_pause_shelves_and_restore_preserves_newer_commit(self) -> None:
        # The paused issue owns its own branch.
        self.git("switch", "-q", "-c", "ai/claude/issue-101")
        self.worker.write_state(self.paused_state())
        (self.repo / "tracked.txt").write_text("base\npaused change\n", encoding="utf-8")
        (self.repo / "untracked.txt").write_text("untracked change\n", encoding="utf-8")
        self.worker.suspend_paused()
        paused_file = self.worker.paused_dir / "101.json"
        self.assertTrue(paused_file.is_file())
        self.assertFalse(self.worker.in_progress_file.exists())
        self.assertEqual(self.git("status", "--porcelain"), "")
        # A newer commit lands on the issue branch while it is shelved.
        self.git("switch", "-q", "ai/claude/issue-101")
        (self.repo / "other.txt").write_text("other issue\n", encoding="utf-8")
        self.git("add", "other.txt")
        self.git("commit", "-q", "-m", "other issue")
        newer_sha = self.git("rev-parse", "HEAD")
        self.git("switch", "-q", "ai-main")
        self.worker.restore_paused(paused_file)
        self.assertEqual(self.git("rev-parse", "HEAD"), newer_sha)
        self.assertIn("paused change", (self.repo / "tracked.txt").read_text())
        self.assertEqual((self.repo / "untracked.txt").read_text(), "untracked change\n")

    def test_closed_shelved_pause_is_archived_without_ai_or_quota_check(self) -> None:
        self.worker.paused_dir.mkdir()
        paused_file = self.worker.paused_dir / "101.json"
        self.worker.write_state(self.paused_state(), paused_file)

        with (
            mock.patch.object(self.worker, "issue_is_closed", return_value=True),
            mock.patch.object(self.worker, "provider_capacity") as capacity,
            mock.patch.object(self.worker, "restore_paused") as restore,
        ):
            self.assertFalse(self.worker.prepare_paused_resume())

        capacity.assert_not_called()
        restore.assert_not_called()
        self.assertFalse(paused_file.exists())
        archive = self.worker.closed_paused_dir / "101.json"
        self.assertTrue(archive.is_file())
        archived_state = self.worker.read_state(archive)
        self.assertEqual(archived_state["status"], "closed_while_paused")
        self.assertEqual(archived_state["archived_from"], "101.json")

    def test_closed_in_progress_pause_shelves_work_and_returns_to_main_without_ai(self) -> None:
        worker = self.pr_worker()
        worker.issue = IssueContext(102, "Closed pause", "", [], "https://example.invalid/102")
        worker.choice = ProviderChoice("Claude", "test", "high", "session-102")
        worker.prepare_repository()
        worker.update_state(
            status="quota_paused",
            quota_pause_count=1,
            quota_paused_at="2026-08-25T10:00:00-05:00",
        )
        (self.repo / "paused.txt").write_text("preserve this work\n", encoding="utf-8")

        with (
            mock.patch.object(worker, "issue_is_closed", return_value=True),
            mock.patch.object(worker, "post_quota_comment") as quota_comment,
            mock.patch.object(worker, "provider_capacity") as capacity,
        ):
            self.assertFalse(worker.prepare_paused_resume())

        quota_comment.assert_not_called()
        capacity.assert_not_called()
        self.assertEqual(self.git("branch", "--show-current"), "ai-main")
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertFalse(worker.in_progress_file.exists())
        archive = worker.closed_paused_dir / "102.json"
        archived_state = worker.read_state(archive)
        self.assertEqual(archived_state["status"], "closed_while_paused")
        self.assertTrue(archived_state["worktree_stash_oid"])
        self.assertTrue(self.git("cat-file", "-e", archived_state["worktree_stash_oid"] + "^{commit}") == "")

    def test_closed_pause_does_not_prevent_fresh_selection_after_reopen(self) -> None:
        self.worker.paused_dir.mkdir()
        paused_file = self.worker.paused_dir / "103.json"
        self.worker.write_state(self.paused_state(103), paused_file)
        with mock.patch.object(self.worker, "issue_is_closed", return_value=True):
            self.assertFalse(self.worker.prepare_paused_resume())

        issue = self.issue_payload(103)
        with (
            mock.patch.object(self.worker, "assigned_issues", return_value=[issue]),
            mock.patch.object(self.worker, "comments", return_value=[]),
        ):
            selected = self.worker.select_issue()
        assert selected is not None
        self.assertEqual(selected.number, 103)
        self.assertEqual(selected.work_type, "initial")

    def test_issue_is_closed_reads_current_github_state(self) -> None:
        with mock.patch.object(
            self.worker.github, "gh", return_value=json.dumps({"number": 104, "state": "closed"})
        ) as gh:
            self.assertTrue(self.worker.issue_is_closed(104))
        gh.assert_called_once_with(
            ["api", "--method", "GET", "repos/DotNetRockStar/swarm/issues/104"]
        )

    def test_auto_approval_also_merges_an_open_issue_pull_request(self) -> None:
        worker = self.pr_worker()
        pull_requests = json.dumps(
            [
                {
                    "url": "https://example.invalid/pull/105",
                    "state": "OPEN",
                    "headRefName": "ai/claude/issue-105",
                    "headRefOid": "1" * 40,
                    "isDraft": False,
                    "mergeable": "MERGEABLE",
                }
            ]
        )
        with (
            mock.patch.object(worker.github, "gh", return_value=pull_requests),
            mock.patch.object(worker, "issue_is_closed", return_value=False) as issue_is_closed,
            mock.patch.object(worker, "approve_pull_request") as approve,
            mock.patch.object(worker, "merge_pull_request", return_value="2" * 40) as merge,
            mock.patch.object(worker, "delete_remote_issue_branch") as delete,
        ):
            worker.reconcile_issue_pull_requests()
        approve.assert_called_once_with("https://example.invalid/pull/105", "claude")
        merge.assert_called_once_with(
            "https://example.invalid/pull/105", "1" * 40, "claude", 105
        )
        delete.assert_called_once_with("ai/claude/issue-105", "claude")
        issue_is_closed.assert_not_called()

    def test_auto_approval_leaves_a_conflicting_pull_request_open(self) -> None:
        worker = self.pr_worker()
        pull_requests = json.dumps(
            [
                {
                    "url": "https://example.invalid/pull/106",
                    "state": "OPEN",
                    "headRefName": "ai/codex/issue-106",
                    "headRefOid": "2" * 40,
                    "isDraft": False,
                    "mergeable": "CONFLICTING",
                }
            ]
        )
        with (
            mock.patch.object(worker.github, "gh", return_value=pull_requests),
            mock.patch.object(worker, "approve_pull_request") as approve,
            mock.patch.object(worker, "merge_pull_request") as merge,
            mock.patch.object(worker, "delete_remote_issue_branch") as delete,
        ):
            worker.reconcile_issue_pull_requests()
        approve.assert_called_once_with("https://example.invalid/pull/106", "codex")
        merge.assert_not_called()
        delete.assert_not_called()

    def test_closed_merged_pull_request_prunes_its_stale_remote_branch(self) -> None:
        # Cleanup is a safety reconciliation, not opt-in auto-merge behavior.
        worker = self.worker
        pull_requests = json.dumps(
            [
                {
                    "url": "https://example.invalid/pull/109",
                    "state": "MERGED",
                    "headRefName": "ai/codex/issue-109",
                    "headRefOid": "7" * 40,
                    "isDraft": False,
                    "mergeable": "UNKNOWN",
                    "reviewDecision": "APPROVED",
                }
            ]
        )
        with (
            mock.patch.object(worker.github, "gh", return_value=pull_requests),
            mock.patch.object(worker, "git_ok", return_value=True),
            mock.patch.object(worker, "issue_is_closed", return_value=True),
            mock.patch.object(worker, "delete_remote_issue_branch") as delete,
            mock.patch.object(worker, "merge_pull_request") as merge,
        ):
            worker.reconcile_issue_pull_requests()
        delete.assert_called_once_with("ai/codex/issue-109", "codex")
        merge.assert_not_called()

    def test_merged_pull_request_keeps_branch_until_issue_is_closed(self) -> None:
        pull_requests = json.dumps(
            [
                {
                    "url": "https://example.invalid/pull/110",
                    "state": "MERGED",
                    "headRefName": "ai/claude/issue-110",
                    "headRefOid": "8" * 40,
                    "isDraft": False,
                    "mergeable": "UNKNOWN",
                    "reviewDecision": "APPROVED",
                }
            ]
        )
        with (
            mock.patch.object(self.worker.github, "gh", return_value=pull_requests),
            mock.patch.object(self.worker, "git_ok", return_value=True),
            mock.patch.object(self.worker, "issue_is_closed", return_value=False),
            mock.patch.object(self.worker, "delete_remote_issue_branch") as delete,
        ):
            self.worker.reconcile_issue_pull_requests()
        delete.assert_not_called()

    def test_merged_pull_request_skips_cleanup_when_remote_branch_is_already_gone(self) -> None:
        pull_requests = json.dumps(
            [
                {
                    "url": "https://example.invalid/pull/111",
                    "state": "MERGED",
                    "headRefName": "ai/codex/issue-111",
                    "headRefOid": "9" * 40,
                    "isDraft": False,
                    "mergeable": "UNKNOWN",
                    "reviewDecision": "APPROVED",
                }
            ]
        )
        with (
            mock.patch.object(self.worker.github, "gh", return_value=pull_requests),
            mock.patch.object(self.worker, "git_ok", return_value=False),
            mock.patch.object(self.worker, "issue_is_closed") as issue_is_closed,
            mock.patch.object(self.worker, "delete_remote_issue_branch") as delete,
        ):
            self.worker.reconcile_issue_pull_requests()
        issue_is_closed.assert_not_called()
        delete.assert_not_called()

    def test_remote_branch_deletion_rejects_non_issue_branch(self) -> None:
        with mock.patch.object(self.worker, "push_ref") as push:
            with self.assertRaisesRegex(WorkerError, "unexpected branch name"):
                self.worker.delete_remote_issue_branch("ai-main", "codex")
        push.assert_not_called()

    def test_merge_helper_does_not_require_issue_closure(self) -> None:
        worker = self.pr_worker()
        merge_sha = "5" * 40
        with (
            mock.patch.object(worker, "issue_is_closed", return_value=False) as issue_is_closed,
            mock.patch.object(worker.github, "gh", side_effect=["", merge_sha, ""]) as gh,
        ):
            result = worker.merge_pull_request(
                "https://example.invalid/pull/107", "4" * 40, "claude", 107
            )
            issue_is_closed.assert_not_called()
        self.assertEqual(result, merge_sha)
        self.assertEqual(gh.call_args_list[0].args[0][:2], ["pr", "merge"])

    def test_issue_pr_merge_comments_without_closing_the_issue(self) -> None:
        worker = self.pr_worker()
        merge_sha = "5" * 40
        with (
            mock.patch.object(worker, "issue_is_closed", return_value=True),
            mock.patch.object(worker.github, "gh", side_effect=["", merge_sha, ""]) as gh,
        ):
            result = worker.merge_pull_request(
                "https://example.invalid/pull/108", "6" * 40, "codex", 108
            )
        self.assertEqual(result, merge_sha)
        commands = [call.args[0] for call in gh.call_args_list]
        self.assertEqual(commands[0][:2], ["pr", "merge"])
        self.assertEqual(commands[2][:2], ["issue", "comment"])
        self.assertNotIn(["issue", "close"], [command[:2] for command in commands])

    def test_damaged_recovery_sha_is_repaired_by_unique_prefix(self) -> None:
        (self.repo / "tracked.txt").write_text("base\nissue work\n", encoding="utf-8")
        self.git("add", "tracked.txt")
        self.git("commit", "-q", "-m", "issue work #202")
        candidate = self.git("rev-parse", "HEAD")
        state = self.paused_state(202)
        state["ai_tool"] = "Codex"
        state["candidate_sha"] = candidate[:8] + "0" * 32
        state["attempt_start_sha"] = candidate
        self.worker.write_state(state)
        normalized = self.worker.normalize_recovery_commits(self.worker.in_progress_file, candidate)
        self.assertEqual(normalized["candidate_sha"], candidate)

    def test_recovery_records_existing_candidate_with_dirty_worktree(self) -> None:
        self.git("switch", "-q", "-c", "ai/codex/issue-303")
        self.worker.issue = IssueContext(303, "Recovery", "", [], "https://example.invalid/303")
        self.worker.choice = ProviderChoice("Codex", "test", "high", "session", True)
        self.worker.save_new_state(self.worker.issue, self.worker.choice, self.base_sha)
        (self.repo / "tracked.txt").write_text("base\nfixed\n", encoding="utf-8")
        self.git("add", "tracked.txt")
        self.git("commit", "-q", "-m", "complete issue #303")
        (self.repo / "unrelated.txt").write_text("unrelated\n", encoding="utf-8")
        run_start, recovery, candidate, dirty = self.worker.prepare_repository()
        self.assertTrue(recovery)
        self.assertTrue(dirty)
        self.assertEqual(candidate, run_start)
        self.assertEqual(self.worker.read_state()["candidate_sha"], run_start)

    def test_pr_state_exists_before_branch_creation_and_recreates_interrupted_branch(self) -> None:
        worker = self.pr_worker()
        worker.issue = IssueContext(401, "Transactional branch", "", [], "https://example.invalid/401")
        worker.choice = ProviderChoice("Codex", "test", "high", "session")
        original_git = worker.git

        def interrupt_branch_creation(*arguments: str, **kwargs: object) -> str:
            if arguments[:2] == ("switch", "-c"):
                raise RuntimeError("simulated interruption")
            return original_git(*arguments, **kwargs)

        with mock.patch.object(worker, "git", side_effect=interrupt_branch_creation):
            with self.assertRaisesRegex(RuntimeError, "simulated interruption"):
                worker.prepare_repository()
        state = worker.read_state()
        self.assertEqual(state["branch_name"], "ai/codex/issue-401")
        self.assertEqual(state["base_sha"], self.git("rev-parse", "ai-main"))
        self.assertEqual(self.git("branch", "--show-current"), "ai-main")

        run_start, recovery, candidate, dirty = worker.prepare_repository()
        self.assertTrue(recovery)
        self.assertFalse(candidate)
        self.assertFalse(dirty)
        self.assertEqual(run_start, state["base_sha"])
        self.assertEqual(self.git("branch", "--show-current"), "ai/codex/issue-401")

    def test_linked_issue_branch_uses_github_graphql_mutation(self) -> None:
        self.worker.issue = IssueContext(419, "Linked branch", "", [], "https://example.invalid/419")
        self.worker.choice = ProviderChoice("Codex", "test", "high", "session")
        issue_id = "I_kwDOExample"
        with mock.patch.object(
            self.worker.github,
            "gh",
            side_effect=[
                json.dumps({"node_id": issue_id}),
                json.dumps(
                    {"data": {"createLinkedBranch": {"issue": {"id": issue_id}}}}
                ),
            ],
        ) as github:
            self.worker.create_linked_issue_branch(
                "ai/codex/issue-419", "1" * 40
            )

        self.assertEqual(
            github.call_args_list[0].args,
            (["api", "--method", "GET", "repos/DotNetRockStar/swarm/issues/419"], "codex"),
        )
        mutation_args = github.call_args_list[1].args[0]
        self.assertEqual(mutation_args[:2], ["api", "graphql"])
        self.assertIn(f"issueId={issue_id}", mutation_args)
        self.assertIn("oid=" + "1" * 40, mutation_args)
        self.assertIn("name=ai/codex/issue-419", mutation_args)

    def test_fresh_github_branch_is_created_from_the_issue(self) -> None:
        worker = self.pr_worker()
        worker.issue = IssueContext(420, "Linked branch", "", [], "https://example.invalid/420")
        worker.choice = ProviderChoice("Claude", "test", "high", "session")
        with (
            mock.patch.object(worker, "remote_is_github_host", return_value=True),
            mock.patch.object(worker, "create_linked_issue_branch") as create_linked,
        ):
            run_start, recovery, _, _ = worker.prepare_repository()

        self.assertFalse(recovery)
        create_linked.assert_called_once_with("ai/claude/issue-420", run_start)
        self.assertTrue(worker.read_state()["branch_linked"])
        self.assertEqual(self.git("branch", "--show-current"), "ai/claude/issue-420")

    def test_followup_recreated_github_branch_is_linked_to_the_issue(self) -> None:
        worker = self.pr_worker()
        worker.issue = IssueContext(
            421,
            "Recreated linked branch",
            "",
            [],
            "https://example.invalid/421",
            work_type="followup",
            previous_commit_sha=self.base_sha,
        )
        worker.choice = ProviderChoice("Codex", "test", "high", "session")
        with (
            mock.patch.object(worker, "remote_is_github_host", return_value=True),
            mock.patch.object(worker, "create_linked_issue_branch") as create_linked,
        ):
            run_start, _, _, _ = worker.prepare_repository()

        create_linked.assert_called_once_with("ai/codex/issue-421", run_start)
        self.assertTrue(worker.read_state()["branch_linked"])

    def test_fresh_pr_branch_fast_forwards_main_before_branching(self) -> None:
        updater = self.root / "updater"
        subprocess.run(
            ["git", "clone", "-q", "--branch", "main", str(self.remote), str(updater)], check=True
        )
        subprocess.run(["git", "-C", str(updater), "config", "user.name", "updater"], check=True)
        subprocess.run(
            ["git", "-C", str(updater), "config", "user.email", "updater@example.invalid"], check=True
        )
        (updater / "remote.txt").write_text("new main work\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(updater), "add", "remote.txt"], check=True)
        subprocess.run(["git", "-C", str(updater), "commit", "-q", "-m", "remote update"], check=True)
        subprocess.run(["git", "-C", str(updater), "push", "-q", "origin", "main"], check=True)
        remote_main = subprocess.run(
            ["git", "-C", str(updater), "rev-parse", "HEAD"], text=True,
            stdout=subprocess.PIPE, check=True,
        ).stdout.strip()

        worker = self.pr_worker()
        worker.issue = IssueContext(402, "Fresh base", "", [], "https://example.invalid/402")
        worker.choice = ProviderChoice("Claude", "test", "high", "session")
        run_start, recovery, _, _ = worker.prepare_repository()
        self.assertFalse(recovery)
        self.assertEqual(run_start, remote_main)
        self.assertEqual(self.git("rev-parse", "main"), remote_main)
        self.assertEqual(self.git("branch", "--show-current"), "ai/claude/issue-402")

    def test_conflicting_main_sync_refuses_to_cut_an_issue_branch(self) -> None:
        self.git("switch", "-q", "ai-main")
        (self.repo / "shared.txt").write_text("integration\n", encoding="utf-8")
        self.git("add", "shared.txt")
        self.git("commit", "-q", "-m", "integration change")
        self.git("push", "-q", "origin", "ai-main")

        updater = self.root / "conflicting-main"
        subprocess.run(
            ["git", "clone", "-q", "--branch", "main", str(self.remote), str(updater)],
            check=True,
        )
        subprocess.run(["git", "-C", str(updater), "config", "user.name", "Other"], check=True)
        subprocess.run(["git", "-C", str(updater), "config", "user.email", "other@example.com"], check=True)
        (updater / "shared.txt").write_text("main\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(updater), "add", "shared.txt"], check=True)
        subprocess.run(["git", "-C", str(updater), "commit", "-q", "-m", "main change"], check=True)
        subprocess.run(["git", "-C", str(updater), "push", "-q", "origin", "main"], check=True)

        worker = self.pr_worker()
        worker.issue = IssueContext(413, "Conflicting parity", "", [], "https://example.invalid/413")
        worker.choice = ProviderChoice("Claude", "test", "high", "session")
        with self.assertRaisesRegex(WorkerError, "refusing to create an issue branch"):
            worker.prepare_repository()
        self.assertEqual(self.git("branch", "--show-current"), "ai-main")
        self.assertNotIn(
            "ai/claude/issue-413",
            self.git("branch", "--format=%(refname:short)").splitlines(),
        )

    def test_worker_commits_uncommitted_completed_work_with_the_tool_prefix(self) -> None:
        worker = self.pr_worker()
        worker.issue = IssueContext(403, "Commit completed files", "", [], "https://example.invalid/403")
        worker.choice = ProviderChoice("Codex", "test", "high", "session")
        run_start, _, _, _ = worker.prepare_repository()
        (self.repo / "completed.txt").write_text("done\n", encoding="utf-8")
        committed = worker.commit_completed_work(run_start)
        self.assertNotEqual(committed, run_start)
        self.assertEqual(self.git("status", "--porcelain"), "")
        subject = self.git("log", "-1", "--format=%s")
        self.assertTrue(subject.startswith("[codex] "), subject)
        self.assertIn("#403", subject)

    def test_worker_refuses_to_push_an_untagged_commit_from_a_multi_commit_run(self) -> None:
        worker = self.pr_worker()
        worker.issue = IssueContext(412, "Tagged history", "", [], "https://example.invalid/412")
        worker.choice = ProviderChoice("Codex", "test", "high", "session")
        run_start, _, _, _ = worker.prepare_repository()
        (self.repo / "first.txt").write_text("first\n", encoding="utf-8")
        self.git("add", "first.txt")
        self.git("commit", "-q", "-m", "missing provider tag (#412)")
        (self.repo / "second.txt").write_text("second\n", encoding="utf-8")
        self.git("add", "second.txt")
        self.git("commit", "-q", "-m", "[codex] tagged commit (#412)")
        completion = self.git("rev-parse", "HEAD")

        with self.assertRaisesRegex(WorkerError, r"required \[codex\] prefix"):
            worker.validate_new_commit_messages(run_start, completion)

    def test_issue_branch_is_cut_from_the_integration_branch(self) -> None:
        worker = self.pr_worker()
        worker.issue = IssueContext(410, "From ai-main", "", [], "https://example.invalid/410")
        worker.choice = ProviderChoice("Claude", "test", "high", "session")
        run_start, recovery, _, _ = worker.prepare_repository()
        self.assertFalse(recovery)
        self.assertEqual(self.git("branch", "--show-current"), "ai/claude/issue-410")
        # The branch descends from ai-main, and ai-main contains main.
        self.assertEqual(run_start, self.git("rev-parse", "ai-main"))
        self.assertTrue(
            worker.git_ok("merge-base", "--is-ancestor", "main", "ai/claude/issue-410")
        )

    def test_followup_reworks_after_branch_was_merged_into_integration(self) -> None:
        # First pass creates + pushes the branch, then it is merged into
        # ai-main (a real merge commit, as `gh pr merge --merge` produces) and
        # the follow-up records that *merge commit* as previous_commit_sha.
        first = self.pr_worker()
        first.issue = IssueContext(415, "Merged then reworked", "", [], "https://example.invalid/415")
        first.choice = ProviderChoice("Claude", "test", "high", "session")
        run_start, _, _, _ = first.prepare_repository()
        (self.repo / "one.txt").write_text("first pass\n", encoding="utf-8")
        branch_tip = first.commit_completed_work(run_start)
        branch = first.expected_branch()
        self.git("push", "-q", "origin", branch)
        self.git("switch", "-q", "ai-main")
        self.git("merge", "-q", "--no-ff", "-m", f"Merge {branch}", branch)
        merge_commit = self.git("rev-parse", "HEAD")
        self.git("push", "-q", "origin", "ai-main")
        first.in_progress_file.unlink()

        second = self.pr_worker()
        second.issue = IssueContext(415, "Merged then reworked", "", [], "https://example.invalid/415")
        second.issue.work_type = "followup"
        second.issue.trigger_comment_id = 99
        # The bug: previous_commit_sha is the PR merge commit, which is a
        # descendant of the resumed branch tip, never an ancestor.
        second.issue.previous_commit_sha = merge_commit
        second.choice = ProviderChoice("Codex", "test", "high", "")
        second.prepare_repository()

        self.assertEqual(second.expected_branch(), branch)
        self.assertEqual(self.git("branch", "--show-current"), branch)
        # The resumed branch was fast-forwarded to the integration branch, so
        # the previous work (and the merge commit) is now present.
        self.assertTrue(
            second.git_ok("merge-base", "--is-ancestor", merge_commit, "HEAD")
        )
        self.assertTrue(
            second.git_ok("merge-base", "--is-ancestor", branch_tip, "HEAD")
        )

    def test_a_second_provider_follow_up_reuses_the_same_branch(self) -> None:
        # First pass by Claude creates the branch and pushes it.
        first = self.pr_worker()
        first.issue = IssueContext(411, "Reuse branch", "", [], "https://example.invalid/411")
        first.choice = ProviderChoice("Claude", "test", "high", "session")
        run_start, _, _, _ = first.prepare_repository()
        (self.repo / "one.txt").write_text("first pass\n", encoding="utf-8")
        first_commit = first.commit_completed_work(run_start)
        branch = first.expected_branch()
        self.assertEqual(branch, "ai/claude/issue-411")
        self.git("push", "-q", "origin", branch)
        # Reset local state as if a fresh scheduler cycle picked up a follow-up.
        first.in_progress_file.unlink()
        self.git("switch", "-q", "ai-main")
        self.git("branch", "-D", branch)

        second = self.pr_worker()
        second.issue = IssueContext(411, "Reuse branch", "", [], "https://example.invalid/411")
        second.issue.work_type = "followup"
        second.issue.trigger_comment_id = 99
        second.issue.previous_commit_sha = first_commit
        second.choice = ProviderChoice("Codex", "test", "high", "")
        second.prepare_repository()
        # Same branch name (Claude's), and the previous commit is present.
        self.assertEqual(second.expected_branch(), "ai/claude/issue-411")
        self.assertEqual(self.git("branch", "--show-current"), "ai/claude/issue-411")
        self.assertTrue(second.git_ok("cat-file", "-e", f"{first_commit}^{{commit}}"))

    def test_start_comment_is_posted_once_by_selected_provider(self) -> None:
        self.worker.issue = IssueContext(407, "Start notice", "", [], "https://example.invalid/407")
        self.worker.choice = ProviderChoice("Codex", "test-model", "high", "session")
        self.worker.save_new_state(self.worker.issue, self.worker.choice, self.base_sha)
        with (
            mock.patch.object(self.worker, "comments", return_value=[]),
            mock.patch.object(self.worker.github, "gh", return_value="") as github,
        ):
            self.worker.post_started_comment()
            self.worker.post_started_comment()
        github.assert_called_once()
        arguments, provider, body = github.call_args.args
        self.assertEqual(provider, "codex")
        self.assertIn("issue", arguments)
        self.assertIn("**Codex Bot** started working on this issue", body)
        self.assertIn("- Model: `test-model`", body)
        self.assertIn("- Branch: `ai/codex/issue-407`", body)
        self.assertTrue(is_worker_comment({"body": body}))
        self.assertTrue(self.worker.read_state()["started_comment_posted"])

    def test_start_comment_reports_provider_usage_remaining(self) -> None:
        self.worker.issue = IssueContext(410, "Usage notice", "", [], "https://example.invalid/410")
        self.worker.choice = ProviderChoice("Claude", "test-model", "high", "session")
        self.worker.save_new_state(self.worker.issue, self.worker.choice, self.base_sha)
        self.worker.start_usage = ProviderUsage(0, 82.0, "session 82% / week 95% remaining")
        with (
            mock.patch.object(self.worker, "comments", return_value=[]),
            mock.patch.object(self.worker.github, "gh", return_value="") as github,
        ):
            self.worker.post_started_comment()
        body = github.call_args.args[2]
        self.assertIn(
            "Claude usage remaining: 82% remaining (session 82% / week 95% remaining)", body
        )
        self.assertEqual(self.worker.read_state()["usage_at_start"]["remaining_percent"], 82.0)

    def test_completion_comment_reports_usage_spent_on_the_issue(self) -> None:
        pending = {
            "ai": "Claude", "ai_tool": "Claude", "model": "m", "effort": "high",
            "commit_sha": "1" * 40, "commit_message": "Do it (#410)",
            "ai_output": "done", "work_type": "initial",
            "usage_at_start": {"remaining_percent": 80.0, "detail": "session 80% / week 95% remaining"},
            "usage_at_completion": {"remaining_percent": 73.5, "detail": "session 73.5% / week 95% remaining"},
        }
        rendered = self.worker.render_pending_comment(pending)
        self.assertIn("Claude usage at start: 80% remaining", rendered)
        self.assertIn("Claude usage at completion: 73.5% remaining", rendered)
        self.assertIn("Approx. Claude usage for this issue: 6.5 percentage points", rendered)

    def test_completion_comment_without_usage_snapshots_is_unchanged(self) -> None:
        pending = {
            "ai": "Codex", "ai_tool": "Codex", "model": "m", "effort": "high",
            "commit_sha": "2" * 40, "commit_message": "Do it (#411)",
            "ai_output": "done", "work_type": "initial",
        }
        rendered = self.worker.render_pending_comment(pending)
        self.assertNotIn("usage at start", rendered)
        self.assertIn("Completed by **Codex**.", rendered)

    def test_existing_start_marker_repairs_state_without_duplicate_comment(self) -> None:
        self.worker.issue = IssueContext(408, "Crash-safe notice", "", [], "https://example.invalid/408")
        self.worker.choice = ProviderChoice("Claude", "test-model", "high", "session")
        self.worker.save_new_state(self.worker.issue, self.worker.choice, self.base_sha)
        marker = self.worker.started_comment_marker()
        with (
            mock.patch.object(self.worker, "comments", return_value=[{"body": marker}]),
            mock.patch.object(self.worker.github, "gh", return_value="") as github,
        ):
            self.worker.post_started_comment()
        github.assert_not_called()
        self.assertTrue(self.worker.read_state()["started_comment_posted"])

    def _prime_resumed_worker(self) -> None:
        self.worker.issue = IssueContext(420, "Resume notice", "", [], "https://example.invalid/420")
        self.worker.choice = ProviderChoice("Codex", "test-model", "high", "session-420", resume=True)
        self.worker.save_new_state(self.worker.issue, self.worker.choice, self.base_sha)
        self.worker.update_state(
            started_comment_posted=True,
            session_started=True,
            session_comment_id=0,
            quota_resumed_at="2026-09-05T09:00:00-05:00",
        )
        self.worker.quota_resume_ready = True

    def test_resume_comment_is_posted_once_when_a_paused_session_resumes(self) -> None:
        self._prime_resumed_worker()
        with (
            mock.patch.object(self.worker, "comments", return_value=[]),
            mock.patch.object(self.worker.github, "gh", return_value="") as github,
        ):
            self.worker.post_resumed_comment()
            self.worker.post_resumed_comment()
        github.assert_called_once()
        arguments, provider, body = github.call_args.args
        self.assertEqual(provider, "codex")
        self.assertIn("issue", arguments)
        self.assertIn("**Codex Bot** is resuming work on this issue", body)
        self.assertIn("- Branch: `ai/codex/issue-420`", body)
        self.assertTrue(is_worker_comment({"body": body}))
        self.assertEqual(
            self.worker.read_state()["resumed_comment_token"], "2026-09-05T09:00:00-05:00"
        )

    def test_resume_comment_calls_out_comments_left_while_paused(self) -> None:
        self._prime_resumed_worker()
        left_while_paused = [
            {"id": 7, "author": "DotNetRockStar", "created_at": "", "body": "One more thing."},
            {"id": 8, "author": "DotNetRockStar", "created_at": "", "body": "And another."},
        ]
        with (
            mock.patch.object(self.worker, "comments", return_value=[]),
            mock.patch.object(
                self.worker, "load_resume_comments", return_value=left_while_paused
            ),
            mock.patch.object(self.worker.github, "gh", return_value="") as github,
        ):
            self.worker.post_resumed_comment()
        body = github.call_args.args[2]
        self.assertIn("Picking up 2 new trusted comments left while the work was paused", body)

    def test_resume_comment_is_skipped_for_a_fresh_first_round(self) -> None:
        self.worker.issue = IssueContext(421, "Fresh start", "", [], "https://example.invalid/421")
        self.worker.choice = ProviderChoice("Codex", "test-model", "high", "session-421")
        self.worker.save_new_state(self.worker.issue, self.worker.choice, self.base_sha)
        with mock.patch.object(self.worker.github, "gh", return_value="") as github:
            self.worker.post_resumed_comment()
        github.assert_not_called()

    def test_existing_resume_marker_repairs_state_without_duplicate_comment(self) -> None:
        self._prime_resumed_worker()
        marker = self.worker.resumed_comment_marker("2026-09-05T09:00:00-05:00")
        with (
            mock.patch.object(self.worker, "comments", return_value=[{"body": marker}]),
            mock.patch.object(self.worker.github, "gh", return_value="") as github,
        ):
            self.worker.post_resumed_comment()
        github.assert_not_called()
        self.assertEqual(
            self.worker.read_state()["resumed_comment_token"], "2026-09-05T09:00:00-05:00"
        )

    def test_dry_run_does_not_post_start_comment(self) -> None:
        args = build_parser().parse_args(
            [
                "--repo-dir", str(self.repo), "--state-dir", str(self.state),
                "--dry-run", "--no-require-bot-auth", "--gh-bin", "/usr/bin/false",
                "--claude-bin", "", "--codex-bin", "", "--grok-bin", "",
            ]
        )
        worker = Worker(Config.from_args(args))
        worker.issue = IssueContext(409, "Dry run", "", [], "https://example.invalid/409")
        with (
            mock.patch.object(worker, "provider_usage", return_value=ProviderUsage(0, 100.0)),
            mock.patch.object(worker, "post_started_comment") as start_comment,
        ):
            self.assertEqual(worker.run_selected_issue(), 0)
        start_comment.assert_not_called()

    def test_a_different_provider_approves_the_pull_request(self) -> None:
        worker = self.pr_worker()
        worker.issue = IssueContext(404, "Approval", "", [], "https://example.invalid/404")
        worker.choice = ProviderChoice("Claude", "test", "high", "session")
        with mock.patch.object(worker.github, "gh", return_value="") as github:
            reviewer = worker.approve_pull_request("https://example.invalid/pull/404")
        self.assertNotEqual(reviewer, "claude", "the implementer must not approve its own PR")
        self.assertIn(reviewer, {"codex", "grok"})
        self.assertEqual(github.call_args.args[1], reviewer)
        self.assertIn("--approve", github.call_args.args[0])

        # When the implementer is the only enabled provider, it falls back to
        # approving its own PR rather than blocking.
        worker.config = dataclasses.replace(
            worker.config,
            providers=tuple(
                dataclasses.replace(s, enabled=(s.key == "claude"))
                for s in worker.config.providers
            ),
        )
        self.assertEqual(worker.review_provider(), "claude")

    def test_followup_uses_a_new_comment_specific_branch(self) -> None:
        worker = self.pr_worker()
        worker.issue = IssueContext(
            404,
            "Follow-up",
            "",
            [],
            "https://example.invalid/404",
            work_type="followup",
            trigger_comment_id=9876,
        )
        worker.choice = ProviderChoice("Codex", "test", "high", "session")
        self.assertEqual(worker.expected_branch(), "ai/codex/issue-404")

    def test_pr_completion_returns_clean_checkout_to_the_integration_branch(self) -> None:
        worker = self.pr_worker()
        worker.issue = IssueContext(405, "Return to ai-main", "", [], "https://example.invalid/405")
        worker.choice = ProviderChoice("Codex", "test", "high", "session")
        run_start, _, _, _ = worker.prepare_repository()
        (self.repo / "merged.txt").write_text("merged\n", encoding="utf-8")
        worker.commit_completed_work(run_start)
        branch = worker.expected_branch()
        self.git("push", "-q", "-u", "origin", branch)

        # Simulate the squash-merge of the issue branch into ai-main on the remote.
        merger = self.root / "merger"
        subprocess.run(
            ["git", "clone", "-q", "--branch", "ai-main", str(self.remote), str(merger)], check=True
        )
        subprocess.run(["git", "-C", str(merger), "config", "user.name", "merger"], check=True)
        subprocess.run(
            ["git", "-C", str(merger), "config", "user.email", "merger@example.invalid"], check=True
        )
        subprocess.run(["git", "-C", str(merger), "fetch", "-q", "origin", branch], check=True)
        subprocess.run(
            ["git", "-C", str(merger), "merge", "-q", "--no-ff", "FETCH_HEAD", "-m", "merge issue"],
            check=True,
        )
        subprocess.run(["git", "-C", str(merger), "push", "-q", "origin", "ai-main"], check=True)
        merged_head = subprocess.run(
            ["git", "-C", str(merger), "rev-parse", "HEAD"], text=True,
            stdout=subprocess.PIPE, check=True,
        ).stdout.strip()

        synchronized = worker.return_to_integration_branch(branch)
        self.assertEqual(synchronized, merged_head)
        self.assertEqual(self.git("branch", "--show-current"), "ai-main")
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertNotIn(branch, self.git("branch", "--format=%(refname:short)").splitlines())

    def test_paused_pr_branch_returns_to_main_and_restores_its_own_branch(self) -> None:
        worker = self.pr_worker()
        worker.issue = IssueContext(406, "Paused PR", "", [], "https://example.invalid/406")
        worker.choice = ProviderChoice("Claude", "test", "high", "session-406")
        worker.prepare_repository()
        state = worker.read_state()
        state.update({"status": "quota_paused", "quota_pause_count": 1})
        worker.write_state(state)
        (self.repo / "paused.txt").write_text("paused work\n", encoding="utf-8")
        worker.suspend_paused()
        paused_file = worker.paused_dir / "406.json"
        self.assertEqual(self.git("branch", "--show-current"), "ai-main")
        self.assertTrue(paused_file.is_file())

        worker.restore_paused(paused_file)
        self.assertEqual(self.git("branch", "--show-current"), "ai/claude/issue-406")
        self.assertEqual((self.repo / "paused.txt").read_text(), "paused work\n")
        self.assertTrue(worker.in_progress_file.is_file())

    def test_completion_markdown_is_not_indented(self) -> None:
        pending = {
            "ai": "Codex", "ai_tool": "Codex", "model": "test-model", "effort": "high",
            "commit_sha": "1" * 40, "commit_message": "Test completion (#404)",
            "ai_output": "## Summary\n\nDone.\n\n## Changes\n\n- Fixed it.", "work_type": "initial",
        }
        rendered = self.worker.render_pending_comment(pending)
        self.assertIn("\n## Summary\n", rendered)
        self.assertIn("\n- Fixed it.\n", rendered)
        self.assertNotIn("    ## Summary", rendered)

    def test_defaults_and_parameters(self) -> None:
        args = build_parser().parse_args([])
        self.assertEqual(args.github_repository, "DotNetRockStar/swarm")
        self.assertEqual(args.assignee, "DotNetRockStar")
        self.assertEqual(args.minimum_remaining_percent, 10)
        self.assertEqual(args.base_branch, "main")
        self.assertEqual(args.integration_branch, "ai-main")
        self.assertEqual(args.branch_prefix, "ai")
        self.assertTrue(args.require_bot_auth)
        self.assertFalse(args.auto_approve)
        self.assertFalse(args.auto_merge)
        # delivery-mode / merge-method were removed with the integration model.
        with self.assertRaises(SystemExit):
            build_parser().parse_args(["--delivery-mode", "pull-request"])
        overridden = build_parser().parse_args(
            ["--github-repository", "example/repo", "--preferred-provider", "codex",
             "--integration-branch", "staging"]
        )
        self.assertEqual(overridden.github_repository, "example/repo")
        self.assertEqual(overridden.preferred_provider, "codex")
        self.assertEqual(overridden.integration_branch, "staging")


class RunnerTestCase(unittest.TestCase):
    def test_scheduler_keeps_repos_file_out_of_worker_arguments(self) -> None:
        args, worker_arguments = runner_module.build_parser().parse_known_args(
            [
                "--repos-file", "/tmp/repos.json",
                "--state-dir", "/tmp/state",
                "--enabled-provider", "codex",
                "--codex-model", "gpt-test",
            ]
        )

        self.assertEqual(args.repos_file, "/tmp/repos.json")
        self.assertNotIn("--repos-file", worker_arguments)
        self.assertIn("--enabled-provider", worker_arguments)
        self.assertIn("--codex-model", worker_arguments)

    def test_schedule_parser_and_next_run(self) -> None:
        args = runner_module.build_parser().parse_args(
            [
                "--schedule-mode", "custom",
                "--schedule-time", "14:30",
                "--schedule-days", "mon,wed,fri",
            ]
        )
        runner = runner_module.Runner(args, [])
        monday_before = dt.datetime(2026, 8, 31, 13, 0).astimezone()
        self.assertEqual(runner.next_scheduled_run(monday_before).weekday(), 0)
        self.assertEqual(runner.next_scheduled_run(monday_before).strftime("%H:%M"), "14:30")
        monday_after = dt.datetime(2026, 8, 31, 15, 0).astimezone()
        self.assertEqual(runner.next_scheduled_run(monday_after).weekday(), 2)

    def test_invalid_schedule_values_are_rejected(self) -> None:
        with self.assertRaises(SystemExit):
            runner_module.build_parser().parse_args(["--schedule-time", "25:00"])
        with self.assertRaises(SystemExit):
            runner_module.build_parser().parse_args(["--schedule-days", "monday,nonesday"])

    def test_scheduler_reports_queued_issue_waiting_for_provider(self) -> None:
        with tempfile.TemporaryDirectory(prefix="swarm-runner-capacity-test.") as temporary:
            root = Path(temporary)
            worker = root / "worker.py"
            worker.write_text("raise SystemExit(12)\n", encoding="utf-8")
            args = runner_module.build_parser().parse_args(
                [
                    "--repo-dir", str(root), "--state-dir", str(root / "state"),
                    "--worker", str(worker), "--once",
                    "--crontab-bin", "", "--pgrep-bin", "",
                ]
            )
            runner = runner_module.Runner(args, [])
            output = io.StringIO()
            with (
                mock.patch.object(runner, "synchronize_repository", return_value=True),
                mock.patch.object(
                    runner, "run_worker", return_value=runner_module.PROVIDER_UNAVAILABLE_EXIT_CODE
                ),
                mock.patch.object(runner, "prune_cargo_target"),
                contextlib.redirect_stdout(output),
            ):
                status = runner.run()
        self.assertEqual(status, runner_module.PROVIDER_UNAVAILABLE_EXIT_CODE)
        self.assertIn("an issue is queued", output.getvalue())
        self.assertIn("Cycle complete: queued issue work is waiting for AI capacity", output.getvalue())
        self.assertNotIn("no issue to work", output.getvalue())

    def test_active_transcode_diagnostic_and_runner_lock(self) -> None:
        with tempfile.TemporaryDirectory(prefix="swarm-runner-test.") as temporary:
            root = Path(temporary)
            pgrep = root / "pgrep"
            pgrep.write_text("#!/bin/sh\nexit \"${FAKE_PGREP_STATUS:-1}\"\n", encoding="utf-8")
            pgrep.chmod(0o755)
            args = runner_module.build_parser().parse_args(
                ["--state-dir", str(root / "state"), "--pgrep-bin", str(pgrep), "--check-transcode-active"]
            )
            with mock.patch.dict(os.environ, {"FAKE_PGREP_STATUS": "0"}):
                self.assertEqual(runner_module.Runner(args, []).run(), 0)
            args.check_transcode_active = False
            lock = Path(args.state_dir) / "runner.lock"
            lock.mkdir(parents=True)
            (lock / "pid").write_text(f"{os.getpid()}\n", encoding="utf-8")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(runner_module.Runner(args, []).run(), 0)
            self.assertIn("already active", output.getvalue())

    def test_scheduler_snapshot_forwards_shared_paths(self) -> None:
        with tempfile.TemporaryDirectory(prefix="swarm-runner-snapshot-test.") as temporary:
            root = Path(temporary)
            repo = root / "repo"
            state = root / "state"
            repo.mkdir()
            subprocess.run(["git", "-C", str(repo), "init", "-q", "-b", "main"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "runner test"], check=True)
            subprocess.run(
                ["git", "-C", str(repo), "config", "user.email", "runner@example.invalid"], check=True
            )
            subprocess.run(["git", "-C", str(repo), "commit", "-q", "--allow-empty", "-m", "base"], check=True)
            remote = root / "remote.git"
            subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
            subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", str(remote)], check=True)
            subprocess.run(["git", "-C", str(repo), "push", "-q", "-u", "origin", "main"], check=True)
            worker = root / "fake_worker.py"
            result_file = root / "result.json"
            worker.write_text(
                "import json, os, sys\n"
                "from pathlib import Path\n"
                "Path(os.environ['FAKE_RESULT_FILE']).write_text(json.dumps({"
                "'repo': os.environ.get('SWARM_REPO_DIR'), "
                "'state': os.environ.get('SWARM_ISSUE_WORKER_STATE_DIR'), "
                "'args': sys.argv[1:]}))\n",
                encoding="utf-8",
            )
            args = runner_module.build_parser().parse_args(
                [
                    "--repo-dir", str(repo), "--state-dir", str(state), "--worker", str(worker),
                    "--once", "--pgrep-bin", "",
                ]
            )
            with mock.patch.dict(os.environ, {"FAKE_RESULT_FILE": str(result_file)}):
                self.assertEqual(runner_module.Runner(args, ["--github-repository", "example/repo"]).run(), 0)
            result = json.loads(result_file.read_text(encoding="utf-8"))
            self.assertEqual(result["repo"], str(repo.resolve()))
            self.assertEqual(result["state"], str(state.resolve()))
            self.assertEqual(result["args"], ["--github-repository", "example/repo"])

    def test_scheduler_preflight_fetches_and_runs_the_worker_snapshot(self) -> None:
        with tempfile.TemporaryDirectory(prefix="swarm-runner-sync-test.") as temporary:
            root = Path(temporary)
            repo = root / "repo"
            remote = root / "remote.git"
            state = root / "state"
            result_file = root / "result.txt"
            subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            for name, value in (("user.name", "runner test"), ("user.email", "runner@example.invalid")):
                subprocess.run(["git", "-C", str(repo), "config", name, value], check=True)
            worker = repo / "worker.py"
            worker.write_text(
                "import os\nfrom pathlib import Path\n"
                "Path(os.environ['FAKE_RESULT_FILE']).write_text('ran')\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "-C", str(repo), "add", "worker.py"], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "worker"], check=True)
            subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", str(remote)], check=True)
            subprocess.run(["git", "-C", str(repo), "push", "-q", "-u", "origin", "main"], check=True)

            # A commit lands on origin/main after the scheduler starts.
            updater = root / "updater"
            subprocess.run(
                ["git", "clone", "-q", "--branch", "main", str(remote), str(updater)], check=True
            )
            for name, value in (("user.name", "updater"), ("user.email", "updater@example.invalid")):
                subprocess.run(["git", "-C", str(updater), "config", name, value], check=True)
            subprocess.run(
                ["git", "-C", str(updater), "commit", "-q", "--allow-empty", "-m", "remote update"],
                check=True,
            )
            subprocess.run(["git", "-C", str(updater), "push", "-q", "origin", "main"], check=True)
            remote_sha = subprocess.run(
                ["git", "-C", str(updater), "rev-parse", "HEAD"], text=True,
                stdout=subprocess.PIPE, check=True,
            ).stdout.strip()

            args = runner_module.build_parser().parse_args(
                [
                    "--repo-dir", str(repo), "--state-dir", str(state), "--worker", str(worker),
                    "--once", "--pgrep-bin", "",
                ]
            )
            with mock.patch.dict(os.environ, {"FAKE_RESULT_FILE": str(result_file)}):
                self.assertEqual(runner_module.Runner(args, []).run(), 0)

            # The worker ran, and the pre-flight fetched (origin/main now points
            # at the update the worker's own integration sync would use).
            self.assertEqual(result_file.read_text(encoding="utf-8"), "ran")
            fetched = subprocess.run(
                ["git", "-C", str(repo), "rev-parse", "origin/main"], text=True,
                stdout=subprocess.PIPE, check=True,
            ).stdout.strip()
            self.assertEqual(fetched, remote_sha)

    def test_scheduler_does_not_switch_an_active_issue_checkout(self) -> None:
        with tempfile.TemporaryDirectory(prefix="swarm-runner-active-test.") as temporary:
            root = Path(temporary)
            repo = root / "repo"
            state = root / "state"
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "runner test"], check=True)
            subprocess.run(
                ["git", "-C", str(repo), "config", "user.email", "runner@example.invalid"], check=True
            )
            subprocess.run(["git", "-C", str(repo), "commit", "-q", "--allow-empty", "-m", "base"], check=True)
            subprocess.run(["git", "-C", str(repo), "switch", "-q", "-c", "ai/codex/issue-114"], check=True)
            state.mkdir()
            (state / "in-progress-issue.json").write_text("{}\n", encoding="utf-8")
            (repo / "dirty.txt").write_text("active work\n", encoding="utf-8")
            args = runner_module.build_parser().parse_args(
                ["--repo-dir", str(repo), "--state-dir", str(state)]
            )
            runner = runner_module.Runner(args, [])

            self.assertTrue(runner.synchronize_repository(runner.repos[0]))
            branch = subprocess.run(
                ["git", "-C", str(repo), "branch", "--show-current"], text=True,
                stdout=subprocess.PIPE, check=True,
            ).stdout.strip()
            self.assertEqual(branch, "ai/codex/issue-114")
            self.assertTrue((repo / "dirty.txt").is_file())

    @staticmethod
    def _repos_file(root: Path, labels: tuple[str, ...]) -> Path:
        entries = []
        for label in labels:
            workspace = root / label
            workspace.mkdir(parents=True, exist_ok=True)
            entries.append(
                {
                    "label": label,
                    "workspace_dir": str(workspace),
                    "state_dir": str(root / "state" / label),
                    "base_branch": "main",
                    "remote_name": "origin",
                    "integration_branch": "ai-main",
                    "worker_args": ["--github-repository", f"acme/{label}"],
                }
            )
        path = root / "repos.json"
        path.write_text(json.dumps(entries), encoding="utf-8")
        return path

    def test_parallel_repos_flag_is_ignored_for_a_single_repository(self) -> None:
        args = runner_module.build_parser().parse_args(
            ["--repo-dir", "/tmp/repo", "--state-dir", "/tmp/state", "--parallel-repos"]
        )
        self.assertTrue(args.parallel_repos)
        # One synthesized repo -> nothing to parallelize.
        self.assertFalse(runner_module.Runner(args, []).parallel_repos)

    def test_parallel_cycle_works_every_repository_and_aggregates_status(self) -> None:
        with tempfile.TemporaryDirectory(prefix="swarm-runner-parallel-test.") as temporary:
            root = Path(temporary)
            repos_file = self._repos_file(root, ("beta", "gamma", "delta"))
            args = runner_module.build_parser().parse_args(
                [
                    "--repos-file", str(repos_file), "--state-dir", str(root / "state"),
                    "--once", "--parallel-repos", "--pgrep-bin", "",
                ]
            )
            runner = runner_module.Runner(args, [])
            self.assertTrue(runner.parallel_repos)

            worked: list[str] = []
            lock = threading.Lock()

            def fake_worker(repo: dict[str, object], _password: str, _prefix: str = "") -> int:
                with lock:
                    worked.append(str(repo["label"]))
                return (
                    runner_module.ISSUE_COMPLETED_EXIT_CODE
                    if repo["label"] == "gamma"
                    else 0
                )

            output = io.StringIO()
            with (
                mock.patch.object(runner, "synchronize_repository", return_value=True),
                mock.patch.object(runner, "run_worker", side_effect=fake_worker),
                mock.patch.object(runner, "prune_cargo_target"),
                contextlib.redirect_stdout(output),
            ):
                status = runner.run()

            self.assertEqual(sorted(worked), ["beta", "delta", "gamma"])
            self.assertEqual(status, runner_module.ISSUE_COMPLETED_EXIT_CODE)
            self.assertIn("repositories in parallel", output.getvalue())

    def test_sequential_cycle_is_the_default(self) -> None:
        with tempfile.TemporaryDirectory(prefix="swarm-runner-sequential-test.") as temporary:
            root = Path(temporary)
            repos_file = self._repos_file(root, ("beta", "gamma"))
            args = runner_module.build_parser().parse_args(
                [
                    "--repos-file", str(repos_file), "--state-dir", str(root / "state"),
                    "--once", "--pgrep-bin", "",
                ]
            )
            runner = runner_module.Runner(args, [])
            self.assertFalse(runner.parallel_repos)

            order: list[str] = []
            with (
                mock.patch.object(runner, "synchronize_repository", return_value=True),
                mock.patch.object(
                    runner,
                    "run_worker",
                    side_effect=lambda repo, *_: order.append(str(repo["label"])) or 0,
                ),
                mock.patch.object(runner, "prune_cargo_target"),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(runner.run(), 0)
            self.assertEqual(order, ["beta", "gamma"])


class GitHubAppAuthTestCase(unittest.TestCase):
    def test_exec_parser_accepts_repository_scope(self) -> None:
        args = auth_module._build_parser().parse_args(
            [
                "exec", "--provider", "codex", "--repository", "octocat/example",
                "--", "gh", "pr", "review", "42", "--approve",
            ]
        )
        self.assertEqual(args.repository, "octocat/example")
        self.assertEqual(args.command_args, ["--", "gh", "pr", "review", "42", "--approve"])

    def test_token_and_bot_identity_are_short_lived_and_not_persisted(self) -> None:
        with tempfile.TemporaryDirectory(prefix="swarm-app-auth-test.") as temporary:
            root = Path(temporary)
            key = root / "bot.pem"
            subprocess.run(["openssl", "genrsa", "-out", str(key), "2048"], check=True, capture_output=True)
            key.chmod(0o600)
            config = root / "apps.json"
            config.write_text(
                json.dumps(
                    {
                        "codex": {
                            "app_id": 123,
                            "installation_id": 456,
                            "private_key_path": str(key),
                            "bot_login": "swarm-codex-bot[bot]",
                            "bot_name": "Swarm Codex Bot",
                        }
                    }
                ),
                encoding="utf-8",
            )
            responses = [
                io.BytesIO(json.dumps({"token": "installation-token"}).encode()),
                io.BytesIO(json.dumps({"id": 789}).encode()),
            ]
            with mock.patch.object(auth_module.urllib.request, "urlopen", side_effect=responses) as urlopen:
                auth = auth_module.GitHubAppAuth(config)
                environment = auth.bot_environment("codex")
                self.assertEqual(environment["GH_TOKEN"], "installation-token")
                self.assertEqual(
                    environment["GIT_AUTHOR_EMAIL"],
                    "789+swarm-codex-bot[bot]@users.noreply.github.com",
                )
                self.assertEqual(urlopen.call_count, 2)
            self.assertNotIn("installation-token", config.read_text(encoding="utf-8"))

    def _apps_config(self, root: Path, **claude_overrides: object) -> Path:
        key = root / "bot.pem"
        key.write_text("not used", encoding="utf-8")
        key.chmod(0o600)
        entry: dict[str, object] = {
            "app_id": 1,
            "private_key_path": str(key),
            "bot_login": "swarm-claude-bot[bot]",
            "bot_name": "Swarm Claude Bot",
            "bot_email": "bot@example.com",
        }
        entry.update(claude_overrides)
        config = root / "apps.json"
        config.write_text(json.dumps({"claude": entry}), encoding="utf-8")
        return config

    def test_installation_resolves_from_the_repository_owner_without_discovery(self) -> None:
        with tempfile.TemporaryDirectory(prefix="swarm-app-owner-test.") as temporary:
            root = Path(temporary)
            config = self._apps_config(
                root,
                installation_id=111,
                installations={"batocera-fleet-federation": 222},
            )
            with mock.patch.object(auth_module.GitHubAppAuth, "_jwt", return_value="jwt"):
                with mock.patch.object(
                    auth_module.urllib.request,
                    "urlopen",
                    side_effect=[io.BytesIO(json.dumps({"token": "owner-token"}).encode())],
                ) as urlopen:
                    auth = auth_module.GitHubAppAuth(
                        config, repository="Batocera-Fleet-Federation/batocera.drone"
                    )
                    self.assertEqual(auth.token("claude"), "owner-token")
            self.assertIn("installations/222/access_tokens", urlopen.call_args_list[0].args[0].full_url)

    def test_missing_owner_installation_raises_actionable_error(self) -> None:
        with tempfile.TemporaryDirectory(prefix="swarm-app-missing-test.") as temporary:
            root = Path(temporary)
            config = self._apps_config(root, installation_id=111)
            listing = io.BytesIO(
                json.dumps([{"id": 111, "account": {"login": "SWARM-Media-Steaming"}}]).encode()
            )
            with mock.patch.object(auth_module.GitHubAppAuth, "_jwt", return_value="jwt"):
                with mock.patch.object(
                    auth_module.urllib.request, "urlopen", side_effect=[listing]
                ):
                    auth = auth_module.GitHubAppAuth(
                        config, repository="Batocera-Fleet-Federation/batocera.drone"
                    )
                    with self.assertRaisesRegex(
                        RuntimeError,
                        r"not installed on 'Batocera-Fleet-Federation'.*installations/new",
                    ):
                        auth.verify_installation("claude")

    def test_discovered_owner_installation_is_persisted(self) -> None:
        with tempfile.TemporaryDirectory(prefix="swarm-app-persist-test.") as temporary:
            root = Path(temporary)
            config = self._apps_config(root, installation_id=111)
            responses = [
                io.BytesIO(
                    json.dumps(
                        [{"id": 333, "account": {"login": "Batocera-Fleet-Federation"}}]
                    ).encode()
                ),
                io.BytesIO(json.dumps({"token": "discovered-token"}).encode()),
            ]
            with mock.patch.object(auth_module.GitHubAppAuth, "_jwt", return_value="jwt"):
                with mock.patch.object(
                    auth_module.urllib.request, "urlopen", side_effect=responses
                ):
                    auth = auth_module.GitHubAppAuth(
                        config, repository="Batocera-Fleet-Federation/batocera.drone"
                    )
                    self.assertEqual(auth.token("claude"), "discovered-token")
            persisted = json.loads(config.read_text(encoding="utf-8"))
            self.assertEqual(
                persisted["claude"]["installations"]["Batocera-Fleet-Federation"], 333
            )
            self.assertEqual(persisted["claude"]["installation_id"], 111)

    def test_repository_status_reports_ready_and_missing_states(self) -> None:
        with tempfile.TemporaryDirectory(prefix="swarm-app-status-test.") as temporary:
            root = Path(temporary)
            config = self._apps_config(
                root,
                installation_id=111,
                installations={"my-org": 222},
            )
            with mock.patch.object(auth_module.GitHubAppAuth, "_jwt", return_value="jwt"):
                # Ready: installation known, "all repositories" selection.
                with mock.patch.object(
                    auth_module.urllib.request,
                    "urlopen",
                    side_effect=[
                        io.BytesIO(json.dumps({"repository_selection": "all"}).encode())
                    ],
                ):
                    ready = auth_module.GitHubAppAuth(
                        config, repository="My-Org/widget"
                    ).repository_status("claude")
                self.assertEqual(ready["state"], "ready")
                self.assertEqual(ready["installationId"], 222)

                # Not installed on a different owner (discovery finds nothing).
                with mock.patch.object(
                    auth_module.urllib.request,
                    "urlopen",
                    side_effect=[io.BytesIO(json.dumps([]).encode())],
                ):
                    missing = auth_module.GitHubAppAuth(
                        config, repository="Other-Org/widget"
                    ).repository_status("claude")
                self.assertEqual(missing["state"], "not_installed_on_owner")
                self.assertEqual(
                    missing["installUrl"],
                    "https://github.com/apps/swarm-claude-bot/installations/new",
                )

            unconfigured = auth_module.GitHubAppAuth(
                root / "absent.json", repository="My-Org/widget"
            ).repository_status("claude")
            self.assertEqual(unconfigured["state"], "unconfigured")

    def test_private_key_must_not_be_group_readable(self) -> None:
        with tempfile.TemporaryDirectory(prefix="swarm-app-key-test.") as temporary:
            root = Path(temporary)
            key = root / "bot.pem"
            key.write_text("not used", encoding="utf-8")
            key.chmod(0o644)
            config = root / "apps.json"
            config.write_text(
                json.dumps(
                    {
                        "claude": {
                            "app_id": 1,
                            "installation_id": 2,
                            "private_key_path": str(key),
                            "bot_login": "swarm-claude-bot[bot]",
                        }
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RuntimeError, "permissions are too broad"):
                auth_module.GitHubAppAuth(config).definition("claude")

    def test_setup_manifest_is_public_and_minimally_scoped(self) -> None:
        with tempfile.TemporaryDirectory(prefix="swarm-app-manifest-test.") as temporary:
            state = setup_module.SetupState(
                "DotNetRockStar/swarm", Path(temporary) / "apps.json", 8765
            )
            manifest = state.manifest("codex")
            # Public so one app installs on every org the operator uses.
            self.assertTrue(manifest["public"])
            self.assertNotIn("hook_attributes", manifest)
            self.assertNotIn("setup_url", manifest)
            self.assertNotIn("setup_on_update", manifest)
            self.assertEqual(
                manifest["default_permissions"],
                {
                    "contents": "write",
                    "issues": "write",
                    "pull_requests": "write",
                    "workflows": "write",
                },
            )

    def test_setup_registration_url_is_org_scoped(self) -> None:
        with tempfile.TemporaryDirectory(prefix="swarm-app-owner-test.") as temporary:
            state = setup_module.SetupState(
                "SWARM-Media-Steaming/swarm",
                Path(temporary) / "apps.json",
                8765,
            )
            state.repository_owner_type = "Organization"
            self.assertEqual(
                state.registration_url(),
                "https://github.com/organizations/SWARM-Media-Steaming/settings/apps/new",
            )
            self.assertLessEqual(len(state.app_name("claude")), 34)

    def test_setup_accepts_a_public_app_owned_by_another_account(self) -> None:
        with tempfile.TemporaryDirectory(prefix="swarm-app-owner-any-test.") as temporary:
            root = Path(temporary)
            key = root / "claude.pem"
            key.write_text("not used", encoding="utf-8")
            key.chmod(0o600)
            config = root / "apps.json"
            config.write_text(
                json.dumps(
                    {
                        "claude": {
                            "app_id": 1,
                            "installation_id": 2,
                            "private_key_path": str(key),
                            "bot_login": "swarm-claude-bot[bot]",
                        }
                    }
                ),
                encoding="utf-8",
            )
            state = setup_module.SetupState(
                "Some-Other-Org/thing", config, 8765, ("claude",)
            )
            with mock.patch.object(state, "detect_repository_owner"):
                with mock.patch.object(setup_module, "GitHubAppAuth") as auth_type:
                    auth_type.return_value.app_profile.return_value = {
                        "owner": {"login": "SWARM-Media-Steaming"}
                    }
                    auth_type.return_value.find_installation_for_repository.return_value = 77
                    state.validate_existing()
                    self.assertFalse(hasattr(state, "owner_mismatches"))
                    self.assertIn("claude", state.valid_installations)
                    self.assertEqual(state.config["claude"]["installation_id"], 77)

    def test_setup_only_waits_for_enabled_providers(self) -> None:
        with tempfile.TemporaryDirectory(prefix="swarm-app-provider-test.") as temporary:
            state = setup_module.SetupState(
                "octocat/example",
                Path(temporary) / "apps.json",
                8765,
                ("claude",),
            )
            self.assertFalse(state.complete.is_set())
            state.valid_installations.add("claude")
            state.refresh_complete()
            self.assertTrue(state.complete.is_set())

    def test_app_definition_can_exist_before_repository_installation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="swarm-app-partial-test.") as temporary:
            root = Path(temporary)
            key = root / "bot.pem"
            key.write_text("not used", encoding="utf-8")
            key.chmod(0o600)
            config = root / "apps.json"
            config.write_text(
                json.dumps(
                    {
                        "claude": {
                            "app_id": 1,
                            "installation_id": 0,
                            "private_key_path": str(key),
                            "bot_login": "swarm-claude[bot]",
                        }
                    }
                ),
                encoding="utf-8",
            )
            auth = auth_module.GitHubAppAuth(config)
            self.assertEqual(auth.definition("claude").app_id, 1)
            self.assertFalse(auth.configured("claude"))
            with self.assertRaisesRegex(RuntimeError, "has not been installed"):
                auth.token("claude")

    def test_setup_confirms_installation_access_before_saving_callback(self) -> None:
        with tempfile.TemporaryDirectory(prefix="swarm-app-install-test.") as temporary:
            state = setup_module.SetupState(
                "octocat/example",
                Path(temporary) / "apps.json",
                8765,
                ("claude",),
            )
            with mock.patch.object(setup_module, "GitHubAppAuth") as auth_type:
                with mock.patch.object(state, "save_installation") as save_installation:
                    auth_type.return_value.find_installation_for_repository.return_value = 42
                    self.assertTrue(state.confirm_installation("claude", 42))
                    save_installation.assert_called_once_with("claude", 42)

                    save_installation.reset_mock()
                    self.assertFalse(state.confirm_installation("claude", 99))
                    save_installation.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
