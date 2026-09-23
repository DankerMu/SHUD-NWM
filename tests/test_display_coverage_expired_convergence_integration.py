"""Real-database proof for #2504 D6: the #1446 guard, scoped to the retention window.

Run with the repo's standard opt-in against a throwaway database:

    NHMS_RUN_INTEGRATION=1 NHMS_INTEGRATION_DATABASE_URL=... uv run pytest -q \
        tests/test_display_coverage_expired_convergence_integration.py

``throwaway_database_url`` (tests/conftest.py) creates and drops a
uniquely-named database per TEST, so nothing here can touch a live one.

The guard and the ``--skip-fresh`` selection are SQL clauses; a fake cursor
cannot evaluate either, so every relaxation claim is executed here against the
fully migrated catalog (narrow key/enum ``hydro.river_timeseries``):

* an out-of-window populated row with no facts is lowered to 0 by an ordinary
  refresh and selected by ``--skip-fresh``;
* an in-window populated row with no facts — the #1446 legacy-cohort shape — is
  still refused (#2504 AC2, reinterpreted as window-scoped);
* an out-of-window row whose facts still exist keeps its count;
* a NULL stored end is never relaxed;
* the cutoff is the display watermark minus the window, never wall time: with a
  lagging watermark the band ``[watermark - window, now - window)`` stays
  protected;
* an unreadable watermark or an absent window leaves today's guard exactly;
* the parallel ``--all --skip-fresh`` path converges once, resolves the
  watermark once, and does not reselect a surviving expired row inside the
  rescan interval;
* the read-only audit reports the buckets on the same cutoff.

The watermark here (2026-06-20) lags the wall clock by months on purpose: that
is the geometry in which a ``now() - window`` anchor would be wrong.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg2
import pytest
from psycopg2.extras import RealDictCursor

from packages.common import display_coverage
from packages.common.display_coverage import (
    DisplayCoverageRefreshRefused,
    _stale_run_ids,
    refresh_run_display_coverage,
    resolve_expired_cutoff,
)
from packages.common.display_watermark import DisplayWatermarkError
from packages.common.display_watermark import fetch_display_watermark as real_fetch_display_watermark
from scripts import node27_refresh_coverage
from tests.integration_helpers import apply_migrations_from_zero, insert_river_timeseries_dual_written

pytestmark = pytest.mark.integration

_WINDOW_DAYS = 21
_WINDOW_ENV = {"NODE27_TIMESERIES_RETENTION_WINDOW_DAYS": str(_WINDOW_DAYS)}

_BASIN_ID = "basin-2504"
_BASIN_VERSION_ID = "bv-2504"
_NETWORK_ID = "rnv-2504"
_MODEL_ID = "model-2504"
_SEGMENTS = ("seg-a", "seg-b", "seg-c")

#: The display watermark: the newest displayable forecast cycle.
_WATERMARK = datetime(2026, 6, 20, tzinfo=UTC)
_CUTOFF = _WATERMARK - timedelta(days=_WINDOW_DAYS)

_LATEST = "run-2504-latest"
_EXPIRED_EMPTY = "run-2504-expired-empty"
_EXPIRED_WITH_FACTS = "run-2504-expired-with-facts"
_IN_WINDOW_EMPTY = "run-2504-in-window-empty"
_LAG_BAND_EMPTY = "run-2504-lag-band-empty"
_NULL_END_EMPTY = "run-2504-null-end-empty"

_CYCLES = {
    _LATEST: _WATERMARK,
    _EXPIRED_EMPTY: _WATERMARK - timedelta(days=30),
    _EXPIRED_WITH_FACTS: _WATERMARK - timedelta(days=31),
    _IN_WINDOW_EMPTY: _WATERMARK - timedelta(days=5),
    # Inside [watermark - window, watermark] but long before now() - window.
    _LAG_BAND_EMPTY: _WATERMARK - timedelta(days=10),
    _NULL_END_EMPTY: _WATERMARK - timedelta(days=40),
}
_POPULATED = (_EXPIRED_EMPTY, _EXPIRED_WITH_FACTS, _IN_WINDOW_EMPTY, _LAG_BAND_EMPTY, _NULL_END_EMPTY)

_COVERAGE_SQL = """
    SELECT segment_count, river_sample_count, river_valid_time_start, river_valid_time_end,
           min_lead_time_hours, max_lead_time_hours, refreshed_at
    FROM hydro.run_display_coverage WHERE run_id = %s
