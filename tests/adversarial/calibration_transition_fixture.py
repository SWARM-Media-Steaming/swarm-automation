"""Real offline backend snapshots for automatic-activation UI transitions."""

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
                active = service.status_report()
                repeated = service.refresh(source="json", source_url=SOURCE_URL,
                                           force=True, now=NOW + 2, initiated_by="STARTUP")
                rechecked = service.status_report()
        return {
            "refresh": refresh, "active": active,
            "repeated": repeated, "rechecked": rechecked,
        }


if __name__ == "__main__":
    print(json.dumps(snapshots()))
