"""Issue #469: image import discovery works with Python 3.9 interfaces."""

from __future__ import annotations

import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
ISSUE_WORKER = ROOT / "issue_worker"
if str(ISSUE_WORKER) not in sys.path:
    sys.path.insert(0, str(ISSUE_WORKER))

import test_storage  # noqa: E402


class Python39StdlibDiscoveryTests(unittest.TestCase):
    def test_discovers_modules_packages_and_extensions_without_new_sys_api(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            stdlib = Path(temporary) / "stdlib"
            shared = Path(temporary) / "shared"
            dynload = stdlib / "lib-dynload"
            for directory in (stdlib, shared, dynload):
                directory.mkdir(parents=True)
            (stdlib / "json.py").touch()
            (stdlib / "asyncio").mkdir()
            (shared / "configparser.py").touch()
            (dynload / "_ssl.cpython-39-x86_64-linux-gnu.so").touch()
            (stdlib / "not_a_module.txt").touch()

            # Python 3.9 exposes builtin_module_names, but not
            # stdlib_module_names. This fake runtime deliberately has no such
            # attribute, so any accidental regression to it raises AttributeError.
            python39_sys = types.SimpleNamespace(builtin_module_names=("builtins",))
            with mock.patch.object(test_storage, "sys", python39_sys), \
                    mock.patch.object(test_storage.sysconfig, "get_path", side_effect=lambda key: {
                        "stdlib": str(stdlib), "platstdlib": str(shared)
                    }[key]), \
                    mock.patch.object(test_storage.sysconfig, "get_config_var", return_value=str(shared)):
                names = test_storage._stdlib_module_names()

            self.assertTrue({"builtins", "json", "asyncio", "configparser", "_ssl", "tomllib"} <= names)
            self.assertNotIn("not_a_module", names)


if __name__ == "__main__":
    unittest.main()