"""


def _connect(url: str) -> Any:
    connection = psycopg2.connect(url, cursor_factory=RealDictCursor)
    connection.autocommit = True
    return connection


def _seed(url: str) -> None:
    connection = _connect(url)
    try:
        with connection.cursor() as cursor:
            cursor.execute("INSERT INTO core.basin (basin_id, basin_name) VALUES (%s, 'Basin 2504')", (_BASIN_ID,))
            cursor.execute(
                """
                INSERT INTO core.basin_version (basin_version_id, basin_id, version_label, geom, active_flag)
                VALUES (%s, %s, 'v1',
                        ST_SetSRID(ST_GeomFromText('MULTIPOLYGON(((99 37, 99 39, 101 39, 101 37, 99 37)))'), 4490),
                        true)
                """,
                (_BASIN_VERSION_ID, _BASIN_ID),
            )
            cursor.execute(
                """
                INSERT INTO core.river_network_version
                    (river_network_version_id, basin_version_id, version_label, segment_count)
                VALUES (%s, %s, 'v1', %s)
                """,
                (_NETWORK_ID, _BASIN_VERSION_ID, len(_SEGMENTS)),
            )
            for index, segment_id in enumerate(_SEGMENTS):
                lon = 100.0 + index * 0.01
                cursor.execute(
                    f"""
                    INSERT INTO core.river_segment
                        (river_segment_id, river_network_version_id, segment_order, geom, properties_json)
                    VALUES (%s, %s, %s,
                            ST_Multi(ST_SetSRID(ST_GeomFromText(
                                'LINESTRING({lon} 38, {lon + 0.005} 38.005)'), 4490)),
                            '{{}}'::jsonb)
                    """,
                    (segment_id, _NETWORK_ID, index),
                )
            cursor.execute(
                """
                INSERT INTO core.model_instance
                    (model_id, basin_version_id, river_network_version_id, mesh_version_id,
                     calibration_version_id, shud_code_version, model_package_uri, active_flag, lifecycle_state)
                VALUES (%s, %s, %s, 'mesh-2504', 'cal-2504', '1.0', 's3://nhms/model', true, 'active')
                """,
                (_MODEL_ID, _BASIN_VERSION_ID, _NETWORK_ID),
            )
            for run_id, cycle in _CYCLES.items():
                cursor.execute(
                    """
                    INSERT INTO hydro.hydro_run
                        (run_id, run_type, scenario_id, model_id, basin_version_id, cycle_time,
                         start_time, end_time, status, run_manifest_uri, updated_at)
                    VALUES (%s, 'forecast', 'sc', %s, %s, %s, %s, %s, 'published', 's3://nhms/manifest',
                            now() - interval '10 days')
                    """,
                    (run_id, _MODEL_ID, _BASIN_VERSION_ID, cycle, cycle, cycle + timedelta(days=5)),
                )
            # Facts survive only for the one expired run whose chunk retention
            # "has not dropped yet".
            cycle = _CYCLES[_EXPIRED_WITH_FACTS]
            insert_river_timeseries_dual_written(
                cursor,
                [
                    (_EXPIRED_WITH_FACTS, _BASIN_VERSION_ID, _NETWORK_ID, segment, cycle + timedelta(hours=lead),
                     lead, "q_down", 1.0 + lead, "m3/s", "ok")
                    for segment in _SEGMENTS
                    for lead in (0, 1)
                ],
            )
            # Populated rows measured "earlier", refreshed after the run's last
            # data mutation, so the pre-#2504 staleness criterion calls them fresh.
            for run_id in _POPULATED:
                start = _CYCLES[run_id]
                end = None if run_id == _NULL_END_EMPTY else start + timedelta(hours=1)
                cursor.execute(
                    """
                    INSERT INTO hydro.run_display_coverage
                        (run_id, segment_count, river_sample_count, river_valid_time_start,
                         river_valid_time_end, min_lead_time_hours, max_lead_time_hours, refreshed_at)
                    VALUES (%s, %s, %s, %s, %s, 0, 1, now() - interval '2 days')
                    """,
                    (run_id, len(_SEGMENTS), len(_SEGMENTS) * 2, None if end is None else start, end),
                )
    finally:
        connection.close()


@pytest.fixture()
def seeded(throwaway_database_url: str) -> Iterator[str]:
    apply_migrations_from_zero(throwaway_database_url)
    _seed(throwaway_database_url)
    yield throwaway_database_url


def _coverage(url: str, run_id: str) -> dict[str, Any] | None:
    connection = _connect(url)
    try:
        with connection.cursor() as cursor:
            cursor.execute(_COVERAGE_SQL, (run_id,))
            row = cursor.fetchone()
    finally:
        connection.close()
    return None if row is None else dict(row)


def _all_coverage(url: str) -> dict[str, dict[str, Any] | None]:
    return {run_id: _coverage(url, run_id) for run_id in _CYCLES}


def _refresh(url: str, run_id: str, cutoff: datetime | None) -> bool:
    connection = psycopg2.connect(url, cursor_factory=RealDictCursor)
    try:
        return refresh_run_display_coverage(connection, run_id, expired_cutoff=cutoff)
    finally:
        connection.close()


def _stale(url: str, cutoff: datetime | None) -> set[str]:
    connection = psycopg2.connect(url, cursor_factory=RealDictCursor)
    try:
        return _stale_run_ids(connection, list(_CYCLES), expired_cutoff=cutoff)
    finally:
        connection.close()


def _assert_refused_and_unchanged(url: str, run_id: str, cutoff: datetime | None) -> None:
    before = _coverage(url, run_id)
    with pytest.raises(DisplayCoverageRefreshRefused) as excinfo:
        _refresh(url, run_id, cutoff)
    assert excinfo.value.existing_segment_count == len(_SEGMENTS)
    assert _coverage(url, run_id) == before


def test_the_cutoff_is_the_real_watermark_minus_the_window(seeded: str) -> None:
    assert resolve_expired_cutoff(seeded, _WINDOW_DAYS) == _CUTOFF


def test_an_out_of_window_empty_row_converges_to_zero_and_is_selected(seeded: str) -> None:
    assert _EXPIRED_EMPTY in _stale(seeded, _CUTOFF)
    assert _EXPIRED_EMPTY not in _stale(seeded, None)

    assert _refresh(seeded, _EXPIRED_EMPTY, _CUTOFF) is True

    after = _coverage(seeded, _EXPIRED_EMPTY)
    assert after is not None
    assert after["segment_count"] == 0
    assert after["river_sample_count"] == 0
    assert after["river_valid_time_end"] is None
    # Converged rows leave the expired selection for good.
    assert _EXPIRED_EMPTY not in _stale(seeded, _CUTOFF)


def test_an_in_window_populated_empty_row_is_still_refused(seeded: str) -> None:
    """#2504 AC2 / #1446: the legacy-cohort shape inside the window is protected."""
    _assert_refused_and_unchanged(seeded, _IN_WINDOW_EMPTY, _CUTOFF)
    assert _IN_WINDOW_EMPTY not in _stale(seeded, _CUTOFF)


