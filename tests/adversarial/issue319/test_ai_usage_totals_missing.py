"""Issue #319: the GitHub AI Usage totals block must keep missing != zero."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "issue_worker"))
from token_usage import render_ai_usage_markdown  # noqa: E402

FIELDS = ("input_tokens", "output_tokens", "cached_input_tokens", "reasoning_tokens", "total_tokens")
LABELS = {
    "input_tokens": "Input",
    "cached_input_tokens": "Cached Input",
    "reasoning_tokens": "Reasoning",
    "output_tokens": "Output",
    "total_tokens": "Total Tokens",
}


def ev(**kw):
    base = dict(
        id="x", sequence=1, agent_type="primary", prompt_type="initial", provider="Claude",
        model="m", reasoning_effort="medium", attempt_number=1, estimated_cost=None,
        currency="USD", started_at=None, completed_at=None, duration_ms=None, success=True, error_type=None,
    )
    base.update({f: None for f in FIELDS})
    base.update(kw)
    return base


def totals(events):
    text = render_ai_usage_markdown(events).split("**AI Usage Totals**", 1)[1]
    out = {}
    for line in text.splitlines():
        if line.startswith("**") and ":** " in line:
            k, v = line[2:].split(":** ", 1)
            out[k] = v.strip()
    return out


class TotalsMissingVsZero(unittest.TestCase):
    def test_issue_reproduction(self):
        t = totals([ev(output_tokens=0)])
        self.assertEqual(t["Output"], "0")
        for f in FIELDS:
            if f != "output_tokens":
                self.assertEqual(t[LABELS[f]], "—", f)

    def test_all_missing_every_metric_dash(self):
        t = totals([ev(), ev(id="y", sequence=2)])
        for f in FIELDS:
            self.assertEqual(t[LABELS[f]], "—", f)
        self.assertEqual(t["AI Invocations"], "2")

    def test_all_zero_stays_zero(self):
        t = totals([ev(**{f: 0 for f in FIELDS}), ev(id="y", **{f: 0 for f in FIELDS})])
        for f in FIELDS:
            self.assertEqual(t[LABELS[f]], "0", f)

    def test_mixed_rows_sum_known_only_with_separators(self):
        t = totals([
            ev(input_tokens=1000, total_tokens=1000),
            ev(id="y", input_tokens=None, output_tokens=None, total_tokens=234),
            ev(id="z", input_tokens=500, output_tokens=0),
        ])
        self.assertEqual(t["Input"], "1,500")
        self.assertEqual(t["Output"], "0")
        self.assertEqual(t["Total Tokens"], "1,234")
        self.assertEqual(t["Cached Input"], "—")

    def test_metrics_independent_per_field(self):
        t = totals([ev(cached_input_tokens=7), ev(id="y", reasoning_tokens=3)])
        self.assertEqual(t["Cached Input"], "7")
        self.assertEqual(t["Reasoning"], "3")
        self.assertEqual(t["Input"], "—")

    def test_cost_missing_and_zero(self):
        self.assertEqual(totals([ev()])["Estimated Cost"], "—")
        self.assertEqual(totals([ev(estimated_cost=0.0)])["Estimated Cost"], "$0.00")

    def test_row_cells_match_totals_semantics(self):
        md = render_ai_usage_markdown([ev(output_tokens=0)])
        row = [l for l in md.splitlines() if l.startswith("| 1 ")][0]
        self.assertIn("| 0 |", row)
        self.assertIn("—", row)


if __name__ == "__main__":
    unittest.main()
