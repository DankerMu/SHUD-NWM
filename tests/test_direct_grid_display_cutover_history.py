"""Pre-cutover ``(cycle, model)`` answerability regression lock.

Change: ``direct-grid-display-cutover`` — Epic #992 SUB-5 (§2.2 /
``historical-cycle-display-degradation`` ADDED requirement
"Pre-cutover products and timeseries stay answerable by cycle and model key").

This suite is independent of the SUB-1 station-set flip AND compatible with
the SUB-4 B4 inactive-row 404-details desensitization: three claims are
locked so that after cutover a pre-cutover cycle keyed by its old
``model_id`` still resolves and no historical row is deleted or hidden.

Claim 1 — station/forcing timeseries by old ``model_id``
    A post-flip ``active_flag=false`` M0 legacy station whose requested
    pre-cutover file exists resolves through the immutable old-variant
    asset path (``packages/common/object_store_forcing.py::
    read_station_forcing_csv``) and returns its series. When the pre-
    cutover file is missing, the endpoint returns the SUB-4 desensitized
    ``STATION_FORCING_FILE_NOT_FOUND`` 404 (``details == {"station_id":
    ...}``) — never a series-hiding 404 that could be mistaken for
    "row deleted". The active-station baseline is preserved.

Claim 2 — ``PsycopgStationLookup._lookup_with_cursor`` SQL byte-shape lock
    The lookup SQL SHALL select ``active_flag`` in the projection but MUST
    NOT filter on it in the WHERE clause. Filtering on ``active_flag``
    would 404 post-flip historical M0 reads whose files exist, breaking
    Claim 1 and contradicting the SUB-4 fix form pinned to 404-details
    desensitization (task 3.2 non-goal: "no ``active_flag`` filtering in
    the lookup").

Claim 3 — flow-product resolution is orthogonal to ``met.met_station.active_flag``
    The ``PsycopgForecastStore`` flow-product resolution methods
    (``_fetch_forecast_segment_rows`` and siblings) resolve by
    ``(basin_version_id, segment_id, river_network_version_id, cycle_time,
    model_id)`` from ``hydro.river_timeseries JOIN hydro.hydro_run`` and
    MUST NOT join ``met.met_station`` — so they cannot be affected by an
    ``active_flag`` flip on stations. The display-plane
    ``apps/api/routes/hydro_display.py::_run_row`` (the flow-product route
    resolution that dispatches into forecast_store) MUST also not join
    ``met.met_station``.

Claim 4 — flip is UPDATE-only, deletes/hides no historical row
    ``packages/common/station_set_flip.py::build_station_flag_flip_hook``
    issues ONLY two ``UPDATE met.met_station SET active_flag = ...``
    statements and MUST NOT contain ``DELETE`` / ``TRUNCATE``, and MUST
    NOT write to ``hydro.river_timeseries`` / ``hydro.hydro_run`` /
    ``met.forcing_station_timeseries``. This is the byte-level lock that
    proves "the flip deletes/hides no historical record" (spec scenario:
    "flip does not delete or hide historical products").
"""

from __future__ import annotations

import inspect
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from apps.api.routes import hydro_display
from packages.common.forecast_store import ForecastStoreError, PsycopgForecastStore
from packages.common.object_store_forcing import (
    PsycopgStationLookup,
    StationMetadata,
    _compute_cycle_compact,
    _normalize_source_id,
    _resolve_disk_path,
    raise_station_not_found,
    read_station_forcing_csv,
)
from packages.common.station_set_flip import build_station_flag_flip_hook

# --- pre-cutover / M0 legacy identity fixtures ----------------------------
#
# The naming below is deliberately explicit ("pre_cutover", "m0_legacy") so
# a grep from either side of the cutover boundary lands on the right suite.
# Values are chosen to be distinct from the SUB-4 exemplar so a copy-paste
# mistake between the two suites shows up in a diff.

