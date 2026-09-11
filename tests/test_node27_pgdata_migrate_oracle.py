"""Isolated node-27 Docker oracle for offline physical PGDATA relocation."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import time
import uuid
from pathlib import Path

import pytest

from packages.common.compressed_chunk_cold_residency import PINNED_IMAGE_ID
from packages.common.node27_pgdata_migrate import Migration, MigrationError
from packages.common.node27_timeseries_lifecycle_lock import timeseries_lifecycle_lock
from packages.common.safe_fs import rmtree_no_follow

pytestmark = [pytest.mark.integration, pytest.mark.timescaledb_210, pytest.mark.node27_docker]

DOCKER = "/usr/bin/docker"
PGDATA = "/home/postgres/pgdata/data"
IMAGE = PINNED_IMAGE_ID


def _port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = int(sock.getsockname()[1])
    if port == 55432:
        raise RuntimeError("ephemeral port collided with production")
    return port


def _run(argv: list[str], *, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(argv, check=False, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(result.stderr[-400:] or argv[1])
    return result


def _wait_sql(name: str, role: str, database: str) -> None:
    deadline = time.monotonic() + 90
    last = ""
    while time.monotonic() < deadline:
        result = subprocess.run(
            [
                DOCKER,
                "exec",
                "-u",
                "1000:1000",
                name,
                "psql",
                "-X",
                "-qAt",
                "-U",
                role,
                "-d",
                database,
                "-c",
                "SELECT 1",
            ],
            capture_output=True,
            text=True,
            timeout=20,
        )
        if result.returncode == 0 and result.stdout.strip() == "1":
            return
        last = result.stderr
        time.sleep(0.5)
    raise RuntimeError(last[-400:] or "sql not ready")


def _private(path: Path, text: str) -> Path:
    path.write_text(text)
    path.chmod(0o600)
    os.chmod(path, 0o600)
    return path


def _bootstrap(name: str, password: str) -> None:
    sql = f"""
CREATE ROLE nhms SUPERUSER LOGIN PASSWORD '{password}';
CREATE DATABASE nhms OWNER nhms;
"""
    subprocess.run(
        [DOCKER, "exec", "-u", "1000:1000", "-i", name, "psql", "-X", "-q", "-U", "postgres", "-d", "postgres"],
        input=sql,
        text=True,
        check=True,
        timeout=30,
    )
    body = """
