"""Issue #381 acceptance: what the patch ships and what its reports say.

Oracle (from the issue and the repository's own conventions):

* ``ui/`` is Tauri's ``frontendDist`` (see ``.claude/rules/ui-design-system.md``):
  every file on disk there is bundled into the desktop app. A tool-generated
  backup of a source file (``usage-cost.js-E`` from ``sed -i -E`` on macOS, an
  ``.orig``/``.rej`` from a patch) is never an intended deliverable and ships a
  stale second copy of a module.
* Section 7 requires documentation that describes session lifecycle, telemetry
  and failure recovery. A document must not promise a section that does not
  exist ("see ... below"), and the relative links of the documents the change
  touches must resolve.
* Section 4 requires reported costs, estimated savings and *unavailable*
  statistics to be clearly distinguishable. A real, non-zero measurement that is
  printed as ``$0.00`` is indistinguishable from a measured zero (the Feedback
  view already keeps four decimals for exactly this reason), while a measured
  zero must stay ``$0.00``.
"""
from __future__ import annotations

import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ISSUE_WORKER_DIR = ROOT / "issue_worker"
if str(ISSUE_WORKER_DIR) not in sys.path:
    sys.path.insert(0, str(ISSUE_WORKER_DIR))

from token_usage import render_ai_usage_markdown  # noqa: E402

BACKUP_SUFFIXES = ("-E", ".orig", ".rej", ".bak", ".swp", ".swo", "~")
# Where a stray backup would be committed or bundled with the product.
SHIPPED_ROOTS = ("ui", "issue_worker", "docs", ".claude", "skills", "src", "scripts")
DOCUMENTS_TOUCHED_BY_381 = (
    "README.md",
    "AGENTS.md",
    "docs/prompt-caching.md",
    "docs/model-pricing.md",
    ".claude/rules/prompt-caching.md",
    ".claude/skills/swarm-automation-dev/SKILL.md",
)


def git_files() -> set[str]:
    output = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        check=True, capture_output=True, text=True).stdout
    return {name for name in output.split("\0") if name}


def is_backup(name: str) -> bool:
    return name.endswith(BACKUP_SUFFIXES)


class ShippedFilesTests(unittest.TestCase):
    def test_no_tool_generated_backup_is_bundled_with_the_frontend(self) -> None:
        # frontendDist is a directory: judge the files on disk, tracked or not.
        stray = sorted(
            str(path.relative_to(ROOT)) for path in (ROOT / "ui").rglob("*")
            if path.is_file() and is_backup(path.name)
        )
        self.assertEqual(stray, [], f"backup/artefact files would ship inside the app bundle: {stray}")

    def test_no_backup_artefact_is_committed_anywhere_the_product_is_built_from(self) -> None:
        stray = sorted(
            name for name in git_files()
            if name.split("/")[0] in SHIPPED_ROOTS and is_backup(name.rsplit("/", 1)[-1])
        )
        self.assertEqual(stray, [], f"backup/artefact files are tracked or untracked-and-unignored: {stray}")


def headings(markdown: str) -> list[str]:
    return [line.lstrip("#").strip().lower() for line in markdown.splitlines() if re.match(r"#{1,6}\s", line)]


class DocumentationTests(unittest.TestCase):
    def test_a_see_below_reference_points_at_a_section_that_exists(self) -> None:
        for relative in DOCUMENTS_TOUCHED_BY_381:
            text = (ROOT / relative).read_text(encoding="utf-8")
            flat = re.sub(r"\s+", " ", text)
            for match in re.finditer(r"(?i)\bsee the ([a-z][a-z' -]{2,60}?) below", flat):
                subject = match.group(1).strip().lower()
                key = subject.split()[-1]
                with self.subTest(document=relative, reference=subject):
                    self.assertTrue(
                        any(key in heading for heading in headings(text)),
                        f"{relative} says 'see the {subject} below' but has no heading mentioning '{key}'",
                    )

    def test_relative_links_in_the_documents_this_change_touches_resolve(self) -> None:
        broken = []
        for relative in DOCUMENTS_TOUCHED_BY_381:
            document = ROOT / relative
            for target in re.findall(r"\]\(([^)\s]+)\)", document.read_text(encoding="utf-8")):
                if re.match(r"[a-z][a-z0-9+.-]*:|#|//", target, re.I):
                    continue  # absolute URL, mail link or in-page anchor
                path = (document.parent / target.split("#", 1)[0]).resolve()
                if not path.exists():
                    broken.append(f"{relative} -> {target}")
        self.assertEqual(broken, [], f"unresolvable relative links: {broken}")

    def test_the_benchmark_the_docs_describe_exists_and_is_documented(self) -> None:
        doc = (ROOT / "docs" / "prompt-caching.md").read_text(encoding="utf-8")
        self.assertIn("benchmark_prompt_caching.py", doc)
        self.assertTrue((ROOT / "issue_worker" / "benchmark_prompt_caching.py").is_file())

    def test_router_skill_documents_the_cache_evidence_it_now_consumes(self) -> None:
        # .claude/rules/jev-decision-engine.md ("Source of truth") and
        # repository-complexity-scoring.md require a change to what the router
        # consumes to be reflected in skills/model-router/SKILL.md, and section 7
        # of the issue lists the Claude skills among the documents to update.
        # build_router_prompt now sends measured cache evidence to the router and
        # saved-attempt cost comparisons apply it, so the routing skill must say so.
        skill = (ROOT / "skills" / "model-router" / "SKILL.md").read_text(encoding="utf-8")
        flat = re.sub(r"\s+", " ", skill)
        self.assertRegex(
            flat, r"(?is)\bcache\b[^.]{0,240}evidence|evidence[^.]{0,240}\bcache\b",
            "skills/model-router/SKILL.md does not mention the measured native-cache evidence the router uses")
        self.assertRegex(
            flat, r"(?is)(\bcache\b[^.]{0,400}(capab|safety|independen|gate))|((capab|safety|independen|gate)[^.]{0,400}\bcache\b)",
            "the routing skill must state that cache evidence never overrides capability, safety or independence")


