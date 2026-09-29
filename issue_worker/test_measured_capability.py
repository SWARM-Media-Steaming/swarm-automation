"""Measured Intelligence Index drives capability rank; the latest release wins safely."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import available_models
import dynamic_router
import model_calibration as calib
import model_router
from test_model_calibration import _entry


def _row(model: str, index: float | None, provider: str = "anthropic") -> dict:
    evaluations = {} if index is None else {"artificial_analysis_intelligence_index": index}
    return {"provider": provider, "model": model, "source_id": model, "evaluations": evaluations}


class CapabilityRankTests(unittest.TestCase):
    def test_bands_keep_existing_ranks_and_rank_new_releases_on_their_score(self) -> None:
        rank = model_router.measured_capability
        self.assertEqual(rank({"xhigh": 34.4}), 3)   # Sonnet 5
        self.assertEqual(rank({"xhigh": 49.7}), 4)   # Opus 5
        self.assertEqual(rank({"xhigh": 53.2}), 5)   # Fable 5.1
        self.assertEqual(rank({"xhigh": 51.9}), 4)   # Sonnet 5.5
        self.assertEqual(rank({"xhigh": 56.0}), 5)   # Opus 5.5
        self.assertEqual(rank({"xhigh": 12.0}), 1)
        self.assertIsNone(rank({}))
        self.assertIsNone(rank(None))

    def test_xhigh_is_preferred_and_max_is_only_a_fallback(self) -> None:
        # Opus 5.5 needs `max` (minutes to a first answer) to reach 57.6; the
        # rank follows the effort the router actually uses.
        self.assertEqual(model_router.measured_intelligence({"max": 57.6, "xhigh": 56.0, "low": 42.3}), (56.0, "xhigh"))
        self.assertEqual(model_router.measured_intelligence({"max": 57.6, "low": 42.3}), (57.6, "max"))
        self.assertEqual(model_router.measured_intelligence({"base": 49.6}), (49.6, "base"))

    def test_junk_scores_are_ignored(self) -> None:
        cleaned = model_router._clean_intelligence({"xhigh": "n/a", "high": float("nan"), "low": 30, "": 5})
        self.assertEqual(cleaned, (("low", 30.0),))


class FeedFoldingTests(unittest.TestCase):
    def test_effort_variants_fold_into_their_base_model(self) -> None:
        rows = [
            _row("claude-opus-5-5", 57.6), _row("claude-opus-5-5-xhigh", 56.0),
            _row("claude-opus-5-5-low", 42.3), _row("claude-fable-5", 49.6),
            _row("gemini-x", 60.0, provider="google"),
        ]
        folded = {r["model"]: r for r in calib.fold_benchmark_rows(rows)}
        self.assertEqual(set(folded), {"claude-opus-5-5", "claude-fable-5"})
        self.assertEqual(folded["claude-opus-5-5"]["intelligence_by_effort"],
                         {"xhigh": 56.0, "low": 42.3, "max": 57.6})
        # No variants to compare with: the base entry is labelled honestly.
        self.assertEqual(folded["claude-fable-5"]["intelligence_by_effort"], {"base": 49.6})

    def test_a_variant_without_a_base_row_is_not_invented_into_a_model(self) -> None:
        self.assertEqual(calib.fold_benchmark_rows([_row("claude-x-high", 40.0)])[0]["model"], "claude-x-high")


class RefreshTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.service = calib.ModelCalibrationService(self.dir)
        local = mock.patch.object(
            calib, "fetch_local_source",
            return_value=[_entry("claude-sonnet-5", provider="anthropic", agent="claude", capability=3)],
        )
        local.start()
        self.addCleanup(local.stop)
        self.service.ensure_bootstrap(now=1.0)

    def refresh(self, rows: list[dict], available=None) -> dict:
        def fetch(kind, url=""):
            return [dict(r) for r in rows], {"kind": kind, "status": "ok"}

        with mock.patch.object(calib._sources, "fetch_source", side_effect=fetch), \
                mock.patch.dict("os.environ", {"ARTIFICIAL_ANALYSIS_API_KEY": "aa-test"}):
            return self.service.refresh(
                source="models_dev", force=True, now=10.0, activation_policy="auto",
                available_models=available,
            )

    FEED = [
        _row("claude-sonnet-5", 38.2), _row("claude-sonnet-5-xhigh", 34.4),
        _row("claude-sonnet-5-5", 56.0), _row("claude-sonnet-5-5-xhigh", 51.9),
        _row("claude-fable-9", 60.0),
        _row("gpt-9", 70.0, provider="openai"),
    ]

    def test_measured_scores_set_capability_and_keep_the_catalog_value(self) -> None:
        self.refresh(self.FEED)
        model = next(m for m in self.service.load_active()["models"] if m["model"] == "claude-sonnet-5")
        self.assertEqual(model["relative_capability"], 3)
        self.assertEqual(model["capability_source"], "measured")
        self.assertEqual(model["catalog_capability"], 3)
        self.assertEqual(model["intelligence_by_effort"], {"xhigh": 34.4, "max": 38.2})
        # The value the router reads comes from the published catalog.
        published = json.loads((self.dir / "active_catalog.json").read_text())
        routed = next(m for m in published["models"] if m["model"] == "claude-sonnet-5")
        self.assertEqual(routed["intelligence_by_effort"], {"xhigh": 34.4, "max": 38.2})

    def test_a_higher_score_raises_the_rank_the_router_sees(self) -> None:
        feed = [_row("claude-sonnet-5", 56.0), _row("claude-sonnet-5-xhigh", 56.0)]
        self.refresh(feed)
        published = json.loads((self.dir / "active_catalog.json").read_text())
        routed = next(m for m in published["models"] if m["model"] == "claude-sonnet-5")
        self.assertEqual(routed["relative_capability"], 5)

    def test_without_a_model_list_every_feed_model_is_a_candidate(self) -> None:
        self.refresh(self.FEED)
        names = {d["model"] for d in self.service.load_active()["discovered_models"]}
        self.assertIn("claude-sonnet-5-5", names)
        self.assertIn("claude-fable-9", names)
        self.assertNotIn("gpt-9", {d["model"] for d in self.service.load_active()["discovered_models"] if d["provider"] != "openai"})

    def test_only_models_a_cli_reports_become_candidates(self) -> None:
        self.refresh(self.FEED, available={"claude": ["claude-sonnet-5-5", "claude-sonnet-5"], "codex": []})
        discovered = self.service.load_active()["discovered_models"]
        self.assertEqual([d["model"] for d in discovered], ["claude-sonnet-5-5"])
        self.assertEqual(discovered[0]["intelligence_by_effort"], {"xhigh": 51.9, "max": 56.0})

    def test_available_models_json_is_parsed_defensively(self) -> None:
        parse = calib._available_from_json
        self.assertEqual(parse('{"Claude": [{"value": "a"}, "b", {"x": 1}], "codex": "nope"}'),
                         {"claude": ["a", "b"], "codex": []})
        for bad in ("", "not json", "[]", "{}"):
            self.assertIsNone(parse(bad))


class DiscoveredEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        available_models.reset()
        self.addCleanup(available_models.reset)

    def test_a_calibration_ranks_a_new_release_on_its_measurements(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = calib.ModelCalibrationService(Path(tmp))
            local = [_entry("claude-sonnet-5", provider="anthropic", agent="claude", capability=3)]
            rows = [_row("claude-sonnet-5", 38.2), _row("claude-sonnet-5-5", 56.0), _row("claude-sonnet-5-5-xhigh", 51.9)]
            with mock.patch.object(calib, "fetch_local_source", return_value=local), \
                    mock.patch.object(calib._sources, "fetch_source",
                                      side_effect=lambda kind, url="": ([dict(r) for r in rows], {"kind": kind, "status": "ok"})), \
                    mock.patch.dict("os.environ", {"ARTIFICIAL_ANALYSIS_API_KEY": "aa-test"}):
                service.ensure_bootstrap(now=1.0)
                service.refresh(source="models_dev", force=True, now=10.0, activation_policy="auto",
                                available_models={"claude": ["claude-sonnet-5-5"]})
            available_models.configure({"claude": [{"value": "claude-sonnet-5-5"}, {"value": "claude-sonnet-5"}]})
            catalog = {s.model: s for s in model_router.load_model_catalog(Path(tmp) / "active_catalog.json")}
        new = catalog["claude-sonnet-5-5"]
        self.assertEqual(new.relative_capability, 4, "ranked on its xhigh score, not its predecessor's 3")
        self.assertEqual(dict(new.intelligence_by_effort), {"xhigh": 51.9, "max": 56.0})
        self.assertIn("measured Intelligence Index", new.notes)
        self.assertEqual(catalog["claude-sonnet-5"].relative_capability, 3)


class LatestReleaseTests(unittest.TestCase):
    def setUp(self) -> None:
        available_models.configure({
            "claude": [{"value": v} for v in (
                "claude-sonnet-5", "claude-sonnet-5-5", "claude-opus-5", "claude-opus-5-5",
                "claude-opus-4-8", "claude-fable-5", "claude-fable-5-1", "claude-haiku-4-5-20251001")],
            "grok": [{"value": "grok-4.6"}, {"value": "grok-4.7"}],
        })
        self.addCleanup(available_models.reset)

    def test_an_older_release_moves_to_the_newest_of_its_family(self) -> None:
        upgrade = dynamic_router.latest_release("claude", "claude-sonnet-5", "medium")
        self.assertEqual((upgrade.model, upgrade.previous), ("claude-sonnet-5-5", "claude-sonnet-5"))
        self.assertEqual(dynamic_router.latest_release("claude", "claude-opus-5", "xhigh").model, "claude-opus-5-5")
        # Two generations back still lands on the newest, not the next one.
        self.assertEqual(dynamic_router.latest_release("claude", "claude-opus-4-8", "high").model, "claude-opus-5-5")

    def test_the_newest_release_and_unrelated_families_are_left_alone(self) -> None:
        self.assertIsNone(dynamic_router.latest_release("claude", "claude-sonnet-5-5", "high"))
        self.assertIsNone(dynamic_router.latest_release("claude", "claude-haiku-4-5", "low"))
        # Sonnet never becomes Opus, and a provider never changes.
        self.assertNotIn("opus", dynamic_router.latest_release("claude", "claude-sonnet-5", "high").model)

    def test_a_price_increase_blocks_the_upgrade(self) -> None:
        self.assertIsNone(dynamic_router.latest_release("grok", "grok-4.6", "high"))

    def test_usage_credit_and_excluded_releases_are_not_used(self) -> None:
        self.assertIsNone(dynamic_router.latest_release("claude", "claude-fable-5", "high"))
        allowed = dynamic_router.latest_release("claude", "claude-fable-5", "high", allow_usage_credit_models=True)
        self.assertEqual(allowed.model, "claude-fable-5-1")
        self.assertIsNone(dynamic_router.latest_release(
            "claude", "claude-fable-5", "high", allow_usage_credit_models=True,
            excluded=[("claude", "claude-fable-5-1")]))
        self.assertIsNone(dynamic_router.latest_release(
            "claude", "claude-sonnet-5", "medium", excluded=[("claude", "claude-sonnet-5-5")]))

    def test_an_effort_the_newer_release_lacks_blocks_the_upgrade(self) -> None:
        self.assertIsNone(dynamic_router.latest_release("claude", "claude-sonnet-5", "no-such-effort"))

    def test_a_clearly_lower_measured_score_vetoes_the_upgrade(self) -> None:
        old = model_router.load_model_catalog()
        weaker = tuple(
            __import__("dataclasses").replace(
                s, intelligence_by_effort=(("high", 30.0),) if s.model == "claude-sonnet-5-5" else (("high", 40.0),) if s.model == "claude-sonnet-5" else s.intelligence_by_effort)
            for s in old
        )
        with mock.patch.object(dynamic_router, "_active_calibration_catalog", return_value=weaker):
            self.assertIsNone(dynamic_router.latest_release("claude", "claude-sonnet-5", "high"))
        stronger = tuple(
            __import__("dataclasses").replace(
                s, intelligence_by_effort=(("high", 51.9),) if s.model == "claude-sonnet-5-5" else (("high", 34.4),) if s.model == "claude-sonnet-5" else s.intelligence_by_effort)
            for s in old
        )
        with mock.patch.object(dynamic_router, "_active_calibration_catalog", return_value=stronger):
            upgrade = dynamic_router.latest_release("claude", "claude-sonnet-5", "high")
        self.assertEqual(upgrade.model, "claude-sonnet-5-5")
        self.assertIn("51.9 against 34.4 at high", upgrade.reason)


if __name__ == "__main__":
    unittest.main()
