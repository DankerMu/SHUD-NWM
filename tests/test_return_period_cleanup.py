from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from workers.flood_frequency import cli as flood_cli
from workers.flood_frequency.return_period_cleanup import (
    CANDIDATE_PREDICATE_SQL,
    IDENTITY_COLUMNS,
    NO_CURVE_QUALITY_FLAGS,
    NoCurveCleanupError,
    NoCurveCleanupFilters,
    cleanup_no_curve_results,
    redact_database_url,
)

RUN_PRODUCT_QUALITY_COLUMNS_SQL = """
    run_id TEXT PRIMARY KEY,
    quality_state TEXT NOT NULL DEFAULT 'ready',
    quality_source TEXT NOT NULL DEFAULT 'historical_backfill',
    unavailable_products TEXT NOT NULL DEFAULT '[]',
    residual_blockers TEXT NOT NULL DEFAULT '[]',
    result_rows INTEGER NOT NULL DEFAULT 0,
    max_result_rows INTEGER NOT NULL DEFAULT 0,
    return_period_rows INTEGER NOT NULL DEFAULT 0,
    warning_rows INTEGER NOT NULL DEFAULT 0,
    max_return_period_rows INTEGER NOT NULL DEFAULT 0,
    max_warning_rows INTEGER NOT NULL DEFAULT 0,
    expected_result_rows INTEGER NOT NULL DEFAULT 0,
    expected_max_result_rows INTEGER NOT NULL DEFAULT 0,
    expected_timestep_result_rows INTEGER NOT NULL DEFAULT 0,
    meaningful_result_rows INTEGER NOT NULL DEFAULT 0,
    meaningful_max_result_rows INTEGER NOT NULL DEFAULT 0,
    meaningful_timestep_result_rows INTEGER NOT NULL DEFAULT 0,
    no_frequency_curve_rows INTEGER NOT NULL DEFAULT 0,
    no_usable_frequency_curve_rows INTEGER NOT NULL DEFAULT 0,
    warning_threshold_unavailable_rows INTEGER NOT NULL DEFAULT 0,
    refreshed_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
"""


