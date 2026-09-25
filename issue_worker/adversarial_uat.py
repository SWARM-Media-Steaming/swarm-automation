"""The adversarial UAT agent: independent acceptance tests before delivery.

Round zero is the independent assessment of the normal implementation. Each
of the six subsequent rounds is exactly one fix plus one fresh assessment.
Only suite exit codes decide success; an agent's self-reported pass never does.

The durable loop itself lives in `adversarial_core.py` and is shared with the
adversarial cybersecurity agent (`adversarial_security.py`). Everything here is
what makes *UAT* different: its prompts, its owned test root, and its suites.
"""
from __future__ import annotations

import json
from typing import Any

from adversarial_core import (  # noqa: F401  (re-exported for callers and tests)
    CAP_HIT_PR_MARKER,
    CAP_HIT_PR_NOTICE,
    DEFINITION,
    FINDING_TITLE_MAX_SYMMETRIC_DIFFERENCE,
    FINDING_TITLE_SIMILARITY_THRESHOLD,
    FINDING_TITLE_STOPWORDS,
    FRAMEWORK_FILES,
    TEST_ROOT,
    AdversarialStage,
    AdversarialStageMixin,
    _finding_title_is_reworded_duplicate,
    _finding_title_similarity,
    _finding_title_stem,
    _finding_title_token_sequence,
    _finding_title_tokens,
    read_definition,
    run_suites,
)

import adversarial_core

# Declared here, not merely re-exported, so the UAT agent's own module states
# its round cap: round zero is the independent assessment and at most six
# counted fix/re-test rounds follow it. It must stay identical to the shared
# loop's cap or the two would disagree about when a deadlock has been reached.
MAX_ROUNDS = 6
if MAX_ROUNDS != adversarial_core.MAX_ROUNDS:
    raise RuntimeError("Adversarial UAT round cap diverged from the shared adversarial loop")

RESULT_MARKER = "SWARM_ADVERSARIAL_RESULT:"
#: The cybersecurity agent keeps its suites in a subtree of the same root so
#: everything adversarial stays under one directory; UAT does not own it.
SECURITY_TEST_ROOT = "tests/adversarial/security/"


class UatStage(AdversarialStage):
    key = "adversarial"
    slug = "uat"
    label = "Adversarial UAT"
    log_name = "adversarial UAT"
    test_root = TEST_ROOT
    excluded_test_roots = (SECURITY_TEST_ROOT,)
    origin = "adversarial"
    blocking_origins = ("adversarial",)
    suite_prefix = "adversarial-"
    result_marker = RESULT_MARKER
    dispute_marker = "SWARM_TEST_DISPUTE:"
    finding_marker_kind = "adversarial-finding"
    finding_labels = (
        ("bug", "d73a4a", "Something is not working"),
        ("adversarial-uat", "5319e7", "Found by independent adversarial tests"),
    )
    router_task = "Adversarial UAT"
    filed_findings_column = "adversarial_filed_findings"
    require_suites = True
    require_tests = True

    def prompt(self, worker, loop: dict[str, Any], common: str) -> str:
        if loop["phase"] == "fix":
            return (
                "You are the implementer in a bounded adversarial fix/test cycle. Fix failures "
                "within the issue's scope. Do not change, delete, skip, disable, or retire any "
                "test under tests/adversarial/ or any part of .swarm/tests.json. "
                "If an expectation is wrong, state the specific test and issue/spec evidence "
                "on a line beginning SWARM_TEST_DISPUTE:; a fresh independent tester will decide.\n" +
                common + "\nSpecific suite failures:\n" + json.dumps(loop["results"], indent=2)
            )
        amendments = "\n".join(str(c.get("body") or "") for c in loop.get("amendments", []))
        return (
            "You are a fresh, independent adversarial tester. Challenge prior assumptions and "
            "use repository evidence as the source of truth. Derive expected behavior from "
            "the issue/spec and domain invariants BEFORE inspecting the diff; never assert "
            "current behavior merely because the implementation does it. Try boundary conditions, "
            "malformed input, error paths and related past regressions. Add meaningful executable "
            "UAT/integration tests under tests/adversarial/. You may write tests, not product fixes. "
            "Register suites in .swarm/tests.json with origin='adversarial', ids prefixed 'adversarial-', "
            "enabled=true, disruptive=false, deterministic fixtures, explicit argv commands and "
            "timeoutSeconds <= 1800. Retain existing non-adversarial suites and metadata. "
            "Every command must actually collect the new tests and fail if an assertion fails. "
            "Do not weaken/retire earlier tests unless adjudicating the dispute below; you are "
            "a new instance, not their author. Explain any revision against the issue's requirements. "
            "Real findings outside this issue's scope must not block delivery: report evidence "
            "as out_of_scope findings for the worker to file as separate labelled, assigned issues; "
            "do not add those findings as blocking tests. If an existing suite fails for an "
            "unrelated reason, retain it for scheduled runs and report its ID in that finding's "
            "suite_ids array; use separate suites for in-scope and out-of-scope assertions. "
            f"Leave {SECURITY_TEST_ROOT} and any origin='adversarial-security' suite untouched; "
            "a separate cybersecurity agent owns them.\n"
            + common + "\nFramework scaffold plan (apply autonomously, no sign-off):\n" +
            json.dumps(loop.get("bootstrap", {}), indent=2) +
            "\nOnly tests/adversarial/, .swarm/tests.json and necessary test framework manifests "
            "may be changed. Preserve the framework choice.\nDispute to adjudicate:\n" +
            (loop.get("dispute") or ("Adjudicate prior tests against these trusted amendments: " + amendments
                                      if amendments else "None. Keep earlier test expectations intact.")) +
            '\nReturn one final line: SWARM_ADVERSARIAL_RESULT: {"dispute_resolution":"",'
            '"out_of_scope":[{"title":"...","body":"reproduction, evidence and why outside scope","suite_ids":[]}]}\n'
        )


UAT_STAGE = UatStage()


def test_path(path: str) -> bool:
    return UAT_STAGE.owns_path(path)


def adversarial_activity(loop: dict[str, Any]) -> str:
    return UAT_STAGE.activity(loop)


def result_payload(output: str) -> dict[str, Any]:
    return UAT_STAGE.parse_report(output)


class AdversarialUatMixin(AdversarialStageMixin):
    """The UAT-named entry points the worker and its tests call."""

    def save_adversarial(self, loop: dict[str, Any]) -> None:
        self.save_stage(UAT_STAGE, loop)

    def initialize_adversarial(self, completion: str, output: str) -> None:
        self.initialize_stage(UAT_STAGE, completion, output)

    def choose_adversarial_provider(self, loop: dict[str, Any]):
        return self.choose_stage_provider(UAT_STAGE, loop)

    def file_adversarial_findings(self, loop: dict[str, Any], findings: list[dict[str, str]]) -> None:
        self.file_stage_findings(UAT_STAGE, loop, findings)

    def validate_adversarial_edits(self, loop: dict[str, Any], report: dict[str, Any]) -> tuple[int, int]:
        return self.validate_stage_edits(UAT_STAGE, loop, report)

    def run_adversarial_delivery(self) -> int:
        return self.run_adversarial_pipeline()
