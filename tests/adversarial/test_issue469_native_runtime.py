"""Exercise the worker image import scan on the configured Python runtime."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ISSUE_WORKER = ROOT / "issue_worker"
if str(ISSUE_WORKER) not in sys.path:
    sys.path.insert(0, str(ISSUE_WORKER))

import test_storage  # noqa: E402


class NativePythonRuntimeTests(unittest.TestCase):
    def test_worker_image_import_scan_runs_on_configured_python(self) -> None:
        # This is deliberately unmocked: the test command's interpreter is the
        # configured runtime, so the scan must not depend on post-3.9 APIs.
        self.assertIn("json", test_storage._stdlib_module_names())
        test_storage.WorkerImageTests().test_worker_modules_import_only_stdlib_and_siblings()


if __name__ == "__main__":
    unittest.main()
