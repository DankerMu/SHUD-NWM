"""Owned disposable exact-image oracle, exercising the CLI in fresh processes."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import uuid
from contextlib import closing
from pathlib import Path

import psycopg2
import pytest

from packages.common.compressed_chunk_cold_residency import PINNED_IMAGE_ID
from packages.common.safe_fs import rmtree_no_follow

pytestmark = [pytest.mark.integration, pytest.mark.timescaledb_210, pytest.mark.node27_docker]
DOCKER = "/usr/bin/docker"
PGDATA = "/home/postgres/pgdata/data"
IMAGE = PINNED_IMAGE_ID
REPO = Path(__file__).resolve().parents[1]


def _run(argv: list[str], *, timeout: int = 120, input: str | None = None):
    result = subprocess.run(argv, check=False, capture_output=True, text=True, timeout=timeout, input=input)
    if result.returncode:
        raise RuntimeError(result.stderr[-2000:] or result.stdout[-2000:] or argv[1])
    return result


def _private(path: Path, text: str) -> Path:
    path.write_text(text)
    path.chmod(0o600)
    return path


def _inspect(identity: str) -> dict:
    return json.loads(_run([DOCKER, "inspect", "--type", "container", identity]).stdout)[0]


def _owned(raw: dict, root: Path, token: str, operation: str | None) -> bool:
    labels = raw["Config"].get("Labels") or {}
    mounts = raw.get("Mounts", [])
    return (
        raw["Image"] == IMAGE
        and (
            labels.get("nhms.pgdata.oracle") == token
            or (operation is not None and labels.get("nhms.pgdata.operation") == operation)
        )
        and bool(mounts)
        and all(mount.get("Type") == "bind" and Path(mount["Source"]).is_relative_to(root) for mount in mounts)
    )


def _cleanup(root: Path, token: str, workspace: Path) -> None:
    state_path = workspace / "state.json"
    operation = json.loads(state_path.read_text())["operation"] if state_path.exists() else None
    selectors = ["label=nhms.pgdata.oracle=" + token]
    if operation:
        selectors.append("label=nhms.pgdata.operation=" + operation)
    identities = set()
    for selector in selectors:
        identities.update(_run([DOCKER, "ps", "-aq", "--filter", selector]).stdout.split())
    # Inspect every discovered ID before removing anything, including stopped helpers.
    inspected = [_inspect(identity) for identity in identities]
    if not all(_owned(raw, root, token, operation) for raw in inspected):
        raise RuntimeError("oracle cleanup ownership differs; all evidence retained")
    for raw in inspected:
        _run([DOCKER, "rm", "-f", raw["Id"]])
    rmtree_no_follow(root, missing_ok=True)


def _sql(fixture: dict, sql: str, *, database: str = "nhms", role: str = "nhms") -> str:
    raw = _inspect(fixture["name"])
    assert _owned(raw, fixture["root"], fixture["token"], None)
    return _run(
        [
            DOCKER,
            "exec",
            "-i",
            raw["Id"],
            "psql",
            "-X",
            "-qAt",
            "-v",
            "ON_ERROR_STOP=1",
            "-U",
            role,
            "-d",
            database,
        ],
        input=sql,
    ).stdout


def _wait_sql(fixture: dict) -> None:
    # The entrypoint's temporary init server accepts Unix sockets but not TCP.
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        try:
            with (
                closing(
                    psycopg2.connect(
                        host="127.0.0.1",
                        port=fixture["port"],
                        dbname="postgres",
                        user="postgres",
                        password="admin-secret",
                        connect_timeout=2,
                    )
                ) as connection,
                connection.cursor() as cursor,
            ):
                cursor.execute("SELECT 1")
                if cursor.fetchone() == (1,):
                    return
        except psycopg2.OperationalError:
            pass
        time.sleep(0.5)
    raise RuntimeError("disposable TCP SQL startup failed")


def _bootstrap(fixture: dict) -> None:
    _sql(
        fixture,
        "CREATE ROLE nhms SUPERUSER LOGIN; CREATE DATABASE nhms OWNER nhms;",
        database="postgres",
        role="postgres",
    )
    _sql(
        fixture,
        """
