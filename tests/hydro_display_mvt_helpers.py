"""Shared doubles, fixtures and landmark literals for the ``hydro`` MVT suites.

Non-collectible support module (#2074): the single home of every helper the
``tests/test_hydro_display_mvt_scaling*.py`` partitions share, so none of them
can drift from another's copy.

Load-bearing details, each proved live before it was written down:

* ``_dual_patch`` patches a national-discharge read on BOTH
  ``hydro_display`` and ``hydro_display_catalog`` (#2026). Both halves bite:
  ``_default_layer_catalog`` resolves these names from the catalog module's
  globals while the route handlers resolve them from the facade. Collapsing it
  to one ``setattr`` leaves the other path running the real SQL.
* ``_TILE_ROUTE_LOGGER`` is the literal logger name the ``#2030`` budget
  warnings are emitted under, which survived ``_fetch_postgis_tile_bytes``
  moving to ``apps/api/routes/hydro_display_postgis.py`` (design Appendix C).
  ``_truncation_records`` / ``_blanked_records`` filter ``caplog`` by it and
  back NEGATIVE assertions, so a wrong name here passes vacuously.
* ``_keep_fixed_instant_fixtures_inside_the_cycle_lookback`` is autouse and
  applied to EVERY case of the original single module. Each partition imports
  it so that reach is unchanged; ``_REAL_CYCLE_LOOKBACK_DAYS`` is read at import
  time, before the fixture patches the constant.
* ``_SOURCE_CONJUNCT`` / ``_CYCLE_CONJUNCT`` keep their leading ``AND (``; see
  the comment above them.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from apps.api import main
from apps.api.routes import hydro_display, hydro_display_catalog
from services.tiles import mvt as mvt_module
from services.tiles.mvt import TileInput, TileResponse, cache_key, canonical_mvt_time


class _Rows:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows
        self.first_count = 0

    def mappings(self) -> _Rows:
        return self

    def all(self) -> list[dict[str, Any]]:
        return self._rows

    def first(self) -> dict[str, Any] | None:
        self.first_count += 1
        return self._rows[0] if self._rows else None


class _Session:
    def __init__(self, rows: list[dict[str, Any]], dialect: str = "postgresql") -> None:
        self.rows = rows
        self.sql = ""
        self.executions: list[tuple[str, Any]] = []
        self.bind = SimpleNamespace(dialect=SimpleNamespace(name=dialect))
        self.results: list[_Rows] = []

    def execute(self, statement: Any, _params: Any = None) -> _Rows:
        self.sql = str(statement)
        self.executions.append((self.sql, _params))
        result = _Rows(self.rows)
        self.results.append(result)
        return result

    def get_bind(self) -> Any:
        return self.bind


# #2030: the tile route's logger tree; `apps.api.routes.hydro_display` propagates
# to the root, which is where `caplog` attaches.
_TILE_ROUTE_LOGGER = "apps.api.routes.hydro_display"


def _dual_patch(monkeypatch: Any, name: str, value: Any) -> None:
    """#2026: patch a national-discharge read in BOTH of its homes.

    `_default_layer_catalog` moved to `apps/api/routes/hydro_display_catalog.py`
    and resolves these names from THAT module's globals; the `/api/v1/layers*` and
    `hydro-national` route handlers stayed on the facade and resolve them from
    there. Patching one home leaves the other path running the real SQL, so both
    are patched at every site. Over-patching cannot produce a silent pass.
    """
    monkeypatch.setattr(hydro_display, name, value)
    monkeypatch.setattr(hydro_display_catalog, name, value)


def _truncation_records(caplog: Any) -> list[Any]:
    return [record for record in caplog.records if "MVT_TILE_BUDGET_TRUNCATED" in record.getMessage()]


def _budget_row(coordinate_count: int) -> dict[str, Any]:
    # #2030: the four truncation-signal columns default to the untruncated state
    # (intersecting == selected, no overflow), so every pre-existing caller of
    # this helper keeps its 413/200 verdict *and* stays silent.
    return {
        "tile": b"pbf-bytes",
        "feature_count": 12,
        "coordinate_count": coordinate_count,
        "source_identity_count": 1,
        "invalid_property_count": 0,
        "invalid_properties": "",
        "intersecting_feature_count": 12,
        "intersecting_coordinate_count": coordinate_count,
        "feature_coordinate_overflow_count": 0,
        "coordinate_dimension_overflow_count": 0,
    }


_NATIONAL_CYCLE = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
_NATIONAL_VALID_TIME = datetime(2026, 9, 3, 0, 0, tzinfo=UTC)
_NATIONAL_TILE_Z = 4
_NATIONAL_TILE_X = 13
_NATIONAL_TILE_Y = 6
_NATIONAL_ROUTE_PREFIX = "/api/v1/tiles/hydro-national"


# The identity pair verbatim, INCLUDING the leading `AND (`. Asserting only the
# inner `CAST(...) IS NULL OR ...` half leaves the conjunct/disjunct distinction
# unpinned, and that distinction is the whole predicate: SQL's AND binds tighter
# than OR, so `... AND mi.active_flag OR (guard OR match) AND (guard OR match)`
# parses as `(everything unbound) OR (both matches)` and admits EVERY candidate
# run again. Measured: with the digest's `AND (` flipped to `OR  (`, all three
# of `national_discharge_source_version(session)`,
# `...(source="gfs", cycle=<early>)` and `...(source="gfs", cycle=<late>)`
# collapse onto one value against a real database, and the whole unit suite
# stayed green before these constants existed.
_SOURCE_CONJUNCT = "AND (CAST(:source AS text) IS NULL OR lower(h.source_id) = :source)"
_CYCLE_CONJUNCT = "AND (CAST(:cycle AS timestamptz) IS NULL OR h.cycle_time = :cycle)"


class _CapturingSession(_Session):
    def __init__(self, rows: list[dict[str, Any]], dialect: str = "postgresql") -> None:
        super().__init__(rows, dialect=dialect)
        self.params: list[dict[str, Any]] = []

    def execute(self, statement: Any, params: Any = None) -> _Rows:
        self.params.append(dict(params or {}))
        return super().execute(statement, params)


class _TileResult:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def mappings(self) -> _TileResult:
        return self

    def all(self) -> list[dict[str, Any]]:
        return self._rows

    def first(self) -> dict[str, Any] | None:
        return self._rows[0] if self._rows else None


class _NationalRouteSession:
    """Answers every statement the national routes issue, and records their SQL and binds.

    #2153 added the per-cycle coverage pair to the canonical route's miss path,
    EXECUTED since #2087 as coverage rows then active set (`_statement_kinds`
    pins that). Classification is a separate, stricter-check-first dispatch order
    and is NOT the execution order: the tile (`ST_AsMVT`), the digest
    (`geometry_generation` -- `national_river_network_source_version` also
    mentions `core.model_instance mi`, so this check must precede the active-set
    one), the coverage rows (their `PARTITION BY`), the active set
    (`core.model_instance mi` without `hydro.hydro_run`). Anything else -- the
    per-basin sibling digests -- keeps landing on the digest recorder, as before.
    The defaults answer active = coverage = `{rnv_a}`, a fully covered identity,
    so every pre-#2153 200 case keeps its meaning.
    """

    _DIGEST_ROWS = [
        {
            "run_id": "run_a",
            "river_network_version_id": "rnv_a",
            "cycle_time": "2026-09-02T12:00:00Z",
            "updated_at": "2026-09-02T13:00:00Z",
        }
    ]
    _TILE_ROW = {
        "tile": b"pbf-bytes",
        "source_identity_count": 1,
        "source_feature_count": 1,
        "feature_count": 1,
        "coordinate_count": 2,
        "feature_coordinate_overflow_count": 0,
        "coordinate_dimension_overflow_count": 0,
        "invalid_property_count": 0,
        "invalid_properties": None,
    }

    def __init__(
        self,
        digest_rows: list[dict[str, Any]] | None = None,
        *,
        active_networks: tuple[str, ...] = ("rnv_a",),
        coverage_networks: tuple[str, ...] = ("rnv_a",),
        tile_row: dict[str, Any] | None = None,
    ) -> None:
        self.bind = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))
        self.tile_params: list[dict[str, Any]] = []
        self.digest_params: list[dict[str, Any]] = []
        self.active_params: list[dict[str, Any]] = []
        self.coverage_params: list[dict[str, Any]] = []
        # `(kind, sql)` in execution order, so a case can assert statement order.
        self.statements: list[tuple[str, str]] = []
        # Per instance, never by mutating `_DIGEST_ROWS`: the class attribute is
        # shared by every other case in this file.
        self.digest_rows = self._DIGEST_ROWS if digest_rows is None else digest_rows
        self.active_networks = active_networks
        self.coverage_networks = coverage_networks
        self.tile_row = dict(self._TILE_ROW) if tile_row is None else tile_row

    def execute(self, statement: Any, params: Any = None) -> _TileResult:
        sql = str(statement)
        bound = dict(params or {})
        if "ST_AsMVT" in sql:
            self.statements.append(("tile", sql))
            self.tile_params.append(bound)
            return _TileResult([dict(self.tile_row)])
        if "geometry_generation" not in sql:
            if "PARTITION BY mi.river_network_version_id, h.cycle_time" in sql:
                self.statements.append(("coverage", sql))
                self.coverage_params.append(bound)
                return _TileResult(
                    [
                        {"river_network_version_id": network, "cycle_time": bound.get("cycle")}
                        for network in self.coverage_networks
                    ]
                )
            if "core.model_instance mi" in sql and "hydro.hydro_run" not in sql:
                self.statements.append(("active", sql))
                self.active_params.append(bound)
                return _TileResult([{"river_network_version_id": network} for network in self.active_networks])
        # Otherwise `national_discharge_source_version`'s digest (or a sibling
        # route's); recording its binds is what lets a case assert the route
        # narrowed it to the requested identity (the tile binds alone cannot see
        # that call at all).
        self.statements.append(("digest", sql))
        self.digest_params.append(bound)
        return _TileResult([dict(row) for row in self.digest_rows])

    def get_bind(self) -> Any:
        return self.bind

    def rollback(self) -> None:
        return None


class _ExplodingSession:
    """Any use at all is a failure: validation must precede every statement."""

    def execute(self, *_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("a validation failure must not reach the database")

    def get_bind(self) -> Any:
        raise AssertionError("a validation failure must not reach the database")


def _national_identity_url(
    source: str,
    cycle: str,
    valid_time: str = "2026-09-03T00:00:00Z",
    variable: str = "q_down",
    z: int = _NATIONAL_TILE_Z,
    x: int = _NATIONAL_TILE_X,
    y: int = _NATIONAL_TILE_Y,
) -> str:
    return f"{_NATIONAL_ROUTE_PREFIX}/{source}/{cycle}/{variable}/{valid_time}/{z}/{x}/{y}.pbf"


def _request_national_identity_tile(
    url: str,
    session: Any,
    monkeypatch: Any,
    tmp_path: Any,
    *,
    cached: TileResponse | None = None,
    live_postgis: bool = True,
    built: list[bytes] | None = None,
) -> tuple[Any, list[TileInput]]:
    if live_postgis:
        monkeypatch.setenv("NHMS_ENABLE_LIVE_POSTGIS_MVT", "true")
    else:
        monkeypatch.delenv("NHMS_ENABLE_LIVE_POSTGIS_MVT", raising=False)
    monkeypatch.setenv("NHMS_MVT_FILE_CACHE_DIR", str(tmp_path))
    captured: list[TileInput] = []

    def fake_read(_session: object, tile: TileInput) -> TileResponse | None:
        captured.append(tile)
        return cached

    def fake_build(_session: object, tile: TileInput, data: bytes) -> TileResponse:
        if built is not None:
            built.append(data)
        return TileResponse(
            data=data,
            checksum="checksum",
            etag='W/"etag"',
            cache_key=cache_key(tile),
            cache_status="miss",
            layer_id=tile.layer_id,
        )

    monkeypatch.setattr(hydro_display, "read_cached_tile_response", fake_read)
    monkeypatch.setattr(hydro_display, "build_raw_tile_response", fake_build)

    app = main.create_app()
    app.dependency_overrides[hydro_display.get_hydro_display_session] = lambda: session
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get(url)
    finally:
        app.dependency_overrides.clear()
    return response, captured


def _legacy_national_url(valid_time: str = "2026-09-03T00:00:00Z") -> str:
    return (
        f"{_NATIONAL_ROUTE_PREFIX}/q_down/{valid_time}"
        f"/{_NATIONAL_TILE_Z}/{_NATIONAL_TILE_X}/{_NATIONAL_TILE_Y}.pbf"
    )


_CYCLE = datetime(2026, 9, 2, 12, tzinfo=UTC)
_PREVIOUS_CYCLE = datetime(2026, 9, 2, 6, tzinfo=UTC)
_INSTANT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


# `national_discharge_cycles` bounds its scan at `now() - LOOKBACK`, but the cases
# above and below are about intersection, ordering and spelling, and they express
# their expectations as FIXED instants. Left alone they would pass today and start
# failing the day wall-clock time walks past `_CYCLE + 12 days` -- a suite that
# rots on a calendar, not on a code change. So the window is widened for the whole
# module and the three cases that are actually ABOUT the bound put the real value
# back. `national_discharge_cycles` reads the module global on every call, so this
# reaches the route/catalog tests through `TestClient` as well.
_REAL_CYCLE_LOOKBACK_DAYS = mvt_module.NATIONAL_DISCHARGE_CYCLE_LOOKBACK_DAYS


@pytest.fixture(autouse=True)
def _keep_fixed_instant_fixtures_inside_the_cycle_lookback(monkeypatch: Any) -> None:
    monkeypatch.setattr(mvt_module, "NATIONAL_DISCHARGE_CYCLE_LOOKBACK_DAYS", 100_000)


def _coverage_row(
    *,
    network: str,
    cycle: datetime,
    start: datetime,
    end: datetime,
    source: str = "gfs",
    segment_count: int = 2,
    run_id: str | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    """One `hydro.run_display_coverage` row that proves a complete hourly rectangle."""
    lead_count = int((end - start).total_seconds()) // 3600 + 1
    row: dict[str, Any] = {
        "run_id": run_id or f"run-{network}-{cycle:%Y%m%d%H}",
        "basin_version_id": f"bv-{network}",
        "river_network_version_id": network,
        "cycle_time": cycle,
        "source_id": source,
        "segment_count": segment_count,
        "river_sample_count": segment_count * lead_count,
        "river_valid_time_start": start,
        "river_valid_time_end": end,
        "min_lead_time_hours": 0,
        "max_lead_time_hours": lead_count - 1,
    }
    row.update(overrides)
    return row


class _NationalDiscoverySession:
    """Answers the active-network query and the identity-bound coverage query."""

    def __init__(self, rows: list[dict[str, Any]], *, active_networks: list[str] | None = None) -> None:
        self.rows = rows
        self.active_networks = (
            active_networks
            if active_networks is not None
            else sorted({row["river_network_version_id"] for row in rows})
        )
        self.executions: list[tuple[str, Any]] = []
        self.bind = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

    def execute(self, statement: Any, params: Any = None) -> _Rows:
        sql = str(statement)
        self.executions.append((sql, params))
        if "core.model_instance mi" in sql and "hydro.hydro_run" not in sql:
            return _Rows([{"river_network_version_id": network} for network in self.active_networks])
        if "hydro.run_display_coverage" not in sql:
            # Anything else (`display_ready_run`, `_run_row`) finds nothing.
            return _Rows([])
        bound = params or {}
        source = bound.get("source")
        cycle = bound.get("cycle")
        since = bound.get("since")
        selected = []
        for row in self.rows:
            if source is not None and str(row["source_id"]).lower() != source:
                continue
            if cycle is not None and canonical_mvt_time(row["cycle_time"]) != canonical_mvt_time(cycle):
                continue
            # `h.cycle_time >= :since` in SQL, NULL semantics included: `NULL >= x`
            # is NULL, so a run with no cycle is dropped by a bound `:since` rather
            # than kept. Without this branch the lookback predicate could be deleted
            # from the statement and no behavioural test would notice.
            if since is not None and (row["cycle_time"] is None or row["cycle_time"] < since):
                continue
            # The real statement does not select `source_id`; neither does this.
            selected.append({key: value for key, value in row.items() if key != "source_id"})
        return _Rows(selected)

    def get_bind(self) -> Any:
        return self.bind


def _full_coverage_rows(cycle: datetime, networks: tuple[str, ...] = ("rn-a", "rn-b", "rn-c")) -> list[dict[str, Any]]:
    return [
        _coverage_row(network=network, cycle=cycle, start=cycle, end=cycle + timedelta(hours=168))
        for network in networks
    ]


def _entry(items: list[dict[str, Any]], layer_id: str) -> dict[str, Any]:
    return next(item for item in items if item["layer_id"] == layer_id)


_DETAIL_LAYER_IDS = {
    "river-network-national": "river-network-national",
    "river-network": "river-network",
    "hydro": "discharge",
    "hydro-national": "discharge",
    "met-stations": "met-stations",
}


def _blanked_records(caplog: Any) -> list[Any]:
    return [record for record in caplog.records if "MVT_TILE_FEATURE_OVERFLOW_BLANKED" in record.getMessage()]


_BLANKED_FIELDS = (
    "layer_id",
    "z",
    "x",
    "y",
    "feature_coordinate_overflow_count",
    "feature_coordinate_count",
    "max_feature_coordinates",
    "coordinate_dimension_overflow_count",
    "coordinate_dimension_count",
    "max_coordinate_dimensions",
)


def _assert_one_blanked_record(caplog: Any, session: _Session, layer: str, **expected: Any) -> None:
    records = _blanked_records(caplog)
    assert len(records) == 1, [record.getMessage() for record in records]
    record = records[0]
    assert record.levelno == logging.WARNING
    assert record.name == _TILE_ROUTE_LOGGER
    bind = session.executions[0][1]
    values = {name: getattr(record, name) for name in _BLANKED_FIELDS}
    assert values == {
        "layer_id": _DETAIL_LAYER_IDS[layer],
        "z": 3,
        "x": 6,
        "y": 3,
        "max_feature_coordinates": bind["feature_coordinate_limit"],
        "max_coordinate_dimensions": bind["max_coordinate_dimensions"],
        **expected,
    }
    message = record.getMessage()
    for name, value in values.items():
        assert f"{name}={value}" in message, (name, message)