PRE_CUTOVER_STATION_ID = "m0_legacy_forc_042"
PRE_CUTOVER_BASIN_VERSION_ID = "basins_heihe_vbasins"
PRE_CUTOVER_SOURCE_ID = "gfs"
PRE_CUTOVER_M0_MODEL_ID = "basins_heihe_m0_shud"
PRE_CUTOVER_CYCLE_TIME = datetime(2026, 5, 15, 0, tzinfo=UTC)
PRE_CUTOVER_FORCING_FILENAME = "X100.75Y37.65.csv"


class _FakeStationLookup:
    """In-memory ``StationLookup`` seam that returns a fixed metadata row.

    Mirrors the SUB-4 exemplar's fake — the lookup returns whatever row it
    was constructed with (inactive or active). This is the exact shape that
    would come back from ``PsycopgStationLookup._lookup_with_cursor`` if
    that SQL did NOT filter ``active_flag`` (Claim 2's byte-shape lock).
    """

    def __init__(self, station: StationMetadata) -> None:
        self._station = station

    def lookup(self, station_id: str) -> StationMetadata:
        if station_id != self._station.station_id:
            raise_station_not_found(station_id)
        return self._station


def _pre_cutover_station(*, active_flag: bool | None) -> StationMetadata:
    return StationMetadata(
        station_id=PRE_CUTOVER_STATION_ID,
        basin_version_id=PRE_CUTOVER_BASIN_VERSION_ID,
        station_name="HEIHE M0 legacy forcing station 042",
        longitude=100.75,
        latitude=37.65,
        elevation_m=0.0,
        station_role="forcing_grid",
        active_flag=active_flag,
        properties_json={"forcing_filename": PRE_CUTOVER_FORCING_FILENAME},
    )


def _write_pre_cutover_csv_at(root: Path, *, station: StationMetadata) -> Path:
    """Write a valid SHUD-shaped station forcing CSV at the historical path.

    Path components come exclusively from the pre-cutover
    ``(source, cycle, basin_version_id, m0_model_id, forcing_filename)``
    tuple — the same immutable-asset resolution the read path resolves
    when called with the old ``model_id``.
    """
    path = _resolve_disk_path(
        root,
        _normalize_source_id(PRE_CUTOVER_SOURCE_ID),
        _compute_cycle_compact(PRE_CUTOVER_CYCLE_TIME),
        station.basin_version_id,
        PRE_CUTOVER_M0_MODEL_ID,
        station.forcing_filename or PRE_CUTOVER_FORCING_FILENAME,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "1\t6\t20260515\t20260522\n"
        "Time_Day\tPrecip\tTemp\tRH\tWind\tRN\n"
        "0\t2.500\t273.150\t0.620\t3.200\t180.000\n",
        encoding="utf-8",
    )
    return path


# --- Claim 1: station/forcing timeseries by old model_id ------------------


def test_inactive_m0_legacy_station_with_pre_cutover_file_serves_series(
    tmp_path: Path,
) -> None:
    """(cross-requirement) Inactive M0 legacy + pre-cutover file present -> series.

    Locks the SUB-5 answerability contract's load-bearing case (task §2.2
    "explicitly including the cross-requirement case"): after cutover an
    ``active_flag=false`` M0 legacy station whose pre-cutover file exists
    within retention still serves its series when the read path is called
    with the old ``model_id``. The SUB-4 B4 fix (which desensitizes only
    disk-MISS 404 details) must not break this successful disk-HIT read.
    """
    inactive_m0_station = _pre_cutover_station(active_flag=False)
    _write_pre_cutover_csv_at(tmp_path, station=inactive_m0_station)

    response = read_station_forcing_csv(
        station_lookup=_FakeStationLookup(inactive_m0_station),
        object_store_root=tmp_path,
        station_id=PRE_CUTOVER_STATION_ID,
        source_id=PRE_CUTOVER_SOURCE_ID,
        cycle_time=PRE_CUTOVER_CYCLE_TIME,
        model_id=PRE_CUTOVER_M0_MODEL_ID,
    )

    # Response is the normal series shape, keyed by the old (cycle, model).
    assert response["station_id"] == PRE_CUTOVER_STATION_ID
    assert response["model_id"] == PRE_CUTOVER_M0_MODEL_ID
    assert response["station"]["active_flag"] is False
    # The old-variant assets are immutable: at least one series with at
    # least one point comes back.
    assert len(response["series"]) >= 1
    assert sum(len(item["points"]) for item in response["series"]) >= 1


