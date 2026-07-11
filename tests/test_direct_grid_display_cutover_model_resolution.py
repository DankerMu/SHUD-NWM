"""``model_id`` propagation audit — no display surface pins across a cutover.

Change: ``direct-grid-display-cutover`` — Epic #992 SUB-6 (§3.1 /
``active-model-dynamic-resolution`` ADDED requirement "No display surface
pins or caches ``model_id`` across a cutover").

This suite closes the five ``model_id`` propagation paths identified by
the SUB-6 audit and locks each with a regression test. There is NO
server-side active-model resolution on the station-series path by design
— ``model_id`` is a required client-supplied filter — and this suite
locks that property rather than inventing a server-side resolver.

Path 1 — station-series endpoint required-filter contract
    The ``/api/v1/met/stations/{station_id}/series`` Query default for
    ``model_id`` is literal ``None``; the object-store read layer raises
    ``MISSING_REQUIRED_FILTER`` when the client omits it. No server-side
    "active-model" fallback lives on this route.

Path 2 — MVT tile cache key derivation
    ``apps/api/routes/hydro_display.py::_station_source_version`` derives
    the tile cache key exclusively from the ``active_flag=true`` station
    source identity — the SQL literal references no ``model_id`` column
    and the key self-invalidates when the SUB-1 flip flips
    ``active_flag``.

Path 3 — explicit historical ``(cycle, model_id)`` route
    An explicit per-request ``model_id`` still resolves the immutable
    historical asset (mirror of the SUB-5 answerability contract, framed
    here on the "explicit legal historical model_id" scenario).

Path 4 / Path 5 — frontend live ``model_id`` resolution and no-pin
    Frontend claims are closed by the extended
    ``M11StationForcingPopup.test.tsx`` suite (``product.model_id`` at
    request time; no reuse across two live requests when the latest
    product changes). Backend guardrail here: the object-store read path
    and the ``PsycopgStationLookup`` MUST NOT reference any
    "active-model resolver" identifier — the frontend is the sole owner
    of live ``model_id`` sourcing.
"""

from __future__ import annotations

import inspect
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from apps.api.routes import data_sources as data_sources_module
from apps.api.routes.data_sources import get_met_station_series
from apps.api.routes.hydro_display import _station_source_version
from packages.common.forecast_store import ForecastStoreError
from packages.common.object_store_forcing import (
    PsycopgStationLookup,
    StationMetadata,
    _compute_cycle_compact,
    _normalize_source_id,
    _resolve_disk_path,
    raise_station_not_found,
    read_station_forcing_csv,
)

# --- fixtures shared across paths -----------------------------------------

HISTORICAL_STATION_ID = "m0_legacy_forc_042"
HISTORICAL_BASIN_VERSION_ID = "basins_heihe_vbasins"
HISTORICAL_SOURCE_ID = "ifs"
HISTORICAL_MODEL_ID = "basins_heihe_shud_v0"
HISTORICAL_CYCLE_TIME = datetime(2026, 5, 15, 0, tzinfo=UTC)
HISTORICAL_FORCING_FILENAME = "X100.75Y37.65.csv"


class _FakeStationLookup:
    """In-memory ``StationLookup`` returning a single fixed row.

    Mirrors the SUB-4 / SUB-5 exemplar seams — no DB, no ``active_flag``
    filtering. The read path resolves the row and then resolves the disk
    asset by (source, cycle, basin_version_id, model_id, filename).
    """

    def __init__(self, station: StationMetadata) -> None:
        self._station = station

    def lookup(self, station_id: str) -> StationMetadata:
        if station_id != self._station.station_id:
            raise_station_not_found(station_id)
        return self._station


def _historical_station(*, active_flag: bool | None) -> StationMetadata:
    return StationMetadata(
        station_id=HISTORICAL_STATION_ID,
        basin_version_id=HISTORICAL_BASIN_VERSION_ID,
        station_name="HEIHE M0 legacy forcing station 042",
        longitude=100.75,
        latitude=37.65,
        elevation_m=0.0,
        station_role="forcing_grid",
        active_flag=active_flag,
        properties_json={"forcing_filename": HISTORICAL_FORCING_FILENAME},
    )