def test_an_expired_row_whose_facts_survive_keeps_its_count(seeded: str) -> None:
    assert _refresh(seeded, _EXPIRED_WITH_FACTS, _CUTOFF) is True

    after = _coverage(seeded, _EXPIRED_WITH_FACTS)
    assert after is not None
    assert after["segment_count"] == len(_SEGMENTS)
    assert after["river_sample_count"] == len(_SEGMENTS) * 2


def test_a_null_end_row_is_never_relaxed(seeded: str) -> None:
    _assert_refused_and_unchanged(seeded, _NULL_END_EMPTY, _CUTOFF)
    assert _NULL_END_EMPTY not in _stale(seeded, _CUTOFF)


def test_a_lagging_watermark_protects_the_band_wall_time_would_expire(seeded: str) -> None:
    connection = _connect(seeded)
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT now() - %s * interval '1 day' AS wall_cutoff", (_WINDOW_DAYS,))
            wall_cutoff = cursor.fetchone()["wall_cutoff"]
    finally:
        connection.close()
    lag_end = _CYCLES[_LAG_BAND_EMPTY] + timedelta(hours=1)
    # The geometry is real: a wall-clock anchor would call this row expired...
    assert lag_end < wall_cutoff
    # ...the watermark anchor does not.
    assert lag_end >= _CUTOFF

    _assert_refused_and_unchanged(seeded, _LAG_BAND_EMPTY, resolve_expired_cutoff(seeded, _WINDOW_DAYS))


