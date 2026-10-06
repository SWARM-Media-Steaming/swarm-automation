"""``web_bridge``: the hosted API's window onto history, usage and the calculator.

Runs the real operations over ``LocalStorage`` (the hosted backends pass the same
storage contract) and the real ``python -I web_bridge.py`` process protocol.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import web_bridge  # noqa: E402
from storage import LocalStorage  # noqa: E402
from storage_contract import execution_start  # noqa: E402

TENANT_A = "t00000000000000a1"
TENANT_B = "t00000000000000b2"
NOW = "2026-01-01T00:00:00Z"


def record(storage: LocalStorage, tenant: str, issue: int, repository: str) -> str:
    store = storage.execution_history(tenant)
    return store.create(execution_start(issue=issue, repository=repository), NOW)


class WebBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.storage = LocalStorage(Path(self._dir.name))

    def call(self, op: str, tenant: str = TENANT_A, args=None, config=None) -> dict:
        return web_bridge.handle(
            {"op": op, "tenant": tenant, "args": args or {}, "config": config or {}}, self.storage
        )

    # -- history ------------------------------------------------------------------------
    def test_history_is_the_tenants_own_and_paged_like_the_desktop(self) -> None:
        record(self.storage, TENANT_A, 1, "acme/demo")
        record(self.storage, TENANT_A, 2, "acme/demo")
        record(self.storage, TENANT_B, 3, "acme/demo")
        result = self.call("execution_history", args={"repositories": ["acme/demo"]})
        self.assertTrue(result["ok"], result)
        page = result["result"]
        self.assertEqual(page["total"], 2)
        self.assertEqual({row["issue_number"] for row in page["records"]}, {1, 2})
        self.assertIn("adversarial", page)
        self.assertIn("security", page)
        other = self.call("execution_history", tenant=TENANT_B, args={"repositories": ["acme/demo"]})
        self.assertEqual({row["issue_number"] for row in other["result"]["records"]}, {3})

    def test_a_tenant_without_history_reads_empty_and_is_not_provisioned(self) -> None:
        for op in ("execution_history", "jev_feedback", "usage_report", "prompt_grades"):
            result = self.call(op, tenant=TENANT_B)
            self.assertTrue(result["ok"], (op, result))
        self.assertFalse(self.storage.has_execution_history(TENANT_B))
        self.assertEqual(self.call("execution_history", tenant=TENANT_B)["result"]["total"], 0)

    def test_filters_are_validated(self) -> None:
        record(self.storage, TENANT_A, 1, "acme/demo")
        for args in ({"sort": "random"}, {"delivery": "nope"}, {"offset": "-1"}, {"offset": "abc"}):
            result = self.call("execution_history", args=args)
            self.assertEqual(result["code"], "bad_request", args)
        bad_usage = self.call("usage_report", args={"query": {"groupBy": "tenant-secrets"}})
        self.assertEqual(bad_usage["code"], "bad_request")
        self.assertEqual(self.call("usage_report", args={"query": "nope"})["code"], "bad_request")

    def test_usage_jev_and_grades_answer_for_a_tenant_with_history(self) -> None:
        record(self.storage, TENANT_A, 1, "acme/demo")
        args = {"repositories": ["acme/demo"], "query": {"groupBy": "issue"}}
        usage = self.call("usage_report", args=args)
        self.assertTrue(usage["ok"], usage)
        self.assertIn("groups", usage["result"])
        self.assertTrue(self.call("jev_feedback", args={"repositories": ["acme/demo"]})["ok"])
        grades = self.call("prompt_grades", args=args)
        self.assertTrue(grades["ok"], grades)
        self.assertIn("summary", grades["result"])

    # -- documents and computation ---------------------------------------------------------
    def test_architecture_docs_read_the_tenants_snapshot_only_for_configured_repositories(self) -> None:
        config = {"repositories": [{"id": "acme__demo", "github_repository": "acme/demo", "architecture_docs_enabled": True}]}
        result = self.call("architecture_docs", args={"repository": "acme/demo"}, config=config)
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.call("architecture_docs", args={"repository": "acme/other"}, config=config)["code"], "bad_request")
        self.assertEqual(self.call("architecture_docs", args={}, config=config)["code"], "bad_request")

    def test_routing_calculator_describes_and_simulates(self) -> None:
        described = self.call("routing_describe", config={"providers": [{"id": "claude", "enabled": True}]})
        self.assertTrue(described["ok"], described)
        self.assertEqual(self.call("routing_simulate", args={})["code"], "bad_request")

    # -- protocol ---------------------------------------------------------------------------
    def test_unavailable_operations_say_why_and_unknown_ones_are_refused(self) -> None:
        for op in web_bridge.UNAVAILABLE:
            result = self.call(op)
            self.assertEqual(result["code"], "unavailable", op)
            self.assertTrue(result["error"])
        self.assertEqual(self.call("drop_tables")["code"], "bad_request")
        self.assertEqual(self.call("execution_history", tenant="../etc")["code"], "bad_request")
        self.assertEqual(web_bridge.handle(["not", "an", "object"], self.storage)["code"], "bad_request")

    def test_storage_failures_never_escape_as_a_traceback(self) -> None:
        def broken():
            raise RuntimeError("postgres://user:hunter2hunter2@db/x is down")

        result = web_bridge.handle({"op": "execution_history", "tenant": TENANT_A}, broken)
        self.assertEqual(result["code"], "failed")
        self.assertNotIn("hunter2", json.dumps(result))

    def test_process_protocol_over_stdin_with_isolated_python(self) -> None:
        env = {"PATH": os.environ.get("PATH", ""), "SWARM_STORAGE_S3_BUCKET": ""}
        request = {"op": "routing_describe", "tenant": TENANT_A, "args": {}, "config": {}}
        done = subprocess.run(
            [sys.executable, "-I", str(HERE / "web_bridge.py")],
            input=json.dumps(request), capture_output=True, text=True, env=env, timeout=60,
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertTrue(json.loads(done.stdout)["ok"])
        # An unavailable operation never needs storage; a storage-backed one with
        # no hosted settings fails with a message naming variables, not values.
        history_request = {"op": "execution_history", "tenant": TENANT_A, "args": {}, "config": {}}
        done = subprocess.run(
            [sys.executable, "-I", str(HERE / "web_bridge.py")],
            input=json.dumps(history_request), capture_output=True, text=True, env=env, timeout=60,
        )
        answer = json.loads(done.stdout)
        self.assertFalse(answer["ok"])
        self.assertEqual(answer["code"], "failed")


if __name__ == "__main__":
    unittest.main()