CREATE EXTENSION IF NOT EXISTS timescaledb;
CREATE EXTENSION postgis VERSION '3.3.2';
CREATE SCHEMA hydro;
CREATE SCHEMA ops;
CREATE TABLE hydro.spatial(geom geometry(Point,4326));
INSERT INTO hydro.spatial VALUES (ST_SetSRID(ST_MakePoint(0,0),4326)), (ST_SetSRID(ST_MakePoint(2,2),4326));
ANALYZE hydro.spatial;
CREATE TABLE hydro.obs(ts timestamptz NOT NULL, value double precision NOT NULL);
SELECT create_hypertable('hydro.obs','ts');
INSERT INTO hydro.obs SELECT '2020-01-01'::timestamptz + g * interval '1 hour', g
FROM generate_series(0,47) g;
ALTER TABLE hydro.obs SET (timescaledb.compress);
SELECT compress_chunk(c) FROM show_chunks('hydro.obs') c;
INSERT INTO hydro.obs SELECT '2024-01-01'::timestamptz + g * interval '1 hour', 1000+g
FROM generate_series(0,11) g;
CREATE TABLE ops.migration_meta(k text PRIMARY KEY, v text);
INSERT INTO ops.migration_meta VALUES ('relocated','pending');
CREATE ROLE nhms_display_ro LOGIN PASSWORD 'reader-secret';
CREATE ROLE nhms_ingest_rw LOGIN PASSWORD 'writer-secret';
GRANT CONNECT ON DATABASE nhms TO nhms_display_ro, nhms_ingest_rw;
GRANT USAGE ON SCHEMA hydro, ops TO nhms_display_ro, nhms_ingest_rw;
GRANT SELECT ON ALL TABLES IN SCHEMA hydro, ops TO nhms_display_ro;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA hydro, ops TO nhms_ingest_rw;
""",
    )
    assert _sql(fixture, "SELECT extversion FROM pg_extension WHERE extname='postgis'").strip() == "3.3.2"
    assert (
        _sql(
            fixture,
            """