CREATE EXTENSION IF NOT EXISTS timescaledb;
CREATE SCHEMA hydro;
CREATE SCHEMA ops;
CREATE TABLE hydro.obs(ts timestamptz NOT NULL, value double precision NOT NULL);
SELECT create_hypertable('hydro.obs','ts');
INSERT INTO hydro.obs SELECT '2020-01-01'::timestamptz + g * interval '1 hour', g FROM generate_series(0, 47) g;
ALTER TABLE hydro.obs SET (timescaledb.compress);
SELECT compress_chunk(c) FROM show_chunks('hydro.obs') c;
INSERT INTO hydro.obs SELECT '2024-01-01'::timestamptz + g * interval '1 hour', 1000+g FROM generate_series(0, 11) g;
CREATE TABLE ops.migration_meta(k text PRIMARY KEY, v text);
INSERT INTO ops.migration_meta VALUES ('relocated','pending');
CREATE ROLE nhms_display_ro LOGIN PASSWORD 'reader-secret';
CREATE ROLE nhms_ingest_rw LOGIN PASSWORD 'writer-secret';
GRANT CONNECT ON DATABASE nhms TO nhms_display_ro, nhms_ingest_rw;
GRANT USAGE ON SCHEMA hydro, ops TO nhms_display_ro, nhms_ingest_rw;
GRANT SELECT ON ALL TABLES IN SCHEMA hydro, ops TO nhms_display_ro;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA hydro, ops TO nhms_ingest_rw;
"""
    subprocess.run(
        [DOCKER, "exec", "-u", "1000:1000", "-i", name, "psql", "-X", "-q", "-U", "nhms", "-d", "nhms"],
        input=body,
        text=True,
        check=True,
        timeout=60,
    )


def _counts(host: str, port: int, user: str, password: str) -> tuple[int, int, str]:
    import psycopg2

    connection = psycopg2.connect(host=host, port=port, user=user, password=password, dbname="nhms", connect_timeout=5)
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM hydro.obs")
            rows = cursor.fetchone()[0]
            cursor.execute(
                "SELECT coalesce(sum(number_compressed_chunks),0) FROM timescaledb_information.compressed_hypertable_stats"
            )
            compressed = cursor.fetchone()[0]
            cursor.execute("SELECT v FROM ops.migration_meta WHERE k='relocated'")
            meta = cursor.fetchone()[0]
        return rows, compressed, meta
    finally:
        connection.close()


@pytest.fixture
def oracle(tmp_path: Path):
    token = uuid.uuid4().hex
    root = tmp_path / f"nhms-pgdata-oracle-{token}"
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    source = root / "source"
    target_parent = root / "target-parent"
    workspace = root / "workspace"
    for path in (source, target_parent, workspace):
        path.mkdir(mode=0o700)
        path.chmod(0o700)
    env = _private(
        root / "postgres.env",
        "POSTGRES_USER=postgres\nPOSTGRES_PASSWORD=admin-secret\nPOSTGRES_DB=postgres\n"
        f"PGDATA={PGDATA}\nTIMESCALEDB_TELEMETRY=off\n",
    )
    port = _port()
    name = root.name + "-db"
    argv = [
        DOCKER,
        "run",
        "-d",
        "--name",
        name,
        "--user",
        "1000:1000",
        "--env-file",
        str(env),
        "--restart",
        "unless-stopped",
        "-p",
        f"127.0.0.1:{port}:5432",
        "-v",
        f"{source}:{PGDATA}:rw",
        IMAGE,
        "postgres",
        "-c",
        "shared_preload_libraries=timescaledb",
    ]
    _run(argv)
    try:
        _wait_sql(name, "postgres", "postgres")
        _bootstrap(name, "admin-secret")
        _wait_sql(name, "nhms", "nhms")
        reader = _private(
            root / "reader.dsn", f"host=127.0.0.1 port={port} dbname=nhms user=nhms_display_ro password=reader-secret"
        )
        writer = _private(
            root / "writer.dsn", f"host=127.0.0.1 port={port} dbname=nhms user=nhms_ingest_rw password=writer-secret"
        )
        lock = root / "lifecycle.lock"
        yield {
            "root": root,
            "source": source,
            "target": target_parent / "pgdata",
            "workspace": workspace,
            "name": name,
            "port": port,
            "reader": reader,
            "writer": writer,
            "lock": lock,
            "password": "admin-secret",
        }
    finally:
        subprocess.run([DOCKER, "rm", "-f", name], capture_output=True, text=True, timeout=60)
        listed = subprocess.run(
            [DOCKER, "ps", "-aq", "--filter", f"name={root.name}"], capture_output=True, text=True, timeout=20
        )
        for identity in listed.stdout.split():
            subprocess.run([DOCKER, "rm", "-f", identity], capture_output=True, text=True, timeout=60)
        if root.exists():
            rmtree_no_follow(root, missing_ok=True)


def _overrides(fixture: dict) -> dict:
    return {
        "source_container": fixture["name"],
        "source_pgdata": str(fixture["source"]),
        "target_pgdata": str(fixture["target"]),
        "reserve_bytes": 1024 * 1024,
        "disposable_root": str(fixture["root"]),
        "database": "nhms",
        "admin_role": "nhms",
        "reader_dsn_file": str(fixture["reader"]),
        "writer_dsn_file": str(fixture["writer"]),
        "drain_timeout": 30,
    }


def _engine(fixture: dict) -> Migration:
    return Migration(fixture["workspace"])


def test_plan_does_not_stop_the_disposable_cluster(oracle: dict) -> None:
    result = _engine(oracle).run("plan", overrides=_overrides(oracle))
    inspect = json.loads(_run([DOCKER, "inspect", oracle["name"]]).stdout)[0]
    assert inspect["State"]["Running"] is True
    assert result["mutation"] is False
    assert not (oracle["workspace"] / "state.json").exists()


def test_copy_activate_preserves_rows_and_rejects_writer_then_rollback(
    oracle: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "packages.common.node27_pgdata_migrate.timeseries_lifecycle_lock",
        lambda: timeseries_lifecycle_lock(oracle["lock"]),
    )
    before = _counts("127.0.0.1", oracle["port"], "nhms_display_ro", "reader-secret")
    engine = _engine(oracle)
    engine.run("prepare", enforce=True, overrides=_overrides(oracle))
    inspect = json.loads(_run([DOCKER, "inspect", oracle["name"]]).stdout)[0]
    assert inspect["State"]["Running"] is False
    engine.run("copy", enforce=True)
    engine.run("activate", enforce=True)
    after = _counts("127.0.0.1", oracle["port"], "nhms_display_ro", "reader-secret")
    assert after == before
    assert after[0] == 60
    with pytest.raises(Exception):
        _counts("127.0.0.1", oracle["port"], "nhms_ingest_rw", "writer-secret")
    binds = json.loads(_run([DOCKER, "inspect", "--format", "{{json .HostConfig.Binds}}", oracle["name"]]).stdout)
    assert str(oracle["target"]) + f":{PGDATA}:rw" in binds
    assert str(oracle["source"]) + f":{PGDATA}:rw" not in binds
    assert not any("nhms_cold" in item for item in binds)
    engine.run("rollback", enforce=True)
    restored = json.loads(_run([DOCKER, "inspect", oracle["name"]]).stdout)[0]
    assert restored["State"]["Running"] is True
    restored_binds = json.loads(
        _run([DOCKER, "inspect", "--format", "{{json .HostConfig.Binds}}", oracle["name"]]).stdout
    )
    assert str(oracle["source"]) + f":{PGDATA}:rw" in restored_binds
    assert oracle["source"].exists() and oracle["target"].exists()
    assert json.loads((oracle["workspace"] / "state.json").read_text())["stage"] == "rolled_back"


def test_release_marks_writes_and_refuses_stale_rollback(oracle: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "packages.common.node27_pgdata_migrate.timeseries_lifecycle_lock",
        lambda: timeseries_lifecycle_lock(oracle["lock"]),
    )
    engine = _engine(oracle)
    for action in ("prepare", "copy", "activate", "release"):
        engine.run(action, enforce=True, overrides=_overrides(oracle) if action == "prepare" else None)
    state = json.loads((oracle["workspace"] / "state.json").read_text())
    assert state["writes_released"] is True
    assert state["stage"] == "released"
    _counts("127.0.0.1", oracle["port"], "nhms_ingest_rw", "writer-secret")
    with pytest.raises(MigrationError, match="forward-only|stale"):
        engine.run("rollback", enforce=True)
    assert json.loads((oracle["workspace"] / "state.json").read_text())["writes_released"] is True


def test_interrupted_copy_cannot_activate(oracle: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "packages.common.node27_pgdata_migrate.timeseries_lifecycle_lock",
        lambda: timeseries_lifecycle_lock(oracle["lock"]),
    )
    engine = _engine(oracle)
    engine.run("prepare", enforce=True, overrides=_overrides(oracle))
    original_helper = engine.host.helper

    def fail_copy(state, script, **kwargs):
        if script.startswith("cp -a"):
            original_helper(
                state,
                "mkdir -p /destination/partial; echo interrupted > /destination/partial/note; sync -f /destination",
                **{key: value for key, value in kwargs.items() if key != "timeout"},
            )
            raise MigrationError("copy interrupted")
        return original_helper(state, script, **kwargs)

    engine.host.helper = fail_copy  # type: ignore[method-assign]
    with pytest.raises(MigrationError, match="interrupted"):
        engine.run("copy", enforce=True)
    assert oracle["source"].exists()
    with pytest.raises(MigrationError, match="state/action pair refused"):
        Migration(oracle["workspace"]).run("activate", enforce=True)
    assert oracle["target"].exists()


def test_live_identity_is_refused_by_disposable_mode(oracle: dict) -> None:
    overrides = _overrides(oracle)
    overrides["source_container"] = "nhms-db"
    with pytest.raises(MigrationError):
        _engine(oracle).run("prepare", enforce=True, overrides=overrides)
