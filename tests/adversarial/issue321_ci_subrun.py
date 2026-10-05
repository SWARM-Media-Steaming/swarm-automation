"""Helpers for CI-alignment subprocess unittest assertions (issues #321 and #384)."""

from __future__ import annotations

import re
import subprocess
import unittest


def assert_verbose_subrun_passed(
    test: unittest.TestCase,
    completed: subprocess.CompletedProcess[str],
    *,
    method: str,
    class_qualname: str,
) -> None:
    """Assert a ``python -m unittest -v`` child passed.

    Python 3.13 names the case as ``Class.method`` rather than ``Class``, and
    a ``ResourceWarning`` can land between the ellipsis and ``ok``. Match the
    result summary and a format-tolerant verbose header instead of one exact
    3.9 status line.
    """
    output = completed.stdout + completed.stderr
    test.assertEqual(completed.returncode, 0, output)
    test.assertIn(method, output)
    test.assertRegex(output, r"Ran 1 test")
    test.assertRegex(output, r"\nOK\n?\Z")
    test.assertNotIn("FAILED (failures=", output)
    tail = output.lower().split("ran 1 test", 1)[-1]
    test.assertNotIn("skipped", tail)
    header = (
        rf"{re.escape(method)} \({re.escape(class_qualname)}"
        rf"(?:\.{re.escape(method)})?\) \.\.\."
    )
    match = re.search(header, output)
    test.assertIsNotNone(
        match,
        f"verbose header for {method} ({class_qualname}) missing in:\n{output}",
    )
    assert match is not None
    rest = output[match.end() :]
    result = re.search(r"\b(ok|FAIL|ERROR|skipped)\b", rest)
    test.assertIsNotNone(result, rest)
    assert result is not None
    test.assertEqual(result.group(1), "ok", output)