def test_inactive_m0_legacy_station_with_missing_pre_cutover_file_returns_desensitized_404(
    tmp_path: Path,
) -> None:
    """Inactive M0 legacy + pre-cutover file absent -> SUB-4 desensitized 404.

    When the pre-cutover file has rotated out of retention, the endpoint
    returns the desensitized ``STATION_FORCING_FILE_NOT_FOUND`` details
    (``{station_id}`` only). This is compatibility with SUB-4: the 404
    is a "file rotated out" signal, NOT a "row was deleted / hidden by
    the flip" signal — Claim 4 covers the latter.
    """
    inactive_m0_station = _pre_cutover_station(active_flag=False)
    # Deliberately do not write the CSV — the resolved disk path must miss.

    with pytest.raises(ForecastStoreError) as excinfo:
        read_station_forcing_csv(
            station_lookup=_FakeStationLookup(inactive_m0_station),
            object_store_root=tmp_path,
            station_id=PRE_CUTOVER_STATION_ID,
            source_id=PRE_CUTOVER_SOURCE_ID,
            cycle_time=PRE_CUTOVER_CYCLE_TIME,
            model_id=PRE_CUTOVER_M0_MODEL_ID,
        )

    error = excinfo.value
    # Stable code + SUB-4-desensitized details.
    assert error.status_code == 404
    assert error.code == "STATION_FORCING_FILE_NOT_FOUND"
    assert error.details == {"station_id": PRE_CUTOVER_STATION_ID}
    # The desensitized details must not carry any leaky leaf identity
    # (basin_version_id, model_id, source_id, cycle key). SUB-5's own
    # inline check — not a re-import of the SUB-4 exemplar's assertion.
    for leak_field in (
        "expected_path",
        "basin_version_id",
        "source_id",
        "cycle_time",
        "model_id",
    ):
        assert leak_field not in error.details


def test_active_station_with_pre_cutover_file_serves_series(
    tmp_path: Path,
) -> None:
    """(baseline) Active station + pre-cutover file present -> series unchanged.

    Baselines the "pre-cutover reads still work" contract for the trivial
    case (station is still ``active_flag=True``): the response shape and
    ``model_id`` echo do not depend on the flag.
    """
    active_station = _pre_cutover_station(active_flag=True)
    _write_pre_cutover_csv_at(tmp_path, station=active_station)

    response = read_station_forcing_csv(
        station_lookup=_FakeStationLookup(active_station),
        object_store_root=tmp_path,
        station_id=PRE_CUTOVER_STATION_ID,
        source_id=PRE_CUTOVER_SOURCE_ID,
        cycle_time=PRE_CUTOVER_CYCLE_TIME,
        model_id=PRE_CUTOVER_M0_MODEL_ID,
    )

    assert response["station_id"] == PRE_CUTOVER_STATION_ID
    assert response["model_id"] == PRE_CUTOVER_M0_MODEL_ID
    assert response["station"]["active_flag"] is True
    assert len(response["series"]) >= 1
    assert sum(len(item["points"]) for item in response["series"]) >= 1


# --- Claim 2: lookup SQL byte-shape lock ----------------------------------


