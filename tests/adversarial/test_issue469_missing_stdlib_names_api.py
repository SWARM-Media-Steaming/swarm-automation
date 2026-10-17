"""The worker image import scan must run without Python 3.10's sys API."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ISSUE_WORKER = ROOT / "issue_worker"
if str(ISSUE_WORKER) not in sys.path:
    sys.path.insert(0, str(ISSUE_WORKER))

import test_storage  # noqa: E402


class MissingStdlibNamesApiTests(unittest.TestCase):
    def test_worker_image_scan_runs_when_stdlib_module_names_is_unavailable(self) -> None:
        """Model Python 3.9 even when this suite runs on a newer interpreter."""
        had_attribute = hasattr(sys, "stdlib_module_names")
        previous_value = getattr(sys, "stdlib_module_names", None)
        if had_attribute:
            del sys.stdlib_module_names
        try:
            names = test_storage._stdlib_module_names()
            self.assertIn("json", names)
            test_storage.WorkerImageTests().test_worker_modules_import_only_stdlib_and_siblings()
        finally:
            if had_attribute:
                sys.stdlib_module_names = previous_value


if __name__ == "__main__":
    unittest.main()