def _write_historical_csv_at(root: Path, *, station: StationMetadata) -> Path:
    path = _resolve_disk_path(
        root,
        _normalize_source_id(HISTORICAL_SOURCE_ID),
        _compute_cycle_compact(HISTORICAL_CYCLE_TIME),
        station.basin_version_id,
        HISTORICAL_MODEL_ID,
        station.forcing_filename or HISTORICAL_FORCING_FILENAME,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "1\t6\t20260515\t20260522\n"
        "Time_Day\tPrecip\tTemp\tRH\tWind\tRN\n"
        "0\t2.500\t273.150\t0.620\t3.200\t180.000\n",
        encoding="utf-8",
    )
    return path


# =========================================================================
# Path 1 — station-series endpoint required-filter contract
# =========================================================================


def test_station_series_missing_model_id_returns_missing_required_filter_via_object_store_layer(
    tmp_path: Path,
) -> None:
    """(P1) ``read_station_forcing_csv`` raises ``MISSING_REQUIRED_FILTER`` on absent ``model_id``.

    The route's Query default is ``None`` (locked by the sibling test
    below); the object-store read layer is what raises the 422 error.
    Directly exercising ``read_station_forcing_csv`` here locks the leaf
    behavior independent of FastAPI dependency wiring.
    """
    station = _historical_station(active_flag=True)

    with pytest.raises(ForecastStoreError) as excinfo:
        read_station_forcing_csv(
            station_lookup=_FakeStationLookup(station),
            object_store_root=tmp_path,
            station_id=HISTORICAL_STATION_ID,
            model_id=None,  # type: ignore[arg-type]
            source_id=HISTORICAL_SOURCE_ID,
            cycle_time=HISTORICAL_CYCLE_TIME,
        )

    error = excinfo.value
    assert error.status_code == 422
    assert error.code == "MISSING_REQUIRED_FILTER"
    assert error.details == {
        "required_alternatives": [
            ["forcing_version_id"],
            ["model_id", "source_id", "cycle_time"],
        ]
    }


def test_data_sources_route_has_no_server_side_active_model_default_for_model_id() -> None:
    """(P1) The station-series route's ``model_id`` Query default is literal ``None``.

    Byte-shape lock on ``apps/api/routes/data_sources.py`` — the route
    body MUST NOT resolve ``model_id`` from an "active-model" lookup and
    MUST NOT set a server-side default. Any future refactor that
    introduces ``resolve_active_model`` / ``active_model_for_basin`` /
    ``latest_active_model`` on this route would silently pin the display
    to a captured ``model_id`` across a cutover and break the SUB-6
    invariant.
    """
    source = inspect.getsource(get_met_station_series)

    # Query default lock — the exact ``model_id: str | None = Query(default=None``
    # signature the route currently carries.
    assert re.search(
        r"model_id:\s*str\s*\|\s*None\s*=\s*Query\(\s*default=None",
        source,
    ) is not None, (
        "expected 'model_id: str | None = Query(default=None, ...)' on "
        "the station-series route — a server-side default would pin "
        "model_id across a cutover"
    )

    # Negative predicate: no "active-model" resolver identifier appears
    # anywhere in the route body. Case-insensitive so an uppercase-
    # mutated identifier does not evade the check.
    banned_identifier_patterns = (
        r"\bresolve_active_model\b",
        r"\bactive_model_for_basin\b",
        r"\blatest_active_model\b",
        r"\bresolve_current_model\b",
        r"\bget_active_model\b",
    )
    for pattern in banned_identifier_patterns:
        assert re.search(pattern, source, re.IGNORECASE) is None, (
            f"forbidden active-model resolver identifier in "
            f"get_met_station_series: {pattern!r} — the route must keep "
            "model_id a required client-supplied filter"
        )

    # Sibling lock: the module-level identifier surface also does not
    # import an "active-model" resolver. A local ``from ... import
    # resolve_active_model`` inside a nested helper would sneak past the
    # function-source check; the module-source check closes that hole.
    module_source = inspect.getsource(data_sources_module)
    for pattern in banned_identifier_patterns:
        assert re.search(pattern, module_source, re.IGNORECASE) is None, (
            f"forbidden active-model resolver identifier imported into "
            f"apps/api/routes/data_sources.py: {pattern!r}"
        )


# =========================================================================
# Path 2 — MVT tile cache key derivation
# =========================================================================