SELECT bool_or(is_compressed), bool_or(NOT is_compressed)
FROM timescaledb_information.chunks WHERE hypertable_schema='hydro' AND hypertable_name='obs';
""",
        ).strip()
        == "t|t"
    )
    assert _counts(fixture) == (60, 48, 12, "pending")
    _postgis_read(fixture)


def _connection(fixture: dict, *, writer: bool = False):
    return psycopg2.connect(
        host="127.0.0.1",
        port=fixture["port"],
        dbname="nhms",
        connect_timeout=5,
        user="nhms_ingest_rw" if writer else "nhms_display_ro",
        password="writer-secret" if writer else "reader-secret",
    )


def _counts(fixture: dict) -> tuple:
    with closing(_connection(fixture)) as connection, connection.cursor() as cursor:
        cursor.execute("""SELECT count(*), count(*) FILTER (WHERE ts < '2021-01-01'),
        count(*) FILTER (WHERE ts >= '2024-01-01') FROM hydro.obs""")
        counts = cursor.fetchone()
        cursor.execute("SELECT v FROM ops.migration_meta WHERE k='relocated'")
        return (*counts, cursor.fetchone()[0])


def _postgis_read(fixture: dict) -> None:
    with closing(_connection(fixture)) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT ST_EstimatedExtent('hydro','spatial','geom') IS NOT NULL")
        assert cursor.fetchone() == (True,)


def _write(fixture: dict) -> None:
    with closing(_connection(fixture, writer=True)) as connection:
        with connection.cursor() as cursor:
            cursor.execute("UPDATE ops.migration_meta SET v='written-after-release' WHERE k='relocated'")
            assert cursor.rowcount == 1
        connection.commit()
    assert _counts(fixture) == (60, 48, 12, "written-after-release")


@pytest.fixture
def oracle(tmp_path: Path):
    token = uuid.uuid4().hex
    root = tmp_path / ("nhms-pgdata-oracle-" + token)
    root.mkdir(mode=0o700)
    source, parent, workspace = (root / name for name in ("source", "target-parent", "workspace"))
    for path in (source, parent, workspace):
        path.mkdir(mode=0o700)
    uid, gid = os.geteuid(), os.getegid()
    assert uid > 0 and gid > 0
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    assert port != 55432
    fixture = {
        "token": token,
        "root": root,
        "source": source,
        "target": parent / "pgdata",
        "workspace": workspace,
        "name": root.name + "-db",
        "port": port,
    }
    try:
        # Disposable capability helper: measured runtime plus image account database.
        helper = _run(
            [
                DOCKER,
                "create",
                "--label",
                "nhms.pgdata.oracle=" + token,
                "--network",
                "none",
                "--user",
                f"{uid}:{gid}",
                "--mount",
                f"type=bind,src={root},dst=/fixture,readonly",
                "--entrypoint",
                "/bin/sh",
                IMAGE,
                "-ceu",
                "id -u; id -g; cat /etc/passwd; printf '\\n--GROUP--\\n'; cat /etc/group",
            ]
        ).stdout.strip()
        assert _owned(_inspect(helper), root, token, None)
        accounts = _run([DOCKER, "start", "-a", helper]).stdout
        measured_uid, measured_gid, accounts = accounts.split("\n", 2)
        assert (int(measured_uid), int(measured_gid)) == (uid, gid)
        passwd, group = accounts.split("\n--GROUP--\n")
        # Supply a passwd entry for initdb without changing the image or host accounts.
        passwd_rows = [line.split(":") for line in passwd.splitlines()]
        group_rows = [line.split(":") for line in group.splitlines()]
        for row in passwd_rows:
            if row[0] == "postgres":
                row[2:4] = [str(uid), str(gid)]
        for row in group_rows:
            if row[0] == "postgres":
                row[2] = str(gid)
        passwd_path = _private(root / "passwd", "\n".join(":".join(row) for row in passwd_rows) + "\n")
        group_path = _private(root / "group", "\n".join(":".join(row) for row in group_rows) + "\n")
        env = _private(
            root / "postgres.env",
            "POSTGRES_USER=postgres\nPOSTGRES_PASSWORD=admin-secret\n"
            f"POSTGRES_DB=postgres\nPGDATA={PGDATA}\nTIMESCALEDB_TELEMETRY=off\n",
        )
        _run(
            [
                DOCKER,
                "run",
                "-d",
                "--name",
                fixture["name"],
                "--label",
                "nhms.pgdata.oracle=" + token,
                "--user",
                f"{uid}:{gid}",
                "--env-file",
                str(env),
                "--restart",
                "unless-stopped",
                "-p",
                f"127.0.0.1:{port}:5432",
                "-v",
                f"{source}:{PGDATA}:rw",
                "-v",
                f"{passwd_path}:/etc/passwd:ro",
                "-v",
                f"{group_path}:/etc/group:ro",
                IMAGE,
                "postgres",
                "-c",
                "shared_preload_libraries=timescaledb",
                "-c",
                "shared_buffers=32MB",
                "-c",
                "work_mem=4MB",
                "-c",
                "maintenance_work_mem=32MB",
                "-c",
                "max_connections=20",
            ]
        )
        _wait_sql(fixture)
        _bootstrap(fixture)
        for kind, role, password in (
            ("reader", "nhms_display_ro", "reader-secret"),
            ("writer", "nhms_ingest_rw", "writer-secret"),
        ):
            fixture[kind] = _private(
                root / (kind + ".dsn"), f"host=127.0.0.1 port={port} dbname=nhms user={role} password={password}"
            )
        yield fixture
    except BaseException:
        # Capture startup/test failure logs while the owned containers still exist.
        listed = _run([DOCKER, "ps", "-aq", "--filter", "label=nhms.pgdata.oracle=" + token])
        logs = []
        for identity in listed.stdout.split():
            raw = _inspect(identity)
            if _owned(raw, root, token, None):
                result = subprocess.run(
                    [DOCKER, "logs", "--tail", "200", raw["Id"]], capture_output=True, text=True, timeout=30
                )
                logs.append(result.stdout + result.stderr)
        _private(tmp_path / (root.name + "-failure.log"), "\n".join(logs))
        # Pytest's failed-only tmp retention can remove setup-error directories.
        print("\n".join(logs))
        raise
    finally:
        _cleanup(root, token, workspace)


FAULT_DRIVER = """import json, os, sys
from pathlib import Path
from packages.common.node27_pgdata_host import Host, MigrationError
from scripts.node27_pgdata_migrate import main
original = Host.command
fault = os.environ["ORACLE_FAULT"]
workspace = Path(sys.argv[sys.argv.index("--workspace") + 1])
root = workspace.parent
assert root.name.startswith("nhms-pgdata-oracle-") and workspace.name == "workspace"
assert "--disposable-root" not in sys.argv or Path(sys.argv[sys.argv.index("--disposable-root") + 1]) == root
original_health = Host.health
def health(self, config):
    assert Path(config["disposable_root"]) == root
    if fault == "copy-health-unavailable":
        raise MigrationError("owned fixture health observation unavailable")
    return original_health(self, config)
