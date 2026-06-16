"""Requirement-driven unit tests for ``TilePublisher.publish_qdown_cycle``.

These tests exercise the q_down display-publication contract that is decoupled
from flood-frequency readiness. Because ``publish_qdown_cycle`` constructs its
own SQLAlchemy engine internally (and sqlite cannot resolve ``schema.table``
without an ATTACH on that same connection), most success / identity / frequency /
private-path assertions drive the lower-level ``_publish_qdown_from_database``
entry point with a session whose connection has the schemas ATTACHed. The
``DATABASE_URL_MISSING`` case and the F6 public-entry happy path exercise the
public ``publish_qdown_cycle`` directly.

Contract (post q_down fix):
* layer_id / artifact keys embed the river_network_version_id segment so a single
  run that spans multiple river networks yields distinct, non-colliding layers:
  ``q_down_{run_id}_{network_segment}`` and
  ``tiles/hydro/{cycle_id}/q-down/{run_id}/{network_segment}/manifest.json``.
* When ``map.tile_layer`` exists each published layer is upserted + committed and
  lineage carries ``db_registered=True``; a missing table is tolerated
  (``db_registered=False``, no commit, never break).
* cycle manifest/lineage: ``source_run_ids`` is deduped (sorted set),
  ``published_basins`` counts distinct run_id, ``published_products`` counts the
  run x river_network rows.
* per-run identity skip (F5): in a multi-run cycle an identity-incomplete run is
  skipped (blocker accumulated, peers still publish); only if *every* run is
  incomplete does the cycle raise PUBLISH_IDENTITY_INCOMPLETE.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from packages.common.object_store import ObjectStoreError
from services.tile_publisher.publisher import (
    PublishError,
    PublishResult,
    TilePublisher,
    _is_private_display_path,
    _replace_directory_tree_no_follow,
)

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
        dbapi_connection.execute("ATTACH DATABASE ':memory:' AS map")
        dbapi_connection.execute("ATTACH DATABASE ':memory:' AS met")


def _create_hydro_tables(connection: Any) -> None:
    # F8: river_timeseries carries the real composite primary key and hydro_run
    # run_manifest_uri is NOT NULL to match the production schema fidelity.
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
                run_manifest_uri TEXT NOT NULL,
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
                river_network_version_id TEXT NOT NULL,
                river_segment_id TEXT NOT NULL,
                valid_time DATETIME NOT NULL,
                variable TEXT NOT NULL,
                value REAL NOT NULL,
                unit TEXT,
                quality_flag TEXT DEFAULT 'ok',
                PRIMARY KEY (run_id, river_network_version_id, river_segment_id, variable, valid_time)
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
                max_over_window REAL,
                quality_flag TEXT DEFAULT 'ok'
            )
            """
        )
    )


