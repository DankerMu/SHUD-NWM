from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from apps.api.main import create_app
from packages.common.object_store_forcing import PsycopgStationLookup
from tests.object_store_forcing_real_disk_support import ResolvedCombo, Selection, latest_complete_cycle

pytestmark = [pytest.mark.e2e, pytest.mark.real_disk]

# Shape-only baseline: `_assert_station_series_shape` compares key order and JSON
# kinds, never values, so the cycle, model and station recorded in it are not
# the ones under test, and the file cannot carry this note itself (an extra key
# breaks the key-list equality). #2699: recorded from a Direct Grid station as
# the API served it (heihe, IFS), kept on one line.
BASELINE_FIXTURE = Path(__file__).parent / "fixtures" / "station_series_baseline_direct_grid.json"
# Negative case: a cycle no retention window will ever hold again.
MISSING_CYCLE = "2020-01-01T00:00:00Z"
# #2699: what the suite covers. The model and the station of each combination
# are resolved per cycle from the store and `met.interp_weight`, never named
# here -- the store holds only `dg_*` model directories and their names change
# with every Direct Grid generation.
BASIN_SOURCE_COMBOS = [
    ("basins_heihe_vbasins", "IFS"),
    ("basins_heihe_vbasins", "gfs"),
    ("basins_qhh_vbasins", "IFS"),
    ("basins_qhh_vbasins", "gfs"),
]
# The combination the baseline was recorded from; the point count of another
# source or basin is not known.
SHAPE_COMBO = ("basins_heihe_vbasins", "IFS")
# Owner decision (#2699): a legacy model id whose artifacts have left the store
# answers 404 and is never mapped to a Direct Grid variant.
LEGACY_MODEL_ID = "basins_heihe_shud"
_FORCING_STEP_SECONDS = 3 * 3600
_PLUS_EIGHT = timezone(timedelta(hours=8))


@pytest.fixture(scope="module")
def selection() -> Selection:
    """#2595/#2699: the newest cycle where all four combos resolve in the real store.

    Chosen at run time, once per module, AFTER the real-disk skip gate: node-27
    retention keeps a rolling window, so any pinned cycle eventually 404s every
    case. Import time computes nothing -- the CI `--collect-only` smoke imports
    this module without `OBJECT_STORE_ROOT`. The station of a model is asked of
    `met.interp_weight` by `model_id` only, as the production list path does, on
    one read-only connection of this fixture's own; station metadata keeps going
    through `PsycopgStationLookup.from_env()`, which needs its dict-row cursor.
    """
    root = _real_object_store_root()
    import psycopg2

    connection = psycopg2.connect(os.environ["DATABASE_URL"].strip())
    try:
        connection.set_session(readonly=True, autocommit=True)

        def station_for_model(model_id: str) -> str | None:
            with connection.cursor() as cursor:
                cursor.execute("SELECT min(station_id) FROM met.interp_weight WHERE model_id = %s", (model_id,))
                row = cursor.fetchone()
            return row[0] if row else None

        chosen = latest_complete_cycle(root, BASIN_SOURCE_COMBOS, station_for_model, PsycopgStationLookup.from_env())
    finally:
        connection.close()
    print(f"real_disk cycle: {chosen.cycle} (newest cycle with all {len(chosen.combos)} combos settled under {root})")
    return chosen


def test_real_disk_latest_cycle_serves_all_currently_409_combinations(selection: Selection) -> None:
    with _client() as client:
        for combo in selection.combos:
            _report(selection, combo)
            response = _get_series(client, combo, cycle_time=selection.cycle)

            assert response.status_code == 200, response.text
            data = response.json()["data"]
            profile = _csv_profile(combo.path, selection.cycle)
            assert data["station_id"] == combo.station_id
            assert data["model_id"] == combo.model_id
            assert [series["variable"] for series in data["series"]] == ["PRCP", "TEMP", "RH", "wind", "Rn"]
            assert {series["unit"] for series in data["series"]} == {"mm/day", "degC", "0-1", "m/s", "W/m^2"}
            assert sum(len(series["points"]) for series in data["series"]) == 5 * profile["nrow"]
            assert data["valid_time_start"] == profile["valid_time_start"]
            assert data["valid_time_end"] == profile["valid_time_end"]
            for series in data["series"]:
                assert len(series["points"]) == profile["nrow"]
                assert series["metadata"]["returned_points"] == profile["nrow"]
                assert series["metadata"]["returned_from"] == profile["valid_time_start"]
                assert series["metadata"]["returned_to"] == profile["valid_time_end"]
                assert series["metadata"]["truncated"] is False
                assert series["truncated"] is False