Host.health = health
original_display_ready = Host.display_ready
def display_ready(self, state):
    assert Path(state["config"]["disposable_root"]) == root
    if fault == "display-unavailable":
        raise MigrationError("owned fixture display observation unavailable")
    return original_display_ready(self, state)
Host.display_ready = display_ready
def command(self, argv, **kwargs):
    argv = list(argv)
    partial = fault == "partial-copy" and "cp -a /source/. /destination/;" in argv[-1]
    if partial:
        argv[-1] = argv[-1].replace("cp -a /source/. /destination/;",
                                   "cp -a /source/PG_VERSION /destination/;")
    if fault == "completed-copy-mismatch" and "cp -a /source/. /destination/;" in argv[-1]:
        argv[-1] += "; printf corrupted > /destination/oracle-copy-sentinel; sync -f /destination"
    result = original(self, argv, **kwargs)
    path = workspace / "state.json"
    state = json.loads(path.read_text()) if path.exists() else {}
    args = list(argv)
    candidate = state.get("candidate_id")
    source = state.get("original_id")
    rollback = state.get("stage") == "rollback_intent"
    if state:
        assert Path(state["config"]["disposable_root"]) == root
        if fault == "candidate-id" and args[1:4] == ["inspect", "--type", "container"] and args[-1] == candidate:
            from types import SimpleNamespace
            observed = json.loads(result.stdout)
            observed[0]["Id"] = "b" * 64
            return SimpleNamespace(returncode=0, stdout=json.dumps(observed), stderr="")
    hit = (
        (fault == "candidate-rename" and rollback and args[1:3] == ["rename", candidate])
        or (fault == "original-policy" and rollback and args[1:3] == ["update", "--restart=unless-stopped"]
            and args[-1] == source)
        or (fault == "original-start" and rollback and args[1:3] == ["start", source])
        or (fault == "candidate-policy" and state.get("writes_released")
            and args[1:3] == ["update", "--restart=unless-stopped"] and args[-1] == candidate)
        or (fault == "prepare-stop" and state.get("stage") == "prepare_intent"
            and args[1:3] == ["stop", "--signal"] and args[-1] == source)
        or partial
    )
    if hit:
        os._exit(71)
    return result
