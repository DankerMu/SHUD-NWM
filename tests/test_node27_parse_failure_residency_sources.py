"""#2590: the residency lane's two extra sources.

- `published_reparse`: the latest `PUBLISHED_REPARSE_FAILED` decline of a run
  that has not parsed since; `declined_at` is its last-updated time and is
  never renewed, so the run alerts once and then ages out of the liveness bound.
- `legacy_store_refused`: a `failed` run on a legacy-store forcing version,
  renewed every tick by the register with no decline to stop it (#1991); it
  alerts once and is never re-alerted.

Expected values follow the fixture's rules on an injected clock; the SQL is
executed against PostgreSQL by the integration case at the bottom.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg2
import pytest

from scripts import node27_parse_failure_residency_alert as alert

T0 = datetime(2026, 9, 28, 6, 0, tzinfo=UTC)
TICK = timedelta(minutes=30)
DSN = "postgresql://nhms_display_ro:s3cretpw@127.0.0.1:55432/nhms"


def _run(
    run_id: str, *, classification: str, touched: datetime, code: str = "MODEL_RIVER_FILE_MALFORMED"
) -> alert.FailingRun:
    return alert.FailingRun(
        run_id=run_id, run_key=1, first_error_code=code, updated_at=touched, classification=classification
    )


def _tick(tmp_path: Path, capsys: pytest.CaptureFixture[str], now: datetime, rows: list[alert.FailingRun], **env: str):
    environment = {"DATABASE_URL": DSN, "NHMS_PARSE_RESIDENCY_STATE_PATH": str(tmp_path / "state.json"), **env}
    rc = alert.main([], now=now, observe=lambda _config, _floor: list(rows), env=environment)
    return rc, capsys.readouterr().out.splitlines()


def test_a_published_reparse_decline_alerts_once_and_then_ages_out(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    declined_at = T0
    row = [_run("run-pub", classification=alert.CLASS_PUBLISHED_REPARSE, touched=declined_at)]
    outcomes = []
    for step in range(13):  # every 30 min for 6 h: declined_at is never renewed
        rc, lines = _tick(tmp_path, capsys, T0 + TICK + step * TICK, row)
        outcomes.append((rc, lines[0].split(" watched=")[1].split()[0]))

    alerts = [index for index, (rc, _watched) in enumerate(outcomes) if rc == 1]
    assert alerts == [4], outcomes  # first seen at T0+30m, resident 2 h later
    assert outcomes[-1] == (0, "0")  # past the 6 h bound: dropped, no further mail

    rc, lines = _tick(tmp_path, capsys, T0 + TICK + 4 * TICK, row, NHMS_PARSE_RESIDENCY_STATE_PATH=str(tmp_path / "s2"))
    assert rc == 0  # a fresh state starts a fresh clock: not resident yet


def test_the_report_names_the_source_and_the_reparse_code(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    row = [_run("run-pub", classification=alert.CLASS_PUBLISHED_REPARSE, touched=T0)]
    _tick(tmp_path, capsys, T0, row, NHMS_PARSE_RESIDENCY_THRESHOLD_HOURS="0")

    rc, lines = _tick(tmp_path, capsys, T0 + TICK, row, NHMS_PARSE_RESIDENCY_THRESHOLD_HOURS="0")

    assert rc == 0  # already alerted on the first tick
    assert any(
        "run_id=run-pub first_error_code=MODEL_RIVER_FILE_MALFORMED source=published_reparse " in line for line in lines
    ), lines


def test_a_legacy_store_refused_run_alerts_once_and_is_never_re_alerted(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    now = T0
    rcs = []
    for _ in range(8):  # 2 h to become resident, then ~27 h more, renewed every tick
        rcs.append(
            _tick(
                tmp_path,
                capsys,
                now,
                [_run("run-legacy", classification=alert.CLASS_LEGACY_STORE_REFUSED, touched=now)],
            )[0]
        )
        now += TICK if len(rcs) < 5 else timedelta(hours=9)

    assert rcs.count(1) == 1 and rcs.index(1) == 4, rcs
    assert now - T0 > timedelta(hours=26)


def test_an_ordinary_failed_run_is_still_re_alerted(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    now = T0
    rcs = []
    for _ in range(8):
        rcs.append(
            _tick(tmp_path, capsys, now, [_run("run-failed", classification=alert.CLASS_FAILED, touched=now)])[0]
        )
        now += TICK if len(rcs) < 5 else timedelta(hours=9)

    assert rcs.count(1) == 2, rcs  # first alert, then again once 24 h have passed


@pytest.mark.parametrize(("threshold", "liveness"), [("6", "6"), ("7", ""), ("2", "1")])
def test_a_threshold_not_shorter_than_the_liveness_bound_is_a_config_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], threshold: str, liveness: str
) -> None:
    rc, lines = _tick(
        tmp_path,
        capsys,
        T0,
        [],
        NHMS_PARSE_RESIDENCY_THRESHOLD_HOURS=threshold,
        NHMS_PARSE_RESIDENCY_RETRY_LIVENESS_HOURS=liveness,
    )

    assert rc == 2
    assert lines[0].startswith(alert.CODE_CONFIG_INVALID)
    assert not (tmp_path / "state.json").exists()


def test_a_failed_row_wins_over_a_decline_for_the_same_run() -> None:
    decline = _run("run-x", classification=alert.CLASS_PUBLISHED_REPARSE, touched=T0, code="RIVQDOWN_EMPTY")
    failed = _run("run-x", classification=alert.CLASS_FAILED, touched=T0, code="OUTPUT_PARSE_DB_ERROR")

    assert alert.merge_sources([decline, failed]) == [failed]
    assert alert.merge_sources([failed, decline]) == [failed]


# ---------------------------------------------------------------------------
# The real observation statement, on PostgreSQL.
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_the_observation_query_reads_both_new_sources(throwaway_database_url: str) -> None:
    from tests.integration_helpers import apply_migrations_from_zero

    apply_migrations_from_zero(throwaway_database_url)
    connection = psycopg2.connect(throwaway_database_url)
    connection.autocommit = True
    try:
        with connection.cursor() as cursor:
            cursor.execute("INSERT INTO core.basin (basin_id, basin_name) VALUES ('b-2590', 'b')")
            cursor.execute(
                "INSERT INTO core.basin_version (basin_version_id, basin_id, version_label, geom, active_flag) "
                "VALUES ('bv-2590', 'b-2590', 'v1', ST_SetSRID(ST_GeomFromText("
                "'MULTIPOLYGON(((99 37, 99 39, 101 39, 101 37, 99 37)))'), 4490), true)"
            )
            cursor.execute(
                "INSERT INTO core.river_network_version (river_network_version_id, basin_version_id, "
                "version_label, segment_count) VALUES ('rnv-2590', 'bv-2590', 'v1', 1)"
            )
            cursor.execute(
                "INSERT INTO core.model_instance (model_id, basin_version_id, river_network_version_id, "
                "mesh_version_id, calibration_version_id, shud_code_version, model_package_uri, active_flag, "
                "lifecycle_state) "
                "VALUES ('m-2590', 'bv-2590', 'rnv-2590', 'mesh', 'cal', '1.0', 's3://nhms/m', true, 'active')"
            )
            cursor.execute(
                "INSERT INTO met.data_source (source_id, source_name, source_type, status, adapter_name) "
                "VALUES ('src-2590', 'GFS', 'forecast', 'mock', 'gfs')"
            )
            for forcing_id, store in (("fv-legacy", "legacy"), ("fv-narrow", "narrow")):
                cursor.execute(
                    "INSERT INTO met.forcing_version (forcing_version_id, model_id, source_id, start_time, end_time, "
                    "station_count, forcing_package_uri, timeseries_store) "
                    "VALUES (%s, 'm-2590', 'src-2590', now(), now(), 1, 's3://f', %s)",
                    (forcing_id, store),
                )
            for run_id, status, code, forcing_id, parsed_ago in (
                ("run-failed-legacy", "failed", "RIVQDOWN_EMPTY", "fv-legacy", None),
                ("run-failed-narrow", "failed", "OUTPUT_PARSE_DB_ERROR", "fv-narrow", None),
                ("run-pub-latest", "published", None, "fv-narrow", "3 hours"),
                ("run-pub-reparsed", "published", None, "fv-narrow", "5 minutes"),
                ("run-pub-old", "published", None, "fv-narrow", "3 days"),
                ("run-pub-other-reason", "published", None, "fv-narrow", "3 hours"),
                ("run-pub-never-parsed", "published", None, None, None),
            ):
                cursor.execute(
                    "INSERT INTO hydro.hydro_run (run_id, run_type, scenario_id, model_id, basin_version_id, "
                    "forcing_version_id, cycle_time, start_time, end_time, status, run_manifest_uri, error_code, "
                    "parsed_at, updated_at) "
                    "VALUES (%s, 'forecast', 'sc', 'm-2590', 'bv-2590', %s, now(), now(), now(), %s, 's3://m', %s, "
                    "now() - %s::interval, now() - interval '10 minutes')",
                    (run_id, forcing_id, status, code, parsed_ago),
                )
            for run_id, reason, detail, ago, mtime in (
                ("run-pub-latest", "PUBLISHED_REPARSE_FAILED", "RIVQDOWN_EMPTY: first", "2 hours", 1.0),
                ("run-pub-latest", "PUBLISHED_REPARSE_FAILED", "MODEL_RIVER_FILE_MALFORMED: latest", "1 hour", 2.0),
                ("run-pub-reparsed", "PUBLISHED_REPARSE_FAILED", "RIVQDOWN_EMPTY: repaired since", "1 hour", 1.0),
                ("run-pub-old", "PUBLISHED_REPARSE_FAILED", "RIVQDOWN_EMPTY: aged out", "7 hours", 1.0),
                ("run-pub-other-reason", "APPLY_COMPRESSED_CHUNK_BLOCKED", "chunk", "1 hour", 1.0),
                ("run-pub-never-parsed", "PUBLISHED_REPARSE_FAILED", None, "30 minutes", 1.0),
            ):
                cursor.execute(
                    "INSERT INTO ops.ingest_recompute_decline "
                    "(run_id, init_state_id, product_mtime, reason_code, detail, declined_at) "
                    "VALUES (%s, '', %s, %s, %s, now() - %s::interval)",
                    (run_id, mtime, reason, detail, ago),
                )
    finally:
        connection.close()

    config = alert.config_from_env({"DATABASE_URL": throwaway_database_url})
    rows = alert.default_observe(config, datetime.now(UTC) - timedelta(hours=6))

    assert [(row.run_id, row.first_error_code, row.classification) for row in rows] == [
        ("run-failed-legacy", "RIVQDOWN_EMPTY", "legacy_store_refused"),
        ("run-failed-narrow", "OUTPUT_PARSE_DB_ERROR", "failed"),
        ("run-pub-latest", "MODEL_RIVER_FILE_MALFORMED", "published_reparse"),
        ("run-pub-never-parsed", "", "published_reparse"),
    ]
