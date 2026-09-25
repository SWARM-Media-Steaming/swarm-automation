"""Shared framework for the pre-delivery adversarial agents.

One issue can be challenged by several independent adversarial agents before it
is delivered — UAT today (`adversarial_uat.py`) and cybersecurity
(`adversarial_security.py`). They differ only in what they look for, what they
say and which suites they own; the durable loop around them is identical:

    build -> attack -> fix -> learn -> repeat

Round zero is an independent assessment of the normal implementation. Each of
the six subsequent rounds is exactly one fix plus one fresh assessment by a new
context. Only executable suite exit codes and a fresh agent's structured report
decide success; an agent's self-reported pass never does.

`AdversarialStage` is the per-agent description (state key, owned test root,
suite origin, prompts, report schema). `AdversarialStageMixin` is the loop, and
is stage-agnostic: every UAT-specific string used to live in it and none does
now. Add an agent by subclassing `AdversarialStage`, not by copying the loop.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import tempfile
from typing import Any

MAX_ROUNDS = 6
DEFINITION = ".swarm/tests.json"
TEST_ROOT = "tests/adversarial/"
CAP_HIT_PR_MARKER = "<!-- swarm-issue-worker:adversarial-cap-hit -->"
CAP_HIT_PR_NOTICE = (CAP_HIT_PR_MARKER + "\nAdversarial UAT is still failing after six fix/re-test rounds. "
                     "Automation is held; review the failing tests and adjudicate on the linked issue.\n\n")
# A later round's fresh-context tester rediscovering an earlier round's
# out-of-scope bug almost never reproduces the same title/body wording, so an
# exact-digest marker match cannot dedup it. Title token overlap is a coarse
# but wording-independent stand-in: it survives rewording ("crashes on empty
# YAML" vs "crashes on empty YAML file") without a semantic model. The
# threshold trades a few merged near-duplicates for not spamming GitHub with
# repeat issues for the same bug.
#
# Jaccard similarity alone is not enough: two *distinct* bugs that share a
# phrasing template ("<subject> crashes on empty <field>") can clear a high
# similarity bar purely from the shared scaffolding words even though the one
# differing word is the entire distinguishing content (Login vs. Signup form,
# JSON vs. XML parser). A pure rewording only ever adds or drops filler words
# (the symmetric difference between the two token sets is small), whereas a
# substituted content word removes one token and adds a different one (the
# symmetric difference is at least 2). Require both: high overlap and a small
# symmetric difference, so a substitution can't hide behind a high ratio.
#
# Symmetric difference alone still misses *insertion*: a second, genuinely
# distinct bug whose title is the first bug's title plus one qualifying word
# ("Export fails silently" -> "CSV export fails silently") only adds one
# token, so it clears the symmetric-difference bar too even though the
# inserted word is exactly what makes it a different bug. But an appended
# elaboration of the same subject ("... empty YAML" -> "... empty YAML
# file") also only adds one token, and that *is* the same bug reworded --
# so symmetric difference can't be resolved by "is it inserted" alone; where
# the extra word lands matters. A word prepended ahead of everything else
# narrows the sentence's own subject/verb ("CSV export...", "Settings
# sidebar..."), which is how a genuinely distinct, more specific bug reads.
# A word appended at the end, or folded into a reordered clause a fresh-
# context tester wrote from scratch ("Config parser crashes on empty YAML"
# -> "Empty YAML file crashes config parser"), is an elaboration or a
# paraphrase of the same bug -- but that is only true when the extra word is
# itself a plain, lowercase filler noun. A specific format/platform qualifier
# ("XML", "Safari") reads as a proper noun or acronym in the original title
# even when it lands mid-sentence or at the end ("Export crashes on large
# XML files", "Video playback stutters on Safari"), and *that* capitalization
# is what marks it as the distinguishing content of a genuinely separate bug
# rather than incidental elaboration -- ordinary English filler words like
# "file" stay lowercase wherever they land. So: once similarity and symmetric
# difference both clear their bars, reject the match (treat as distinct) when
# the single differing token is either the very first token of the title
# that contains it, or is capitalized/acronym-cased in that title's original
# wording.
FINDING_TITLE_SIMILARITY_THRESHOLD = 0.6
FINDING_TITLE_MAX_SYMMETRIC_DIFFERENCE = 1
FINDING_TITLE_STOPWORDS = {
    "a", "an", "and", "are", "at", "by", "for", "in", "is", "of", "on", "or", "the", "to", "with",
}


_FINDING_TITLE_ES_SUFFIX_STEMS = ("s", "x", "z", "ch", "sh")


def _finding_title_stem(token: str) -> str:
    # Plain suffix stripping so morphological variants of the same word
    # ("upload"/"uploads", "time"/"times") land on the same token instead of
    # being counted as unrelated content words when a reworded rediscovery
    # changes verb tense or number.
    #
    # A base ending in a sibilant (s/x/z/ch/sh) takes "-es", not "-s"
    # ("crash"/"crashes", "fix"/"fixes", "catch"/"catches") -- stripping only
    # the trailing "s" leaves a dangling "e" ("crashe") that never matches
    # the base form, so an ordinary verb-form rewording between rounds would
    # wrongly look like a distinct content word. Strip the full "-es" first
    # when the remaining stem itself ends in one of those sibilants.
    if len(token) > 4 and token.endswith("es") and token[:-2].endswith(_FINDING_TITLE_ES_SUFFIX_STEMS):
        return token[:-2]
    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def _finding_title_token_sequence(title: str) -> list[tuple[str, str]]:
    # Each entry pairs the stemmed/lowered token used for set comparison
    # with its original-cased spelling, so a caller can tell a plain filler
    # word from a proper noun or acronym occupying the same slot.
    tokens = []
    for raw in re.findall(r"[A-Za-z0-9]+", title):
        lowered = raw.lower()
        if lowered in FINDING_TITLE_STOPWORDS:
            continue
        tokens.append((_finding_title_stem(lowered), raw))
    return tokens


def _finding_title_tokens(title: str) -> set[str]:
    return {stem for stem, _raw in _finding_title_token_sequence(title)}


def _finding_title_similarity(a: str, b: str) -> float:
    tokens_a, tokens_b = _finding_title_tokens(a), _finding_title_tokens(b)
    if not tokens_a or not tokens_b:
        return 0.0
    return len(tokens_a & tokens_b) / len(tokens_a | tokens_b)


def _finding_title_is_reworded_duplicate(a: str, b: str) -> bool:
    sequence_a, sequence_b = _finding_title_token_sequence(a), _finding_title_token_sequence(b)
    set_a = {stem for stem, _raw in sequence_a}
    set_b = {stem for stem, _raw in sequence_b}
    if not set_a or not set_b:
        return False
    symmetric_difference = set_a ^ set_b
    similarity = len(set_a & set_b) / len(set_a | set_b)
    if not (similarity >= FINDING_TITLE_SIMILARITY_THRESHOLD
            and len(symmetric_difference) <= FINDING_TITLE_MAX_SYMMETRIC_DIFFERENCE):
        return False
    if not symmetric_difference:
        return True
    extra_token = next(iter(symmetric_difference))
    extra_sequence = sequence_a if extra_token in set_a else sequence_b
    extra_index = next(i for i, (stem, _raw) in enumerate(extra_sequence) if stem == extra_token)
    if extra_index == 0:
        return False
    extra_raw = extra_sequence[extra_index][1]
    return extra_raw == extra_raw.lower()
# Framework wiring is the only non-test code a tester may scaffold. This list
# is deliberately explicit: adding a test framework must not grant product edits.
FRAMEWORK_FILES = {
    "Cargo.toml", "Cargo.lock", "package.json", "package-lock.json", "pnpm-lock.yaml",
    "yarn.lock", "pyproject.toml", "pytest.ini", "tox.ini", "requirements-test.txt",
    "build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts",
    "pom.xml", "go.mod", "go.sum", "tsconfig.json",
}


def read_definition(root: Path) -> dict[str, Any]:
    path = root / DEFINITION
    value = json.loads(path.read_text()) if path.exists() else {"version": 1, "suites": []}
    if not isinstance(value, dict) or not isinstance(value.get("suites"), list) or not all(isinstance(s, dict) for s in value.get("suites", [])):
        raise ValueError(".swarm/tests.json must contain a suites array")
    return value


def parse_result(marker: str, output: str) -> dict[str, Any]:
    """The single structured verdict line a fresh adversarial agent returns."""
    all_lines = output.splitlines()
    marker_indices = [i for i, line in enumerate(all_lines) if line.strip().startswith(marker)]
    if len(marker_indices) != 1:
        raise ValueError(f"Tester must return exactly one {marker.rstrip(':')} JSON line")
    index = marker_indices[0]
    after_marker = all_lines[index].strip()[len(marker):].strip()
    # The prompt asks for the JSON on the marker's own line, but a model
    # occasionally pretty-prints it across several lines instead; splice in
    # everything after the marker through the end of the output and parse
    # with raw_decode so that still works, along with a trailing markdown
    # fence or prose the model tacks on after the JSON value ends.
    remainder = "\n".join([after_marker, *all_lines[index + 1:]]).strip()
    remainder = re.sub(r"^```[\w-]*\n?", "", remainder).strip()
    try:
        value, _ = json.JSONDecoder().raw_decode(remainder)
    except json.JSONDecodeError as error:
        raise ValueError(f"Could not parse {marker.rstrip(':')} JSON: {error}") from error
    if not isinstance(value, dict):
        raise ValueError("Invalid adversarial result")
    if not isinstance(value.get("dispute_resolution", ""), str):
        raise ValueError("Invalid dispute resolution")
    return value


def validate_finding_list(value: Any, *, required: tuple[str, ...]) -> list[dict[str, Any]]:
    """Reject a finding list that cannot be acted on.

    Every adversarial agent reports findings the worker turns into GitHub
    issues, so the fields a filed issue needs are mandatory here rather than
    silently defaulted: an issue with an empty title or no evidence is noise a
    human then has to triage.
    """
    if not isinstance(value, list):
        raise ValueError("Findings must be an array")
    for finding in value:
        if not isinstance(finding, dict) or not all(
            isinstance(finding.get(field), str) and finding[field].strip() for field in required
        ):
            raise ValueError("Findings require " + " and ".join(required))
        if not isinstance(finding.get("suite_ids", []), list) or not all(
            isinstance(v, str) for v in finding.get("suite_ids", [])
        ):
            raise ValueError("Finding suite_ids must be an array of suite IDs")
    return value


def run_suites(root: Path, suites: list[dict[str, Any]], *, require_suites: bool = True) -> list[dict[str, Any]]:
    """Direct argv, bounded runtime/output, no shell or AI-reported verdicts."""
    results = []
    if not suites:
        if not require_suites:
            return []
        return [{"id": "adversarial-registration", "exit_code": 1,
                 "output": "No enabled adversarial suite was registered."}]
    for suite in suites:
        command = suite.get("command")
        error = ""
        if not isinstance(command, list) or not command or not all(isinstance(v, str) and v and "\0" not in v for v in command):
            error = "Invalid suite command; expected a nonempty argv array."
        if suite.get("enabled", True) is not True or suite.get("disruptive", False):
            error = "Adversarial suites must be enabled and non-disruptive."
        requirements = suite.get("requirements", {})
        if not isinstance(requirements, dict):
            error = "Suite requirements must be an object."
        if isinstance(requirements, dict) and requirements.get("aiTestData"):
            error = "Adversarial suites must use deterministic fixtures, not generated test data."
        timeout = suite.get("timeoutSeconds", 1800)
        if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= 1800:
            error = "Suite timeoutSeconds must be an integer from 1 to 1800."
        code = 1
        output = error
        if not error:
            with tempfile.TemporaryFile() as log:
                try:
                    process = subprocess.Popen(command, cwd=root, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}, stdout=log, stderr=subprocess.STDOUT,
                                               start_new_session=True)
                    try:
                        code = process.wait(timeout=timeout)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()
                        code = 124
                        output = f"Suite timed out after {timeout}s.\n"
                    log.seek(0, os.SEEK_END)
                    log.seek(max(0, log.tell() - 12000))
                    output += log.read().decode("utf-8", errors="replace")
                except OSError as exc:
                    output = str(exc)
        # Several common runners return success when they collect no tests.
        # A zero-test run cannot prove the implementation survived UAT.
        counts = re.findall(r"(?:Ran|running) (\d+) tests?\b|# tests (\d+)\b", output)
        if code == 0 and counts and not any(int(a or b) for a, b in counts):
            code = 1
            output += "\nNo tests were collected; this is not a passing UAT suite."
        results.append({"id": str(suite.get("id", "unknown")), "exit_code": code, "output": output})
    return results


class AdversarialStage:
    """What makes one adversarial agent different from the others.

    Everything below is data or a small pure hook. The durable fix/re-test
    loop lives once, in `AdversarialStageMixin`.
    """

    #: Worker-state key holding this stage's loop checkpoint.
    key = "adversarial"
    #: Short identifier persisted with each execution-history round row.
    slug = "uat"
    #: Human label used in operator logs and the GitHub completion comment.
    label = "Adversarial UAT"
    #: Sentence-case form for mid-sentence log text ("filed out-of-scope
    #: <log_name> finding"). Overview and log-integrity checks match on it.
    log_name = "adversarial UAT"
    #: Root this stage's tests live under, and subtrees inside it it does not own.
    test_root = TEST_ROOT
    excluded_test_roots: tuple[str, ...] = ()
    #: `.swarm/tests.json` `origin` this stage owns, and every origin whose
    #: suites must pass for this stage's round to be clean.
    origin = "adversarial"
    blocking_origins: tuple[str, ...] = ("adversarial",)
    suite_prefix = "adversarial-"
    #: The single structured line a fresh tester must return.
    result_marker = "SWARM_ADVERSARIAL_RESULT:"
    dispute_marker = "SWARM_TEST_DISPUTE:"
    #: Idempotency marker kind and labels for auto-filed out-of-scope findings.
    finding_marker_kind = "adversarial-finding"
    finding_labels: tuple[tuple[str, str, str], ...] = (
        ("bug", "d73a4a", "Something is not working"),
        ("adversarial-uat", "5319e7", "Found by independent adversarial tests"),
    )
    #: When set, open issues carrying this label are also checked for a
    #: reworded rediscovery before a new finding is filed.
    dedup_label = ""
    #: How the dynamic model router is told what kind of work this is.
    router_task = "Adversarial UAT"
    #: A stage whose verdict is suite exit codes alone must have suites.
    require_suites = True
    require_tests = True
    #: Whether a review that could not execute is persisted as an explicit
    #: FAILED status rather than only raised.
    reports_failures = False

    def owns_path(self, path: str) -> bool:
        return (path.startswith(self.test_root)
                and not any(path.startswith(root) for root in self.excluded_test_roots)
                and "__pycache__" not in path.split("/") and not path.endswith(".pyc"))

    def activity(self, loop: dict[str, Any]) -> str:
        """What the log should say a provider is doing right now.

        An adversarial stage can route implementation, fixing and testing to
        different providers for the same issue, so a bare "is working" no
        longer says which of those this invocation is. Round 0 in the "test"
        phase is the first independent assessment of the fresh implementation,
        before any fix has happened; every later "test" is a re-test after the
        fix round of the same number.
        """
        round_number = loop["round"]
        if loop["phase"] == "fix":
            return f"fixing adversarial round {round_number} findings"
        if round_number == 0:
            return "running independent adversarial UAT"
        return f"re-testing after adversarial fix round {round_number}"

    def round_start_log(self, issue_number: int, loop: dict[str, Any]) -> str:
        round_number, phase = loop["round"], loop["phase"]
        if phase == "fix":
            detail = f"starting fix/re-test round {round_number} of {MAX_ROUNDS}."
        elif round_number == 0:
            detail = f"starting independent test run (round 0 of {MAX_ROUNDS})."
        else:
            detail = f"starting re-test for round {round_number} of {MAX_ROUNDS}."
        return f"{self.label} for issue #{issue_number}: {detail}"

    def on_round_start(self, worker, loop: dict[str, Any]) -> None:
        """Extra observability a stage wants when a round begins."""

    def fix_applied_log(self, issue_number: int, loop: dict[str, Any]) -> str:
        return (f"{self.label} for issue #{issue_number}: fix applied in round "
                f"{loop['round']} of {MAX_ROUNDS}.")

    def parse_report(self, output: str) -> dict[str, Any]:
        report = parse_result(self.result_marker, output)
        report["out_of_scope"] = validate_finding_list(
            report.get("out_of_scope", []), required=("title", "body")
        )
        return report

    def finding_issue_body(self, worker, finding: dict[str, Any]) -> str:
        return (f"Found while testing #{worker.issue.number}; outside that delivery's scope.\n\n"
                f"{finding['body']}")

    #: Execution-history column holding this stage's auto-filed finding list.
    filed_findings_column = "adversarial_filed_findings"

    def record_round(self, worker, loop: dict[str, Any], report: dict[str, Any],
                     round_value: dict[str, Any], blocking: list[dict[str, Any]],
                     results: list[dict[str, Any]] | None = None) -> None:
        """Fold this round's report into the durable loop state.

        UAT carries no structured findings beyond the tests it writes, so there
        is nothing to accumulate.
        """

    def review_status(self, loop: dict[str, Any]) -> str:
        """PASS / FIXED / FINDINGS_CREATED / FAILED for the finished stage."""
        if loop.get("review_error"):
            return "FAILED"
        outcome = loop.get("outcome")
        if outcome == "cap_hit":
            return "FAILED"
        if outcome == "resolved_after_n":
            return "FIXED"
        if loop.get("filed_finding_details"):
            return "FINDINGS_CREATED"
        return "PASS" if outcome else "FAILED"

    def failure_history_fields(self, reason: str) -> dict[str, Any]:
        return {}

    def findings_to_file(self, report: dict[str, Any]) -> list[dict[str, Any]]:
        """Out-of-scope findings worth a separate GitHub issue."""
        return report.get("out_of_scope", [])

    def blocking_findings(self, report: dict[str, Any]) -> list[dict[str, Any]]:
        """In-scope findings that must be fixed before this stage can pass.

        UAT expresses every in-scope finding as a failing executable test, so
        the suite results are the whole verdict.
        """
        return []

    def excludable_findings(self, report: dict[str, Any]) -> list[dict[str, Any]]:
        """Out-of-scope findings whose cited suite_ids may exclude a suite
        from this round's blocking set.

        UAT's out-of-scope findings carry no confidence signal, so every one
        of them is eligible. A stage whose findings do carry confidence (see
        `SecurityStage`) must restrict this to findings it would actually act
        on — a merely speculative claim must never be able to silently retire
        a suite this round would otherwise have to pass.
        """
        return report.get("out_of_scope", [])

    def prompt(self, worker, loop: dict[str, Any], common: str) -> str:
        raise NotImplementedError

    def summary_line(self, loop: dict[str, Any]) -> str:
        outcome = loop.get("outcome")
        if not outcome:
            return ""
        description = {"clean_first_pass": "clean first pass",
                       "resolved_after_n": f"resolved after {loop['round']} rounds",
                       "cap_hit": f"still failing after {MAX_ROUNDS} rounds"}[outcome]
        return f"- {self.label}: {description}, {loop['tests_added']} test files added.\n"

    def history_fields(self, loop: dict[str, Any]) -> dict[str, Any]:
        return {"adversarial_round_count": loop["round"], "adversarial_outcome": loop["outcome"]}

    def disabled_history_fields(self) -> dict[str, Any]:
        return {"adversarial_outcome": "disabled"}

    def cap_hit_output(self, loop: dict[str, Any]) -> str:
        failures = "\n".join(f"- {r['id']}: {r['output'][-2000:]}" for r in loop["results"] if r["exit_code"])
        return (
            "## Summary\nAdversarial UAT did not pass after six fix/re-test rounds. Delivered as best "
            "effort: this is the last fix attempt, not a verified-clean pass.\n\n" +
            self.summary_line(loop) + "\n" + failures +
            "\n\n## Still failing\nThese adversarial tests were not satisfied. Automatic approval, "
            "merging and promotion are held; review the linked PR and adjudicate the failing tests.\n"
        )


class AdversarialStageMixin:
    """Worker integration kept separate from delivery and lifecycle mechanics.

    Every method here is stage-agnostic: it is handed an `AdversarialStage` and
    reads its own checkpoint from `stage.key`, so a second agent type costs a
    stage class rather than a second copy of the loop.
    """

    #: Ordered stages a repository can run, filled in by the concrete Worker.
    def adversarial_stages(self) -> list[AdversarialStage]:
        raise NotImplementedError

    def save_stage(self, stage: AdversarialStage, loop: dict[str, Any]) -> None:
        self.update_state(**{stage.key: loop})

    def read_stage(self, stage: AdversarialStage) -> dict[str, Any] | None:
        return self.read_state().get(stage.key)

    def initialize_stage(self, stage: AdversarialStage, completion: str, output: str,
                         delivery_choice: dict[str, Any] | None = None,
                         excluded_suites: list[str] | None = None) -> None:
        from swarm_issue_worker import iso_timestamp
        import dataclasses
        state = self.read_state()
        self.save_stage(stage, {
            "phase": "test", "round": 0, "completion": completion,
            "amendments": list(self.issue.followup_comments),
            "implementation_output": output, "implementer": self.choice.name,
            "delivery_choice": delivery_choice or dataclasses.asdict(self.choice),
            "capacity_used": [self.choice.name],
            "fixer_provider": self.choice.name, "fixer_model": self.choice.model,
            "round_started": iso_timestamp(), "outcome": "", "results": [],
            "tests_added": 0, "tests_modified": 0,
            "dispute": state.get(f"{stage.key}_initial_dispute", ""), "rounds": [],
            "capacity_start": ({self.choice.name: state["usage_at_start"]["remaining_percent"]}
                               if (state.get("usage_at_start") or {}).get("remaining_percent") is not None else {}),
            "capacity_end": {}, "filed_findings": [], "filed_finding_details": [],
            # A prior stage's out-of-scope scope decision (a pre-existing,
            # unrelated suite failure it already excluded from blocking) still
            # describes the same repository state for a later stage of the
            # same pipeline run. Without carrying it forward, a later stage
            # would re-discover that same unrelated failure as its own blocker
            # and burn its rounds on a bug the earlier stage already scoped out.
            "excluded_suites": sorted(set(excluded_suites or [])),
            "findings": [], "advisory_findings": [], "fixed_findings": [], "status": "",
        })

    def refresh_adversarial_requirements(self) -> None:
        """Trusted comments may clarify the spec during a quota pause."""
        state = self.read_state()
        stages = [stage for stage in self.adversarial_stages() if state.get(stage.key)]
        if not stages:
            return
        comments = self.load_resume_comments(self.issue.number, int(state.get("session_comment_id", 0)))
        if not comments:
            return
        self.update_state(session_comment_id=int(comments[-1]["id"]))
        for stage in stages:
            loop = self.read_state()[stage.key]
            known = {c["id"] for c in loop.get("amendments", [])}
            loop.setdefault("amendments", []).extend(c for c in comments if c["id"] not in known)
            # A previously returned report cannot establish success against a
            # requirement received later. Reassess with a fresh tester first.
            if loop["phase"] in {"done", "test"}:
                loop.update(phase="test", outcome="", active=False, response=None)
                loop.pop("delivery", None)
            self.save_stage(stage, loop)

    def choose_stage_provider(self, stage: AdversarialStage, loop: dict[str, Any]):
        from swarm_issue_worker import ProviderChoice, RouterCandidate, RouterError, build_router_prompt, iso_timestamp
        usages = {s.name: self.provider_usage(s.key) for s in self.config.enabled_specs}
        remaining = {name: u.remaining_percent for name, u in usages.items() if u.usable}
        previous = loop["fixer_provider"] if loop["phase"] == "test" else ""
        choice = self.choose_provider(previous, remaining)
        for name, usage in usages.items():
            if usage.remaining_percent is not None:
                loop["capacity_start"].setdefault(name, usage.remaining_percent)
                loop["capacity_end"][name] = usage.remaining_percent
        if choice is None:
            self.save_stage(stage, loop)
            return None
        # Prefer a different tester provider even if the model router would
        # have preferred the implementer. Same-provider fallback is still fresh.
        names = self.provider_priority_order(previous, remaining)
        alternatives = [name for name in names if name != previous]
        if previous and alternatives:
            names = alternatives
        if self.config.dynamic_model_routing:
            candidates = [RouterCandidate(
                key=s.key, name=s.name, tiers=self.config.routing_tiers[s.key],
                strengths=s.strengths, usage_remaining=remaining[s.name],
            ) for s in self.config.enabled_specs if s.name in names and self.config.routing_tiers.get(s.key)]
            host = self.config.require_spec(choice.key)
            if candidates:
                try:
                    prompt = build_router_prompt(
                        title=f"{stage.router_task} {loop['phase']}: {self.issue.title}",
                        body=self.issue.body, labels=self.issue.labels, candidates=candidates,
                        previous_provider=previous.lower(), rework=bool(previous),
                        routing_optimization=self.config.routing_optimization,
                        allow_usage_credit_models=self.config.allow_usage_credit_models,
                    )
                    decision = self.resolve_router_response(
                        self.run_router(host, prompt, []), prompt=prompt, candidates=candidates,
                        host=host, images=[], previous_provider=previous.lower(), rework=bool(previous),
                    )
                    spec = self.config.require_spec(decision["provider"])
                    choice = ProviderChoice(spec.name, decision["selected_model"], decision["reasoning_effort"], self.new_session_id(spec))
                except RouterError as error:
                    self.history.warning(f"Adversarial routing used capacity fallback: {error}", iso_timestamp())
        if choice.name not in loop["capacity_used"]:
            loop["capacity_used"].append(choice.name)
        return choice

    def adversarial_common_prompt(self, stage: AdversarialStage, loop: dict[str, Any]) -> str:
        base = str(self.read_state()["base_sha"])
        # No completion summary, session transcript, or implementer reasoning
        # enters the tester context. It sees the spec, diff and repo conventions.
        diff = self.git("diff", "--no-ext-diff", base, "--", ".", f":(exclude){TEST_ROOT.rstrip('/')}")
        amendments = "\n".join(str(c.get("body") or "") for c in loop.get("amendments", []))
        rejected = loop.get("retry_rejection") or {}
        rejection_guidance = ""
        if rejected:
            rejection_guidance = (
                "\nA prior tester result was rejected and its edits were rolled back. Do not repeat it.\n"
                f"Rejection: {rejected.get('reason', 'invalid tester result')}\n"
                f"Rejected paths: {json.dumps(rejected.get('paths', []))}\n"
            )
        return (
            f"Issue #{self.issue.number}: {self.issue.title}\n\n{self.issue.body}\n\n"
            f"Trusted issue amendments (authoritative clarifications):\n{amendments or 'None.'}\n\n"
            f"Repository: {self.config.repo_dir}\nRemain on {self.expected_branch()}. "
            "Read repository conventions (AGENTS.md, CLAUDE.md, .claude/rules and existing test patterns). "
            "Do not inspect prior agent transcripts, session logs, or completion summaries. "
            "Do not commit, push, open PRs, post comments, or file issues; the worker handles delivery. "
            "Run checks in the foreground. Do not edit VERSION.\n" + rejection_guidance +
            "\nResulting patch:\n" + diff + "\n"
        )

    def adversarial_prompt(self, stage: AdversarialStage, loop: dict[str, Any]) -> str:
        return stage.prompt(self, loop, self.adversarial_common_prompt(stage, loop))

    def adversarial_changed_paths(self, baseline: str) -> set[str]:
        # --no-renames sees a moved test as a deletion plus an addition.
        paths = self.git("diff", "--no-renames", "--name-only", "-z", baseline).split("\0")
        paths += self.git("ls-files", "--others", "--exclude-standard", "-z").split("\0")
        return {p for p in paths if p and (not p.startswith(".swarm/") or p == DEFINITION)}

    def reject_adversarial_edits(self, baseline: str, error: Exception) -> tuple[list[str], Path]:
        """Archive rejected tester work, then restore the guarded baseline.

        A fresh tester must not inherit edits that the validator has already
        declared invalid.  Keep the patch in worker state for diagnosis, but
        remove only paths the adversarial role was allowed to touch; unrelated
        checkout work remains intact.
        """
        paths = sorted(self.adversarial_changed_paths(baseline))
        patch_path = self.state / "last-rejected-adversarial.patch"
        patch = self.git("diff", "--binary", "--no-ext-diff", baseline, "--", *paths, check=False) if paths else ""
        untracked = sorted(set(self.git("ls-files", "--others", "--exclude-standard", "-z", "--", *paths,
                                        check=False).split("\0")) - {""}) if paths else []
        if untracked:
            # A single batched diff instead of one `git diff --no-index`
            # subprocess per file: an ungitignored Cargo/npm scaffold under
            # tests/adversarial/ can leave thousands of untracked build
            # artifacts here, and spawning a subprocess per file made this
            # pass slow enough to still be running when the next scheduled
            # cycle started, colliding with it on .git/index.lock.
            self.git("add", "--intent-to-add", "--", *untracked)
            untracked_patch = self.git("diff", "--binary", "--no-ext-diff", "--", *untracked, check=False)
            self.git("reset", "--", *untracked)
            patch += ("\n" if patch and not patch.endswith("\n") else "") + untracked_patch
        patch_path.write_text(patch, encoding="utf-8")
        # One batched existence lookup instead of one `git cat-file -e` per
        # path, for the same reason.
        existed_at_baseline = set(self.git("ls-tree", "-r", "--name-only", "-z", baseline, "--", *paths,
                                           check=False).split("\0")) - {""} if paths else set()
        tracked_at_baseline = [p for p in paths if p in existed_at_baseline]
        newly_added = [p for p in paths if p not in existed_at_baseline]
        if tracked_at_baseline:
            self.git("restore", f"--source={baseline}", "--staged", "--worktree", "--", *tracked_at_baseline)
        if newly_added:
            self.git("rm", "-f", "--ignore-unmatch", "--", *newly_added, check=False)
            for path in newly_added:
                (self.config.repo_dir / path).unlink(missing_ok=True)
        return paths, patch_path

    def validate_stage_edits(self, stage: AdversarialStage, loop: dict[str, Any],
                             report: dict[str, Any]) -> tuple[int, int]:
        from swarm_issue_worker import WorkerError
        baseline = loop["stage_base"]
        if self.git("branch", "--show-current") != self.expected_branch():
            raise WorkerError("Adversarial role changed the issue branch")
        if not self.git_ok("merge-base", "--is-ancestor", baseline, "HEAD"):
            raise WorkerError("Adversarial role rewrote the issue history")
        paths = self.adversarial_changed_paths(baseline)
        if loop["phase"] == "fix":
            # Every adversarial test root is protected from every fixer, not
            # just this stage's own: a security fixer must not quietly weaken a
            # UAT expectation to make its round go green either.
            protected = {p for p in paths if p.startswith(TEST_ROOT) or p == DEFINITION}
            if protected:
                # Preserve product repairs, but undo attempts to grade one's own
                # homework. A fresh tester, not the fixer, sees this as a dispute.
                # Batched (git ls-tree once, then one restore call and one rm
                # call) rather than one git subprocess per path: an
                # ungitignored Cargo/npm scaffold under tests/adversarial/ can
                # leave thousands of untracked build artifacts here, which
                # also fall under TEST_ROOT and made a per-file loop slow
                # enough to collide with other git activity on .git/index.lock.
                sorted_protected = sorted(protected)
                existed_at_baseline = set(self.git("ls-tree", "-r", "--name-only", "-z", baseline, "--",
                                                    *sorted_protected, check=False).split("\0")) - {""}
                to_restore = [p for p in sorted_protected if p in existed_at_baseline]
                to_remove = [p for p in sorted_protected if p not in existed_at_baseline]
                if to_restore:
                    self.git("restore", f"--source={baseline}", "--staged", "--worktree", "--", *to_restore)
                if to_remove:
                    self.git("rm", "-f", "--ignore-unmatch", "--", *to_remove)
                    for path in to_remove:
                        (self.config.repo_dir / path).unlink(missing_ok=True)
                loop["dispute"] += "\nFixer attempted to alter protected tests; worker restored them: " + ", ".join(sorted_protected)
            return 0, 0
        forbidden = paths - {p for p in paths if stage.owns_path(p) or p == DEFINITION or p in FRAMEWORK_FILES}
        if forbidden:
            raise WorkerError("Tester changed product files: " + ", ".join(sorted(forbidden)))
        before = json.loads(self.git("show", f"{baseline}:{DEFINITION}"))
        after = read_definition(self.config.repo_dir)
        others = lambda v: {**v, "suites": [s for s in v["suites"] if s.get("origin") != stage.origin]}
        if others(before) != others(after):
            raise WorkerError("Tester changed existing framework choice or non-adversarial suites/metadata")
        changed_tests = {p for p in paths if stage.owns_path(p)}
        # One batched git ls-tree instead of one `git cat-file -e` per path,
        # for the same reason as the fix-phase restoration above.
        existed_at_baseline = set(self.git("ls-tree", "-r", "--name-only", "-z", baseline, "--",
                                           *changed_tests, check=False).split("\0")) - {""} if changed_tests else set()
        prior = changed_tests & existed_at_baseline
        old_suites = [s for s in before["suites"] if s.get("origin") == stage.origin]
        new_suites = [s for s in after["suites"] if s.get("origin") == stage.origin]
        revised = prior or any(s not in new_suites for s in old_suites)
        if revised and not ((loop.get("dispute") or loop.get("amendments")) and report.get("dispute_resolution", "").strip()):
            raise WorkerError("Only a fresh tester adjudicating a dispute may revise or retire existing adversarial tests")
        owned = [p for p in self.git("ls-files", "--cached", "--others", "--exclude-standard", "-z").split("\0")
                 if stage.owns_path(p)]
        if stage.require_tests and not owned:
            raise WorkerError("Tester did not leave any adversarial test files")
        ids = [s.get("id") for s in after["suites"]]
        if len(ids) != len(set(ids)) or any(not str(s.get("id", "")).startswith(stage.suite_prefix) for s in new_suites):
            raise WorkerError("Adversarial suite IDs must be unique and visibly prefixed")
        for path in changed_tests | {DEFINITION}:
            if (self.config.repo_dir / path).is_symlink():
                raise WorkerError("Adversarial files may not be symlinks")
        return len(changed_tests - prior), len(prior)

    def prepare_adversarial_framework(self, stage: AdversarialStage, loop: dict[str, Any]) -> None:
        from ai_test_assist import bootstrap
        from swarm_issue_worker import WorkerError, atomic_write_json
        if loop.get("bootstrap"):
            return
        definition = read_definition(self.config.repo_dir)
        if not definition.get("adversarialBootstrap"):
            spec = self.config.require_spec(self.choice.key)
            plan = bootstrap(str(self.config.repo_dir), spec.key, spec.bin or "", spec.model)
            if not plan.get("ok"):
                raise WorkerError(f"Test framework bootstrap failed: {plan.get('error')}")
            plan.pop("ok", None)
            definition["adversarialBootstrap"] = plan
            atomic_write_json(self.config.repo_dir / DEFINITION, definition)
            # .swarm drafts are normally app-owned/untracked (the app excludes
            # .swarm/ via info/exclude so its own scratch files never show up as
            # dirty); this file is intentionally a repository artifact, so it
            # must be force-staged or a plain `git add` fails outright on an
            # ignored, previously-untracked path.
            self.git("add", "--force", "--", DEFINITION)
            self.commit_completed_work(self.git("rev-parse", "HEAD"))
        loop["bootstrap"] = definition["adversarialBootstrap"]
        self.save_stage(stage, loop)

    def existing_finding_titles(self, stage: AdversarialStage) -> list[dict[str, str]]:
        """Open issues already filed by this stage, for cross-issue dedup.

        Only stages with a dedicated label use this: the digest marker dedups
        rediscoveries within one issue's own loop, but a vulnerability class
        found again while working a *different* issue reaches GitHub through a
        different marker entirely.
        """
        from swarm_issue_worker import WorkerError
        if not stage.dedup_label:
            return []
        output = self.github.gh([
            "issue", "list", "--repo", self.config.github_repository, "--state", "open",
            "--label", stage.dedup_label, "--json", "title,url", "--limit", "100",
        ], self.choice.key)
        try:
            issues = json.loads(output)
        except json.JSONDecodeError as error:
            raise WorkerError(
                "GitHub returned malformed JSON while listing existing adversarial findings"
            ) from error
        if not isinstance(issues, list) or not all(isinstance(issue, dict) for issue in issues):
            raise WorkerError("GitHub returned an invalid issue list while listing existing adversarial findings")
        return [{"title": str(issue.get("title") or ""), "url": str(issue.get("url") or "")}
                for issue in issues]

    def file_stage_findings(self, stage: AdversarialStage, loop: dict[str, Any],
                            findings: list[dict[str, Any]]) -> None:
        # Same labelled/assigned issue creation path as the Actions monitor,
        # with an idempotent marker so a retry after create cannot duplicate it.
        # Out-of-scope findings are specified not to block delivery of the
        # issue under test, so a GitHub failure here (rate limit, 403,
        # network) must never escape this loop: it would otherwise abort the
        # whole adversarial round from inside the stage loop, stalling the
        # original issue over a problem that isn't its own.
        from ai_execution_history import sanitize_text
        from swarm_issue_worker import WorkerError, github_issue_url_from_output, iso_timestamp, log

        def find_existing_finding(digest: str, marker: str) -> dict[str, Any] | None:
            """Return a prior filing, treating bad GitHub output as a retryable failure."""
            output = self.github.gh([
                "issue", "list", "--repo", self.config.github_repository, "--state", "all",
                "--search", f'"{digest}" in:body', "--json", "body,url", "--limit", "100",
            ], self.choice.key)
            try:
                issues = json.loads(output)
            except json.JSONDecodeError as error:
                raise WorkerError(
                    "GitHub returned malformed JSON while searching for an existing finding"
                ) from error
            if not isinstance(issues, list) or not all(isinstance(issue, dict) for issue in issues):
                raise WorkerError(
                    "GitHub returned an invalid issue list while searching for an existing finding"
                )
            return next((issue for issue in issues if marker in str(issue.get("body") or "")), None)

        for finding in findings:
            digest = hashlib.sha256(json.dumps(finding, sort_keys=True).encode()).hexdigest()[:20]
            marker = f"<!-- swarm-issue-worker:{stage.finding_marker_kind}:issue:{self.issue.number};id:{digest} -->"
            stored_title = finding["title"][:120]
            title = " ".join(stored_title.split())
            # A finding's title can itself quote the vulnerability (a
            # credential, a token) — the log is operator-visible output, not
            # the filed issue body, so it goes through the same secret
            # scrubber persisted history uses rather than the raw title.
            safe_title = sanitize_text(title)
            details = loop.setdefault("filed_finding_details", [])
            existing_detail = next((item for item in details if item.get("marker") == marker), None)
            if marker in loop["filed_findings"] and existing_detail and existing_detail.get("url"):
                log(f"Out-of-scope {stage.log_name} finding already filed for #{self.issue.number}: {safe_title}")
                continue
            # A fresh-context tester in a later round rediscovering the same
            # underlying bug almost never reproduces round 0's exact wording,
            # so the digest above will differ. Check title similarity against
            # everything already filed for this issue (accumulated across
            # rounds and resumes in `details`) before trusting the digest.
            reworded_duplicate = next(
                (item for item in details
                 if item.get("url") and _finding_title_is_reworded_duplicate(title, item.get("title", ""))),
                None,
            )
            if reworded_duplicate:
                log(f"Out-of-scope {stage.log_name} finding already filed for #{self.issue.number} "
                    f"(reworded rediscovery of \"{sanitize_text(reworded_duplicate.get('title', ''))}\"): "
                    f"{reworded_duplicate.get('url')}")
                continue
            try:
                if marker in loop["filed_findings"]:
                    # A prior filing's `gh issue create` succeeded but its stdout
                    # didn't yield a usable URL, so this finding's stored detail
                    # is stuck at "". Re-check search: the issue is genuinely on
                    # GitHub and may now be discoverable, so recover its URL
                    # instead of leaving it permanently blank.
                    recovered = find_existing_finding(digest, marker)
                    url = github_issue_url_from_output(str(recovered.get("url", ""))) if recovered else ""
                    if existing_detail is not None:
                        existing_detail["url"] = url
                    else:
                        details.append({"marker": marker, "title": stored_title, "url": url})
                    log(f"Out-of-scope {stage.log_name} finding already filed for #{self.issue.number}: {url or safe_title}")
                    self.save_stage(stage, loop)
                    self.history.update(
                        iso_timestamp(),
                        **{stage.filed_findings_column: [
                            {"title": item.get("title", ""), "url": item.get("url", "")}
                            for item in details
                        ]},
                    )
                    continue
                already_filed = find_existing_finding(digest, marker)
                if already_filed:
                    url = github_issue_url_from_output(str(already_filed.get("url", "")))
                    log(f"Out-of-scope {stage.log_name} finding already filed for #{self.issue.number}: {url or safe_title}")
                else:
                    # A vulnerability class rediscovered while working a
                    # different issue carries a different marker, so the digest
                    # search above cannot see it. Compare titles against this
                    # stage's still-open findings before adding another.
                    open_duplicate = next(
                        (item for item in self.existing_finding_titles(stage)
                         if _finding_title_is_reworded_duplicate(title, item["title"])),
                        None,
                    )
                    if open_duplicate:
                        log(f"Out-of-scope {stage.log_name} finding suppressed as a duplicate of the open "
                            f"issue \"{sanitize_text(open_duplicate['title'])}\": {open_duplicate['url']}")
                        continue
                    output = self.file_labelled_issue(
                        finding["title"][:120],
                        f"{marker}\n{stage.finding_issue_body(self, finding)}",
                        stage.finding_labels, self.choice.key)
                    url = github_issue_url_from_output(output)
                    if url:
                        log(f"Filed out-of-scope {stage.log_name} finding for #{self.issue.number}: {url}")
                    else:
                        log(f"GitHub did not return an issue URL for the out-of-scope {stage.log_name} finding: {output.strip()!r}")
                loop["filed_findings"].append(marker)
                if not any(item.get("marker") == marker for item in details):
                    details.append({"marker": marker, "title": stored_title, "url": url})
                self.save_stage(stage, loop)
                self.history.update(
                    iso_timestamp(),
                    **{stage.filed_findings_column: [
                        {"title": item.get("title", ""), "url": item.get("url", "")}
                        for item in details
                    ]},
                )
            except WorkerError as error:
                # Leave `filed_findings`/`filed_finding_details` untouched so a
                # later round or a retried tester run gets another chance to
                # file this same finding, deduplicated by the same marker.
                log(f"Could not file out-of-scope {stage.log_name} finding for #{self.issue.number}: {error}")

    def pause_adversarial(self) -> int:
        from swarm_issue_worker import QUOTA_PAUSED_EXIT_CODE, iso_timestamp
        # A probe can stop before Codex creates a session. A placeholder gives
        # the existing pause format an identity but session_started stays false.
        if not self.choice.session_id:
            import uuid
            self.choice.session_id = str(uuid.uuid4())
            self.update_state(session_id=self.choice.session_id, session_started=False)
        self.mark_quota_paused()
        self.history.update(iso_timestamp(), final_status="quota_paused")
        self.post_quota_comment()
        self.suspend_paused()
        return QUOTA_PAUSED_EXIT_CODE

    def adversarial_summary_line(self) -> str:
        """Every finished stage's one-line verdict, for the completion comment."""
        if not self.in_progress_file.exists():
            return ""
        state = self.read_state()
        return "".join(stage.summary_line(state[stage.key])
                       for stage in self.adversarial_stages() if state.get(stage.key))

    def run_adversarial_stage(self, stage: AdversarialStage) -> int | None:
        """Run one stage to `done`. Returns an exit code only to stop early."""
        from swarm_issue_worker import WorkerError, iso_timestamp, log
        loop = self.read_state()[stage.key]
        while loop["phase"] != "done":
            if not loop.get("active"):
                choice = self.choose_stage_provider(stage, loop)
                if choice is None:
                    return self.pause_adversarial()
                self.choice = choice
                self.update_state_for_choice(choice)
                try:
                    self.ensure_bot_auth()
                    self.prepare_adversarial_framework(stage, loop)
                    loop.update(active=True, stage_base=loop.pop("retry_stage_base", None) or self.git("rev-parse", "HEAD"), response=None)
                    self.save_stage(stage, loop)
                    log(stage.round_start_log(self.issue.number, loop))
                    stage.on_round_start(self, loop)
                except WorkerError as error:
                    # Setup/auth/provider errors happen before any report is
                    # even attempted, so the narrow report/session failure
                    # checks below never see them. Record the failure here too
                    # or a broken setup silently bypasses failure recording.
                    self.record_stage_failure(stage, loop, str(error))
                    raise
            if loop.get("response") is None:
                # Every tester phase starts with a new CLI session. Only an
                # interrupted *same phase* resumes its existing session.
                self.issue_images = []
                try:
                    status = self.run_ai(self.adversarial_prompt(stage, loop), activity=stage.activity(loop))
                except WorkerError as error:
                    # An unavailable executable or other setup failure raises
                    # directly rather than returning a nonzero status, so it
                    # would otherwise bypass the failure recording below entirely.
                    self.record_stage_failure(stage, loop, str(error))
                    raise
                if status != 0 or not self.ai_output_file.exists() or not self.ai_output_file.stat().st_size:
                    if self.ai_failure_is_quota():
                        return self.pause_adversarial()
                    self.record_stage_failure(stage, loop, "the adversarial coding session failed")
                    raise WorkerError("Adversarial coding session failed; phase and worktree preserved")
                loop["response"] = self.ai_output_file.read_text(encoding="utf-8", errors="replace")
                self.save_stage(stage, loop)
            output = loop["response"]
            if loop["phase"] == "fix":
                loop["dispute"] = "\n".join(line.partition(":")[2].strip() for line in output.splitlines()
                                             if line.startswith(stage.dispute_marker))
                self.validate_stage_edits(stage, loop, {})
                loop["fixer_provider"], loop["fixer_model"] = self.choice.name, self.choice.model
                import dataclasses
                loop["delivery_choice"] = dataclasses.asdict(self.choice)
            else:
                try:
                    report = stage.parse_report(output)
                    added, modified = self.validate_stage_edits(stage, loop, report)
                    definition = read_definition(self.config.repo_dir)
                    # The tester prompt explicitly permits citing a pre-existing,
                    # non-adversarial suite's ID in an out-of-scope finding ("If
                    # an existing suite fails for an unrelated reason ... report
                    # its ID in that finding's suite_ids array"), so the set of
                    # names this validates against must include every suite in
                    # the definition, not just this stage's own —
                    # otherwise a tester following that instruction to the
                    # letter gets rejected for naming a real, known suite. This
                    # reference check, and the exclusion it feeds, must live
                    # inside this same try/except: an invalid reference is
                    # exactly as unrecoverable a tester result as a malformed
                    # report, and needs the same rejection/retry and failure
                    # recording, not a silent escape from both.
                    known_suites = {s["id"] for s in definition["suites"]}
                    cited_suite_ids = {sid for finding in report.get("out_of_scope", [])
                                       for sid in finding.get("suite_ids", [])}
                    if cited_suite_ids - known_suites:
                        raise WorkerError("Out-of-scope findings named unknown suites")
                    # Only findings this stage would actually act on may retire
                    # a suite from blocking. A merely speculative (low
                    # confidence) claim must never be able to silently exclude
                    # a suite the round would otherwise have to satisfy.
                    excluded = {sid for finding in stage.excludable_findings(report)
                               for sid in finding.get("suite_ids", [])}
                    candidate_excluded = set(loop.get("excluded_suites", [])) | excluded
                    runnable = [s for s in definition["suites"]
                                if s.get("origin") == stage.origin and s["id"] not in candidate_excluded]
                    if stage.require_suites and not runnable:
                        # A tester may legitimately register a *new* suite purely
                        # to formally record a pre-existing, unrelated regression
                        # for future scheduled runs (test_unrelated_failing_suite_
                        # remains_scheduled_but_does_not_block) — new suite IDs
                        # are not disqualified on their own. What must never
                        # happen is excluding every adversarial suite this round
                        # would otherwise run: that includes this issue's own
                        # just-registered acceptance suite, which run_suites then
                        # fails closed on ("no enabled adversarial suite was
                        # registered") with nothing able to fix it, burning every
                        # round until the cap hits and delivery stalls needing
                        # input — the 2026-09-23 production incident on issue
                        # #360, where Claude cited its own new suite alongside a
                        # real pre-existing one in the same out_of_scope finding.
                        raise WorkerError(
                            "Out-of-scope findings would exclude every adversarial suite this round; "
                            "this issue's own acceptance suite may not be marked out of scope"
                        )
                except (ValueError, TypeError, KeyError, WorkerError) as error:
                    # A rejected report must not be replayed forever from the
                    # checkpoint, and its invalid edits must not poison every
                    # subsequent fresh tester. Archive them for inspection,
                    # restore the guard baseline, and explain the rejection to
                    # the next tester.
                    rejected_paths, patch_path = self.reject_adversarial_edits(loop["stage_base"], error)
                    loop.update(
                        active=False,
                        response=None,
                        retry_stage_base=loop["stage_base"],
                        retry_rejection={"reason": str(error), "paths": rejected_paths, "patch": str(patch_path)},
                    )
                    self.save_stage(stage, loop)
                    self.record_stage_failure(stage, loop, str(error))
                    raise WorkerError(
                        f"Invalid adversarial test result: {error}. Rejected edits were restored; "
                        f"diagnostic patch: {patch_path}"
                    ) from error
                # A successfully parsed and validated report is a completed
                # analysis attempt; a prior failed attempt's error must not go
                # on masquerading as the stage's current verdict once a fresh
                # attempt actually completes, whatever that attempt concludes.
                loop.pop("review_error", None)
                self.file_stage_findings(stage, loop, stage.findings_to_file(report))
                loop.pop("retry_rejection", None)
                loop["excluded_suites"] = sorted(candidate_excluded)
                before = sum(r["exit_code"] != 0 for r in loop["results"])
                results = run_suites(
                    self.config.repo_dir,
                    [s for s in definition["suites"] if s.get("origin") in stage.blocking_origins
                     and s["id"] not in loop["excluded_suites"]],
                    require_suites=stage.require_suites,
                )
                completed = iso_timestamp()
                blocking = stage.blocking_findings(report)
                round_value = {
                    "stage": stage.slug,
                    "round_number": loop["round"], "fixer_provider": loop["fixer_provider"],
                    "fixer_model": loop["fixer_model"], "tester_provider": self.choice.name,
                    "tester_model": self.choice.model, "tests_added": added, "tests_modified": modified,
                    "tests_failing_before": before, "tests_failing_after": sum(r["exit_code"] != 0 for r in results),
                    "disputed": bool(loop["dispute"] or (loop.get("amendments") and report.get("dispute_resolution"))), "dispute_resolution": report.get("dispute_resolution", ""),
                    "started_at": loop["round_started"], "completed_at": completed,
                    "duration_seconds": max(0, (dt.datetime.fromisoformat(completed) - dt.datetime.fromisoformat(loop["round_started"])).total_seconds()),
                }
                stage.record_round(self, loop, report, round_value, blocking, results)
                # Do not append until the phase is durably complete; repeated
                # reporting of this round is an upsert in the existing store.
                self.history.adversarial_round(round_value)
                loop["rounds"] = [r for r in loop["rounds"] if r["round_number"] != loop["round"]] + [round_value]
                loop["tests_added"] = sum(r["tests_added"] for r in loop["rounds"])
                loop["tests_modified"] = sum(r["tests_modified"] for r in loop["rounds"])
                loop["results"] = results
                # Same as prepare_adversarial_framework: force-stage since a
                # plain `git add` refuses an app-excluded, untracked path.
                self.git("add", "--force", "--", DEFINITION)
                if not round_value["tests_failing_after"] and not blocking:
                    loop["outcome"] = "clean_first_pass" if loop["round"] == 0 else "resolved_after_n"
                elif loop["round"] >= MAX_ROUNDS:
                    loop["outcome"] = "cap_hit"
            completion = self.commit_completed_work(loop["stage_base"])
            self.validate_new_commit_messages(loop["stage_base"], completion)
            loop["completion"] = completion
            if loop["phase"] == "fix":
                log(stage.fix_applied_log(self.issue.number, loop))
            usage = self.provider_usage(self.choice.key)
            if usage.remaining_percent is not None:
                loop["capacity_end"][self.choice.name] = usage.remaining_percent
            if loop["outcome"]:
                loop["phase"] = "done"
            elif loop["phase"] == "fix":
                loop["phase"] = "test"
            else:
                loop["phase"] = "fix"
                loop["round"] += 1
                loop["round_started"] = iso_timestamp()
            loop.update(active=False, response=None)
            self.save_stage(stage, loop)
        consumed = [max(0, start - loop["capacity_end"][name]) for name, start in loop["capacity_start"].items()
                    if name in loop["capacity_end"] and name in loop["capacity_used"]]
        loop["consumed_percent"] = sum(consumed) if consumed else None
        loop["status"] = stage.review_status(loop)
        self.save_stage(stage, loop)
        self.history.update(iso_timestamp(), **stage.history_fields(loop),
                            capacity_consumed_percent=self.adversarial_capacity_consumed())
        log(f"{stage.label} for issue #{self.issue.number}: review completed with status {loop['status']}.")
        return None

    def adversarial_capacity_consumed(self) -> float | None:
        state = self.read_state()
        values = [state[stage.key]["consumed_percent"] for stage in self.adversarial_stages()
                  if state.get(stage.key) and state[stage.key].get("consumed_percent") is not None]
        return sum(values) if values else None

    def record_stage_failure(self, stage: AdversarialStage, loop: dict[str, Any], reason: str) -> None:
        """A review that could not execute must never read as a clean pass."""
        from ai_execution_history import sanitize_text
        from swarm_issue_worker import iso_timestamp, log
        if not stage.reports_failures:
            return
        loop["status"] = "FAILED"
        loop["review_error"] = reason
        self.save_stage(stage, loop)
        self.history.update(iso_timestamp(), **stage.failure_history_fields(reason))
        # The persisted history column is sanitized by history.update itself;
        # the operator log is a separate surface and must not repeat a
        # credential a setup/auth error's exception message happened to quote.
        log(f"{stage.label} for issue #{self.issue.number}: review failed — {sanitize_text(reason)}")

    def run_adversarial_pipeline(self) -> int:
        """Run every enabled adversarial stage in order, then deliver once."""
        from swarm_issue_worker import ISSUE_COMPLETED_EXIT_CODE, ProviderChoice, WorkerError
        stages = self.adversarial_stages()
        previous = None
        for stage in stages:
            loop = self.read_stage(stage)
            if loop is None:
                if previous is None:
                    continue
                self.initialize_stage(stage, previous["completion"], previous["implementation_output"],
                                      previous["delivery_choice"], previous.get("excluded_suites"))
                loop = self.read_stage(stage)
            early = self.run_adversarial_stage(stage)
            if early is not None:
                return early
            previous = self.read_stage(stage)
        finished = [(stage, loop) for stage, loop in
                    ((stage, self.read_stage(stage)) for stage in stages) if loop]
        if not finished:
            raise RuntimeError("No adversarial stage state to deliver")
        # Checked once, here, after every enabled stage has reached "done" —
        # not inside run_adversarial_stage, which runs once per stage on
        # every pipeline invocation including ones where that stage did no
        # new work this round. An already-finished earlier stage (e.g. UAT)
        # was tripping this same check on every retry because a *later*
        # stage (e.g. cybersecurity) was still mid-round with its own
        # uncommitted fix, which aborted the whole pipeline before the later
        # stage ever got a turn to resume and commit its own work.
        if self.worktree_status():
            raise WorkerError("Adversarial delivery has uncommitted changes")
        last = finished[-1][1]
        self.choice = ProviderChoice(**last["delivery_choice"])
        self.update_state_for_choice(self.choice)
        # A deadlock in any stage owns the completion summary; the first one
        # reached is the one a reader needs to act on.
        capped = next(((stage, loop) for stage, loop in finished
                       if loop.get("outcome") == "cap_hit"), None)
        if capped:
            stage, loop = capped
            # A cap hit is not a verified-clean pass, so it must not enter the
            # same automatic approve/merge/promote path a clean delivery does
            # (see issue-branch-delivery.md). finalize_issue still owns the
            # push/PR step, but with automation held it hands off to
            # trusted-author adjudication instead of reporting a misleading
            # "Completed".
            self.finalize_issue(last["completion"], stage.cap_hit_output(loop), allow_automation=False)
        else:
            self.finalize_issue(last["completion"], finished[0][1]["implementation_output"])
        return ISSUE_COMPLETED_EXIT_CODE
