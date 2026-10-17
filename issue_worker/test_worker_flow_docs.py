"""docs/worker-flow.md and its generated page must stay true to the code.

The flow diagram is the reference for how the worker picks up, works, attacks and
delivers an issue. These checks fail when a documented limit, exit code or named
function drifts from the code, or when docs/worker-flow.html was not rebuilt.
When one fails, update the diagram (see .claude/rules/worker-flow-diagram.md).
"""
import importlib
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOC = ROOT / "docs" / "worker-flow.md"
PAGE = ROOT / "docs" / "worker-flow.html"
sys.path.insert(0, str(ROOT / "scripts"))

import build_worker_flow_html as builder  # noqa: E402

LIMIT_MODULES = ("adversarial_core", "finding_dedup", "prompt_budget", "swarm_issue_worker")


def worker_source() -> str:
    return "\n".join(path.read_text(encoding="utf-8") for path in sorted((ROOT / "issue_worker").glob("*.py"))
                     if not path.name.startswith("test_"))


def mermaid_blocks(text: str) -> list[str]:
    return re.findall(r"```mermaid\n(.*?)```", text, re.S)


class WorkerFlowDocTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = DOC.read_text(encoding="utf-8")
        cls.source = worker_source()
        cls.modules = [importlib.import_module(name) for name in LIMIT_MODULES]

    def constant(self, name):
        for module in self.modules:
            if hasattr(module, name):
                return getattr(module, name)
        self.fail(f"{name} is documented in docs/worker-flow.md but defined nowhere")

    def test_documented_limits_match_the_code(self):
        rows = re.findall(r"^\| `([A-Z][A-Z0-9_]+)` \| ([0-9.]+) \|", self.text, re.M)
        self.assertGreaterEqual(len(rows), 10, "the limits table is missing or malformed")
        for name, value in rows:
            self.assertEqual(float(self.constant(name)), float(value),
                             f"{name} is {self.constant(name)} in the code but {value} in docs/worker-flow.md")

    def test_documented_exit_codes_match_the_code(self):
        rows = re.findall(r"^\| (\d+) \| `([A-Z_]+_EXIT_CODE)`", self.text, re.M)
        self.assertGreaterEqual(len(rows), 6)
        for number, name in rows:
            self.assertEqual(self.constant(name), int(number), name)

    def test_named_functions_exist(self):
        names = set(re.findall(r"`(?:Worker\.)?([A-Za-z_]+)\(\)`", self.text))
        for block in mermaid_blocks(self.text):
            for label in re.findall(r'"([^"]*)"', block):
                first = label.split("<br/>")[0]
                if re.fullmatch(r"(Worker\.)?[a-z_]+", first) and ("_" in first or first.startswith("Worker.")):
                    names.add(first.removeprefix("Worker."))
        table = re.search(r"\| Transition \| Function \|.*?\n\n", self.text, re.S)
        self.assertIsNotNone(table, "the comments table is missing")
        names |= set(re.findall(r"^\| [^|]+ \| `([a-z_]+)` \|", table.group(0), re.M))
        self.assertGreater(len(names), 40)
        missing = sorted(n for n in names if not re.search(rf"def {re.escape(n)}\b", self.source))
        self.assertEqual(missing, [], f"docs/worker-flow.md names functions that no longer exist: {missing}")

    def test_diagrams_are_well_formed(self):
        self.assertEqual(self.text.count("```") % 2, 0)
        blocks = mermaid_blocks(self.text)
        self.assertGreaterEqual(len(blocks), 8)
        for block in blocks:
            self.assertRegex(block, r"^flowchart (TD|LR)\n")
            self.assertNotRegex(block, r"(?m)^\s*end\s*$", "a bare 'end' breaks Mermaid")

    def test_contents_links_point_at_real_sections(self):
        links = re.findall(r"^\d+\. \[[^\]]+\]\(#([^)]+)\)", self.text, re.M)
        headings = [builder.slug(h) for h in re.findall(r"^## (.+)$", self.text, re.M)]
        self.assertGreaterEqual(len(links), 10)
        self.assertEqual(links, headings)

    def test_visual_page_is_rebuilt_from_the_markdown(self):
        self.assertTrue(PAGE.exists(), "run python3 scripts/build_worker_flow_html.py")
        self.assertEqual(PAGE.read_text(encoding="utf-8"), builder.build_full(self.text),
                         "docs/worker-flow.html is stale; run python3 scripts/build_worker_flow_html.py")

    def test_pages_landing_page_is_rebuilt_and_jekyll_is_off(self):
        index = ROOT / "docs" / "index.html"
        self.assertTrue(index.exists(), "run python3 scripts/build_worker_flow_html.py")
        self.assertEqual(index.read_text(encoding="utf-8"), builder.build_index(),
                         "docs/index.html is stale; run python3 scripts/build_worker_flow_html.py")
        self.assertIn('href="worker-flow.html"', index.read_text(encoding="utf-8"))
        self.assertTrue((ROOT / "docs" / ".nojekyll").exists())

    def test_visual_page_carries_every_diagram(self):
        page = PAGE.read_text(encoding="utf-8")
        self.assertEqual(page.count('<pre class="mermaid">'), len(mermaid_blocks(self.text)))


if __name__ == "__main__":
    unittest.main()