def test_station_source_version_sql_does_not_reference_model_id() -> None:
    """(P2) ``_station_source_version`` SQL literal references no ``model_id`` column.

    The tile cache key is derived from station inventory identity alone
    (``station_id, basin_version_id, station_name, station_role,
    active_flag, geom, created_at``). Adding a ``model_id`` predicate or
    projected column would pin the tile key to a captured ``model_id``
    and break the flip's self-invalidation contract (SUB-1 test 7 covers
    the sibling ``basin_version_id + active_flag=true`` invariant; this
    test adds the "no ``model_id`` reference" positive claim).

    Case-insensitive matching guards against an uppercase-mutated
    ``MODEL_ID`` column reference (PostgreSQL folds unquoted identifiers
    to lowercase at parse time, so an uppercase form would compile but
    would evade a case-sensitive substring check).
    """
    source = inspect.getsource(_station_source_version)

    # Positive anchor: both branches (SQLite dev + PostGIS prod) still
    # resolve from ``met.met_station`` keyed by ``basin_version_id``.
    # This surfaces a SUT rename rather than silently passing.
    assert "met.met_station" in source, (
        "expected _station_source_version to query met.met_station "
        "(SUT anchor moved?)"
    )
    assert "basin_version_id = :basin_version_id" in source, (
        "expected _station_source_version to key on basin_version_id "
        "(SUT anchor moved?)"
    )

    # Negative predicate: no ``model_id`` reference anywhere in the
    # function body — column, projection, WHERE clause, or bind param.
    assert re.search(r"model_id", source, re.IGNORECASE) is None, (
        "_station_source_version must not reference model_id — the tile "
        "cache key is derived from station inventory identity alone "
        "(SUB-6 §3.1: 'MVT tile cache key derived from active_flag=true "
        "station source identity so it self-invalidates on flip')"
    )


