"""Pipeline dress-rehearsal for the #1069 replay plan-author + capture-producer.

These tests close the two remaining live-measured gaps behind the one-shot
replay:

* the "reviewed run-plan" now has a committed, executable author whose plan the
  *real* supervisor gate accepts; and
* the twelve captures are real invocations of the committed capture-producer
  whose documents the *real* verifier validators accept -- not placeholders that
  shape-pass ``validate_run_plan`` yet burn the window when the verifier rejects
  the terminal after the mutation.

The dress-rehearsal runs the REAL supervisor state machine against binary stubs
re-anchored to tonight's measured node-27 responses, with the REAL
capture-producer running for real against a stubbed DB/probe layer, then feeds
the produced capture documents to the REAL verifier content validators.  A
placeholder capture, a dropped ``PREFLIGHT_KEYS`` field, or a swapped plan token
turns this suite RED -- the exact class caught live five times.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from scripts import node27_timeseries_compression_bundle_author as bundle_author
from scripts import node27_timeseries_compression_live_evidence as evidence
from scripts import node27_timeseries_compression_plan_author as plan_author
from scripts import node27_timeseries_compression_supervisor as supervisor
from tests import test_node27_timeseries_compression_supervisor as sup

ROOT = Path(__file__).resolve().parents[1]
HEAD = "904d488538d3c5c8459525c19daf4dbb769b9df3"
CATALOG_KINDS = ("catalog_before", "catalog_after_first", "catalog_after_second")

_DB_IDENTITY = {
    "dbname": "nhms",
    "instance": "node27-primary-pg15",
    "postgres_version": "15.2",
    "timescaledb_version": "2.10.2",
}
_ROLE = {
    "current_user": "nhms",
    "rolsuper": True,
    "rolcreaterole": True,
    "rolcreatedb": True,
    "owns_hydro_river_timeseries": True,
    "owns_met_forcing_station_timeseries": True,
    "execute_compress_chunk_regclass_boolean": True,
    "role_created": False,
    "grant_executed": False,
    "role_mutated": False,
}
_PROBE_QUERY = (
    "SELECT current_database() AS dbname, "
    "current_setting('server_version') AS postgres_version, "
    "extversion AS timescaledb_version FROM pg_extension "
    "WHERE extname = 'timescaledb'"
)


def _write_stub(bindir: Path, name: str, responses: list[dict[str, Any]]) -> str:
    bindir.mkdir(parents=True, exist_ok=True)
    script = bindir / name
    script.write_text(sup._STUB_TEMPLATE.replace("{python}", sys.executable), encoding="utf-8")
    script.chmod(0o755)
    (bindir / f"{name}.responses.json").write_text(json.dumps(responses), encoding="utf-8")
    return str(script)


def _fixture_repo(root: Path) -> Path:
    """A minimal checkout the preflight/cleanup producers read locally."""

    repo = root / "repo"
    for relative in ("packages/common/timescale_write_guard.py", "workers/forcing_producer/store.py"):
        source = ROOT / relative
        dest = repo / relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(source.read_bytes())
    for relative in (
        "infra/systemd/nhms-node27-timeseries-compression.service",
        "infra/systemd/nhms-node27-timeseries-compression.timer",
    ):
        source = ROOT / relative
        dest = repo / relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(source.read_bytes())
    env = repo / "infra/env/node27-timeseries-compression.env"
    env.parent.mkdir(parents=True, exist_ok=True)
    env.write_text("PGHOST=127.0.0.1\n", encoding="utf-8")
    env.chmod(0o600)
    return repo


def _unit_show(enabled: str, active: str, sub: str, result: str, pid: int) -> str:
    return f"UnitFileState={enabled}\nActiveState={active}\nSubState={sub}\nResult={result}\nMainPID={pid}\n"


def _capture_stub_dir(bindir: Path, *, schema_dump_container: str) -> None:
    """Stub the capture-producer's DB/systemd/docker/git probe layer."""

    d3 = sup._d3_catalog()
    catalog_body = {"captured_at": "2026-07-15T11:25:00.500000Z", "catalog": d3}
    catalog_post_body = {
        "captured_at": "2026-07-15T12:05:03.500000Z",
        "catalog": d3,
        "compressed_chunk_identities": [
            {
                "hypertable_schema": "hydro",
                "hypertable_name": "river_timeseries",
                "chunk_schema": "_timescaledb_internal",
                "chunk_name": "_hyper_3_7_chunk",
                "range_start": "2026-05-28T00:00:00Z",
                "range_end": "2026-06-04T00:00:00Z",
            }
        ],
    }
    candidate = {
        "hypertable_schema": "hydro",
        "hypertable_name": "river_timeseries",
        "chunk_schema": "_timescaledb_internal",
        "chunk_name": "_hyper_3_7_chunk",
        "range_start": "2026-05-28T00:00:00Z",
        "range_end": "2026-06-04T00:00:00Z",
        "is_compressed": False,
        "before_bytes": 4_115_734_528,
    }
    selection_body = {
        "observed_at": "2026-07-15T12:00:10Z",
        "cutoff": "2026-07-08T12:00:10Z",
        "candidates": [candidate],
    }
    sizes_body = {
        "captured_at": "2026-07-15T12:00:24Z",
        "tables": {
            key: {
                "hypertable_size": 1,
                "parent_relation_size": 8192,
                "compressed_chunks": 0,
                "uncompressed_chunks": 1,
                "compressed_relations": [],
            }
            for key in ("hydro.river_timeseries", "met.forcing_station_timeseries")
        },
    }
    # Capture markers are more specific than the checkpoint queries; `*_probe`
    # must precede its prefix.
    psql = [
        {"match": ["capture:now"], "stdout": json.dumps("2026-07-15T11:20:00Z") + "\n"},
        {
            "match": ["capture:preflight_probe"],
            "stdout": json.dumps(
                {"captured_at": "2026-07-15T11:49:59Z", "query": _PROBE_QUERY, "row": _DB_IDENTITY}
            )
            + "\n",
        },
        {
            "match": ["capture:preflight"],
            "stdout": json.dumps({"captured_at": "2026-07-15T11:50:00Z", "database_identity": _DB_IDENTITY}) + "\n",
        },
        {"match": ["capture:role"], "stdout": json.dumps(_ROLE) + "\n"},
        {
            "match": ["capture:quiescence"],
            "stdout": json.dumps({"database_writes_quiescent": True, "conflicting_locks_absent": True}) + "\n",
        },
        {
            "match": ["capture:recovery_preflight"],
            "stdout": json.dumps(
                {"free_bytes": 500_000_000_000, "before_compressed": True, "before_row_count": 12}
            )
            + "\n",
        },
        {"match": ["capture:catalog_before"], "stdout": json.dumps(catalog_body) + "\n"},
        {"match": ["capture:catalog_after_first"], "stdout": json.dumps(catalog_body) + "\n"},
        {"match": ["capture:catalog_after_second"], "stdout": json.dumps(catalog_body) + "\n"},
        {"match": ["capture:catalog_post"], "stdout": json.dumps(catalog_post_body) + "\n"},
        {"match": ["capture:post_dry_selection"], "stdout": json.dumps(selection_body) + "\n"},
        {"match": ["capture:pre_enforce_selection"], "stdout": json.dumps(selection_body) + "\n"},
        {"match": ["capture:sizes_pre"], "stdout": json.dumps(sizes_body) + "\n"},
        {"match": ["capture:sizes_post"], "stdout": json.dumps(sizes_body) + "\n"},
        {
            "match": ["capture:cleanup_window"],
            "stdout": json.dumps(
                {
                    "captured_at": "2026-07-15T12:20:01Z",
                    "window_started_at": "2026-07-15T11:40:01Z",
                    "window_finished_at": "2026-07-15T12:20:00Z",
                }
            )
            + "\n",
        },
    ]
    _write_stub(bindir, "psql", psql)
    systemctl = []
    for unit, (enabled, active, sub, pid) in {
        "nhms-node27-autopipe.timer": ("enabled", "active", "waiting", 0),
        "nhms-node27-autopipe.service": ("static", "inactive", "dead", 0),
        "nhms-node27-timeseries-compression.timer": ("enabled", "inactive", "dead", 0),
        "nhms-node27-timeseries-compression.service": ("static", "inactive", "dead", 0),
        # MEASURED: the replay supervisor captures this preflight from INSIDE its
        # own running process, so it is activating with a live MainPID.
        "nhms-node27-timeseries-compression-replay.service": ("static", "activating", "start", 4137040),
    }.items():
        systemctl.append({"match": ["UnitFileState", unit], "stdout": _unit_show(enabled, active, sub, "success", pid)})
    _write_stub(bindir, "systemctl", systemctl)
    _write_stub(bindir, "journalctl", [{"match": ["--user"], "stdout": "-- boot --\njournal line\n"}])
    realpath = supervisor.CONTAINER_PG_RESTORE_REALPATH
    entries = "TABLE hydro river_timeseries\nTABLE met forcing_station_timeseries\n"
    _write_stub(
        bindir,
        "docker",
        [
            {
                # MEASURED node-27 shape: docker templates have no `dict`
                # function, so the producer asks for tab-separated fields.
                "match": ["inspect", ".State.Running"],
                "stdout": (
                    "/nhms-db\tcontainer-123\ttimescale/timescaledb:2.10.2-pg15\trunning\ttrue\n"
                ),
            },
            {"match": ["inspect", ".Image"], "stdout": "sha256:" + "a" * 64 + "\n"},
            {"match": ["exec", "--version"], "stdout": "pg_restore (PostgreSQL) 15.2\n"},
            {"match": ["exec", "--list"], "stdout": entries},
            {"match": ["exec", "readlink"], "stdout": realpath + "\n"},
            {"match": ["exec", "sha256sum"], "stdout": "b" * 64 + "  " + realpath + "\n"},
        ],
    )
    _write_stub(
        bindir,
        "git",
        [
            {"match": ["status"], "stdout": ""},
            {"match": ["remote", "get-url"], "stdout": "https://github.com/DankerMu/SHUD-NWM.git\n"},
        ],
    )