Host.command = command
sys.exit(main(sys.argv[1:]))
"""


def _cli(
    fixture: dict, action: str, *, fault: str | None = None, expect: int = 0, extra: tuple[str, ...] = ()
) -> subprocess.CompletedProcess[str]:
    executable = REPO / "scripts/node27_pgdata_migrate.py"
    env = {**os.environ, "PYTHONPATH": str(REPO)}
    if fault:
        executable = _private(fixture["root"] / "fault-driver.py", FAULT_DRIVER)
        env["ORACLE_FAULT"] = fault
    args = [sys.executable, str(executable), "--workspace", str(fixture["workspace"]), "--action", action]
    if action != "plan":
        args.append("--enforce")
    if action in {"prepare", "plan"}:
        for key, value in {
            "source-container": fixture["name"],
            "source-pgdata": fixture["source"],
            "target-pgdata": fixture["target"],
            "reserve-bytes": 1024 * 1024,
            "disposable-root": fixture["root"],
            "reader-dsn-file": fixture["reader"],
            "writer-dsn-file": fixture["writer"],
        }.items():
            args += ["--" + key, str(value)]
    result = subprocess.run([*args, *extra], capture_output=True, text=True, timeout=300, env=env, cwd=REPO)
    assert result.returncode == expect, result.stdout + result.stderr
    return result


def _activate(fixture: dict) -> None:
    for action in ("prepare", "copy", "activate"):
        _cli(fixture, action)
    assert _counts(fixture) == (60, 48, 12, "pending")
    _postgis_read(fixture)
    assert (
        _sql(
            fixture,
            """