def test_dry_run_emits_manifest_and_deletes_zero_rows(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    with _store() as session:
        _insert_run(session, "run-a")
        _insert_quality(session, "run-a", quality_source="explicit")
        _insert_result(session, "run-a", "seg-1", quality_flag="no_frequency_curve")
        _insert_result(session, "run-a", "seg-2", quality_flag="no_usable_frequency_curve", max_over_window=True)
        session.commit()

        manifest = cleanup_no_curve_results(
            session,
            manifest_path=manifest_path,
            database_url="postgresql://operator:secret@example/db?sslpassword=topsecret",
        )

        assert _row_count(session) == 2

    persisted = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["mode"] == "dry-run"
    assert manifest["dry_run"] is True
    assert manifest["target"]["total_candidates"] == 2
    assert manifest["target"]["affected_runs"] == {"count": 1, "run_ids": ["run-a"]}
    assert manifest["target"]["quality_coverage"]["missing_explicit_quality_run_ids"] == []
    assert persisted["target"]["total_candidates"] == 2
    assert "secret" not in persisted["database"]["url"]
    assert "topsecret" not in persisted["database"]["url"]
    assert persisted["database"]["url"] == "postgresql://operator:***@example/db?sslpassword=%2A%2A%2A"


def test_apply_deletes_only_candidates_and_preserves_meaningful_warning_rows(tmp_path: Path) -> None:
    with _store() as session:
        _insert_run(session, "run-a")
        _insert_quality(session, "run-a", quality_source="explicit", quality_state="unavailable")
        _insert_result(session, "run-a", "candidate-a", quality_flag="no_frequency_curve")
        _insert_result(
            session,
            "run-a",
            "meaningful-return-period",
            quality_flag="no_frequency_curve",
            return_period=5.0,
        )
        _insert_result(
            session,
            "run-a",
            "meaningful-warning",
            quality_flag="no_usable_frequency_curve",
            warning_level="warning",
        )
        _insert_result(session, "run-a", "non-candidate-flag", quality_flag="ok")
        session.commit()

        manifest = cleanup_no_curve_results(
            session,
            apply_changes=True,
            batch_size=10,
            manifest_path=tmp_path / "apply.json",
        )

        rows = _result_rows(session)
        quality = _quality_row(session, "run-a")

    assert manifest["deleted_rows"] == 1
    assert manifest["post_cleanup"]["quality_coverage"]["status"] == "complete"
    assert manifest["post_cleanup"]["quality_coverage"]["missing_explicit_quality_run_ids"] == []
    assert [row["river_segment_id"] for row in rows] == [
        "meaningful-return-period",
        "meaningful-warning",
        "non-candidate-flag",
    ]
    assert rows[0]["return_period"] == 5.0
    assert rows[1]["warning_level"] == "warning"
    assert quality["quality_source"] == "explicit"
    assert quality["quality_state"] == "unavailable"


def test_filters_scope_summary_guard_batches_and_delete_consistently(tmp_path: Path) -> None:
    with _store() as session:
        _insert_run(session, "run-a", basin_version_id="basin-a", source_id="GFS", cycle_time=datetime(2026, 5, 1))
        _insert_run(session, "run-b", basin_version_id="basin-b", source_id="IFS", cycle_time=datetime(2026, 5, 2))
        _insert_quality(session, "run-a", quality_source="explicit")
        _insert_result(
            session,
            "run-a",
            "target",
            basin_version_id="basin-a",
            source_id="GFS",
            cycle_time=datetime(2026, 5, 1),
            quality_flag="no_frequency_curve",
        )
        _insert_result(
            session,
            "run-a",
            "wrong-source",
            basin_version_id="basin-a",
            source_id="IFS",
            cycle_time=datetime(2026, 5, 1),
            quality_flag="no_frequency_curve",
        )
        _insert_result(
            session,
            "run-a",
            "wrong-basin",
            basin_version_id="basin-b",
            source_id="GFS",
            cycle_time=datetime(2026, 5, 1),
            quality_flag="no_frequency_curve",
        )
        _insert_result(
            session,
            "run-a",
            "wrong-cycle",
            basin_version_id="basin-a",
            source_id="GFS",
            cycle_time=datetime(2026, 5, 3),
            quality_flag="no_frequency_curve",
        )
        _insert_result(
            session,
            "run-b",
            "outside-run-filter",
            basin_version_id="basin-a",
            source_id="GFS",
            cycle_time=datetime(2026, 5, 1),
            quality_flag="no_frequency_curve",
        )
        session.commit()

        filters = NoCurveCleanupFilters(
            run_ids=("run-a",),
            basin_version_ids=("basin-a",),
            source_ids=("GFS",),
            cycle_time_start="2026-05-01 00:00:00",
            cycle_time_end="2026-05-01 23:59:59",
        )
        manifest = cleanup_no_curve_results(
            session,
            filters=filters,
            apply_changes=True,
            batch_size=1,
            manifest_path=tmp_path / "filtered-apply.json",
        )
        rows = _result_rows(session)

    assert manifest["target"]["total_candidates"] == 1
    assert manifest["target"]["affected_runs"]["run_ids"] == ["run-a"]
    assert manifest["target"]["quality_coverage"]["missing_explicit_quality_run_ids"] == []
    assert manifest["deleted_rows"] == 1
    assert [row["river_segment_id"] for row in rows] == [
        "outside-run-filter",
        "wrong-basin",
        "wrong-cycle",
        "wrong-source",
    ]


def test_missing_explicit_quality_blocks_apply_before_deletion(tmp_path: Path) -> None:
    with _store() as session:
        _insert_run(session, "run-a")
        _insert_run(session, "run-b")
        _insert_quality(session, "run-a", quality_source="explicit")
        _insert_quality(session, "run-b", quality_source="historical_backfill")
        _insert_result(session, "run-a", "safe", quality_flag="no_frequency_curve")
        _insert_result(session, "run-b", "blocked", quality_flag="no_frequency_curve")
        session.commit()

        with pytest.raises(NoCurveCleanupError) as error:
            cleanup_no_curve_results(
                session,
                apply_changes=True,
                batch_size=1,
                manifest_path=tmp_path / "blocked.json",
            )

        assert _row_count(session) == 2

    assert error.value.error_code == "MISSING_EXPLICIT_RUN_PRODUCT_QUALITY"
    assert error.value.details == {"missing_run_ids": ["run-b"]}
    assert error.value.manifest is not None
    assert error.value.manifest["status"] == "blocked"


def test_apply_requires_manifest_path() -> None:
    with _store() as session:
        with pytest.raises(NoCurveCleanupError) as error:
            cleanup_no_curve_results(session, apply_changes=True)

    assert error.value.error_code == "MANIFEST_PATH_REQUIRED"


def test_empty_filter_value_is_rejected_before_summary_or_delete(tmp_path: Path) -> None:
    with _store() as session:
        _insert_run(session, "run-a")
        _insert_quality(session, "run-a", quality_source="explicit")
        _insert_result(session, "run-a", "candidate", quality_flag="no_frequency_curve")
        session.commit()

        with pytest.raises(NoCurveCleanupError) as error:
            cleanup_no_curve_results(
                session,
                filters=NoCurveCleanupFilters(run_ids=("",)),
                apply_changes=True,
                manifest_path=tmp_path / "empty-filter.json",
            )

        assert _row_count(session) == 1

    assert error.value.error_code == "INVALID_FILTER_VALUE"


def test_batching_records_deleted_rows_and_stable_cursor_without_offset(tmp_path: Path) -> None:
    with _store() as session:
        _insert_run(session, "run-a")
        _insert_quality(session, "run-a", quality_source="explicit")
        for index in range(3):
            _insert_result(
                session,
                "run-a",
                f"seg-{index}",
                valid_time=datetime(2026, 5, 1) + timedelta(hours=index),
                quality_flag="no_frequency_curve",
            )
        session.commit()

        manifest_path = tmp_path / "batched.json"
        manifest = cleanup_no_curve_results(
            session,
            apply_changes=True,
            batch_size=2,
            manifest_path=manifest_path,
        )

    assert manifest["deleted_rows"] == 3
    persisted = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert persisted["deleted_rows"] == 3
    assert [batch["deleted_rows"] for batch in persisted["batches"]] == [2, 1]
    assert [batch["deleted_rows"] for batch in manifest["batches"]] == [2, 1]
    assert manifest["batches"][0]["cursor_after"] == {
        "run_id": "run-a",
        "river_network_version_id": "rnv-1",
        "river_segment_id": "seg-1",
        "duration": "1h",
        "valid_time": "2026-05-01 01:00:00",
        "max_over_window": False,
    }
    assert manifest["resume"]["pagination"] == "keyset"
    assert manifest["resume"]["offset_pagination"] is False
    assert manifest["resume"]["identity_columns"] == list(IDENTITY_COLUMNS)


def test_delete_rechecks_explicit_quality_guard_for_selected_identities(tmp_path: Path) -> None:
    with _store() as session:
        _insert_run(session, "run-a")
        _insert_run(session, "run-b")
        _insert_quality(session, "run-a", quality_source="explicit")
        _insert_quality(session, "run-b", quality_source="explicit")
        _insert_result(session, "run-a", "candidate", quality_flag="no_frequency_curve")
        session.commit()

        def revoke_quality_guard(
            _conn: Any,
            _cursor: Any,
            statement: str,
            _parameters: Any,
            _context: Any,
            _executemany: bool,
        ) -> None:
            normalized = " ".join(statement.split()).upper()
            if normalized.startswith("SELECT R.RUN_ID, R.RIVER_NETWORK_VERSION_ID"):
                session.execute(
                    text(
                        """
                        UPDATE flood.run_product_quality
                        SET quality_source = 'historical_backfill'
                        WHERE run_id = 'run-a'
                        """
                    )
                )

        bind = session.get_bind()
        event.listen(bind, "after_cursor_execute", revoke_quality_guard)
        try:
            with pytest.raises(NoCurveCleanupError) as error:
                cleanup_no_curve_results(
                    session,
                    apply_changes=True,
                    batch_size=1,
                    manifest_path=tmp_path / "race.json",
                )
        finally:
            event.remove(bind, "after_cursor_execute", revoke_quality_guard)

        assert _row_count(session) == 1
        assert _quality_row(session, "run-b")["quality_source"] == "explicit"

    assert error.value.error_code == "EXPLICIT_QUALITY_GUARD_CHANGED"
    assert error.value.manifest is not None
    assert error.value.manifest["batches"][0]["status"] == "aborted"
    assert error.value.manifest["batches"][0]["deleted_rows"] == 0


def test_manifest_path_no_clobber_and_explicit_overwrite(tmp_path: Path) -> None:
    manifest_path = tmp_path / "cleanup.json"
    manifest_path.write_text("existing\n", encoding="utf-8")
    with _store() as session:
        _insert_run(session, "run-a")
        session.commit()

        with pytest.raises(NoCurveCleanupError, match="already exists"):
            cleanup_no_curve_results(session, manifest_path=manifest_path)

        cleanup_no_curve_results(session, manifest_path=manifest_path, overwrite_manifest=True)

    assert json.loads(manifest_path.read_text(encoding="utf-8"))["operation"] == (
        "flood.return_period_result_no_curve_cleanup"
    )


def test_db_url_password_redaction_handles_manifest_and_error(tmp_path: Path) -> None:
    database_url = "postgresql://operator:super-secret@example/db?password=also-secret"
    assert "super-secret" not in redact_database_url(database_url)
    assert "also-secret" not in redact_database_url(database_url)

    manifest_path = tmp_path / "blocked.json"
    with _store() as session:
        _insert_run(session, "run-a")
        _insert_result(session, "run-a", "blocked", quality_flag="no_frequency_curve")
        session.commit()

        with pytest.raises(NoCurveCleanupError) as error:
            cleanup_no_curve_results(
                session,
                apply_changes=True,
                manifest_path=manifest_path,
                database_url=database_url,
            )

    manifest_text = manifest_path.read_text(encoding="utf-8")
    assert "super-secret" not in manifest_text
    assert "also-secret" not in manifest_text
    assert "super-secret" not in json.dumps(error.value.manifest)
    assert "also-secret" not in json.dumps(error.value.manifest)


def test_summary_database_error_is_redacted_in_manifest(tmp_path: Path) -> None:
    database_url = "postgresql://operator:super-secret@example/db"
    manifest_path = tmp_path / "failed.json"
    with _store() as session:
        session.execute(text("DROP TABLE flood.return_period_result"))
        session.commit()

        with pytest.raises(NoCurveCleanupError) as error:
            cleanup_no_curve_results(
                session,
                manifest_path=manifest_path,
                database_url=database_url,
            )

    assert error.value.error_code == "NO_CURVE_CLEANUP_SUMMARY_FAILED"
    manifest_text = manifest_path.read_text(encoding="utf-8")
    assert "super-secret" not in manifest_text
    assert "postgresql://operator:***@example/db" in manifest_text


def test_timescale_metadata_absence_is_non_fatal_and_records_unavailable_marker() -> None:
    with _store() as session:
        _insert_run(session, "run-a")
        _insert_quality(session, "run-a", quality_source="explicit")
        _insert_result(session, "run-a", "seg-1", quality_flag="no_frequency_curve")
        session.commit()

        manifest = cleanup_no_curve_results(session)

    assert manifest["target"]["chunk_distribution"]["status"] == "unavailable"
    assert manifest["target"]["chunk_distribution"]["items"] == []
    assert manifest["target"]["time_bucket_distribution"]["status"] == "available"
    assert manifest["target"]["time_bucket_distribution"]["items"] == [
        {"bucket_start": "2026-05-01", "rows": 1}
    ]


def test_committed_batch_manifest_is_persisted_when_postcheck_fails(tmp_path: Path) -> None:
    manifest_path = tmp_path / "postcheck-failed.json"
    with _store() as session:
        _insert_run(session, "run-a")
        _insert_quality(session, "run-a", quality_source="explicit")
        _insert_result(session, "run-a", "candidate-a", quality_flag="no_frequency_curve")
        _insert_result(session, "run-a", "candidate-b", quality_flag="no_frequency_curve")
        session.commit()

        def fail_remaining_count(
            _conn: Any,
            _cursor: Any,
            statement: str,
            _parameters: Any,
            _context: Any,
            _executemany: bool,
        ) -> None:
            normalized = " ".join(statement.split()).upper()
            if normalized.startswith("SELECT COUNT(*) FROM FLOOD.RETURN_PERIOD_RESULT"):
                raise RuntimeError("remaining-count-failed password=leaked")

        bind = session.get_bind()
        event.listen(bind, "before_cursor_execute", fail_remaining_count)
        try:
            with pytest.raises(NoCurveCleanupError) as error:
                cleanup_no_curve_results(
                    session,
                    apply_changes=True,
                    batch_size=2,
                    manifest_path=manifest_path,
                )
        finally:
            event.remove(bind, "before_cursor_execute", fail_remaining_count)

    persisted = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert error.value.error_code == "NO_CURVE_CLEANUP_POSTCHECK_FAILED"
    assert persisted["status"] == "failed"
    assert persisted["deleted_rows"] == 2
    assert persisted["batches"][0]["status"] == "committed"
    assert persisted["resume"]["last_committed_cursor"] == persisted["batches"][0]["cursor_after"]
    assert "password=leaked" not in json.dumps(persisted)


def test_manifest_path_validation_failure_after_commit_returns_in_memory_audit(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    with _store() as session:
        _insert_run(session, "run-a")
        _insert_quality(session, "run-a", quality_source="explicit")
        _insert_result(session, "run-a", "candidate", quality_flag="no_frequency_curve")
        session.commit()

        def replace_manifest_with_directory(
            _conn: Any,
            _cursor: Any,
            statement: str,
            _parameters: Any,
            _context: Any,
            _executemany: bool,
        ) -> None:
            normalized = " ".join(statement.split()).upper()
            if normalized.startswith("DELETE FROM FLOOD.RETURN_PERIOD_RESULT"):
                manifest_path.unlink()
                manifest_path.mkdir()

        bind = session.get_bind()
        event.listen(bind, "after_cursor_execute", replace_manifest_with_directory)
        try:
            with pytest.raises(NoCurveCleanupError) as error:
                cleanup_no_curve_results(
                    session,
                    apply_changes=True,
                    batch_size=1,
                    manifest_path=manifest_path,
                )
        finally:
            event.remove(bind, "after_cursor_execute", replace_manifest_with_directory)

    assert error.value.error_code == "NO_CURVE_CLEANUP_MANIFEST_WRITE_FAILED"
    assert error.value.manifest is not None
    assert error.value.manifest["deleted_rows"] == 1
    assert error.value.manifest["batches"][0]["status"] == "committed"
    assert error.value.manifest["resume"]["last_committed_cursor"] == error.value.manifest["batches"][0]["cursor_after"]


def test_cleanup_implementation_has_no_out_of_scope_destructive_operations() -> None:
    source = "\n".join(
        [
            Path("workers/flood_frequency/return_period_cleanup.py").read_text(encoding="utf-8"),
            Path("workers/flood_frequency/cli.py").read_text(encoding="utf-8"),
        ]
    ).upper()

    assert "DROP INDEX" not in source
    assert "REINDEX" not in source
    assert "VACUUM FULL" not in source
    assert "/RUNS" not in source
    assert "DELETE FROM HYDRO.RIVER_TIMESERIES" not in source


def test_cli_help_mentions_disk_reclamation_and_issue_491(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as error:
        flood_cli._click_main(["cleanup-no-curve-results", "--help"])

    help_text = " ".join(capsys.readouterr().out.split())
    assert error.value.code == 0
    assert "DELETE does not immediately reclaim disk" in help_text
    assert "#491 owns index, vacuum, and repack work" in help_text


def test_apply_selects_at_most_batch_size_identities_per_batch(tmp_path: Path) -> None:
    batch_selects: list[tuple[str, Any]] = []
    return_period_selects: list[str] = []
    with _store() as session:
        _insert_run(session, "run-a")
        _insert_quality(session, "run-a", quality_source="explicit")
        for index in range(5):
            _insert_result(session, "run-a", f"seg-{index}", quality_flag="no_frequency_curve")
        session.commit()

        def capture_sql(
            _conn: Any,
            _cursor: Any,
            statement: str,
            parameters: Any,
            _context: Any,
            _executemany: bool,
        ) -> None:
            normalized = " ".join(statement.split()).upper()
            if normalized.startswith("SELECT") and "FLOOD.RETURN_PERIOD_RESULT" in normalized:
                return_period_selects.append(normalized)
            if normalized.startswith("SELECT R.RUN_ID, R.RIVER_NETWORK_VERSION_ID"):
                batch_selects.append((normalized, parameters))

        bind = session.get_bind()
        event.listen(bind, "before_cursor_execute", capture_sql)
        try:
            cleanup_no_curve_results(
                session,
                apply_changes=True,
                batch_size=2,
                manifest_path=tmp_path / "batch-size.json",
            )
        finally:
            event.remove(bind, "before_cursor_execute", capture_sql)

    assert len(batch_selects) == 4
    for statement, parameters in batch_selects:
        assert "LIMIT ?" in statement or "LIMIT :BATCH_SIZE" in statement
        assert 2 in _parameter_values(parameters)
    assert all("OFFSET" not in statement for statement, _parameters in batch_selects)
    assert all("SELECT *" not in statement for statement in return_period_selects)
    assert all(
        (
            "COUNT(" in statement
            or "GROUP BY" in statement
            or statement.startswith("SELECT R.RUN_ID, R.RIVER_NETWORK_VERSION_ID")
        )
        for statement in return_period_selects
    )


def test_candidate_predicate_constant_is_exact_fixture_contract() -> None:
    assert CANDIDATE_PREDICATE_SQL == (
        "return_period IS NULL AND warning_level IS NULL "
        "AND quality_flag IN ('no_frequency_curve','no_usable_frequency_curve')"
    )
    assert NO_CURVE_QUALITY_FLAGS == ("no_frequency_curve", "no_usable_frequency_curve")


@contextmanager
def _store() -> Iterator[Session]:
    engine = create_engine(
        "sqlite://",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    _attach_schemas(engine)
    with engine.begin() as connection:
        _create_tables(connection)
    session = Session(engine)
    try:
        yield session
    finally:
        session.close()


def _attach_schemas(engine: Engine) -> None:
    @event.listens_for(engine, "connect")
    def _attach(dbapi_connection: Any, _connection_record: Any) -> None:
        dbapi_connection.execute("ATTACH DATABASE ':memory:' AS hydro")
        dbapi_connection.execute("ATTACH DATABASE ':memory:' AS flood")


def _create_tables(connection: Any) -> None:
    connection.execute(
        text(
            """
            CREATE TABLE hydro.hydro_run (
                run_id TEXT PRIMARY KEY,
                run_type TEXT NOT NULL,
                scenario_id TEXT NOT NULL,
                model_id TEXT NOT NULL,
                basin_version_id TEXT NOT NULL,
                source_id TEXT,
                cycle_time DATETIME,
                start_time DATETIME NOT NULL,
                end_time DATETIME NOT NULL,
                status TEXT NOT NULL
            )
            """
        )
    )
    connection.execute(
        text(
            """
            CREATE TABLE flood.return_period_result (
                run_id TEXT NOT NULL,
                scenario_id TEXT NOT NULL,
                basin_version_id TEXT NOT NULL,
                river_network_version_id TEXT NOT NULL,
                model_id TEXT NOT NULL,
                river_segment_id TEXT NOT NULL,
                valid_time DATETIME NOT NULL,
                duration TEXT NOT NULL,
                q_value REAL NOT NULL,
                q_unit TEXT NOT NULL DEFAULT 'm3/s',
                return_period REAL,
                warning_level TEXT,
                source_id TEXT,
                cycle_time DATETIME,
                max_over_window BOOLEAN NOT NULL DEFAULT 0,
                quality_flag TEXT NOT NULL DEFAULT 'ok',
                PRIMARY KEY (
                    run_id, river_network_version_id, river_segment_id,
                    duration, valid_time, max_over_window
                )
            )
            """
        )
    )
    connection.execute(text(f"CREATE TABLE flood.run_product_quality ({RUN_PRODUCT_QUALITY_COLUMNS_SQL})"))


def _insert_run(
    session: Session,
    run_id: str,
    *,
    basin_version_id: str = "basin-1",
    source_id: str = "GFS",
    cycle_time: datetime = datetime(2026, 5, 1),
) -> None:
    session.execute(
        text(
            """
            INSERT INTO hydro.hydro_run (
                run_id, run_type, scenario_id, model_id, basin_version_id, source_id,
                cycle_time, start_time, end_time, status
            )
            VALUES (
                :run_id, 'forecast', 'scenario-1', 'model-1', :basin_version_id, :source_id,
                :cycle_time, :cycle_time, :end_time, 'parsed'
            )
            """
        ),
        {
            "run_id": run_id,
            "basin_version_id": basin_version_id,
            "source_id": source_id,
            "cycle_time": cycle_time,
            "end_time": cycle_time + timedelta(days=1),
        },
    )


def _insert_quality(
    session: Session,
    run_id: str,
    *,
    quality_source: str,
    quality_state: str = "unavailable",
) -> None:
    session.execute(
        text(
            """
            INSERT INTO flood.run_product_quality (
                run_id, quality_state, quality_source, unavailable_products, residual_blockers
            )
            VALUES (
                :run_id, :quality_state, :quality_source,
                '["frequency_curves","return_period_result"]', '[]'
            )
            """
        ),
        {"run_id": run_id, "quality_state": quality_state, "quality_source": quality_source},
    )


def _insert_result(
    session: Session,
    run_id: str,
    segment_id: str,
    *,
    basin_version_id: str = "basin-1",
    river_network_version_id: str = "rnv-1",
    valid_time: datetime = datetime(2026, 5, 1),
    duration: str = "1h",
    return_period: float | None = None,
    warning_level: str | None = None,
    source_id: str = "GFS",
    cycle_time: datetime = datetime(2026, 5, 1),
    max_over_window: bool = False,
    quality_flag: str = "no_frequency_curve",
) -> None:
    session.execute(
        text(
            """
            INSERT INTO flood.return_period_result (
                run_id, scenario_id, basin_version_id, river_network_version_id, model_id,
                river_segment_id, valid_time, duration, q_value, q_unit, return_period,
                warning_level, source_id, cycle_time, max_over_window, quality_flag
            )
            VALUES (
                :run_id, 'scenario-1', :basin_version_id, :river_network_version_id, 'model-1',
                :river_segment_id, :valid_time, :duration, 42.0, 'm3/s', :return_period,
                :warning_level, :source_id, :cycle_time, :max_over_window, :quality_flag
            )
            """
        ),
        {
            "run_id": run_id,
            "basin_version_id": basin_version_id,
            "river_network_version_id": river_network_version_id,
            "river_segment_id": segment_id,
            "valid_time": valid_time,
            "duration": duration,
            "return_period": return_period,
            "warning_level": warning_level,
            "source_id": source_id,
            "cycle_time": cycle_time,
            "max_over_window": max_over_window,
            "quality_flag": quality_flag,
        },
    )


def _result_rows(session: Session) -> list[dict[str, Any]]:
    rows = session.execute(
        text(
            """
            SELECT river_segment_id, return_period, warning_level, quality_flag
            FROM flood.return_period_result
            ORDER BY river_segment_id
            """
        )
    ).mappings()
    return [dict(row) for row in rows]


def _row_count(session: Session) -> int:
    return int(session.execute(text("SELECT COUNT(*) FROM flood.return_period_result")).scalar_one())


def _quality_row(session: Session, run_id: str) -> dict[str, Any]:
    row = session.execute(
        text("SELECT * FROM flood.run_product_quality WHERE run_id = :run_id"),
        {"run_id": run_id},
    ).mappings().one()
    return dict(row)


def _parameter_values(parameters: Any) -> list[Any]:
    if isinstance(parameters, Mapping):
        return list(parameters.values())
    if isinstance(parameters, list | tuple):
        return list(parameters)
    return []
