#!/usr/bin/env python3

from __future__ import annotations

import contextlib
import dataclasses
import datetime as dt
import io
import json
import os
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import github_app_auth as auth_module
import install_swarm_issue_cron as runner_module
import setup_github_bots as setup_module
from ai_execution_history import (
    GRADE_POINTS,
    ExecutionHistoryRepository,
    ExecutionHistoryService,
    ExecutionStart,
    import_missing_issues,
    main as execution_history_main,
    normalize_provider_key,
    sanitize_text,
    summarize_router_matrix,
)
from dynamic_router import PROMPT_GRADES, RouterError
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
    extract_needs_input_metadata,
    extract_question_answer_metadata,
    is_worker_comment,
    priority_rank,
    resolve_preferred_provider,
    bump_minor_in_version_text,
    parse_version_file,
    is_merge_blocked_by_policy,
    MODEL_REJECTED_RE,
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
            "--no-dynamic-model-routing",
            "--routing-tiers",
            "",
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

    def test_required_input_reply_resumes_without_a_previous_commit(self) -> None:
        comments = [
            {
                "id": 300,
                "created_at": "2026-09-22T13:00:00Z",
                "user": {"login": "swarm-claude-bot[bot]"},
                "body": (
                    "<!-- swarm-issue-worker:needs-input:issue:157;provider:claude -->\n"
                    "Input requested by **Claude**.\n\n## Action required\nConfigure signing."
                ),
            },
            {
                "id": 301,
                "created_at": "2026-09-22T14:00:00Z",
                "user": {"login": "DotNetRockStar"},
                "body": "Signing secrets configured. Please continue.",
            },
        ]
        completion_authors = {"swarm-claude-bot"}
        waiting = extract_needs_input_metadata(comments, completion_authors)
        assert waiting is not None
        self.assertEqual(waiting["provider"], "claude")

        followup = extract_followup_metadata(comments, {"DotNetRockStar"}, completion_authors)
        assert followup is not None
        self.assertEqual(followup["previous_commit_sha"], "")
        self.assertEqual(followup["previous_ai"], "Claude")
        self.assertEqual(followup["trigger_comment_id"], 301)
        self.assertEqual([item["id"] for item in followup["followup_comments"]], [301])

    def test_required_input_marker_is_durable_completed_state(self) -> None:
        comments = [{
            "id": 310,
            "user": {"login": "DotNetRockStar"},
            "body": (
                "<!-- swarm-issue-worker:needs-input:issue:157;provider:claude -->\n"
                "Input requested by **Claude**."
            ),
        }]
        with mock.patch.object(self.worker, "clear_in_progress") as clear:
            self.assertTrue(self.worker.record_completed_from_comments(157, comments))
        self.assertIn(157, self.worker.completed_numbers())
        clear.assert_called_once_with(157)

    def test_old_input_request_does_not_hide_a_later_unlanded_commit(self) -> None:
        comments = [
            {
                "id": 310,
                "user": {"login": "DotNetRockStar"},
                "body": (
                    "<!-- swarm-issue-worker:needs-input:issue:157;provider:claude -->\n"
                    "Input requested by **Claude**."
                ),
            },
            {
                "id": 311,
                "user": {"login": "DotNetRockStar"},
                "body": "<!-- swarm-issue-worker:commit:" + "1" * 40 + " -->\nReworked by **Claude**.",
            },
        ]
        self.assertFalse(self.worker.record_completed_from_comments(157, comments))
        self.assertNotIn(157, self.worker.completed_numbers())

    def test_question_answer_can_receive_a_trusted_followup_without_a_commit(self) -> None:
        comments = [
            {
                "id": 320,
                "created_at": "2026-09-22T13:00:00Z",
                "user": {"login": "swarm-codex-bot[bot]"},
                "body": (
                    "<!-- swarm-issue-worker:question-answer:issue:159;provider:codex -->\n"
                    "Answered by **Codex**.\n\n## Answer\nYes."
                ),
            },
            {
                "id": 321,
                "created_at": "2026-09-22T14:00:00Z",
                "user": {"login": "DotNetRockStar"},
                "body": "Can you clarify why?",
            },
        ]
        completion_authors = {"swarm-codex-bot"}
        answer = extract_question_answer_metadata(comments, completion_authors)
        assert answer is not None
        self.assertEqual(answer["provider"], "codex")
        followup = extract_followup_metadata(comments, {"DotNetRockStar"}, completion_authors)
        assert followup is not None
        self.assertEqual(followup["previous_commit_sha"], "")
        self.assertEqual(followup["previous_ai"], "Codex")
        self.assertEqual(followup["trigger_comment_id"], 321)

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

    def test_no_preference_selects_the_provider_with_the_most_usage_remaining(self) -> None:
        self.worker.config = dataclasses.replace(self.worker.config, preferred_provider="auto")
        # A named favorite does not apply. The fullest remaining quota wins,
        # including when that provider would have lost an equal-usage tie.
        highest = self.worker.choose_provider(
            "", {"Claude": 20.0, "Codex": 70.0, "Grok": 55.0}
        )
        assert highest is not None
        self.assertEqual(highest.name, "Codex")

        tied = self.worker.choose_provider(
            "", {"Claude": 50.0, "Codex": 50.0, "Grok": 50.0}
        )
        assert tied is not None
        self.assertEqual(tied.name, "Claude")

        # Follow-up still rotates away from the previous provider.
        after_codex = self.worker.choose_provider(
            "Codex", {"Claude": 20.0, "Codex": 90.0, "Grok": 55.0}
        )
        assert after_codex is not None
        self.assertEqual(after_codex.name, "Grok")
        self.assertIsNone(self.worker.preferred_provider_key())
        self.assertEqual(self.worker.default_provider(), "claude")
        self.assertEqual(self.worker.review_provider("claude"), "codex")

    def test_named_preference_still_only_breaks_equal_usage_ties(self) -> None:
        self.worker.config = dataclasses.replace(self.worker.config, preferred_provider="codex")
        drained = self.worker.choose_provider(
            "", {"Claude": 90.0, "Codex": 40.0, "Grok": 55.0}
        )
        assert drained is not None
        self.assertEqual(drained.name, "Claude")
        tied = self.worker.choose_provider(
            "", {"Claude": 50.0, "Codex": 50.0, "Grok": 50.0}
        )
        assert tied is not None
        self.assertEqual(tied.name, "Codex")

    def test_no_preference_is_kept_and_a_disabled_favorite_falls_back(self) -> None:
        self.assertEqual(resolve_preferred_provider("auto", {"codex"}), "auto")
        self.assertEqual(resolve_preferred_provider("claude", {"codex", "grok"}), "codex")
        self.assertEqual(resolve_preferred_provider("AUTO", {"grok"}), "auto")

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

    def test_execution_history_configuration_is_independent(self) -> None:
        args = build_parser().parse_args(
            self._worker_argv(auto=False)
            + ["--ai-execution-history-enabled", "--no-prompt-feedback-upload-enabled"]
        )
        config = Config.from_args(args)
        self.assertTrue(config.ai_execution_history_enabled)
        self.assertFalse(config.prompt_feedback_upload_enabled)

    def test_execution_history_stores_exact_effective_prompt_and_lifecycle(self) -> None:
        database_path = self.state / "history.sqlite3"
        service = ExecutionHistoryService(True, database_path)
        execution_id = service.start(
            ExecutionStart(
                repository="octocat/example",
                issue_number=63,
                issue_url="https://github.com/octocat/example/issues/63",
                issue_title="Store prompt",
                issue_body="Original body",
                provider="Codex",
                model="test-model",
                effort="high",
                branch_name="ai/codex/issue-63",
                application_version="1.2.3",
            ),
            "2026-09-11T10:00:00-05:00",
        )
        prompt = "Issue description:\nOriginal body\n\nWrapped instruction exactly.\n"
        service.update(
            "2026-09-11T10:00:01-05:00",
            effective_prompt=prompt,
            final_status="running",
        )
        service.note("Tests began", "2026-09-11T10:00:02-05:00")
        service.update(
            "2026-09-11T10:00:03-05:00",
            completed_at="2026-09-11T10:00:03-05:00",
            duration_seconds=3.0,
            files_changed=["worker.py"],
            commit_shas=["a" * 40],
            final_status="completed",
        )

        repository = ExecutionHistoryRepository(database_path)
        with repository.connect() as database:
            row = database.execute(
                "SELECT * FROM ai_executions WHERE execution_id = ?", (execution_id,)
            ).fetchone()
        assert row is not None
        self.assertEqual(row["original_issue_body"], "Original body")
        self.assertEqual(row["effective_prompt"], prompt)
        self.assertEqual(row["final_status"], "completed")
        self.assertEqual(json.loads(row["files_changed"]), ["worker.py"])
        self.assertEqual(row["attempt_number"], 1)
        retry = service.start(
            ExecutionStart(
                repository="octocat/example",
                issue_number=63,
                issue_url="",
                issue_title="Store prompt",
                issue_body="Original body",
                provider="Claude",
                model="retry-model",
                effort="medium",
                branch_name="ai/codex/issue-63",
                application_version="1.2.3",
            ),
            "2026-09-11T10:01:00-05:00",
        )
        with repository.connect() as database:
            retry_row = database.execute(
                "SELECT * FROM ai_executions WHERE execution_id = ?", (retry,)
            ).fetchone()
        assert retry_row is not None
        self.assertEqual(retry_row["attempt_number"], 2)
        self.assertEqual(len(repository.pending_upload()), 2)

    def test_execution_history_for_repository_and_cli_export(self) -> None:
        database_path = self.state / "history.sqlite3"
        service = ExecutionHistoryService(True, database_path)
        service.start(
            ExecutionStart(
                repository="octocat/example",
                issue_number=63,
                issue_url="https://github.com/octocat/example/issues/63",
                issue_title="Store prompt",
                issue_body="Original body",
                provider="Codex",
                model="test-model",
                effort="high",
                branch_name="ai/codex/issue-63",
                application_version="1.2.3",
            ),
            "2026-09-11T10:00:00-05:00",
        )
        service.update(
            "2026-09-11T10:00:01-05:00",
            files_changed=["worker.py"],
            commit_shas=["a" * 40],
            final_status="completed",
        )
        service.start(
            ExecutionStart(
                repository="octocat/other",
                issue_number=1,
                issue_url="",
                issue_title="Unrelated",
                issue_body="",
                provider="Claude",
                model="m",
                effort="high",
                branch_name="ai/claude/issue-1",
                application_version="1.2.3",
            ),
            "2026-09-11T09:00:00-05:00",
        )

        repository = ExecutionHistoryRepository(database_path)
        rows = repository.for_repository("octocat/example")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["repository"], "octocat/example")

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            exit_code = execution_history_main(
                ["--db", str(database_path), "--repository", "octocat/example"]
            )
        self.assertEqual(exit_code, 0)
        records = json.loads(buffer.getvalue())
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["files_changed"], ["worker.py"])
        self.assertEqual(records[0]["commit_shas"], ["a" * 40])
        self.assertNotIn("octocat/other", buffer.getvalue())

    def test_import_missing_issues_skips_already_tracked_issue_numbers(self) -> None:
        database_path = self.state / "history.sqlite3"
        service = ExecutionHistoryService(True, database_path)
        service.start(
            ExecutionStart(
                repository="octocat/example",
                issue_number=63,
                issue_url="https://github.com/octocat/example/issues/63",
                issue_title="Already tracked",
                issue_body="Original body",
                provider="Codex",
                model="test-model",
                effort="high",
                branch_name="ai/codex/issue-63",
                application_version="1.2.3",
            ),
            "2026-09-11T10:00:00-05:00",
        )
        repository = ExecutionHistoryRepository(database_path)
        issues = [
            {
                "number": 63,
                "title": "Already tracked",
                "url": "https://github.com/octocat/example/issues/63",
                "body": "Original body",
                "state": "open",
                "createdAt": "2026-08-01T10:00:00Z",
                "closedAt": None,
            },
            {
                "number": 78,
                "title": "Add ability to import existing or missed issues",
                "url": "https://github.com/octocat/example/issues/78",
                "body": "Import missed issues into Feedback.",
                "state": "closed",
                "createdAt": "2026-08-02T10:00:00Z",
                "closedAt": "2026-08-03T12:00:00Z",
            },
        ]

        summary = import_missing_issues(
            repository, "octocat/example", issues, "2026-09-14T09:00:00-05:00"
        )

        self.assertEqual(summary, {"totalIssues": 2, "imported": 1, "skipped": 1})
        rows = {row["issue_number"]: row for row in repository.for_repository("octocat/example")}
        self.assertEqual(set(rows), {63, 78})
        # The already-tracked issue keeps its real row untouched, not shadowed
        # by a second "imported" one for the same issue number.
        self.assertEqual(rows[63]["final_status"], "accepted")
        self.assertEqual(rows[63]["attempt_number"], 1)
        imported_row = rows[78]
        self.assertEqual(imported_row["final_status"], "imported")
        self.assertEqual(imported_row["ai_provider"], "")
        self.assertEqual(imported_row["attempt_number"], 1)
        self.assertEqual(imported_row["completed_at"], "2026-08-03T12:00:00Z")
        self.assertIn("Imported from existing GitHub issue (state: closed)", imported_row["operational_notes"])

        # Running the import again must not duplicate the already-imported issue.
        again = import_missing_issues(
            repository, "octocat/example", issues, "2026-09-14T09:05:00-05:00"
        )
        self.assertEqual(again, {"totalIssues": 2, "imported": 0, "skipped": 2})
        self.assertEqual(len(repository.for_repository("octocat/example")), 2)

    def test_execution_history_cli_import_from_github(self) -> None:
        database_path = self.state / "history.sqlite3"
        issues = [
            {
                "number": 5,
                "title": "Missed issue",
                "url": "https://github.com/octocat/example/issues/5",
                "body": "",
                "state": "open",
                "createdAt": "2026-08-01T10:00:00Z",
                "closedAt": None,
            }
        ]
        buffer = io.StringIO()
        with mock.patch("ai_execution_history.fetch_github_issues", return_value=issues) as fetch:
            with contextlib.redirect_stdout(buffer):
                exit_code = execution_history_main(
                    [
                        "--db", str(database_path),
                        "--repository", "octocat/example",
                        "--import-from-github",
                        "--gh-bin", "/usr/bin/gh",
                    ]
                )
        self.assertEqual(exit_code, 0)
        fetch.assert_called_once_with("/usr/bin/gh", "octocat/example")
        self.assertEqual(json.loads(buffer.getvalue()), {"totalIssues": 1, "imported": 1, "skipped": 0})
        repository = ExecutionHistoryRepository(database_path)
        rows = repository.for_repository("octocat/example")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["issue_number"], 5)
        self.assertEqual(rows[0]["final_status"], "imported")

    def test_execution_history_cli_missing_database_returns_empty_list(self) -> None:
        missing = self.state / "does-not-exist.sqlite3"
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            exit_code = execution_history_main(["--db", str(missing), "--repository", "octocat/example"])
        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(buffer.getvalue()), [])

        page_buffer = io.StringIO()
        with contextlib.redirect_stdout(page_buffer):
            page_exit = execution_history_main(
                [
                    "--db", str(missing),
                    "--repository", "octocat/example",
                    "--limit", "10",
                    "--offset", "30",
                    "--search", "widget",
                ]
            )
        self.assertEqual(page_exit, 0)
        self.assertEqual(
            json.loads(page_buffer.getvalue()),
            {"records": [], "total": 0, "offset": 0, "limit": 10},
        )

    def test_execution_history_page_fetches_ten_records_and_filters_in_sqlite(self) -> None:
        database_path = self.state / "history.sqlite3"
        repository = ExecutionHistoryRepository(database_path)
        for number in range(1, 26):
            title = f"Issue {number}"
            if number == 7:
                title = "100% done"
            elif number == 8:
                title = "a_b"
            elif number == 9:
                title = "axb"
            repository.import_issue(
                "octocat/example",
                {
                    "number": number,
                    "title": title,
                    "url": f"https://github.com/octocat/example/issues/{number}",
                    "body": "secret-body-token",
                    "state": "open",
                    "createdAt": f"2026-01-01T00:00:{number:02d}Z",
                },
                "2026-09-14T09:00:00-05:00",
            )
        repository.import_issue(
            "octocat/other",
            {
                "number": 1,
                "title": "Ship widget",
                "url": "https://github.com/octocat/other/issues/1",
                "body": "",
                "state": "open",
                "createdAt": "2026-03-01T00:00:00Z",
            },
            "2026-09-14T09:00:00-05:00",
        )
        service = ExecutionHistoryService(True, database_path)
        service.start(
            ExecutionStart(
                repository="octocat/example",
                issue_number=40,
                issue_url="https://github.com/octocat/example/issues/40",
                issue_title="Ship widget",
                issue_body="secret-body-token",
                provider="Codex",
                model="test-model",
                effort="high",
                branch_name="ai/codex/issue-40",
                application_version="1.2.3",
            ),
            "2026-02-01T00:00:00Z",
        )
        service.update("2026-02-01T00:05:00Z", final_status="completed")

        first, total, offset, limit = repository.page_for_repository(
            "octocat/example", search="", limit=100, offset=0
        )
        self.assertEqual(total, 26)
        self.assertEqual(limit, 10)
        self.assertEqual(offset, 0)
        self.assertEqual(len(first), 10)
        self.assertEqual(
            [row["issue_number"] for row in first],
            [40, 25, 24, 23, 22, 21, 20, 19, 18, 17],
        )
        self.assertTrue(all(row["repository"] == "octocat/example" for row in first))

        second, second_total, second_offset, _ = repository.page_for_repository(
            "octocat/example", search="", limit=10, offset=10
        )
        self.assertEqual(second_total, 26)
        self.assertEqual(second_offset, 10)
        self.assertEqual(len(second), 10)
        self.assertEqual([row["issue_number"] for row in second], list(range(16, 6, -1)))
        self.assertTrue(set(row["issue_number"] for row in first).isdisjoint(
            row["issue_number"] for row in second
        ))

        last, _, last_offset, _ = repository.page_for_repository(
            "octocat/example", search="", limit=10, offset=1000
        )
        self.assertEqual(last_offset, 20)
        self.assertEqual([row["issue_number"] for row in last], list(range(6, 0, -1)))

        widgets, widget_total, _, _ = repository.page_for_repository(
            "octocat/example", search="WIDGET", limit=10, offset=0
        )
        self.assertEqual(widget_total, 1)
        self.assertEqual(widgets[0]["issue_number"], 40)

        by_provider, provider_total, _, _ = repository.page_for_repository(
            "octocat/example", search="codex", limit=10, offset=0
        )
        self.assertEqual(provider_total, 1)
        self.assertEqual(by_provider[0]["issue_number"], 40)

        by_status, status_total, _, _ = repository.page_for_repository(
            "octocat/example", search="completed", limit=10, offset=0
        )
        self.assertEqual(status_total, 1)
        self.assertEqual(by_status[0]["final_status"], "completed")

        by_number, number_total, _, _ = repository.page_for_repository(
            "octocat/example", search="40", limit=10, offset=0
        )
        self.assertEqual(number_total, 1)
        self.assertEqual(by_number[0]["issue_title"], "Ship widget")

        literal_percent, percent_total, _, _ = repository.page_for_repository(
            "octocat/example", search="100%", limit=10, offset=0
        )
        self.assertEqual(percent_total, 1)
        self.assertEqual(literal_percent[0]["issue_number"], 7)
        any_percent, any_percent_total, _, _ = repository.page_for_repository(
            "octocat/example", search="%", limit=10, offset=0
        )
        self.assertEqual(any_percent_total, 1)

        literal_underscore, underscore_total, _, _ = repository.page_for_repository(
            "octocat/example", search="a_b", limit=10, offset=0
        )
        self.assertEqual(underscore_total, 1)
        self.assertEqual(literal_underscore[0]["issue_number"], 8)

        body_only, body_total, _, _ = repository.page_for_repository(
            "octocat/example", search="secret-body-token", limit=10, offset=0
        )
        self.assertEqual(body_total, 0)
        self.assertEqual(body_only, [])

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            exit_code = execution_history_main(
                [
                    "--db", str(database_path),
                    "--repository", "octocat/example",
                    "--limit", "10",
                    "--offset", "0",
                    "--search", "widget",
                ]
            )
        self.assertEqual(exit_code, 0)
        payload = json.loads(buffer.getvalue())
        self.assertEqual(payload["total"], 1)
        self.assertEqual(payload["limit"], 10)
        self.assertEqual(len(payload["records"]), 1)
        self.assertEqual(payload["records"][0]["issue_number"], 40)
        self.assertNotIn("octocat/other", buffer.getvalue())

    def test_execution_history_sanitizes_credentials(self) -> None:
        token = "ghp_abcdefghijklmnopqrstuvwxyz123456"
        cleaned = sanitize_text(f"Authorization: Bearer {token}\napi_key={token}")
        self.assertNotIn(token, cleaned)
        self.assertEqual(cleaned.count("[REDACTED]"), 2)

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

    def test_required_input_posts_clear_question_and_uses_waiting_label(self) -> None:
        self.worker.issue = IssueContext(
            157, "Signing failure", "Body", ["Ready For Testing"], "https://example.invalid/157"
        )

        def fake_run_ai(_prompt: str) -> int:
            self.worker.ai_output_file.write_text(
                "## Action required\nConfigure the signing secrets and reply `done`.\n\n"
                "## Summary\nPublishing cannot sign artifacts without the private key.\n\n"
                "## Recommendations\nDo not paste the private key into this issue.\n\n"
                "## Step-by-step guide\n1. Open repository secrets.\n2. Add both values.\n\n"
                "SWARM_NEEDS_INPUT\n",
                encoding="utf-8",
            )
            return 0

        with (
            mock.patch.object(self.worker, "provider_usage", return_value=ProviderUsage(0, 100.0)),
            mock.patch.object(
                self.worker, "maybe_apply_dynamic_routing", wraps=self.worker.maybe_apply_dynamic_routing
            ) as routing,
            mock.patch.object(self.worker, "post_started_comment"),
            mock.patch.object(self.worker, "run_ai", side_effect=fake_run_ai),
            mock.patch.object(self.worker, "comments", return_value=[]),
            mock.patch.object(self.worker.github, "gh", return_value="") as github,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            status = self.worker.run_selected_issue()

        self.assertEqual(status, ISSUE_COMPLETED_EXIT_CODE)
        routing.assert_called_once_with()
        self.assertIn(157, self.worker.completed_numbers())
        self.assertFalse(self.worker.in_progress_file.exists())
        bodies = [call.args[2] for call in github.call_args_list if len(call.args) > 2]
        self.assertEqual(len(bodies), 1)
        body = bodies[0]
        self.assertIn("# 🤖 AI needs your input", body)
        self.assertIn("## Action required", body)
        self.assertIn("## Summary", body)
        self.assertIn("## Recommendations", body)
        self.assertIn("## Step-by-step guide", body)
        self.assertIn("## How to resume", body)
        self.assertNotIn("SWARM_NEEDS_INPUT", body)
        flattened = [part for call in github.call_args_list for part in call.args[0]]
        self.assertIn("AI Needs Input", flattened)
        edit = next(call.args[0] for call in github.call_args_list if call.args[0][0:2] == ["issue", "edit"])
        self.assertIn("--remove-label", edit)
        self.assertIn("Ready For Testing", edit)

    def test_trusted_reply_removes_needs_input_label_before_rework(self) -> None:
        self.worker.issue = IssueContext(
            158, "Continue", "Body", ["AI Needs Input"], "https://example.invalid/158",
            work_type="followup", trigger_comment_id=401,
        )
        self.worker.choice = ProviderChoice("Claude", "test-model", "high", "session")
        with mock.patch.object(self.worker.github, "gh", return_value="") as github:
            self.worker.clear_needs_input_label()
        arguments = github.call_args.args[0]
        self.assertEqual(arguments[0:2], ["issue", "edit"])
        self.assertIn("--remove-label", arguments)
        self.assertIn("AI Needs Input", arguments)

    def test_question_label_runs_ai_but_posts_answer_without_code_or_ready_label(self) -> None:
        self.worker.issue = IssueContext(
            159, "How does routing work?", "Explain model selection.",
            ["Question", "Ready For Testing"], "https://example.invalid/159",
        )
        captured_prompt = ""

        def fake_run_ai(prompt: str) -> int:
            nonlocal captured_prompt
            captured_prompt = prompt
            self.worker.ai_output_file.write_text(
                "## Answer\nThe router selects a provider, then configured tiers select the model.\n\n"
                "## Evidence\n`resolve_routing_decision` applies the tier table.\n\n"
                "## Recommendations\n- None.\n\nSWARM_QUESTION_ANSWER\n",
                encoding="utf-8",
            )
            return 0

        with (
            mock.patch.object(self.worker, "provider_usage", return_value=ProviderUsage(0, 100.0)),
            mock.patch.object(
                self.worker, "maybe_apply_dynamic_routing", wraps=self.worker.maybe_apply_dynamic_routing
            ) as routing,
            mock.patch.object(self.worker, "post_started_comment"),
            mock.patch.object(self.worker, "run_ai", side_effect=fake_run_ai),
            mock.patch.object(self.worker, "comments", return_value=[]),
            mock.patch.object(self.worker.github, "gh", return_value="") as github,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            status = self.worker.run_selected_issue()

        self.assertEqual(status, ISSUE_COMPLETED_EXIT_CODE)
        routing.assert_called_once_with()
        self.assertIn("This issue is labelled Question", captured_prompt)
        self.assertIn("do not edit files", captured_prompt)
        bodies = [call.args[2] for call in github.call_args_list if len(call.args) > 2]
        self.assertEqual(len(bodies), 1)
        self.assertIn("# 🤖 AI answer", bodies[0])
        self.assertIn("## Answer", bodies[0])
        self.assertNotIn("SWARM_QUESTION_ANSWER", bodies[0])
        flattened = [part for call in github.call_args_list for part in call.args[0]]
        self.assertNotIn("--add-label", flattened)
        self.assertIn("--remove-label", flattened)
        self.assertIn("Ready For Testing", flattened)

    def test_question_label_rejects_repository_changes(self) -> None:
        self.worker.issue = IssueContext(
            160, "Question with accidental edit", "Explain this.", ["question"],
            "https://example.invalid/160",
        )

        def fake_run_ai(_prompt: str) -> int:
            (self.repo / "tracked.txt").write_text("changed\n", encoding="utf-8")
            self.worker.ai_output_file.write_text(
                "## Answer\nAn answer.\n\n## Evidence\nEvidence.\n\n"
                "## Recommendations\n- None.\n\nSWARM_QUESTION_ANSWER\n",
                encoding="utf-8",
            )
            return 0

        with (
            mock.patch.object(self.worker, "provider_usage", return_value=ProviderUsage(0, 100.0)),
            mock.patch.object(self.worker, "post_started_comment"),
            mock.patch.object(self.worker, "run_ai", side_effect=fake_run_ai),
            mock.patch.object(self.worker.github, "gh", return_value=""),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            with self.assertRaisesRegex(WorkerError, "question issues must remain code-free"):
                self.worker.run_selected_issue()

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

    # ---- a provider CLI rejecting the model it was told to use ------------

    GROK_UNKNOWN_MODEL = (
        '{"type":"error","message":"Couldn\'t set model \'grok-4.3\': Invalid params: '
        '\\"unknown model id\\". Run \'grok models\' to see available models."}\n'
        "Error: Couldn't set model 'grok-4.3': Invalid params: \"unknown model id\".\n"
    )

    def model_run_worker(self, model: str = "grok-4.3", effort: str = "low"):
        self.worker.write_state(self.paused_state(141))
        self.worker.choice = ProviderChoice("Grok", model, effort, "old-session")
        return self.worker

    def run_ai_with(self, worker, outcomes: list[tuple[int, str]]) -> tuple[int, list[tuple[str, str, str]], str]:
        """Drive run_ai with scripted (status, diagnostic text) attempts."""
        calls: list[tuple[str, str, str]] = []

        def fake_grok(prompt: str, env: dict[str, str]) -> int:
            calls.append((worker.choice.model, worker.choice.effort, worker.choice.session_id))
            status, diagnostic = outcomes[len(calls) - 1]
            worker.ai_diagnostic_file.write_text(diagnostic, encoding="utf-8")
            if status == 0:
                worker.ai_output_file.write_text("done\n", encoding="utf-8")
            return status

        output = io.StringIO()
        with mock.patch.object(worker, "_run_grok", side_effect=fake_grok), contextlib.redirect_stdout(output):
            status = worker.run_ai("prompt")
        return status, calls, output.getvalue()

    def test_model_rejection_pattern_matches_the_real_grok_error_but_not_ordinary_failures(self) -> None:
        self.assertTrue(MODEL_REJECTED_RE.search(self.GROK_UNKNOWN_MODEL))
        for rejected in (
            "There's an issue with the selected model (claude-foo). It may not exist",
            "Error: unsupported model 'x'",
            "model gpt-9 does not exist or you do not have access to it",
            "Fable 5.1 requires usage credits. Switch to another model to continue.",
        ):
            self.assertTrue(MODEL_REJECTED_RE.search(rejected), rejected)
        for ordinary in (
            "usage limit reached, resets at 5pm",
            "HTTP 502 Bad Gateway",
            "tests failed: 3 assertions",
            "Grok model call timed out",
        ):
            self.assertFalse(MODEL_REJECTED_RE.search(ordinary), ordinary)

    def test_a_rejected_model_falls_back_to_the_configured_model_and_retries_once(self) -> None:
        worker = self.model_run_worker()
        spec = worker.config.spec("grok")
        self.assertNotEqual(spec.model, "grok-4.3")

        status, calls, output = self.run_ai_with(
            worker, [(1, self.GROK_UNKNOWN_MODEL), (0, "")]
        )

        self.assertEqual(status, 0)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0], ("grok-4.3", "low", "old-session"))
        self.assertEqual(calls[1][:2], (spec.model, spec.effort))
        self.assertNotEqual(calls[1][2], "old-session")
        self.assertIn("does not offer model 'grok-4.3'", output)
        state = worker.read_state()
        self.assertEqual((state["model"], state["effort"]), (spec.model, spec.effort))

    def test_a_credit_only_model_is_rerouted_immediately_and_audited(self) -> None:
        self.worker.config = dataclasses.replace(
            self.worker.config, dynamic_model_routing=True
        )
        self.worker.issue = IssueContext(
            157, "Credit-only model", "ORIGINAL", [], "https://example.invalid/157"
        )
        self.worker.choice = ProviderChoice(
            "Claude", "fable", "high", "old-session", resume=True
        )
        self.worker.routing = {
            "provider": "claude",
            "selected_model": "fable",
            "reasoning_effort": "high",
        }
        self.worker.save_new_state(
            self.worker.issue, self.worker.choice, self.base_sha
        )
        calls: list[tuple[str, str, str]] = []

        def fake_claude(prompt: str, env: dict[str, str]) -> int:
            calls.append(
                (
                    self.worker.choice.model,
                    self.worker.choice.effort,
                    self.worker.choice.session_id,
                )
            )
            if len(calls) == 1:
                self.worker.ai_output_file.write_text(
                    "Fable 5.1 requires usage credits. Switch to another model to continue.\n",
                    encoding="utf-8",
                )
                return 1
            self.worker.ai_output_file.write_text("done\n", encoding="utf-8")
            return 0

        routed = self._routing_payload(
            selected_provider="claude", selected_model="fable"
        )
        output = io.StringIO()
        with (
            mock.patch.object(
                self.worker, "_run_claude", side_effect=fake_claude
            ),
            mock.patch(
                "swarm_issue_worker.run_provider_router", return_value=routed
            ) as router,
            contextlib.redirect_stdout(output),
        ):
            status = self.worker.run_ai("prompt")

        self.assertEqual(status, 0)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0], ("fable", "high", "old-session"))
        self.assertNotEqual(calls[1][0], "fable")
        self.assertNotEqual(calls[1][2], "old-session")
        router.assert_called_once()
        self.assertNotIn("fable", router.call_args.kwargs["prompt"].lower())
        state = self.worker.read_state()
        audit = state["routing_re_evaluation"]
        self.assertEqual(audit["trigger"], "model_requires_usage_credits")
        self.assertEqual(audit["original_model"], "fable")
        self.assertIn("requires usage credits", audit["provider_message"])
        self.assertIn("current account/configuration", audit["configuration"])
        self.assertEqual(audit["replacement_model"], calls[1][0])
        self.assertEqual(
            audit["previous_routing_decision"]["selected_model"], "fable"
        )
        self.assertEqual(
            state["routing_decision"]["re_evaluation"], audit
        )
        self.assertEqual(state["session_id"], calls[1][2])
        self.assertFalse(state["session_started"])
        self.assertIn("originally selected Claude fable", output.getvalue())
        self.assertIn("required separate usage credits", output.getvalue())

    def test_a_failure_that_is_not_a_model_rejection_is_not_retried(self) -> None:
        worker = self.model_run_worker()
        status, calls, _ = self.run_ai_with(worker, [(1, "HTTP 502 Bad Gateway")])
        self.assertEqual(status, 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(worker.choice.model, "grok-4.3")

    def test_nothing_to_fall_back_to_when_the_configured_model_was_the_one_rejected(self) -> None:
        spec = self.worker.config.spec("grok")
        worker = self.model_run_worker(spec.model, spec.effort)
        status, calls, _ = self.run_ai_with(worker, [(1, self.GROK_UNKNOWN_MODEL)])
        self.assertEqual(status, 1)
        self.assertEqual(len(calls), 1)

    def test_the_fallback_is_tried_once_not_in_a_loop(self) -> None:
        worker = self.model_run_worker()
        status, calls, _ = self.run_ai_with(
            worker, [(1, self.GROK_UNKNOWN_MODEL), (1, self.GROK_UNKNOWN_MODEL)]
        )
        self.assertEqual(status, 1)
        self.assertEqual(len(calls), 2)

    def test_grok_capacity_reflects_install_and_sign_in(self) -> None:
        # Empty --grok-bin in setUp -> not installed -> unavailable.
        self.assertEqual(self.worker.grok_capacity(), 2)

    def grok_signed_in_home(self) -> dict[str, str]:
        home = self.root / "grok-home"
        (home / ".grok").mkdir(parents=True, exist_ok=True)
        (home / ".grok" / "auth.json").write_text("{}", encoding="utf-8")
        return {"HOME": str(home)}

    def grok_usage_with(
        self, results: list[subprocess.CompletedProcess[str]], environment: dict[str, str] | None = None
    ):
        environment = environment if environment is not None else self.grok_signed_in_home()
        with (
            mock.patch.object(self.worker, "provider_bin", return_value="/test/grok"),
            mock.patch("swarm_issue_worker.command_available", return_value=True),
            mock.patch("swarm_issue_worker.run_command", side_effect=results) as run,
            mock.patch("swarm_issue_worker.time.sleep"),
            mock.patch.dict("os.environ", environment),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            if "XAI_API_KEY" not in environment:
                os.environ.pop("XAI_API_KEY", None)  # restored when patch.dict exits
            usage = self.worker.grok_usage()
        return usage, run

    @staticmethod
    def grok_limits(used: float, period: str = "week") -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            ["grok-rate-limits"], 0,
            stdout=json.dumps({"usedPercent": used, "period": period, "tier": "SuperGrok"}), stderr="",
        )

    def test_grok_usage_reports_the_real_account_allowance(self) -> None:
        usage, run = self.grok_usage_with([self.grok_limits(30.0)])
        self.assertEqual(usage.status, 0)
        self.assertEqual(usage.remaining_percent, 70.0)
        self.assertEqual(usage.detail, "week 70% remaining")
        self.assertIn("grok_rate_limits.py", str(run.call_args.args[0][1]))
        self.assertIn("/test/grok", run.call_args.args[0])

    def test_grok_usage_below_the_minimum_reserve_is_not_usable(self) -> None:
        usage, _ = self.grok_usage_with([self.grok_limits(95.0)])
        self.assertEqual(usage.status, 1)
        self.assertEqual(usage.remaining_percent, 5.0)

    def test_grok_usage_retries_one_transient_failure(self) -> None:
        failed = subprocess.CompletedProcess(["grok-rate-limits"], 1, stdout="", stderr="agent timeout")
        usage, run = self.grok_usage_with([failed, self.grok_limits(10.0, "month")])
        self.assertEqual(usage.status, 0)
        self.assertEqual(usage.detail, "month 90% remaining")
        self.assertEqual(run.call_count, 2)

    def test_grok_usage_is_unavailable_rather_than_assumed_full_when_unreadable(self) -> None:
        failed = subprocess.CompletedProcess(["grok-rate-limits"], 1, stdout="", stderr="Could not read Grok usage: boom")
        usage, run = self.grok_usage_with([failed, failed])
        self.assertEqual(usage.status, 2)
        self.assertIsNone(usage.remaining_percent)
        self.assertEqual(run.call_count, 2)

        garbled = subprocess.CompletedProcess(["grok-rate-limits"], 0, stdout="{\"period\": \"week\"}", stderr="")
        usage, _ = self.grok_usage_with([garbled])
        self.assertEqual(usage.status, 2)

    def test_grok_usage_with_only_an_api_key_has_no_allowance_to_read(self) -> None:
        empty_home = self.root / "no-grok-home"
        empty_home.mkdir()
        usage, run = self.grok_usage_with([], {"HOME": str(empty_home), "XAI_API_KEY": "test-key"})
        self.assertEqual(usage.status, 0)
        self.assertEqual(usage.remaining_percent, 100.0)
        self.assertIn("API-key", usage.detail)
        run.assert_not_called()

    def test_grok_usage_requires_sign_in(self) -> None:
        empty_home = self.root / "no-grok-home"
        empty_home.mkdir()
        usage, run = self.grok_usage_with([], {"HOME": str(empty_home)})
        self.assertEqual(usage.status, 2)
        run.assert_not_called()

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

    def promotion_worker(self, promote: bool = True, ahead: bool = True) -> Worker:
        """An auto-approving worker whose `origin/ai-main` is (optionally) one
        commit ahead of `origin/main`."""
        argv = self._worker_argv(auto=True) + (["--auto-promote"] if promote else [])
        if ahead:
            self.git("switch", "-q", "ai-main")
            (self.repo / "tracked.txt").write_text("base\nai work\n", encoding="utf-8")
            self.git("commit", "-q", "-am", "ai work #1")
            self.git("push", "-q", "origin", "ai-main")
            self.git("switch", "-q", "main")
        return Worker(Config.from_args(build_parser().parse_args(argv)))

    def test_auto_promote_opens_approves_and_merge_commits_the_integration_pr(self) -> None:
        worker = self.promotion_worker()
        pr_url = "https://example.invalid/pull/200"
        with (
            mock.patch.object(
                worker.github, "gh", side_effect=["[]", pr_url + "\n", "3" * 40, ""]
            ) as gh,
            mock.patch.object(worker, "approve_pull_request") as approve,
        ):
            result = worker.auto_promote_integration_branch("claude")
        self.assertEqual(result, pr_url)
        commands = [call.args[0] for call in gh.call_args_list]
        self.assertEqual(commands[1][:2], ["pr", "create"])
        self.assertEqual(commands[1][commands[1].index("--base") + 1], "main")
        self.assertEqual(commands[1][commands[1].index("--head") + 1], "ai-main")
        self.assertEqual(commands[3][:2], ["pr", "merge"])
        self.assertIn("--merge", commands[3])
        self.assertNotIn("--squash", commands[3])
        self.assertIn("--match-head-commit", commands[3])
        approve.assert_called_once()
        self.assertEqual(approve.call_args.args[:2], (pr_url, "claude"))

    def test_auto_promote_reuses_an_open_already_approved_pr(self) -> None:
        worker = self.promotion_worker()
        pr_url = "https://example.invalid/pull/201"
        listing = json.dumps(
            [{"url": pr_url, "headRefOid": "3" * 40, "mergeable": "MERGEABLE",
              "reviewDecision": "APPROVED"}]
        )
        with (
            mock.patch.object(worker.github, "gh", side_effect=[listing, "3" * 40, ""]) as gh,
            mock.patch.object(worker, "approve_pull_request") as approve,
        ):
            self.assertEqual(worker.auto_promote_integration_branch("codex"), pr_url)
        approve.assert_not_called()
        self.assertNotIn(["pr", "create"], [call.args[0][:2] for call in gh.call_args_list])

    def test_auto_promote_leaves_a_conflicting_promotion_pr_open(self) -> None:
        worker = self.promotion_worker()
        listing = json.dumps(
            [{"url": "https://example.invalid/pull/202", "headRefOid": "3" * 40,
              "mergeable": "CONFLICTING", "reviewDecision": ""}]
        )
        with (
            mock.patch.object(worker.github, "gh", return_value=listing) as gh,
            mock.patch.object(worker, "approve_pull_request") as approve,
        ):
            self.assertIsNone(worker.auto_promote_integration_branch("claude"))
        approve.assert_not_called()
        self.assertNotIn(["pr", "merge"], [call.args[0][:2] for call in gh.call_args_list])

    POLICY_ERROR = (
        "Command failed (/opt/homebrew/bin/gh pr merge https://example.invalid/pull/64 --merge): "
        "X Pull request Example/repo#64 is not mergeable: the base branch policy prohibits the merge."
    )

    def blocked_merge_gh(self, pr_url: str, comments: str = ""):
        """gh side effects for: no open PR, create, head sha, merge refused, then the comment calls."""
        return [
            "[]", pr_url + "\n", "3" * 40, WorkerError(self.POLICY_ERROR),
            comments, "",
        ]

    def test_detects_a_merge_refused_by_branch_protection(self) -> None:
        self.assertTrue(is_merge_blocked_by_policy(self.POLICY_ERROR))
        self.assertTrue(is_merge_blocked_by_policy("GH006: Protected branch update failed"))
        self.assertFalse(is_merge_blocked_by_policy("Command failed: gh: HTTP 502 Bad Gateway"))
        self.assertFalse(is_merge_blocked_by_policy("Pull request has merge conflicts"))

    def test_a_promotion_blocked_by_branch_protection_is_recorded_and_commented_once(self) -> None:
        worker = self.promotion_worker()
        pr_url = "https://example.invalid/pull/64"
        output = io.StringIO()
        with (
            mock.patch.object(worker.github, "gh", side_effect=self.blocked_merge_gh(pr_url)) as gh,
            mock.patch.object(worker, "approve_pull_request"),
            contextlib.redirect_stdout(output),
        ):
            result = worker.auto_promote_integration_branch("claude")

        self.assertIsNone(result)
        commands = [call.args[0] for call in gh.call_args_list]
        self.assertEqual([c[:2] for c in commands], [["pr", "list"], ["pr", "create"], ["pr", "view"], ["pr", "merge"], ["pr", "view"], ["pr", "comment"]])
        comment = gh.call_args_list[-1]
        self.assertIn(f"swarm-issue-worker:promotion-blocked:pr:{pr_url}", comment.args[2])
        self.assertIn("needs to merge it", comment.args[2])
        record = json.loads(worker.promotion_blocked_file().read_text(encoding="utf-8"))
        self.assertEqual((record["pr_url"], record["head_sha"]), (pr_url, "3" * 40))
        self.assertIn("needs a manual merge", output.getvalue())
        self.assertNotIn("Could not promote", output.getvalue())

    def test_a_blocked_promotion_pr_is_left_alone_until_its_head_changes(self) -> None:
        worker = self.promotion_worker()
        pr_url = "https://example.invalid/pull/64"
        with (
            mock.patch.object(worker.github, "gh", side_effect=self.blocked_merge_gh(pr_url)),
            mock.patch.object(worker, "approve_pull_request"),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            worker.auto_promote_integration_branch("claude")

        # Next cycle: same PR, same head -> no approval, no merge attempt, no noise.
        same = json.dumps([{"url": pr_url, "headRefOid": "3" * 40, "mergeable": "MERGEABLE", "reviewDecision": ""}])
        output = io.StringIO()
        with (
            mock.patch.object(worker.github, "gh", return_value=same) as gh,
            mock.patch.object(worker, "approve_pull_request") as approve,
            contextlib.redirect_stdout(output),
        ):
            self.assertIsNone(worker.auto_promote_integration_branch("claude"))
        self.assertEqual([call.args[0][:2] for call in gh.call_args_list], [["pr", "list"]])
        approve.assert_not_called()
        self.assertEqual(output.getvalue(), "")

        # New commits on ai-main change the head, so the merge is tried again
        # (and, being blocked again, the PR is not commented on twice).
        moved = json.dumps([{"url": pr_url, "headRefOid": "4" * 40, "mergeable": "MERGEABLE", "reviewDecision": ""}])
        marker = f"<!-- swarm-issue-worker:promotion-blocked:pr:{pr_url} -->"
        with (
            mock.patch.object(
                worker.github, "gh",
                side_effect=[moved, "4" * 40, WorkerError(self.POLICY_ERROR), marker + "\nearlier comment"],
            ) as gh,
            mock.patch.object(worker, "approve_pull_request") as approve,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertIsNone(worker.auto_promote_integration_branch("claude"))
        approve.assert_called_once()
        self.assertNotIn(["pr", "comment"], [call.args[0][:2] for call in gh.call_args_list])
        record = json.loads(worker.promotion_blocked_file().read_text(encoding="utf-8"))
        self.assertEqual(record["head_sha"], "4" * 40)

    def test_other_merge_failures_are_still_reported_and_not_recorded(self) -> None:
        worker = self.promotion_worker()
        output = io.StringIO()
        with (
            mock.patch.object(
                worker.github, "gh",
                side_effect=["[]", "https://example.invalid/pull/65\n", "3" * 40, WorkerError("gh: HTTP 502 Bad Gateway")],
            ),
            mock.patch.object(worker, "approve_pull_request"),
            contextlib.redirect_stdout(output),
        ):
            self.assertIsNone(worker.auto_promote_integration_branch("claude"))
        self.assertIn("Could not promote", output.getvalue())
        self.assertFalse(worker.promotion_blocked_file().exists())

    def test_a_failed_pr_comment_does_not_undo_the_blocked_record(self) -> None:
        worker = self.promotion_worker()
        pr_url = "https://example.invalid/pull/66"
        gh_effects = ["[]", pr_url + "\n", "3" * 40, WorkerError(self.POLICY_ERROR), WorkerError("comment failed")]
        output = io.StringIO()
        with (
            mock.patch.object(worker.github, "gh", side_effect=gh_effects),
            mock.patch.object(worker, "approve_pull_request"),
            contextlib.redirect_stdout(output),
        ):
            self.assertIsNone(worker.auto_promote_integration_branch("claude"))
        self.assertTrue(worker.promotion_blocked_file().exists())
        self.assertIn("could not comment", output.getvalue())

    def test_auto_promote_does_nothing_when_the_toggle_is_off(self) -> None:
        worker = self.promotion_worker(promote=False)
        with mock.patch.object(worker.github, "gh") as gh:
            self.assertIsNone(worker.auto_promote_integration_branch("claude"))
        gh.assert_not_called()

    def test_auto_promote_does_nothing_when_issue_pr_merging_is_off(self) -> None:
        worker = self.promotion_worker()
        worker.config = dataclasses.replace(worker.config, auto_approve=False)
        with mock.patch.object(worker.github, "gh") as gh:
            self.assertIsNone(worker.auto_promote_integration_branch("claude"))
        gh.assert_not_called()

    def test_auto_promote_does_nothing_when_integration_is_not_ahead(self) -> None:
        worker = self.promotion_worker(ahead=False)
        with mock.patch.object(worker.github, "gh") as gh:
            self.assertIsNone(worker.auto_promote_integration_branch("claude"))
        gh.assert_not_called()

    def test_auto_promote_failure_is_logged_not_raised(self) -> None:
        worker = self.promotion_worker()
        with mock.patch.object(worker.github, "gh", side_effect=WorkerError("gh failed")):
            self.assertIsNone(worker.auto_promote_integration_branch("claude"))

    def test_start_of_run_sweep_picks_an_enabled_provider_for_promotion(self) -> None:
        worker = self.promotion_worker()
        keys = [spec.key for spec in worker.config.providers]
        for enabled_keys, expected in (
            (keys, worker.config.preferred_provider),
            ([keys[-1]], keys[-1]),
            ([], None),
        ):
            worker.config = dataclasses.replace(
                worker.config,
                providers=tuple(
                    dataclasses.replace(spec, enabled=spec.key in enabled_keys)
                    for spec in worker.config.providers
                ),
            )
            with mock.patch.object(worker, "promote_integration_branch", return_value=None) as promote:
                worker.auto_promote_integration_branch()
            if expected is None:
                promote.assert_not_called()
            else:
                promote.assert_called_once_with(expected)

    def monitor_worker(self, monitor: bool = True) -> Worker:
        argv = self._worker_argv(auto=False) + (["--monitor-actions"] if monitor else [])
        return Worker(Config.from_args(build_parser().parse_args(argv)))

    @staticmethod
    def pipeline_run(name: str, conclusion: str, created: str, sha: str = "a" * 40,
                     status: str = "completed") -> dict[str, object]:
        return {
            "databaseId": abs(hash((name, created))) % 100000, "workflowName": name,
            "status": status, "conclusion": conclusion, "headSha": sha,
            "url": f"https://example.invalid/runs/{name}", "event": "push", "createdAt": created,
        }

    def monitor_gh(self, runs: list[dict[str, object]], issues: list[dict[str, str]] | None = None,
                   created_url: str = "https://example.invalid/issues/300"):
        def fake(arguments, provider=None, input_text=None):
            if arguments[:2] == ["run", "list"]:
                return json.dumps(runs)
            if arguments[:2] == ["run", "view"]:
                return "step output\nboom\n"
            if arguments[:2] == ["issue", "list"]:
                return json.dumps(issues or [])
            if arguments[:2] == ["issue", "create"]:
                return created_url + "\n"
            return ""
        return fake

    def test_monitor_actions_is_off_by_default_and_toggled_by_flag(self) -> None:
        self.assertFalse(build_parser().parse_args([]).monitor_actions)
        self.assertTrue(build_parser().parse_args(["--monitor-actions"]).monitor_actions)
        self.assertFalse(build_parser().parse_args(["--monitor-actions", "--no-monitor-actions"]).monitor_actions)

    def test_monitor_does_nothing_when_the_toggle_is_off(self) -> None:
        worker = self.monitor_worker(monitor=False)
        with mock.patch.object(worker.github, "gh") as gh:
            self.assertIsNone(worker.monitor_repository_actions())
        gh.assert_not_called()

    def test_monitor_files_a_labelled_assigned_issue_for_a_failing_pipeline(self) -> None:
        worker = self.monitor_worker()
        runs = [self.pipeline_run("Build", "failure", "2026-09-21T10:00:00Z", "b" * 40),
                self.pipeline_run("Lint", "success", "2026-09-21T09:00:00Z")]
        fake = self.monitor_gh(runs)
        with mock.patch.object(worker.github, "gh", side_effect=fake) as gh:
            issue = worker.monitor_repository_actions()
        assert issue is not None
        self.assertEqual((issue.number, issue.ci_monitor), (300, True))
        create = next(c for c in gh.call_args_list if c.args[0][:2] == ["issue", "create"])
        arguments = create.args[0]
        self.assertEqual(arguments[arguments.index("--assignee") + 1], worker.config.github_assignee)
        labels = [arguments[i + 1] for i, part in enumerate(arguments) if part == "--label"]
        self.assertEqual(labels, ["bug", "ci-failure"])
        body = create.args[2]
        self.assertIn("swarm-issue-worker:ci-failure:branch:ai-main;sha:" + "b" * 40, body)
        self.assertIn("Build", body)
        self.assertIn("boom", body)
        self.assertNotIn("Lint", body)
        self.assertEqual(issue.labels, ["bug", "ci-failure"])

    def test_monitor_ignores_healthy_running_and_superseded_failures(self) -> None:
        worker = self.monitor_worker()
        runs = [
            self.pipeline_run("Build", "success", "2026-09-21T11:00:00Z"),
            self.pipeline_run("Build", "failure", "2026-09-21T10:00:00Z"),  # superseded
            self.pipeline_run("Test", "", "2026-09-21T11:00:00Z", status="in_progress"),
            self.pipeline_run("Test", "failure", "2026-09-21T10:00:00Z"),  # verdict pending
            self.pipeline_run("Deploy", "cancelled", "2026-09-21T11:00:00Z"),
        ]
        with mock.patch.object(worker.github, "gh", side_effect=self.monitor_gh(runs)) as gh:
            self.assertIsNone(worker.monitor_repository_actions())
        self.assertNotIn(["issue", "create"], [c.args[0][:2] for c in gh.call_args_list])

    def test_monitor_does_not_refile_while_an_issue_is_open_or_for_the_same_commit(self) -> None:
        worker = self.monitor_worker()
        runs = [self.pipeline_run("Build", "failure", "2026-09-21T10:00:00Z", "b" * 40)]
        marker = "<!-- swarm-issue-worker:ci-failure:branch:ai-main;sha:{} -->"
        for issues in (
            [{"state": "OPEN", "body": marker.format("c" * 40)}],   # open, older commit
            [{"state": "CLOSED", "body": marker.format("b" * 40)}],  # closed, same commit
        ):
            with mock.patch.object(worker.github, "gh", side_effect=self.monitor_gh(runs, issues)) as gh:
                self.assertIsNone(worker.monitor_repository_actions())
            self.assertNotIn(["issue", "create"], [c.args[0][:2] for c in gh.call_args_list])

    def test_monitor_refiles_for_a_new_failure_after_the_old_issue_was_closed(self) -> None:
        worker = self.monitor_worker()
        runs = [self.pipeline_run("Build", "failure", "2026-09-21T10:00:00Z", "b" * 40)]
        issues = [
            {"state": "CLOSED", "body": "<!-- swarm-issue-worker:ci-failure:branch:ai-main;sha:" + "c" * 40 + " -->"},
            {"state": "OPEN", "body": "<!-- swarm-issue-worker:ci-failure:branch:other;sha:" + "d" * 40 + " -->"},
        ]
        with mock.patch.object(worker.github, "gh", side_effect=self.monitor_gh(runs, issues)):
            self.assertIsNotNone(worker.monitor_repository_actions())

    def test_monitor_skips_dry_runs_and_interrupted_issues(self) -> None:
        runs = [self.pipeline_run("Build", "failure", "2026-09-21T10:00:00Z")]
        dry = Worker(Config.from_args(build_parser().parse_args(
            self._worker_argv(auto=False) + ["--monitor-actions", "--dry-run"])))
        with mock.patch.object(dry.github, "gh", side_effect=self.monitor_gh(runs)) as gh:
            self.assertIsNone(dry.monitor_repository_actions())
        self.assertNotIn(["issue", "create"], [c.args[0][:2] for c in gh.call_args_list])
        busy = self.monitor_worker()
        busy.in_progress_file.write_text("{}", encoding="utf-8")
        with mock.patch.object(busy.github, "gh") as gh:
            self.assertIsNone(busy.monitor_repository_actions())
        gh.assert_not_called()

    def test_monitor_failure_is_logged_and_never_blocks_the_queue(self) -> None:
        worker = self.monitor_worker()
        with mock.patch.object(worker.github, "gh", side_effect=WorkerError("HTTP 403")):
            self.assertIsNone(worker.monitor_repository_actions())

    def test_run_works_the_monitor_issue_without_also_selecting_it(self) -> None:
        worker = self.monitor_worker()
        filed = IssueContext(number=300, title="Fix CI", body="", labels=["bug"],
                             url="https://example.invalid/issues/300", ci_monitor=True)
        with (
            mock.patch.object(worker, "deliver_pending"),
            mock.patch.object(worker, "reconcile_issue_pull_requests"),
            mock.patch.object(worker, "prepare_paused_resume", return_value=False),
            mock.patch.object(worker, "monitor_repository_actions", return_value=filed),
            mock.patch.object(worker, "select_issue") as select,
            mock.patch.object(worker, "run_selected_issue", return_value=10) as run_issue,
        ):
            self.assertEqual(worker.run(), 10)
        select.assert_not_called()
        run_issue.assert_called_once()
        self.assertIs(worker.issue, filed)

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

    def linked_branch_worker(self, number: int) -> None:
        self.worker.issue = IssueContext(number, "Linked branch", "", [], f"https://example.invalid/{number}")
        self.worker.choice = ProviderChoice("Claude", "test", "high", "session")

    def test_linked_branch_retries_transient_graphql_error(self) -> None:
        self.linked_branch_worker(111)
        issue_id = "I_kwDOExample"
        flaky = WorkerError(
            "Command failed (gh api graphql): gh: Something went wrong while executing "
            "your query on 2026-09-21T15:13:32Z. Please include `C391:3DF102` when reporting this issue."
        )
        with (
            mock.patch("swarm_issue_worker.time.sleep") as sleep,
            mock.patch.object(self.worker, "remote_branch_exists_at", return_value=False),
            mock.patch.object(
                self.worker.github,
                "gh",
                side_effect=[
                    json.dumps({"node_id": issue_id}),
                    flaky,
                    json.dumps({"data": {"createLinkedBranch": {"issue": {"id": issue_id}}}}),
                ],
            ) as github,
        ):
            self.worker.create_linked_issue_branch("ai/claude/issue-111", "1" * 40)

        self.assertEqual(github.call_count, 3)
        sleep.assert_called_once()

    def test_linked_branch_accepts_branch_created_despite_graphql_error(self) -> None:
        self.linked_branch_worker(111)
        issue_id = "I_kwDOExample"
        flaky = WorkerError("gh: Something went wrong while executing your query")
        with (
            mock.patch("swarm_issue_worker.time.sleep") as sleep,
            mock.patch.object(self.worker, "remote_branch_exists_at", return_value=True),
            mock.patch.object(
                self.worker.github, "gh", side_effect=[json.dumps({"node_id": issue_id}), flaky]
            ) as github,
        ):
            self.worker.create_linked_issue_branch("ai/claude/issue-111", "1" * 40)

        self.assertEqual(github.call_count, 2)
        sleep.assert_not_called()

    def test_linked_branch_gives_up_after_repeated_transient_errors(self) -> None:
        self.linked_branch_worker(111)
        flaky = WorkerError("gh: Something went wrong while executing your query")
        with (
            mock.patch("swarm_issue_worker.time.sleep"),
            mock.patch.object(self.worker, "remote_branch_exists_at", return_value=False),
            mock.patch.object(
                self.worker.github,
                "gh",
                side_effect=[json.dumps({"node_id": "I_x"}), flaky, flaky, flaky],
            ) as github,
        ):
            with self.assertRaisesRegex(WorkerError, "Something went wrong"):
                self.worker.create_linked_issue_branch("ai/claude/issue-111", "1" * 40)

        self.assertEqual(github.call_count, 4)

    def test_linked_branch_does_not_retry_permanent_errors(self) -> None:
        self.linked_branch_worker(111)
        denied = WorkerError("gh: Resource not accessible by integration (HTTP 403)")
        with (
            mock.patch("swarm_issue_worker.time.sleep") as sleep,
            mock.patch.object(self.worker, "remote_branch_exists_at", return_value=False),
            mock.patch.object(
                self.worker.github, "gh", side_effect=[json.dumps({"node_id": "I_x"}), denied]
            ) as github,
        ):
            with self.assertRaisesRegex(WorkerError, "not accessible"):
                self.worker.create_linked_issue_branch("ai/claude/issue-111", "1" * 40)

        self.assertEqual(github.call_count, 2)
        sleep.assert_not_called()

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

    def empty_repo_worker(self, *extra: str) -> tuple[Worker, Path, Path]:
        """A worker whose checkout was cloned from a brand-new, empty remote."""
        remote = self.root / "empty-remote.git"
        checkout = self.root / "empty-repo"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
        subprocess.run(
            ["git", "clone", "-q", str(remote), str(checkout)],
            check=True, stderr=subprocess.DEVNULL,
        )
        for key, value in (("user.name", "SWARM worker test"), ("user.email", "worker-test@example.invalid")):
            subprocess.run(["git", "-C", str(checkout), "config", key, value], check=True)
        argv = self._worker_argv(auto=True)
        argv[argv.index("--repo-dir") + 1] = str(checkout)
        argv.extend(["--github-repository", "acme/feedback", *extra])
        return Worker(Config.from_args(build_parser().parse_args(argv))), remote, checkout

    @staticmethod
    def remote_heads(remote: Path) -> list[str]:
        listing = subprocess.run(
            ["git", "-C", str(remote), "for-each-ref", "--format=%(refname:short)", "refs/heads"],
            text=True, stdout=subprocess.PIPE, check=True,
        ).stdout
        return sorted(listing.split())

    def test_empty_repository_gets_a_readme_main_and_then_an_issue_branch(self) -> None:
        worker, remote, checkout = self.empty_repo_worker()
        # A local unborn branch under another name must not matter.
        subprocess.run(["git", "-C", str(checkout), "symbolic-ref", "HEAD", "refs/heads/master"], check=True)
        worker.issue = IssueContext(1, "Initial application creation", "", [], "https://example.invalid/1")
        worker.choice = ProviderChoice("Claude", "test", "high", "session")

        run_start, recovery, _, _ = worker.prepare_repository()

        self.assertFalse(recovery)
        heads = self.remote_heads(remote)
        self.assertIn("main", heads)
        self.assertIn("ai-main", heads)
        readme = subprocess.run(
            ["git", "-C", str(remote), "show", "main:README.md"],
            text=True, stdout=subprocess.PIPE, check=True,
        ).stdout
        self.assertIn("# feedback", readme)
        self.assertIn("SWARM Automation", readme)
        self.assertEqual(
            subprocess.run(
                ["git", "-C", str(remote), "log", "--format=%s", "main"],
                text=True, stdout=subprocess.PIPE, check=True,
            ).stdout.split("\n")[:-1],
            ["Initial commit"],
        )
        current = subprocess.run(
            ["git", "-C", str(checkout), "branch", "--show-current"],
            text=True, stdout=subprocess.PIPE, check=True,
        ).stdout.strip()
        self.assertEqual(current, "ai/claude/issue-1")
        self.assertEqual(
            subprocess.run(
                ["git", "-C", str(checkout), "status", "--porcelain"],
                text=True, stdout=subprocess.PIPE, check=True,
            ).stdout,
            "",
        )
        self.assertTrue(run_start)

    def test_empty_repository_bootstrap_leaves_a_repository_with_branches_alone(self) -> None:
        # The standard fixture has commits locally and branches on the remote.
        self.assertFalse(self.worker.bootstrap_empty_repository())
        self.assertEqual(self.remote_heads(self.remote), ["ai-main", "main"])

        # Empty locally but the remote has branches: that is a real problem, not
        # a new repo, so nothing is pushed.
        worker, remote, checkout = self.empty_repo_worker()
        subprocess.run(["git", "-C", str(self.repo), "push", "-q", str(remote), "main"], check=True)
        self.assertFalse(worker.bootstrap_empty_repository())
        self.assertEqual(self.remote_heads(remote), ["main"])

    def test_empty_repository_bootstrap_never_overwrites_checkout_files(self) -> None:
        worker, remote, checkout = self.empty_repo_worker()
        (checkout / "README.md").write_text("mine\n", encoding="utf-8")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertFalse(worker.bootstrap_empty_repository())
        self.assertEqual(self.remote_heads(remote), [])
        self.assertEqual((checkout / "README.md").read_text(encoding="utf-8"), "mine\n")
        self.assertIn("not creating main over them", output.getvalue())

    def test_empty_repository_bootstrap_does_nothing_in_a_dry_run(self) -> None:
        worker, remote, _ = self.empty_repo_worker("--dry-run")
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertFalse(worker.bootstrap_empty_repository())
        self.assertEqual(self.remote_heads(remote), [])
        self.assertIn("Dry run", output.getvalue())

    def test_failed_empty_repository_bootstrap_changes_nothing_and_can_be_retried(self) -> None:
        worker, remote, checkout = self.empty_repo_worker()
        hook = remote / "hooks" / "pre-receive"
        hook.write_text("#!/bin/sh\necho 'push rejected for the test' >&2\nexit 1\n", encoding="utf-8")
        hook.chmod(0o755)

        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(WorkerError, "Could not create main in the empty repository"):
                worker.bootstrap_empty_repository()
        self.assertEqual(self.remote_heads(remote), [])
        self.assertFalse(
            subprocess.run(
                ["git", "-C", str(checkout), "rev-parse", "--verify", "--quiet", "HEAD"],
                stdout=subprocess.DEVNULL,
            ).returncode == 0,
            "the local checkout stays empty so the next cycle can retry",
        )
        self.assertEqual(
            subprocess.run(
                ["git", "-C", str(checkout), "status", "--porcelain"],
                text=True, stdout=subprocess.PIPE, check=True,
            ).stdout,
            "",
        )

        hook.unlink()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(worker.bootstrap_empty_repository())
        self.assertEqual(self.remote_heads(remote), ["main"])

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

    def test_untracked_swarm_draft_does_not_block_new_issue_work(self) -> None:
        worker = self.pr_worker()
        worker.issue = IssueContext(57, "Reference ROMs", "", [], "https://example.invalid/57")
        worker.choice = ProviderChoice("Claude", "test", "high", "session")
        (self.repo / ".swarm").mkdir()
        draft = self.repo / ".swarm" / "tests.json"
        draft.write_text('{"version": 1}\n', encoding="utf-8")

        run_start, recovery, _, dirty = worker.prepare_repository()

        self.assertFalse(recovery)
        self.assertFalse(dirty)
        self.assertEqual(run_start, self.git("rev-parse", "ai-main"))
        self.assertEqual(self.git("branch", "--show-current"), "ai/claude/issue-57")
        self.assertEqual(draft.read_text(encoding="utf-8"), '{"version": 1}\n')
        self.assertEqual(self.git("status", "--porcelain"), "?? .swarm/")
        self.assertNotIn(".swarm/tests.json", self.git("ls-files").splitlines())

    def test_tracked_swarm_edit_still_defers_new_issue_work(self) -> None:
        (self.repo / ".swarm").mkdir()
        definition = self.repo / ".swarm" / "tests.json"
        definition.write_text('{"version": 1}\n', encoding="utf-8")
        self.git("add", ".swarm/tests.json")
        self.git("commit", "-q", "-m", "track the test definition")
        definition.write_text('{"version": 1, "changed": true}\n', encoding="utf-8")
        worker = self.pr_worker()
        worker.issue = IssueContext(58, "Should wait", "", [], "https://example.invalid/58")
        worker.choice = ProviderChoice("Claude", "test", "high", "session")

        with self.assertRaises(SystemExit) as raised:
            worker.prepare_repository()

        self.assertEqual(raised.exception.code, 0)
        self.assertFalse(worker.in_progress_file.exists())
        self.assertEqual(self.git("branch", "--show-current"), "main")
        self.assertIn("changed", definition.read_text(encoding="utf-8"))

    def test_other_untracked_file_still_defers_new_issue_work(self) -> None:
        (self.repo / "scratch.txt").write_text("not the app draft\n", encoding="utf-8")
        worker = self.pr_worker()
        worker.issue = IssueContext(60, "Should wait", "", [], "https://example.invalid/60")
        worker.choice = ProviderChoice("Claude", "test", "high", "session")

        with self.assertRaises(SystemExit) as raised:
            worker.prepare_repository()

        self.assertEqual(raised.exception.code, 0)
        self.assertFalse(worker.in_progress_file.exists())
        self.assertTrue((self.repo / "scratch.txt").is_file())

    # ---- product versioning (VERSION file, `minor` label) ------------------

    def version_repo(self, main_version: str = "0.1.5", ai_main_version: str | None = None) -> None:
        """Track a VERSION file on main and ai-main (optionally further ahead on ai-main)."""
        (self.repo / "VERSION").write_text(f"# product version\n{main_version}\n", encoding="utf-8")
        self.git("add", "VERSION")
        self.git("commit", "-q", "-m", "Add VERSION")
        self.git("push", "-q", "origin", "main")
        self.git("branch", "-f", "ai-main", "main")
        if ai_main_version:
            self.git("switch", "-q", "ai-main")
            (self.repo / "VERSION").write_text(f"# product version\n{ai_main_version}\n", encoding="utf-8")
            self.git("commit", "-q", "-am", "Bump minor")
            self.git("switch", "-q", "main")
        self.git("push", "-q", "-f", "origin", "ai-main")

    def labelled_issue_worker(self, labels: list[str], number: int = 501):
        worker = self.pr_worker()
        worker.issue = IssueContext(number, "Versioned work", "", labels, f"https://example.invalid/{number}")
        worker.choice = ProviderChoice("Claude", "test", "high", "session")
        run_start, _, _, _ = worker.prepare_repository()
        return worker, run_start

    @staticmethod
    def label_events(actor: str, label: str = "minor") -> list[dict[str, object]]:
        return [
            {"event": "labeled", "label": {"name": "other"}, "actor": {"login": "someone"}},
            {"event": "labeled", "label": {"name": label}, "actor": {"login": actor}},
        ]

    def committed_files(self) -> list[str]:
        return self.git("show", "--name-only", "--format=", "HEAD").splitlines()

    def test_version_file_helpers(self) -> None:
        self.assertEqual(parse_version_file("# c\n\n0.1.9\n"), (0, 1, 9))
        for bad in ("", "0.1\n", "v0.1.1\n", "0.1.1\n0.1.2\n", "0.1.1-beta\n", "# only a comment\n"):
            self.assertIsNone(parse_version_file(bad), repr(bad))
        self.assertEqual(bump_minor_in_version_text("# c\n0.1.9\n"), "# c\n0.2.0\n")
        self.assertEqual(bump_minor_in_version_text("1.4.22"), "1.5.0\n")
        with self.assertRaises(WorkerError):
            bump_minor_in_version_text("nonsense\n")

    def test_minor_label_from_a_trusted_user_bumps_the_minor_once(self) -> None:
        self.version_repo("0.1.5")
        worker, run_start = self.labelled_issue_worker(["minor", "enhancement"])
        (self.repo / "feature.txt").write_text("new\n", encoding="utf-8")
        with mock.patch.object(worker.github, "api_list", return_value=self.label_events("DotNetRockStar")) as api:
            committed = worker.commit_completed_work(run_start)

        self.assertNotEqual(committed, run_start)
        self.assertEqual(sorted(self.committed_files()), ["VERSION", "feature.txt"])
        self.assertEqual((self.repo / "VERSION").read_text(encoding="utf-8"), "# product version\n0.2.0\n")
        self.assertIn("issues/501/events", api.call_args.args[0])

    def test_minor_label_applied_by_an_untrusted_user_is_ignored(self) -> None:
        self.version_repo("0.1.5")
        worker, run_start = self.labelled_issue_worker(["minor"])
        (self.repo / "feature.txt").write_text("new\n", encoding="utf-8")
        output = io.StringIO()
        with (
            mock.patch.object(worker.github, "api_list", return_value=self.label_events("drive-by-user")),
            contextlib.redirect_stdout(output),
        ):
            worker.commit_completed_work(run_start)

        self.assertEqual(self.committed_files(), ["feature.txt"])
        self.assertIn("not a trusted author", output.getvalue())

    def test_no_minor_label_means_no_bump_and_no_api_call(self) -> None:
        self.version_repo("0.1.5")
        worker, run_start = self.labelled_issue_worker(["bug"])
        (self.repo / "fix.txt").write_text("fix\n", encoding="utf-8")
        with mock.patch.object(worker.github, "api_list") as api:
            worker.commit_completed_work(run_start)
        api.assert_not_called()
        self.assertEqual(self.committed_files(), ["fix.txt"])

    def test_minor_label_alone_does_not_create_a_commit(self) -> None:
        self.version_repo("0.1.5")
        worker, run_start = self.labelled_issue_worker(["minor"])
        with mock.patch.object(worker.github, "api_list", return_value=self.label_events("DotNetRockStar")) as api:
            committed = worker.commit_completed_work(run_start)
        self.assertEqual(committed, run_start)
        api.assert_not_called()
        self.assertEqual((self.repo / "VERSION").read_text(encoding="utf-8"), "# product version\n0.1.5\n")

    def test_only_one_minor_bump_per_release(self) -> None:
        # ai-main already carries 0.2.0 that main (0.1.5) has not shipped yet.
        self.version_repo("0.1.5", ai_main_version="0.2.0")
        worker, run_start = self.labelled_issue_worker(["minor"])
        self.assertEqual((self.repo / "VERSION").read_text(encoding="utf-8"), "# product version\n0.2.0\n")
        (self.repo / "feature.txt").write_text("new\n", encoding="utf-8")
        output = io.StringIO()
        with (
            mock.patch.object(worker.github, "api_list", return_value=self.label_events("DotNetRockStar")),
            contextlib.redirect_stdout(output),
        ):
            worker.commit_completed_work(run_start)
        self.assertEqual(self.committed_files(), ["feature.txt"])
        self.assertEqual((self.repo / "VERSION").read_text(encoding="utf-8"), "# product version\n0.2.0\n")
        self.assertIn("one bump per release", output.getvalue())

    def test_an_ai_edit_to_version_is_discarded_without_the_label(self) -> None:
        self.version_repo("0.1.5")
        worker, run_start = self.labelled_issue_worker(["bug"])
        (self.repo / "VERSION").write_text("# product version\n9.9.9\n", encoding="utf-8")
        (self.repo / "fix.txt").write_text("fix\n", encoding="utf-8")
        worker.commit_completed_work(run_start)
        self.assertEqual(self.committed_files(), ["fix.txt"])
        self.assertEqual((self.repo / "VERSION").read_text(encoding="utf-8"), "# product version\n0.1.5\n")

    def test_a_version_edit_the_ai_already_committed_is_put_back(self) -> None:
        self.version_repo("0.1.5")
        worker, run_start = self.labelled_issue_worker(["bug"])
        (self.repo / "VERSION").write_text("# product version\n9.9.9\n", encoding="utf-8")
        (self.repo / "fix.txt").write_text("fix\n", encoding="utf-8")
        self.git("add", "--all")
        self.git("commit", "-q", "-m", "[claude] Fix it and bump the version (#501)")
        committed = worker.commit_completed_work(run_start)
        self.assertNotEqual(committed, run_start)
        self.assertEqual(self.git("show", "HEAD:VERSION"), self.git("show", f"{run_start}:VERSION"))
        self.assertEqual((self.repo / "VERSION").read_text(encoding="utf-8"), "# product version\n0.1.5\n")
        self.assertEqual(self.git("diff", run_start, "HEAD", "--name-only"), "fix.txt")

    def test_an_edit_to_version_alone_leaves_nothing_to_commit(self) -> None:
        self.version_repo("0.1.5")
        worker, run_start = self.labelled_issue_worker(["bug"])
        (self.repo / "VERSION").write_text("# product version\n9.9.9\n", encoding="utf-8")
        self.assertEqual(worker.commit_completed_work(run_start), run_start)
        self.assertEqual((self.repo / "VERSION").read_text(encoding="utf-8"), "# product version\n0.1.5\n")

    def test_repositories_without_a_version_file_are_left_alone(self) -> None:
        worker, run_start = self.labelled_issue_worker(["minor"])
        (self.repo / "feature.txt").write_text("new\n", encoding="utf-8")
        with mock.patch.object(worker.github, "api_list") as api:
            worker.commit_completed_work(run_start)
        api.assert_not_called()
        self.assertEqual(self.committed_files(), ["feature.txt"])
        self.assertFalse((self.repo / "VERSION").exists())

    def test_prompt_tells_the_ai_not_to_edit_version_when_the_repo_has_one(self) -> None:
        self.version_repo("0.1.5")
        worker, _ = self.labelled_issue_worker(["bug"], number=502)
        self.assertIn("Do not edit the `VERSION` file", worker.build_prompt(False, "", False))

    def test_prompt_does_not_mention_version_when_the_repo_has_none(self) -> None:
        worker, _ = self.labelled_issue_worker(["bug"], number=503)
        self.assertNotIn("`VERSION`", worker.build_prompt(False, "", False))

    def test_issue_commit_leaves_the_untracked_swarm_draft_out(self) -> None:
        worker = self.pr_worker()
        worker.issue = IssueContext(59, "Commit beside draft", "", [], "https://example.invalid/59")
        worker.choice = ProviderChoice("Claude", "test", "high", "session")
        run_start, _, _, _ = worker.prepare_repository()
        (self.repo / "completed.txt").write_text("done\n", encoding="utf-8")
        draft = self.repo / ".swarm" / "tests.json"
        draft.parent.mkdir()
        draft.write_text('{"version": 1}\n', encoding="utf-8")

        committed = worker.commit_completed_work(run_start)

        self.assertNotEqual(committed, run_start)
        committed_files = self.git("show", "--name-only", "--format=", "HEAD").splitlines()
        self.assertEqual(committed_files, ["completed.txt"])
        self.assertEqual(draft.read_text(encoding="utf-8"), '{"version": 1}\n')
        self.assertEqual(self.git("status", "--porcelain"), "?? .swarm/")
        self.assertEqual(worker.worktree_status(), "")

    def test_pause_stash_leaves_the_untracked_swarm_draft_in_place(self) -> None:
        self.git("switch", "-q", "-c", "ai/claude/issue-101")
        self.worker.write_state(self.paused_state())
        (self.repo / "tracked.txt").write_text("base\npaused change\n", encoding="utf-8")
        draft = self.repo / ".swarm" / "tests.json"
        draft.parent.mkdir()
        draft.write_text('{"version": 1}\n', encoding="utf-8")

        self.worker.suspend_paused()

        self.assertEqual(draft.read_text(encoding="utf-8"), '{"version": 1}\n')
        self.assertEqual(self.git("status", "--porcelain"), "?? .swarm/")
        self.assertEqual((self.repo / "tracked.txt").read_text(encoding="utf-8"), "base\n")
        paused_file = self.worker.paused_dir / "101.json"
        self.worker.restore_paused(paused_file)
        self.assertIn("paused change", (self.repo / "tracked.txt").read_text(encoding="utf-8"))
        self.assertEqual(draft.read_text(encoding="utf-8"), '{"version": 1}\n')

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
        self.assertNotIn("SWARM AI Routing", body)
        self.assertTrue(is_worker_comment({"body": body}))
        self.assertTrue(self.worker.read_state()["started_comment_posted"])

    def _routing_payload(self, **overrides: object) -> str:
        payload: dict[str, object] = {
            "task_type": "debugging",
            "complexity": 7,
            "risk": "medium",
            "context_requirement": "large",
            "selected_provider": "codex",
            "provider_reason": "Codex is best at test-driven bug fixes like this one.",
            "selected_model": "gpt-5.6-luna",
            "reasoning_effort": "low",
            "confidence": 0.91,
            "prompt_grade": "B+",
            "grade_reason": "Clear objective and context, but acceptance criteria are incomplete.",
        }
        payload.update(overrides)
        return json.dumps(payload)

    def test_dynamic_routing_defaults_off_and_keeps_the_manual_model(self) -> None:
        self.assertFalse(self.worker.config.dynamic_model_routing)
        self.assertEqual(self.worker.config.spec("claude").router_model, "claude-haiku-4-5")
        self.assertEqual(self.worker.config.spec("codex").router_model, "gpt-5.6-luna")
        self.assertEqual(self.worker.config.spec("codex").router_effort, "low")
        self.assertEqual(self.worker.config.spec("grok").router_model, "grok-4.6")
        self.worker.issue = IssueContext(501, "Manual", "ORIGINAL", [], "https://example.invalid/501")
        self.worker.choice = ProviderChoice("Codex", "gpt-5.6-luna", "medium", "")
        with mock.patch("swarm_issue_worker.run_provider_router") as router:
            self.worker.maybe_apply_dynamic_routing()
        router.assert_not_called()
        self.assertIsNone(self.worker.routing)
        self.assertEqual(self.worker.choice.model, "gpt-5.6-luna")
        self.assertEqual(self.worker.choice.effort, "medium")
        self.assertEqual(self.worker.issue.body, "ORIGINAL")

    def test_dynamic_routing_selects_the_tier_and_leaves_the_worker_prompt_unchanged(self) -> None:
        self.worker.config = dataclasses.replace(self.worker.config, dynamic_model_routing=True)
        body = "Keep this sentence exactly.\nAcceptance: the toggle persists."
        self.worker.issue = IssueContext(502, "Route me", body, ["enhancement"], "https://example.invalid/502")
        self.worker.choice = ProviderChoice("Codex", "gpt-5.6-luna", "medium", "session-502")
        self.worker.save_new_state(self.worker.issue, self.worker.choice, self.base_sha)
        before = self.worker.build_prompt(False, "", False)
        with mock.patch("swarm_issue_worker.run_provider_router", return_value=self._routing_payload()) as router:
            self.worker.maybe_apply_dynamic_routing()
        router.assert_called_once()
        self.assertIn(body, router.call_args.kwargs["prompt"])
        self.assertEqual(self.worker.issue.body, body)
        self.assertEqual(self.worker.choice.model, "gpt-5.6-sol")
        self.assertEqual(self.worker.choice.effort, "high")
        self.assertFalse(self.worker.routing["fallback"])
        self.assertEqual(self.worker.build_prompt(False, "", False), before)
        self.assertEqual(self.worker.read_state()["model"], "gpt-5.6-sol")
        self.assertEqual(self.worker.read_state()["routing_decision"]["prompt_grade"], "B+")
        self.assertEqual(self.worker.read_state()["routing_decision"]["provider"], "codex")

    def test_dynamic_routing_falls_back_and_still_posts_the_configured_model(self) -> None:
        self.worker.config = dataclasses.replace(self.worker.config, dynamic_model_routing=True)
        body = "ORIGINAL ISSUE"
        self.worker.issue = IssueContext(503, "Fallback", body, [], "https://example.invalid/503")
        self.worker.choice = ProviderChoice("Codex", "gpt-5.6-luna", "medium", "session-503")
        self.worker.save_new_state(self.worker.issue, self.worker.choice, self.base_sha)
        with mock.patch(
            "swarm_issue_worker.run_provider_router",
            side_effect=RouterError("router returned an empty response"),
        ):
            self.worker.maybe_apply_dynamic_routing()
        self.assertEqual(self.worker.choice.model, "gpt-5.6-luna")
        self.assertEqual(self.worker.choice.effort, "medium")
        self.assertTrue(self.worker.routing["fallback"])
        self.assertEqual(self.worker.issue.body, body)
        with (
            mock.patch.object(self.worker, "comments", return_value=[]),
            mock.patch.object(self.worker.github, "gh", return_value="") as github,
        ):
            self.worker.post_started_comment()
        notice = github.call_args.args[2]
        self.assertIn("SWARM AI Routing", notice)
        self.assertIn("fell back", notice)
        self.assertIn("Selected Model: GPT-5.6 Luna", notice)
        self.assertIn("router returned an empty response", notice)

    def test_dynamic_routing_comment_reports_the_grade_without_rewriting_the_issue(self) -> None:
        self.worker.issue = IssueContext(504, "Graded", "ORIGINAL", [], "https://example.invalid/504")
        self.worker.choice = ProviderChoice("Codex", "gpt-5.6-sol", "high", "session-504")
        self.worker.routing = json.loads(self._routing_payload())
        self.worker.routing.update(
            {
                "provider": "codex",
                "provider_name": "Codex",
                "provider_candidates": ["codex", "claude", "grok"],
                "selected_model": "gpt-5.6-sol",
                "reasoning_effort": "high",
                "fallback": False,
            }
        )
        self.worker.save_new_state(self.worker.issue, self.worker.choice, self.base_sha)
        with (
            mock.patch.object(self.worker, "comments", return_value=[]),
            mock.patch.object(self.worker.github, "gh", return_value="") as github,
        ):
            self.worker.post_started_comment()
        notice = github.call_args.args[2]
        self.assertIn("Prompt Grade: B+", notice)
        self.assertIn("Complexity: 7/10", notice)
        self.assertIn("Selected Model: GPT-5.6 Sol", notice)
        self.assertIn("Reasoning: High", notice)
        self.assertIn("Routing Confidence: 91%", notice)
        self.assertIn("Selected AI: Codex", notice)
        self.assertIn("AI Tools Considered: Codex, Claude, Grok", notice)
        self.assertIn("Why Codex: Codex is best at test-driven bug fixes", notice)
        self.assertIn("acceptance criteria are incomplete", notice)
        self.assertEqual(self.worker.issue.body, "ORIGINAL")

    def test_dynamic_routing_does_not_reroute_a_resumed_session(self) -> None:
        self.worker.config = dataclasses.replace(self.worker.config, dynamic_model_routing=True)
        self.worker.issue = IssueContext(505, "Resume", "ORIGINAL", [], "https://example.invalid/505")
        self.worker.choice = ProviderChoice("Codex", "gpt-5.6-luna", "medium", "session-505", resume=True)
        with mock.patch("swarm_issue_worker.run_provider_router") as router:
            self.worker.maybe_apply_dynamic_routing()
        router.assert_not_called()
        self.assertIsNone(self.worker.routing)
        self.assertEqual(self.worker.choice.model, "gpt-5.6-luna")

    def test_dynamic_routing_reuses_a_retried_attempts_decision_when_still_valid(self) -> None:
        # A prior cycle for this issue picked a real tier ("gpt-5.6-sol"),
        # then the worker process exited (e.g. transient failure) before
        # delivering anything. The retry that follows must resume the exact
        # same model rather than asking the router again.
        self.worker.config = dataclasses.replace(self.worker.config, dynamic_model_routing=True)
        issue = IssueContext(508, "Retry", "ORIGINAL", [], "https://example.invalid/508")
        self.worker.issue = issue
        self.worker.choice = ProviderChoice("Codex", "gpt-5.6-luna", "medium", "session-508")
        self.worker.routing = json.loads(
            self._routing_payload(selected_provider="codex", selected_model="gpt-5.6-sol", reasoning_effort="high")
        )
        self.worker.routing.update({"provider": "codex", "fallback": False})
        self.worker.choice = ProviderChoice("Codex", "gpt-5.6-sol", "high", "session-508")
        self.worker.save_new_state(issue, self.worker.choice, self.base_sha)

        # A fresh worker process picks up the retry with the plain configured
        # model, exactly as the scheduler restarts it.
        self.worker.choice = ProviderChoice("Codex", "gpt-5.6-luna", "medium", "session-508")
        with mock.patch("swarm_issue_worker.run_provider_router") as router:
            self.worker.maybe_apply_dynamic_routing()
        router.assert_not_called()
        self.assertEqual(self.worker.choice.model, "gpt-5.6-sol")
        self.assertEqual(self.worker.choice.effort, "high")

    def test_dynamic_routing_reroutes_a_retried_attempts_decision_the_catalog_no_longer_offers(self) -> None:
        # Regression for the live "fable requires usage credits" incident:
        # a prior cycle fell back to a model the catalog has since dropped
        # (retired, or repaired away because it needs usage credits the
        # account doesn't have). The retry must not keep replaying that
        # stale pick forever — it should re-route against the current,
        # healed configuration instead.
        self.worker.config = dataclasses.replace(self.worker.config, dynamic_model_routing=True)
        issue = IssueContext(509, "Stale fallback", "ORIGINAL", [], "https://example.invalid/509")
        self.worker.issue = issue
        self.worker.routing = json.loads(
            self._routing_payload(selected_provider="claude", selected_model="fable", reasoning_effort="high")
        )
        self.worker.routing.update({"provider": "claude", "fallback": True})
        self.worker.choice = ProviderChoice("Claude", "fable", "high", "session-509")
        self.worker.save_new_state(issue, self.worker.choice, self.base_sha)

        # A fresh worker process picks up the retry; the configured model has
        # since been healed back to a real one by the desktop app.
        self.worker.choice = ProviderChoice("Claude", "claude-sonnet-5", "low", "session-509")
        with mock.patch(
            "swarm_issue_worker.run_provider_router",
            return_value=self._routing_payload(selected_provider="claude"),
        ) as router:
            self.worker.maybe_apply_dynamic_routing()
        router.assert_called_once()
        self.assertNotEqual(self.worker.choice.model, "fable")
        self.assertFalse(self.worker.routing["fallback"])

    def test_dynamic_routing_hands_the_issue_to_the_tool_that_suits_it(self) -> None:
        self.worker.config = dataclasses.replace(self.worker.config, dynamic_model_routing=True)
        self.worker.issue = IssueContext(506, "Route the tool", "ORIGINAL", [], "https://example.invalid/506")
        self.worker.choice = ProviderChoice("Codex", "gpt-5.6-luna", "medium", "session-506")
        self.worker.provider_usages = {
            "Claude": ProviderUsage(0, 80.0, "week 80% remaining"),
            "Codex": ProviderUsage(0, 90.0, "week 90% remaining"),
            "Grok": ProviderUsage(0, 70.0, "week 70% remaining"),
        }
        self.worker.provider_priority = ("Codex", "Claude", "Grok")
        payload = self._routing_payload(
            selected_provider="grok",
            provider_reason="Grok is quickest on a small scripted change.",
        )
        with mock.patch("swarm_issue_worker.run_provider_router", return_value=payload) as router:
            self.worker.maybe_apply_dynamic_routing()
        prompt = router.call_args.kwargs["prompt"]
        for key in ("codex", "claude", "grok"):
            self.assertIn(f"- {key} (", prompt)
        # The router still runs on the provider that was picked for capacity.
        self.assertEqual(router.call_args.kwargs["provider"], "codex")
        self.assertEqual(self.worker.choice.name, "Grok")
        self.assertEqual(self.worker.choice.model, "grok-4.6")
        self.assertEqual(self.worker.choice.effort, "high")
        self.assertTrue(self.worker.choice.session_id)
        self.assertEqual(self.worker.expected_branch(), "ai/xai/issue-506")
        self.assertEqual(self.worker.start_usage, self.worker.provider_usages["Grok"])
        self.assertEqual(self.worker.routing["provider"], "grok")
        self.assertEqual(
            self.worker.routing["provider_reason"],
            "Grok is quickest on a small scripted change.",
        )
        self.assertEqual(self.worker.issue.body, "ORIGINAL")

    def test_dynamic_routing_only_offers_tools_that_have_capacity(self) -> None:
        self.worker.config = dataclasses.replace(self.worker.config, dynamic_model_routing=True)
        self.worker.issue = IssueContext(507, "Capacity", "ORIGINAL", [], "https://example.invalid/507")
        self.worker.choice = ProviderChoice("Grok", "grok-4.6", "low", "session-507")
        self.worker.provider_priority = ("Grok", "Claude")
        with mock.patch(
            "swarm_issue_worker.run_provider_router",
            return_value=self._routing_payload(selected_provider="codex"),
        ) as router:
            self.worker.maybe_apply_dynamic_routing()
        prompt = router.call_args.kwargs["prompt"]
        self.assertIn("- grok (", prompt)
        self.assertIn("- claude (", prompt)
        self.assertNotIn("- codex (", prompt)
        # Codex has no capacity this pass, so the issue stays with Grok.
        self.assertEqual(self.worker.choice.name, "Grok")
        self.assertIn("not an available AI tool", self.worker.routing["provider_override_reason"])

    def test_dynamic_routing_favors_another_tool_when_reworking(self) -> None:
        self.worker.config = dataclasses.replace(self.worker.config, dynamic_model_routing=True)
        self.worker.issue = IssueContext(
            508, "Rework", "ORIGINAL", [], "https://example.invalid/508",
            work_type="followup", previous_ai="Codex",
        )
        self.worker.choice = ProviderChoice("Claude", "claude-sonnet-5", "low", "session-508")
        self.worker.provider_priority = ("Claude", "Grok", "Codex")
        with mock.patch(
            "swarm_issue_worker.run_provider_router",
            return_value=self._routing_payload(selected_provider="codex", confidence=0.55),
        ) as router:
            self.worker.maybe_apply_dynamic_routing()
        prompt = router.call_args.kwargs["prompt"]
        self.assertIn("codex completed the previous pass", prompt)
        self.assertEqual(self.worker.choice.name, "Claude")
        self.assertIn("Rework:", self.worker.routing["provider_override_reason"])
        with (
            mock.patch.object(self.worker, "comments", return_value=[]),
            mock.patch.object(self.worker.github, "gh", return_value="") as github,
        ):
            self.worker.save_new_state(self.worker.issue, self.worker.choice, self.base_sha)
            self.worker.post_started_comment()
        notice = github.call_args.args[2]
        self.assertIn("Selected AI: Claude", notice)
        self.assertIn("Rework: Codex completed the previous pass", notice)

    def test_dynamic_routing_keeps_the_previous_tool_when_it_is_clearly_better(self) -> None:
        self.worker.config = dataclasses.replace(self.worker.config, dynamic_model_routing=True)
        self.worker.issue = IssueContext(
            509, "Rework", "ORIGINAL", [], "https://example.invalid/509",
            work_type="followup", previous_ai="Codex",
        )
        self.worker.choice = ProviderChoice("Claude", "claude-sonnet-5", "low", "session-509")
        self.worker.provider_priority = ("Claude", "Codex")
        with mock.patch(
            "swarm_issue_worker.run_provider_router",
            return_value=self._routing_payload(
                selected_provider="codex",
                confidence=0.95,
                provider_reason="Codex already has the failing test reproduced.",
            ),
        ):
            self.worker.maybe_apply_dynamic_routing()
        self.assertEqual(self.worker.choice.name, "Codex")
        self.assertEqual(self.worker.choice.model, "gpt-5.6-sol")
        self.assertEqual(self.worker.routing["provider_override_reason"], "")

    def test_dynamic_routing_cannot_change_tools_on_an_owned_branch(self) -> None:
        self.worker.config = dataclasses.replace(self.worker.config, dynamic_model_routing=True)
        self.worker.issue = IssueContext(510, "Owned", "ORIGINAL", [], "https://example.invalid/510")
        self.worker.choice = ProviderChoice("Codex", "gpt-5.6-luna", "medium", "session-510")
        self.worker.save_new_state(self.worker.issue, self.worker.choice, self.base_sha)
        self.worker.provider_priority = ("Codex", "Claude", "Grok")
        with mock.patch(
            "swarm_issue_worker.run_provider_router",
            return_value=self._routing_payload(selected_provider="grok"),
        ) as router:
            self.worker.maybe_apply_dynamic_routing()
        self.assertNotIn("- grok (", router.call_args.kwargs["prompt"])
        self.assertEqual(self.worker.choice.name, "Codex")
        self.assertEqual(self.worker.choice.model, "gpt-5.6-sol")
        self.assertEqual(self.worker.read_state()["routing_decision"]["provider"], "codex")

    def test_provider_strengths_reach_the_router_prompt(self) -> None:
        parsed = build_parser().parse_args(
            self._worker_argv(auto=False) + ["--grok-router-strengths", "Grok is best at scripting"]
        )
        config = Config.from_args(parsed)
        self.assertEqual(config.spec("grok").strengths, "Grok is best at scripting")
        self.assertIn("refactor", config.spec("claude").strengths.lower())
        worker = Worker(dataclasses.replace(config, dynamic_model_routing=True))
        worker.issue = IssueContext(511, "Strengths", "ORIGINAL", [], "https://example.invalid/511")
        worker.choice = ProviderChoice("Grok", "grok-4.6", "low", "session-511")
        worker.provider_priority = ("Grok", "Claude")
        with mock.patch(
            "swarm_issue_worker.run_provider_router",
            return_value=self._routing_payload(selected_provider="grok"),
        ) as router:
            worker.maybe_apply_dynamic_routing()
        self.assertIn("Best at: Grok is best at scripting", router.call_args.kwargs["prompt"])

    def test_invalid_routing_tier_json_is_rejected(self) -> None:
        with self.assertRaises(WorkerError):
            Config.from_args(build_parser().parse_args(self._worker_argv(auto=False) + ["--routing-tiers", "{"]))

    def test_execution_history_stores_the_routing_decision(self) -> None:
        database_path = self.state / "routing-history.sqlite3"
        decision = {
            "provider": "codex",
            "provider_name": "Codex",
            "provider_reason": "Codex is best at test-driven bug fixes like this one.",
            "provider_override_reason": "",
            "provider_candidates": ["claude", "codex", "grok"],
            "router_provider": "claude",
            "task_type": "debugging",
            "complexity": 7,
            "risk": "medium",
            "context_requirement": "large",
            "selected_model": "gpt-5.6-sol",
            "reasoning_effort": "high",
            "confidence": 0.91,
            "prompt_grade": "B+",
            "grade_reason": "Clear objective and context, but acceptance criteria are incomplete.",
            "fallback": False,
        }
        service = ExecutionHistoryService(True, database_path)
        execution_id = service.start(
            ExecutionStart(
                repository="octocat/example",
                issue_number=506,
                issue_url="https://github.com/octocat/example/issues/506",
                issue_title="Route",
                issue_body="ORIGINAL",
                provider="Codex",
                model="gpt-5.6-sol",
                effort="high",
                branch_name="ai/codex/issue-506",
                application_version="1.2.3",
                routing_decision=decision,
            ),
            "2026-09-21T10:00:00-05:00",
        )
        repository = ExecutionHistoryRepository(database_path)
        with repository.connect() as database:
            row = database.execute(
                "SELECT model, effort, routing_decision, original_issue_body FROM ai_executions "
                "WHERE execution_id = ?",
                (execution_id,),
            ).fetchone()
        assert row is not None
        self.assertEqual(row["model"], "gpt-5.6-sol")
        self.assertEqual(row["effort"], "high")
        self.assertEqual(row["original_issue_body"], "ORIGINAL")
        stored = json.loads(row["routing_decision"])
        self.assertEqual(stored["prompt_grade"], "B+")
        # Which AI tool was chosen, out of which set, and why, are all recorded.
        self.assertEqual(stored["provider"], "codex")
        self.assertEqual(stored["provider_candidates"], ["claude", "codex", "grok"])
        self.assertEqual(stored["router_provider"], "claude")
        self.assertIn("test-driven", stored["provider_reason"])

    def test_prompt_grades_page_lists_only_graded_runs_with_a_summary(self) -> None:
        self.assertEqual(set(GRADE_POINTS), set(PROMPT_GRADES))
        database_path = self.state / "grades-history.sqlite3"
        service_args = dict(
            repository="octocat/example",
            issue_url="",
            issue_body="SECRET BODY",
            provider="Codex",
            model="m",
            effort="high",
            branch_name="b",
            application_version="1",
        )
        decisions = {
            1: {"prompt_grade": "A", "grade_reason": "Clear.", "complexity": 3, "fallback": False},
            2: {"prompt_grade": "C", "grade_reason": "Vague.", "complexity": 6, "fallback": False},
            3: {"prompt_grade": "", "grade_reason": "router unavailable", "fallback": True},
            4: None,
        }
        for number, decision in decisions.items():
            ExecutionHistoryService(True, database_path).start(
                ExecutionStart(
                    issue_number=number,
                    issue_title=f"Issue {number}",
                    routing_decision=decision,
                    **service_args,
                ),
                f"2026-09-2{number}T10:00:00-05:00",
            )
        repository = ExecutionHistoryRepository(database_path)
        page = repository.graded_for_repository("octocat/example")
        self.assertEqual([r["issue_number"] for r in page["records"]], [2, 1])
        self.assertEqual(page["total"], 2)
        self.assertNotIn("original_issue_body", page["records"][0])
        self.assertEqual(page["records"][0]["routing_decision"]["grade_reason"], "Vague.")
        summary = page["summary"]
        self.assertEqual(summary["graded"], 2)
        self.assertEqual(summary["averagePoints"], 3.0)
        self.assertEqual(summary["averageGrade"], "B")
        self.assertEqual(summary["distribution"]["A"], 1)
        self.assertEqual(summary["distribution"]["C"], 1)
        self.assertEqual(summary["distribution"]["F"], 0)

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            exit_code = execution_history_main(
                ["--db", str(database_path), "--repository", "octocat/example",
                 "--grades", "--limit", "1", "--offset", "99"]
            )
        self.assertEqual(exit_code, 0)
        cli_page = json.loads(buffer.getvalue())
        self.assertEqual(cli_page["offset"], 1)
        self.assertEqual([r["issue_number"] for r in cli_page["records"]], [1])

        missing = io.StringIO()
        with contextlib.redirect_stdout(missing):
            execution_history_main(
                ["--db", str(self.state / "none.sqlite3"), "--repository", "x/y", "--grades"]
            )
        empty = json.loads(missing.getvalue())
        self.assertEqual(empty["total"], 0)
        self.assertIsNone(empty["summary"]["averagePoints"])

    def test_prompt_grades_search_and_grade_filter_page_like_history(self) -> None:
        database_path = self.state / "grades-filter.sqlite3"
        service_args = dict(
            repository="octocat/example",
            issue_url="",
            issue_body="SECRET BODY",
            provider="Codex",
            model="m",
            effort="high",
            branch_name="b",
            application_version="1",
        )
        rows = [
            (1, "Alpha widget", {"prompt_grade": "A", "grade_reason": "Clear.", "fallback": False}),
            (2, "Beta", {"prompt_grade": "B-", "grade_reason": "Thin.", "fallback": False}),
            (3, "Widget beta", {"prompt_grade": "B-", "grade_reason": "Named.", "fallback": False}),
            (4, "Skipped", {"prompt_grade": "", "grade_reason": "router unavailable", "fallback": True}),
        ]
        for number, title, decision in rows:
            ExecutionHistoryService(True, database_path).start(
                ExecutionStart(
                    issue_number=number,
                    issue_title=title,
                    routing_decision=decision,
                    **service_args,
                ),
                f"2026-09-2{number}T10:00:00-05:00",
            )
        # Enough B- rows to prove the grade filter is paged in SQLite, not sliced
        # after the whole history is loaded.
        for number in range(5, 16):
            ExecutionHistoryService(True, database_path).start(
                ExecutionStart(
                    issue_number=number,
                    issue_title=f"Extra {number}",
                    routing_decision={"prompt_grade": "B-", "grade_reason": "Extra.", "fallback": False},
                    **service_args,
                ),
                f"2026-09-21T10:{number:02d}:00-05:00",
            )
        repository = ExecutionHistoryRepository(database_path)

        searched = repository.graded_for_repository("octocat/example", search="  widget ")
        self.assertEqual([row["issue_number"] for row in searched["records"]], [3, 1])
        self.assertEqual(searched["total"], 2)
        self.assertEqual(searched["summary"]["distribution"]["A"], 1)
        self.assertEqual(searched["summary"]["distribution"]["B-"], 1)
        self.assertEqual(searched["summary"]["graded"], 2)
        self.assertNotIn("original_issue_body", searched["records"][0])

        only_b_minus = repository.graded_for_repository(
            "octocat/example", search="widget", grade="B-"
        )
        self.assertEqual([row["issue_number"] for row in only_b_minus["records"]], [3])
        self.assertEqual(only_b_minus["total"], 1)
        # The chart still counts every grade in the search, so another bar can be chosen.
        self.assertEqual(only_b_minus["summary"]["distribution"]["A"], 1)
        self.assertEqual(only_b_minus["summary"]["graded"], 2)

        grade_page = repository.graded_for_repository("octocat/example", grade=" B- ")
        self.assertEqual(grade_page["total"], 13)
        self.assertEqual(len(grade_page["records"]), 10)
        self.assertTrue(all(
            row["routing_decision"]["prompt_grade"] == "B-" for row in grade_page["records"]
        ))
        self.assertEqual(grade_page["summary"]["distribution"]["A"], 1)
        self.assertEqual(grade_page["summary"]["graded"], 14)
        last = repository.graded_for_repository("octocat/example", grade="B-", offset=99)
        self.assertEqual(last["offset"], 10)
        self.assertEqual(len(last["records"]), 3)

        ignored = repository.graded_for_repository("octocat/example", grade="nope")
        self.assertEqual(ignored["total"], 14)

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            exit_code = execution_history_main(
                ["--db", str(database_path), "--repository", "octocat/example",
                 "--grades", "--search", "widget", "--grade", "B-", "--limit", "10"]
            )
        self.assertEqual(exit_code, 0)
        cli_page = json.loads(buffer.getvalue())
        self.assertEqual([row["issue_number"] for row in cli_page["records"]], [3])
        self.assertEqual(cli_page["summary"]["distribution"]["A"], 1)

    def test_router_matrix_counts_who_graded_and_who_they_picked(self) -> None:
        database_path = self.state / "grades-routers.sqlite3"
        service_args = dict(
            repository="octocat/example",
            issue_url="",
            issue_body="SECRET BODY",
            model="m",
            effort="high",
            branch_name="b",
            application_version="1",
        )
        # (issue, grading platform, platform it picked, grade)
        rows = [
            (1, "claude", "Claude", "A"),
            (2, "claude", "Codex", "B"),
            (3, "claude", "Codex", "B-"),
            (4, "grok", "Grok", "C"),
            (5, "grok", "Claude", "A-"),
        ]
        for number, router, worked, grade in rows:
            ExecutionHistoryService(True, database_path).start(
                ExecutionStart(
                    issue_number=number,
                    issue_title=f"Issue {number}",
                    provider=worked,
                    routing_decision={
                        "prompt_grade": grade,
                        "grade_reason": "Reason.",
                        "fallback": False,
                        "provider": worked.lower(),
                        "router_provider": router,
                        "router_model": f"{router}-router",
                        "router_effort": "low",
                    },
                    **service_args,
                ),
                f"2026-09-2{number}T10:00:00-05:00",
            )
        # A fallback row is never graded, so it must not reach the matrix.
        ExecutionHistoryService(True, database_path).start(
            ExecutionStart(
                issue_number=6,
                issue_title="Ungraded",
                provider="Codex",
                routing_decision={
                    "prompt_grade": "",
                    "fallback": True,
                    "router_provider": "codex",
                },
                **service_args,
            ),
            "2026-09-26T10:00:00-05:00",
        )
        repository = ExecutionHistoryRepository(database_path)

        page = repository.graded_for_repository("octocat/example")
        matrix = page["routerMatrix"]
        self.assertEqual([row["router"] for row in matrix], ["claude", "grok"])
        self.assertEqual(matrix[0]["graded"], 3)
        self.assertEqual(
            matrix[0]["selections"],
            [
                {"provider": "codex", "count": 2, "percent": 66.7},
                {"provider": "claude", "count": 1, "percent": 33.3},
            ],
        )
        self.assertEqual(matrix[1]["graded"], 2)
        self.assertEqual({entry["percent"] for entry in matrix[1]["selections"]}, {50.0})
        # The grading model and effort ride along on every record so the UI can
        # show which model graded without a second lookup.
        self.assertEqual(page["records"][0]["routing_decision"]["router_model"], "grok-router")
        self.assertEqual(page["records"][0]["routing_decision"]["router_effort"], "low")

        # Filtering by the grading platform narrows the page and the grade
        # summary, but never the matrix — another router must stay pickable.
        only_claude = repository.graded_for_repository("octocat/example", router=" Claude ")
        self.assertEqual([row["issue_number"] for row in only_claude["records"]], [3, 2, 1])
        self.assertEqual(only_claude["total"], 3)
        self.assertEqual(only_claude["summary"]["graded"], 3)
        self.assertEqual(only_claude["summary"]["distribution"]["C"], 0)
        self.assertEqual([row["router"] for row in only_claude["routerMatrix"]], ["claude", "grok"])

        # Both filters together, and the grade chart still counts the router's
        # other grades so a different bar can be chosen.
        narrowed = repository.graded_for_repository("octocat/example", router="claude", grade="B")
        self.assertEqual([row["issue_number"] for row in narrowed["records"]], [2])
        self.assertEqual(narrowed["summary"]["graded"], 3)

        # Anything that is not a provider key is no filter at all.
        self.assertEqual(
            repository.graded_for_repository("octocat/example", router="../etc")["total"], 5
        )
        self.assertEqual(normalize_provider_key(" CLAUDE "), "claude")
        self.assertEqual(normalize_provider_key("drop table"), "")
        self.assertEqual(normalize_provider_key("-lead"), "")

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            exit_code = execution_history_main(
                ["--db", str(database_path), "--repository", "octocat/example",
                 "--grades", "--router", "grok", "--limit", "10"]
            )
        self.assertEqual(exit_code, 0)
        cli_page = json.loads(buffer.getvalue())
        self.assertEqual([row["issue_number"] for row in cli_page["records"]], [5, 4])
        self.assertEqual(len(cli_page["routerMatrix"]), 2)

        missing = io.StringIO()
        with contextlib.redirect_stdout(missing):
            execution_history_main(
                ["--db", str(self.state / "none.sqlite3"), "--repository", "x/y", "--grades"]
            )
        self.assertEqual(json.loads(missing.getvalue())["routerMatrix"], [])

    def test_router_matrix_keeps_rows_whose_router_was_never_recorded(self) -> None:
        # Dropping them would make the percentages disagree with the grade
        # count; they are reported under "" and the UI marks them unfilterable.
        matrix = summarize_router_matrix(
            [("", "codex", 2), ("claude", "codex", 1), ("claude", "claude", 3), ("grok", "grok", 0)]
        )
        self.assertEqual([row["router"] for row in matrix], ["claude", ""])
        self.assertEqual(matrix[0]["selections"][0], {"provider": "claude", "count": 3, "percent": 75.0})
        self.assertEqual(matrix[1]["graded"], 2)
        self.assertEqual(summarize_router_matrix([]), [])

    def test_execution_history_migration_adds_routing_decision(self) -> None:
        database_path = self.state / "legacy-history.sqlite3"
        connection = sqlite3.connect(database_path)
        connection.executescript(
            """
            CREATE TABLE schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            INSERT INTO schema_migrations(version) VALUES (1);
            CREATE TABLE ai_executions (
                execution_id TEXT PRIMARY KEY,
                repository TEXT NOT NULL,
                issue_number INTEGER NOT NULL,
                issue_url TEXT NOT NULL DEFAULT '',
                issue_title TEXT NOT NULL,
                original_issue_body TEXT NOT NULL,
                effective_prompt TEXT NOT NULL DEFAULT '',
                ai_provider TEXT NOT NULL,
                model TEXT NOT NULL DEFAULT '',
                effort TEXT NOT NULL DEFAULT '',
                reasoning_config TEXT NOT NULL DEFAULT '{}',
                started_at TEXT NOT NULL,
                completed_at TEXT,
                duration_seconds REAL,
                requested_work_summary TEXT NOT NULL DEFAULT '',
                changes_summary TEXT NOT NULL DEFAULT '',
                files_changed TEXT NOT NULL DEFAULT '[]',
                branch_name TEXT NOT NULL DEFAULT '',
                commit_shas TEXT NOT NULL DEFAULT '[]',
                pull_request_number INTEGER,
                pull_request_url TEXT NOT NULL DEFAULT '',
                operational_notes TEXT NOT NULL DEFAULT '[]',
                warnings_errors TEXT NOT NULL DEFAULT '[]',
                final_status TEXT NOT NULL,
                attempt_number INTEGER NOT NULL,
                application_version TEXT NOT NULL DEFAULT '',
                prompt_template_version TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL,
                uploaded_at TEXT,
                upload_status TEXT NOT NULL DEFAULT 'never_uploaded',
                upload_error TEXT NOT NULL DEFAULT '',
                uploaded_record_updated_at TEXT,
                reviewer_feedback TEXT NOT NULL DEFAULT '',
                reviewer_feedback_at TEXT,
                UNIQUE(repository, issue_number, attempt_number)
            );
            """
        )
        connection.commit()
        connection.close()
        repository = ExecutionHistoryRepository(database_path)
        with repository.connect() as database:
            columns = {row[1] for row in database.execute("PRAGMA table_info(ai_executions)")}
            versions = {row[0] for row in database.execute("SELECT version FROM schema_migrations")}
        self.assertIn("routing_decision", columns)
        self.assertIn(2, versions)

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

    def test_deliver_pull_request_pushes_a_new_commit_on_a_reused_branch_instead_of_treating_it_as_already_merged(
        self,
    ) -> None:
        """A branch name is reused across every work-round on the same
        issue. If round one's PR under that name already merged (`merged_head`
        below), a *new* commit from a later round on the same branch name
        must still be delivered -- not silently dropped because a merged PR
        with that head branch name already exists (see
        issue-branch-delivery.md). Uses `self.worker` (--no-auto-approve/
        --no-auto-merge) rather than `pr_worker()` so `deliver_pull_request`
        stops once the PR itself is created/reused, without also exercising
        the separate approve/merge machinery."""
        worker = self.worker
        worker.issue = IssueContext(500, "Reused branch", "", [], "https://example.invalid/500")
        worker.choice = ProviderChoice("Claude", "test", "high", "session-500")
        run_start, _, _, _ = worker.prepare_repository()
        (self.repo / "round-one.txt").write_text("round one\n", encoding="utf-8")
        worker.commit_completed_work(run_start)
        branch = worker.expected_branch()
        self.git("push", "-q", "-u", "origin", branch)

        # Simulate round one's PR merging into ai-main on the remote.
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
            ["git", "-C", str(merger), "merge", "-q", "--no-ff", "FETCH_HEAD", "-m", "merge round one"],
            check=True,
        )
        subprocess.run(["git", "-C", str(merger), "push", "-q", "origin", "ai-main"], check=True)
        merged_head = subprocess.run(
            ["git", "-C", str(merger), "rev-parse", "HEAD"], text=True,
            stdout=subprocess.PIPE, check=True,
        ).stdout.strip()
        worker.return_to_integration_branch(branch)

        # Round two: the same branch name is reused for a follow-up work
        # round, with a genuinely new commit round one's (already-merged) PR
        # knows nothing about.
        self.git("switch", "-c", branch)
        (self.repo / "round-two.txt").write_text("round two\n", encoding="utf-8")
        self.git("add", "round-two.txt")
        self.git("commit", "-q", "-m", "round two work")
        second_commit = self.git("rev-parse", "HEAD")

        stale_merged_pr = json.dumps(
            [
                {
                    "url": "https://example.invalid/pull/900",
                    "state": "MERGED",
                    "baseRefName": "ai-main",
                    "mergeCommit": {"oid": merged_head},
                }
            ]
        )
        with mock.patch.object(
            worker.github, "gh", side_effect=[stale_merged_pr, "https://example.invalid/pull/901"]
        ) as gh:
            pr_url, delivered_branch, delivered_sha = worker.deliver_pull_request(second_commit)

        self.assertEqual(pr_url, "https://example.invalid/pull/901")
        self.assertEqual(delivered_branch, branch)
        self.assertEqual(delivered_sha, second_commit)
        self.assertEqual(gh.call_count, 2)
        # The new commit must actually have reached the remote -- not just
        # been reported as delivered.
        remote_branch_head = subprocess.run(
            ["git", "rev-parse", branch], cwd=str(self.remote), text=True,
            stdout=subprocess.PIPE, check=True,
        ).stdout.strip()
        self.assertEqual(remote_branch_head, second_commit)

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
        self.assertFalse(args.auto_promote)
        self.assertTrue(build_parser().parse_args(["--auto-promote"]).auto_promote)
        # delivery-mode / merge-method were removed with the integration model.
        with self.assertRaises(SystemExit):
            build_parser().parse_args(["--delivery-mode", "pull-request"])
        overridden = build_parser().parse_args(
            ["--github-repository", "example/repo", "--preferred-provider", "codex",
             "--integration-branch", "staging"]
        )
        self.assertEqual(overridden.github_repository, "example/repo")
        self.assertEqual(overridden.preferred_provider, "codex")
        self.assertEqual(
            build_parser().parse_args(["--preferred-provider", "auto"]).preferred_provider,
            "auto",
        )
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

    def test_scheduler_defers_checkout_owned_by_test_run(self) -> None:
        with tempfile.TemporaryDirectory(prefix="swarm-runner-test-lock.") as temporary:
            root = Path(temporary)
            repo = root / "repo"
            state = root / "state"
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "runner test"], check=True)
            subprocess.run(
                ["git", "-C", str(repo), "config", "user.email", "runner@example.invalid"], check=True
            )
            subprocess.run(
                ["git", "-C", str(repo), "commit", "-q", "--allow-empty", "-m", "base"], check=True
            )
            (repo / ".git" / "swarm-test-run.lock").write_text(
                f"{os.getpid()}\n", encoding="utf-8"
            )
            args = runner_module.build_parser().parse_args(
                ["--repo-dir", str(repo), "--state-dir", str(state)]
            )
            runner = runner_module.Runner(args, [])

            self.assertFalse(runner.synchronize_repository(runner.repos[0]))

    def test_scheduler_recovers_a_checkout_with_only_harmless_untracked_files(self) -> None:
        """A stale issue branch left over from an interrupted work-round
        (no in-progress-issue.json, so no owner) whose tree already matches
        the integration branch on the remote -- exactly what an untracked
        scratch file plus a leftover checkout looks like -- should be
        repositioned automatically instead of deferring every cycle
        forever (see issue-branch-delivery.md)."""
        with tempfile.TemporaryDirectory(prefix="swarm-runner-recover-test.") as temporary:
            root = Path(temporary)
            repo = root / "repo"
            remote = root / "remote.git"
            state = root / "state"
            subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            for name, value in (("user.name", "runner test"), ("user.email", "runner@example.invalid")):
                subprocess.run(["git", "-C", str(repo), "config", name, value], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-q", "--allow-empty", "-m", "base"], check=True)
            subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", str(remote)], check=True)
            subprocess.run(["git", "-C", str(repo), "push", "-q", "-u", "origin", "main"], check=True)
            subprocess.run(["git", "-C", str(repo), "branch", "ai-main", "main"], check=True)
            subprocess.run(["git", "-C", str(repo), "push", "-q", "origin", "ai-main"], check=True)

            # A stale, reused issue branch with no unique content -- and a
            # harmless untracked scratch file, the way a UAT test-detection
            # scan leaves one behind.
            subprocess.run(["git", "-C", str(repo), "switch", "-q", "-c", "ai/claude/issue-999"], check=True)
            (repo / ".swarm").mkdir()
            (repo / ".swarm" / "tests.json").write_text("{}\n", encoding="utf-8")

            args = runner_module.build_parser().parse_args(
                ["--repo-dir", str(repo), "--state-dir", str(state)]
            )
            runner = runner_module.Runner(args, [])

            self.assertTrue(runner.synchronize_repository(runner.repos[0]))
            branch = subprocess.run(
                ["git", "-C", str(repo), "branch", "--show-current"], text=True,
                stdout=subprocess.PIPE, check=True,
            ).stdout.strip()
            self.assertEqual(branch, "ai-main")
            self.assertTrue((repo / ".swarm" / "tests.json").is_file())

    def test_scheduler_still_defers_untracked_files_on_a_checkout_with_unmerged_work(self) -> None:
        """The same untracked-only shape, but this time the current commit
        genuinely has content the remote integration branch does not --
        auto-recovery must never fire here, or it would be exactly the
        silent-discard bug issue-branch-delivery.md describes."""
        with tempfile.TemporaryDirectory(prefix="swarm-runner-no-recover-test.") as temporary:
            root = Path(temporary)
            repo = root / "repo"
            remote = root / "remote.git"
            state = root / "state"
            subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            for name, value in (("user.name", "runner test"), ("user.email", "runner@example.invalid")):
                subprocess.run(["git", "-C", str(repo), "config", name, value], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-q", "--allow-empty", "-m", "base"], check=True)
            subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", str(remote)], check=True)
            subprocess.run(["git", "-C", str(repo), "push", "-q", "-u", "origin", "main"], check=True)
            subprocess.run(["git", "-C", str(repo), "branch", "ai-main", "main"], check=True)
            subprocess.run(["git", "-C", str(repo), "push", "-q", "origin", "ai-main"], check=True)

            subprocess.run(["git", "-C", str(repo), "switch", "-q", "-c", "ai/claude/issue-999"], check=True)
            (repo / "unmerged.txt").write_text("unique work\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "unmerged.txt"], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "unmerged work"], check=True)
            (repo / "scratch.json").write_text("{}\n", encoding="utf-8")

            args = runner_module.build_parser().parse_args(
                ["--repo-dir", str(repo), "--state-dir", str(state)]
            )
            runner = runner_module.Runner(args, [])

            self.assertFalse(runner.synchronize_repository(runner.repos[0]))
            branch = subprocess.run(
                ["git", "-C", str(repo), "branch", "--show-current"], text=True,
                stdout=subprocess.PIPE, check=True,
            ).stdout.strip()
            self.assertEqual(branch, "ai/claude/issue-999")
            self.assertTrue((repo / "unmerged.txt").is_file())

    @staticmethod
    def _git(repo: Path, *arguments: str) -> str:
        return subprocess.run(
            ["git", "-C", str(repo), *arguments], text=True, stdout=subprocess.PIPE, check=True
        ).stdout.strip()

    def _recovery_runner(self, root: Path) -> tuple[runner_module.Runner, Path, Path, list[str]]:
        """A checkout on `main` with a pushed `ai-main`, plus a runner whose
        log lines are captured (and whose fetch retries don't sleep)."""
        repo = root / "repo"
        remote = root / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
        subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
        for name, value in (("user.name", "runner test"), ("user.email", "runner@example.invalid")):
            self._git(repo, "config", name, value)
        self._git(repo, "commit", "-q", "--allow-empty", "-m", "base")
        self._git(repo, "remote", "add", "origin", str(remote))
        self._git(repo, "push", "-q", "-u", "origin", "main")
        self._git(repo, "branch", "ai-main", "main")
        self._git(repo, "push", "-q", "origin", "ai-main")
        args = runner_module.build_parser().parse_args(
            ["--repo-dir", str(repo), "--state-dir", str(root / "state")]
        )
        runner = runner_module.Runner(args, [])
        runner.repos[0]["label"] = "acme/widgets"
        logged: list[str] = []
        patches = (
            mock.patch.object(runner, "log", side_effect=logged.append),
            mock.patch.object(runner_module.time, "sleep"),
        )
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        return runner, repo, remote, logged

    def test_scheduler_repositions_a_stale_branch_whose_commits_are_already_merged(self) -> None:
        """The leftover issue branch's commit was merged into ai-main and
        ai-main has since moved on, so the trees differ -- but every commit
        on the branch is reachable from the remote integration branch, so
        nothing can be lost by leaving it."""
        with tempfile.TemporaryDirectory(prefix="swarm-runner-ancestor-test.") as temporary:
            runner, repo, _remote, logged = self._recovery_runner(Path(temporary))
            self._git(repo, "switch", "-q", "-c", "ai/claude/issue-7")
            (repo / "feature.txt").write_text("done\n", encoding="utf-8")
            self._git(repo, "add", "feature.txt")
            self._git(repo, "commit", "-q", "-m", "feature")
            self._git(repo, "switch", "-q", "ai-main")
            self._git(repo, "merge", "-q", "--no-ff", "-m", "merge feature", "ai/claude/issue-7")
            (repo / "later.txt").write_text("later\n", encoding="utf-8")
            self._git(repo, "add", "later.txt")
            self._git(repo, "commit", "-q", "-m", "later work")
            self._git(repo, "push", "-q", "origin", "ai-main")
            self._git(repo, "switch", "-q", "ai/claude/issue-7")
            (repo / "scratch.json").write_text("{}\n", encoding="utf-8")

            self.assertTrue(runner.synchronize_repository(runner.repos[0]))
            self.assertEqual(self._git(repo, "branch", "--show-current"), "ai-main")
            self.assertTrue((repo / "scratch.json").is_file())
            self.assertTrue(any(
                line.startswith("acme/widgets: ") and "already contained in origin/ai-main" in line
                for line in logged
            ), logged)

    def test_scheduler_names_repo_branch_and_unmerged_commits_when_it_defers(self) -> None:
        with tempfile.TemporaryDirectory(prefix="swarm-runner-explain-test.") as temporary:
            runner, repo, _remote, logged = self._recovery_runner(Path(temporary))
            self._git(repo, "switch", "-q", "-c", "ai/claude/issue-999")
            (repo / "unmerged.txt").write_text("unique work\n", encoding="utf-8")
            self._git(repo, "add", "unmerged.txt")
            self._git(repo, "commit", "-q", "-m", "unmerged work")
            (repo / "scratch.json").write_text("{}\n", encoding="utf-8")

            self.assertFalse(runner.synchronize_repository(runner.repos[0]))
            self.assertEqual(len(logged), 1, logged)
            message = logged[0]
            self.assertTrue(message.startswith("acme/widgets: "), message)
            self.assertIn("'ai/claude/issue-999' has 1 commit(s) not in origin/ai-main", message)
            self.assertIn("unmerged work", message)
            self.assertIn("untracked files (scratch.json)", message)
            self.assertIn("manual review", message)
            # The UI's activity feed classifies deferrals by this phrase.
            self.assertRegex(message, r"; deferring synchronization")

    def test_scheduler_retries_a_flaky_fetch_before_deferring_the_recovery(self) -> None:
        with tempfile.TemporaryDirectory(prefix="swarm-runner-fetch-retry-test.") as temporary:
            runner, repo, remote, logged = self._recovery_runner(Path(temporary))
            self._git(repo, "switch", "-q", "-c", "ai/claude/issue-5")
            (repo / "scratch.json").write_text("{}\n", encoding="utf-8")
            real_git = runner.git
            failures = {"left": 2}

            def flaky(target, *arguments):
                if arguments[:1] == ("fetch",) and failures["left"]:
                    failures["left"] -= 1
                    return subprocess.CompletedProcess(arguments, 128, "", "fatal: unable to access remote\n")
                return real_git(target, *arguments)

            with mock.patch.object(runner, "git", side_effect=flaky):
                self.assertTrue(runner.synchronize_repository(runner.repos[0]))
            self.assertEqual(failures["left"], 0)
            self.assertEqual(self._git(repo, "branch", "--show-current"), "ai-main")

    def test_scheduler_reports_a_persistent_fetch_failure_as_transient(self) -> None:
        with tempfile.TemporaryDirectory(prefix="swarm-runner-fetch-down-test.") as temporary:
            runner, repo, remote, logged = self._recovery_runner(Path(temporary))
            self._git(repo, "switch", "-q", "-c", "ai/claude/issue-5")
            (repo / "scratch.json").write_text("{}\n", encoding="utf-8")
            self._git(repo, "remote", "set-url", "origin", str(remote.parent / "gone.git"))

            self.assertFalse(runner.synchronize_repository(runner.repos[0]))
            self.assertEqual(self._git(repo, "branch", "--show-current"), "ai/claude/issue-5")
            message = logged[-1]
            self.assertTrue(message.startswith("acme/widgets: could not fetch origin/ai-main ("), message)
            self.assertIn("will retry next cycle", message)
            # The UI's activity feed classifies this as "could not reach GitHub".
            self.assertRegex(message, r"(?i)could not fetch .*; deferring")

    def test_scheduler_skips_recovery_when_already_on_the_integration_branch(self) -> None:
        """Untracked files on `ai-main` itself need no repositioning, so the
        recovery must not fetch (a failure there used to defer the repo for
        manual review) or log a no-op 'repositioned' line every cycle."""
        with tempfile.TemporaryDirectory(prefix="swarm-runner-on-integration-test.") as temporary:
            runner, repo, _remote, logged = self._recovery_runner(Path(temporary))
            self._git(repo, "switch", "-q", "ai-main")
            (repo / ".swarm").mkdir()
            (repo / ".swarm" / "tests.json").write_text("{}\n", encoding="utf-8")

            self.assertTrue(runner.synchronize_repository(runner.repos[0]))
            self.assertEqual(logged, [])

    def test_scheduler_names_repo_and_files_when_tracked_changes_defer_it(self) -> None:
        with tempfile.TemporaryDirectory(prefix="swarm-runner-tracked-test.") as temporary:
            runner, repo, _remote, logged = self._recovery_runner(Path(temporary))
            (repo / "notes.txt").write_text("v1\n", encoding="utf-8")
            self._git(repo, "add", "notes.txt")
            self._git(repo, "commit", "-q", "-m", "notes")
            (repo / "notes.txt").write_text("v2\n", encoding="utf-8")

            self.assertFalse(runner.synchronize_repository(runner.repos[0]))
            message = logged[-1]
            self.assertTrue(message.startswith("acme/widgets: 'main' has uncommitted changes"), message)
            self.assertIn("notes.txt", message)
            self.assertRegex(message, r"; deferring synchronization")

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

    def test_scheduler_picks_up_repositories_added_after_it_started(self) -> None:
        with tempfile.TemporaryDirectory(prefix="swarm-runner-reload-test.") as temporary:
            root = Path(temporary)
            repos_file = self._repos_file(root, ("alpha", "beta"))
            args = runner_module.build_parser().parse_args(
                [
                    "--repos-file", str(repos_file), "--state-dir", str(root / "state"),
                    "--pgrep-bin", "", "--parallel-repos",
                ]
            )
            runner = runner_module.Runner(args, [])
            self.assertEqual([r["label"] for r in runner.repos], ["alpha", "beta"])

            # The desktop app rewrites the file when a repository is added.
            self._repos_file(root, ("alpha", "beta", "feedback"))
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                runner.reload_repos()

            self.assertEqual([r["label"] for r in runner.repos], ["alpha", "beta", "feedback"])
            self.assertTrue(runner.parallel_repos)
            self.assertIn("added feedback", output.getvalue())
            self.assertIn("now working 3 repository(ies)", output.getvalue())

            # Removing repositories down to one turns parallel mode off again.
            self._repos_file(root, ("alpha",))
            with contextlib.redirect_stdout(io.StringIO()):
                runner.reload_repos()
            self.assertEqual([r["label"] for r in runner.repos], ["alpha"])
            self.assertFalse(runner.parallel_repos)

    def test_scheduler_keeps_its_repositories_when_the_repos_file_is_unreadable(self) -> None:
        with tempfile.TemporaryDirectory(prefix="swarm-runner-reload-bad-test.") as temporary:
            root = Path(temporary)
            repos_file = self._repos_file(root, ("alpha", "beta"))
            args = runner_module.build_parser().parse_args(
                ["--repos-file", str(repos_file), "--state-dir", str(root / "state"), "--pgrep-bin", ""]
            )
            runner = runner_module.Runner(args, [])

            for bad in ("{not json", "[]", '[{"label": "x"}]'):
                repos_file.write_text(bad, encoding="utf-8")
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    runner.reload_repos()
                self.assertEqual([r["label"] for r in runner.repos], ["alpha", "beta"], bad)
                self.assertIn("keeping the current repository list", output.getvalue())

            repos_file.unlink()
            with contextlib.redirect_stdout(io.StringIO()):
                runner.reload_repos()
            self.assertEqual([r["label"] for r in runner.repos], ["alpha", "beta"])

    def test_scheduler_ignores_a_reloaded_list_whose_checkouts_do_not_exist(self) -> None:
        with tempfile.TemporaryDirectory(prefix="swarm-runner-reload-missing-test.") as temporary:
            root = Path(temporary)
            repos_file = self._repos_file(root, ("alpha", "beta"))
            args = runner_module.build_parser().parse_args(
                ["--repos-file", str(repos_file), "--state-dir", str(root / "state"), "--pgrep-bin", ""]
            )
            runner = runner_module.Runner(args, [])

            # e.g. a fixture whose temp checkout has since been deleted.
            entries = json.loads(repos_file.read_text(encoding="utf-8"))
            for entry in entries:
                entry["workspace_dir"] = str(root / "gone" / str(entry["label"]))
            repos_file.write_text(json.dumps(entries), encoding="utf-8")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                runner.reload_repos()

            self.assertEqual([r["label"] for r in runner.repos], ["alpha", "beta"])
            self.assertIn("has a checkout on disk", output.getvalue())

            # One real checkout among them is enough to adopt the new list.
            self._repos_file(root, ("alpha", "gamma"))
            with contextlib.redirect_stdout(io.StringIO()):
                runner.reload_repos()
            self.assertEqual([r["label"] for r in runner.repos], ["alpha", "gamma"])

    def test_scheduler_reloads_repositories_at_the_start_of_each_cycle(self) -> None:
        with tempfile.TemporaryDirectory(prefix="swarm-runner-cycle-reload-test.") as temporary:
            root = Path(temporary)
            repos_file = self._repos_file(root, ("alpha",))
            args = runner_module.build_parser().parse_args(
                ["--repos-file", str(repos_file), "--state-dir", str(root / "state"), "--once", "--pgrep-bin", ""]
            )
            runner = runner_module.Runner(args, [])
            self._repos_file(root, ("alpha", "feedback"))

            worked: list[str] = []
            with (
                mock.patch.object(runner, "synchronize_repository", return_value=True),
                mock.patch.object(
                    runner, "run_worker",
                    side_effect=lambda repo, *_: worked.append(str(repo["label"])) or 0,
                ),
                mock.patch.object(runner, "prune_cargo_target"),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                runner.run()

            self.assertEqual(worked, ["alpha", "feedback"])

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