def event(**overrides) -> dict:
    base = dict(
        id="a", sequence=1, agent_type="primary", prompt_type="initial", provider="Claude",
        model="claude-sonnet-5", reasoning_effort="high", attempt_number=1, input_tokens=10_000,
        output_tokens=1_000, reasoning_tokens=None, cached_input_tokens=2_000, cache_read_tokens=2_000,
        cache_write_tokens=None, total_tokens=13_000, estimated_cost=0.05, currency="USD",
        started_at="2026-03-10T10:00:00+00:00", completed_at="2026-03-10T10:05:00+00:00",
        duration_ms=300_000, success=True, error_type="", pricing_status="priced",
        pricing_version="2026-09-28", pricing_rate_id="claude/claude-sonnet-5@2026-01-01",
        pricing_source="https://www.anthropic.com/pricing", input_rate_per_million=3.0,
        cached_input_rate_per_million=0.3, cache_write_rate_per_million=3.75,
        output_rate_per_million=15.0, cache_input_tokens=2_500, session_reused=True,
    )
    base.update(overrides)
    return base


def cache_lines(markdown: str) -> dict[str, str]:
    lines = {}
    for line in markdown.splitlines():
        lowered = line.lower()
        if "provider-reported cost" in lowered:
            lines["reported"] = line
        elif "cache savings" in lowered and "not realized" not in lowered:
            lines["savings"] = line
    return lines


class ReportAmountTests(unittest.TestCase):
    ZERO_DOLLARS = re.compile(r"\$0\.00(?!\d)")

    def test_a_measured_non_zero_sub_cent_amount_is_not_printed_as_zero(self) -> None:
        for reported, savings in ((0.0042, 0.0031), (0.0001, 0.0009), (0.0042, -0.0031)):
            with self.subTest(reported=reported, savings=savings):
                lines = cache_lines(render_ai_usage_markdown(
                    [event(reported_cost=reported, cache_savings_estimate=savings)]))
                self.assertEqual(set(lines), {"reported", "savings"})
                self.assertNotRegex(lines["reported"], self.ZERO_DOLLARS,
                                    f"{reported} reads as a measured zero: {lines['reported']!r}")
                self.assertNotRegex(lines["savings"], self.ZERO_DOLLARS,
                                    f"{savings} reads as a measured zero: {lines['savings']!r}")

    def test_a_measured_zero_stays_zero_and_a_missing_value_stays_unavailable(self) -> None:
        zero = cache_lines(render_ai_usage_markdown([event(reported_cost=0.0, cache_savings_estimate=0.0)]))
        self.assertRegex(zero["reported"], self.ZERO_DOLLARS)
        self.assertRegex(zero["savings"], self.ZERO_DOLLARS)
        missing = cache_lines(render_ai_usage_markdown([event()]))
        for line in missing.values():
            self.assertNotRegex(line, r"\$\d", f"an unreported value must read —: {line!r}")
            self.assertIn("—", line)

    def test_ordinary_amounts_keep_their_conventional_form(self) -> None:
        lines = cache_lines(render_ai_usage_markdown(
            [event(reported_cost=12.5, cache_savings_estimate=-1.5)]))
        self.assertIn("$12.50", lines["reported"])
        self.assertIn("1.50", lines["savings"])
        self.assertNotRegex(lines["savings"], r"\$-")


if __name__ == "__main__":
    unittest.main()