class _FakeSessionResult:
    """Minimal ``session.execute(...)`` result stub returning fixed rows."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def mappings(self) -> "_FakeSessionResult":
        return self

    def all(self) -> list[dict[str, Any]]:
        return list(self._rows)


class _FakeBind:
    """Minimal ``session.get_bind()`` returning a dialect stub."""

    class _Dialect:
        name = "sqlite"

    dialect = _Dialect()


class _FakeSession:
    """In-memory ``sqlalchemy.orm.Session`` seam for ``_station_source_version``.

    The SUT calls ``session.get_bind().dialect.name`` (routes SQLite vs
    PostGIS) and ``session.execute(text(...), params).mappings().all()``.
    We satisfy both surfaces and return the constructor-supplied rows.
    """

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def get_bind(self) -> _FakeBind:
        return _FakeBind()

    def execute(self, _stmt: Any, _params: Any) -> _FakeSessionResult:
        return _FakeSessionResult(self._rows)


def _station_row(
    *,
    station_id: str,
    active_flag: int,
    basin_version_id: str = HISTORICAL_BASIN_VERSION_ID,
) -> dict[str, Any]:
    return {
        "station_id": station_id,
        "basin_version_id": basin_version_id,
        "station_name": f"{station_id} name",
        "station_role": "forcing_grid",
        "active_flag": active_flag,
        "geom": "0101000020e6100000",  # opaque hex placeholder; identity-relevant only
        "created_at": datetime(2026, 5, 1, 0, tzinfo=UTC),
    }


def test_station_source_version_key_self_invalidates_when_active_flag_flips() -> None:
    """(P2) Tile cache key changes when the station-source inventory flips.

    The SUT hashes the projected row set; two inventories that differ
    ONLY in the ``active_flag`` column of one row MUST produce different
    cache-key strings. This is the runtime lock on the "self-invalidates
    on flip" property; combined with the byte-shape test above, it
    closes the P2 claim end-to-end.
    """
    # Pre-flip: two rows active (identity == "before")
    pre_flip_rows = [
        _station_row(station_id="alpha", active_flag=1),
        _station_row(station_id="beta", active_flag=1),
    ]
    # Post-flip: same rows, but "beta" has been flipped to inactive. The
    # ``_station_source_version`` SQL WHERE clause filters
    # ``active_flag = 1`` (SQLite branch), so the fake ``execute`` MUST
    # already reflect the WHERE-filtered rows. That means the post-flip
    # row set is just [alpha] — the whole point of the test.
    post_flip_rows = [
        _station_row(station_id="alpha", active_flag=1),
    ]

    pre_key = _station_source_version(
        _FakeSession(pre_flip_rows), HISTORICAL_BASIN_VERSION_ID  # type: ignore[arg-type]
    )
    post_key = _station_source_version(
        _FakeSession(post_flip_rows), HISTORICAL_BASIN_VERSION_ID  # type: ignore[arg-type]
    )

    assert pre_key != post_key, (
        "expected tile cache key to change when the station-source "
        "inventory flips (self-invalidates on flip); a stable key would "
        "serve stale tiles across a cutover"
    )
    # The key prefix and basin scope survive the flip — only the digest
    # and row-count segments should differ. This proves the key format
    # is still ``met-stations:<digest>:<basin_version_id>:<len>``.
    assert pre_key.startswith("met-stations:")
    assert post_key.startswith("met-stations:")
    assert pre_key.endswith(f":{HISTORICAL_BASIN_VERSION_ID}:2")
    assert post_key.endswith(f":{HISTORICAL_BASIN_VERSION_ID}:1")


# =========================================================================
# Path 3 — explicit historical ``(cycle, model_id)`` route
# =========================================================================


def test_explicit_historical_model_id_still_resolves_via_read_path(
    tmp_path: Path,
) -> None:
    """(P3) Explicit per-request ``model_id`` resolves the immutable historical asset.

    Mirror of the SUB-5 answerability contract, framed here on the
    SUB-6 "explicit legal historical ``model_id``" scenario: a client
    passing an old ``model_id`` on a pre-cutover cycle MUST resolve the
    historical file (which was written by the pre-cutover producer).
    This is the "legal immutable-asset resolution" branch — the
    non-goal in tasks.md §3.1 explicitly preserves this path.
    """
    active_station = _historical_station(active_flag=True)
    _write_historical_csv_at(tmp_path, station=active_station)

    response = read_station_forcing_csv(
        station_lookup=_FakeStationLookup(active_station),
        object_store_root=tmp_path,
        station_id=HISTORICAL_STATION_ID,
        model_id=HISTORICAL_MODEL_ID,
        source_id=HISTORICAL_SOURCE_ID,
        cycle_time=HISTORICAL_CYCLE_TIME,
    )

    # The response echoes the requested ``model_id`` — the read path
    # does NOT rewrite the caller's ``model_id`` to some resolved
    # "active" value. This is the SUB-6 P3 lock.
    assert response["model_id"] == HISTORICAL_MODEL_ID
    assert response["station_id"] == HISTORICAL_STATION_ID
    assert response["cycle_time"] == HISTORICAL_CYCLE_TIME.isoformat().replace(
        "+00:00", "Z"
    ) or response["cycle_time"] == HISTORICAL_CYCLE_TIME
    assert len(response["series"]) >= 1
    assert sum(len(item["points"]) for item in response["series"]) >= 1


# =========================================================================
# Path 4 / Path 5 — backend guardrail (frontend claims live in
# apps/frontend/src/components/map/__tests__/M11StationForcingPopup.test.tsx)
# =========================================================================


def test_object_store_forcing_never_calls_active_model_resolver() -> None:
    """(P4/P5 backend guardrail) The read path never resolves an "active model".

    The frontend is the sole owner of live ``model_id`` sourcing (via
    ``fetchHydroMetLatestProduct`` at request time — locked by the
    extended M11StationForcingPopup suite). The backend read path MUST
    keep ``model_id`` a caller-supplied argument and MUST NOT introduce
    an "active-model resolver" that would silently pin a captured value
    across a cutover.

    Case-insensitive matching guards against uppercase-mutated
    identifiers. The exact set of banned identifiers mirrors the P1
    Query-default lock — any name here would represent server-side
    active-model resolution and break the SUB-6 invariant.
    """
    banned_identifier_patterns = (
        r"\bresolve_active_model\b",
        r"\bactive_model_for_basin\b",
        r"\blatest_active_model\b",
        r"\bresolve_current_model\b",
        r"\bget_active_model\b",
    )

    for target in (read_station_forcing_csv, PsycopgStationLookup._lookup_with_cursor):
        source = inspect.getsource(target)
        for pattern in banned_identifier_patterns:
            assert re.search(pattern, source, re.IGNORECASE) is None, (
                f"forbidden active-model resolver identifier in "
                f"{target.__qualname__}: {pattern!r} — the backend read "
                "path must keep model_id caller-supplied"
            )