def test_an_unreadable_watermark_leaves_the_guard_closed(seeded: str, monkeypatch: pytest.MonkeyPatch) -> None:
    def _broken(*_args: Any, **_kwargs: Any) -> datetime:
        raise DisplayWatermarkError("display watermark query failed (OperationalError)")

    monkeypatch.setattr(display_coverage, "fetch_display_watermark", _broken)
    before = _coverage(seeded, _EXPIRED_EMPTY)

    rc = node27_refresh_coverage.main(["--run-id", _EXPIRED_EMPTY, "--database-url", seeded], env=_WINDOW_ENV)

    assert rc == 3
    assert _coverage(seeded, _EXPIRED_EMPTY) == before


def test_without_the_window_the_backstop_behaves_as_before(
    seeded: str, capsys: pytest.CaptureFixture[str]
) -> None:
    before = _all_coverage(seeded)

    rc = node27_refresh_coverage.main(["--all", "--skip-fresh", "--database-url", seeded], env={})

    assert rc == 0
    report = json.loads(capsys.readouterr().out)
    assert report["expired_cutoff"] is None
    # Only the run with no coverage row at all was selected (and inserted);
    # every populated row is untouched, refreshed_at included.
    assert report["refreshed"] == 1
    after = _all_coverage(seeded)
    assert before[_LATEST] is None and after[_LATEST] is not None
    assert {run_id: after[run_id] for run_id in _POPULATED} == {run_id: before[run_id] for run_id in _POPULATED}


def test_parallel_skip_fresh_converges_once_and_does_not_reselect(
    seeded: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[str] = []

    def _spy(dsn: str, **kwargs: Any) -> datetime:
        calls.append(dsn)
        return real_fetch_display_watermark(dsn, **kwargs)

    monkeypatch.setattr(display_coverage, "fetch_display_watermark", _spy)
    before = _all_coverage(seeded)
    argv = ["--all", "--skip-fresh", "--workers", "2", "--database-url", seeded]

    assert node27_refresh_coverage.main(argv, env=_WINDOW_ENV) == 0

    first = json.loads(capsys.readouterr().out)
    assert calls == [seeded]  # one watermark per batch, not per run
    assert first["expired_cutoff"] == _CUTOFF.isoformat().replace("+00:00", "Z")
    # Selected: the uncovered latest run, and the two expired populated rows.
    assert (first["refreshed"], first["refused"], first["failed"]) == (3, 0, 0)
    after = _all_coverage(seeded)
    assert after[_EXPIRED_EMPTY]["segment_count"] == 0
    assert after[_EXPIRED_WITH_FACTS]["segment_count"] == len(_SEGMENTS)
    for run_id in (_IN_WINDOW_EMPTY, _LAG_BAND_EMPTY, _NULL_END_EMPTY):
        assert after[run_id] == before[run_id], run_id

    # The next tick inside the rescan interval selects nothing: the surviving
    # expired row was just refreshed, the empty one reads 0 now.
    assert node27_refresh_coverage.main(argv, env=_WINDOW_ENV) == 0
    second = json.loads(capsys.readouterr().out)
    assert (second["refreshed"], second["refused"], second["skipped"], second["failed"]) == (0, 0, 0, 0)

    # Once the interval has elapsed the surviving row is rescanned again.
    connection = _connect(seeded)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE hydro.run_display_coverage SET refreshed_at = now() - interval '25 hours' WHERE run_id = %s",
                (_EXPIRED_WITH_FACTS,),
            )
    finally:
        connection.close()
    assert _stale(seeded, _CUTOFF) == {_EXPIRED_WITH_FACTS}


def test_the_audit_reports_the_buckets_on_the_same_cutoff_and_writes_nothing(
    seeded: str, capsys: pytest.CaptureFixture[str]
) -> None:
    before = _all_coverage(seeded)

    rc = node27_refresh_coverage.main(["--audit-populated-empty", "--database-url", seeded], env=_WINDOW_ENV)

    assert rc == 0
    report = json.loads(capsys.readouterr().out)
    assert report["watermark"] == "2026-06-20T00:00:00Z"
    assert report["cutoff"] == "2026-05-30T00:00:00Z"
    assert report["window_days"] == _WINDOW_DAYS
    assert report["in_window"] == {
        "total": 2,
        "empty": 2,
        "probe_failed": 0,
        "sample_empty_run_ids": [_IN_WINDOW_EMPTY, _LAG_BAND_EMPTY],
    }
    assert report["out_of_window"] == {
        "total": 2,
        "empty": 1,
        "probe_failed": 0,
        "sample_empty_run_ids": [_EXPIRED_EMPTY],
    }
    assert report["null_end"] == {"total": 1}
    assert _all_coverage(seeded) == before
