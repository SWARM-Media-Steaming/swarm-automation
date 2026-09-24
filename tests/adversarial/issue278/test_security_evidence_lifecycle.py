"""Issue #278 AC 6/7/11-13: findings describe vulnerabilities, not filenames.

Independent oracle: another owner may neither read nor delete a record, and
only an administrator may delete their own record. Two broken authorization
checks in one module are two findings. A finding still exploitable at delivery
cannot be counted as fixed, regardless of earlier rounds or changing evidence.

All code under attack is generated in a disposable checkout. The production
worker runs real child-process suites and writes real SQLite history; coding
providers, GitHub, and final delivery are intercepted by the existing harness.
"""
import json
import runpy
import unittest

from harness import LocalReviewFixture, finding, report


READ_ID = "adversarial-security-fixture-read"
DELETE_ID = "adversarial-security-fixture-delete"


class EvidenceFixture(LocalReviewFixture):
    def write_service(self, *, read_broken, delete_broken):
        read = "True" if read_broken else "user == owner"
        delete = "True" if delete_broken else "user == owner and is_admin"
        (self.repo / "access.py").write_text(
            f"def can_read(user, owner):\n    return {read}\n\n"
            f"def can_delete(user, owner, is_admin):\n    return {delete}\n")

    def prepare_service(self, *, separate_files=False):
        self.prepare(vulnerable=True)
        self.write_service(read_broken=True, delete_broken=True)
        if separate_files:
            # The same calls are reachable through separate API adapters. This
            # avoids coupling the regression test to same-file identity bugs.
            (self.repo / "read_api.py").write_text("from access import can_read\n")
            (self.repo / "delete_api.py").write_text("from access import can_delete\n")
        self.git("add", ".")
        self.git("commit", "-qm", "[claude] Add record deletion fixture (#278)")
        self.completion = self.git("rev-parse", "HEAD")
        self.read_finding = finding(
            files=["read_api.py"] if separate_files else ["access.py"],
            suite_ids=[READ_ID])
        self.delete_finding = finding(
            title="Record deletion bypasses administrator authorization",
            description="can_delete grants destructive access to non-administrators.",
            files=["delete_api.py"] if separate_files else ["access.py"],
            severity="Critical", suite_ids=[DELETE_ID],
            attack_scenario="An ordinary user deletes another tenant's records.",
            impact="Destruction of private records across tenants.",
            evidence="can_delete('alice', 'bob', False) returns True.",
            remediation="Require both ownership and administrator privilege.")
        self.start()

    def review_actual_service(self):
        if not self.definition["suites"]:
            self.add_suite(READ_ID, assertion=(
                "self.assertFalse(access['can_read']('alice', 'bob')); "
                "self.assertTrue(access['can_read']('alice', 'alice'))"))
            self.add_suite(DELETE_ID, assertion=(
                "self.assertFalse(access['can_delete']('alice', 'bob', False)); "
                "self.assertFalse(access['can_delete']('alice', 'alice', False)); "
                "self.assertTrue(access['can_delete']('alice', 'alice', True))"))
        service = runpy.run_path(str(self.repo / "access.py"))
        findings = []
        if service["can_read"]("alice", "bob"):
            findings.append(self.read_finding)
        if service["can_delete"]("alice", "bob", False):
            findings.append(self.delete_finding)
        self.worker.ai_output_file.write_text(report(in_scope=findings))

    def metadata(self):
        return json.loads(self.row()["security_findings"])