def _run_capture(
    kind: str, *, repo: Path, capture_bin: Path, evidence_dir: Path, extra: list[str] | None = None
) -> dict[str, Any]:
    argv = [
        sys.executable,
        str(ROOT / "scripts/node27_timeseries_compression_capture.py"),
        "--kind",
        kind,
        "--database",
        "nhms",
        "--mutation-head-sha",
        HEAD,
        "--repo",
        str(repo),
        "--container",
        "nhms-db",
        "--evidence-dir",
        str(evidence_dir),
        "--psql",
        str(capture_bin / "psql"),
        "--systemctl",
        str(capture_bin / "systemctl"),
        "--docker",
        str(capture_bin / "docker"),
        "--journalctl",
        str(capture_bin / "journalctl"),
        "--git",
        str(capture_bin / "git"),
        *(extra or []),
    ]
    import subprocess

    completed = subprocess.run(argv, capture_output=True, check=True)
    assert completed.stderr == b"", completed.stderr.decode()
    return json.loads(completed.stdout)


# --------------------------------------------------------------------------- #
# Plan-author gate
# --------------------------------------------------------------------------- #
def test_plan_author_emits_a_plan_the_real_supervisor_gate_accepts() -> None:
    plan, canonical, digest = plan_author.author_and_validate(mutation_head_sha=HEAD)
    # The real gate accepted it (no raise); identity is stable and reproducible.
    assert supervisor.validate_run_plan(plan, inherited_env={})["run_plan_id"] == plan["run_plan_id"]
    assert supervisor.run_plan_id(plan) == plan["run_plan_id"]
    assert canonical.endswith(b"\n")
    assert len(digest) == 64
    assert tuple(c["kind"] for c in plan["commands"]) == supervisor.EXPECTED_COMMAND_SEQUENCE
    assert tuple(c["kind"] for c in plan["captures"]) == supervisor.EXPECTED_CAPTURE_SEQUENCE


