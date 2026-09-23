"""Unit tests for the dependency-free YAML-subset loader (model_router_yaml).

Run with ``python3 -m unittest test_model_router_yaml`` from this directory
(not pytest — see test_swarm_issue_worker.py's module docstring for why).
"""

from __future__ import annotations

import unittest
from pathlib import Path

import model_router_yaml as yamlish


class ScalarTests(unittest.TestCase):
    def test_bare_scalars(self) -> None:
        data = yamlish.load(
            "\n".join(
                [
                    "a: 1",
                    "b: 1.5",
                    "c: -2",
                    "d: true",
                    "e: false",
                    "f: null",
                    "g: ~",
                    "h: plain text",
                ]
            )
        )
        self.assertEqual(
            data,
            {"a": 1, "b": 1.5, "c": -2, "d": True, "e": False, "f": None, "g": None, "h": "plain text"},
        )

    def test_quoted_strings_and_colons(self) -> None:
        data = yamlish.load('a: "hello: world"\nb: \'single: quoted\'')
        self.assertEqual(data, {"a": "hello: world", "b": "single: quoted"})

    def test_comments_are_stripped(self) -> None:
        data = yamlish.load("# leading comment\na: 1  # trailing comment\n# another\nb: 2\n")
        self.assertEqual(data, {"a": 1, "b": 2})

    def test_hash_inside_quotes_is_not_a_comment(self) -> None:
        data = yamlish.load('a: "not # a comment"')
        self.assertEqual(data, {"a": "not # a comment"})


class FlowTests(unittest.TestCase):
    def test_flow_sequence(self) -> None:
        data = yamlish.load("a: [one, two, three]")
        self.assertEqual(data, {"a": ["one", "two", "three"]})

    def test_flow_mapping(self) -> None:
        data = yamlish.load("a: {x: 1, y: null, z: HEURISTIC}")
        self.assertEqual(data, {"a": {"x": 1, "y": None, "z": "HEURISTIC"}})

    def test_empty_flow_sequence(self) -> None:
        self.assertEqual(yamlish.load("a: []"), {"a": []})


class BlockTests(unittest.TestCase):
    def test_nested_mapping(self) -> None:
        text = "\n".join(
            [
                "top:",
                "  inner:",
                "    leaf: 1",
                "  sibling: 2",
            ]
        )
        self.assertEqual(yamlish.load(text), {"top": {"inner": {"leaf": 1}, "sibling": 2}})

    def test_block_sequence_of_scalars(self) -> None:
        text = "items:\n  - one\n  - two\n  - three\n"
        self.assertEqual(yamlish.load(text), {"items": ["one", "two", "three"]})

    def test_block_sequence_of_mappings(self) -> None:
        text = "\n".join(
            [
                "models:",
                "  - name: a",
                "    cost: 1",
                "  - name: b",
                "    cost: 2",
            ]
        )
        self.assertEqual(
            yamlish.load(text),
            {"models": [{"name": "a", "cost": 1}, {"name": "b", "cost": 2}]},
        )

    def test_block_sequence_of_mappings_with_nested_flow(self) -> None:
        text = "\n".join(
            [
                "models:",
                "  - name: a",
                "    benchmarks:",
                "      low: {score: 1, quality: HEURISTIC}",
            ]
        )
        self.assertEqual(
            yamlish.load(text),
            {"models": [{"name": "a", "benchmarks": {"low": {"score": 1, "quality": "HEURISTIC"}}}]},
        )


class RealConfigTests(unittest.TestCase):
    """The loader must round-trip the actual shipped config files."""

    def test_models_yaml_parses(self) -> None:
        path = Path(__file__).resolve().parent.parent / "skills" / "model-router" / "models.yaml"
        data = yamlish.load(path.read_text(encoding="utf-8"))
        self.assertIsInstance(data, dict)
        self.assertIsInstance(data.get("models"), list)
        self.assertGreater(len(data["models"]), 0)
        for entry in data["models"]:
            self.assertIn("provider", entry)
            self.assertIn("model", entry)

    def test_routing_rules_yaml_parses(self) -> None:
        path = Path(__file__).resolve().parent.parent / "skills" / "model-router" / "routing-rules.yaml"
        data = yamlish.load(path.read_text(encoding="utf-8"))
        self.assertIsInstance(data, dict)
        self.assertIn("complexity_bands", data)
        self.assertIn("weights", data)
        self.assertIn("cost_consideration_off", data["weights"])
        self.assertIn("cost_consideration_on", data["weights"])
        self.assertIn("minimum_expected_success", data)
        self.assertIn("cost_optimization_quality_tolerance", data)


if __name__ == "__main__":
    unittest.main()
