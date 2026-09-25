"""#2222: `/api/v1/runs{,/{run_id}}` expose only the public `HydroRun` projection.

Three layers, each proven on its own seam:

1. SQL: `get_run` / `list_runs` select `HYDRO_RUN_PUBLIC_COLUMNS` by name
   (never `h.*`) plus the three join aliases -- fake-cursor SQL-shape seam.
2. Serializer: `_hydro_run_response` projects a post-I7 row (carrying
   `timeseries_store` and a synthetic `authority_internal`) onto the allowlist.
3. Route model: `HydroRun` (`extra="ignore"`) drops those keys even when a
   store double hands them straight to the route.

The expected public key set is the live pre-state `/runs/{run_id}` payload
(`.workplans/l1/pre/run.json`, 2026-09-24): 22 `hydro.hydro_run` columns plus
`river_network_version_id`, `basin_id`, `source`.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

from apps.api.main import app
from apps.api.routes import forecast as forecast_routes
from packages.common.forecast_store import HYDRO_RUN_PUBLIC_COLUMNS, PsycopgForecastStore
from tests.test_openapi_drift import _openapi_spec

LIVE_PUBLIC_RUN_KEYS = (
    "run_id",
    "run_type",
    "scenario_id",
    "model_id",
    "basin_version_id",
    "forcing_version_id",
    "init_state_id",
    "source_id",
    "cycle_time",
    "start_time",
    "end_time",
    "status",
    "slurm_job_id",
    "run_manifest_uri",
    "output_uri",
    "log_uri",
    "error_code",
    "error_message",
    "created_at",
    "updated_at",
    "run_key",
    "parsed_at",
    "river_network_version_id",
    "basin_id",
    "source",
)
INTERNAL_KEYS = ("timeseries_store", "authority_internal")
RUN_ID = "fcst_gfs_2026092312_dg_model"


def _post_i7_row(run_id: str = RUN_ID) -> dict[str, Any]:
    """A `hydro.hydro_run` row after a hypothetical internal-column migration."""
    stamp = datetime(2026, 9, 24, 6, 34, 30, 305969, tzinfo=UTC)
    return {
        "run_id": run_id,
        "run_type": "forecast",
        "scenario_id": "forecast_gfs_deterministic",
        "model_id": "dg_model",
        "basin_version_id": "basins_huaiyss_vbasins",
        "forcing_version_id": "forc_gfs_2026092312_dg_model",
        "init_state_id": None,
        "source_id": "gfs",
        "cycle_time": datetime(2026, 9, 23, 12, tzinfo=UTC),
        "start_time": datetime(2026, 9, 23, 12, tzinfo=UTC),
        "end_time": datetime(2026, 9, 30, 12, tzinfo=UTC),
        "status": "published",
        "slurm_job_id": None,
        "run_manifest_uri": f"s3://nhms/runs/{run_id}/input/manifest.json",
        "output_uri": f"s3://nhms/runs/{run_id}/output/",
        "log_uri": None,
        "error_code": None,
        "error_message": None,
        "created_at": stamp,
        "updated_at": stamp,
        "run_key": 23188,
        "parsed_at": stamp,
        "timeseries_store": "narrow",
        "authority_internal": "reader-routing-secret",
        "river_network_version_id": "basins_huaiyss_rivnet_vbasins",
        "basin_id": "basins_huaiyss",
        "source": "gfs_adapter",
    }


class _Cursor:
    """Records every statement; answers `SELECT COUNT(*)` and row selects."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.statements: list[str] = []
        self._last = ""

    def execute(self, statement: str, parameters: Any = None) -> None:
        del parameters
        self.statements.append(statement)
        self._last = statement

    def fetchone(self) -> dict[str, Any]:
        assert "COUNT(" in self._last
        return {"total_count": len(self.rows)}

    def fetchall(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.rows]


class _Transaction:
    def __init__(self, cursor: _Cursor) -> None:
        self.cursor = cursor

    def __enter__(self) -> _Cursor:
        return self.cursor

    def __exit__(self, *_args: Any) -> bool:
        return False


class _SqlShapeStore(PsycopgForecastStore):
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        super().__init__("postgresql://sql-shape-seam")
        object.__setattr__(self, "cursor", _Cursor(rows))

    def _transaction(self) -> Any:
        return _Transaction(self.cursor)


def _row_select(statements: list[str]) -> list[str]:
    return [
        statement for statement in statements if "FROM hydro.hydro_run h" in statement and "COUNT(*)" not in statement
    ]


def _projected_hydro_run_columns(statement: str) -> list[str]:
    select_list = statement.split("SELECT", 1)[1].split("FROM hydro.hydro_run h", 1)[0]
    return re.findall(r"\bh\.([a-z_]+)\b(?!\s*\))", select_list)


def test_get_run_and_list_runs_select_the_public_allowlist_by_name_never_h_star() -> None:
    store = _SqlShapeStore([_post_i7_row()])
    store.get_run(RUN_ID)
    store.list_runs(basin_id=None, source="GFS", cycle_time=None, status=None, limit=5, offset=0)

    selects = _row_select(store.cursor.statements)
    assert len(selects) == 2
    for statement in selects:
        assert not re.search(r"\bh\s*\.\s*\*", statement), statement
        assert "timeseries_store" not in statement
        assert _projected_hydro_run_columns(statement) == list(LIVE_PUBLIC_RUN_KEYS[:22])
        assert "mi.river_network_version_id" in statement
        assert "bv.basin_id" in statement
        assert "COALESCE(ds.adapter_name, h.source_id) AS source" in statement