class SecurityEvidenceLifecycleTests(EvidenceFixture, unittest.TestCase):
    def test_two_vulnerabilities_in_one_module_are_both_counted_and_verified(self):
        self.prepare_service()

        def role(prompt, activity=""):
            if self.loop()["phase"] == "fix":
                self.write_service(read_broken=False, delete_broken=False)
                self.worker.ai_output_file.write_text("Repaired both authorization checks.")
            else:
                self.review_actual_service()
            return 0

        with self.patches(role):
            self.assertEqual(self.worker.run_adversarial_pipeline(), 10)
        self.assertEqual(self.loop()["status"], "FIXED")
        self.assertEqual(len(self.loop()["results"]), 2)
        self.assertTrue(all(r["exit_code"] == 0 for r in self.loop()["results"]))
        metadata = self.metadata()
        self.assertEqual(metadata["inScopeDiscovered"], 2,
                         "Sharing a filename does not merge distinct attack paths")
        self.assertEqual(metadata["inScopeFixed"], 2)
        self.assertEqual(metadata["severity"], {"Critical": 1, "High": 1, "Medium": 0, "Low": 0})
        summary = self.worker.adversarial_summary_line()
        self.assertIn(self.read_finding["title"], summary)
        self.assertIn(self.delete_finding["title"], summary)

    def test_fixing_only_one_of_two_same_file_findings_receives_partial_credit(self):
        self.prepare_service()

        def role(prompt, activity=""):
            if self.loop()["phase"] == "fix":
                self.write_service(read_broken=False, delete_broken=True)
                self.worker.ai_output_file.write_text("Fixed reads; deletion still needs repair.")
            else:
                self.review_actual_service()
            return 0

        with self.patches(role):
            self.worker.run_adversarial_pipeline()
        self.assertEqual(self.loop()["status"], "FAILED")
        self.assertFalse(self.deliver.call_args.kwargs["allow_automation"])
        metadata = self.metadata()
        self.assertEqual(metadata["inScopeFixed"], 1,
                         "The read exploit is fixed even while the deletion exploit in that file remains")
        self.assertEqual(metadata["inScopeDiscovered"], 2)
        self.assertEqual(metadata["inScopeOpen"], 1)

    def test_a_later_fix_that_reintroduces_a_vulnerability_revokes_fixed_status(self):
        self.prepare_service(separate_files=True)

        def role(prompt, activity=""):
            if self.loop()["phase"] == "fix":
                first_fix = self.loop()["round"] == 1
                self.write_service(read_broken=not first_fix, delete_broken=first_fix)
                self.worker.ai_output_file.write_text("Changed the authorization checks.")
            else:
                self.review_actual_service()
            return 0

        with self.patches(role):
            self.worker.run_adversarial_pipeline()
        self.assertEqual(self.loop()["status"], "FAILED")
        metadata = self.metadata()
        self.assertEqual(metadata["inScopeOpen"], 1)
        self.assertEqual(metadata["inScopeFixed"], 1,
                         "Only deletion remains fixed; the read exploit returned in round 2")
        summary = self.worker.adversarial_summary_line()
        self.assertNotIn(self.read_finding["title"] + " (High) — remediated", summary)

    def test_more_precise_file_evidence_is_not_itself_a_security_fix(self):
        self.prepare(vulnerable=True)
        (self.repo / "read_api.py").write_text("from access import can_read\n")
        self.git("add", "read_api.py")
        self.git("commit", "-qm", "[claude] Add reader adapter fixture (#278)")
        self.completion = self.git("rev-parse", "HEAD")
        self.start()

        def role(prompt, activity=""):
            loop = self.loop()
            if loop["phase"] == "test":
                # Repeated review traces an additional caller, with no code
                # changed and the very same exploitable access check.
                files = ["access.py"] if loop["round"] == 0 else ["access.py", "read_api.py"]
                self.worker.ai_output_file.write_text(report(in_scope=[finding(files=files)]))
            else:
                self.worker.ai_output_file.write_text("No remediation was made.")
            return 0

        with self.patches(role):
            self.worker.run_adversarial_pipeline()
        service = runpy.run_path(str(self.repo / "access.py"))
        self.assertTrue(service["can_read"]("alice", "bob"))
        self.assertEqual(self.loop()["status"], "FAILED")
        self.assertEqual(self.metadata()["inScopeFixed"], 0,
                         "An expanded list of affected files cannot verify an unchanged exploit")


if __name__ == "__main__":
    unittest.main()
