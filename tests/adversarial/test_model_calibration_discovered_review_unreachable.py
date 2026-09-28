"""Issue #205 (Model Routing Calibration) — approving a DISCOVERED model does
not, in the one situation an operator actually does it for, ever clear that
model's status. `ModelCalibrationService.approve_discovered_model` records the
approval, but the very next refresh — with nothing else meaningfully changed,
which is exactly the state right after an operator reviews and approves a
model — reports `"no_change"` and discards the corrected calibration entirely
without writing it anywhere. The model stays DISCOVERED forever. On top of
that, nothing in the shipped desktop application can even call
`approve_discovered_model` in the first place.

Expected behaviour, derived from the issue before reading the implementation:

- "Make clear that discovering a new model does not automatically make it
  available for routing." Read with the "Model Detail View" and "Advanced
  User" progressive-disclosure sections ("Should be able to inspect ...
  calibration versions ... routing decisions"), a review gate exists so a
  human can deliberately decide a newly discovered model is fine to route
  to — and for that decision to matter, it has to be possible to *act* on
  it, not just read about it.
- Acceptance criterion 15: "Failed refreshes never corrupt or replace the
  active calibration" and acceptance criterion 9's "AI Configuration
  displays last refresh time and refresh status" together describe a
  refresh as either changing the stored calibration (and saying so) or
  leaving it untouched (and saying so) — never silently computing a
  corrected calibration in memory and then throwing it away while reporting
  a successful, unremarkable "no_change".
- `test_model_calibration_discovered_gate.py` (an earlier adversarial round)
  established that DISCOVERED must be *sticky* until explicit approval,
  because silently promoting an unreviewed model is unacceptable. That fix
  only prevents the bad case; it does nothing to guarantee the good case —
  a deliberately approved model actually becoming routable — actually works.

What the implementation does:

1. `approve_discovered_model` writes `key` into `state["approved_models"]`
   and saves `state.json`. It does not touch `active_catalog.json`,
   `calibration_active.json`, or write any new calibration.
2. The *next* refresh's `_build_calibration` correctly reads
   `approved_models` and computes ACTIVE/CANDIDATE status for that model in
   the in-memory `new_calibration` (see `_status_for`'s `approved_keys`
   branch, checked first).
3. But `_refresh_locked` only ever persists that `new_calibration` — via
   `_write_history` plus either `activate()` or writing `proposed_path` —
   inside the `diff["has_meaningful_change"]` branch. `diff_calibrations`
   never compares the `status` field between the previous and new model
   lists, only pricing/benchmark/routing fields and *new* keys. A model
   that already existed in the previous calibration (as DISCOVERED) and
   only changes `status` this refresh produces `has_meaningful_change =
   False`, so control falls into::

       if not run_calibration or not diff["has_meaningful_change"]:
           state["last_attempted_status"] = "no_change"
           self.save_state(state)
           ...
           return result

   which returns without ever calling `_write_history`, `activate`, or
   writing `proposed_path`. The correctly-computed `new_calibration` — the
   one where the approved model is finally ACTIVE — is discarded. The file
   on disk, and therefore the AI Configuration page and live routing, keep
   showing DISCOVERED.
4. Approval only "works" by accident, when the same refresh happens to also
   contain an unrelated pricing/benchmark/routing change large enough to
   make `has_meaningful_change` true on its own — asserted below as the
   contrasting case, to show this is specifically about the change-diffing
   logic ignoring `status`, not about approval being fundamentally unable to
   work.
5. Independently, `src/main.rs` registers no Tauri command that shells out
   to `model_calibration.py approve`, and `ui/app.js` / `ui/index.html`
   contain no invocation of or affordance for one — so even fixing (3) would
   still leave approval reachable only by hand-running the bundled Python
   script outside the application.
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

REPO_ROOT = Path(__file__).resolve().parents[2]
ISSUE_WORKER_DIR = REPO_ROOT / "issue_worker"
if str(ISSUE_WORKER_DIR) not in sys.path:
    sys.path.insert(0, str(ISSUE_WORKER_DIR))

import model_calibration as calib  # noqa: E402


def model_entry(model: str, **overrides: object) -> dict:
    entry = {
        "provider": "fixture",
        "agent": "fixture",
        "model": model,
        "model_id": model,
        "active": True,
        "recommended": True,
        "deprecated": False,
        "superseded_by": None,
        "supported_efforts": ["medium"],
        "strengths": [],
        "weaknesses": [],
        "relative_capability": 3,
        "relative_cost": 3,
        "relative_token_efficiency": 3,
        "relative_latency": 3,
        "benchmarks": {},
        "benchmark_source": None,
        "benchmark_date": None,
        "notes": "",
    }
    entry.update(overrides)
    return entry


ESTABLISHED = "established"
BRAND_NEW = "brand-new"


class ApprovalDoesNotSurviveANoOtherwiseMeaningfulChangeRefreshTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.service = calib.ModelCalibrationService(Path(self._tmp.name))
        self.service.refresh(
            fetch_fn=lambda: [model_entry(ESTABLISHED)], now=1.0, activation_policy="auto"
        )
        self.service.refresh(
            fetch_fn=lambda: [model_entry(ESTABLISHED), model_entry(BRAND_NEW)],
            now=100.0,
            force=True,
            activation_policy="auto",
        )
        self.assertEqual(
            self._status_of(f"fixture/{BRAND_NEW}"),
            calib.STATUS_DISCOVERED,
            "sanity check: the model must land as DISCOVERED before approval",
        )

    def _status_of(self, key: str) -> str | None:
        active = self.service.load_active() or {}
        return {entry["key"]: entry["status"] for entry in active.get("models", [])}.get(key)

    def test_approving_then_refreshing_with_nothing_else_changed_clears_discovered(self) -> None:
        # This is the realistic operator sequence: review the model, approve
        # it, then refresh (or wait for the next scheduled/startup refresh)
        # to pick it up. Nothing else about the catalog needs to change for
        # this to be the whole point of calling approve_discovered_model.
        approval = self.service.approve_discovered_model(f"fixture/{BRAND_NEW}")
        self.assertIn(f"fixture/{BRAND_NEW}", approval["approved_models"])

        result = self.service.refresh(
            fetch_fn=lambda: [model_entry(ESTABLISHED), model_entry(BRAND_NEW)],
            now=200.0,
            force=True,
            activation_policy="auto",
        )
        status_after = self._status_of(f"fixture/{BRAND_NEW}")
        self.assertIn(
            status_after,
            (calib.STATUS_ACTIVE, calib.STATUS_CANDIDATE),
            "approve_discovered_model recorded the approval, but the refresh "
            f"that should have picked it up returned status={result['status']!r} "
            f"and left the model at status={status_after!r}. `diff_calibrations` "
            "never compares the `status` field, so a refresh whose only real "
            "change is the just-approved model's status computes "
            "has_meaningful_change=False and `_refresh_locked` returns before "
            "ever writing the corrected calibration to calibration_active.json "
            "— the in-memory new_calibration that correctly shows this model "
            "as ACTIVE/CANDIDATE is silently thrown away. Approval is a no-op "
            "unless an unrelated, independently-meaningful change happens to "
            "land in the exact same refresh.",
        )

    def test_approval_only_takes_effect_when_an_unrelated_change_rides_along(self) -> None:
        """Contrast case: proves the defect is specifically that `status`
        changes are invisible to `diff_calibrations`, not that approval can
        never work at all."""
        self.service.approve_discovered_model(f"fixture/{BRAND_NEW}")
        self.service.refresh(
            fetch_fn=lambda: [
                model_entry(ESTABLISHED, relative_cost=4),
                model_entry(BRAND_NEW),
            ],
            now=200.0,
            force=True,
            activation_policy="auto",
        )
        self.assertIn(
            self._status_of(f"fixture/{BRAND_NEW}"),
            (calib.STATUS_ACTIVE, calib.STATUS_CANDIDATE),
            "sanity check: when an unrelated pricing change makes "
            "has_meaningful_change True in the same refresh, approval does "
            "take effect — confirming the defect above is specifically "
            "about a status-only change being treated as 'nothing changed'",
        )


class DiscoveredModelApprovalIsUnreachableFromTheAppTests(unittest.TestCase):
    """Independent of the persistence bug above: even a correctly working
    `approve_discovered_model` has no path from the shipped application."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.main_rs = (REPO_ROOT / "src" / "main.rs").read_text(encoding="utf-8")
        cls.app_js = (REPO_ROOT / "ui" / "app.js").read_text(encoding="utf-8")
        cls.index_html = (REPO_ROOT / "ui" / "index.html").read_text(encoding="utf-8")

    def _registered_tauri_commands(self) -> list[str]:
        match = re.search(r"tauri::generate_handler!\[(.*?)\]", self.main_rs, re.DOTALL)
        self.assertIsNotNone(
            match, "src/main.rs must register its Tauri commands via tauri::generate_handler![...]"
        )
        return [name.strip() for name in match.group(1).split(",") if name.strip()]

    def test_a_tauri_command_exposes_approving_a_discovered_model(self) -> None:
        commands = self._registered_tauri_commands()
        approving = [name for name in commands if "approve" in name.lower()]
        self.assertTrue(
            approving,
            "No registered Tauri command approves a DISCOVERED model. "
            "issue_worker/model_calibration.py's ModelCalibrationService."
            "approve_discovered_model (and the `model_calibration.py approve "
            "<key>` CLI subcommand it backs) is the only documented way to "
            "clear a model out of DISCOVERED status, but src/main.rs's "
            f"tauri::generate_handler! list is {commands!r} — nothing in it "
            "shells out to that subcommand, so this action cannot be invoked "
            "from the desktop app at all.",
        )

    def test_the_frontend_calls_the_approval_command(self) -> None:
        invoked_commands = set(re.findall(r'invoke\(\s*"([^"]+)"', self.app_js))
        approving_invocations = {name for name in invoked_commands if "approve" in name.lower()}
        self.assertTrue(
            approving_invocations,
            "ui/app.js never calls invoke(...) with any command name "
            "containing 'approve'. Even if src/main.rs eventually registers "
            "an approval command, nothing in the frontend would ever call "
            "it. Calibration-related commands actually invoked: "
            f"{sorted(c for c in invoked_commands if 'calibration' in c.lower() or 'model' in c.lower())}",
        )

    def test_the_model_routing_table_offers_an_approve_action_in_its_markup(self) -> None:
        self.assertTrue(
            re.search(r"approve", self.index_html, re.IGNORECASE),
            "ui/index.html contains no element (button, row action, or "
            "otherwise) whose text or attributes mention approving a model. "
            "The Model Routing Table / Model Detail View sections show "
            "DISCOVERED status (informational only) but offer no way to act "
            "on it.",
        )


if __name__ == "__main__":
    unittest.main()
