"""Durable, pre-delivery adversarial UAT using the worker's full coding CLIs.

Round zero is the independent assessment of the normal implementation. Each
of the six subsequent rounds is exactly one fix plus one fresh assessment.
Only suite exit codes decide success; an agent's self-reported pass never does.
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
TEST_ROOT = "tests/adversarial/"
DEFINITION = ".swarm/tests.json"
RESULT_MARKER = "SWARM_ADVERSARIAL_RESULT:"
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


def test_path(path: str) -> bool:
    return path.startswith(TEST_ROOT) and "__pycache__" not in path.split("/") and not path.endswith(".pyc")


def adversarial_activity(loop: dict[str, Any]) -> str:
    """What the log should say a provider is doing right now.

    Adversarial UAT can route implementation, fixing and testing to
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


def result_payload(output: str) -> dict[str, Any]:
    all_lines = output.splitlines()
    marker_indices = [i for i, line in enumerate(all_lines) if line.strip().startswith(RESULT_MARKER)]
    if len(marker_indices) != 1:
        raise ValueError("Tester must return exactly one SWARM_ADVERSARIAL_RESULT JSON line")
    index = marker_indices[0]
    after_marker = all_lines[index].strip()[len(RESULT_MARKER):].strip()
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
        raise ValueError(f"Could not parse SWARM_ADVERSARIAL_RESULT JSON: {error}") from error
    if not isinstance(value, dict) or not isinstance(value.get("out_of_scope", []), list):
        raise ValueError("Invalid adversarial result")
    for finding in value.get("out_of_scope", []):
        if not isinstance(finding, dict) or not all(isinstance(finding.get(k), str) and finding[k].strip()
                                                   for k in ("title", "body")):
            raise ValueError("Out-of-scope findings require title and body evidence")
        if not isinstance(finding.get("suite_ids", []), list) or not all(isinstance(v, str) for v in finding.get("suite_ids", [])):
            raise ValueError("Out-of-scope suite_ids must be an array of suite IDs")
    if not isinstance(value.get("dispute_resolution", ""), str):
        raise ValueError("Invalid dispute resolution")
    return value


