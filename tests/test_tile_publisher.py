"""Requirement-driven unit tests for ``TilePublisher.publish_qdown_cycle``.

These tests exercise the q_down display-publication contract that is decoupled
from flood-frequency readiness. Because ``publish_qdown_cycle`` constructs its
own SQLAlchemy engine internally (and sqlite cannot resolve ``schema.table``
without an ATTACH on that same connection), the success / identity / frequency /
private-path assertions drive the lower-level ``_publish_qdown_from_database``
entry point with a session whose connection has the schemas ATTACHed. Only the
``DATABASE_URL_MISSING`` case must go through the public ``publish_qdown_cycle``
because it short-circuits before any engine is created.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from services.tile_publisher.publisher import PublishError, PublishResult, TilePublisher

CYCLE_TIME = datetime(2024, 6, 1, 12, tzinfo=UTC)
COMPACT_TIME = "2024060112"
SOURCE_ID = "gfs"
CYCLE_ID = f"{SOURCE_ID}_{COMPACT_TIME}"


# --------------------------------------------------------------------------- #
# sqlite schema harness (mirrors tests/test_flood_frequency.py)
# --------------------------------------------------------------------------- #
def _attach_schemas(engine: Engine) -> None:
    @event.listens_for(engine, "connect")
    def _attach(dbapi_connection: Any, _record: Any) -> None:  # pragma: no cover - sqlite hook
        dbapi_connection.execute("ATTACH DATABASE ':memory:' AS hydro")
        dbapi_connection.execute("ATTACH DATABASE ':memory:' AS flood")
        dbapi_connection.execute("ATTACH DATABASE ':memory:' AS ops")


def _create_hydro_tables(connection: Any) -> None:
    connection.execute(
        text(
            """
            CREATE TABLE hydro.hydro_run (
                run_id TEXT PRIMARY KEY,
                run_type TEXT NOT NULL,
                scenario_id TEXT,
                model_id TEXT,
                basin_version_id TEXT,
                forcing_version_id TEXT,
                source_id TEXT,
                cycle_time DATETIME,
                start_time DATETIME,
                end_time DATETIME,
                status TEXT NOT NULL,
                run_manifest_uri TEXT,
                output_uri TEXT
            )
            """
        )
    )
    connection.execute(
        text(
            """
            CREATE TABLE hydro.river_timeseries (
                run_id TEXT NOT NULL,
                basin_version_id TEXT,
                river_network_version_id TEXT,
                river_segment_id TEXT NOT NULL,
                valid_time DATETIME NOT NULL,
                variable TEXT NOT NULL,
                value REAL NOT NULL,
                unit TEXT,
                quality_flag TEXT DEFAULT 'ok'
            )
            """
        )
    )


def _create_flood_table(connection: Any) -> None:
    connection.execute(
        text(
            """
            CREATE TABLE flood.return_period_result (
                run_id TEXT NOT NULL,
                river_segment_id TEXT NOT NULL,
                river_network_version_id TEXT,
                return_period REAL,
                warning_level TEXT,
                max_over_window REAL
            )
            """
        )
    )


@contextmanager
def _store(*, create_hydro: bool = True, create_flood: bool = False) -> Iterator[Session]:
    engine = create_engine(
        "sqlite://",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    _attach_schemas(engine)
    with engine.begin() as connection:
        if create_hydro:
            _create_hydro_tables(connection)
        if create_flood:
            _create_flood_table(connection)
    session = Session(engine)
    try:
        yield session
    finally:
        session.close()


def _publisher(tmp_path: Any, *, database_url: str | None = None) -> TilePublisher:
    return TilePublisher(
        workspace_root=tmp_path / "workspace",
        object_store_root=tmp_path / "object-store",
        object_store_prefix="",
        database_url=database_url,
    )


def _insert_run(
    session: Session,
    *,
    run_id: str,
    status: str = "parsed",
    run_type: str = "forecast",
    model_id: str | None = "model-1",
    basin_version_id: str | None = "basin-1",
    forcing_version_id: str | None = "forcing-1",
    river_network_version_id: str | None = "rivnet-1",
    output_uri: str | None = "published://tiles/hydro",
    run_manifest_uri: str | None = "published://tiles/hydro/manifest.json",
    segments: int = 3,
) -> None:
    session.execute(
        text(
            """
            INSERT INTO hydro.hydro_run (
                run_id, run_type, scenario_id, model_id, basin_version_id,
                forcing_version_id, source_id, cycle_time, start_time, end_time,
                status, run_manifest_uri, output_uri
            ) VALUES (
                :run_id, :run_type, 'scn', :model_id, :basin_version_id,
                :forcing_version_id, :source_id, :cycle_time, :cycle_time, :cycle_time,
                :status, :run_manifest_uri, :output_uri
            )
            """
        ),
        {
            "run_id": run_id,
            "run_type": run_type,
            "model_id": model_id,
            "basin_version_id": basin_version_id,
            "forcing_version_id": forcing_version_id,
            "source_id": SOURCE_ID,
            "cycle_time": CYCLE_TIME,
            "status": status,
            "run_manifest_uri": run_manifest_uri,
            "output_uri": output_uri,
        },
    )
    for index in range(segments):
        session.execute(
            text(
                """
                INSERT INTO hydro.river_timeseries (
                    run_id, basin_version_id, river_network_version_id,
                    river_segment_id, valid_time, variable, value, unit, quality_flag
                ) VALUES (
                    :run_id, :basin_version_id, :river_network_version_id,
                    :segment, :valid_time, 'q_down', :value, 'm3 s-1', 'ok'
                )
                """
            ),
            {
                "run_id": run_id,
                "basin_version_id": basin_version_id,
                "river_network_version_id": river_network_version_id,
                "segment": f"seg-{index}",
                "valid_time": CYCLE_TIME,
                "value": float(index + 1),
            },
        )
    session.commit()


def _insert_return_period(session: Session, *, run_id: str) -> None:
    session.execute(
        text(
            """
            INSERT INTO flood.return_period_result (
                run_id, river_segment_id, river_network_version_id,
                return_period, warning_level, max_over_window
            ) VALUES (:run_id, 'seg-0', 'rivnet-1', 100.0, 'major', 42.0)
            """
        ),
        {"run_id": run_id},
    )
    session.commit()


# --------------------------------------------------------------------------- #
# Scenario 1: q_down publish success (no flood table)
# --------------------------------------------------------------------------- #
def test_publish_qdown_success_publishes_one_layer_per_run(tmp_path: Any) -> None:
    publisher = _publisher(tmp_path)
    with _store(create_flood=False) as session:
        _insert_run(session, run_id="run-a", segments=3)
        _insert_run(session, run_id="run-b", segments=2)

        result = publisher._publish_qdown_from_database(session, CYCLE_ID)

    assert isinstance(result, PublishResult)
    assert result.status == "published"
    assert result.cycle_id == CYCLE_ID
    assert len(result.layers) == 2

    layer_ids = {layer["layer_id"] for layer in result.layers}
    assert layer_ids == {"q_down_run-a", "q_down_run-b"}
    for layer in result.layers:
        assert layer["layer_type"] == "q_down_timeseries"

    # artifacts: manifest + log per run.
    artifact_ids = {artifact["artifact_id"] for artifact in result.artifacts}
    assert "q_down_manifest_run-a" in artifact_ids
    assert "q_down_log_run-a" in artifact_ids
    assert "q_down_manifest_run-b" in artifact_ids
    assert "q_down_log_run-b" in artifact_ids

    # cycle manifest physically written to the object store.
    manifest_key = f"tiles/hydro/{CYCLE_ID}/q-down/manifest.json"
    assert publisher.object_store.exists(manifest_key)
    manifest = json.loads(publisher.object_store.read_bytes(manifest_key))
    assert manifest["published_basins"] == 2
    assert sorted(manifest["source_run_ids"]) == ["run-a", "run-b"]

    # per-run manifest also present.
    assert publisher.object_store.exists(f"tiles/hydro/{CYCLE_ID}/q-down/run-a/manifest.json")


def test_publish_qdown_identity_carries_all_nine_fields(tmp_path: Any) -> None:
    publisher = _publisher(tmp_path)
    with _store(create_flood=False) as session:
        _insert_run(session, run_id="run-a", segments=3)

        result = publisher._publish_qdown_from_database(session, CYCLE_ID)

    identity = result.layers[0]["identity"]
    expected_keys = {
        "run_id",
        "source",
        "cycle_time",
        "model_id",
        "basin_version_id",
        "river_network_version_id",
        "forcing_version_id",
        "station_count",
        "station_count_source",
        "segment_count",
    }
    assert expected_keys.issubset(identity.keys())
    assert identity["run_id"] == "run-a"
    assert identity["source"] == SOURCE_ID
    assert identity["model_id"] == "model-1"
    assert identity["basin_version_id"] == "basin-1"
    assert identity["river_network_version_id"] == "rivnet-1"
    assert identity["forcing_version_id"] == "forcing-1"
    assert identity["segment_count"] == 3
    assert identity["station_count"] == 3
    assert identity["station_count_source"] == "river_segment_proxy"


# --------------------------------------------------------------------------- #
# Scenario 2: frequency unavailable -> degraded but still published
# --------------------------------------------------------------------------- #
def test_publish_qdown_without_flood_table_is_degraded_but_published(tmp_path: Any) -> None:
    publisher = _publisher(tmp_path)
    with _store(create_flood=False) as session:
        _insert_run(session, run_id="run-a", segments=2)

        result = publisher._publish_qdown_from_database(session, CYCLE_ID)

    # q_down still published despite missing flood-frequency.
    assert result.status == "published"
    assert len(result.layers) == 1

    layer = result.layers[0]
    assert set(layer["unavailable_products"]) == {
        "return_period_result",
        "frequency_curves",
        "warning_thresholds",
    }
    assert layer["quality_state"] == "degraded"

    lineage = result.lineage
    assert lineage["quality_state"] == "degraded"
    assert set(lineage["unavailable_products"]) == {
        "return_period_result",
        "frequency_curves",
        "warning_thresholds",
    }
    blocker_codes = {blocker["code"] for blocker in lineage["residual_blockers"]}
    assert "RETURN_PERIOD_RESULT_UNAVAILABLE" in blocker_codes

    # No fabricated return-period / warning fields leak into identity or layer.
    assert "return_period" not in layer
    assert "warning_level" not in layer
    assert "warning_thresholds" not in layer["identity"]
    assert "return_period" not in layer["identity"]


def test_publish_qdown_flood_table_present_but_no_rows_is_degraded(tmp_path: Any) -> None:
    publisher = _publisher(tmp_path)
    with _store(create_flood=True) as session:
        _insert_run(session, run_id="run-a", segments=2)
        # flood table exists but holds no return-period rows for this run.

        result = publisher._publish_qdown_from_database(session, CYCLE_ID)

    assert result.status == "published"
    layer = result.layers[0]
    assert layer["quality_state"] == "degraded"
    assert "return_period_result" in layer["unavailable_products"]
    blocker_codes = {blocker["code"] for blocker in result.lineage["residual_blockers"]}
    assert "RETURN_PERIOD_RESULT_UNAVAILABLE" in blocker_codes


# --------------------------------------------------------------------------- #
# Scenario 3: frequency ready (decoupled readiness)
# --------------------------------------------------------------------------- #
def test_publish_qdown_with_return_period_rows_is_ready(tmp_path: Any) -> None:
    publisher = _publisher(tmp_path)
    with _store(create_flood=True) as session:
        _insert_run(session, run_id="run-a", segments=2)
        _insert_return_period(session, run_id="run-a")

        result = publisher._publish_qdown_from_database(session, CYCLE_ID)

    assert result.status == "published"
    assert result.lineage["quality_state"] == "ready"
    assert result.lineage["unavailable_products"] == []
    assert result.lineage["residual_blockers"] == []

    layer = result.layers[0]
    assert layer["quality_state"] == "ready"
    assert layer["unavailable_products"] == []


def test_display_readiness_independent_from_frequency_readiness(tmp_path: Any) -> None:
    """Same display layer publishes either way; only quality_state differs."""
    publisher_degraded = _publisher(tmp_path / "a")
    with _store(create_flood=False) as session:
        _insert_run(session, run_id="run-a", segments=2)
        degraded = publisher_degraded._publish_qdown_from_database(session, CYCLE_ID)

    publisher_ready = _publisher(tmp_path / "b")
    with _store(create_flood=True) as session:
        _insert_run(session, run_id="run-a", segments=2)
        _insert_return_period(session, run_id="run-a")
        ready = publisher_ready._publish_qdown_from_database(session, CYCLE_ID)

    # Display layer is published in both cases (decoupled readiness).
    assert degraded.status == ready.status == "published"
    assert len(degraded.layers) == len(ready.layers) == 1
    # The only material difference is the frequency-driven quality state.
    assert degraded.lineage["quality_state"] == "degraded"
    assert ready.lineage["quality_state"] == "ready"


# --------------------------------------------------------------------------- #
# Scenario 4: private workspace URI rejection
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "output_uri",
    [
        "/scratch/run/out",
        ".nhms-runs/run-a/output",
        "file:///tmp/run-a/output",
        "/var/lib/run-a/output",
    ],
)
def test_publish_qdown_rejects_private_workspace_uri(tmp_path: Any, output_uri: str) -> None:
    publisher = _publisher(tmp_path / output_uri.replace("/", "_"))
    with _store(create_flood=False) as session:
        _insert_run(session, run_id="run-a", segments=2, output_uri=output_uri)

        with pytest.raises(PublishError) as excinfo:
            publisher._publish_qdown_from_database(session, CYCLE_ID)

    assert excinfo.value.error_code == "DISPLAY_BOUNDARY_VIOLATION"


def test_publish_qdown_rejects_private_run_manifest_uri(tmp_path: Any) -> None:
    publisher = _publisher(tmp_path)
    with _store(create_flood=False) as session:
        _insert_run(
            session,
            run_id="run-a",
            segments=2,
            output_uri="published://tiles/hydro",
            run_manifest_uri="/scratch/run-a/manifest.json",
        )

        with pytest.raises(PublishError) as excinfo:
            publisher._publish_qdown_from_database(session, CYCLE_ID)

    assert excinfo.value.error_code == "DISPLAY_BOUNDARY_VIOLATION"


# --------------------------------------------------------------------------- #
# Scenario 5: strict product identity
# --------------------------------------------------------------------------- #
def test_publish_qdown_missing_lineage_raises_identity_incomplete(tmp_path: Any) -> None:
    publisher = _publisher(tmp_path)
    with _store(create_flood=False) as session:
        _insert_run(session, run_id="run-a", segments=2, forcing_version_id=None)

        with pytest.raises(PublishError) as excinfo:
            publisher._publish_qdown_from_database(session, CYCLE_ID)

    error = excinfo.value
    assert error.error_code == "PUBLISH_IDENTITY_INCOMPLETE"
    assert "forcing_version_id" in error.details["missing_fields"]


def test_publish_qdown_missing_model_id_raises_identity_incomplete(tmp_path: Any) -> None:
    publisher = _publisher(tmp_path)
    with _store(create_flood=False) as session:
        _insert_run(session, run_id="run-a", segments=2, model_id=None)

        with pytest.raises(PublishError) as excinfo:
            publisher._publish_qdown_from_database(session, CYCLE_ID)

    assert excinfo.value.error_code == "PUBLISH_IDENTITY_INCOMPLETE"
    assert "model_id" in excinfo.value.details["missing_fields"]


# --------------------------------------------------------------------------- #
# Scenario 6: error branches
# --------------------------------------------------------------------------- #
def test_publish_qdown_cycle_without_database_url_raises(tmp_path: Any) -> None:
    publisher = _publisher(tmp_path, database_url=None)

    with pytest.raises(PublishError) as excinfo:
        publisher.publish_qdown_cycle(CYCLE_ID)

    assert excinfo.value.error_code == "DATABASE_URL_MISSING"


def test_publish_qdown_non_canonical_cycle_id_raises(tmp_path: Any) -> None:
    publisher = _publisher(tmp_path)
    with _store(create_flood=False) as session:
        _insert_run(session, run_id="run-a", segments=2)

        with pytest.raises(PublishError) as excinfo:
            publisher._publish_qdown_from_database(session, "not-a-canonical-cycle")

    assert excinfo.value.error_code == "NON_CANONICAL_CYCLE_ID"


def test_publish_qdown_no_publishable_runs_raises(tmp_path: Any) -> None:
    publisher = _publisher(tmp_path)
    with _store(create_flood=False) as session:
        # A run that does not qualify (wrong status) -> nothing publishable.
        _insert_run(session, run_id="run-a", segments=2, status="running")

        with pytest.raises(PublishError) as excinfo:
            publisher._publish_qdown_from_database(session, CYCLE_ID)

    assert excinfo.value.error_code == "NO_PUBLISHABLE_QDOWN_PRODUCTS"


def test_publish_qdown_missing_hydro_run_table_raises_schema_missing(tmp_path: Any) -> None:
    publisher = _publisher(tmp_path)
    with _store(create_hydro=False, create_flood=False) as session:
        with pytest.raises(PublishError) as excinfo:
            publisher._publish_qdown_from_database(session, CYCLE_ID)

    assert excinfo.value.error_code == "DELIVERY_SCHEMA_MISSING"


# --------------------------------------------------------------------------- #
# Scenario 7: duplicate / reparse idempotency
# --------------------------------------------------------------------------- #
def test_publish_qdown_twice_is_idempotent(tmp_path: Any) -> None:
    publisher = _publisher(tmp_path)
    with _store(create_flood=False) as session:
        _insert_run(session, run_id="run-a", segments=3)
        _insert_run(session, run_id="run-b", segments=2)

        first = publisher._publish_qdown_from_database(session, CYCLE_ID)
        second = publisher._publish_qdown_from_database(session, CYCLE_ID)

    # Re-publishing the same cycle overwrites artifacts; layer count never doubles.
    assert len(first.layers) == len(second.layers) == 2
    first_ids = sorted(layer["layer_id"] for layer in first.layers)
    second_ids = sorted(layer["layer_id"] for layer in second.layers)
    assert first_ids == second_ids == ["q_down_run-a", "q_down_run-b"]

    # Object store holds a single canonical manifest (overwritten, not duplicated).
    manifest_key = f"tiles/hydro/{CYCLE_ID}/q-down/manifest.json"
    manifest = json.loads(publisher.object_store.read_bytes(manifest_key))
    assert manifest["published_basins"] == 2