def test_plan_author_binds_the_mutation_head_into_the_decompress_command() -> None:
    plan = plan_author.build_run_plan(mutation_head_sha=HEAD)
    decompress = next(c for c in plan["commands"] if c["kind"] == "decompress")
    assert decompress["argv"][5] == HEAD
    list_command = next(c for c in plan["commands"] if c["kind"] == "pg_restore_list")
    assert list_command["argv"][-1].startswith("/var/lib/postgresql/")


# --------------------------------------------------------------------------- #
# Per-kind capture content locks (host-independent kinds)
# --------------------------------------------------------------------------- #
def test_capture_preflight_document_satisfies_the_verifier_contract(tmp_path: Path) -> None:
    repo = _fixture_repo(tmp_path)
    capture_bin = tmp_path / "capture-bin"
    _capture_stub_dir(capture_bin, schema_dump_container="/var/lib/postgresql/evidence/schema.dump")
    document = _run_capture("preflight_evidence", repo=repo, capture_bin=capture_bin, evidence_dir=tmp_path / "ev")
    summary = evidence._validate_preflight(document, HEAD)
    assert set(document) == set(evidence.PREFLIGHT_KEYS)
    assert len(summary["units"]) == len(evidence.EXPECTED_UNITS)


@pytest.mark.parametrize("kind", CATALOG_KINDS)
def test_capture_catalog_snapshot_satisfies_the_verifier_contract(tmp_path: Path, kind: str) -> None:
    repo = _fixture_repo(tmp_path)
    capture_bin = tmp_path / "capture-bin"
    _capture_stub_dir(capture_bin, schema_dump_container="/var/lib/postgresql/evidence/schema.dump")
    document = _run_capture(kind, repo=repo, capture_bin=capture_bin, evidence_dir=tmp_path / "ev")
    phase = {
        "catalog_before": "pre-migration",
        "catalog_after_first": "after-first-apply",
        "catalog_after_second": "after-second-apply",
    }[kind]
    validator = evidence._validate_pre_migration_catalog if kind == "catalog_before" else evidence._validate_d3_catalog
    evidence._catalog_snapshot(document, label=kind, mutation_head_sha=HEAD, phase=phase, validator=validator)


