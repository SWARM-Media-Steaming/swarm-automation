"""Adversarial architecture checks for Issue #215.

The removed scheduler was a cross-cutting subsystem.  These tests guard the
backend boundary that ordinary compile tests can miss: no dormant command or
configuration API may remain in the desktop app, while the independent
Adversarial UAT path must remain wired into issue execution.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"


def rust_sources() -> str:
    return "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(SRC.glob("*.rs"))
    )


class TestSchedulerBackendRemovalTests(unittest.TestCase):
    def test_scheduler_modules_commands_and_config_contract_are_gone(self) -> None:
        self.assertFalse((SRC / "testing.rs").exists())
        self.assertFalse((SRC / "test_discovery.rs").exists())

        source = rust_sources()
        removed_identifiers = (
            "start_uat_scheduler",
            "get_test_plan",
            "get_test_plan_background",
            "detect_test_definition",
            "create_test_definition",
            "audit_test_coverage",
            "get_test_runs",
            "get_test_runs_background",
            "save_test_input",
            "choose_test_input_path",
            "uat_hour",
            "allow_disruptive_tests",
            "uat_ai_test_data_enabled",
            "uat_triage_enabled",
            "test_inputs",
            "--swarm-test-runner",
        )
        for identifier in removed_identifiers:
            with self.subTest(identifier=identifier):
                self.assertNotIn(
                    identifier,
                    source,
                    f"deleted scheduler API/config identifier still exists: {identifier}",
                )

        self.assertNotRegex(source, r"(?m)^\s*mod\s+(?:testing|test_discovery)\s*;")

    def test_legacy_test_manifests_and_checkout_locks_are_inert_to_the_app(self) -> None:
        source = rust_sources()
        self.assertNotIn(".swarm/tests.json", source)
        self.assertNotIn("swarm-test-run.lock", source)

        installer = (ROOT / "issue_worker/install_swarm_issue_cron.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("checkout_test_lock_active", installer)
        self.assertNotIn("swarm-test-run.lock", installer)

        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertRegex(
            readme,
            r"(?is)\.swarm/tests\.json.*(?:otherwise inert|left over.*inert)",
            "the no-migration data behavior must be documented for existing repositories",
        )

    def test_scheduler_is_removed_from_the_package_contract(self) -> None:
        cargo = (ROOT / "Cargo.toml").read_text(encoding="utf-8")
        description_match = re.search(
            r'(?m)^description\s*=\s*"([^"]*)"', cargo
        )
        self.assertIsNotNone(description_match, "Cargo package needs a description")
        description = description_match.group(1).lower()
        self.assertNotIn("test scheduler", description)
        self.assertNotIn("test scheduling", description)

    def test_scheduler_only_keyring_dependency_is_removed(self) -> None:
        cargo = (ROOT / "Cargo.toml").read_text(encoding="utf-8")
        dependency_block = cargo.split("[dependencies]", 1)[1].split(
            "[build-dependencies]", 1
        )[0]
        self.assertNotRegex(
            dependency_block,
            r"(?m)^keyring\s*=",
            "keyring was used only for scheduler test inputs and should leave with that subsystem",
        )

    def test_adversarial_uat_remains_wired_through_repo_configuration(self) -> None:
        config = (SRC / "config.rs").read_text(encoding="utf-8")
        main = (SRC / "main.rs").read_text(encoding="utf-8")
        worker = (ROOT / "issue_worker/swarm_issue_worker.py").read_text(
            encoding="utf-8"
        )
        adversarial = ROOT / "issue_worker/adversarial_uat.py"

        self.assertIn("pub adversarial_uat_enabled: bool", config)
        self.assertIn('"--adversarial-uat-enabled"', main)
        self.assertIn('"--no-adversarial-uat-enabled"', main)
        self.assertIn("adversarial_uat_enabled", worker)
        self.assertTrue(adversarial.is_file())
        self.assertIn("MAX_ROUNDS = 6", adversarial.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