def test_real_disk_error_and_filter_scenarios(selection: Selection) -> None:
    combo = selection.combos[0]
    _report(selection, combo)
    cycle_time = _dt(selection.cycle)
    # The window below expects exactly the two points at cycle+3h and cycle+6h,
    # which holds only on the producer's 3-hour grid: assert the grid, do not
    # assume it.
    window_profile = _csv_profile(combo.path, selection.cycle)
    assert window_profile["step_seconds"] == _FORCING_STEP_SECONDS
    window_from = _format_time(cycle_time + timedelta(hours=3))
    window_to = _format_time(cycle_time + timedelta(hours=6))
    with _client() as client:
        missing_cycle = _get_series(client, combo, cycle_time=MISSING_CYCLE)
        missing_station = _get_series(client, combo, station_id="bogus_forc_999", cycle_time=selection.cycle)
        variables = _get_series(client, combo, cycle_time=selection.cycle, variables="PRCP,TEMP")
        window = _get_series(
            client,
            combo,
            cycle_time=selection.cycle,
            **{"from": window_from, "to": window_to, "variables": "PRCP"},
        )
        zulu = _get_series(client, combo, cycle_time=selection.cycle, variables="PRCP")
        plus_eight = _get_series(
            client, combo, cycle_time=cycle_time.astimezone(_PLUS_EIGHT).isoformat(), variables="PRCP"
        )
        press_only = _get_series(client, combo, cycle_time=selection.cycle, variables="Press")
        prcp_press = _get_series(client, combo, cycle_time=selection.cycle, variables="PRCP,Press")

    assert missing_cycle.status_code == 404
    assert missing_cycle.json()["error"]["code"] == "STATION_FORCING_FILE_NOT_FOUND"
    missing_details = missing_cycle.json()["error"]["details"]
    if combo.active_flag is False:
        # An inactive station (every Direct Grid station today) gets the
        # desensitized miss of `StationForcingFileNotFoundError`: the station
        # id and nothing that names a storage key.
        assert missing_details == {"station_id": combo.station_id}
    else:
        assert missing_details["expected_path"].startswith(f"forcing/{combo.source_id.lower()}/2020010100/")
    assert str(_real_object_store_root()) not in missing_cycle.text
    assert missing_station.status_code == 404
    assert missing_station.json()["error"]["code"] == "STATION_NOT_FOUND"
    assert variables.status_code == 200
    assert [series["variable"] for series in variables.json()["data"]["series"]] == ["PRCP", "TEMP"]
    assert window.status_code == 200
    assert [point["valid_time"] for point in window.json()["data"]["series"][0]["points"]] == [
        window_from,
        window_to,
    ]
    assert zulu.status_code == 200
    assert plus_eight.status_code == 200
    assert zulu.json()["data"] == plus_eight.json()["data"]
    assert press_only.status_code == 200
    assert press_only.json()["data"]["series"] == []
    assert prcp_press.status_code == 200
    assert [series["variable"] for series in prcp_press.json()["data"]["series"]] == ["PRCP"]


def test_real_disk_legacy_model_id_is_404_while_the_direct_grid_model_serves(selection: Selection) -> None:
    combo = _combo(selection, *SHAPE_COMBO)
    _report(selection, combo)
    # <cycle>/<basin_version>/<model>/shud/<file>: the basin directory of the
    # chosen cycle must hold no legacy model directory, or the 404 below would
    # not be the "artifacts have left the store" case.
    legacy_dir = combo.path.parents[2] / LEGACY_MODEL_ID
    assert not legacy_dir.exists(), f"legacy model directory is back in the store: {legacy_dir}"

    with _client() as client:
        direct_grid = _get_series(client, combo, cycle_time=selection.cycle)
        legacy = _get_series(client, combo, model_id=LEGACY_MODEL_ID, cycle_time=selection.cycle)

    # Same station, cycle and source: the model id alone decides. Status and
    # code only -- the details of an inactive station carry no model id.
    assert direct_grid.status_code == 200, direct_grid.text
    assert legacy.status_code == 404, legacy.text
    assert legacy.json()["error"]["code"] == "STATION_FORCING_FILE_NOT_FOUND"