def test_capture_catalog_post_document_binds_the_compressed_chunk(tmp_path: Path) -> None:
    repo = _fixture_repo(tmp_path)
    capture_bin = tmp_path / "capture-bin"
    _capture_stub_dir(capture_bin, schema_dump_container="/var/lib/postgresql/evidence/schema.dump")
    document = _run_capture("catalog_post", repo=repo, capture_bin=capture_bin, evidence_dir=tmp_path / "ev")
    assert set(document) == {
        "captured_at",
        "snapshot_id",
        "mutation_head_sha",
        "catalog",
        "compressed_chunk_identities",
    }
    evidence._validate_d3_catalog(document["catalog"], "catalog.post.catalog")
    assert {"chunk_name": "_hyper_3_7_chunk"}.items() <= document["compressed_chunk_identities"][0].items()


# --------------------------------------------------------------------------- #
# Full pipeline dress-rehearsal: real plan-author + real supervisor state
# machine + real capture-producer against measured-node-27 stub binaries.
# --------------------------------------------------------------------------- #
def _install_checkpoint_stubs(bindir: Path, *, schema_dump_container: str) -> None:
    _write_stub(bindir, "psql", sup._psql_responses())
    _write_stub(bindir, "systemctl", sup._systemctl_responses())
    _write_stub(bindir, "journalctl", sup._journalctl_responses())
    _write_stub(bindir, "docker", sup._docker_responses(dump_path=schema_dump_container))


def _stub_command_writers(plan: dict[str, Any], schema_dump_host: str) -> None:
    for command in plan["commands"]:
        associations = command["artifact_associations"]
        if command["kind"] == "pg_restore_list":
            command["argv"] = [sys.executable, "-c", "pass", "/var/lib/postgresql/evidence/schema.dump"]
            continue
        if not associations:
            command["argv"] = [sys.executable, "-c", "pass"]
            continue
        writes = []
        for name, path in associations.items():
            if name == "schema_dump":
                writes.append(f"Path({path!r}).write_bytes(b'PGDMP\\x00forensic schema\\n')")
            else:
                writes.append(f"Path({path!r}).write_text('{{\"owner\":\"{name}\"}}\\n')")
        command["argv"] = [sys.executable, "-c", "from pathlib import Path; " + "; ".join(writes)]


