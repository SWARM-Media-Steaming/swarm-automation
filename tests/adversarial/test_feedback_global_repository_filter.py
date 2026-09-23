"""Issue #211: Feedback must default to a true global aggregate across every
configured repository, with an optional multi-repository filter — computed as
one combined SQL query server-side, never merged client-side from several
per-repo calls.

These are adversarial, spec-first checks of `ai_execution_history.py`'s
repository-filtering primitives (`_repository_filter`, `page_for_repository`,
`graded_for_repository`, `adversarial_summary`, and the `main()` CLI). They
intentionally probe boundary conditions the issue calls out explicitly:

* Omitted/empty filter -> no `WHERE repository` predicate at all (global).
* A non-empty filter -> `WHERE repository IN (...)`, deduplicated.
* Every returned row still carries `repository` so the UI can badge it.
* Aggregate numbers (adversarial summary, grade distribution, router matrix)
  are true unions across the filtered repositories, not an average of
  per-repo averages (the failure mode a client-side merge would produce).
* A single-repo filter reproduces the exact same numbers the old
  single-repository-only API produced for that repository.
* Repository names are bound as query parameters, so a value shaped like a
  SQL injection attempt matches nothing rather than corrupting the query.
* The CLI's `--repository` flag is repeatable and optional; imports still
  require exactly one repository per invocation (the Rust side loops).
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
ISSUE_WORKER_DIR = REPO_ROOT / "issue_worker"
if str(ISSUE_WORKER_DIR) not in sys.path:
    sys.path.insert(0, str(ISSUE_WORKER_DIR))

import test_swarm_issue_worker as fixtures  # noqa: E402
from ai_execution_history import (  # noqa: E402
    ExecutionHistoryRepository,
    ExecutionHistoryService,
    ExecutionStart,
    main as execution_history_main,
)


class FeedbackGlobalRepositoryFilterTests(unittest.TestCase):
    setUp = fixtures.WorkerTestCase.setUp
    tearDown = fixtures.WorkerTestCase.tearDown
    git = fixtures.WorkerTestCase.git
    _worker_argv = fixtures.WorkerTestCase._worker_argv

    def _seed(self, database_path: Path) -> None:
        """Three repositories, three distinct adversarial outcomes.

        Chosen so a "merge per-repo averages" bug and a true "union of rows"
        implementation disagree on the aggregate numbers: rounds are 0, 3, 6
        (mean of means == mean of the pooled rows only because there are
        exactly three repos with one row each here, so the real regression
        test is in `test_adversarial_summary_is_a_true_union_not_an_average_of_averages`
        below, which unbalances the row counts per repository instead).
        """
        rows = [
            ("octocat/alpha", 101, "A", "claude", "claude-model", "clean_first_pass", 0, 5.0),
            ("octocat/beta", 202, "C-", "grok", "grok-model", "resolved_after_n", 3, 40.0),
            ("octocat/gamma", 303, "F", "codex", "codex-model", "cap_hit", 6, 90.0),
        ]
        for index, (repo, number, grade, router, router_model, outcome, rounds, capacity) in enumerate(rows):
            service = ExecutionHistoryService(True, database_path)
            service.start(
                ExecutionStart(
                    repository=repo,
                    issue_number=number,
                    issue_url=f"https://github.com/{repo}/issues/{number}",
                    issue_title=f"Issue {number}",
                    issue_body="",
                    provider="Codex",
                    model="m",
                    effort="high",
                    branch_name="b",
                    application_version="1",
                    routing_decision={
                        "prompt_grade": grade,
                        "grade_reason": "Reason.",
                        "fallback": False,
                        "provider": "codex",
                        "router_provider": router,
                        "router_model": router_model,
                    },
                ),
                f"2026-09-2{index + 1}T10:00:00-05:00",
            )
            service.update(
                f"2026-09-2{index + 1}T10:05:00-05:00",
                adversarial_round_count=rounds,
                adversarial_outcome=outcome,
                capacity_consumed_percent=capacity,
            )

    # -- _repository_filter / page_for_repository -------------------------

    def test_omitted_filter_means_global_not_a_missing_required_argument(self) -> None:
        """The old API required `repository` as a mandatory single string.
        The new one must treat "no argument at all" as "every repository",
        not raise, not silently return zero rows.
        """
        database_path = self.state / "global.sqlite3"
        self._seed(database_path)
        repository = ExecutionHistoryRepository(database_path)

        page, total, offset, limit = repository.page_for_repository()
        self.assertEqual(total, 3)
        self.assertEqual(len(page), 3)
        self.assertEqual(offset, 0)

        empty_list_page, empty_total, _, _ = repository.page_for_repository([])
        self.assertEqual(empty_total, 3)
        self.assertEqual(
            [row["repository"] for row in empty_list_page],
            [row["repository"] for row in page],
        )

    def test_every_row_still_carries_its_repository_for_ui_badging(self) -> None:
        database_path = self.state / "badges.sqlite3"
        self._seed(database_path)
        repository = ExecutionHistoryRepository(database_path)
        page, _, _, _ = repository.page_for_repository([])
        for row in page:
            self.assertIn(row["repository"], {"octocat/alpha", "octocat/beta", "octocat/gamma"})
            self.assertTrue(row["repository"])

    def test_single_repository_filter_reproduces_old_single_repo_numbers(self) -> None:
        """Selecting exactly one repo in the new filter must be indistinguishable
        from the old, single-repository-only behavior for that repository —
        the acceptance criterion the issue states explicitly.
        """
        database_path = self.state / "single-repo-parity.sqlite3"
        self._seed(database_path)
        repository = ExecutionHistoryRepository(database_path)

        multi_api_page, multi_api_total, _, _ = repository.page_for_repository(["octocat/beta"])
        legacy_rows = repository.for_repository("octocat/beta")

        self.assertEqual(multi_api_total, len(legacy_rows))
        self.assertEqual(len(multi_api_page), 1)
        self.assertEqual(multi_api_page[0]["issue_number"], legacy_rows[0]["issue_number"])
        self.assertEqual(multi_api_page[0]["repository"], "octocat/beta")

    def test_deselecting_a_repository_removes_its_rows_and_reselecting_restores_them(self) -> None:
        database_path = self.state / "toggle.sqlite3"
        self._seed(database_path)
        repository = ExecutionHistoryRepository(database_path)

        with_all_three = repository.page_for_repository(["octocat/alpha", "octocat/beta", "octocat/gamma"])[1]
        without_gamma = repository.page_for_repository(["octocat/alpha", "octocat/beta"])[1]
        self.assertEqual(with_all_three, 3)
        self.assertEqual(without_gamma, 2)

        restored, restored_total, _, _ = repository.page_for_repository(
            ["octocat/alpha", "octocat/beta", "octocat/gamma"]
        )
        self.assertEqual(restored_total, 3)
        self.assertEqual({row["repository"] for row in restored}, {"octocat/alpha", "octocat/beta", "octocat/gamma"})

    def test_repository_filter_deduplicates_repeated_names(self) -> None:
        database_path = self.state / "dedupe.sqlite3"
        self._seed(database_path)
        repository = ExecutionHistoryRepository(database_path)
        _, total, _, _ = repository.page_for_repository(
            ["octocat/alpha", "octocat/alpha", "octocat/alpha"]
        )
        self.assertEqual(total, 1)

    def test_repository_name_shaped_like_sql_injection_matches_nothing_not_everything(self) -> None:
        """Repository names must be bound as parameters. A value built to look
        like it escapes the query (`' OR '1'='1`) must behave like any other
        non-matching repository name: zero rows, not every row and not a
        crash.
        """
        database_path = self.state / "injection.sqlite3"
        self._seed(database_path)
        repository = ExecutionHistoryRepository(database_path)
        page, total, _, _ = repository.page_for_repository(["octocat/alpha' OR '1'='1"])
        self.assertEqual(total, 0)
        self.assertEqual(page, [])

    def test_blank_and_whitespace_only_entries_are_ignored_not_treated_as_a_real_repository(self) -> None:
        database_path = self.state / "blank-entries.sqlite3"
        self._seed(database_path)
        repository = ExecutionHistoryRepository(database_path)
        # A list of only blanks/whitespace must degrade to "no filter" (global),
        # matching the documented "empty/omitted list" behavior rather than
        # matching a literal empty-string repository column.
        page, total, _, _ = repository.page_for_repository(["", "   "])
        self.assertEqual(total, 3)
        self.assertEqual(len(page), 3)

    # -- adversarial_summary: true union, not merged per-repo averages ----

    def test_adversarial_summary_is_a_true_union_not_an_average_of_averages(self) -> None:
        """Unbalance the row counts per repository (2 rows in one repo, 1 in
        another) so a "merge the per-repo summaries" implementation and a
        real pooled-SQL implementation disagree, catching a client-side
        merge regression that a balanced 1-row-per-repo fixture would hide.
        """
        database_path = self.state / "union-not-average.sqlite3"
        # repo A: two clean passes (0, 0 rounds). repo B: one cap_hit (6 rounds).
        # True pooled average = (0 + 0 + 6) / 3 = 2.0.
        # A "compute per-repo average, then average the averages" bug would
        # instead yield (0 + 6) / 2 = 3.0.
        rows = [
            ("octocat/alpha", 1, "clean_first_pass", 0),
            ("octocat/alpha", 2, "clean_first_pass", 0),
            ("octocat/beta", 3, "cap_hit", 6),
        ]
        for index, (repo, number, outcome, rounds) in enumerate(rows):
            service = ExecutionHistoryService(True, database_path)
            service.start(
                ExecutionStart(
                    repository=repo,
                    issue_number=number,
                    issue_url=f"https://github.com/{repo}/issues/{number}",
                    issue_title=f"Issue {number}",
                    issue_body="",
                    provider="Codex",
                    model="m",
                    effort="high",
                    branch_name="b",
                    application_version="1",
                ),
                f"2026-09-2{index + 1}T10:00:00-05:00",
            )
            service.update(
                f"2026-09-2{index + 1}T10:05:00-05:00",
                adversarial_round_count=rounds,
                adversarial_outcome=outcome,
            )

        repository = ExecutionHistoryRepository(database_path)
        summary = repository.adversarial_summary(["octocat/alpha", "octocat/beta"])
        self.assertEqual(summary["loops"], 3)
        self.assertAlmostEqual(summary["averageRounds"], 2.0)
        # 2 of 3 loops were clean-first-pass.
        self.assertAlmostEqual(summary["cleanFirstPassPercent"], round(2 / 3 * 100, 1))
        self.assertAlmostEqual(summary["capHitPercent"], round(1 / 3 * 100, 1))

    def test_adversarial_summary_excludes_repositories_outside_the_filter(self) -> None:
        database_path = self.state / "summary-scope.sqlite3"
        self._seed(database_path)
        repository = ExecutionHistoryRepository(database_path)
        summary = repository.adversarial_summary(["octocat/alpha"])
        self.assertEqual(summary["loops"], 1)
        self.assertAlmostEqual(summary["averageRounds"], 0.0)
        self.assertAlmostEqual(summary["cleanFirstPassPercent"], 100.0)

    # -- graded_for_repository: repository field, union summary, matrix ---

    def test_graded_records_carry_repository_and_pool_grade_summary_globally(self) -> None:
        database_path = self.state / "grades-global.sqlite3"
        self._seed(database_path)
        repository = ExecutionHistoryRepository(database_path)

        grades = repository.graded_for_repository([])
        self.assertEqual(grades["total"], 3)
        self.assertEqual({row["repository"] for row in grades["records"]}, {"octocat/alpha", "octocat/beta", "octocat/gamma"})
        self.assertEqual(grades["summary"]["graded"], 3)
        self.assertEqual(grades["summary"]["distribution"]["A"], 1)
        self.assertEqual(grades["summary"]["distribution"]["F"], 1)

    def test_grade_filter_never_leaks_rows_from_repositories_outside_the_selection(self) -> None:
        """Filtering the grade chart to a letter that only exists in an
        unselected repository must return zero rows — the repository
        boundary must be applied together with, not instead of, the grade
        filter.
        """
        database_path = self.state / "grade-repo-boundary.sqlite3"
        self._seed(database_path)
        repository = ExecutionHistoryRepository(database_path)
        # "F" only exists for octocat/gamma, which is excluded here.
        grades = repository.graded_for_repository(["octocat/alpha", "octocat/beta"], grade="F")
        self.assertEqual(grades["total"], 0)
        self.assertEqual(grades["records"], [])

    def test_router_matrix_is_pooled_across_selected_repositories(self) -> None:
        database_path = self.state / "router-matrix-pooled.sqlite3"
        self._seed(database_path)
        repository = ExecutionHistoryRepository(database_path)
        grades = repository.graded_for_repository(["octocat/alpha", "octocat/beta", "octocat/gamma"])
        routers = {row["router"] for row in grades["routerMatrix"]}
        self.assertEqual(routers, {"claude", "grok", "codex"})

        scoped = repository.graded_for_repository(["octocat/alpha"])
        self.assertEqual({row["router"] for row in scoped["routerMatrix"]}, {"claude"})

    # -- CLI: repeatable --repository, optional, single-repo import -------

    def test_cli_accepts_repeated_repository_flags_and_omission_means_global(self) -> None:
        database_path = self.state / "cli-global.sqlite3"
        self._seed(database_path)

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            exit_code = execution_history_main(["--db", str(database_path), "--limit", "10"])
        self.assertEqual(exit_code, 0)
        payload = json.loads(buffer.getvalue())
        self.assertEqual(payload["total"], 3)

        filtered_buffer = io.StringIO()
        with contextlib.redirect_stdout(filtered_buffer):
            exit_code = execution_history_main(
                [
                    "--db", str(database_path),
                    "--repository", "octocat/alpha",
                    "--repository", "octocat/beta",
                    "--limit", "10",
                ]
            )
        self.assertEqual(exit_code, 0)
        filtered_payload = json.loads(filtered_buffer.getvalue())
        self.assertEqual(filtered_payload["total"], 2)
        self.assertEqual(filtered_payload["adversarial"]["loops"], 2)

    def test_cli_import_from_github_rejects_zero_or_multiple_repositories(self) -> None:
        """The Rust side is documented to loop the GitHub import once per
        checked repository. The CLI itself must refuse to silently import
        for "the first repository" or "all repositories" if it is ever asked
        to import across zero or several at once — that ambiguity belongs to
        the caller, not this process.
        """
        database_path = self.state / "cli-import-guard.sqlite3"

        no_repo_buffer = io.StringIO()
        with contextlib.redirect_stdout(no_repo_buffer):
            exit_code = execution_history_main(
                [
                    "--db", str(database_path),
                    "--import-from-github",
                    "--gh-bin", "gh",
                ]
            )
        self.assertEqual(exit_code, 1)
        self.assertIn("error", json.loads(no_repo_buffer.getvalue()))

        many_repo_buffer = io.StringIO()
        with contextlib.redirect_stdout(many_repo_buffer):
            exit_code = execution_history_main(
                [
                    "--db", str(database_path),
                    "--repository", "octocat/alpha",
                    "--repository", "octocat/beta",
                    "--import-from-github",
                    "--gh-bin", "gh",
                ]
            )
        self.assertEqual(exit_code, 1)
        self.assertIn("error", json.loads(many_repo_buffer.getvalue()))


if __name__ == "__main__":
    unittest.main()
