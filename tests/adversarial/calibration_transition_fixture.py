"""Real offline backend snapshots for review/activation UI transitions."""

import copy
import json
import tempfile
from pathlib import Path
from unittest import mock

from calibration_uat_fixture import NOW, SOURCE_URL, calibration, model_entry, price_row, sources


def snapshots():
    with tempfile.TemporaryDirectory() as directory, mock.patch.object(calibration.time, "time", return_value=NOW + 60):
        service = calibration.ModelCalibrationService(Path(directory))
        local = [model_entry(input_cost=2, output_cost=8)]
        payload = {"models": [price_row(input_cost=5.25, output_cost=23.5)]}
        with mock.patch.object(calibration, "fetch_local_source", side_effect=lambda: copy.deepcopy(local)):
            service.ensure_bootstrap(now=NOW)
            with mock.patch.object(sources, "fetch_json", return_value=(payload, "fixture-v1")):
                refresh = service.refresh(source="json", source_url=SOURCE_URL,
                                          force=True, now=NOW + 1)
                proposed = service.status_report()
                repeated = service.refresh(source="json", source_url=SOURCE_URL,
                                           force=True, now=NOW + 2, initiated_by="STARTUP")
                rechecked = service.status_report()
            service.activate(refresh["calibration_version"])
            activated = service.status_report()
            # Separate sequence models clicking Activate immediately after
            # the manual refresh, without an intervening startup check.
            second = calibration.ModelCalibrationService(Path(directory) / "manual")
            second.ensure_bootstrap(now=NOW)
            with mock.patch.object(sources, "fetch_json", return_value=(payload, "fixture-v1")):
                manual_refresh = second.refresh(source="json", source_url=SOURCE_URL,
                                                force=True, now=NOW + 1)
            manual_proposed = second.status_report()
            second.activate(manual_refresh["calibration_version"])
            manual_activated = second.status_report()
        return {
            "refresh": refresh, "proposed": proposed,
            "repeated": repeated, "rechecked": rechecked, "activated": activated,
            "manual_refresh": manual_refresh, "manual_proposed": manual_proposed,
            "manual_activated": manual_activated,
        }


if __name__ == "__main__":
    print(json.dumps(snapshots()))
