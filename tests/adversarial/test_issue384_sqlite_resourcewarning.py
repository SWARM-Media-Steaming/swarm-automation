"""Issue #384: CI-alignment suites must survive Python 3.13 ResourceWarning.

Oracle (from the issue, before the patch):

* `ExecutionHistoryRepository.connect()` returned a raw Connection. Callers
  used `with`, which commits but does not close. Python 3.13 then prints
  `ResourceWarning: unclosed database` onto unittest `-v` stderr after the
  test name, so the exact 3.9 string
  `'<method> (test_adversarial_uat.AdversarialUatTests) ... ok'` is missing
  even when the inner test passed.
* Closing the handle is the product fix. CI-alignment assertions must also
  accept 3.13's `Class.method` verbose form and interleaved warnings.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SUITE_DIR = Path(__file__).resolve().parent
ISSUE_WORKER_DIR = REPO_ROOT / "issue_worker"
for _path in (SUITE_DIR, ISSUE_WORKER_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from issue321_ci_subrun import assert_verbose_subrun_passed  # noqa: E402

from ai_execution_history import ClosingConnection, ExecutionHistoryRepository  # noqa: E402

CAP_HOLDS_CLASS = "AdversarialUatTests"
CAP_HOLDS_METHOD = "test_cap_holds_automation_and_asks_a_trusted_author_to_adjudicate"
CI_ALIGNMENT_FILES = (
    REPO_ROOT / "tests" / "adversarial" / "test_issue321_ci_cap_hit_tester_count.py",
    REPO_ROOT / "tests" / "adversarial" / "test_issue321_ci_python_discover_alignment.py",
)


class Issue384SqliteResourceWarningTests(unittest.TestCase):
    def test_leaked_sqlite_with_block_breaks_exact_verbose_ok_line(self) -> None:
        """Reproduce the 3.13 interleave without depending on the product store."""
        script = textwrap.dedent(
            """
            import gc
            import sqlite3
            import tempfile
            import unittest
            from pathlib import Path

            class AdversarialUatTests(unittest.TestCase):
                def test_cap_holds_automation_and_asks_a_trusted_author_to_adjudicate(self):
                    path = Path(tempfile.mkdtemp()) / "probe.sqlite3"
                    connection = sqlite3.connect(path)
                    with connection:
                        connection.execute("CREATE TABLE t(x INTEGER)")
                    del connection
                    gc.collect()
            """
        )
        with tempfile.TemporaryDirectory(prefix="issue384-leak.") as temp:
            (Path(temp) / "test_adversarial_uat.py").write_text(script, encoding="utf-8")
            completed = subprocess.run(
                [
                    sys.executable,
                    "-W",
                    "default::ResourceWarning",
                    "-m",
                    "unittest",
                    "-v",
                    f"test_adversarial_uat.AdversarialUatTests.{CAP_HOLDS_METHOD}",
                ],
                cwd=temp,
                capture_output=True,
                text=True,
                check=False,
            )
        output = completed.stdout + completed.stderr
        self.assertEqual(completed.returncode, 0, output)
        exact = f"{CAP_HOLDS_METHOD} (test_adversarial_uat.{CAP_HOLDS_CLASS}) ... ok"
        if sys.version_info >= (3, 13):
            self.assertIn("unclosed database", output)
            self.assertNotIn(exact, output)
        assert_verbose_subrun_passed(
            self,
            completed,
            method=CAP_HOLDS_METHOD,
            class_qualname=f"test_adversarial_uat.{CAP_HOLDS_CLASS}",
        )

    def test_history_connect_is_a_closing_connection(self) -> None:
        with tempfile.TemporaryDirectory(prefix="issue384-history.") as temp:
            repository = ExecutionHistoryRepository(Path(temp) / "history.sqlite3")
            with repository.connect() as database:
                self.assertIsInstance(database, ClosingConnection)
                held = database
            with self.assertRaises(sqlite3.ProgrammingError):
                held.execute("SELECT 1")

    def test_ci_alignment_suites_do_not_require_the_brittle_39_status_line(self) -> None:
        brittle = (
            'f"{CAP_HOLDS_METHOD} (test_adversarial_uat.{CAP_HOLDS_CLASS}) ... ok"'
        )
        for path in CI_ALIGNMENT_FILES:
            source = path.read_text(encoding="utf-8")
            self.assertNotIn(
                brittle,
                source,
                f"{path.name} still requires the Python 3.9 verbose status line",
            )
            self.assertIn("assert_verbose_subrun_passed", source)

    def test_cap_holds_subrun_passes_without_unclosed_database_warning(self) -> None:
        env = os.environ.copy()
        env["PYTHONWARNINGS"] = "default::ResourceWarning"
        completed = subprocess.run(
            [
                sys.executable,
                "-W",
                "default::ResourceWarning",
                "-m",
                "unittest",
                "-v",
                f"test_adversarial_uat.{CAP_HOLDS_CLASS}.{CAP_HOLDS_METHOD}",
            ],
            cwd=ISSUE_WORKER_DIR,
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )
        output = completed.stdout + completed.stderr
        self.assertNotIn("unclosed database", output, output)
        assert_verbose_subrun_passed(
            self,
            completed,
            method=CAP_HOLDS_METHOD,
            class_qualname=f"test_adversarial_uat.{CAP_HOLDS_CLASS}",
        )


if __name__ == "__main__":
    unittest.main()
