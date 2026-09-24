"""The adversarial cybersecurity agent: attack the change before it ships.

Same durable loop as adversarial UAT (`adversarial_core.py`) with security as
its exclusive focus, so the two pipelines are one pipeline:

    build -> attack -> fix -> learn -> repeat

A fresh, independent security engineer reviews the implementation, the code it
touches, its configuration, infrastructure and dependencies, then reports
structured findings. In-scope vulnerabilities are fixed inside this issue and
re-verified by a new reviewer; legitimate findings elsewhere in the repository
become their own labelled GitHub issues instead of silently widening the scope.

Two things separate this stage from UAT and are deliberate:

* Its verdict is not suite exit codes alone. A vulnerability such as a leaked
  credential or a permissive IAM policy has no natural failing unit test, so a
  fresh reviewer's structured in-scope findings block the round as well. A
  review that could not execute records an explicit FAILED status — it can
  never be mistaken for a clean PASS.
* Findings carry severity and confidence, and only confident findings act.
  Low-confidence observations are recorded as advisory rather than fixed or
  filed, because a security agent that files everything it imagines is a
  triage cost, not a control.
"""
from __future__ import annotations

import json
from typing import Any

from adversarial_core import MAX_ROUNDS, AdversarialStage, AdversarialStageMixin

RESULT_MARKER = "SWARM_SECURITY_RESULT:"
TEST_ROOT = "tests/adversarial/security/"
ORIGIN = "adversarial-security"
SUITE_PREFIX = "adversarial-security-"
FINDING_LABEL = "adversarial-security"

SEVERITIES = ("Critical", "High", "Medium", "Low")
CONFIDENCES = ("high", "medium", "low")
#: Only a reviewer who is reasonably sure acts. A "low" confidence finding is
#: recorded for the human summary but never blocks delivery and never becomes
#: a GitHub issue — that is the main defence against security-agent noise.
ACTIONABLE_CONFIDENCES = ("high", "medium")

FINDING_FIELDS = ("title", "description", "severity", "confidence", "attack_scenario",
                  "impact", "evidence", "remediation")

#: Guidance, not a checklist. The reviewer is told to reason about the actual
#: stack and attack surface the issue touches rather than walk this list.
ANALYSIS_AREAS = (
    "authentication and authorization, privilege escalation, access-control bypass; "
    "injection of every kind (command execution, SQL/NoSQL, template, XSS and output "
    "encoding, CSRF where relevant, SSRF); insecure deserialization; path traversal and "
    "file handling; secrets and credential exposure; cryptography misuse and insecure "
    "defaults; sensitive-data exposure including logging of sensitive values; API security, "
    "request/input validation and abuse cases; dependency vulnerabilities and supply-chain "
    "risk; race conditions and unsafe concurrency with security consequences; network "
    "exposure, container, Kubernetes, cloud/IAM and Terraform/IaC configuration; insecure "
    "file and process permissions; OWASP-style application security risks"
)


def normalize_severity(value: Any) -> str:
    text = str(value or "").strip().lower()
    for severity in SEVERITIES:
        if severity.lower() == text:
            return severity
    raise ValueError("Finding severity must be one of " + ", ".join(SEVERITIES))