SELECT bool_or(is_compressed), bool_or(NOT is_compressed)
FROM timescaledb_information.chunks WHERE hypertable_schema='hydro' AND hypertable_name='obs';
""",
        ).strip()
        == "t|t"
    )
    with pytest.raises(psycopg2.OperationalError, match="pg_hba.conf rejects connection"):
        with closing(_connection(fixture, writer=True)):
            pytest.fail("writer admitted before release")


def test_plan_preserves_running_cluster_without_journal(oracle: dict) -> None:
    result = _cli(oracle, "plan")
    assert json.loads(result.stdout)["blockers"] == []
    assert _inspect(oracle["name"])["State"]["Running"]
    assert not (oracle["workspace"] / "state.json").exists()


@pytest.mark.parametrize("fault", ["candidate-rename", "original-policy", "original-start"])
def test_fresh_process_rollback_recovers_journaled_side_effect(oracle: dict, fault: str) -> None:
    _activate(oracle)
    _cli(oracle, "rollback", fault=fault, expect=71)
    if fault == "original-policy":
        interrupted = json.loads((oracle["workspace"] / "state.json").read_text())
        original = _inspect(interrupted["original_id"])
        assert _owned(original, oracle["root"], oracle["token"], interrupted["operation"])
        if not original["State"]["Running"]:
            _run([DOCKER, "start", original["Id"]])
    _cli(oracle, "rollback")
    state = json.loads((oracle["workspace"] / "state.json").read_text())
    assert state["stage"] == "rolled_back" and not state["writes_released"]
    assert _inspect(oracle["name"])["Id"] == state["original_id"]
    assert not _inspect(state["candidate_id"])["State"]["Running"]
    assert _counts(oracle) == (60, 48, 12, "pending")
    _write(oracle)


def test_partial_release_recovers_forward_and_allows_durable_business_write(oracle: dict) -> None:
    _activate(oracle)
    _cli(oracle, "release", fault="candidate-policy", expect=71)
    state = json.loads((oracle["workspace"] / "state.json").read_text())
    assert state["stage"] == "release_intent" and state["writes_released"]
    _write(oracle)
    _cli(oracle, "rollback", expect=2)
    _cli(oracle, "release")
    assert _counts(oracle) == (60, 48, 12, "written-after-release")
    assert json.loads((oracle["workspace"] / "state.json").read_text())["stage"] == "released"


@pytest.mark.parametrize(
    "sql",
    [
        "CREATE FUNCTION public.unsafe() RETURNS void LANGUAGE sql SECURITY DEFINER AS "
        "$$ UPDATE ops.migration_meta SET v='unsafe' $$; GRANT EXECUTE ON FUNCTION public.unsafe() TO nhms_display_ro;",
        "ALTER ROLE nhms_display_ro NOINHERIT; GRANT nhms_ingest_rw TO nhms_display_ro;",
    ],
)
def test_application_definer_and_assumable_writer_are_refused_before_fencing(oracle: dict, sql: str) -> None:
    _sql(oracle, sql)
    _cli(oracle, "prepare", expect=2)
    assert not (oracle["workspace"] / "state.json").exists()
    assert _inspect(oracle["name"])["State"]["Running"]
    assert _counts(oracle) == (60, 48, 12, "pending")


def test_disposable_cli_refuses_live_identity_without_lookup(oracle: dict) -> None:
    result = _cli(oracle, "prepare", expect=2, extra=("--source-container", "nhms-db"))
    assert "disposable production identity refused" in result.stderr
    assert not (oracle["root"] / "lifecycle.lock").exists()
    assert not (oracle["workspace"] / "state.json").exists()
    assert _inspect(oracle["name"])["State"]["Running"]


def test_partial_copy_is_retained_and_cannot_activate(oracle: dict) -> None:
    _cli(oracle, "prepare")
    _cli(oracle, "copy", fault="partial-copy", expect=71)
    partial = oracle["target"] / "PG_VERSION"
    expected = (oracle["source"] / "PG_VERSION").read_bytes()
    assert partial.read_bytes() == expected
    _cli(oracle, "activate", expect=2)
    _cli(oracle, "copy", expect=2)
    assert partial.read_bytes() == expected
    _cli(oracle, "rollback")
    assert _counts(oracle) == (60, 48, 12, "pending")


def test_prepare_interruption_rolls_back_in_fresh_process(oracle: dict) -> None:
    _cli(oracle, "prepare", fault="prepare-stop", expect=71)
    state = json.loads((oracle["workspace"] / "state.json").read_text())
    assert state["stage"] == "prepare_intent" and not state["writes_released"]
    assert not _inspect(state["original_id"])["State"]["Running"]
    assert not oracle["target"].exists()
    _cli(oracle, "rollback")
    assert _inspect(oracle["name"])["Id"] == state["original_id"]
    assert _inspect(oracle["name"])["HostConfig"]["RestartPolicy"]["Name"] == "unless-stopped"
    assert _counts(oracle) == (60, 48, 12, "pending")
    _write(oracle)


@pytest.mark.parametrize("refusal", ("prepare-replay", "source-override", "target-override"))
def test_prepared_identity_cannot_be_replayed_or_redirected(oracle: dict, refusal: str) -> None:
    _cli(oracle, "prepare")
    state_bytes = (oracle["workspace"] / "state.json").read_bytes()
    redirected = oracle["root"] / "redirected"
    if refusal == "prepare-replay":
        _cli(oracle, "prepare", expect=2)
    else:
        flag = "--source-pgdata" if refusal == "source-override" else "--target-pgdata"
        _cli(oracle, "copy", expect=2, extra=(flag, str(redirected)))
    assert (oracle["workspace"] / "state.json").read_bytes() == state_bytes
    assert not oracle["target"].exists() and not redirected.exists()
    assert not _inspect(oracle["name"])["State"]["Running"]
    _cli(oracle, "rollback")
    assert _counts(oracle) == (60, 48, 12, "pending")


def test_stopped_source_drift_refuses_before_target_creation(oracle: dict) -> None:
    sentinel = _private(oracle["source"] / "oracle-source-sentinel", "before")
    _cli(oracle, "prepare")
    before = sentinel.stat()
    sentinel.write_text("after")
    _cli(oracle, "copy", expect=2)
    assert not oracle["target"].exists()
    assert json.loads((oracle["workspace"] / "state.json").read_text())["stage"] == "prepared"
    assert sentinel.read_text() == "after"
    sentinel.write_text("before")
    os.utime(sentinel, ns=(before.st_atime_ns, before.st_mtime_ns))
    _cli(oracle, "rollback")
    assert _counts(oracle) == (60, 48, 12, "pending")


def test_completed_copy_hash_mismatch_never_becomes_activatable(oracle: dict) -> None:
    sentinel = _private(oracle["source"] / "oracle-copy-sentinel", "original")
    _cli(oracle, "prepare")
    _cli(oracle, "copy", fault="completed-copy-mismatch", expect=2)
    assert (oracle["target"] / "oracle-copy-sentinel").read_text() == "corrupted"
    assert (oracle["target"] / "PG_VERSION").read_bytes() == (oracle["source"] / "PG_VERSION").read_bytes()
    assert json.loads((oracle["workspace"] / "state.json").read_text())["stage"] == "copy_intent"
    _cli(oracle, "activate", expect=2)
    assert sentinel.read_text() == "original"
    _cli(oracle, "rollback")
    assert _counts(oracle) == (60, 48, 12, "pending")


def test_copy_rechecks_health_after_successful_prepare(oracle: dict) -> None:
    _cli(oracle, "prepare")
    _cli(oracle, "copy", fault="copy-health-unavailable", expect=2)
    assert not oracle["target"].exists()
    assert json.loads((oracle["workspace"] / "state.json").read_text())["stage"] == "prepared"
    _cli(oracle, "activate", expect=2)
    _cli(oracle, "rollback")
    assert _counts(oracle) == (60, 48, 12, "pending")


@pytest.mark.parametrize("unsafe", (None, "live-original", "candidate-id", "candidate-name", "candidate-config"))
def test_marked_stopped_candidate_recovery_is_owned_and_forward_only(oracle: dict, unsafe: str | None) -> None:
    _activate(oracle)
    _cli(oracle, "release", fault="candidate-policy", expect=71)
    state = json.loads((oracle["workspace"] / "state.json").read_text())
    assert state["writes_released"] and state["stage"] == "release_intent"
    candidate = _inspect(state["candidate_id"])
    original = _inspect(state["original_id"])
    assert _owned(candidate, oracle["root"], oracle["token"], state["operation"])
    assert _owned(original, oracle["root"], oracle["token"], state["operation"])
    _run([DOCKER, "stop", "--signal", "SIGINT", "--timeout", "-1", candidate["Id"]])
    _run([DOCKER, "update", "--restart=no", candidate["Id"]])
    if unsafe == "live-original":
        _run([DOCKER, "start", original["Id"]])
    elif unsafe == "candidate-name":
        _run([DOCKER, "rename", candidate["Id"], oracle["name"] + "-unexpected"])
    elif unsafe == "candidate-config":
        _run([DOCKER, "update", "--memory", "256m", "--memory-swap", "512m", candidate["Id"]])
    _cli(oracle, "rollback", expect=2)
    _cli(oracle, "release", fault="candidate-id" if unsafe == "candidate-id" else None, expect=2 if unsafe else 0)
    after = json.loads((oracle["workspace"] / "state.json").read_text())
    assert after["writes_released"]
    if unsafe:
        assert after["stage"] == "release_intent"
        assert not _inspect(candidate["Id"])["State"]["Running"]
    else:
        assert after["stage"] == "released"
        assert _inspect(oracle["name"])["Id"] == candidate["Id"]
        assert not _inspect(original["Id"])["State"]["Running"]
        _write(oracle)


@pytest.mark.parametrize("action", ("activate", "release"))
def test_display_readiness_failure_prevents_terminal_receipt(oracle: dict, action: str) -> None:
    _cli(oracle, "prepare")
    _cli(oracle, "copy")
    if action == "release":
        _cli(oracle, "activate")
    _cli(oracle, action, fault="display-unavailable", expect=2)
    state = json.loads((oracle["workspace"] / "state.json").read_text())
    assert state["stage"] == ("activate_intent" if action == "activate" else "release_intent")
    assert state["writes_released"] is (action == "release")
    assert _counts(oracle) == (60, 48, 12, "pending")
    if action == "release":
        _cli(oracle, "rollback", expect=2)
        _cli(oracle, "release")
    else:
        _cli(oracle, "rollback")
    _write(oracle)