def test_psycopg_station_lookup_sql_does_not_filter_active_flag() -> None:
    """The lookup SQL WHERE clause must not carry any ``active_flag`` predicate.

    Filtering ``active_flag`` in the lookup would 404 post-flip historical
    M0 reads whose files exist (breaks Claim 1) and would contradict the
    SUB-4 fix form pinned to 404-details desensitization
    (``openspec/changes/direct-grid-display-cutover/tasks.md`` §3.2
    non-goal). This is a byte-level regression lock.

    Precision: ``active_flag`` is allowed in the SELECT list (the caller
    receives it via ``StationMetadata.active_flag``); it is NOT allowed as
    a predicate in the WHERE clause.
    """
    source = inspect.getsource(PsycopgStationLookup._lookup_with_cursor)

    # Sanity: the projection still returns ``active_flag`` to the caller.
    assert "active_flag" in source, (
        "expected active_flag in the SELECT projection so callers receive it "
        "on StationMetadata"
    )

    # Extract the WHERE clause of the inline SQL literal and prove
    # ``active_flag`` does not appear as a predicate. The lookup keeps the
    # byte-unchanged ``WHERE station_id = %s`` predicate.
    where_match = re.search(
        r"WHERE\s+(.+?)(?:\"\"\"|\Z)", source, re.DOTALL | re.IGNORECASE
    )
    assert where_match is not None, "lookup SQL must have a WHERE clause"
    where_clause = where_match.group(1)
    assert "active_flag" not in where_clause, (
        "PsycopgStationLookup._lookup_with_cursor must not filter on "
        "active_flag in the WHERE clause (breaks pre-cutover answerability "
        "of post-flip inactive M0 legacy stations)"
    )

    # Belt-and-braces: the exact predicate token forms a fix would sneak
    # in via — bare or joined by AND / OR — must not appear anywhere in
    # the WHERE clause. Case-insensitive for defense-in-depth.
    banned_predicate_patterns = (
        r"active_flag\s*=\s*true",
        r"active_flag\s*=\s*false",
        r"AND\s+active_flag",
        r"OR\s+active_flag",
        r"active_flag\s*IS\s+NOT\s+NULL",
        r"active_flag\s*!=\s*",
        r"active_flag\s*<>\s*",
    )
    for pattern in banned_predicate_patterns:
        assert re.search(pattern, where_clause, re.IGNORECASE) is None, (
            f"forbidden active_flag predicate matched in WHERE clause: {pattern!r}"
        )


# --- Claim 3: flow-product resolution is orthogonal to met.met_station ----


_FORECAST_STORE_FLOW_PRODUCT_METHODS = (
    PsycopgForecastStore._latest_issue_time,
    PsycopgForecastStore._per_source_latest_cycles,
    PsycopgForecastStore._latest_analysis_issue_time,
    PsycopgForecastStore._fetch_analysis_segment_rows,
    PsycopgForecastStore._fetch_forecast_segment_rows,
)


def test_forecast_store_flow_product_sql_does_not_join_met_station() -> None:
    """Flow-product resolution methods must not reference ``met.met_station``.

    The flow-product path resolves from ``hydro.river_timeseries JOIN
    hydro.hydro_run`` keyed by ``(basin_version_id, segment_id,
    river_network_version_id, cycle_time)`` — it never joins the station
    catalog. An ``active_flag`` flip on stations therefore cannot affect
    the answerability of a pre-cutover flow product keyed by ``(cycle,
    model)``.

    This is asserted method-by-method so a future refactor that touches
    only one path is still caught.
    """
    offenders: list[str] = []
    for method in _FORECAST_STORE_FLOW_PRODUCT_METHODS:
        source = inspect.getsource(method)
        if "met.met_station" in source:
            offenders.append(f"{method.__qualname__} references met.met_station")
        # Also lock the "no active_flag predicate leaked in" property. The
        # flow-product SQL has no legitimate reason to reference
        # ``active_flag`` at all (it's not projected either).
        if "active_flag" in source:
            offenders.append(f"{method.__qualname__} references active_flag")

    assert not offenders, "; ".join(offenders)


