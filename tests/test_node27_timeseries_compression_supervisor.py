"""Focused invariant tests for the issue #1069 controlled producer."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from packages.common import compression_terminal_state as terminal_state
from packages.common.evidence_io import (
    BoundedEvidenceError,
    assert_output_disjoint_from_closure,
    resolve_artifact_closure,
)
from scripts import node27_timeseries_compression_supervisor as supervisor


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        (
            {
                "_SYSTEMD_UNIT": "user@1000.service",
                "_SYSTEMD_USER_UNIT": "nhms-node27-timeseries-compression-replay.service",
            },
            "nhms-node27-timeseries-compression-replay.service",
        ),
        (
            {
                "_SYSTEMD_UNIT": "user@1000.service",
                "_SYSTEMD_USER_UNIT": "init.scope",
                "USER_UNIT": "nhms-node27-timeseries-compression-replay.service",
            },
            "nhms-node27-timeseries-compression-replay.service",
        ),
        (
            {
                "_SYSTEMD_UNIT": "user@1000.service",
                "USER_UNIT": "nhms-node27-timeseries-compression.service",
            },
            "nhms-node27-timeseries-compression.service",
        ),
        ({"_SYSTEMD_UNIT": "user@1000.service"}, None),
        ({"_SYSTEMD_UNIT": "nhms-node27-timeseries-compression-replay.service"},
         "nhms-node27-timeseries-compression-replay.service"),
        (
            {
                "_SYSTEMD_UNIT": "nhms-node27-timeseries-compression-replay.service",
                "_SYSTEMD_USER_UNIT": "unrelated.service",
            },
            None,
        ),
        (
            {
                "_SYSTEMD_UNIT": "nhms-node27-timeseries-compression-replay.service",
                "_SYSTEMD_USER_UNIT": "",
            },
            None,
        ),
    ],
)
def test_governed_user_unit_prefers_user_journal_fields(
    row: dict[str, Any], expected: str | None
) -> None:
    assert supervisor._governed_user_unit(row) == expected


def test_governed_user_unit_rejects_conflicting_user_fields() -> None:
    with pytest.raises(supervisor.SupervisorError, match="fields conflict"):
        supervisor._governed_user_unit(
            {
                "_SYSTEMD_USER_UNIT": "nhms-node27-timeseries-compression-replay.service",
                "USER_UNIT": "nhms-node27-timeseries-compression.service",
            }
        )


def _plan() -> dict[str, Any]:
    commands: list[dict[str, Any]] = []
    serial = 0
    occurrence: dict[str, int] = {}
    for kind in supervisor.EXPECTED_COMMAND_SEQUENCE:
        index = occurrence.get(kind, 0)
        occurrence[kind] = index + 1
        serial += 1
        associations: dict[str, str] = {}
        if kind == "pg_dump":
            associations = {
                "schema_dump": "/tmp/schema.dump",
            }
            argv = [
                "/usr/bin/pg_dump",
                "--dbname",
                "nhms",
                "--format=custom",
                "--schema-only",
                "--file",
                associations["schema_dump"],
            ]
        elif kind == "pg_restore_version":
            argv = ["/usr/bin/docker", "exec", "nhms-db", "/usr/bin/pg_restore", "--version"]
        elif kind == "pg_restore_list":
            associations = {}
            argv = [
                "/usr/bin/docker",
                "exec",
                "nhms-db",
                "/usr/bin/pg_restore",
                "--list",
                "/var/lib/postgresql/evidence/schema.dump",
            ]
        elif kind == "migration_apply":
            associations = {}
            argv = [
                "/usr/bin/psql",
                "--dbname",
                "nhms",
                "--no-psqlrc",
                "--set",
                "ON_ERROR_STOP=1",
                "--file",
                "/home/nwm/NWM/db/migrations/000047_hypertable_compression_settings.sql",
            ]
        elif kind == "decompress":
            associations = {
                "recovery_receipt": "/tmp/recovery-receipt.json",
            }
            argv = [
                "/home/nwm/NWM/.venv/bin/python",
                "/home/nwm/NWM/scripts/node27_timeseries_decompression_replay.py",
                "--database",
                "nhms",
                "--mutation-head-sha",
                "a" * 40,
                "--receipt-path",
                associations["recovery_receipt"],
                "--hypertable-schema",
                "hydro",
                "--hypertable-name",
                "river_timeseries",
                "--chunk-schema",
                "_timescaledb_internal",
                "--chunk-name",
                "_hyper_3_7_chunk",
                "--range-start",
                "2026-05-28T00:00:00Z",
                "--range-end",
                "2026-06-04T00:00:00Z",
            ]
        elif kind.startswith("compression_"):
            associations = (
                {"dry_run_receipt": "/tmp/dry.json"}
                if kind == "compression_dry_run"
                else {"enforce_receipt": "/tmp/enforce.json"}
            )
            receipt = associations["enforce_receipt" if kind == "compression_enforce" else "dry_run_receipt"]
            argv = [
                "/home/nwm/NWM/scripts/node27_timeseries_compression_once.sh",
                *(["--enforce"] if kind == "compression_enforce" else []),
                "--receipt-path",
                receipt,
                "--lock-path",
                "/home/nwm/node27-timeseries-compression-replay/compression.lock",
            ]
        else:
            associations = (
                {"benchmark_before": "/home/nwm/node27-timeseries-compression-replay/benchmark-before.json"}
                if kind == "benchmark_before"
                else {
                    "benchmarks": "/tmp/benchmarks.json",
                }
            )
            phase = "before" if kind == "benchmark_before" else "after"
            argv = [
                "/home/nwm/NWM/.venv/bin/python",
                "/home/nwm/NWM/scripts/node27_timeseries_compression_benchmark.py",
                "--phase",
                phase,
                *(
                    ["--before-path", "/home/nwm/node27-timeseries-compression-replay/benchmark-before.json"]
                    if phase == "after"
                    else []
                ),
                "--output",
                associations["benchmark_before" if phase == "before" else "benchmarks"],
                "--curve-basin-version-id",
                "basins_heihe_vbasins",
                "--curve-river-segment-id",
                "basins_heihe_shud_reach_000001",
                "--curve-river-network-version-id",
                "basins_heihe_rivnet_vbasins",
                "--curve-issue-time",
                "2026-05-31T06:00:00Z",
                "--curve-end-time",
                "2026-06-07T06:00:00Z",
                "--curve-scenario",
                "forecast_gfs_deterministic",
                "--mvt-run-id",
                "fcst_gfs_2026053106_basins_heihe_shud",
                "--mvt-basin-version-id",
                "basins_heihe_vbasins",
                "--mvt-river-network-version-id",
                "basins_heihe_rivnet_vbasins",
                "--mvt-valid-time",
                "2026-05-31T06:00:00Z",
                "--mvt-z",
                "9",
                "--mvt-x",
                "399",
                "--mvt-y",
                "189",
            ]
        commands.append(
            {
                "command_id": f"command-{serial}",
                "kind": kind,
                "argv": argv,
                "artifact_associations": associations,
            }
        )
    mutation_ids = [item["command_id"] for item in commands if item["kind"] in supervisor.MUTATION_KINDS]
    captures = [
        {
            "capture_id": f"capture-{kind}",
            "kind": kind,
            "argv": [sys.executable, "-c", "print('{}')"],
            "output_path": f"/tmp/{kind}.json",
        }
        for kind in supervisor.EXPECTED_CAPTURE_SEQUENCE
    ]
    checkpoints = [
        {"checkpoint_id": "preflight", "phase": "preflight", "command_id": None},
        {"checkpoint_id": "postflight", "phase": "postflight", "command_id": None},
        {"checkpoint_id": "cleanup", "phase": "cleanup", "command_id": None},
    ]
    for command_id in mutation_ids:
        checkpoints.extend(
            [
                {
                    "checkpoint_id": f"before-{command_id}",
                    "phase": "before_mutation",
                    "command_id": command_id,
                },
                {
                    "checkpoint_id": f"after-{command_id}",
                    "phase": "after_mutation",
                    "command_id": command_id,
                },
            ]
        )
    plan = {
        "plan_version": "1.0",
        "run_plan_id": "",
        "mutation_head_sha": "a" * 40,
        "reviewed_remote_ref": "refs/remotes/origin/feat/issue-1069-live-compression",
        "database": "nhms",
        "repo_path": "/home/nwm/NWM",
        "operator_attestation": {
            "sole_db_user_during_window": True,
            "database_audit_proof": False,
            "trust_limit": "discrete observations; no absolute direct-SQL bypass proof",
        },
        "commands": commands,
        "captures": captures,
        "checkpoints": checkpoints,
    }
    plan["run_plan_id"] = supervisor.run_plan_id(plan)
    return plan


def _write_ref(path: Path, value: Any) -> dict[str, Any]:
    raw = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    path.write_bytes(raw)
    return {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}


def test_run_plan_accepts_exact_concrete_cardinality_and_current_d3() -> None:
    plan = _plan()
    assert supervisor.validate_run_plan(plan, inherited_env={})["commands"] == plan["commands"]
    settings = [
        ("hydro", "river_timeseries", "run_id", 1, None, None, None),
        ("hydro", "river_timeseries", "river_network_version_id", 2, None, None, None),
        ("hydro", "river_timeseries", "river_segment_id", 3, None, None, None),
        ("hydro", "river_timeseries", "variable", None, 1, True, False),
        ("hydro", "river_timeseries", "valid_time", None, 2, True, False),
        ("met", "forcing_station_timeseries", "forcing_version_id", 1, None, None, None),
        ("met", "forcing_station_timeseries", "station_id", 2, None, None, None),
        ("met", "forcing_station_timeseries", "variable", None, 1, True, False),
        ("met", "forcing_station_timeseries", "valid_time", None, 2, True, False),
    ]
    fields = (
        "hypertable_schema",
        "hypertable_name",
        "attname",
        "segmentby_column_index",
        "orderby_column_index",
        "orderby_asc",
        "orderby_nullsfirst",
    )
    supervisor.validate_current_d3(
        {
            "hypertables": {
                "hydro.river_timeseries": True,
                "met.forcing_station_timeseries": True,
            },
            "compression_settings": [dict(zip(fields, values, strict=True)) for values in settings],
            "policy_jobs": [],
        }
    )


@pytest.mark.parametrize("failure", ["placeholder", "override", "extra", "checkpoint"])
def test_run_plan_rejects_unbound_or_unowned_execution(failure: str) -> None:
    plan = _plan()
    inherited: dict[str, str] = {}
    if failure == "placeholder":
        plan["commands"][0]["argv"][0] = "<psql>"
    elif failure == "override":
        inherited["PYTHONPATH"] = "/tmp/attacker"
    elif failure == "extra":
        plan["commands"].append(
            {
                "command_id": "extra",
                "kind": "pg_dump",
                "argv": ["/usr/bin/pg_dump"],
                "artifact_associations": {},
            }
        )
    else:
        plan["checkpoints"].pop()
    with pytest.raises(supervisor.SupervisorError):
        supervisor.validate_run_plan(plan, inherited_env=inherited)


@pytest.mark.parametrize("kind", sorted(supervisor.AUTHORIZED_KINDS))
def test_each_command_kind_rejects_true_substitution(kind: str) -> None:
    plan = _plan()
    command = next(item for item in plan["commands"] if item["kind"] == kind)
    command["argv"] = ["/bin/true"]
    plan["run_plan_id"] = supervisor.run_plan_id(plan)
    with pytest.raises(supervisor.SupervisorError, match="argv|executable|contract|differs"):
        supervisor.validate_run_plan(plan, inherited_env={})


def test_external_hard_wall_terms_kills_drains_reaps_and_ledgers(tmp_path: Path) -> None:
    ledger_path = tmp_path / "ledger.jsonl"
    command = {
        "command_id": "blocked",
        "kind": "pg_dump",
        "argv": [sys.executable, "-c", "import time; print('started', flush=True); time.sleep(30)"],
        "artifact_associations": {},
    }
    started = time.monotonic()
    with supervisor.AppendOnlyLedger(ledger_path, run_id="run", run_plan_id="plan", invocation_id="1" * 32) as ledger:
        with pytest.raises(supervisor.HardWallExpired):
            supervisor.run_child(
                command,
                wall=supervisor.HardWall.start(0.2),
                ledger=ledger,
                mutation_head_sha="a" * 40,
                database="nhms",
                term_grace=0.05,
            )
    assert time.monotonic() - started < 2
    event = json.loads(ledger_path.read_text().strip())
    assert event["terminated_by_supervisor"] is True
    assert event["finished_monotonic"] > event["started_monotonic"]
    assert event["stdout"]["bytes"] <= supervisor.MAX_STREAM_BYTES


def test_output_flood_is_bounded_and_reaped(tmp_path: Path) -> None:
    ledger_path = tmp_path / "ledger.jsonl"
    command = {
        "command_id": "flood",
        "kind": "pg_dump",
        "argv": [
            sys.executable,
            "-c",
            "import os; b=b'x'.ljust(65536,b'x')\nwhile True: os.write(1,b)",
        ],
        "artifact_associations": {},
    }
    with supervisor.AppendOnlyLedger(ledger_path, run_id="run", run_plan_id="plan", invocation_id="1" * 32) as ledger:
        with pytest.raises(supervisor.HardWallExpired):
            supervisor.run_child(
                command,
                wall=supervisor.HardWall.start(2),
                ledger=ledger,
                mutation_head_sha="a" * 40,
                database="nhms",
                stdout_limit=32 * 1024,
                term_grace=0.05,
            )
    event = json.loads(ledger_path.read_text().strip())
    assert event["stdout"] == {
        "bytes": 32 * 1024,
        "sha256": event["stdout"]["sha256"],
        "truncated": True,
    }


def test_child_ledgers_observed_produced_artifact_not_authored_identity(tmp_path: Path) -> None:
    produced = tmp_path / "produced.json"
    command = {
        "command_id": "producer",
        "kind": "pg_dump",
        "argv": [sys.executable, "-c", f"from pathlib import Path; Path({str(produced)!r}).write_text('ok')"],
        "artifact_associations": {"catalog": str(produced)},
    }
    ledger_path = tmp_path / "ledger.jsonl"
    with supervisor.AppendOnlyLedger(ledger_path, run_id="run", run_plan_id="plan", invocation_id="1" * 32) as ledger:
        event = supervisor.run_child(
            command,
            wall=supervisor.HardWall.start(2),
            ledger=ledger,
            mutation_head_sha="a" * 40,
            database="nhms",
        )
    observed = event["artifact_associations"]["catalog"]
    assert observed["artifact"] == {
        "path": str(produced),
        "sha256": hashlib.sha256(b"ok").hexdigest(),
        "bytes": 2,
    }
    assert (observed["device"], observed["inode"]) == (
        produced.stat().st_dev,
        produced.stat().st_ino,
    )


def test_child_refuses_preexisting_planned_output(tmp_path: Path) -> None:
    output = tmp_path / "already-there.json"
    output.write_text("stale", encoding="utf-8")
    command = {
        "command_id": "producer",
        "kind": "pg_dump",
        "argv": [sys.executable, "-c", "raise SystemExit(0)"],
        "artifact_associations": {"catalog": str(output)},
    }
    with supervisor.AppendOnlyLedger(
        tmp_path / "ledger.jsonl", run_id="run", run_plan_id="plan", invocation_id="1" * 32
    ) as ledger:
        with pytest.raises(supervisor.SupervisorError, match="exists before spawn"):
            supervisor.run_child(
                command,
                wall=supervisor.HardWall.start(2),
                ledger=ledger,
                mutation_head_sha="a" * 40,
                database="nhms",
            )


def test_capture_step_is_the_only_writer_and_rejects_preexisting_output(tmp_path: Path) -> None:
    output = tmp_path / "capture.json"
    capture = {
        "capture_id": "capture-preflight",
        "kind": "preflight_evidence",
        "argv": [sys.executable, "-c", "print('{\"captured\":true}')"],
        "output_path": str(output),
    }
    ledger_path = tmp_path / "capture-ledger.jsonl"
    assert not output.exists()
    with supervisor.AppendOnlyLedger(
        ledger_path, run_id="run", run_plan_id="plan", invocation_id="1" * 32
    ) as ledger:
        event = supervisor.run_capture_step(
            capture,
            wall=supervisor.HardWall.start(2),
            ledger=ledger,
            artifact_dir=tmp_path,
        )
    assert json.loads(output.read_text()) == {"captured": True}
    assert event["artifact_association"]["artifact"]["path"] == str(output)
    with supervisor.AppendOnlyLedger(
        tmp_path / "second-ledger.jsonl", run_id="run", run_plan_id="plan", invocation_id="1" * 32
    ) as ledger:
        with pytest.raises(supervisor.SupervisorError, match="exists before its owner"):
            supervisor.run_capture_step(
                capture,
                wall=supervisor.HardWall.start(2),
                ledger=ledger,
                artifact_dir=tmp_path,
            )


def test_harmless_full_state_machine_has_no_fixture_prewrite_or_out_of_band_owner(tmp_path: Path) -> None:
    plan = _plan()
    expected_outputs: list[Path] = []
    for command in plan["commands"]:
        associations: dict[str, str] = {}
        for name in command["artifact_associations"]:
            path = tmp_path / f"child-{command['command_id']}-{name}.json"
            associations[name] = str(path)
            expected_outputs.append(path)
        command["artifact_associations"] = associations
        writes = "; ".join(
            f"Path({path!r}).write_text('{{\"owner\":\"{name}\"}}\\n')"
            for name, path in associations.items()
        )
        command["argv"] = [
            sys.executable,
            "-c",
            f"from pathlib import Path; {writes or 'pass'}",
        ]
    for capture in plan["captures"]:
        path = tmp_path / f"capture-{capture['kind']}.json"
        capture["output_path"] = str(path)
        capture["argv"] = [sys.executable, "-c", f"print('{{\"owner\":\"{capture['kind']}\"}}')"]
        expected_outputs.append(path)
    assert all(not path.exists() for path in expected_outputs)
    checkpoints: list[tuple[str, str | None]] = []
    ledger_path = tmp_path / "state-machine-ledger.jsonl"
    with supervisor.AppendOnlyLedger(
        ledger_path, run_id="run", run_plan_id="plan", invocation_id="1" * 32
    ) as ledger:
        supervisor.execute_producer_state_machine(
            plan,
            wall=supervisor.HardWall.start(10),
            ledger=ledger,
            artifact_dir=tmp_path,
            checkpoint_runner=lambda phase, command_id: checkpoints.append((phase, command_id)),
            restore_identity_resolver=lambda _wall, _dump: {
                "dump_sha256": "1" * 64,
                "container_image_id": "sha256:" + "2" * 64,
                "binary_realpath": "/usr/bin/pg_restore",
                "binary_sha256": "3" * 64,
            },
        )
    assert all(path.is_file() for path in expected_outputs)
    events = [json.loads(line) for line in ledger_path.read_text().splitlines()]
    assert sum(event["event_type"] == "child_exit" for event in events) == len(plan["commands"])
    assert [event["kind"] for event in events if event["event_type"] == "capture"] == list(
        supervisor.EXPECTED_CAPTURE_SEQUENCE
    )
    assert checkpoints[0] == ("preflight", None)
    assert checkpoints[-2:] == [("postflight", None), ("cleanup", None)]


def test_child_environment_passes_only_reviewed_database_allowlist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_URL", "opaque-test-value")
    monkeypatch.setenv("UNREVIEWED_OVERRIDE", "must-not-pass")
    command = {
        "command_id": "env",
        "kind": "pg_dump",
        "argv": [
            sys.executable,
            "-c",
            (
                "import os; assert os.environ.get('DATABASE_URL') == 'opaque-test-value'; "
                "assert 'UNREVIEWED_OVERRIDE' not in os.environ"
            ),
        ],
        "artifact_associations": {},
    }
    with supervisor.AppendOnlyLedger(
        tmp_path / "ledger.jsonl", run_id="run", run_plan_id="plan", invocation_id="1" * 32
    ) as ledger:
        event = supervisor.run_child(
            command,
            wall=supervisor.HardWall.start(2),
            ledger=ledger,
            mutation_head_sha="a" * 40,
            database="nhms",
        )
    assert event["exit_code"] == 0
    assert "environment" not in event


def test_hard_wall_kills_forked_grandchild_holding_pipes(tmp_path: Path) -> None:
    ledger_path = tmp_path / "ledger.jsonl"
    source = (
        "import os,signal,time\n"
        "pid=os.fork()\n"
        "if pid==0:\n"
        " signal.signal(signal.SIGTERM, signal.SIG_IGN); print('grandchild', flush=True); time.sleep(30)\n"
        "else: time.sleep(30)\n"
    )
    command = {
        "command_id": "forked",
        "kind": "pg_dump",
        "argv": [sys.executable, "-c", source],
        "artifact_associations": {},
    }
    started = time.monotonic()
    with supervisor.AppendOnlyLedger(ledger_path, run_id="run", run_plan_id="plan", invocation_id="1" * 32) as ledger:
        with pytest.raises(supervisor.HardWallExpired):
            supervisor.run_child(
                command,
                wall=supervisor.HardWall.start(0.2),
                ledger=ledger,
                mutation_head_sha="a" * 40,
                database="nhms",
                term_grace=0.05,
            )
    assert time.monotonic() - started < 2


def test_transitive_closure_rejects_nested_alias_hardlink_cycle_and_incomplete_manifest(
    tmp_path: Path,
) -> None:
    leaf = tmp_path / "leaf.txt"
    leaf.write_text("leaf")
    leaf_ref = {
        "path": str(leaf),
        "sha256": hashlib.sha256(b"leaf").hexdigest(),
        "bytes": 4,
    }
    nested_ref = _write_ref(tmp_path / "nested.json", {"deep": {"artifact": leaf_ref}})
    closure = resolve_artifact_closure({"top": nested_ref})
    assert list(closure.manifest) == [nested_ref, leaf_ref]
    with pytest.raises(BoundedEvidenceError, match="aliases"):
        assert_output_disjoint_from_closure(leaf, closure, label="terminal")
    hardlink = tmp_path / "hardlink.txt"
    os.link(leaf, hardlink)
    hardlink_ref = {**leaf_ref, "path": str(hardlink)}
    with pytest.raises(BoundedEvidenceError, match="alias or cycle"):
        resolve_artifact_closure({"a": leaf_ref, "b": hardlink_ref})
    # Repeated identical edges are de-duplicated; a tampered edge cannot hide
    # behind the first occurrence.
    assert len(resolve_artifact_closure({"a": nested_ref, "b": nested_ref}).manifest) == 2
    with pytest.raises(BoundedEvidenceError, match="identity differs"):
        resolve_artifact_closure({"a": {**nested_ref, "sha256": "0" * 64}})


def test_safe_cas_finalizer_and_invalid_config_no_touch(tmp_path: Path) -> None:
    receipt = tmp_path / "receipt.json"
    stale = b'{"schema_version":"3.0","qualifies_task_4_5":true}\n'
    receipt.write_bytes(stale)
    digest = hashlib.sha256(stale).hexdigest()
    assert supervisor.finalize_receipt(
        receipt,
        expected_stale_sha256=digest,
        run_id="run",
        stage="timeout",
        possible_mutation=True,
        mutation_head_sha="a" * 40,
    )
    tombstone = json.loads(receipt.read_text())
    assert tombstone["failure"]["mutation_state"] == "indeterminate"
    assert tombstone["mutation_head_sha"] == "a" * 40
    schema = json.loads(
        (Path(__file__).parents[1] / "schemas/timeseries_compression_live_evidence.schema.json").read_text()
    )
    jsonschema.Draft202012Validator(schema).validate(tombstone)
    assert not supervisor.finalize_receipt(
        receipt,
        expected_stale_sha256=digest,
        run_id="run",
        stage="timeout",
        possible_mutation=True,
        mutation_head_sha="a" * 40,
    )
    missing = tmp_path / "missing" / "receipt.json"
    assert not supervisor.finalize_receipt(
        missing,
        expected_stale_sha256=digest,
        run_id="run",
        stage="config",
        possible_mutation=False,
        mutation_head_sha="a" * 40,
    )
    assert not missing.parent.exists()
    target = tmp_path / "target"
    target.write_bytes(stale)
    alias = tmp_path / "alias"
    alias.symlink_to(target)
    assert not supervisor.finalize_receipt(
        alias,
        expected_stale_sha256=digest,
        run_id="run",
        stage="config",
        possible_mutation=False,
        mutation_head_sha="a" * 40,
    )
    assert target.read_bytes() == stale


def test_finalizer_detects_publish_race_and_state_is_consumed_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt = tmp_path / "receipt.json"
    stale = b'{"outcome":"stale"}\n'
    newer = b'{"outcome":"newer"}\n'
    receipt.write_bytes(stale)
    digest = hashlib.sha256(stale).hexdigest()
    original_replace = terminal_state._atomic_replace_terminal_at

    def race(
        parent_fd: int,
        parent_path: Path,
        path: Path,
        raw: bytes,
        *,
        expected: Any,
    ) -> Any:
        path.write_bytes(newer)
        return original_replace(parent_fd, parent_path, path, raw, expected=expected)

    monkeypatch.setattr(terminal_state, "_atomic_replace_terminal_at", race)
    assert not supervisor.finalize_receipt(
        receipt,
        expected_stale_sha256=digest,
        run_id="race-run",
        stage="timeout",
        possible_mutation=True,
        mutation_head_sha="a" * 40,
    )
    assert receipt.read_bytes() == newer
    monkeypatch.setattr(terminal_state, "_atomic_replace_terminal_at", original_replace)

    receipt.write_bytes(stale)
    state = tmp_path / "state.json"
    supervisor._write_finalizer_state(
        state,
        receipt_path=receipt,
        expected_stale_sha256=digest,
        run_id="race-run",
        mutation_head_sha="a" * 40,
    )
    assert json.loads(state.read_text())["mutation_head_sha"] == "a" * 40
    assert supervisor.finalize_from_state(state, stage="timeout")
    assert not supervisor.finalize_from_state(state, stage="timeout")
    assert not state.exists()


def test_held_publish_lock_times_out_without_overwriting_newer_terminal(tmp_path: Path) -> None:
    receipt = tmp_path / "receipt.json"
    stale = b'{"outcome":"stale"}\n'
    receipt.write_bytes(stale)
    lock_path = receipt.with_name(f".{receipt.name}.publish.lock")
    lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    started = time.monotonic()
    try:
        assert not supervisor.finalize_receipt(
            receipt,
            expected_stale_sha256=hashlib.sha256(stale).hexdigest(),
            run_id="held-lock",
            stage="timeout",
            possible_mutation=True,
            mutation_head_sha="a" * 40,
            deadline_monotonic=time.monotonic() + 0.05,
        )
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)
    assert time.monotonic() - started < 0.5
    assert receipt.read_bytes() == stale


def test_finalizer_state_retries_after_lock_release_then_consumes_once(tmp_path: Path) -> None:
    receipt = tmp_path / "receipt.json"
    stale = b'{"schema_version":"3.0","qualifies_task_4_5":true}\n'
    receipt.write_bytes(stale)
    state = tmp_path / "state.json"
    supervisor._write_finalizer_state(
        state,
        receipt_path=receipt,
        expected_stale_sha256=hashlib.sha256(stale).hexdigest(),
        run_id="retry-run",
        mutation_head_sha="a" * 40,
    )
    lock_fd = os.open(receipt.with_name(f".{receipt.name}.publish.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        assert not supervisor.finalize_from_state(
            state, stage="timeout", deadline_monotonic=time.monotonic() + 0.05
        )
        assert state.exists()
        assert receipt.read_bytes() == stale
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)
    assert supervisor.finalize_from_state(state, stage="timeout")
    assert json.loads(receipt.read_text())["qualifies_task_4_5"] is False
    assert not state.exists()
    assert not supervisor.finalize_from_state(state, stage="timeout")


def test_shared_pending_verifier_intent_reconciles_bound_finalizer_and_blocks_reader(
    tmp_path: Path,
) -> None:
    receipt = tmp_path / "terminal.json"
    stale = b'{"schema_version":"3.0","qualifies_task_4_5":true}\n'
    receipt.write_bytes(stale)
    expected = terminal_state.terminal_identity(receipt)
    assert expected is not None
    state = tmp_path / "finalizer-state.json"
    supervisor._write_finalizer_state(
        state,
        receipt_path=receipt,
        expected_stale_sha256=expected.sha256,
        run_id="shared-run",
        mutation_head_sha="a" * 40,
    )
    lock_fd = os.open(
        terminal_state._terminal_lock_path(receipt), os.O_RDWR | os.O_CREAT, 0o600
    )
    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        assert not terminal_state.publish_unavailable_failure(
            receipt,
            stage="verifier-provenance",
            expected=expected,
            verifier_head_sha="b" * 40,
            deadline_monotonic=time.monotonic() + 0.05,
        )
        with pytest.raises(terminal_state.TerminalStateError, match="intent is pending"):
            terminal_state.read_authoritative_terminal(receipt)
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)
    assert supervisor.finalize_from_state(state, stage="systemd-stop-post")
    terminal = terminal_state.read_authoritative_terminal(receipt)
    assert terminal["provenance_state"] == "bound"
    assert terminal["run_id"] == "shared-run"
    assert terminal["mutation_head_sha"] == "a" * 40
    assert not state.exists()
    assert not terminal_state._terminal_intent_root_path(receipt).exists()


def test_shared_finalizer_and_authoritative_reader_complete_without_deadlock(
    tmp_path: Path,
) -> None:
    receipt = tmp_path / "terminal.json"
    stale = b'{"schema_version":"3.0","qualifies_task_4_5":true}\n'
    receipt.write_bytes(stale)
    expected = terminal_state.terminal_identity(receipt)
    assert expected is not None
    lock_fd = os.open(
        terminal_state._terminal_lock_path(receipt), os.O_RDWR | os.O_CREAT, 0o600
    )
    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finalizer_result: list[bool] = []
    reader_result: list[str] = []

    def finalize() -> None:
        finalizer_result.append(
            supervisor.finalize_receipt(
                receipt,
                expected_stale_sha256=expected.sha256,
                run_id="ordered-run",
                stage="timeout",
                possible_mutation=True,
                mutation_head_sha="a" * 40,
                deadline_monotonic=time.monotonic() + 0.1,
            )
        )

    def read() -> None:
        try:
            terminal_state.read_authoritative_terminal(
                receipt, deadline_monotonic=time.monotonic() + 0.3
            )
        except terminal_state.TerminalStateError as error:
            reader_result.append(str(error))

    finalizer = threading.Thread(target=finalize)
    reader = threading.Thread(target=read)
    finalizer.start()
    deadline = time.monotonic() + 1
    while not terminal_state._terminal_intent_root_path(receipt).exists() and time.monotonic() < deadline:
        time.sleep(0.005)
    reader.start()
    finalizer.join(1)
    reader.join(1)
    fcntl.flock(lock_fd, fcntl.LOCK_UN)
    os.close(lock_fd)
    assert not finalizer.is_alive() and not reader.is_alive()
    assert finalizer_result == [False]
    assert reader_result and "intent is pending" in reader_result[0]
    assert supervisor.finalize_receipt(
        receipt,
        expected_stale_sha256=expected.sha256,
        run_id="ordered-run",
        stage="timeout",
        possible_mutation=True,
        mutation_head_sha="a" * 40,
    )


def test_shared_finalizer_recovers_post_replace_fsync_retry_without_orphan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt = tmp_path / "terminal.json"
    stale = b'{"schema_version":"3.0","qualifies_task_4_5":true}\n'
    receipt.write_bytes(stale)
    state = tmp_path / "finalizer-state.json"
    supervisor._write_finalizer_state(
        state,
        receipt_path=receipt,
        expected_stale_sha256=hashlib.sha256(stale).hexdigest(),
        run_id="fsync-retry-run",
        mutation_head_sha="a" * 40,
    )
    real_fsync_directory = terminal_state._fsync_directory_fd
    failed = False

    def fail_after_replace(fd: int, path: Path, *, label: str) -> None:
        nonlocal failed
        if label == "terminal parent" and not failed:
            failed = True
            raise terminal_state.TerminalStateError("injected post-replace fsync failure")
        real_fsync_directory(fd, path, label=label)

    monkeypatch.setattr(terminal_state, "_fsync_directory_fd", fail_after_replace)
    assert not supervisor.finalize_from_state(state, stage="timeout")
    assert not state.exists()
    assert not terminal_state._terminal_intent_root_path(receipt).exists()
    monkeypatch.setattr(terminal_state, "_fsync_directory_fd", real_fsync_directory)
    assert not supervisor.finalize_from_state(state, stage="timeout")
    assert terminal_state.read_authoritative_terminal(receipt)["run_id"] == "fsync-retry-run"


def test_finalizer_consumes_state_when_newer_terminal_identity_is_proven(tmp_path: Path) -> None:
    receipt = tmp_path / "receipt.json"
    stale = b'{"schema_version":"3.0","qualifies_task_4_5":true}\n'
    receipt.write_bytes(stale)
    state = tmp_path / "state.json"
    supervisor._write_finalizer_state(
        state,
        receipt_path=receipt,
        expected_stale_sha256=hashlib.sha256(stale).hexdigest(),
        run_id="newer-run",
        mutation_head_sha="a" * 40,
    )
    newer = supervisor._canonical(
        {
            "schema_version": "3.0",
            "qualifies_task_4_5": False,
            "outcome": "failed",
            "provenance_state": "bound",
            "generated_at": "2026-07-15T00:00:00Z",
            "run_id": "newer-run",
            "mutation_head_sha": "b" * 40,
            "failure": {"stage": "newer", "mutation_state": "indeterminate"},
        }
    )
    receipt.write_bytes(newer)
    assert not supervisor.finalize_from_state(state, stage="timeout")
    assert receipt.read_bytes() == newer
    assert not state.exists()


def test_git_probe_process_group_is_killed_and_reaped_by_reserved_wall() -> None:
    started = time.monotonic()
    with pytest.raises(supervisor.SupervisorError, match="probe failed"):
        supervisor._run_capture_argv(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            wall=supervisor.HardWall.start(0.1),
            label="Git lineage blocking child",
        )
    assert time.monotonic() - started < 4


def test_stalled_mutation_uses_reserved_remainder_for_schema_valid_tombstone(tmp_path: Path) -> None:
    receipt = tmp_path / "terminal.json"
    stale = b'{"schema_version":"3.0","qualifies_task_4_5":true}\n'
    receipt.write_bytes(stale)
    state = tmp_path / "state.json"
    supervisor._write_finalizer_state(
        state,
        receipt_path=receipt,
        expected_stale_sha256=hashlib.sha256(stale).hexdigest(),
        run_id="stalled-run",
        mutation_head_sha="a" * 40,
    )
    main_wall = supervisor.HardWall.start(1.0)
    operation_wall = supervisor.HardWall(main_wall.started_monotonic, time.monotonic() + 0.1)
    command = {
        "command_id": "stalled-decompress",
        "kind": "decompress",
        "argv": [sys.executable, "-c", "import time; time.sleep(30)"],
        "artifact_associations": {"recovery_receipt": str(tmp_path / "missing-recovery.json")},
    }
    started = time.monotonic()
    with supervisor.AppendOnlyLedger(
        tmp_path / "stalled-ledger.jsonl", run_id="run", run_plan_id="plan", invocation_id="1" * 32
    ) as ledger:
        with pytest.raises(supervisor.HardWallExpired):
            supervisor.run_child(
                command,
                wall=operation_wall,
                ledger=ledger,
                mutation_head_sha="a" * 40,
                database="nhms",
                term_grace=0.02,
            )
    assert supervisor.finalize_from_state(
        state,
        stage="stalled-child",
        deadline_monotonic=main_wall.deadline_monotonic,
    )
    assert time.monotonic() - started < 1.0
    tombstone = json.loads(receipt.read_text())
    assert tombstone["qualifies_task_4_5"] is False
    assert tombstone["failure"]["mutation_state"] == "indeterminate"
    schema = json.loads(
        (Path(__file__).parents[1] / "schemas/timeseries_compression_live_evidence.schema.json").read_text()
    )
    jsonschema.Draft202012Validator(schema).validate(tombstone)


def test_replay_unit_requires_external_run_plan_digest_pin() -> None:
    root = Path(__file__).parents[1]
    env_example = (root / "infra/env/node27-timeseries-compression-replay.example").read_text()
    unit = (root / "infra/systemd/nhms-node27-timeseries-compression-replay.service").read_text()
    assert "NODE27_COMPRESSION_RUN_PLAN_SHA256=REPLACE_WITH_64_HEX_DIGEST" in env_example
    assert "EnvironmentFile=/home/nwm/NWM/infra/env/node27-timeseries-compression-replay.env" in unit


def test_catalog_boundary_and_over_limit_tombstone_input() -> None:
    rows = [{"ordinal": index} for index in range(3)]
    assert supervisor.bounded_rows(rows, max_rows=3, max_candidates=3) == rows
    with pytest.raises(supervisor.SupervisorError, match="ceiling"):
        supervisor.bounded_rows(rows, max_rows=2)
    with pytest.raises(supervisor.SupervisorError, match="byte ceiling"):
        supervisor.bounded_rows(rows, max_bytes=4)