def test_real_disk_station_series_read_is_side_effect_free(selection: Selection) -> None:
    combo = selection.combos[0]
    _report(selection, combo)
    before = combo.path.stat().st_mtime_ns

    with _client() as client:
        responses = [_get_series(client, combo, cycle_time=selection.cycle) for _ in range(3)]

    assert [response.status_code for response in responses] == [200, 200, 200]
    assert responses[0].json()["data"] == responses[1].json()["data"] == responses[2].json()["data"]
    assert combo.path.stat().st_mtime_ns == before


def test_real_disk_station_series_response_shape_matches_baseline_fixture(selection: Selection) -> None:
    baseline = json.loads(BASELINE_FIXTURE.read_text(encoding="utf-8"))
    combo = _combo(selection, *SHAPE_COMBO)
    _report(selection, combo)

    with _client() as client:
        response = _get_series(client, combo, cycle_time=selection.cycle)

    assert response.status_code == 200, response.text
    _assert_station_series_shape(response.json(), baseline)


def _combo(selection: Selection, basin_version_id: str, source_id: str) -> ResolvedCombo:
    return next(
        combo
        for combo in selection.combos
        if (combo.basin_version_id, combo.source_id) == (basin_version_id, source_id)
    )


def _report(selection: Selection, combo: ResolvedCombo) -> None:
    # The node-27 receipt (`-s`/`-rA`) must record what each case measured.
    print(
        f"real_disk selection: cycle={selection.cycle} basin_version={combo.basin_version_id} "
        f"source={combo.source_id} model={combo.model_id} station={combo.station_id}"
    )


def _get_series(
    client: TestClient,
    combo: ResolvedCombo,
    *,
    cycle_time: str,
    station_id: str | None = None,
    model_id: str | None = None,
    **filters: str,
) -> Any:
    # Direct Grid station ids hold `:` (`dg-ifs-<hash>::cell:<n>`), hence the quoting.
    return client.get(
        f"/api/v1/met/stations/{quote(station_id or combo.station_id, safe='')}/series",
        params={
            "model_id": model_id or combo.model_id,
            "source_id": combo.source_id,
            "cycle_time": cycle_time,
            **filters,
        },
    )


def _client() -> TestClient:
    root = _real_object_store_root()
    app = create_app(
        {
            "NHMS_REQUIRE_SERVICE_ROLE": "true",
            "NHMS_SERVICE_ROLE": "display_readonly",
            "OBJECT_STORE_ROOT": str(root),
        }
    )
    return TestClient(app)


def _real_object_store_root() -> Path:
    if os.getenv("NHMS_RUN_REAL_DISK", "").strip().lower() not in {"1", "true", "yes", "on"}:
        pytest.skip("real-disk tests require NHMS_RUN_REAL_DISK=1")
    if not os.getenv("DATABASE_URL", "").strip():
        pytest.skip("real-disk tests require DATABASE_URL")
    raw_root = os.getenv("OBJECT_STORE_ROOT", "").strip()
    if not raw_root:
        pytest.skip("real-disk tests require OBJECT_STORE_ROOT")
    root = Path(raw_root).expanduser().resolve()
    if not root.is_dir():
        pytest.skip(f"real-disk OBJECT_STORE_ROOT is not a directory: {root}")
    return root