def run_suites(root: Path, suites: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Direct argv, bounded runtime/output, no shell or AI-reported verdicts."""
    results = []
    if not suites:
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


class AdversarialUatMixin:
    """Worker integration kept separate from delivery and lifecycle mechanics."""

    def save_adversarial(self, loop: dict[str, Any]) -> None:
        self.update_state(adversarial=loop)

    def initialize_adversarial(self, completion: str, output: str) -> None:
        from swarm_issue_worker import iso_timestamp
        import dataclasses
        self.save_adversarial({
            "phase": "test", "round": 0, "completion": completion,
            "amendments": list(self.issue.followup_comments),
            "implementation_output": output, "implementer": self.choice.name,
            "delivery_choice": dataclasses.asdict(self.choice), "capacity_used": [self.choice.name],
            "fixer_provider": self.choice.name, "fixer_model": self.choice.model,
            "round_started": iso_timestamp(), "outcome": "", "results": [],
            "tests_added": 0, "tests_modified": 0, "dispute": self.read_state().get("adversarial_initial_dispute", ""), "rounds": [],
            "capacity_start": ({self.choice.name: self.read_state()["usage_at_start"]["remaining_percent"]}
                               if (self.read_state().get("usage_at_start") or {}).get("remaining_percent") is not None else {}),
            "capacity_end": {}, "filed_findings": [], "filed_finding_details": [], "excluded_suites": [],
        })

    def refresh_adversarial_requirements(self) -> None:
        """Trusted comments may clarify the spec during a quota pause."""
        state = self.read_state()
        loop = state["adversarial"]
        comments = self.load_resume_comments(self.issue.number, int(state.get("session_comment_id", 0)))
        known = {c["id"] for c in loop.get("amendments", [])}
        loop.setdefault("amendments", []).extend(c for c in comments if c["id"] not in known)
        if comments:
            self.update_state(session_comment_id=int(comments[-1]["id"]))
            # A previously returned report cannot establish success against a
            # requirement received later. Reassess with a fresh tester first.
            if loop["phase"] in {"done", "test"}:
                loop.update(phase="test", outcome="", active=False, response=None)
                loop.pop("delivery", None)
            self.save_adversarial(loop)

    def choose_adversarial_provider(self, loop: dict[str, Any]):
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
            self.save_adversarial(loop)
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
                        title=f"Adversarial UAT {loop['phase']}: {self.issue.title}",
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

    def adversarial_prompt(self, loop: dict[str, Any]) -> str:
        base = str(self.read_state()["base_sha"])
        # No completion summary, session transcript, or implementer reasoning
        # enters the tester context. It sees the spec, diff and repo conventions.
        diff = self.git("diff", "--no-ext-diff", base, "--", ".", ":(exclude)tests/adversarial")
        amendments = "\n".join(str(c.get("body") or "") for c in loop.get("amendments", []))
        rejected = loop.get("retry_rejection") or {}
        rejection_guidance = ""
        if rejected:
            rejection_guidance = (
                "\nA prior tester result was rejected and its edits were rolled back. Do not repeat it.\n"
                f"Rejection: {rejected.get('reason', 'invalid tester result')}\n"
                f"Rejected paths: {json.dumps(rejected.get('paths', []))}\n"
            )
        common = (
            f"Issue #{self.issue.number}: {self.issue.title}\n\n{self.issue.body}\n\n"
            f"Trusted issue amendments (authoritative clarifications):\n{amendments or 'None.'}\n\n"
            f"Repository: {self.config.repo_dir}\nRemain on {self.expected_branch()}. "
            "Read repository conventions (AGENTS.md, CLAUDE.md, .claude/rules and existing test patterns). "
            "Do not inspect prior agent transcripts, session logs, or completion summaries. "
            "Do not commit, push, open PRs, post comments, or file issues; the worker handles delivery. "
            "Run checks in the foreground. Do not edit VERSION.\n" + rejection_guidance +
            "\nResulting patch:\n" + diff + "\n"
        )
        if loop["phase"] == "fix":
            return (
                "You are the implementer in a bounded adversarial fix/test cycle. Fix failures "
                "within the issue's scope. Do not change, delete, skip, disable, or retire any "
                "test under tests/adversarial/ or any part of .swarm/tests.json. "
                "If an expectation is wrong, state the specific test and issue/spec evidence "
                "on a line beginning SWARM_TEST_DISPUTE:; a fresh independent tester will decide.\n" +
                common + "\nSpecific suite failures:\n" + json.dumps(loop["results"], indent=2)
            )
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
            "suite_ids array; use separate suites for in-scope and out-of-scope assertions.\n"
            + common + "\nFramework scaffold plan (apply autonomously, no sign-off):\n" +
            json.dumps(loop.get("bootstrap", {}), indent=2) +
            "\nOnly tests/adversarial/, .swarm/tests.json and necessary test framework manifests "
            "may be changed. Preserve the framework choice.\nDispute to adjudicate:\n" +
            (loop.get("dispute") or ("Adjudicate prior tests against these trusted amendments: " + amendments
                                      if amendments else "None. Keep earlier test expectations intact.")) +
            '\nReturn one final line: SWARM_ADVERSARIAL_RESULT: {"dispute_resolution":"",'
            '"out_of_scope":[{"title":"...","body":"reproduction, evidence and why outside scope","suite_ids":[]}]}\n'
        )

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
        untracked = set(self.git("ls-files", "--others", "--exclude-standard", "-z", "--", *paths,
                                 check=False).split("\0")) if paths else set()
        for path in sorted(untracked - {""}):
            result = subprocess.run(
                [self.config.git_bin, "-C", str(self.config.repo_dir), "diff", "--binary", "--no-index", "--", "/dev/null", path],
                text=True, capture_output=True, check=False,
            )
            patch += ("\n" if patch and not patch.endswith("\n") else "") + result.stdout
        patch_path.write_text(patch, encoding="utf-8")
        for path in paths:
            if self.git_ok("cat-file", "-e", f"{baseline}:{path}"):
                self.git("restore", f"--source={baseline}", "--staged", "--worktree", "--", path)
            else:
                self.git("rm", "-f", "--ignore-unmatch", "--", path, check=False)
                (self.config.repo_dir / path).unlink(missing_ok=True)
        return paths, patch_path

    def validate_adversarial_edits(self, loop: dict[str, Any], report: dict[str, Any]) -> tuple[int, int]:
        from swarm_issue_worker import WorkerError
        baseline = loop["stage_base"]
        if self.git("branch", "--show-current") != self.expected_branch():
            raise WorkerError("Adversarial role changed the issue branch")
        if not self.git_ok("merge-base", "--is-ancestor", baseline, "HEAD"):
            raise WorkerError("Adversarial role rewrote the issue history")
        paths = self.adversarial_changed_paths(baseline)
        if loop["phase"] == "fix":
            protected = {p for p in paths if test_path(p) or p == DEFINITION}
            if protected:
                # Preserve product repairs, but undo attempts to grade one's own
                # homework. A fresh tester, not the fixer, sees this as a dispute.
                for path in sorted(protected):
                    if self.git_ok("cat-file", "-e", f"{baseline}:{path}"):
                        self.git("restore", f"--source={baseline}", "--staged", "--worktree", "--", path)
                    else:
                        self.git("rm", "-f", "--ignore-unmatch", "--", path)
                        (self.config.repo_dir / path).unlink(missing_ok=True)
                loop["dispute"] += "\nFixer attempted to alter protected tests; worker restored them: " + ", ".join(sorted(protected))
            return 0, 0
        forbidden = paths - {p for p in paths if test_path(p) or p == DEFINITION or p in FRAMEWORK_FILES}
        if forbidden:
            raise WorkerError("Tester changed product files: " + ", ".join(sorted(forbidden)))
        before = json.loads(self.git("show", f"{baseline}:{DEFINITION}"))
        after = read_definition(self.config.repo_dir)
        non_adversarial = lambda v: {**v, "suites": [s for s in v["suites"] if s.get("origin") != "adversarial"]}
        if non_adversarial(before) != non_adversarial(after):
            raise WorkerError("Tester changed existing framework choice or non-adversarial suites/metadata")
        changed_tests = {p for p in paths if test_path(p)}
        prior = {p for p in changed_tests if self.git_ok("cat-file", "-e", f"{baseline}:{p}")}
        old_suites = [s for s in before["suites"] if s.get("origin") == "adversarial"]
        new_suites = [s for s in after["suites"] if s.get("origin") == "adversarial"]
        revised = prior or any(s not in new_suites for s in old_suites)
        if revised and not ((loop.get("dispute") or loop.get("amendments")) and report.get("dispute_resolution", "").strip()):
            raise WorkerError("Only a fresh tester adjudicating a dispute may revise or retire existing adversarial tests")
        if not any(test_path(p) for p in self.git("ls-files", "--cached", "--others", "--exclude-standard", "-z").split("\0")):
            raise WorkerError("Tester did not leave any adversarial test files")
        ids = [s.get("id") for s in after["suites"]]
        if len(ids) != len(set(ids)) or any(not str(s.get("id", "")).startswith("adversarial-") for s in new_suites):
            raise WorkerError("Adversarial suite IDs must be unique and visibly prefixed")
        for path in changed_tests | {DEFINITION}:
            if (self.config.repo_dir / path).is_symlink():
                raise WorkerError("Adversarial files may not be symlinks")
        return len(changed_tests - prior), len(prior)

    def prepare_adversarial_framework(self, loop: dict[str, Any]) -> None:
        from ai_test_assist import bootstrap
        from swarm_issue_worker import WorkerError, atomic_write_json
        if loop.get("bootstrap"):
            return
        spec = self.config.require_spec(self.choice.key)
        plan = bootstrap(str(self.config.repo_dir), spec.key, spec.bin or "", spec.model)
        if not plan.get("ok"):
            raise WorkerError(f"Test framework bootstrap failed: {plan.get('error')}")
        plan.pop("ok", None)
        definition = read_definition(self.config.repo_dir)
        definition.setdefault("adversarialBootstrap", plan)
        atomic_write_json(self.config.repo_dir / DEFINITION, definition)
        # .swarm drafts are normally app-owned/untracked (the app excludes
        # .swarm/ via info/exclude so its own scratch files never show up as
        # dirty); this file is intentionally a repository artifact, so it
        # must be force-staged or a plain `git add` fails outright on an
        # ignored, previously-untracked path.
        self.git("add", "--force", "--", DEFINITION)
        self.commit_completed_work(self.git("rev-parse", "HEAD"))
        loop["bootstrap"] = definition["adversarialBootstrap"]
        self.save_adversarial(loop)

    def file_adversarial_findings(self, loop: dict[str, Any], findings: list[dict[str, str]]) -> None:
        # Same labelled/assigned issue creation path as the Actions monitor,
        # with an idempotent marker so a retry after create cannot duplicate it.
        # Out-of-scope findings are specified not to block delivery of the
        # issue under test, so a GitHub failure here (rate limit, 403,
        # network) must never escape this loop: it would otherwise abort the
        # whole adversarial round from inside run_adversarial_delivery,
        # stalling the original issue over a problem that isn't its own.
        from swarm_issue_worker import WorkerError, github_issue_url_from_output, iso_timestamp, log
        for finding in findings:
            digest = hashlib.sha256(json.dumps(finding, sort_keys=True).encode()).hexdigest()[:20]
            marker = f"<!-- swarm-issue-worker:adversarial-finding:issue:{self.issue.number};id:{digest} -->"
            stored_title = finding["title"][:120]
            title = " ".join(stored_title.split())
            details = loop.setdefault("filed_finding_details", [])
            existing_detail = next((item for item in details if item.get("marker") == marker), None)
            if marker in loop["filed_findings"] and existing_detail and existing_detail.get("url"):
                log(f"Out-of-scope adversarial UAT finding already filed for #{self.issue.number}: {title}")
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
                log(f"Out-of-scope adversarial UAT finding already filed for #{self.issue.number} "
                    f"(reworded rediscovery of \"{reworded_duplicate.get('title')}\"): {reworded_duplicate.get('url')}")
                continue
            try:
                if marker in loop["filed_findings"]:
                    # A prior filing's `gh issue create` succeeded but its stdout
                    # didn't yield a usable URL, so this finding's stored detail
                    # is stuck at "". Re-check search: the issue is genuinely on
                    # GitHub and may now be discoverable, so recover its URL
                    # instead of leaving it permanently blank.
                    recheck = json.loads(self.github.gh([
                        "issue", "list", "--repo", self.config.github_repository, "--state", "all",
                        "--search", f'"{digest}" in:body', "--json", "body,url", "--limit", "100",
                    ], self.choice.key))
                    recovered = next((item for item in recheck if marker in item.get("body", "")), None)
                    url = github_issue_url_from_output(str(recovered.get("url", ""))) if recovered else ""
                    if existing_detail is not None:
                        existing_detail["url"] = url
                    else:
                        details.append({"marker": marker, "title": stored_title, "url": url})
                    log(f"Out-of-scope adversarial UAT finding already filed for #{self.issue.number}: {url or title}")
                    self.save_adversarial(loop)
                    self.history.update(
                        iso_timestamp(),
                        adversarial_filed_findings=[
                            {"title": item.get("title", ""), "url": item.get("url", "")}
                            for item in details
                        ],
                    )
                    continue
                existing = json.loads(self.github.gh([
                    "issue", "list", "--repo", self.config.github_repository, "--state", "all",
                    "--search", f'"{digest}" in:body', "--json", "body,url", "--limit", "100",
                ], self.choice.key))
                already_filed = next((item for item in existing if marker in item.get("body", "")), None)
                if already_filed:
                    url = github_issue_url_from_output(str(already_filed.get("url", "")))
                    log(f"Out-of-scope adversarial UAT finding already filed for #{self.issue.number}: {url or title}")
                else:
                    output = self.file_labelled_issue(finding["title"][:120],
                        f"{marker}\nFound while testing #{self.issue.number}; outside that delivery's scope.\n\n{finding['body']}",
                        (("bug", "d73a4a", "Something is not working"),
                         ("adversarial-uat", "5319e7", "Found by independent adversarial tests")), self.choice.key)
                    url = github_issue_url_from_output(output)
                    if url:
                        log(f"Filed out-of-scope adversarial UAT finding for #{self.issue.number}: {url}")
                    else:
                        log(f"GitHub did not return an issue URL for the out-of-scope adversarial UAT finding: {output.strip()!r}")
                loop["filed_findings"].append(marker)
                if not any(item.get("marker") == marker for item in details):
                    details.append({"marker": marker, "title": stored_title, "url": url})
                self.save_adversarial(loop)
                self.history.update(
                    iso_timestamp(),
                    adversarial_filed_findings=[
                        {"title": item.get("title", ""), "url": item.get("url", "")}
                        for item in details
                    ],
                )
            except WorkerError as error:
                # Leave `filed_findings`/`filed_finding_details` untouched so a
                # later round or a retried tester run gets another chance to
                # file this same finding, deduplicated by the same marker.
                log(f"Could not file out-of-scope adversarial UAT finding for #{self.issue.number}: {error}")

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
        if not self.in_progress_file.exists():
            return ""
        loop = self.read_state().get("adversarial", {})
        outcome = loop.get("outcome")
        if not outcome:
            return ""
        description = {"clean_first_pass": "clean first pass", "resolved_after_n": f"resolved after {loop['round']} rounds",
                       "cap_hit": f"still failing after {MAX_ROUNDS} rounds"}[outcome]
        return f"- Adversarial UAT: {description}, {loop['tests_added']} test files added.\n"

    def run_adversarial_delivery(self) -> int:
        from swarm_issue_worker import ISSUE_COMPLETED_EXIT_CODE, WorkerError, iso_timestamp, log
        loop = self.read_state()["adversarial"]
        while loop["phase"] != "done":
            if not loop.get("active"):
                choice = self.choose_adversarial_provider(loop)
                if choice is None:
                    return self.pause_adversarial()
                self.choice = choice
                self.update_state_for_choice(choice)
                self.ensure_bot_auth()
                self.prepare_adversarial_framework(loop)
                loop.update(active=True, stage_base=loop.pop("retry_stage_base", None) or self.git("rev-parse", "HEAD"), response=None)
                self.save_adversarial(loop)
                if loop["phase"] == "fix":
                    log(f"Adversarial UAT for issue #{self.issue.number}: starting fix/re-test round {loop['round']} of {MAX_ROUNDS}.")
                elif loop["round"] == 0:
                    log(f"Adversarial UAT for issue #{self.issue.number}: starting independent test run (round 0 of {MAX_ROUNDS}).")
                else:
                    log(f"Adversarial UAT for issue #{self.issue.number}: starting re-test for round {loop['round']} of {MAX_ROUNDS}.")
            if loop.get("response") is None:
                # Every tester phase starts with a new CLI session. Only an
                # interrupted *same phase* resumes its existing session.
                self.issue_images = []
                status = self.run_ai(self.adversarial_prompt(loop), activity=adversarial_activity(loop))
                if status != 0 or not self.ai_output_file.exists() or not self.ai_output_file.stat().st_size:
                    if self.ai_failure_is_quota():
                        return self.pause_adversarial()
                    raise WorkerError("Adversarial coding session failed; phase and worktree preserved")
                loop["response"] = self.ai_output_file.read_text(encoding="utf-8", errors="replace")
                self.save_adversarial(loop)
            output = loop["response"]
            if loop["phase"] == "fix":
                loop["dispute"] = "\n".join(line.partition(":")[2].strip() for line in output.splitlines()
                                             if line.startswith("SWARM_TEST_DISPUTE:"))
                self.validate_adversarial_edits(loop, {})
                loop["fixer_provider"], loop["fixer_model"] = self.choice.name, self.choice.model
                import dataclasses
                loop["delivery_choice"] = dataclasses.asdict(self.choice)
            else:
                try:
                    report = result_payload(output)
                    added, modified = self.validate_adversarial_edits(loop, report)
                    definition = read_definition(self.config.repo_dir)
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
                    self.save_adversarial(loop)
                    raise WorkerError(
                        f"Invalid adversarial test result: {error}. Rejected edits were restored; "
                        f"diagnostic patch: {patch_path}"
                    ) from error
                # The tester prompt explicitly permits citing a pre-existing,
                # non-adversarial suite's ID in an out-of-scope finding ("If
                # an existing suite fails for an unrelated reason ... report
                # its ID in that finding's suite_ids array"), so the set of
                # names this validates against must include every suite in
                # the definition, not just origin=='adversarial' ones —
                # otherwise a tester following that instruction to the
                # letter gets rejected for naming a real, known suite.
                known_suites = {s["id"] for s in definition["suites"]}
                excluded = {sid for finding in report.get("out_of_scope", []) for sid in finding.get("suite_ids", [])}
                if excluded - known_suites:
                    raise WorkerError("Out-of-scope findings named unknown suites")
                candidate_excluded = set(loop.get("excluded_suites", [])) | excluded
                runnable = [s for s in definition["suites"] if s.get("origin") == "adversarial" and s["id"] not in candidate_excluded]
                if not runnable:
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
                self.file_adversarial_findings(loop, report.get("out_of_scope", []))
                loop.pop("retry_rejection", None)
                loop["excluded_suites"] = sorted(candidate_excluded)
                before = sum(r["exit_code"] != 0 for r in loop["results"])
                results = run_suites(self.config.repo_dir, [s for s in definition["suites"] if s.get("origin") == "adversarial" and s["id"] not in loop["excluded_suites"]])
                completed = iso_timestamp()
                round_value = {
                    "round_number": loop["round"], "fixer_provider": loop["fixer_provider"],
                    "fixer_model": loop["fixer_model"], "tester_provider": self.choice.name,
                    "tester_model": self.choice.model, "tests_added": added, "tests_modified": modified,
                    "tests_failing_before": before, "tests_failing_after": sum(r["exit_code"] != 0 for r in results),
                    "disputed": bool(loop["dispute"] or (loop.get("amendments") and report.get("dispute_resolution"))), "dispute_resolution": report.get("dispute_resolution", ""),
                    "started_at": loop["round_started"], "completed_at": completed,
                    "duration_seconds": max(0, (dt.datetime.fromisoformat(completed) - dt.datetime.fromisoformat(loop["round_started"])).total_seconds()),
                }
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
                if not round_value["tests_failing_after"]:
                    loop["outcome"] = "clean_first_pass" if loop["round"] == 0 else "resolved_after_n"
                elif loop["round"] >= MAX_ROUNDS:
                    loop["outcome"] = "cap_hit"
            completion = self.commit_completed_work(loop["stage_base"])
            self.validate_new_commit_messages(loop["stage_base"], completion)
            loop["completion"] = completion
            if loop["phase"] == "fix":
                log(f"Adversarial UAT for issue #{self.issue.number}: fix applied in round {loop['round']} of {MAX_ROUNDS}.")
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
            self.save_adversarial(loop)
        consumed = [max(0, start - loop["capacity_end"][name]) for name, start in loop["capacity_start"].items()
                    if name in loop["capacity_end"] and name in loop["capacity_used"]]
        self.history.update(iso_timestamp(), adversarial_round_count=loop["round"], adversarial_outcome=loop["outcome"],
                            capacity_consumed_percent=sum(consumed) if consumed else None)
        if self.worktree_status():
            raise WorkerError("Adversarial delivery has uncommitted changes")
        from swarm_issue_worker import ProviderChoice
        self.choice = ProviderChoice(**loop["delivery_choice"])
        self.update_state_for_choice(self.choice)
        if loop["outcome"] == "cap_hit":
            delivery = tuple(loop["delivery"]) if loop.get("delivery") else None
            if delivery is None:
                delivery = self.deliver_pull_request(loop["completion"], allow_automation=False)
                loop["delivery"] = delivery
                self.save_adversarial(loop)
            failures = "\n".join(f"- {r['id']}: {r['output'][-2000:]}" for r in loop["results"] if r["exit_code"])
            output = (
                "## Action required\nReview the failing tests and implementation in " + delivery[0] +
                "; adjudicate the disputed expectation or specify the required fix. Reply with your decision "
                "in a new trusted-author comment to resume.\n\n## Summary\nAdversarial-test deadlock; "
                "the six fix/re-test rounds are exhausted. The branch and failing tests are published for review. "
                "This is a test/implementation disagreement, not a request for credentials.\n\n" +
                self.adversarial_summary_line() + "\n" + failures +
                "\n\n## Recommendations\nReview the linked PR against the issue's requirements. Automatic approval, "
                "merge and promotion were bypassed.\n\n## Step-by-step guide\n- Review the linked PR and reply with your adjudication.\n"
            )
            self.finalize_needs_input(output, delivery=delivery)
        else:
            self.finalize_issue(loop["completion"], loop["implementation_output"])
        return ISSUE_COMPLETED_EXIT_CODE
