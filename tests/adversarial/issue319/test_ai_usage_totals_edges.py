"""Issue #319: extra edge cases for missing-vs-zero in the AI Usage totals."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "issue_worker"))
from token_usage import render_ai_usage_markdown  # noqa: E402

TOKEN_FIELDS = ("input_tokens", "output_tokens", "cached_input_tokens", "reasoning_tokens", "total_tokens")


def ev(seq=1, **kw):
    base = dict(
        id=f"e{seq}", sequence=seq, agent_type="primary", prompt_type="initial", provider="Claude",
        model="m", reasoning_effort="medium", attempt_number=1, estimated_cost=None,
        currency="USD", started_at=None, completed_at=None, duration_ms=None, success=True, error_type=None,
    )
    base.update({f: None for f in TOKEN_FIELDS})
    base.update(kw)
    return base


def totals_lines(events):
    text = render_ai_usage_markdown(events).split("**AI Usage Totals**", 1)[1]
    return [line.strip() for line in text.splitlines() if line.strip()]


class TotalsEdgeCases(unittest.TestCase):
    def test_thousands_separator_on_summed_totals(self):
        lines = totals_lines([ev(1, input_tokens=1_000_000), ev(2, input_tokens=234_567)])
        self.assertIn("Input: 1,234,567", lines)

    def test_missing_row_does_not_zero_out_other_metrics(self):
        lines = totals_lines([ev(1, input_tokens=7), ev(2, output_tokens=3)])
        self.assertIn("Input: 7", lines)
        self.assertIn("Output: 3", lines)
        self.assertIn("Total Tokens: —", lines)

    def test_zero_then_missing_stays_zero(self):
        lines = totals_lines([ev(1, reasoning_tokens=0), ev(2)])
        self.assertIn("Reasoning: 0", lines)
        self.assertIn("Cached Input: —", lines)

    def test_missing_then_zero_stays_zero(self):
        lines = totals_lines([ev(1), ev(2, reasoning_tokens=0)])
        self.assertIn("Reasoning: 0", lines)

    def test_genuine_zero_cost_is_not_a_dash(self):
        lines = totals_lines([ev(1, estimated_cost=0.0)])
        self.assertIn("Estimated Cost: $0.00", lines)

    def test_missing_cost_is_a_dash(self):
        lines = totals_lines([ev(1, estimated_cost=None)])
        self.assertIn("Estimated Cost: —", lines)

    def test_invocation_count_counts_rows_with_no_usage(self):
        lines = totals_lines([ev(1), ev(2), ev(3)])
        self.assertIn("AI Invocations: 3", lines)

    def test_no_zero_rendered_for_all_missing(self):
        text = render_ai_usage_markdown([ev(1)]).split("**AI Usage Totals**", 1)[1]
        for label in ("Input", "Cached Input", "Reasoning", "Output", "Total Tokens"):
            self.assertNotIn(f"{label}: 0", text)

    def test_empty_input_renders_nothing(self):
        self.assertEqual(render_ai_usage_markdown([]), "")


if __name__ == "__main__":
    unittest.main()