def _dt(value: str) -> Any:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _format_time(value: Any) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _csv_profile(path: Path, cycle: str) -> dict[str, Any]:
    lines = path.read_text(encoding="utf-8").splitlines()
    nrow = int(lines[0].split()[0])
    rows = lines[2 : 2 + nrow]
    assert len(rows) == nrow
    time_days = [float(row.split()[0]) for row in rows]
    cycle_time = _dt(cycle)
    return {
        "nrow": nrow,
        "step_seconds": int(round((time_days[1] - time_days[0]) * 86400)) if nrow > 1 else None,
        "valid_time_start": _format_time(cycle_time + timedelta(seconds=int(round(time_days[0] * 86400)))),
        "valid_time_end": _format_time(cycle_time + timedelta(seconds=int(round(time_days[-1] * 86400)))),
    }


def _assert_station_series_shape(actual: dict[str, Any], baseline: dict[str, Any]) -> None:
    assert list(actual.keys()) == list(baseline.keys())
    assert isinstance(actual["request_id"], str)
    assert actual["status"] == baseline["status"] == "ok"

    actual_data = actual["data"]
    baseline_data = baseline["data"]
    assert list(actual_data.keys()) == list(baseline_data.keys())
    _assert_ordered_shape(actual_data["station"], baseline_data["station"], path="data.station")

    for key, actual_value in actual_data.items():
        if key in {"station", "series"}:
            continue
        assert _json_kind(actual_value) == _json_kind(baseline_data[key]), key

    actual_variables = [series["variable"] for series in actual_data["series"]]
    baseline_variables = [series["variable"] for series in baseline_data["series"]]
    assert actual_variables == [variable for variable in baseline_variables if variable in actual_variables]

    baseline_by_variable = {series["variable"]: series for series in baseline_data["series"]}
    for series in actual_data["series"]:
        baseline_series = baseline_by_variable[series["variable"]]
        _assert_series_shape(series, baseline_series)


def _assert_series_shape(actual: dict[str, Any], baseline: dict[str, Any]) -> None:
    assert list(actual.keys()) == list(baseline.keys())
    assert list(actual["metadata"].keys()) == list(baseline["metadata"].keys())
    _assert_ordered_shape(actual["metadata"], baseline["metadata"], path=f"series[{actual['variable']}].metadata")

    for key, actual_value in actual.items():
        if key in {"points", "metadata"}:
            continue
        assert _json_kind(actual_value) == _json_kind(baseline[key]), f"series[{actual['variable']}].{key}"

    assert actual["points"], f"series[{actual['variable']}].points must not be empty"
    assert len(actual["points"]) == len(baseline["points"])
    assert actual["metadata"]["returned_points"] == baseline["metadata"]["returned_points"]
    assert actual["metadata"]["returned_points"] == len(actual["points"])
    assert actual["truncated"] == baseline["truncated"] is False
    assert actual["metadata"]["truncated"] == baseline["metadata"]["truncated"] is False
    valid_times = [point["valid_time"] for point in actual["points"]]
    assert valid_times == sorted(valid_times, key=_dt)
    assert actual["metadata"]["returned_from"] == valid_times[0]
    assert actual["metadata"]["returned_to"] == valid_times[-1]

    baseline_point = baseline["points"][0]
    for index, point in enumerate(actual["points"]):
        assert list(point.keys()) == list(baseline_point.keys())
        for key, actual_value in point.items():
            assert _json_kind(actual_value) == _json_kind(baseline_point[key]), (
                f"series[{actual['variable']}].points[{index}].{key}"
            )


def _assert_ordered_shape(actual: dict[str, Any], baseline: dict[str, Any], *, path: str) -> None:
    assert list(actual.keys()) == list(baseline.keys()), path
    for key, actual_value in actual.items():
        baseline_value = baseline[key]
        current_path = f"{path}.{key}"
        assert _json_kind(actual_value) == _json_kind(baseline_value), current_path
        if isinstance(actual_value, dict):
            _assert_ordered_shape(actual_value, baseline_value, path=current_path)
        elif isinstance(actual_value, list):
            assert len(actual_value) == len(baseline_value), current_path
            for index, item in enumerate(actual_value):
                if isinstance(item, dict):
                    _assert_ordered_shape(item, baseline_value[index], path=f"{current_path}[{index}]")
                else:
                    assert _json_kind(item) == _json_kind(baseline_value[index]), f"{current_path}[{index}]"


def _json_kind(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int | float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__