def test_allowlist_constant_is_the_live_public_column_set() -> None:
    assert HYDRO_RUN_PUBLIC_COLUMNS == LIVE_PUBLIC_RUN_KEYS[:22]


@pytest.mark.parametrize("call", ["get_run", "list_runs"])
def test_store_serializer_drops_internal_columns_and_keeps_every_public_field(call: str) -> None:
    store = _SqlShapeStore([_post_i7_row()])
    if call == "get_run":
        run = store.get_run(RUN_ID)
    else:
        (run,) = store.list_runs(basin_id=None, source=None, cycle_time=None, status=None, limit=5, offset=0)["items"]

    assert tuple(run) == LIVE_PUBLIC_RUN_KEYS
    assert not set(INTERNAL_KEYS) & set(run)
    assert run["cycle_time"] == "2026-09-23T12:00:00Z"
    assert run["created_at"] == "2026-09-24T06:34:30.305969Z"
    assert run["run_key"] == 23188
    assert run["source"] == "gfs_adapter"


@pytest.fixture
def client() -> Any:
    yield TestClient(app)
    app.dependency_overrides.pop(forecast_routes.get_forecast_store, None)


def _assert_public_run(run: dict[str, Any]) -> None:
    assert set(run) == set(LIVE_PUBLIC_RUN_KEYS)
    assert run["run_id"] == RUN_ID
    assert run["run_key"] == 23188
    assert run["parsed_at"] == "2026-09-24T06:34:30.305969Z"


def test_both_endpoints_drop_internal_columns_through_the_real_store(client: TestClient) -> None:
    app.dependency_overrides[forecast_routes.get_forecast_store] = lambda: _SqlShapeStore([_post_i7_row()])

    detail = client.get(f"/api/v1/runs/{RUN_ID}")
    listing = client.get("/api/v1/runs", params={"source": "gfs", "limit": 5})

    assert detail.status_code == 200, detail.text
    assert listing.status_code == 200, listing.text
    _assert_public_run(detail.json()["data"])
    (item,) = listing.json()["data"]["items"]
    _assert_public_run(item)


class _LeakingStore:
    """A store double that bypasses `_hydro_run_response`: the route model is the last guard."""

    def get_run(self, run_id: str) -> dict[str, Any]:
        return forecast_routes_row(run_id)

    def list_runs(self, **kwargs: Any) -> dict[str, Any]:
        return {"total_count": 1, "items": [forecast_routes_row(RUN_ID)], "limit": kwargs["limit"], "offset": 0}


def forecast_routes_row(run_id: str) -> dict[str, Any]:
    row = _post_i7_row(run_id)
    return {
        key: (value.isoformat().replace("+00:00", "Z") if isinstance(value, datetime) else value)
        for key, value in row.items()
    }


def test_route_model_drops_internal_keys_a_store_leaks(client: TestClient) -> None:
    app.dependency_overrides[forecast_routes.get_forecast_store] = _LeakingStore

    detail = client.get(f"/api/v1/runs/{RUN_ID}")
    listing = client.get("/api/v1/runs", params={"basin_id": "leak-check", "limit": 5})

    assert detail.status_code == 200, detail.text
    _assert_public_run(detail.json()["data"])
    (item,) = listing.json()["data"]["items"]
    _assert_public_run(item)
    assert set(listing.json()["data"]) == {"items", "total", "total_count", "limit", "offset"}


def test_run_detail_and_list_publish_the_same_closed_hydro_run_contract() -> None:
    for spec in (_openapi_spec(), app.openapi()):
        hydro_run = spec["components"]["schemas"]["HydroRun"]
        assert hydro_run["additionalProperties"] is False
        assert set(hydro_run["properties"]) == set(LIVE_PUBLIC_RUN_KEYS)
        assert not set(INTERNAL_KEYS) & set(hydro_run["properties"])
        detail = spec["paths"]["/api/v1/runs/{run_id}"]["get"]["responses"]["200"]
        schema = detail["content"]["application/json"]["schema"]
        assert schema["allOf"][0] == {"$ref": "#/components/schemas/SuccessEnvelope"}
        assert schema["allOf"][1]["properties"]["data"] == {"$ref": "#/components/schemas/HydroRun"}
        page = spec["components"]["schemas"]["HydroRunPage"]
        assert page["properties"]["items"]["items"] == {"$ref": "#/components/schemas/HydroRun"}


def test_route_passes_db_enum_labels_outside_the_published_enum_through_verbatim(client: TestClient) -> None:
    # `enum_range(NULL::hydro.run_status)` carries `frequency_done`: retired, a
    # convergence-only ledger member since 000062, never written, and absent from
    # the published `RunStatus`. A DB label the published enum does not know must
    # pass through verbatim, as it did on master, never 500 the page.
    # `reforecast` stands in for a future run_type.
    drifted = {**_post_i7_row(), "status": "frequency_done", "run_type": "reforecast"}
    app.dependency_overrides[forecast_routes.get_forecast_store] = lambda: _SqlShapeStore([drifted])

    detail = client.get(f"/api/v1/runs/{RUN_ID}")
    listing = client.get("/api/v1/runs", params={"basin_id": "drifted-label", "limit": 5})

    assert detail.status_code == 200, detail.text
    assert listing.status_code == 200, listing.text
    (item,) = listing.json()["data"]["items"]
    for run in (detail.json()["data"], item):
        _assert_public_run(run)
        assert run["status"] == "frequency_done"
        assert run["run_type"] == "reforecast"
