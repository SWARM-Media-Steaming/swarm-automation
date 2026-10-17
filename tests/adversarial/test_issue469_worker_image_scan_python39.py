"""Run the actual image import scan with a Python 3.9-shaped sys module."""

from __future__ import annotations

import sys
import types
import unittest
from unittest import mock

from test_issue469_python39_stdlib_discovery import test_storage


class WorkerImagePython39Tests(unittest.TestCase):
    def test_worker_import_scan_does_not_need_stdlib_module_names(self) -> None:
        # The Python 3.9 contract includes builtins but has no
        # sys.stdlib_module_names. Execute the production image-import test
        # itself against that interface, so its import allowlist is exercised
        # in addition to the isolated directory-enumeration fixture.
        python39_sys = types.SimpleNamespace(
            builtin_module_names=sys.builtin_module_names,
        )
        with mock.patch.object(test_storage, "sys", python39_sys):
            test_storage.WorkerImageTests().test_worker_modules_import_only_stdlib_and_siblings()


if __name__ == "__main__":
    unittest.main()
