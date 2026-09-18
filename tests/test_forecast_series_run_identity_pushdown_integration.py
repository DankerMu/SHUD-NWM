"""Real-database integration tests for #2417's forecast-series run-identity pushdown.

Split out of ``tests/test_display_coverage_residual_debt_integration.py``: these
assert on the PUBLIC forecast-series read across the two physical timeseries
stores, not on the display-coverage residual debt that module owns, and keeping
them there pushed it past the repo's 1000-line guard.

Run with the repo's standard opt-in against a throwaway database:

    NHMS_RUN_INTEGRATION=1 NHMS_INTEGRATION_DATABASE_URL=... uv run pytest -q \
        tests/test_forecast_series_run_identity_pushdown_integration.py

The database seed, the NULL-``forcing_version_id`` newest-run fixture and the
connection helpers stay owned by the residual-debt module, which seeds the same
`ISSUE_126_PREFIX` cohort; they are imported rather than duplicated so the two
modules cannot drift into two different databases.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from typing import Any

import pytest

from packages.common import forecast_store
from packages.common.forecast_store import PsycopgForecastStore
from tests.integration_helpers import (
    BASIN_VERSION_ID,
    FORECAST_RUN_ID,
    ISSUE_126_PREFIX,
    MODEL_ID,
    RIVER_NETWORK_VERSION_ID,
)
from tests.integration_helpers import (
    post_expand_forecast_database as post_expand_forecast_database,
)
from tests.test_display_coverage_residual_debt_integration import (
    _connect,
    _insert_null_forcing_run,
    _prepared_database,
)

pytestmark = pytest.mark.integration


def test_public_forecast_selects_latest_and_pinned_runs(
    throwaway_database_url: str,
    post_expand_forecast_database: Callable[[Mapping[str, str]], None],
) -> None:
    """Latest, pinned and explicit-cycle, against one physical fact table.

    This was parametrised over which of the two runs stayed on the retired
    store. #1342's contract (task 6.3) left one store, so both runs are marked
    narrow and the fixture's poisoning lands entirely on the retired table —
    which is what makes it still a decoy setup rather than a no-op: a reader
    that reached for ``hydro.river_timeseries_legacy`` would come back with
    values 10000 too high and half an hour late.
    """
    _prepared_database(throwaway_database_url)
    connection = _connect(throwaway_database_url)
    try:
        newest_run_id = _insert_null_forcing_run(connection)
    finally:
        connection.close()
    post_expand_forecast_database({newest_run_id: "narrow", FORECAST_RUN_ID: "narrow"})
    store = PsycopgForecastStore(throwaway_database_url)
    parameters = {
        "basin_version_id": BASIN_VERSION_ID,
        "segment_id": f"{ISSUE_126_PREFIX}_seg_inside",
        "river_network_version_id": RIVER_NETWORK_VERSION_ID,
        "issue_time": "latest",
        "variables": ["q_down"],
        "scenarios": ["GFS"],
    }

    latest = store.forecast_series(**parameters)
    assert latest["issue_time"] == "2026-05-03T06:00:00Z"
    assert [point[1] for series in latest["series"] for point in series["points"]] == [101.0, 102.0]

    pinned = store.forecast_series(**parameters, run_id=FORECAST_RUN_ID, model_id=MODEL_ID)
    assert pinned["issue_time"] == "2026-05-03T00:00:00Z"
    assert [point[1] for series in pinned["series"] for point in series["points"]] == [180.0, 250.0]

    parameters["issue_time"] = "2026-05-03T00:00:00Z"
    explicit_cycle = store.forecast_series(**parameters)
    assert explicit_cycle == pinned


#: The node-27 probe serialisation (`/home/nwm/tmp/2410/explain718e.py`), applied
#: to the rows the public route actually returns: sha256 over the newline-joined
#: `repr(sorted(row.items()))` of every row, truncated to 16 hex characters.
#: Reproduced, not reinvented — a row count cannot see a row swapped for another
#: run's row of the same shape, which is the failure mode run-identity pushdown
#: can introduce.
def _series_row_digest(response: Mapping[str, Any]) -> tuple[str, int]:
    rows = [
        {
            "scenario_id": series.get("scenario_id"),
            "source_id": series.get("source_id"),
            "cycle_time": series.get("cycle_time"),
            "valid_time": point[0],
            "value": point[1],
        }
        for series in response["series"]
        for point in series["points"]
    ]
    preimage = "\n".join(repr(sorted(row.items())) for row in rows)
    return hashlib.sha256(preimage.encode("utf-8")).hexdigest()[:16], len(rows)


def test_run_identity_pushdown_returns_byte_identical_rows_on_every_forecast_shape(
    throwaway_database_url: str,
    post_expand_forecast_database: Callable[[Mapping[str, str]], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#2417 task 3.1: row equivalence pre/post, by digest, on all three shapes.

    Only a real database can answer this: a capture cursor returns whatever was
    queued regardless of the predicates, so a text oracle cannot say that the
    pushed query SELECTS the same rows. The unpushed baseline is produced by
    emptying the four pushdown fragments — the same monkeypatch idiom the
    residual-debt module uses on ``_QHH_LATEST_CANDIDATE_RUNS_SQL`` — so both
    sides run the identical outer layer and differ only in what was pushed into
    the branches.
    """
    _prepared_database(throwaway_database_url)
    connection = _connect(throwaway_database_url)
    try:
        newest_run_id = _insert_null_forcing_run(connection)
    finally:
        connection.close()
    post_expand_forecast_database({newest_run_id: "narrow", FORECAST_RUN_ID: "narrow"})
    store = PsycopgForecastStore(throwaway_database_url)
    base = {
        "basin_version_id": BASIN_VERSION_ID,
        "segment_id": f"{ISSUE_126_PREFIX}_seg_inside",
        "river_network_version_id": RIVER_NETWORK_VERSION_ID,
        "variables": ["q_down"],
        "scenarios": ["GFS"],
    }
    shapes = {
        "latest": {**base, "issue_time": "latest"},
        "explicit_cycle_run_bound": {
            **base,
            "issue_time": "2026-05-03T00:00:00Z",
            "run_id": FORECAST_RUN_ID,
            "model_id": MODEL_ID,
        },
        "explicit_cycle_run_unbound": {**base, "issue_time": "2026-05-03T00:00:00Z"},
    }

    pushed = {label: _series_row_digest(store.forecast_series(**call)) for label, call in shapes.items()}

    for constant in (
        "_BOUND_RUN_PUSHDOWN_SQL",
        "_RESOLVED_RUN_PUSHDOWN_SQL",
        "_CYCLE_WINDOW_PUSHDOWN_SQL",
        "_ANALYSIS_SCENARIO_PUSHDOWN_SQL",
    ):
        assert getattr(forecast_store, constant) != ""
        monkeypatch.setattr(forecast_store, constant, "")
    unpushed = {label: _series_row_digest(store.forecast_series(**call)) for label, call in shapes.items()}

    assert pushed == unpushed
    # Non-vacuity: every shape really returned rows, so the equality above is not
    # two empty digests agreeing.
    for label, (digest, row_count) in pushed.items():
        assert row_count > 0, label
        assert digest, label