def normalize_confidence(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text not in CONFIDENCES:
        raise ValueError("Finding confidence must be one of " + ", ".join(CONFIDENCES))
    return text


def normalize_findings(value: Any, *, scope: str) -> list[dict[str, Any]]:
    """Reject anything a human could not triage from the issue alone."""
    if not isinstance(value, list):
        raise ValueError(f"Security {scope} findings must be an array")
    findings = []
    for finding in value:
        if not isinstance(finding, dict) or not all(
            isinstance(finding.get(field), str) and finding[field].strip() for field in FINDING_FIELDS
        ):
            raise ValueError(
                f"Security {scope} findings require " + ", ".join(FINDING_FIELDS)
            )
        files = finding.get("files", [])
        if not isinstance(files, list) or not all(isinstance(v, str) for v in files):
            raise ValueError("Security finding files must be an array of repository paths")
        if not isinstance(finding.get("suite_ids", []), list) or not all(
            isinstance(v, str) for v in finding.get("suite_ids", [])
        ):
            raise ValueError("Finding suite_ids must be an array of suite IDs")
        findings.append({
            **finding,
            "severity": normalize_severity(finding.get("severity")),
            "confidence": normalize_confidence(finding.get("confidence")),
            "files": list(files),
        })
    return findings


def severity_counts(findings: list[dict[str, Any]]) -> dict[str, int]:
    counts = {severity: 0 for severity in SEVERITIES}
    for finding in findings:
        counts[normalize_severity(finding.get("severity"))] += 1
    return counts


def finding_identity(finding: dict[str, Any]) -> str:
    """How a rediscovery of the same vulnerability is recognised across rounds.

    Keyed on the finding's own description, not its title or affected files.
    Neither of those two other fields is stable enough alone: a fresh
    reviewer may reword the same finding's *title* between rounds while the
    vulnerability itself is unchanged (a title-only identity then reads the
    reworded finding as a new one, letting the original "disappear" and get
    wrongly credited as fixed), and the very same unresolved vulnerability
    can gain a longer, more precise *files* list as a reviewer traces
    additional callers without the underlying code ever changing (a
    files-only identity then treats that expanded evidence as a new finding,
    which can equally manufacture a false "fixed" verdict for the original).
    Two independently exploitable checks that merely happen to live in the
    same file are still two distinct findings, so identity cannot be just the
    affected filename either — the description is the field a reviewer keeps
    materially stable for as long as the underlying vulnerability itself is
    unchanged, while still differing between genuinely distinct
    vulnerabilities.
    """
    return " ".join(str(finding.get("description") or "").split()).strip().lower()


def render_finding(finding: dict[str, Any], *, issue_number: int) -> str:
    files = ", ".join(f"`{path}`" for path in finding.get("files", [])) or "_Not identified._"
    return (
        f"Found by the adversarial cybersecurity review of #{issue_number}; outside that "
        "delivery's scope, so it is tracked separately instead of widening that issue.\n\n"
        f"- **Severity:** {finding['severity']}\n"
        f"- **Confidence:** {finding['confidence']}\n"
        f"- **Affected component(s):** {files}\n\n"
        f"## Vulnerability\n{finding['description'].strip()}\n\n"
        f"## Why this is a security concern\n{finding.get('why', finding['impact']).strip()}\n\n"
        f"## Attack scenario\n{finding['attack_scenario'].strip()}\n\n"
        f"## Impact\n{finding['impact'].strip()}\n\n"
        f"## Evidence / reproduction\n{finding['evidence'].strip()}\n\n"
        f"## Recommended remediation\n{finding['remediation'].strip()}\n"
    )


class SecurityStage(AdversarialStage):
    key = "adversarial_security"
    slug = "security"
    label = "Adversarial Cybersecurity"
    log_name = "adversarial cybersecurity"
    test_root = TEST_ROOT
    excluded_test_roots = ()
    origin = ORIGIN
    #: A security fix is product code. Re-running the UAT suites alongside the
    #: security ones is how "rerun affected functional tests" is enforced: a
    #: hardening change that breaks behaviour fails this stage's round too.
    blocking_origins = (ORIGIN, "adversarial")
    suite_prefix = SUITE_PREFIX
    result_marker = RESULT_MARKER
    dispute_marker = "SWARM_SECURITY_DISPUTE:"
    finding_marker_kind = "adversarial-security-finding"
    finding_labels = (
        ("bug", "d73a4a", "Something is not working"),
        (FINDING_LABEL, "B60205", "Found by the adversarial cybersecurity review"),
    )
    dedup_label = FINDING_LABEL
    router_task = "Security / adversarial code analysis task — Adversarial Cybersecurity"
    filed_findings_column = "security_filed_findings"
    #: A clean review legitimately registers no suite and writes no test, so
    #: neither may fail the round closed the way UAT's acceptance suite does.
    require_suites = False
    require_tests = False
    reports_failures = True

    def activity(self, loop: dict[str, Any]) -> str:
        round_number = loop["round"]
        if loop["phase"] == "fix":
            return f"fixing adversarial security round {round_number} findings"
        if round_number == 0:
            return "running independent adversarial security review"
        return f"re-verifying after adversarial security fix round {round_number}"

    def round_start_log(self, issue_number: int, loop: dict[str, Any]) -> str:
        round_number, phase = loop["round"], loop["phase"]
        if phase == "fix":
            detail = f"starting fix/re-test round {round_number} of {MAX_ROUNDS}."
        elif round_number == 0:
            detail = f"starting independent security review (round 0 of {MAX_ROUNDS})."
        else:
            detail = f"starting re-test for round {round_number} of {MAX_ROUNDS}."
        return f"{self.label} for issue #{issue_number}: {detail}"

    def on_round_start(self, worker, loop: dict[str, Any]) -> None:
        from swarm_issue_worker import log
        base = str(worker.read_state()["base_sha"])
        files = [path for path in worker.git("diff", "--name-only", base, "HEAD").splitlines() if path]
        log(f"{self.label} for issue #{worker.issue.number}: analyzing {len(files)} changed "
            f"file(s) and their surrounding attack surface.")

    def parse_report(self, output: str) -> dict[str, Any]:
        from adversarial_core import parse_result
        report = parse_result(self.result_marker, output)
        report["in_scope"] = normalize_findings(report.get("in_scope", []), scope="in-scope")
        report["out_of_scope"] = normalize_findings(report.get("out_of_scope", []), scope="out-of-scope")
        # An empty payload (e.g. bare `{}`) must not read as "analysis ran and
        # found nothing" — a real review always has something to say, even a
        # one-paragraph clean bill of health.
        if not isinstance(report.get("summary", ""), str) or not str(report.get("summary") or "").strip():
            raise ValueError("Security review summary must be a non-empty string")
        return report

    def blocking_findings(self, report: dict[str, Any]) -> list[dict[str, Any]]:
        return [finding for finding in report.get("in_scope", [])
                if finding["confidence"] in ACTIONABLE_CONFIDENCES]

    def finding_issue_body(self, worker, finding: dict[str, Any]) -> str:
        return render_finding(finding, issue_number=worker.issue.number)

    def findings_to_file(self, report: dict[str, Any]) -> list[dict[str, Any]]:
        return [finding for finding in report.get("out_of_scope", [])
                if finding["confidence"] in ACTIONABLE_CONFIDENCES]

    def excludable_findings(self, report: dict[str, Any]) -> list[dict[str, Any]]:
        # A speculative (low-confidence) claim must not be able to retire a
        # suite from this round's blocking set — only findings this stage
        # would actually act on may exclude one.
        return [finding for finding in report.get("out_of_scope", [])
                if finding["confidence"] in ACTIONABLE_CONFIDENCES]

    def record_round(self, worker, loop: dict[str, Any], report: dict[str, Any],
                     round_value: dict[str, Any], blocking: list[dict[str, Any]],
                     results: list[dict[str, Any]] | None = None) -> None:
        from swarm_issue_worker import log
        discovered = loop.setdefault("discovered_findings", [])
        fixed = loop.setdefault("fixed_findings", [])
        advisory = loop.setdefault("advisory_findings", [])
        known = {finding_identity(item) for item in discovered}
        for finding in blocking:
            if finding_identity(finding) not in known:
                discovered.append(finding)
                known.add(finding_identity(finding))
        for finding in report.get("in_scope", []):
            if finding["confidence"] not in ACTIONABLE_CONFIDENCES and not any(
                finding_identity(item) == finding_identity(finding) for item in advisory
            ):
                advisory.append(finding)
        # A fresh reviewer that no longer reports a previously blocking finding
        # is the verification that the fix worked — the fixer's own claim is
        # never accepted as evidence. But when the finding named its own
        # reproduction suite, that suite still failing overrides the
        # reviewer's silence: a failing regression test is stronger evidence
        # of an unresolved vulnerability than a fresh reviewer not mentioning it.
        still_open = {finding_identity(finding) for finding in blocking}
        exit_codes = {str(result.get("id")): result.get("exit_code") for result in (results or [])}
        # "Fixed" is this round's evidence, not a one-way ratchet: a later
        # round can reintroduce a vulnerability an earlier round already
        # verified fixed (a subsequent fix regresses it), so anything blocking
        # again this round must lose its earlier fixed credit.
        fixed[:] = [item for item in fixed if finding_identity(item) not in still_open]
        already_fixed = {finding_identity(item) for item in fixed}
        newly_fixed = []
        for item in discovered:
            identity = finding_identity(item)
            if identity in still_open or identity in already_fixed:
                continue
            suite_ids = item.get("suite_ids") or []
            if suite_ids and any(exit_codes.get(sid, 1) != 0 for sid in suite_ids):
                continue
            newly_fixed.append(item)
            already_fixed.add(identity)
        fixed.extend(newly_fixed)
        loop["open_findings"] = blocking
        loop["summary"] = str(report.get("summary") or "").strip()
        round_value["findings_found"] = len(blocking)
        round_value["findings_fixed"] = len(newly_fixed)
        round_value["findings_filed"] = len(loop.get("filed_finding_details", []))
        round_value["severity_counts"] = json.dumps(severity_counts(discovered), sort_keys=True)
        counts = severity_counts(blocking)
        log(f"{self.label} for issue #{worker.issue.number}: round {loop['round']} found "
            f"{len(blocking)} actionable in-scope finding(s) "
            f"(Critical {counts['Critical']}, High {counts['High']}, Medium {counts['Medium']}, "
            f"Low {counts['Low']}) and verified {len(newly_fixed)} fix(es).")

    def review_status(self, loop: dict[str, Any]) -> str:
        if loop.get("review_error"):
            return "FAILED"
        outcome = loop.get("outcome")
        if outcome == "cap_hit" or not outcome:
            return "FAILED"
        if loop.get("fixed_findings"):
            return "FIXED"
        if loop.get("filed_finding_details"):
            return "FINDINGS_CREATED"
        return "PASS"

    def history_fields(self, loop: dict[str, Any]) -> dict[str, Any]:
        return {
            "security_outcome": loop.get("outcome", ""),
            "security_round_count": loop.get("round", 0),
            "security_review_status": loop.get("status", ""),
            # A completed round (successful or not) is the current verdict; an
            # earlier failed attempt's error must not linger once this round
            # writes its own outcome.
            "security_review_error": loop.get("review_error", ""),
            "security_findings": self.findings_metadata(loop),
        }

    def disabled_history_fields(self) -> dict[str, Any]:
        return {"security_outcome": "disabled", "security_review_status": ""}

    def failure_history_fields(self, reason: str) -> dict[str, Any]:
        return {"security_review_status": "FAILED", "security_review_error": reason}

    def findings_metadata(self, loop: dict[str, Any]) -> dict[str, Any]:
        discovered = loop.get("discovered_findings", [])
        return {
            "status": loop.get("status", ""),
            "inScopeDiscovered": len(discovered),
            "inScopeFixed": len(loop.get("fixed_findings", [])),
            "inScopeOpen": len(loop.get("open_findings", [])),
            "advisory": len(loop.get("advisory_findings", [])),
            "issuesCreated": len(loop.get("filed_finding_details", [])),
            "severity": severity_counts(discovered),
            "testsAdded": loop.get("tests_added", 0),
            "summary": loop.get("summary", ""),
            "findings": [
                {
                    "title": finding.get("title", ""),
                    "severity": finding.get("severity", ""),
                    "confidence": finding.get("confidence", ""),
                    "files": finding.get("files", []),
                }
                for finding in discovered
            ],
        }

    def summary_line(self, loop: dict[str, Any]) -> str:
        status = loop.get("status") or self.review_status(loop)
        if not loop.get("outcome") and not loop.get("review_error"):
            return ""
        metadata = self.findings_metadata(loop)
        counts = metadata["severity"]
        line = (
            f"- {self.label}: {status} — {metadata['inScopeDiscovered']} in-scope finding(s), "
            f"{metadata['inScopeFixed']} fixed, {metadata['issuesCreated']} follow-up issue(s) filed; "
            f"Critical {counts['Critical']} / High {counts['High']} / Medium {counts['Medium']} / "
            f"Low {counts['Low']}; {metadata['testsAdded']} security test file(s) added.\n"
        )
        return line + self.review_block(loop, metadata)

    def review_block(self, loop: dict[str, Any], metadata: dict[str, Any]) -> str:
        """The structured review a human reads on the issue, without transcripts."""
        if not (metadata["inScopeDiscovered"] or metadata["issuesCreated"]
                or metadata["advisory"] or loop.get("review_error")):
            return ""
        filed = loop.get("filed_finding_details", [])
        counts = metadata["severity"]
        lines = [
            "<details><summary>Adversarial Cybersecurity review</summary>\n",
            f"Status: {metadata['status']}\n",
            "Security findings:",
            f"- In-scope discovered: {metadata['inScopeDiscovered']}",
            f"- In-scope fixed: {metadata['inScopeFixed']}",
            f"- In-scope unresolved: {metadata['inScopeOpen']}",
            f"- Low-confidence advisory: {metadata['advisory']}",
            f"- Out-of-scope issues created: {metadata['issuesCreated']}",
            f"- Critical: {counts['Critical']}",
            f"- High: {counts['High']}",
            f"- Medium: {counts['Medium']}",
            f"- Low: {counts['Low']}\n",
            "Summary:",
            metadata["summary"] or "No material security findings were reported.",
            "",
            "Changes made:",
        ]
        lines += ([f"- {item.get('title', '')} ({item.get('severity', '')}) — remediated and re-verified "
                   "by a fresh reviewer." for item in loop.get("fixed_findings", [])] or ["- None."])
        lines += ["", "New GitHub issues:"]
        lines += ([f"- {item.get('url') or item.get('title', '')}" for item in filed] or ["- None."])
        lines += ["", "Validation:"]
        lines += ([f"- {result['id']}: {'passed' if not result['exit_code'] else 'FAILED'}"
                   for result in loop.get("results", [])] or ["- No executable suite was required for this review."])
        if loop.get("review_error"):
            lines += ["", f"Review error: {loop['review_error']}"]
        lines += ["</details>\n"]
        return "\n".join(lines)

    def cap_hit_output(self, loop: dict[str, Any]) -> str:
        metadata = self.findings_metadata(loop)
        open_findings = "\n".join(
            f"- {finding.get('severity')} / {finding.get('confidence')} confidence: {finding.get('title')}"
            for finding in loop.get("open_findings", [])
        ) or "- (no findings were reported in the final round)"
        failures = "\n".join(f"- {r['id']}: {r['output'][-2000:]}" for r in loop["results"] if r["exit_code"])
        return (
            "## Summary\nThe adversarial cybersecurity review did not reach a clean state after six "
            "fix/re-test rounds. Delivered as best effort: this is the last remediation attempt, not a "
            "verified-clean security review.\n\n" + self.summary_line(loop) +
            "\n## Unresolved security findings\n" + open_findings +
            ("\n\n## Failing security validation\n" + failures if failures else "") +
            "\n\n## Still outstanding\nReview the linked pull request and adjudicate these findings "
            "against the issue's requirements.\n"
        )

    def prompt(self, worker, loop: dict[str, Any], common: str) -> str:
        if loop["phase"] == "fix":
            return self.fix_prompt(worker, loop, common)
        return self.review_prompt(worker, loop, common)

    def fix_prompt(self, worker, loop: dict[str, Any], common: str) -> str:
        return (
            "You are the implementer in a bounded adversarial security fix/verify cycle. An "
            "independent security reviewer found the vulnerabilities below in your issue's change. "
            "Fix every one of them properly — remove the weakness, do not mask the symptom or the "
            "reviewer's detection of it. Keep the issue's intended behaviour working. Then re-run "
            "the affected functional and security checks yourself before finishing.\n"
            f"Do not change, delete, skip, disable or retire any test under tests/adversarial/ "
            "(including its security/ subtree) or any part of .swarm/tests.json. If a finding is "
            "wrong — not exploitable, already mitigated elsewhere, or outside this issue — state the "
            f"specific finding and the code evidence on a line beginning {self.dispute_marker}; a "
            "fresh independent reviewer will adjudicate it.\n"
            "Never resolve a secrets finding by committing a replacement secret; remove the value and "
            "read it from configuration or the environment instead.\n"
            + common +
            "\nIn-scope security findings to remediate:\n"
            + json.dumps(loop.get("open_findings", []), indent=2)
            + "\n\nFailing validation suites:\n" + json.dumps(loop.get("results", []), indent=2)
        )

    def review_prompt(self, worker, loop: dict[str, Any], common: str) -> str:
        base = str(worker.read_state()["base_sha"])
        changed = [path for path in worker.git("diff", "--name-only", base, "HEAD").splitlines() if path]
        previous = [
            {"title": finding.get("title"), "severity": finding.get("severity"),
             "confidence": finding.get("confidence")}
            for finding in loop.get("discovered_findings", [])
        ]
        amendments = "\n".join(str(c.get("body") or "") for c in loop.get("amendments", []))
        return (
            "You are a fresh, independent adversarial security engineer. Your only job is to attack "
            "this change and the surface it exposes, then report what a real attacker could do. You "
            "have no memory of the implementer's or any previous reviewer's reasoning and must not "
            "look for it. Read the actual code before asserting anything.\n\n"
            "Reason about the real technology stack and attack surface this issue touches. Areas "
            f"worth considering — guidance, not a checklist to walk: {ANALYSIS_AREAS}.\n\n"
            "Scope rules:\n"
            "- A vulnerability introduced by this change, directly affecting the functionality being "
            "changed, exposed because of this implementation, or that must be corrected for this "
            "issue to be securely implemented, is IN SCOPE. Report it under in_scope so it is fixed "
            "inside this issue.\n"
            "- A legitimate weakness elsewhere in the repository that is not reasonably part of this "
            "issue is OUT OF SCOPE. Report it under out_of_scope with full evidence; the worker files "
            "it as its own labelled GitHub issue. Do not widen this issue to cover it, and do not add "
            "a blocking test for it.\n\n"
            "False positives are the main failure mode of a security agent, so:\n"
            "- inspect the real code path before asserting a vulnerability; quote the evidence;\n"
            "- report exploitable behaviour, not generic best-practice or stylistic hardening;\n"
            "- set confidence honestly: 'high' means you traced an exploitable path, 'medium' means "
            "the weakness is real but exploitability depends on deployment, 'low' means speculative. "
            "Only high and medium findings are acted on — a low finding is recorded and nothing else, "
            "so do not inflate confidence to force action;\n"
            "- set severity from realistic exploitability and impact (remote vs local, authentication "
            "and privileges required, attack complexity, confidentiality/integrity/availability "
            "impact, blast radius), not theoretical possibility. No CVSS vector is required.\n"
            "- prefer a few high-confidence actionable findings over many speculative ones; report "
            "nothing rather than padding the list.\n\n"
            "Where a finding can be demonstrated deterministically, add a failing security regression "
            f"test under {TEST_ROOT} — malicious input, authorization-boundary, access-control, "
            "injection, path-manipulation, malformed-request, permission or insecure-configuration "
            "checks. Tests must be deterministic and safe: never attack an external system, never "
            "perform a destructive or uncontrolled action, and use local fixtures only. Register "
            f"suites in .swarm/tests.json with origin='{ORIGIN}', ids prefixed '{SUITE_PREFIX}', "
            "enabled=true, disruptive=false, explicit argv commands and timeoutSeconds <= 1800. "
            "Retain every existing suite and all other metadata untouched — in particular the "
            "origin='adversarial' UAT suites and everything under tests/adversarial/ that is not in "
            f"{TEST_ROOT}; you do not own them. You may write security tests, not product fixes. "
            "If an existing suite fails for an unrelated reason, report its ID in that finding's "
            "suite_ids array so it does not block this issue.\n"
            + common +
            "\nFiles changed by this issue:\n" + ("\n".join(f"- {path}" for path in changed) or "- None.")
            + "\n\nFindings already recorded for this issue in earlier rounds (do not re-report one "
            "that is now genuinely fixed; do re-report one that is not):\n"
            + (json.dumps(previous, indent=2) if previous else "None.")
            + "\n\nFramework scaffold plan (apply autonomously, no sign-off):\n"
            + json.dumps(loop.get("bootstrap", {}), indent=2)
            + f"\nOnly {TEST_ROOT}, .swarm/tests.json and necessary test framework manifests may be "
            "changed.\nDispute to adjudicate:\n"
            + (loop.get("dispute") or ("Adjudicate earlier findings against these trusted amendments: "
                                       + amendments if amendments
                                       else "None. Keep earlier findings and tests intact."))
            + "\nReturn one final line, and only one, with the whole review as JSON:\n"
            + RESULT_MARKER + ' {"summary":"one paragraph a human can read","dispute_resolution":"",'
            '"in_scope":[{"title":"...","description":"...","severity":"Critical|High|Medium|Low",'
            '"confidence":"high|medium|low","files":["path"],"attack_scenario":"...","impact":"...",'
            '"evidence":"code and reproduction evidence","remediation":"...","suite_ids":[]}],'
            '"out_of_scope":[]}\n'
        )


SECURITY_STAGE = SecurityStage()


class AdversarialSecurityMixin(AdversarialStageMixin):
    """The security-named entry points, mirroring `AdversarialUatMixin`."""

    def save_adversarial_security(self, loop: dict[str, Any]) -> None:
        self.save_stage(SECURITY_STAGE, loop)

    def initialize_adversarial_security(self, completion: str, output: str) -> None:
        self.initialize_stage(SECURITY_STAGE, completion, output)

    def file_security_findings(self, loop: dict[str, Any], findings: list[dict[str, Any]]) -> None:
        self.file_stage_findings(SECURITY_STAGE, loop, findings)

    def validate_security_edits(self, loop: dict[str, Any], report: dict[str, Any]) -> tuple[int, int]:
        return self.validate_stage_edits(SECURITY_STAGE, loop, report)
