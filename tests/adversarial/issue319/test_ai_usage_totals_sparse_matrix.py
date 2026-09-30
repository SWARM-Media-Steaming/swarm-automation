"""Issue #319 UAT: sparse token metrics aggregate independently per field."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "issue_worker"))
from token_usage import render_ai_usage_markdown  # noqa: E402


TOKEN_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "reasoning_tokens",
    "output_tokens",
    "total_tokens",
)
LABELS = {
    "input_tokens": "Input",
    "cached_input_tokens": "Cached Input",
    "reasoning_tokens": "Reasoning",
    "output_tokens": "Output",
    "total_tokens": "Total Tokens",
}


def invocation(sequence, **usage):
    event = {
        "id": f"matrix-{sequence}",
        "sequence": sequence,
        "agent_type": "primary",
        "prompt_type": "initial",
        "provider": "Claude",
        "model": "test-model",
        "reasoning_effort": "medium",
        "attempt_number": 1,
        "estimated_cost": None,
        "currency": "USD",
        "started_at": None,
        "completed_at": None,
        "duration_ms": None,
        "success": True,
        "error_type": None,
    }
    event.update({field: None for field in TOKEN_FIELDS})
    event.update(usage)
    return event


def total_values(markdown):
    totals = markdown.split("**AI Usage Totals**", 1)[1]
    values = {}
    for line in totals.splitlines():
        for field, label in LABELS.items():
            prefix = f"**{label}:** "
            if line.startswith(prefix):
                values[field] = line[len(prefix):].strip()
    return values


class SparseMetricMatrix(unittest.TestCase):
    def test_each_metric_sums_its_own_known_values_in_any_row_order(self):
        # Each column has two recorded values and two missing values. One
        # recorded zero proves that presence, not truthiness, drives summing.
        rows = [
            invocation(1, input_tokens=1_000, cached_input_tokens=0),
            invocation(2, input_tokens=None, cached_input_tokens=5, reasoning_tokens=2),
            invocation(3, reasoning_tokens=None, output_tokens=7, total_tokens=700),
            invocation(4, input_tokens=250, reasoning_tokens=3, output_tokens=None, total_tokens=0),
        ]
        expected = {
            "input_tokens": "1,250",
            "cached_input_tokens": "5",
            "reasoning_tokens": "5",
            "output_tokens": "7",
            "total_tokens": "700",
        }

        for ordered_rows in (rows, list(reversed(rows))):
            with self.subTest(order=[row["id"] for row in ordered_rows]):
                actual = total_values(render_ai_usage_markdown(iter(ordered_rows)))
                self.assertEqual(actual, expected)

    def test_metric_missing_everywhere_is_dash_while_another_zero_is_zero(self):
        rows = [invocation(1, output_tokens=0), invocation(2, output_tokens=None)]
        actual = total_values(render_ai_usage_markdown(rows))
        self.assertEqual(actual["output_tokens"], "0")
        for field in TOKEN_FIELDS:
            if field != "output_tokens":
                self.assertEqual(actual[field], "—", field)


if __name__ == "__main__":
    unittest.main()