def test_hydro_display_flow_product_route_sql_does_not_join_met_station() -> None:
    """Display-plane run-row resolver must not join ``met.met_station``.

    ``apps/api/routes/hydro_display.py::_run_row`` is where the display
    plane dispatches a run-id -> flow-product resolution (identified via
    ``grep 'FROM hydro.hydro_run' apps/api/routes/hydro_display.py``).
    It resolves via ``hydro.hydro_run LEFT JOIN core.model_instance``
    only; it MUST NOT join ``met.met_station``, which would couple the
    flow-product route to ``active_flag`` and break Claim 3.
    """
    source = inspect.getsource(hydro_display._run_row)

    # The run-row query anchors on hydro.hydro_run (positive assertion so
    # the test surfaces a rename of the SUT, not just a silent pass).
    assert "FROM hydro.hydro_run" in source, (
        "_run_row must resolve from hydro.hydro_run (SUT anchor moved?)"
    )
    assert "met.met_station" not in source, (
        "apps/api/routes/hydro_display.py::_run_row must not join "
        "met.met_station — the flow-product route stays orthogonal to the "
        "active_flag flip"
    )
    assert "active_flag" not in source, (
        "apps/api/routes/hydro_display.py::_run_row must not reference "
        "active_flag — it resolves by (run_id) alone"
    )


# --- Claim 4: flip is UPDATE-only, deletes/hides no historical row --------


def test_station_flag_flip_sut_is_update_only_no_delete_or_history_writes() -> None:
    """The flip hook writes ONLY ``UPDATE met.met_station`` — no DELETE, no history writes.

    Spec scenario "flip does not delete or hide historical products":
    ``packages/common/station_set_flip.py`` MUST NOT contain any
    ``DELETE`` / ``TRUNCATE``, and MUST NOT write to
    ``hydro.river_timeseries`` / ``hydro.hydro_run`` /
    ``met.forcing_station_timeseries``. The hook has exactly TWO
    ``UPDATE met.met_station SET active_flag = ...`` statements (the
    deterministic turn-off and the target-identity turn-on).

    Asserting on the WHOLE SUT module (``inspect.getfile``) closes the
    hole where a helper defined outside the closure could sneak in a
    forbidden write.
    """
    module_source = Path(inspect.getfile(build_station_flag_flip_hook)).read_text(
        encoding="utf-8"
    )

    # Positive anchor: the two UPDATE statements exist in the SUT (SUT
    # structure has not silently changed). Match each SET active_flag =
    # <bool> shape. The SUT module docstring also describes the two
    # statements verbatim as documentation, so the file-level count is
    # >=1 for each shape, not exactly 1 — the load-bearing invariant this
    # test locks is the ABSENCE of DELETE/TRUNCATE/INSERT, not the exact
    # UPDATE count.
    turn_off_matches = re.findall(
        r"UPDATE\s+met\.met_station\s+SET\s+active_flag\s*=\s*false",
        module_source,
        re.IGNORECASE,
    )
    turn_on_matches = re.findall(
        r"UPDATE\s+met\.met_station\s+SET\s+active_flag\s*=\s*true",
        module_source,
        re.IGNORECASE,
    )
    assert turn_off_matches, (
        "expected 'UPDATE met.met_station SET active_flag = false' statement "
        "in station_set_flip.py (SUT structure moved?)"
    )
    assert turn_on_matches, (
        "expected 'UPDATE met.met_station SET active_flag = true' statement "
        "in station_set_flip.py (SUT structure moved?)"
    )

    # Byte-level forbidden-token lock: SQL keywords/tokens that would let
    # the flip delete or hide historical rows. Case-insensitive to guard
    # against silent lowercasing during a refactor.
    forbidden_sql_tokens = (
        r"\bDELETE\b",
        r"\bTRUNCATE\b",
        r"\bINSERT\s+INTO\b",
    )
    for token_pattern in forbidden_sql_tokens:
        assert re.search(token_pattern, module_source, re.IGNORECASE) is None, (
            f"forbidden SQL token matched in station_set_flip.py: {token_pattern!r} "
            "— the flip must never delete/truncate/insert (UPDATE-only)"
        )

    # History-plane tables the flip must never write to. A JOIN or a
    # subquery mention would still be a red flag here (the hook has no
    # legitimate reason to touch these tables at all).
    forbidden_tables = (
        "hydro.river_timeseries",
        "hydro.hydro_run",
        "met.forcing_station_timeseries",
    )
    for table in forbidden_tables:
        assert table not in module_source, (
            f"station_set_flip.py must not reference {table} — the flip must "
            "not touch history-plane tables (deletes/hides no historical row)"
        )