def test_authored_plan_survives_the_real_state_machine_and_verifier_validators(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run-user"))
    repo = _fixture_repo(tmp_path)
    root = tmp_path / "replay"
    root.mkdir()
    schema_dump_host = str(tmp_path / "schema-before.dump")
    schema_dump_container = "/var/lib/postgresql/evidence/schema.dump"

    checkpoint_bin = tmp_path / "checkpoint-bin"
    capture_bin = tmp_path / "capture-bin"
    _install_checkpoint_stubs(checkpoint_bin, schema_dump_container=schema_dump_container)
    _capture_stub_dir(capture_bin, schema_dump_container=schema_dump_container)
    monkeypatch.setattr(supervisor, "SUPERVISOR_BIN_DIR", checkpoint_bin)

    plan = plan_author.build_run_plan(
        mutation_head_sha=HEAD,
        capture_repo=str(repo),
        root=str(root),
        schema_dump_host=schema_dump_host,
        schema_dump_container=schema_dump_container,
        capture_python=sys.executable,
        capture_script=str(ROOT / "scripts/node27_timeseries_compression_capture.py"),
        capture_psql=str(capture_bin / "psql"),
        capture_systemctl=str(capture_bin / "systemctl"),
        capture_docker=str(capture_bin / "docker"),
        capture_journalctl=str(capture_bin / "journalctl"),
        capture_git=str(capture_bin / "git"),
    )
    # The real gate accepts the authored plan before any execution.
    supervisor.validate_run_plan(plan, inherited_env={})
    _stub_command_writers(plan, schema_dump_host)

    ledger_path = tmp_path / "supervisor-ledger.jsonl"
    cursor = {"value": "s=stub;i=start;b=stub;m=0;t=0;x=0"}
    checkpoints_by_phase = {(str(c["phase"]), c["command_id"]): c for c in plan["checkpoints"]}
    with sup._ledger(ledger_path) as ledger:

        def live_checkpoint(phase: str, command_id: str | None) -> None:
            cursor["value"] = supervisor.capture_checkpoint(
                checkpoints_by_phase[(phase, command_id)],
                wall=supervisor.HardWall.start(120),
                ledger=ledger,
                artifact_dir=tmp_path,
                journal_cursor=cursor["value"],
                invocation_id=sup.PROBE_INVOCATION_ID,
            )

        supervisor.execute_producer_state_machine(
            plan,
            wall=supervisor.HardWall.start(120),
            ledger=ledger,
            artifact_dir=tmp_path,
            checkpoint_runner=live_checkpoint,
            restore_identity_resolver=lambda w, dump: supervisor.resolve_container_pg_restore_identity(
                wall=w, dump_path=dump
            ),
        )

    events = [json.loads(line) for line in ledger_path.read_text().splitlines()]
    capture_events = [e for e in events if e["event_type"] == "capture"]
    assert tuple(e["kind"] for e in capture_events) == supervisor.EXPECTED_CAPTURE_SEQUENCE

    # Every capture produced a real, published, non-empty artifact.
    for event in capture_events:
        published = Path(event["artifact_association"]["artifact"]["path"])
        assert published.exists() and published.stat().st_size > 0
        json.loads(published.read_text())

    def produced(kind: str) -> dict[str, Any]:
        event = next(e for e in capture_events if e["kind"] == kind)
        return json.loads(Path(event["artifact_association"]["artifact"]["path"]).read_text())

    # Feed the REAL capture documents to the REAL verifier content validators:
    # a placeholder or a dropped field turns this RED after the mutation window.
    preflight_summary = evidence._validate_preflight(produced("preflight_evidence"), HEAD)
    assert len(preflight_summary["units"]) == len(evidence.EXPECTED_UNITS)
    for kind, phase, validator in (
        ("catalog_before", "pre-migration", evidence._validate_pre_migration_catalog),
        ("catalog_after_first", "after-first-apply", evidence._validate_d3_catalog),
        ("catalog_after_second", "after-second-apply", evidence._validate_d3_catalog),
    ):
        evidence._catalog_snapshot(produced(kind), label=kind, mutation_head_sha=HEAD, phase=phase, validator=validator)
    evidence._validate_d3_catalog(produced("catalog_post")["catalog"], "catalog.post.catalog")

    # G10: the committed bundle-author must assemble its input bundle from THIS
    # real supervisor replay work dir -- discovering every artifact's on-disk
    # path from the genuine ledger the state machine just wrote, not from a
    # hand-maintained filename list.  This is the deliverable that closes the
    # "hand-assembled ten-step procedure" gap: the assembler is exercised
    # against real supervisor output, not a synthetic fixture.
    run_plan_path = tmp_path / "run-plan.json"
    run_plan_path.write_text(json.dumps(plan), encoding="utf-8")
    built = bundle_author.build_bundle(
        work_dir=tmp_path,
        repo_path=evidence.REPO_ROOT,
        run_plan_path=run_plan_path,
        ledger_path=ledger_path,
        schema_dump_path=schema_dump_host,
        mutation_head_sha=HEAD,
        verifier_head_sha="89abcdef0123456789abcdef0123456789abcdef",
        generated_at="2026-07-15T12:00:00Z",
    )

    # Exact top-level contract shape the verifier's `verify_bundle` requires.
    assert set(built) == {
        "schema_version",
        "issue",
        "generated_at",
        "node",
        "mutation_head_sha",
        "verifier_head_sha",
        "database_identity",
        "authorization",
        "execution",
        "recovery",
        "preflight",
        "migration",
        "selection",
        "receipts",
        "sizes",
        "catalog",
        "benchmarks",
        "cleanup",
        "out_of_scope",
    }

    # The author bound its capture-sourced references to the exact artifacts the
    # real supervisor published in the ledger -- discovered, not hardcoded.
    ledger_capture_path = {
        e["kind"]: e["artifact_association"]["artifact"]["path"] for e in capture_events
    }
    assert built["preflight"]["catalog_before"]["path"] == ledger_capture_path["catalog_before"]
    assert built["migration"]["catalog_after_first"]["path"] == ledger_capture_path["catalog_after_first"]
    assert built["migration"]["catalog_after_second"]["path"] == ledger_capture_path["catalog_after_second"]
    assert built["catalog"]["post"]["path"] == ledger_capture_path["catalog_post"]
    assert built["cleanup"]["evidence"]["path"] == ledger_capture_path["cleanup"]

    # Child-produced (decompress) receipt is discovered from the child event.
    child_events = [e for e in events if e["event_type"] == "child_exit"]
    decompress_receipt = next(
        e["artifact_associations"]["recovery_receipt"]["artifact"]["path"]
        for e in child_events
        if e["kind"] == "decompress"
    )
    assert built["recovery"]["receipt"]["path"] == decompress_receipt

    # database_identity is echoed from the real preflight evidence document.
    assert built["database_identity"] == produced("preflight_evidence")["database_identity"]

    # Every reference is a real {path,sha256,bytes} triple recomputed from bytes.
    def _refs(value: Any) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        stack: list[Any] = [value]
        while stack:
            current = stack.pop()
            if isinstance(current, dict):
                if set(current) == {"path", "sha256", "bytes"}:
                    found.append(current)
                else:
                    stack.extend(current.values())
            elif isinstance(current, list):
                stack.extend(current)
        return found

    import hashlib as _hashlib
    import re as _re

    for reference in _refs(built):
        path = Path(reference["path"])
        assert path.is_file() and not path.is_symlink()
        raw = path.read_bytes()
        assert _re.fullmatch(r"[0-9a-f]{64}", reference["sha256"]) is not None
        assert reference["sha256"] == _hashlib.sha256(raw).hexdigest()
        assert reference["bytes"] == len(raw) > 0