def _create_tile_layer_table(connection: Any) -> None:
    # Mirrors db/migrations/000008_map.sql map.tile_layer (sqlite-compatible types).
    connection.execute(
        text(
            """
            CREATE TABLE map.tile_layer (
                layer_id TEXT PRIMARY KEY,
                layer_type TEXT NOT NULL,
                source_run_id TEXT,
                source_product_id TEXT,
                source_version TEXT,
                variable TEXT,
                valid_time DATETIME,
                tile_format TEXT NOT NULL,
                tile_uri_template TEXT NOT NULL,
                maplibre_source_layer TEXT,
                property_schema_version TEXT,
                property_schema_json TEXT,
                cache_version TEXT,
                fallback_available BOOLEAN NOT NULL DEFAULT 0,
                release_blocking BOOLEAN NOT NULL DEFAULT 0,
                min_zoom INTEGER NOT NULL DEFAULT 0,
                max_zoom INTEGER NOT NULL DEFAULT 14,
                style_json TEXT,
                published_flag BOOLEAN NOT NULL DEFAULT 0,
                publish_time DATETIME,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
    )


def _create_forcing_version_table(connection: Any) -> None:
    connection.execute(
        text(
            """
            CREATE TABLE met.forcing_version (
                forcing_version_id TEXT PRIMARY KEY,
                model_id TEXT,
                source_id TEXT,
                cycle_time DATETIME,
                start_time DATETIME,
                end_time DATETIME,
                station_count INTEGER,
                forcing_package_uri TEXT,
                checksum TEXT,
                lineage_json TEXT
            )
            """
        )
    )


@contextmanager
def _store(
    *,
    create_hydro: bool = True,
    create_flood: bool = False,
    create_tile_layer: bool = True,
    create_met: bool = False,
) -> Iterator[Session]:
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
        if create_tile_layer:
            _create_tile_layer_table(connection)
        if create_met:
            _create_forcing_version_table(connection)
    session = Session(engine)
    try:
        yield session
    finally:
        session.close()


def _publisher(
    tmp_path: Any,
    *,
    database_url: str | None = None,
    published_artifact_root: Any | None = None,
    object_store_copyback_root: Any | None = None,
    object_store_prefix: str = "",
) -> TilePublisher:
    return TilePublisher(
        workspace_root=tmp_path / "workspace",
        object_store_root=tmp_path / "object-store",
        object_store_prefix=object_store_prefix,
        database_url=database_url,
        published_artifact_root=published_artifact_root,
        published_artifact_uri_prefix="published://",
        object_store_copyback_root=object_store_copyback_root,
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
    run_manifest_uri: str = "published://tiles/hydro/manifest.json",
    segments: int = 3,
    insert_run_row: bool = True,
) -> None:
    """Seed one hydro_run plus its q_down river_timeseries rows.

    ``insert_run_row=False`` adds river_timeseries for an additional
    river_network_version_id without re-inserting the (PK) hydro_run row, so a
    single run can span multiple river networks (F2).
    """
    if insert_run_row:
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


def _layer_id(run_id: str, network: str = "rivnet-1") -> str:
    return f"q_down_{run_id}_{network}"


def _seed_run_products(publisher: TilePublisher, run_id: str) -> None:
    publisher.object_store.write_bytes_atomic(
        f"runs/{run_id}/input/manifest.json",
        json.dumps({"run_id": run_id, "run": "manifest"}).encode("utf-8"),
    )
    publisher.object_store.write_bytes_atomic(f"runs/{run_id}/output/q.rivqdown.csv", b"seg,q\n1,2\n")
    publisher.object_store.write_bytes_atomic(f"runs/{run_id}/logs/shud_stdout.log", b"ok\n")


FORCING_KEY = "forcing/gfs/2024060112/basin-1/model-1"


def _seed_forcing_package(
    publisher: TilePublisher,
    session: Session,
    *,
    forcing_version_id: str = "forcing-1",
    package_key: str = FORCING_KEY,
    package_uri: str | None = None,
    checksum: str | None = None,
    lineage_checksum: str | None = None,
    lineage_json: Any | None = None,
    write_manifest: bool = True,
    write_output: bool = True,
    manifest_payload: dict[str, Any] | None = None,
) -> tuple[str, bytes]:
    output_key = f"{package_key}/forcing.tsd.forc"
    payload = manifest_payload or {
        "forcing_version_id": forcing_version_id,
        "files": [{"role": "tsd_forc", "uri": output_key}],
    }
    manifest_bytes = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    manifest_checksum = sha256(manifest_bytes).hexdigest()
    if write_output:
        publisher.object_store.write_bytes_atomic(output_key, b"forcing-bytes\n")
    if write_manifest:
        publisher.object_store.write_bytes_atomic(f"{package_key}/forcing_package.json", manifest_bytes)
    lineage = (
        lineage_json
        if lineage_json is not None
        else {
            "forcing_package_manifest_uri": f"{package_key}/forcing_package.json",
            "forcing_package_manifest_checksum": lineage_checksum or manifest_checksum,
            "output_files": payload["files"],
        }
    )
    session.execute(
        text(
            """
            INSERT INTO met.forcing_version (
                forcing_version_id, model_id, source_id, cycle_time, start_time, end_time,
                station_count, forcing_package_uri, checksum, lineage_json
            ) VALUES (
                :forcing_version_id, 'model-1', :source_id, :cycle_time, :cycle_time, :cycle_time,
                1, :forcing_package_uri, :checksum, :lineage_json
            )
            """
        ),
        {
            "forcing_version_id": forcing_version_id,
            "source_id": SOURCE_ID,
            "cycle_time": CYCLE_TIME,
            "forcing_package_uri": package_uri if package_uri is not None else f"{package_key}/",
            "checksum": manifest_checksum if checksum is None else checksum,
            "lineage_json": lineage if isinstance(lineage, str) else json.dumps(lineage),
        },
    )
    session.commit()
    return manifest_checksum, manifest_bytes


def _assert_no_qdown_display_advance(publisher: TilePublisher, cycle_id: str = CYCLE_ID) -> None:
    assert not publisher.object_store.exists(f"tiles/hydro/{cycle_id}/q-down/manifest.json")
    assert not publisher.object_store.exists(f"tiles/hydro/{cycle_id}/q-down/run-a/rivnet-1/manifest.json")


def _assert_copyback_publish_error(
    publisher: TilePublisher,
    *,
    expected_code: str,
    session_run_id: str = "run-a",
    seed_forcing: bool = True,
    forcing_options: dict[str, Any] | None = None,
) -> PublishError:
    with _store(create_flood=False, create_met=seed_forcing) as session:
        _insert_run(session, run_id=session_run_id, segments=3)
        if seed_forcing:
            _seed_forcing_package(publisher, session, **(forcing_options or {}))

        with pytest.raises(PublishError) as error:
            publisher._publish_qdown_from_database(session, CYCLE_ID)

        assert not session.execute(text("SELECT 1 FROM map.tile_layer")).first()

    assert error.value.error_code == expected_code
    return error.value


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
    assert layer_ids == {_layer_id("run-a"), _layer_id("run-b")}
    for layer in result.layers:
        assert layer["layer_type"] == "q_down_timeseries"

    # artifacts: manifest + log per run x network (network segment embedded in id).
    artifact_ids = {artifact["artifact_id"] for artifact in result.artifacts}
    assert "q_down_manifest_run-a_rivnet-1" in artifact_ids
    assert "q_down_log_run-a_rivnet-1" in artifact_ids
    assert "q_down_manifest_run-b_rivnet-1" in artifact_ids
    assert "q_down_log_run-b_rivnet-1" in artifact_ids

    # cycle manifest physically written to the object store.
    manifest_key = f"tiles/hydro/{CYCLE_ID}/q-down/manifest.json"
    assert publisher.object_store.exists(manifest_key)
    manifest = json.loads(publisher.object_store.read_bytes(manifest_key))
    assert manifest["published_basins"] == 2
    assert manifest["published_products"] == 2
    assert sorted(manifest["source_run_ids"]) == ["run-a", "run-b"]

    # per-run x network manifest also present under the network segment.
    assert publisher.object_store.exists(
        f"tiles/hydro/{CYCLE_ID}/q-down/run-a/rivnet-1/manifest.json"
    )


def test_publish_qdown_mirrors_artifacts_to_published_root(tmp_path: Any) -> None:
    published_root = tmp_path / "published"
    publisher = _publisher(tmp_path, published_artifact_root=published_root)
    with _store(create_flood=False) as session:
        _insert_run(session, run_id="run-a", segments=3)

        result = publisher._publish_qdown_from_database(session, CYCLE_ID)

    manifest_key = f"tiles/hydro/{CYCLE_ID}/q-down/manifest.json"
    run_manifest_key = f"tiles/hydro/{CYCLE_ID}/q-down/run-a/rivnet-1/manifest.json"
    log_key = f"tiles/hydro/{CYCLE_ID}/q-down/run-a/rivnet-1/publish.log.json"

    assert publisher.object_store.exists(manifest_key)
    assert (published_root / manifest_key).is_file()
    assert (published_root / run_manifest_key).is_file()
    assert (published_root / log_key).is_file()

    artifact_uris = {artifact["uri"] for artifact in result.artifacts}
    assert f"published://{run_manifest_key}" in artifact_uris
    assert f"published://{log_key}" in artifact_uris


def test_publish_qdown_copybacks_complete_run_products_to_shared_object_store(tmp_path: Any) -> None:
    copyback_root = tmp_path / "shared-object-store"
    publisher = _publisher(tmp_path, object_store_copyback_root=copyback_root)
    _seed_run_products(publisher, "run-a")

    with _store(create_flood=False, create_met=True) as session:
        _insert_run(session, run_id="run-a", segments=3)
        _checksum, forcing_manifest_bytes = _seed_forcing_package(publisher, session)

        result = publisher._publish_qdown_from_database(session, CYCLE_ID)

    assert json.loads((copyback_root / "runs/run-a/input/manifest.json").read_text(encoding="utf-8")) == {
        "run": "manifest",
        "run_id": "run-a",
    }
    assert (copyback_root / "runs/run-a/output/q.rivqdown.csv").read_bytes() == b"seg,q\n1,2\n"
    assert (copyback_root / "runs/run-a/logs/shud_stdout.log").read_bytes() == b"ok\n"
    assert (copyback_root / f"{FORCING_KEY}/forcing_package.json").read_bytes() == forcing_manifest_bytes
    assert (copyback_root / f"{FORCING_KEY}/forcing.tsd.forc").read_bytes() == b"forcing-bytes\n"
    assert oct((copyback_root / "runs/run-a").stat().st_mode & 0o777) == "0o755"
    assert oct((copyback_root / "runs/run-a/output/q.rivqdown.csv").stat().st_mode & 0o777) == "0o644"
    assert oct((copyback_root / FORCING_KEY).stat().st_mode & 0o777) == "0o755"
    assert oct((copyback_root / f"{FORCING_KEY}/forcing_package.json").stat().st_mode & 0o777) == "0o644"

    copyback = result.lineage["object_store_copyback"]
    assert copyback["status"] == "copied"
    assert copyback["run_ids"] == ["run-a"]
    run_byte_count = (
        len(json.dumps({"run_id": "run-a", "run": "manifest"}).encode("utf-8"))
        + len(b"seg,q\n1,2\n")
        + len(b"ok\n")
    )
    forcing_byte_count = len(forcing_manifest_bytes) + len(b"forcing-bytes\n")
    assert copyback["file_count"] == 5
    assert copyback["byte_count"] == run_byte_count + forcing_byte_count
    assert copyback["runs"] == [
        {
            "run_id": "run-a",
            "object_key": "runs/run-a",
            "file_count": 3,
            "byte_count": run_byte_count,
        }
    ]
    assert copyback["forcing_packages"] == [
        {
            "object_key": FORCING_KEY,
            "run_ids": ["run-a"],
            "forcing_version_ids": ["forcing-1"],
            "file_count": 2,
            "byte_count": forcing_byte_count,
        }
    ]


def test_publish_qdown_copyback_accepts_s3_prefix_package_and_manifest_file_uris(tmp_path: Any) -> None:
    copyback_root = tmp_path / "shared-object-store"
    publisher = _publisher(
        tmp_path,
        object_store_copyback_root=copyback_root,
        object_store_prefix="s3://bucket/prod",
    )
    _seed_run_products(publisher, "run-a")
    output_key = f"{FORCING_KEY}/forcing.tsd.forc"
    manifest_payload = {
        "forcing_version_id": "forcing-1",
        "files": [{"role": "tsd_forc", "uri": publisher.object_store.uri_for_key(output_key)}],
    }

    with _store(create_flood=False, create_met=True) as session:
        _insert_run(session, run_id="run-a", segments=3)
        _checksum, manifest_bytes = _seed_forcing_package(
            publisher,
            session,
            package_uri=publisher.object_store.uri_for_key(FORCING_KEY),
            manifest_payload=manifest_payload,
        )

        result = publisher._publish_qdown_from_database(session, CYCLE_ID)

    copyback = result.lineage["object_store_copyback"]
    assert copyback["forcing_packages"][0]["object_key"] == FORCING_KEY
    assert (copyback_root / f"{FORCING_KEY}/forcing_package.json").read_bytes() == manifest_bytes
    assert (copyback_root / output_key).read_bytes() == b"forcing-bytes\n"


def test_publish_qdown_copyback_missing_run_products_fails_publish(tmp_path: Any) -> None:
    publisher = _publisher(tmp_path, object_store_copyback_root=tmp_path / "shared-object-store")

    with _store(create_flood=False, create_met=True) as session:
        _insert_run(session, run_id="run-a", segments=3)
        _seed_forcing_package(publisher, session)

        with pytest.raises(PublishError) as error:
            publisher._publish_qdown_from_database(session, CYCLE_ID)

    assert error.value.error_code == "OBJECT_STORE_COPYBACK_SOURCE_MISSING"


def test_publish_qdown_copyback_empty_run_products_fails_before_publish_success(tmp_path: Any) -> None:
    publisher = _publisher(tmp_path, object_store_copyback_root=tmp_path / "shared-object-store")
    (Path(publisher.object_store.root) / "runs/run-a").mkdir(parents=True)

    error = _assert_copyback_publish_error(publisher, expected_code="OBJECT_STORE_COPYBACK_FAILED")

    assert error.details["run_id"] == "run-a"
    assert error.details["object_key"] == "runs/run-a"
    assert error.details["copyback_root"] == str((tmp_path / "shared-object-store").resolve())
    assert error.details["object_store_root"] == str(Path(publisher.object_store.root).resolve())
    assert not (tmp_path / "shared-object-store/runs/run-a").exists()


@pytest.mark.parametrize(
    ("seed_keys", "expected_detail"),
    [
        (
            [
                ("runs/run-a/input/manifest.json", b'{"run_id": "run-a"}'),
            ],
            "output",
        ),
        (
            [
                ("runs/run-a/input/manifest.json", b'{"run_id": "run-a"}'),
                ("runs/run-a/output/q.rivqdown.csv", b"seg,q\n1,2\n"),
            ],
            "logs",
        ),
    ],
)
def test_publish_qdown_copyback_requires_manifest_output_and_logs(
    tmp_path: Any,
    seed_keys: list[tuple[str, bytes]],
    expected_detail: str,
) -> None:
    publisher = _publisher(tmp_path, object_store_copyback_root=tmp_path / "shared-object-store")
    for key, content in seed_keys:
        publisher.object_store.write_bytes_atomic(key, content)

    error = _assert_copyback_publish_error(publisher, expected_code="OBJECT_STORE_COPYBACK_FAILED")

    assert expected_detail in error.details["error"]
    assert not (tmp_path / "shared-object-store/runs/run-a").exists()


def test_publish_qdown_copyback_manifest_without_run_id_is_allowed(tmp_path: Any) -> None:
    copyback_root = tmp_path / "shared-object-store"
    publisher = _publisher(tmp_path, object_store_copyback_root=copyback_root)
    publisher.object_store.write_bytes_atomic("runs/run-a/input/manifest.json", b'{"run": "manifest"}')
    publisher.object_store.write_bytes_atomic("runs/run-a/output/q.rivqdown.csv", b"seg,q\n1,2\n")
    publisher.object_store.write_bytes_atomic("runs/run-a/logs/shud_stdout.log", b"ok\n")

    with _store(create_flood=False, create_met=True) as session:
        _insert_run(session, run_id="run-a", segments=3)
        _seed_forcing_package(publisher, session)

        result = publisher._publish_qdown_from_database(session, CYCLE_ID)

    assert result.lineage["object_store_copyback"]["status"] == "copied"
    assert (copyback_root / "runs/run-a/input/manifest.json").read_bytes() == b'{"run": "manifest"}'


def test_publish_qdown_copyback_manifest_run_id_mismatch_fails(tmp_path: Any) -> None:
    publisher = _publisher(tmp_path, object_store_copyback_root=tmp_path / "shared-object-store")
    publisher.object_store.write_bytes_atomic("runs/run-a/input/manifest.json", b'{"run_id": "other-run"}')
    publisher.object_store.write_bytes_atomic("runs/run-a/output/q.rivqdown.csv", b"seg,q\n1,2\n")
    publisher.object_store.write_bytes_atomic("runs/run-a/logs/shud_stdout.log", b"ok\n")

    error = _assert_copyback_publish_error(publisher, expected_code="OBJECT_STORE_COPYBACK_FAILED")

    assert "does not match" in error.details["error"]


def test_publish_qdown_copyback_replaces_stale_target_tree(tmp_path: Any) -> None:
    copyback_root = tmp_path / "shared-object-store"
    publisher = _publisher(tmp_path, object_store_copyback_root=copyback_root)
    _seed_run_products(publisher, "run-a")
    stale_file = copyback_root / "runs/run-a/output/old-file.csv"
    stale_file.parent.mkdir(parents=True)
    stale_file.write_text("stale\n", encoding="utf-8")
    stale_log = copyback_root / "runs/run-a/logs/old.log"
    stale_log.parent.mkdir(parents=True, exist_ok=True)
    stale_log.write_text("old\n", encoding="utf-8")

    with _store(create_flood=False, create_met=True) as session:
        _insert_run(session, run_id="run-a", segments=3)
        _seed_forcing_package(publisher, session)

        publisher._publish_qdown_from_database(session, CYCLE_ID)

    assert not stale_file.exists()
    assert not stale_log.exists()
    copied_files = sorted(
        path.relative_to(copyback_root / "runs/run-a").as_posix()
        for path in (copyback_root / "runs/run-a").rglob("*")
        if path.is_file()
    )
    assert copied_files == [
        "input/manifest.json",
        "logs/shud_stdout.log",
        "output/q.rivqdown.csv",
    ]


def test_publish_qdown_copyback_rolls_back_run_tree_when_later_forcing_copy_fails(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    copyback_root = tmp_path / "shared-object-store"
    publisher = _publisher(tmp_path, object_store_copyback_root=copyback_root)
    _seed_run_products(publisher, "run-a")
    old_manifest = copyback_root / "runs/run-a/input/manifest.json"
    old_manifest.parent.mkdir(parents=True)
    old_manifest.write_bytes(b'{"run_id":"run-a","old":true}\n')
    old_output = copyback_root / "runs/run-a/output/q.rivqdown.csv"
    old_output.parent.mkdir(parents=True)
    old_output.write_bytes(b"old\n")
    original_write = type(publisher.object_store).write_bytes_atomic

    def fail_forcing_target_write(self: Any, key_or_uri: str, content: bytes) -> str:
        if self.root == copyback_root.resolve() and str(key_or_uri).startswith("forcing/"):
            raise ObjectStoreError(f"forcing target write blocked for {key_or_uri}")
        return original_write(self, key_or_uri, content)

    monkeypatch.setattr("packages.common.object_store.LocalObjectStore.write_bytes_atomic", fail_forcing_target_write)

    with _store(create_flood=False, create_met=True) as session:
        _insert_run(session, run_id="run-a", segments=3)
        _seed_forcing_package(publisher, session)

        with pytest.raises(PublishError) as error:
            publisher._publish_qdown_from_database(session, CYCLE_ID)

    assert error.value.error_code == "OBJECT_STORE_COPYBACK_FAILED"
    assert error.value.details["object_key"] == FORCING_KEY
    assert old_manifest.read_bytes() == b'{"run_id":"run-a","old":true}\n'
    assert old_output.read_bytes() == b"old\n"
    assert not (copyback_root / "runs/run-a/logs/shud_stdout.log").exists()
    assert not (copyback_root / FORCING_KEY).exists()
    assert not (copyback_root / f"{FORCING_KEY}/forcing_package.json").exists()
    _assert_no_qdown_display_advance(publisher)


def test_publish_qdown_copyback_revalidates_copied_forcing_package_before_success(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    copyback_root = tmp_path / "shared-object-store"
    publisher = _publisher(tmp_path, object_store_copyback_root=copyback_root)
    _seed_run_products(publisher, "run-a")
    original_validate = publisher._validate_forcing_source_tree
    source_validation_calls = 0

    def mutate_after_copy_source_validation(ref: Any, source_tree: Any, store: Any) -> None:
        nonlocal source_validation_calls
        original_validate(ref, source_tree, store)
        if store.root == publisher.object_store.root and ref.object_key == FORCING_KEY:
            source_validation_calls += 1
            if source_validation_calls == 2:
                publisher.object_store.write_bytes_atomic(
                    f"{FORCING_KEY}/forcing_package.json",
                    b'{"tampered":true}',
                )

    monkeypatch.setattr(publisher, "_validate_forcing_source_tree", mutate_after_copy_source_validation)

    with _store(create_flood=False, create_met=True) as session:
        _insert_run(session, run_id="run-a", segments=3)
        _seed_forcing_package(publisher, session)

        with pytest.raises(PublishError) as error:
            publisher._publish_qdown_from_database(session, CYCLE_ID)

    assert error.value.error_code == "OBJECT_STORE_COPYBACK_FAILED"
    assert error.value.details["object_key"] == FORCING_KEY
    assert "checksum mismatch" in error.value.details["error"]
    assert source_validation_calls == 2
    assert not (copyback_root / "runs/run-a").exists()
    assert not (copyback_root / FORCING_KEY).exists()
    _assert_no_qdown_display_advance(publisher)


def test_publish_qdown_copyback_rejects_over_limit_source_tree_before_target_promotion(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    copyback_root = tmp_path / "shared-object-store"
    publisher = _publisher(tmp_path, object_store_copyback_root=copyback_root)
    _seed_run_products(publisher, "run-a")
    monkeypatch.setattr("services.tile_publisher.publisher._COPYBACK_MAX_TOTAL_BYTES", 1)

    with _store(create_flood=False, create_met=True) as session:
        _insert_run(session, run_id="run-a", segments=3)
        _seed_forcing_package(publisher, session)

        with pytest.raises(PublishError) as error:
            publisher._publish_qdown_from_database(session, CYCLE_ID)

    assert error.value.error_code == "OBJECT_STORE_COPYBACK_FAILED"
    assert "maximum total bytes limit" in error.value.details["error"]
    assert not (copyback_root / "runs/run-a").exists()
    assert not (copyback_root / "forcing").exists()
    _assert_no_qdown_display_advance(publisher)


def test_publish_qdown_copyback_rejects_single_file_over_per_file_limit_before_target_promotion(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    copyback_root = tmp_path / "shared-object-store"
    publisher = _publisher(tmp_path, object_store_copyback_root=copyback_root)
    _seed_run_products(publisher, "run-a")
    publisher.object_store.write_bytes_atomic("runs/run-a/output/large.bin", b"x" * 41)
    monkeypatch.setattr("services.tile_publisher.publisher._COPYBACK_MAX_FILE_BYTES", 40)

    with _store(create_flood=False, create_met=True) as session:
        _insert_run(session, run_id="run-a", segments=3)
        _seed_forcing_package(publisher, session)

        with pytest.raises(PublishError) as error:
            publisher._publish_qdown_from_database(session, CYCLE_ID)

    assert error.value.error_code == "OBJECT_STORE_COPYBACK_FAILED"
    assert error.value.details["object_key"] == "runs/run-a"
    assert "maximum file bytes limit" in error.value.details["error"]
    assert not (copyback_root / "runs/run-a").exists()
    assert not (copyback_root / "forcing").exists()
    _assert_no_qdown_display_advance(publisher)


def test_publish_qdown_copyback_cleanup_failure_blocks_publish_and_rolls_back(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    copyback_root = tmp_path / "shared-object-store"
    publisher = _publisher(tmp_path, object_store_copyback_root=copyback_root)
    _seed_run_products(publisher, "run-a")
    old_manifest = copyback_root / "runs/run-a/input/manifest.json"
    old_manifest.parent.mkdir(parents=True)
    old_manifest.write_bytes(b'{"run_id":"run-a","old":true}\n')
    old_output = copyback_root / "runs/run-a/output/q.rivqdown.csv"
    old_output.parent.mkdir(parents=True)
    old_output.write_bytes(b"old\n")
    publisher_module = __import__("services.tile_publisher.publisher", fromlist=["rmtree_no_follow"])
    original_rmtree = publisher_module.rmtree_no_follow

    def fail_backup_cleanup(
        path: Path,
        *,
        containment_root: Path | None = None,
        missing_ok: bool = False,
    ) -> None:
        if ".copyback-backup." in Path(path).name:
            raise OSError("backup cleanup blocked")
        original_rmtree(path, containment_root=containment_root, missing_ok=missing_ok)

    monkeypatch.setattr(publisher_module, "rmtree_no_follow", fail_backup_cleanup)

    with _store(create_flood=False, create_met=True) as session:
        _insert_run(session, run_id="run-a", segments=3)
        _seed_forcing_package(publisher, session)

        with pytest.raises(PublishError) as error:
            publisher._publish_qdown_from_database(session, CYCLE_ID)

    assert error.value.error_code == "OBJECT_STORE_COPYBACK_FAILED"
    assert "backup cleanup blocked" in error.value.details["error"]
    assert old_manifest.read_bytes() == b'{"run_id":"run-a","old":true}\n'
    assert old_output.read_bytes() == b"old\n"
    assert not (copyback_root / "runs/run-a/logs/shud_stdout.log").exists()
    assert not (copyback_root / FORCING_KEY).exists()
    _assert_no_qdown_display_advance(publisher)


def test_replace_directory_tree_restores_previous_target_when_promotion_fails(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "shared-object-store"
    target_dir = root / "runs/run-a"
    temp_dir = root / "runs/run-a.copyback.temp"
    (target_dir / "output").mkdir(parents=True)
    (target_dir / "output/old-file.csv").write_text("old\n", encoding="utf-8")
    (temp_dir / "output").mkdir(parents=True)
    (temp_dir / "output/new-file.csv").write_text("new\n", encoding="utf-8")
    original_replace = __import__("os").replace
    calls = 0

    def fail_temp_promotion(src: str, dst: str, *, src_dir_fd: int, dst_dir_fd: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("promotion blocked")
        original_replace(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

    monkeypatch.setattr("services.tile_publisher.publisher.os.replace", fail_temp_promotion)

    with pytest.raises(Exception, match="promotion blocked"):
        _replace_directory_tree_no_follow(temp_dir, target_dir, containment_root=root)

    assert (target_dir / "output/old-file.csv").read_text(encoding="utf-8") == "old\n"
    assert not (target_dir / "output/new-file.csv").exists()
    assert temp_dir.is_dir()


def test_publish_qdown_copyback_exact_root_validates_complete_run_tree(tmp_path: Any) -> None:
    publisher = _publisher(tmp_path, object_store_copyback_root=tmp_path / "object-store")
    _seed_run_products(publisher, "run-a")

    with _store(create_flood=False, create_met=True) as session:
        _insert_run(session, run_id="run-a", segments=3)
        _seed_forcing_package(publisher, session)

        result = publisher._publish_qdown_from_database(session, CYCLE_ID)

    copyback = result.lineage["object_store_copyback"]
    assert copyback["status"] == "skipped"
    assert copyback["reason"] == "copyback_root_matches_object_store_root"
    assert copyback["root"] == str(Path(publisher.object_store.root).resolve())
    assert copyback["run_ids"] == ["run-a"]
    assert copyback["runs"] == [{"run_id": "run-a", "object_key": "runs/run-a"}]
    assert copyback["forcing_packages"] == [
        {"object_key": FORCING_KEY, "run_ids": ["run-a"], "forcing_version_ids": ["forcing-1"]}
    ]
    assert publisher.object_store.exists(f"tiles/hydro/{CYCLE_ID}/q-down/manifest.json")


def test_publish_qdown_copyback_exact_root_rejects_incomplete_run_tree(tmp_path: Any) -> None:
    publisher = _publisher(tmp_path, object_store_copyback_root=tmp_path / "object-store")
    publisher.object_store.write_bytes_atomic("runs/run-a/input/manifest.json", b'{"run_id": "run-a"}')

    error = _assert_copyback_publish_error(publisher, expected_code="OBJECT_STORE_COPYBACK_FAILED")

    assert "output" in error.details["error"]
    _assert_no_qdown_display_advance(publisher)


@pytest.mark.parametrize(
    ("forcing_options", "expected_error"),
    [
        ({"write_manifest": False}, "manifest is missing"),
        ({"checksum": "0" * 64, "lineage_checksum": "0" * 64}, "checksum mismatch"),
        ({"write_output": False}, "output file is missing"),
    ],
)
def test_publish_qdown_copyback_exact_root_rejects_forcing_integrity_failure(
    tmp_path: Any,
    forcing_options: dict[str, Any],
    expected_error: str,
) -> None:
    publisher = _publisher(tmp_path, object_store_copyback_root=tmp_path / "object-store")
    _seed_run_products(publisher, "run-a")

    with _store(create_flood=False, create_met=True) as session:
        _insert_run(session, run_id="run-a", segments=3)
        _seed_forcing_package(publisher, session, **forcing_options)

        with pytest.raises(PublishError) as error:
            publisher._publish_qdown_from_database(session, CYCLE_ID)

    assert error.value.error_code == "OBJECT_STORE_COPYBACK_FAILED"
    assert error.value.details["run_id"] == "run-a"
    assert error.value.details["forcing_version_id"] == "forcing-1"
    assert error.value.details["object_key"] == FORCING_KEY
    assert expected_error in error.value.details["error"]
    _assert_no_qdown_display_advance(publisher)


def test_publish_qdown_copyback_rejects_source_ancestor_symlink_before_target_create(tmp_path: Any) -> None:
    publisher = _publisher(tmp_path, object_store_copyback_root=tmp_path / "shared-object-store")
    object_store_root = Path(publisher.object_store.root)
    outside_runs = tmp_path / "outside-runs"
    outside_run = outside_runs / "run-a"
    (outside_run / "input").mkdir(parents=True)
    (outside_run / "output").mkdir()
    (outside_run / "logs").mkdir()
    (outside_run / "input/manifest.json").write_text('{"run_id": "run-a"}', encoding="utf-8")
    (outside_run / "output/q.rivqdown.csv").write_text("seg,q\n1,2\n", encoding="utf-8")
    (outside_run / "logs/shud_stdout.log").write_text("ok\n", encoding="utf-8")
    (object_store_root / "runs").symlink_to(outside_runs, target_is_directory=True)

    error = _assert_copyback_publish_error(publisher, expected_code="OBJECT_STORE_COPYBACK_FAILED")

    assert error.details["object_key"] == "runs/run-a"
    assert "symlink" in error.details["error"]
    assert not (tmp_path / "shared-object-store/runs/run-a").exists()


def test_publish_qdown_copyback_rejects_copyback_root_ancestor_symlink_before_target_create(
    tmp_path: Any,
) -> None:
    shared_parent = tmp_path / "shared-parent"
    shared_parent.mkdir()
    link_parent = tmp_path / "copyback-link"
    link_parent.symlink_to(shared_parent, target_is_directory=True)
    publisher = _publisher(tmp_path, object_store_copyback_root=link_parent / "shared-object-store")
    _seed_run_products(publisher, "run-a")

    error = _assert_copyback_publish_error(publisher, expected_code="OBJECT_STORE_COPYBACK_FAILED")

    assert "symlink" in error.details["error"]
    assert not (shared_parent / "shared-object-store/runs/run-a").exists()


@pytest.mark.parametrize(
    "copyback_root",
    [
        "object-store/copyback",
        "object-store/runs/run-a/copyback",
    ],
)
def test_publish_qdown_copyback_rejects_copyback_root_inside_object_store_before_target_create(
    tmp_path: Any,
    copyback_root: str,
) -> None:
    publisher = _publisher(tmp_path, object_store_copyback_root=tmp_path / copyback_root)
    _seed_run_products(publisher, "run-a")

    error = _assert_copyback_publish_error(publisher, expected_code="OBJECT_STORE_COPYBACK_FAILED")

    assert error.details["reason"] == "copyback_root_object_store_root_overlap"
    assert not (tmp_path / "object-store/runs/run-a/copyback/runs/run-a").exists()


def test_publish_qdown_copyback_rejects_object_store_inside_copyback_root(tmp_path: Any) -> None:
    copyback_root = tmp_path / "shared"
    object_store_root = copyback_root / "object-store"
    publisher = TilePublisher(
        workspace_root=tmp_path / "workspace",
        object_store_root=object_store_root,
        object_store_prefix="",
        object_store_copyback_root=copyback_root,
    )
    _seed_run_products(publisher, "run-a")

    error = _assert_copyback_publish_error(publisher, expected_code="OBJECT_STORE_COPYBACK_FAILED")

    assert error.details["reason"] == "copyback_root_object_store_root_overlap"
    assert not (copyback_root / "runs/run-a").exists()


def test_publish_qdown_copyback_normalizes_source_read_object_store_error(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publisher = _publisher(tmp_path, object_store_copyback_root=tmp_path / "shared-object-store")
    _seed_run_products(publisher, "run-a")
    original_read = publisher.object_store.read_bytes

    def fail_read(self: Any, key_or_uri: str) -> bytes:
        if self.root == publisher.object_store.root and key_or_uri.startswith("runs/run-a/"):
            raise ObjectStoreError(f"read blocked for {key_or_uri}")
        return original_read(key_or_uri)

    monkeypatch.setattr("packages.common.object_store.LocalObjectStore.read_bytes", fail_read)

    error = _assert_copyback_publish_error(publisher, expected_code="OBJECT_STORE_COPYBACK_FAILED")

    assert error.details["run_id"] == "run-a"
    assert error.details["object_key"] == "runs/run-a"
    assert "read blocked" in error.details["error"]


def test_publish_qdown_copyback_normalizes_target_write_object_store_error(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    copyback_root = tmp_path / "shared-object-store"
    publisher = _publisher(tmp_path, object_store_copyback_root=copyback_root)
    _seed_run_products(publisher, "run-a")
    original_write = type(publisher.object_store).write_bytes_atomic

    def fail_write(self: Any, key_or_uri: str, content: bytes) -> str:
        if self.root == copyback_root.resolve():
            raise ObjectStoreError(f"write blocked for {key_or_uri}")
        return original_write(self, key_or_uri, content)

    monkeypatch.setattr("packages.common.object_store.LocalObjectStore.write_bytes_atomic", fail_write)

    error = _assert_copyback_publish_error(publisher, expected_code="OBJECT_STORE_COPYBACK_FAILED")

    assert error.details["run_id"] == "run-a"
    assert error.details["object_key"] == "runs/run-a"
    assert "write blocked" in error.details["error"]


def test_publish_qdown_copyback_normalizes_unsafe_run_id(tmp_path: Any) -> None:
    publisher = _publisher(tmp_path, object_store_copyback_root=tmp_path / "shared-object-store")

    error = _assert_copyback_publish_error(
        publisher,
        expected_code="OBJECT_STORE_COPYBACK_FAILED",
        session_run_id="bad/run",
    )

    assert error.details["run_id"] == "bad/run"
    assert "Unsafe run_id" in error.details["error"]


def test_publish_qdown_copyback_failure_does_not_advance_stable_display_artifacts(tmp_path: Any) -> None:
    published_root = tmp_path / "published"
    publisher = _publisher(
        tmp_path,
        published_artifact_root=published_root,
        object_store_copyback_root=tmp_path / "shared-object-store",
    )
    _seed_run_products(publisher, "run-a")

    with _store(create_flood=False, create_met=True) as session:
        _insert_run(session, run_id="run-a", segments=3)
        _seed_forcing_package(publisher, session)
        publisher._publish_qdown_from_database(session, CYCLE_ID)
        cycle_manifest_key = f"tiles/hydro/{CYCLE_ID}/q-down/manifest.json"
        previous_cycle_manifest = publisher.object_store.read_bytes(cycle_manifest_key)

        _insert_run(session, run_id="run-b", segments=2)
        with pytest.raises(PublishError) as error:
            publisher._publish_qdown_from_database(session, CYCLE_ID)

    run_b_manifest_key = f"tiles/hydro/{CYCLE_ID}/q-down/run-b/rivnet-1/manifest.json"
    assert error.value.error_code == "OBJECT_STORE_COPYBACK_SOURCE_MISSING"
    assert publisher.object_store.read_bytes(cycle_manifest_key) == previous_cycle_manifest
    assert json.loads(previous_cycle_manifest)["source_run_ids"] == ["run-a"]
    assert not publisher.object_store.exists(run_b_manifest_key)
    assert not (published_root / run_b_manifest_key).exists()
    assert json.loads((published_root / cycle_manifest_key).read_text(encoding="utf-8"))["source_run_ids"] == [
        "run-a"
    ]
    assert not (published_root / "forcing").exists()


def test_publish_qdown_copyback_deduplicates_shared_forcing_package(tmp_path: Any) -> None:
    copyback_root = tmp_path / "shared-object-store"
    publisher = _publisher(tmp_path, object_store_copyback_root=copyback_root)
    _seed_run_products(publisher, "run-a")
    _seed_run_products(publisher, "run-b")

    with _store(create_flood=False, create_met=True) as session:
        _insert_run(session, run_id="run-a", segments=3)
        _insert_run(session, run_id="run-b", segments=2)
        _checksum, manifest_bytes = _seed_forcing_package(publisher, session)

        result = publisher._publish_qdown_from_database(session, CYCLE_ID)

    copyback = result.lineage["object_store_copyback"]
    assert len(copyback["runs"]) == 2
    assert copyback["forcing_packages"] == [
        {
            "object_key": FORCING_KEY,
            "run_ids": ["run-a", "run-b"],
            "forcing_version_ids": ["forcing-1"],
            "file_count": 2,
            "byte_count": len(manifest_bytes) + len(b"forcing-bytes\n"),
        }
    ]
    assert (copyback_root / f"{FORCING_KEY}/forcing_package.json").read_bytes() == manifest_bytes


def test_publish_qdown_copyback_deduplicates_copy_but_validates_sibling_forcing_refs(
    tmp_path: Any,
) -> None:
    copyback_root = tmp_path / "shared-object-store"
    publisher = _publisher(tmp_path, object_store_copyback_root=copyback_root)
    _seed_run_products(publisher, "run-a")
    _seed_run_products(publisher, "run-b")

    with _store(create_flood=False, create_met=True) as session:
        _insert_run(session, run_id="run-a", segments=3, forcing_version_id="forcing-1")
        _insert_run(session, run_id="run-b", segments=2, forcing_version_id="forcing-2")
        checksum, _manifest_bytes = _seed_forcing_package(publisher, session, forcing_version_id="forcing-1")
        _seed_forcing_package(
            publisher,
            session,
            forcing_version_id="forcing-2",
            write_manifest=False,
            write_output=False,
            checksum=checksum,
            lineage_checksum=checksum,
            lineage_json={
                "forcing_package_manifest_uri": f"{FORCING_KEY}/forcing_package.json",
                "forcing_package_manifest_checksum": checksum,
                "output_files": [{"role": "tsd_forc", "uri": f"{FORCING_KEY}/missing.tsd.forc"}],
            },
        )

        with pytest.raises(PublishError) as error:
            publisher._publish_qdown_from_database(session, CYCLE_ID)

    assert error.value.error_code == "OBJECT_STORE_COPYBACK_FAILED"
    assert error.value.details["run_id"] == "run-b"
    assert error.value.details["forcing_version_id"] == "forcing-2"
    assert error.value.details["object_key"] == FORCING_KEY
    assert "output file is missing" in error.value.details["error"]
    assert not (copyback_root / "runs/run-a").exists()
    assert not (copyback_root / "runs/run-b").exists()
    assert not (copyback_root / FORCING_KEY).exists()
    _assert_no_qdown_display_advance(publisher)


@pytest.mark.parametrize(
    ("create_met", "forcing_options", "missing_field"),
    [
        (False, {}, "forcing_version"),
        (True, {"package_uri": ""}, "forcing_package_uri"),
        (True, {"checksum": ""}, "checksum"),
    ],
)
def test_publish_qdown_copyback_missing_forcing_metadata_fails_before_artifact_advance(
    tmp_path: Any,
    create_met: bool,
    forcing_options: dict[str, Any],
    missing_field: str,
) -> None:
    publisher = _publisher(tmp_path, object_store_copyback_root=tmp_path / "shared-object-store")
    _seed_run_products(publisher, "run-a")

    with _store(create_flood=False, create_met=create_met) as session:
        _insert_run(session, run_id="run-a", segments=3)
        if create_met:
            _seed_forcing_package(publisher, session, **forcing_options)

        with pytest.raises(PublishError) as error:
            publisher._publish_qdown_from_database(session, CYCLE_ID)

    assert error.value.error_code == "OBJECT_STORE_COPYBACK_FAILED"
    assert error.value.details["run_id"] == "run-a"
    assert error.value.details["forcing_version_id"] == "forcing-1"
    assert error.value.details["missing_field"] == missing_field
    assert not publisher.object_store.exists(f"tiles/hydro/{CYCLE_ID}/q-down/manifest.json")


@pytest.mark.parametrize(
    ("package_key", "mismatch_field"),
    [
        ("forcing/gfs/2026061400/basin-1/model-1", "cycle_time"),
        ("forcing/gfs/2024060112/basin-2/model-1", "basin_version_id"),
    ],
)
def test_publish_qdown_copyback_rejects_forcing_key_identity_mismatch_before_display_advance(
    tmp_path: Any,
    package_key: str,
    mismatch_field: str,
) -> None:
    publisher = _publisher(tmp_path, object_store_copyback_root=tmp_path / "shared-object-store")
    _seed_run_products(publisher, "run-a")

    with _store(create_flood=False, create_met=True) as session:
        _insert_run(session, run_id="run-a", segments=3)
        _seed_forcing_package(publisher, session, package_key=package_key)

        with pytest.raises(PublishError) as error:
            publisher._publish_qdown_from_database(session, CYCLE_ID)

    assert error.value.error_code == "OBJECT_STORE_COPYBACK_FAILED"
    assert error.value.details["run_id"] == "run-a"
    assert error.value.details["forcing_version_id"] == "forcing-1"
    assert error.value.details["object_key"] == package_key
    assert "identity does not match" in error.value.details["error"]
    assert mismatch_field in error.value.details["error"]
    _assert_no_qdown_display_advance(publisher)
    assert not (tmp_path / "shared-object-store/runs/run-a").exists()


@pytest.mark.parametrize(
    ("forcing_options", "expected_error"),
    [
        ({"write_manifest": False}, "manifest is missing"),
        ({"checksum": "0" * 64, "lineage_checksum": "0" * 64}, "checksum mismatch"),
        ({"lineage_checksum": "1" * 64}, "lineage manifest checksum"),
        ({"write_output": False}, "output file is missing"),
    ],
)
def test_publish_qdown_copyback_forcing_integrity_failures_do_not_publish_display(
    tmp_path: Any,
    forcing_options: dict[str, Any],
    expected_error: str,
) -> None:
    publisher = _publisher(tmp_path, object_store_copyback_root=tmp_path / "shared-object-store")
    _seed_run_products(publisher, "run-a")

    with _store(create_flood=False, create_met=True) as session:
        _insert_run(session, run_id="run-a", segments=3)
        _seed_forcing_package(publisher, session, **forcing_options)

        with pytest.raises(PublishError) as error:
            publisher._publish_qdown_from_database(session, CYCLE_ID)

    assert error.value.error_code in {"OBJECT_STORE_COPYBACK_FAILED", "OBJECT_STORE_COPYBACK_SOURCE_MISSING"}
    assert error.value.details["run_id"] == "run-a"
    assert error.value.details["forcing_version_id"] == "forcing-1"
    assert error.value.details["object_key"] == FORCING_KEY
    assert expected_error in error.value.details["error"]
    assert not publisher.object_store.exists(f"tiles/hydro/{CYCLE_ID}/q-down/manifest.json")


def test_publish_qdown_copyback_validates_manifest_output_file_checksum_when_present(tmp_path: Any) -> None:
    publisher = _publisher(tmp_path, object_store_copyback_root=tmp_path / "shared-object-store")
    _seed_run_products(publisher, "run-a")
    manifest_payload = {
        "forcing_version_id": "forcing-1",
        "files": [{"role": "tsd_forc", "uri": f"{FORCING_KEY}/forcing.tsd.forc", "checksum": "0" * 64}],
    }

    with _store(create_flood=False, create_met=True) as session:
        _insert_run(session, run_id="run-a", segments=3)
        _seed_forcing_package(publisher, session, manifest_payload=manifest_payload)

        with pytest.raises(PublishError) as error:
            publisher._publish_qdown_from_database(session, CYCLE_ID)

    assert error.value.error_code == "OBJECT_STORE_COPYBACK_FAILED"
    assert error.value.details["object_key"] == FORCING_KEY
    assert "output file checksum mismatch" in error.value.details["error"]
    _assert_no_qdown_display_advance(publisher)


@pytest.mark.parametrize(
    ("package_uri", "expected_error"),
    [
        ("runs/run-a/", "must use forcing/<source>/<cycle>/<basin>/<model>"),
        ("forcing/gfs/2026061400/basin-1/model-1/extra", "must use forcing/<source>/<cycle>/<basin>/<model>"),
        ("", "Missing forcing metadata field: forcing_package_uri"),
        ("forcing/gfs//basin-1/model-1", "must not contain empty segments"),
        ("forcing/gfs/../basin-1/model-1", "must not contain '..'"),
        ("/forcing/gfs/2026061400/basin-1/model-1", "must not be absolute"),
    ],
)
def test_publish_qdown_copyback_rejects_unsafe_forcing_keys(
    tmp_path: Any,
    package_uri: str,
    expected_error: str,
) -> None:
    publisher = _publisher(tmp_path, object_store_copyback_root=tmp_path / "shared-object-store")
    _seed_run_products(publisher, "run-a")

    with _store(create_flood=False, create_met=True) as session:
        _insert_run(session, run_id="run-a", segments=3)
        _seed_forcing_package(publisher, session, package_uri=package_uri)

        with pytest.raises(PublishError) as error:
            publisher._publish_qdown_from_database(session, CYCLE_ID)

    assert error.value.error_code == "OBJECT_STORE_COPYBACK_FAILED"
    assert error.value.details["run_id"] == "run-a"
    assert error.value.details["forcing_version_id"] == "forcing-1"
    assert expected_error in error.value.details["error"]
    _assert_no_qdown_display_advance(publisher)
    assert not (tmp_path / "shared-object-store/forcing").exists()


def test_publish_qdown_copyback_rejects_forcing_source_symlink(tmp_path: Any) -> None:
    publisher = _publisher(tmp_path, object_store_copyback_root=tmp_path / "shared-object-store")
    _seed_run_products(publisher, "run-a")
    object_store_root = Path(publisher.object_store.root)
    outside_forcing = tmp_path / "outside-forcing"
    (outside_forcing / "gfs/2026061400/basin-1/model-1").mkdir(parents=True)
    (object_store_root / "forcing").symlink_to(outside_forcing, target_is_directory=True)

    with _store(create_flood=False, create_met=True) as session:
        _insert_run(session, run_id="run-a", segments=3)
        _seed_forcing_package(
            publisher,
            session,
            write_manifest=False,
            write_output=False,
        )

        with pytest.raises(PublishError) as error:
            publisher._publish_qdown_from_database(session, CYCLE_ID)

    assert error.value.error_code == "OBJECT_STORE_COPYBACK_FAILED"
    assert error.value.details["object_key"] == FORCING_KEY
    assert "symlink" in error.value.details["error"]
    _assert_no_qdown_display_advance(publisher)
    assert not (tmp_path / "shared-object-store/forcing").exists()


def test_publish_qdown_copyback_rejects_forcing_source_regular_file(tmp_path: Any) -> None:
    publisher = _publisher(tmp_path, object_store_copyback_root=tmp_path / "shared-object-store")
    _seed_run_products(publisher, "run-a")
    source_file = Path(publisher.object_store.root) / FORCING_KEY
    source_file.parent.mkdir(parents=True)
    source_file.write_text("not a directory", encoding="utf-8")

    with _store(create_flood=False, create_met=True) as session:
        _insert_run(session, run_id="run-a", segments=3)
        _seed_forcing_package(
            publisher,
            session,
            write_manifest=False,
            write_output=False,
        )

        with pytest.raises(PublishError) as error:
            publisher._publish_qdown_from_database(session, CYCLE_ID)

    assert error.value.error_code == "OBJECT_STORE_COPYBACK_FAILED"
    assert error.value.details["object_key"] == FORCING_KEY
    assert "not a directory" in error.value.details["error"]
    _assert_no_qdown_display_advance(publisher)
    assert not (tmp_path / "shared-object-store/forcing").exists()


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
        # Sole run is identity-incomplete -> the whole cycle has nothing
        # publishable, so the cycle-level PUBLISH_IDENTITY_INCOMPLETE is raised.
        _insert_run(session, run_id="run-a", segments=2, forcing_version_id=None)

        with pytest.raises(PublishError) as excinfo:
            publisher._publish_qdown_from_database(session, CYCLE_ID)

    error = excinfo.value
    assert error.error_code == "PUBLISH_IDENTITY_INCOMPLETE"
    # Cycle-level shape (F5): blockers, not raw missing_fields, carry the failure.
    assert error.details["quality_state"] == "unavailable"
    blocker_codes = {blocker["code"] for blocker in error.details["residual_blockers"]}
    assert "PUBLISH_IDENTITY_INCOMPLETE" in blocker_codes


def test_publish_qdown_missing_model_id_raises_identity_incomplete(tmp_path: Any) -> None:
    publisher = _publisher(tmp_path)
    with _store(create_flood=False) as session:
        _insert_run(session, run_id="run-a", segments=2, model_id=None)

        with pytest.raises(PublishError) as excinfo:
            publisher._publish_qdown_from_database(session, CYCLE_ID)

    error = excinfo.value
    assert error.error_code == "PUBLISH_IDENTITY_INCOMPLETE"
    blockers = error.details["residual_blockers"]
    assert any(
        blocker["code"] == "PUBLISH_IDENTITY_INCOMPLETE" and blocker.get("run_id") == "run-a"
        for blocker in blockers
    )


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
    assert first_ids == second_ids == [_layer_id("run-a"), _layer_id("run-b")]

    # Object store holds a single canonical manifest (overwritten, not duplicated).
    manifest_key = f"tiles/hydro/{CYCLE_ID}/q-down/manifest.json"
    manifest = json.loads(publisher.object_store.read_bytes(manifest_key))
    assert manifest["published_basins"] == 2


# --------------------------------------------------------------------------- #
# F1: map.tile_layer DB registration (and never-break missing-table tolerance)
# --------------------------------------------------------------------------- #
def test_publish_qdown_registers_layer_in_tile_layer_table(tmp_path: Any) -> None:
    publisher = _publisher(tmp_path)
    with _store(create_flood=False, create_tile_layer=True) as session:
        _insert_run(session, run_id="run-a", segments=3)

        result = publisher._publish_qdown_from_database(session, CYCLE_ID)

        # F1: a tile_layer row was upserted + committed for the published layer.
        row = session.execute(
            text(
                """
                SELECT layer_id, layer_type, source_run_id, variable,
                       tile_format, tile_uri_template, published_flag
                FROM map.tile_layer
                WHERE layer_id = :layer_id
                """
            ),
            {"layer_id": _layer_id("run-a")},
        ).mappings().first()

    assert result.lineage["db_registered"] is True
    assert row is not None
    assert row["layer_type"] == "q_down_timeseries"
    assert row["source_run_id"] == "run-a"
    assert row["variable"] == "q_down"
    assert row["tile_format"] == "geojson_timeseries"
    assert bool(row["published_flag"]) is True

    # tile_uri_template points at the written per-run manifest URI.
    manifest_uri = publisher.object_store.uri_for_key(
        f"tiles/hydro/{CYCLE_ID}/q-down/run-a/rivnet-1/manifest.json"
    )
    assert row["tile_uri_template"] == manifest_uri


def test_publish_qdown_tolerates_missing_tile_layer_table(tmp_path: Any) -> None:
    publisher = _publisher(tmp_path)
    # never-break: no map.tile_layer table at all.
    with _store(create_flood=False, create_tile_layer=False) as session:
        _insert_run(session, run_id="run-a", segments=2)

        result = publisher._publish_qdown_from_database(session, CYCLE_ID)

    # Still published to the object store, but no DB registration happened.
    assert result.status == "published"
    assert len(result.layers) == 1
    assert result.lineage["db_registered"] is False
    assert publisher.object_store.exists(
        f"tiles/hydro/{CYCLE_ID}/q-down/run-a/rivnet-1/manifest.json"
    )


# --------------------------------------------------------------------------- #
# F2: one run spanning two river networks -> two distinct, non-colliding layers
# --------------------------------------------------------------------------- #
def test_publish_qdown_run_across_two_networks_yields_two_layers(tmp_path: Any) -> None:
    publisher = _publisher(tmp_path)
    with _store(create_flood=False, create_tile_layer=True) as session:
        # Single run_id, q_down rows split over two river_network_version_ids.
        _insert_run(session, run_id="run-a", river_network_version_id="rivnet-1", segments=3)
        _insert_run(
            session,
            run_id="run-a",
            river_network_version_id="rivnet-2",
            segments=2,
            insert_run_row=False,
        )

        result = publisher._publish_qdown_from_database(session, CYCLE_ID)

        layer_rows = session.execute(
            text("SELECT layer_id FROM map.tile_layer ORDER BY layer_id")
        ).scalars().all()

    # Two distinct layers, one per network segment, no collision/overwrite.
    layer_ids = {layer["layer_id"] for layer in result.layers}
    assert layer_ids == {_layer_id("run-a", "rivnet-1"), _layer_id("run-a", "rivnet-2")}
    assert len(result.layers) == 2
    assert set(layer_rows) == layer_ids

    # Two separate per-network manifests written to distinct keys.
    assert publisher.object_store.exists(
        f"tiles/hydro/{CYCLE_ID}/q-down/run-a/rivnet-1/manifest.json"
    )
    assert publisher.object_store.exists(
        f"tiles/hydro/{CYCLE_ID}/q-down/run-a/rivnet-2/manifest.json"
    )

    # cycle manifest: distinct run -> published_basins=1; run x network -> products=2.
    cycle_manifest = json.loads(
        publisher.object_store.read_bytes(f"tiles/hydro/{CYCLE_ID}/q-down/manifest.json")
    )
    assert cycle_manifest["published_basins"] == 1
    assert cycle_manifest["published_products"] == 2
    assert cycle_manifest["source_run_ids"] == ["run-a"]


# --------------------------------------------------------------------------- #
# F5: per-run identity skip (one incomplete run must not sink its peers)
# --------------------------------------------------------------------------- #
def test_publish_qdown_skips_only_identity_incomplete_run(tmp_path: Any) -> None:
    publisher = _publisher(tmp_path)
    with _store(create_flood=False, create_tile_layer=True) as session:
        _insert_run(session, run_id="run-a", segments=3)  # complete
        _insert_run(session, run_id="run-b", segments=2, forcing_version_id=None)  # incomplete

        result = publisher._publish_qdown_from_database(session, CYCLE_ID)

        layer_rows = session.execute(
            text("SELECT layer_id FROM map.tile_layer ORDER BY layer_id")
        ).scalars().all()

    # Only the complete run publishes; the incomplete one is skipped, not fatal.
    assert result.status == "published"
    layer_ids = {layer["layer_id"] for layer in result.layers}
    assert layer_ids == {_layer_id("run-a")}
    assert set(layer_rows) == {_layer_id("run-a")}

    # Degraded cycle with the skipped run recorded as a residual blocker.
    assert result.lineage["quality_state"] == "degraded"
    incomplete = [
        blocker
        for blocker in result.lineage["residual_blockers"]
        if blocker["code"] == "PUBLISH_IDENTITY_INCOMPLETE"
    ]
    assert incomplete, "skipped run must surface a PUBLISH_IDENTITY_INCOMPLETE blocker"
    assert any(blocker.get("run_id") == "run-b" for blocker in incomplete)


def test_publish_qdown_all_runs_incomplete_raises(tmp_path: Any) -> None:
    publisher = _publisher(tmp_path)
    with _store(create_flood=False, create_tile_layer=True) as session:
        _insert_run(session, run_id="run-a", segments=2, forcing_version_id=None)
        _insert_run(session, run_id="run-b", segments=2, model_id=None)

        with pytest.raises(PublishError) as excinfo:
            publisher._publish_qdown_from_database(session, CYCLE_ID)

    # Only when *every* candidate run is incomplete does the cycle fail.
    assert excinfo.value.error_code == "PUBLISH_IDENTITY_INCOMPLETE"


# --------------------------------------------------------------------------- #
# F6: public entry (publish_qdown_cycle) happy path through a real engine.
#
# publish_qdown_cycle builds its own engine via create_engine(self.database_url),
# so to make schema.table resolve there we register a class-level "connect"
# listener that ATTACHes each schema as a *file* database (in-memory ATTACH is
# per-connection and would hide the seeded data from the publisher's connection).
# Class-level listeners fire for every Engine, so we always remove ours in a
# finally block to avoid polluting other fixtures/tests.
# --------------------------------------------------------------------------- #
def test_publish_qdown_cycle_public_entry_happy_path(tmp_path: Any) -> None:
    schemas = ("hydro", "flood", "ops", "map")
    schema_files = {name: tmp_path / f"{name}.db" for name in schemas}
    db_url = f"sqlite:///{tmp_path / 'main.db'}"

    def _attach_all(dbapi_conn: Any, _rec: Any) -> None:  # pragma: no cover - sqlite hook
        for name in schemas:
            dbapi_conn.execute(f"ATTACH DATABASE '{schema_files[name]}' AS {name}")

    event.listen(Engine, "connect", _attach_all)
    try:
        # Seed through a dedicated engine that shares the same ATTACHed files.
        seed_engine = create_engine(db_url, future=True)
        with Session(seed_engine) as session:
            _create_hydro_tables(session)
            _create_tile_layer_table(session)
            _insert_run(session, run_id="run-a", segments=3)
            session.commit()
        seed_engine.dispose()

        publisher = _publisher(tmp_path, database_url=db_url)
        result = publisher.publish_qdown_cycle(CYCLE_ID)
    finally:
        event.remove(Engine, "connect", _attach_all)

    # The public entry constructs the engine, translates errors, manages the
    # session lifecycle, and runs the real publish path end to end.
    assert result.status == "published"
    assert len(result.layers) == 1
    assert {layer["layer_id"] for layer in result.layers} == {_layer_id("run-a")}
    assert result.lineage["db_registered"] is True
    assert publisher.object_store.exists(
        f"tiles/hydro/{CYCLE_ID}/q-down/run-a/rivnet-1/manifest.json"
    )


def test_publish_qdown_cycle_public_entry_translates_engine_failure(tmp_path: Any) -> None:
    # A malformed/unopenable database_url must be translated into QDOWN_PUBLISH_FAILED
    # by the public entry's exception boundary (real postgres happy paths are
    # additionally covered on node-22).
    bad_dir = tmp_path / "missing-dir"
    publisher = _publisher(tmp_path, database_url=f"sqlite:///{bad_dir / 'no.db'}")

    with pytest.raises(PublishError) as excinfo:
        publisher.publish_qdown_cycle(CYCLE_ID)

    assert excinfo.value.error_code == "QDOWN_PUBLISH_FAILED"


# --------------------------------------------------------------------------- #
# F7/G: private-display-path unit assertions (positive + boundary cases)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "private_path",
    [
        "/home/frd_muziyao/run-a/output",
        "/root/run-a/output",
        "file:///home/user/output",
        "file:///root/secret",
        "/scratch/run/out",
        "/tmp/run-a",
        ".nhms-runs/run-a/output",
        # percent-encoded escapes are decoded before the absolute-path check.
        "file:///home/%2e%2e/run-a",
        "file:///%2froot/run-a",
    ],
)
def test_is_private_display_path_rejects_private(private_path: str) -> None:
    assert _is_private_display_path(private_path) is True


@pytest.mark.parametrize(
    "public_path",
    [
        "published://tiles/hydro/run-a/manifest.json",
        "tiles/hydro/run-a/manifest.json",  # relative object-store key
        "s3://bucket/tiles/hydro/manifest.json",
        "",
    ],
)
def test_is_private_display_path_allows_public(public_path: str) -> None:
    assert _is_private_display_path(public_path) is False


# --------------------------------------------------------------------------- #
# Degrade-to-display contract (#290): the flood publish entrypoint
# (_publish_from_database) degrades to the q_down display product when no flood
# return-period tiles are publishable, and only hard-fails when neither flood
# nor q_down is publishable. Product-approved behavior change: the empty-flood
# scenario used to raise NO_PUBLISHABLE_PRODUCTS; it now publishes q_down.
# --------------------------------------------------------------------------- #
def _insert_publishable_flood_run(session: Session, *, run_id: str, segments: int = 3) -> None:
    """Seed a flood run that _discover_publishable_runs treats as ready.

    Status must be frequency_done/published, and every result row needs a
    non-null return_period + warning_level with max_over_window true.
    """
    _insert_run(session, run_id=run_id, status="frequency_done", segments=segments)
    for index in range(segments):
        session.execute(
            text(
                """
                INSERT INTO flood.return_period_result (
                    run_id, river_segment_id, river_network_version_id,
                    return_period, warning_level, max_over_window
                ) VALUES (:run_id, :segment, 'rivnet-1', 100.0, 'major', 1)
                """
            ),
            {"run_id": run_id, "segment": f"seg-{index}"},
        )
    session.commit()


def test_publish_from_database_degrades_to_qdown_when_no_flood_runs(tmp_path: Any) -> None:
    publisher = _publisher(tmp_path)
    # q_down river_timeseries present (parsed), but no publishable flood rows.
    with _store(create_flood=True) as session:
        _insert_run(session, run_id="run-a", status="parsed", segments=3)

        result = publisher._publish_from_database(session, CYCLE_ID)

    assert isinstance(result, PublishResult)
    assert result.status == "published"
    # Layers are the q_down display layers, not flood return-period layers.
    assert {layer["layer_type"] for layer in result.layers} == {"q_down_timeseries"}
    assert result.lineage["degraded_to_display"] is True
    # Missing flood return-period is recorded honestly, not silently dropped.
    assert "return_period_result" in result.lineage["unavailable_products"]
    assert result.lineage["quality_state"] == "degraded"


def test_publish_from_database_happy_path_publishes_flood_without_degrade(tmp_path: Any) -> None:
    publisher = _publisher(tmp_path)
    with _store(create_flood=True) as session:
        _insert_publishable_flood_run(session, run_id="run-a", segments=3)

        result = publisher._publish_from_database(session, CYCLE_ID)

    assert result.status == "published"
    # Flood layers published exactly as before; NO degrade.
    assert {layer["layer_type"] for layer in result.layers} == {"flood_return_period"}
    assert result.lineage.get("degraded_to_display") in (None, False)


def test_publish_from_database_raises_when_neither_flood_nor_qdown(tmp_path: Any) -> None:
    publisher = _publisher(tmp_path)
    # No flood rows AND no q_down river_timeseries → genuinely nothing publishable.
    with _store(create_flood=True) as session:
        session.execute(
            text(
                """
                INSERT INTO hydro.hydro_run (
                    run_id, run_type, status, source_id, cycle_time, run_manifest_uri
                ) VALUES (
                    'run-a', 'forecast', 'parsed', :source_id, :cycle_time,
                    'published://tiles/hydro/manifest.json'
                )
                """
            ),
            {"source_id": SOURCE_ID, "cycle_time": CYCLE_TIME},
        )
        session.commit()

        with pytest.raises(PublishError) as excinfo:
            publisher._publish_from_database(session, CYCLE_ID)

    assert excinfo.value.error_code == "NO_PUBLISHABLE_PRODUCTS"
    # Chained from the qdown empty-runs error.
    assert isinstance(excinfo.value.__cause__, PublishError)
    assert excinfo.value.__cause__.error_code == "NO_PUBLISHABLE_QDOWN_PRODUCTS"
