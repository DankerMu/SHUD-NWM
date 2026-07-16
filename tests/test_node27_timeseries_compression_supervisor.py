"""Focused invariant tests for the issue #1069 controlled producer."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from packages.common import compression_terminal_state as terminal_state
from packages.common import evidence_io
from packages.common.evidence_io import (
    BoundedEvidenceError,
    assert_output_disjoint_from_closure,
    resolve_artifact_closure,
)
from scripts import node27_timeseries_compression_live_evidence as evidence
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
        "reviewed_remote_ref": supervisor.EXPECTED_REVIEWED_REMOTE_REF,
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


# --- SC-F1 (producer side): the run plan that authorizes the mutating lane must carry
# exactly the round-3 operator attestation triple and nothing else, so a future drift on
# the producer that emits the run plan is caught before spawn (mirror of the verifier
# gate at live_evidence.py:1009-1016).

_EXPECTED_OPERATOR_ATTESTATION = {
    "sole_db_user_during_window": True,
    "database_audit_proof": False,
    "trust_limit": "discrete observations; no absolute direct-SQL bypass proof",
}


def test_run_plan_binds_exact_operator_attestation_triple() -> None:
    plan = _plan()
    validated = supervisor.validate_run_plan(plan, inherited_env={})
    assert validated["operator_attestation"] == _EXPECTED_OPERATOR_ATTESTATION
    # Identity-level booleans, so a truthy `1`/`0` producer drift cannot pass.
    assert validated["operator_attestation"]["sole_db_user_during_window"] is True
    assert validated["operator_attestation"]["database_audit_proof"] is False


_OPERATOR_ATTESTATION_DRIFTS = {
    "sole_db_user_denied": lambda plan: plan["operator_attestation"].__setitem__("sole_db_user_during_window", False),
    "audit_proof_promoted": lambda plan: plan["operator_attestation"].__setitem__("database_audit_proof", True),
    "trust_limit_weakened": lambda plan: plan["operator_attestation"].__setitem__(
        "trust_limit", "absolute direct-SQL bypass proof obtained"
    ),
    "sole_db_user_absent": lambda plan: plan["operator_attestation"].pop("sole_db_user_during_window"),
    "audit_proof_absent": lambda plan: plan["operator_attestation"].pop("database_audit_proof"),
    "trust_limit_absent": lambda plan: plan["operator_attestation"].pop("trust_limit"),
    "extra_key": lambda plan: plan["operator_attestation"].__setitem__("database_audit_proof_extra", True),
    "not_a_mapping": lambda plan: plan.__setitem__("operator_attestation", "sole-user"),
}


@pytest.mark.parametrize("drift", sorted(_OPERATOR_ATTESTATION_DRIFTS))
def test_run_plan_rejects_operator_attestation_drift(drift: str) -> None:
    plan = _plan()
    _OPERATOR_ATTESTATION_DRIFTS[drift](plan)
    with pytest.raises(supervisor.SupervisorError, match="sole-DB-user attestation"):
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
                "binary_realpath": "/usr/share/postgresql-common/pg_wrapper",
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


STALE_RECEIPT = b'{"schema_version":"3.0","qualifies_task_4_5":true}\n'


class _MainHarness:
    """Drive ``supervisor.main()`` start-gate wiring with harmless local inputs.

    Nothing here touches a database, systemd, or any live receipt: the child
    state machine, the Git lineage probe, and the journal cursor are the only
    surfaces stubbed, and every path lives under ``tmp_path``.
    """

    def __init__(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, live_journal: bool = False
    ) -> None:
        for key in supervisor.FORBIDDEN_INHERITED_ENV:
            monkeypatch.delenv(key, raising=False)
        self.plan = _plan()
        self.plan_raw = _canonical_bytes(self.plan)
        self.plan_path = tmp_path / "run-plan.json"
        self.plan_path.write_bytes(self.plan_raw)
        self.receipt = tmp_path / "terminal-evidence.json"
        self.receipt.write_bytes(STALE_RECEIPT)
        self.state_path = tmp_path / "finalizer-state.json"
        self.ledger_path = tmp_path / "supervisor-ledger.jsonl"
        self.executed: list[dict[str, Any]] = []
        self.armed: list[dict[str, Any]] = []
        self.ledgers: list[supervisor.AppendOnlyLedger] = []
        self.plan_opens: list[int] = []
        monkeypatch.setenv("INVOCATION_ID", "1" * 32)
        monkeypatch.setenv("NODE27_COMPRESSION_RUN_PLAN_SHA256", hashlib.sha256(self.plan_raw).hexdigest())
        monkeypatch.setenv(
            "NODE27_COMPRESSION_EXPECTED_STALE_SHA256", hashlib.sha256(STALE_RECEIPT).hexdigest()
        )
        monkeypatch.setattr(supervisor, "_verify_checkout_lineage", lambda plan, *, wall: None)
        # When live_journal is set the caller has installed a real journalctl
        # binary stub under SUPERVISOR_BIN_DIR, so main() drives the REAL
        # start-cursor probe (_run_capture_argv) through it; otherwise the probe
        # is stubbed with a canned cursor. execute_producer_state_machine stays
        # stubbed either way, so nothing mutates.
        if not live_journal:
            monkeypatch.setattr(supervisor, "_run_capture_argv", self._journal_cursor)
        monkeypatch.setattr(supervisor, "execute_producer_state_machine", self._state_machine)

    def _journal_cursor(self, argv: list[str], **_: Any) -> bytes:
        assert argv[0] == "/usr/bin/journalctl"
        return b"-- cursor: s=cursor;i=1\n"

    def _state_machine(self, plan: dict[str, Any], **kwargs: Any) -> None:
        self.executed.append(plan)
        self.ledgers.append(kwargs["ledger"])
        # The finalizer must already be armed before any child can mutate.
        self.armed.append(json.loads(self.state_path.read_text()))

    def argv(self) -> list[str]:
        return [
            "--enforce",
            "--run-plan-path",
            str(self.plan_path),
            "--ledger-path",
            str(self.ledger_path),
            "--receipt-path",
            str(self.receipt),
            "--finalizer-state-path",
            str(self.state_path),
            "--wall-seconds",
            "30",
        ]

    def assert_failed_before_mutation(self) -> None:
        assert self.executed == []
        assert not self.state_path.exists()
        assert self.receipt.read_bytes() == STALE_RECEIPT


def _canonical_bytes(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def test_main_start_gate_passes_and_arms_finalizer_for_reviewed_replay_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _MainHarness(tmp_path, monkeypatch)

    assert supervisor.main(harness.argv()) == 0

    assert len(harness.executed) == 1
    assert harness.executed[0]["run_plan_id"] == harness.plan["run_plan_id"]
    armed = harness.armed[0]
    assert armed["receipt_path"] == str(harness.receipt)
    assert armed["expected_stale_sha256"] == hashlib.sha256(STALE_RECEIPT).hexdigest()
    assert armed["mutation_head_sha"] == harness.plan["mutation_head_sha"]
    assert re.fullmatch(supervisor.RUN_ID_PATTERN, armed["run_id"]) is not None
    # A successful run disarms exactly its own state and leaves no residue.
    assert not harness.state_path.exists()
    # The ledger the state machine writes through is bound to the live systemd
    # invocation and to the same run_id the finalizer was armed with.
    ledger = harness.ledgers[0]
    assert ledger.invocation_id == "1" * 32
    assert ledger.run_id == armed["run_id"]
    assert ledger.run_plan_id == harness.plan["run_plan_id"]


def test_main_run_plan_digest_pin_binds_the_bytes_that_are_parsed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An inode ABA must not slip an unpinned plan body past the digest pin."""

    harness = _MainHarness(tmp_path, monkeypatch)
    attacker = _plan()
    attacker["captures"][0]["capture_id"] = "attacker-substituted-capture"
    attacker["run_plan_id"] = supervisor.run_plan_id(attacker)
    attacker_raw = _canonical_bytes(attacker)
    assert attacker_raw != harness.plan_raw
    # A self-consistent swap survives plan validation on its own merits, so
    # only same-read digest binding can reject it.
    supervisor.validate_run_plan(attacker, inherited_env={})
    # A hardlink keeps the reviewed inode alive so the swap can be undone with
    # the original device/inode/size/sha256 intact -- a real ABA, not a
    # detectable one-way replacement.
    reviewed_inode = tmp_path / "reviewed-inode.json"
    os.link(harness.plan_path, reviewed_inode)
    attacker_path = tmp_path / "attacker-plan.json"
    attacker_path.write_bytes(attacker_raw)
    real_open = evidence_io.open_file_no_follow

    def swapping_open(path: Any, *args: Any, **kwargs: Any) -> int:
        if Path(path) == harness.plan_path:
            index = len(harness.plan_opens)
            harness.plan_opens.append(index)
            if index == 1:
                os.replace(attacker_path, harness.plan_path)
            elif index == 2:
                os.replace(reviewed_inode, harness.plan_path)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(evidence_io, "open_file_no_follow", swapping_open)

    assert supervisor.main(harness.argv()) == 0

    # The swapped-in body must never become the executed body.
    executed_captures = {capture["capture_id"] for capture in harness.executed[0]["captures"]}
    assert "attacker-substituted-capture" not in executed_captures
    assert harness.executed[0]["run_plan_id"] == harness.plan["run_plan_id"]
    # ...because exactly one descriptor read owns the plan: the bytes that are
    # digested are the bytes that are parsed.
    assert harness.plan_opens == [0]


@pytest.mark.parametrize(
    ("scenario", "match"),
    [
        ("wrong_plan_pin", "run-plan SHA256 pin differs"),
        ("missing_plan_pin", "run-plan SHA256 pin is required"),
        ("wrong_stale_digest", "expected stale receipt identity/digest is required"),
        ("missing_stale_digest", "expected stale receipt identity/digest is required"),
        ("missing_invocation_id", "INVOCATION_ID is required"),
        ("missing_enforce", "requires literal --enforce"),
    ],
)
def test_main_start_gate_fails_closed_before_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scenario: str, match: str
) -> None:
    harness = _MainHarness(tmp_path, monkeypatch)
    argv = harness.argv()
    if scenario == "wrong_plan_pin":
        monkeypatch.setenv("NODE27_COMPRESSION_RUN_PLAN_SHA256", "0" * 64)
    elif scenario == "missing_plan_pin":
        monkeypatch.delenv("NODE27_COMPRESSION_RUN_PLAN_SHA256")
    elif scenario == "wrong_stale_digest":
        monkeypatch.setenv("NODE27_COMPRESSION_EXPECTED_STALE_SHA256", "0" * 64)
    elif scenario == "missing_stale_digest":
        monkeypatch.delenv("NODE27_COMPRESSION_EXPECTED_STALE_SHA256")
    elif scenario == "missing_invocation_id":
        monkeypatch.delenv("INVOCATION_ID")
    else:
        argv = [item for item in argv if item != "--enforce"]

    with pytest.raises(supervisor.SupervisorError, match=match):
        supervisor.main(argv)

    harness.assert_failed_before_mutation()


@pytest.mark.parametrize("missing", ["run_plan", "receipt"])
def test_main_missing_replay_inputs_fail_before_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    harness = _MainHarness(tmp_path, monkeypatch)
    if missing == "run_plan":
        harness.plan_path.unlink()
        expected: type[Exception] = BoundedEvidenceError
    else:
        harness.receipt.unlink()
        expected = supervisor.SupervisorError

    with pytest.raises(expected):
        supervisor.main(harness.argv())

    assert harness.executed == []
    assert not harness.state_path.exists()


def test_main_binds_the_real_journal_start_cursor_through_the_probe(
    probe_bin, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # F2 positive: exercise the REAL start-cursor journalctl probe (main() start
    # gate) through the binary stub -- not the canned-cursor shortcut -- so the
    # `len(cursor_lines) != 1` bind path is actually driven.
    probe_bin("journalctl", _journalctl_responses())
    harness = _MainHarness(tmp_path, monkeypatch, live_journal=True)
    assert supervisor.main(harness.argv()) == 0
    assert len(harness.executed) == 1
    assert not harness.state_path.exists()


@pytest.mark.parametrize("boundary", ["missing", "duplicate"])
def test_main_fails_closed_when_the_journal_start_cursor_cannot_bind(
    probe_bin, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    # F2 negative: the start-cursor probe must bind EXACTLY one cursor. A probe
    # that returns no cursor (or two) fails the bind guard before any child runs.
    if boundary == "missing":
        responses = _journalctl_responses(boundary_cursor=None)
    else:
        responses = _journalctl_responses()
        responses[-1]["stdout"] += "-- cursor: s=extra;i=2;b=x;m=2;t=2;x=2\n"
    probe_bin("journalctl", responses)
    harness = _MainHarness(tmp_path, monkeypatch, live_journal=True)
    with pytest.raises(supervisor.SupervisorError, match="cannot bind the journal start cursor"):
        supervisor.main(harness.argv())
    assert harness.executed == []


def test_finalizer_state_run_id_cannot_escape_its_marker_path(tmp_path: Path) -> None:
    """A tampered run_id must fail closed, not crash ExecStopPost or escape."""

    receipt = tmp_path / "receipt.json"
    stale = b'{"outcome":"stale"}\n'
    receipt.write_bytes(stale)
    state = tmp_path / "state.json"
    supervisor._write_finalizer_state(
        state,
        receipt_path=receipt,
        expected_stale_sha256=hashlib.sha256(stale).hexdigest(),
        run_id="legit-run",
        mutation_head_sha="a" * 40,
    )
    tampered = json.loads(state.read_text())
    tampered["run_id"] = "../../escape"
    _write_canonical(state, tampered)

    # Returns False instead of raising an uncaught ValueError out of the unit's
    # ExecStopPost, and writes no marker anywhere.
    assert supervisor.finalize_from_state(state, stage="systemd-stop-post") is False
    assert receipt.read_bytes() == stale
    assert not list(tmp_path.parent.glob("*escape*"))
    assert sorted(item.name for item in tmp_path.iterdir()) == ["receipt.json", "state.json"]


def _write_canonical(path: Path, value: Any) -> None:
    path.write_bytes(
        (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode()
    )


def _leave_pending_intent(receipt: Path) -> Any:
    """Leave one durable pending failure intent by holding the publish lock."""

    expected = terminal_state.terminal_identity(receipt)
    assert expected is not None
    lock_fd = os.open(terminal_state._terminal_lock_path(receipt), os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        assert not terminal_state.publish_unavailable_failure(
            receipt,
            stage="verifier-provenance",
            expected=expected,
            verifier_head_sha="b" * 40,
            deadline_monotonic=time.monotonic() + 0.05,
        )
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)
    assert terminal_state._terminal_intent_root_path(receipt).is_dir()
    return expected


def test_gate_lock_inode_anchors_bindings_while_state_document_is_replaced(
    tmp_path: Path,
) -> None:
    """Only the contentless lock inode may be bound; the state document is not.

    The sidecar pins the lock's `(dev, ino)`, which must survive every
    transition, while the state document is a fresh inode after each atomic
    rename -- so binding to it would be incoherent by construction.
    """

    receipt = tmp_path / "terminal.json"
    receipt.write_bytes(STALE_RECEIPT)
    expected = _leave_pending_intent(receipt)
    lock_path = terminal_state._terminal_intent_gate_path(receipt)
    state_path = terminal_state._terminal_intent_state_path(receipt)

    # The lock object is contentless; the state document carries the state.
    assert lock_path.read_bytes() == b""
    sidecar = json.loads(
        (terminal_state._terminal_intent_root_path(receipt) / "identity.json").read_text()
    )
    assert sidecar["gate"] == {
        "device": lock_path.stat().st_dev,
        "inode": lock_path.stat().st_ino,
    }
    lock_inode = lock_path.stat().st_ino
    pending_state_inode = state_path.stat().st_ino
    assert json.loads(state_path.read_text())["state"] == "pending"
    assert oct(state_path.stat().st_mode)[-3:] == "600"

    assert supervisor.finalize_receipt(
        receipt,
        expected_stale_sha256=expected.sha256,
        run_id="inode-anchor-run",
        stage="timeout",
        possible_mutation=True,
        mutation_head_sha="a" * 40,
    )
    # The anchor the sidecar bound is stable across the whole lifecycle...
    assert lock_path.stat().st_ino == lock_inode
    assert lock_path.read_bytes() == b""
    # ...while the state document is a different, deliberately unstable inode.
    assert json.loads(state_path.read_text())["state"] == "idle"
    assert state_path.stat().st_ino != pending_state_inode
    assert state_path.stat().st_ino != lock_inode
    # No temp file from the rename-replace survives.
    assert not list(tmp_path.glob("*.tmp"))


def test_gate_lock_rejects_a_hardlinked_lock_object(tmp_path: Path) -> None:
    """Splitting out the state document must not relax the lock's nlink pin."""

    receipt = tmp_path / "terminal.json"
    receipt.write_bytes(STALE_RECEIPT)
    other = tmp_path / "evidence-input.json"
    other.write_bytes(b'{"input":true}\n')
    other.chmod(0o600)
    os.link(other, terminal_state._terminal_intent_gate_path(receipt))

    with pytest.raises(terminal_state.TerminalStateError, match="gate identity/mode differs"):
        terminal_state.read_authoritative_terminal(receipt)
    assert other.read_bytes() == b'{"input":true}\n'


@pytest.mark.parametrize("alias", ["hardlink", "symlink"])
def test_gate_state_document_alias_fails_closed_without_touching_the_input(
    tmp_path: Path, alias: str
) -> None:
    """A state document aliasing an input is rejected, and rename never writes through it."""

    receipt = tmp_path / "terminal.json"
    receipt.write_bytes(STALE_RECEIPT)
    other = tmp_path / "evidence-input.json"
    other.write_bytes(b'{"input":true}\n')
    other.chmod(0o600)
    state_path = terminal_state._terminal_intent_state_path(receipt)
    if alias == "hardlink":
        os.link(other, state_path)
    else:
        state_path.symlink_to(other)

    with pytest.raises(terminal_state.TerminalStateError, match="state file identity/mode differs"):
        terminal_state.read_authoritative_terminal(receipt)
    assert other.read_bytes() == b'{"input":true}\n'
    assert not terminal_state.publish_bound_failure(
        receipt,
        stage="timeout",
        expected=terminal_state.terminal_identity(receipt),
        run_id="alias-run",
        mutation_head_sha="a" * 40,
        possible_mutation=True,
    )
    assert other.read_bytes() == b'{"input":true}\n'
    assert receipt.read_bytes() == STALE_RECEIPT


def _clear_gate_state(receipt: Path) -> None:
    """Reproduce the durable prefix of a SIGKILL before the gate state landed.

    The gate state document is replaced by rename, so the only prefixes a crash
    can leave are the previous document and the new one.  Removing it is the
    "no state document was ever written" prefix of a first-ever transition.
    """

    state_path = terminal_state._terminal_intent_state_path(receipt)
    assert state_path.is_file()
    state_path.unlink()


def _write_idle_gate_state(receipt: Path) -> None:
    """The other idle prefix: a canonical idle document from a prior lifecycle."""

    _write_canonical(
        terminal_state._terminal_intent_state_path(receipt),
        {"schema_version": terminal_state.INTENT_STATE_SCHEMA_VERSION, "state": "idle"},
    )
    terminal_state._terminal_intent_state_path(receipt).chmod(0o600)


def _apply_idle_gate(receipt: Path, gate: str) -> None:
    if gate == "absent":
        _clear_gate_state(receipt)
    else:
        _write_idle_gate_state(receipt)


def _apply_create_prefix(receipt: Path, files: int) -> None:
    """Reproduce a SIGKILL between os.mkdir and the pending gate write."""

    root = terminal_state._terminal_intent_root_path(receipt)
    assert {entry.name for entry in root.iterdir()} == {"intent.json", "identity.json"}
    (root / "identity.json").unlink()
    if files == 0:
        (root / "intent.json").unlink()
    assert len(list(root.iterdir())) == files


@pytest.mark.parametrize("gate", ["absent", "canonical-idle"])
@pytest.mark.parametrize("files", [0, 1])
def test_idle_gate_create_crash_prefix_is_collected_not_bricked(
    tmp_path: Path, gate: str, files: int
) -> None:
    """A KILL inside intent creation must not brick the lane forever.

    An idle gate is durable proof that no intent reached its commit point, so a
    strict create prefix is provable garbage: it is collected, the terminal
    classifies, and the next publisher succeeds.
    """

    receipt = tmp_path / "terminal.json"
    receipt.write_bytes(STALE_RECEIPT)
    expected = _leave_pending_intent(receipt)
    _apply_create_prefix(receipt, files)
    _apply_idle_gate(receipt, gate)
    root = terminal_state._terminal_intent_root_path(receipt)
    assert root.is_dir()

    # The loader classifies rather than bricking: it reaches the terminal, which
    # only then fails on these fixture bytes' own deliberate non-canonicality.
    # The remnant is collected on the way.
    with pytest.raises(terminal_state.TerminalStateError, match="not canonical JSON"):
        terminal_state.read_authoritative_terminal(receipt)
    assert not root.exists()
    # Recovery is idempotent across a second independent load.
    with pytest.raises(terminal_state.TerminalStateError, match="not canonical JSON"):
        terminal_state.read_authoritative_terminal(receipt)
    assert not root.exists()

    # The lane still works: a later tombstone publishes exactly once.
    assert supervisor.finalize_receipt(
        receipt,
        expected_stale_sha256=expected.sha256,
        run_id="create-crash-run",
        stage="timeout",
        possible_mutation=True,
        mutation_head_sha="a" * 40,
    )
    terminal = terminal_state.read_authoritative_terminal(receipt)
    assert terminal["provenance_state"] == "bound"
    assert terminal["run_id"] == "create-crash-run"
    assert not root.exists()


@pytest.mark.parametrize("gate", ["absent", "canonical-idle"])
@pytest.mark.parametrize("files", [0, 1])
def test_idle_gate_create_crash_prefix_publishes_bound_failure_directly(
    tmp_path: Path, gate: str, files: int
) -> None:
    """publish_bound_failure must not silently return False on a create prefix."""

    receipt = tmp_path / "terminal.json"
    receipt.write_bytes(STALE_RECEIPT)
    expected = _leave_pending_intent(receipt)
    _apply_create_prefix(receipt, files)
    _apply_idle_gate(receipt, gate)

    assert terminal_state.publish_bound_failure(
        receipt,
        stage="timeout",
        expected=expected,
        run_id="create-crash-direct",
        mutation_head_sha="a" * 40,
        possible_mutation=True,
    )
    terminal = terminal_state.read_authoritative_terminal(receipt)
    assert terminal["run_id"] == "create-crash-direct"
    assert not terminal_state._terminal_intent_root_path(receipt).exists()


@pytest.mark.parametrize("gate", ["absent", "canonical-idle"])
def test_idle_gate_fully_bound_intent_is_committed_not_collected(
    tmp_path: Path, gate: str
) -> None:
    """A complete directory is a durable decision: finish its commit, never drop it."""

    receipt = tmp_path / "terminal.json"
    receipt.write_bytes(STALE_RECEIPT)
    expected = _leave_pending_intent(receipt)
    _apply_idle_gate(receipt, gate)
    root = terminal_state._terminal_intent_root_path(receipt)

    # The intent survives and keeps invalidating the stale terminal.
    with pytest.raises(terminal_state.TerminalStateError, match="intent is pending"):
        terminal_state.read_authoritative_terminal(receipt)
    assert root.is_dir()
    with terminal_state._locked_intent_gate(receipt, label="commit audit") as (_, parent_fd):
        committed = terminal_state._read_gate_state(parent_fd, receipt)
    assert committed["state"] == "pending"
    assert committed["intent_directory"] == root.name

    assert supervisor.finalize_receipt(
        receipt,
        expected_stale_sha256=expected.sha256,
        run_id="create-commit-run",
        stage="timeout",
        possible_mutation=True,
        mutation_head_sha="a" * 40,
    )
    terminal = terminal_state.read_authoritative_terminal(receipt)
    assert terminal["provenance_state"] == "bound"
    assert terminal["run_id"] == "create-commit-run"
    assert not root.exists()


@pytest.mark.parametrize(
    "tamper", ["gate_inode", "intent_identity", "payload_digest", "output_path", "intent_payload"]
)
def test_idle_gate_unbound_complete_intent_stays_fail_closed(tmp_path: Path, tamper: str) -> None:
    """A complete directory that does not cross-bind is neither committed nor dropped.

    Each case mutates exactly one artifact, so the surviving artifacts refute it.
    This does not prove resistance to a self-consistent rewrite of the whole
    directory; see the consistent-pair test below for the true behaviour.
    """

    receipt = tmp_path / "terminal.json"
    receipt.write_bytes(STALE_RECEIPT)
    _leave_pending_intent(receipt)
    root = terminal_state._terminal_intent_root_path(receipt)
    sidecar_path = root / "identity.json"
    sidecar = json.loads(sidecar_path.read_text())
    if tamper == "intent_payload":
        intent_path = root / "intent.json"
        intent = json.loads(intent_path.read_text())
        intent["payload"]["failure"]["stage"] = "attacker-stage"
        _write_canonical(intent_path, intent)
    else:
        if tamper == "gate_inode":
            sidecar["gate"]["inode"] += 1
        elif tamper == "intent_identity":
            sidecar["intent"]["sha256"] = "0" * 64
        elif tamper == "payload_digest":
            sidecar["failure_payload_sha256"] = "0" * 64
        else:
            sidecar["output_path"] = str(tmp_path / "other-terminal.json")
        _write_canonical(sidecar_path, sidecar)
    _clear_gate_state(receipt)

    with pytest.raises(terminal_state.TerminalStateError, match="without durable gate state"):
        terminal_state.read_authoritative_terminal(receipt)
    # No commit and no publication: the stale terminal is untouched, the
    # directory is not collected, and the rejection is stable, not a one-shot.
    assert receipt.read_bytes() == STALE_RECEIPT
    with pytest.raises(terminal_state.TerminalStateError, match="without durable gate state"):
        terminal_state.read_authoritative_terminal(receipt)
    assert root.is_dir()


def test_idle_gate_consistent_pair_rewrite_is_accepted_and_non_escalating(
    tmp_path: Path,
) -> None:
    """The sidecar pin proves durable self-consistency, not authorship.

    Every input to the commit is a mode-0600 file inside a mode-0700 directory
    owned by the same uid as the terminal, so an actor able to rewrite both
    artifacts consistently can already replace the terminal directly.  This
    asserts the property the code actually has, so no later reader can mistake
    the single-artifact tamper cases above for forgery resistance.
    """

    receipt = tmp_path / "terminal.json"
    receipt.write_bytes(STALE_RECEIPT)
    _leave_pending_intent(receipt)
    root = terminal_state._terminal_intent_root_path(receipt)
    intent_path = root / "intent.json"
    sidecar_path = root / "identity.json"

    intent = json.loads(intent_path.read_text())
    intent["payload"]["failure"]["stage"] = "attacker-stage"
    intent["payload"]["failure_context"]["reason_category"] = "attacker-stage"
    _write_canonical(intent_path, intent)
    intent_path.chmod(0o600)
    sidecar = json.loads(sidecar_path.read_text())
    sidecar["intent"] = {
        "device": intent_path.stat().st_dev,
        "inode": intent_path.stat().st_ino,
        "bytes": intent_path.stat().st_size,
        "sha256": hashlib.sha256(intent_path.read_bytes()).hexdigest(),
    }
    sidecar["failure_payload_sha256"] = hashlib.sha256(
        _canonical_bytes(intent["payload"])
    ).hexdigest()
    _write_canonical(sidecar_path, sidecar)
    _clear_gate_state(receipt)

    with pytest.raises(terminal_state.TerminalStateError, match="intent is pending"):
        terminal_state.read_authoritative_terminal(receipt)
    with terminal_state._locked_intent_gate(receipt, label="consistent pair audit") as (_, parent_fd):
        committed = terminal_state._read_gate_state(parent_fd, receipt)
    assert committed["state"] == "pending"
    assert committed["failure_payload_sha256"] == sidecar["failure_payload_sha256"]
    # The rewritten payload is still only a failure tombstone: the same-uid
    # actor gains no qualifying PASS and no path outside this directory.
    assert terminal_state._validate_failure_payload(intent["payload"])["qualifies_task_4_5"] is False
    assert sorted(item.name for item in tmp_path.iterdir()) == sorted(
        [
            root.name,
            terminal_state._terminal_intent_gate_path(receipt).name,
            terminal_state._terminal_intent_state_path(receipt).name,
            terminal_state._terminal_lock_path(receipt).name,
            receipt.name,
        ]
    )


def test_consuming_crash_leaves_no_blocking_or_permanent_consumed_residue(
    tmp_path: Path,
) -> None:
    """A KILL before the committed_cleanup state lands must stay recoverable.

    The gate cannot tear, so this prefix is durably `consuming` with the intent
    already renamed aside and the terminal already published.
    """

    receipt = tmp_path / "terminal.json"
    receipt.write_bytes(STALE_RECEIPT)
    expected = _leave_pending_intent(receipt)
    root = terminal_state._terminal_intent_root_path(receipt)
    original_write = terminal_state._write_gate_state
    crashed: list[str] = []
    consuming_durable_before_rename: list[bool] = []

    def crashing_write(parent_fd: int, path: Path, state: Any) -> None:
        # Simulate SIGKILL before the committed_cleanup rename lands.
        if state.get("state") == "committed_cleanup":
            crashed.append("committed_cleanup")
            raise KeyboardInterrupt("simulated SIGKILL before the gate rename")
        original_write(parent_fd, path, state)
        if state.get("state") == "consuming":
            # The consuming state must already be durable while the directory is
            # still active.  A rename that outran its gate state would strand the
            # directory behind a gate that never recorded the transition.
            state_path = terminal_state._terminal_intent_state_path(receipt)
            consuming_durable_before_rename.append(
                root.is_dir() and json.loads(state_path.read_text())["state"] == "consuming"
            )

    terminal_state._write_gate_state = crashing_write
    try:
        with pytest.raises(KeyboardInterrupt):
            supervisor.finalize_receipt(
                receipt,
                expected_stale_sha256=expected.sha256,
                run_id="consuming-crash-run",
                stage="timeout",
                possible_mutation=True,
                mutation_head_sha="a" * 40,
            )
    finally:
        terminal_state._write_gate_state = original_write
    assert crashed == ["committed_cleanup"]
    assert consuming_durable_before_rename == [True]
    # The terminal is published and the active intent was renamed aside.
    assert not root.exists()
    residue = sorted(tmp_path.glob(f"{root.name}.consumed-*"))
    assert len(residue) == 1
    with terminal_state._locked_intent_gate(receipt, label="consuming audit") as (_, parent_fd):
        assert terminal_state._read_gate_state(parent_fd, receipt)["state"] == "consuming"

    # The loader finishes the state machine (no deadlock) and drops the residue.
    terminal = terminal_state.read_authoritative_terminal(receipt)
    assert terminal["provenance_state"] == "bound"
    assert terminal["run_id"] == "consuming-crash-run"
    assert sorted(tmp_path.glob(f"{root.name}.consumed-*")) == []
    assert terminal_state.read_authoritative_terminal(receipt) == terminal


def test_catalog_boundary_and_over_limit_tombstone_input() -> None:
    rows = [{"ordinal": index} for index in range(3)]
    assert supervisor.bounded_rows(rows, max_rows=3, max_candidates=3) == rows
    with pytest.raises(supervisor.SupervisorError, match="ceiling"):
        supervisor.bounded_rows(rows, max_rows=2)
    with pytest.raises(supervisor.SupervisorError, match="byte ceiling"):
        supervisor.bounded_rows(rows, max_bytes=4)


# ---------------------------------------------------------------------------
# Binary-level stub harness for the supervisor-owned probes.
#
# The producers (`capture_checkpoint`, `resolve_container_pg_restore_identity`,
# `_verify_checkout_lineage`) pin absolute argv[0] paths under `SUPERVISOR_BIN_DIR`
# and never resolve through $PATH, so a callable substitution is the only way the
# suite could reach them -- which is exactly the seam that let I-F1/I-F2 ship
# green.  These tests instead repoint `SUPERVISOR_BIN_DIR` at a directory of stub
# executables that reproduce the *measured* node-27 contracts for
# systemctl/journalctl/psql/docker/git, so the real producer code executes end to
# end offline.  Every stub encodes an observed external behaviour, not an assumed
# one (see the deviation ledger in the task report for the residue).
# ---------------------------------------------------------------------------

PROBE_INVOCATION_ID = "1" * 32
BOUNDARY_CURSOR = "s=stub;i=00000abc;b=stub;m=1;t=1;x=1"

_STUB_TEMPLATE = """#!{python}
import json
import os
import sys

_here = os.path.dirname(os.path.abspath(__file__))
_name = os.path.basename(__file__)
with open(os.path.join(_here, _name + ".responses.json"), encoding="utf-8") as _fh:
    _responses = json.load(_fh)
_argv = " ".join(sys.argv[1:])
for _response in _responses:
    _require_env = _response.get("require_env")
    if _require_env is not None:
        if os.environ.get(_require_env):
            continue
        sys.stderr.write(_response.get("stderr", ""))
        sys.stdout.write(_response.get("stdout", ""))
        sys.exit(_response.get("exit", 1))
    if all(_token in _argv for _token in _response["match"]):
        sys.stdout.write(_response.get("stdout", "").replace("__PPID__", str(os.getppid())))
        sys.stderr.write(_response.get("stderr", ""))
        sys.exit(_response.get("exit", 0))
sys.stderr.write("no stub response for argv: " + _argv + "\\n")
sys.exit(97)
"""


def _write_stub(bindir: Path, name: str, responses: list[dict[str, Any]]) -> None:
    script = bindir / name
    script.write_text(_STUB_TEMPLATE.replace("{python}", sys.executable), encoding="utf-8")
    script.chmod(0o755)
    (bindir / f"{name}.responses.json").write_text(json.dumps(responses), encoding="utf-8")


def _d3_catalog() -> dict[str, Any]:
    fields = (
        "hypertable_schema",
        "hypertable_name",
        "attname",
        "segmentby_column_index",
        "orderby_column_index",
        "orderby_asc",
        "orderby_nullsfirst",
    )
    rows = [
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
    return {
        "hypertables": {"hydro.river_timeseries": True, "met.forcing_station_timeseries": True},
        "compression_settings": [dict(zip(fields, row, strict=True)) for row in rows],
        "policy_jobs": [],
    }


def _psql_responses(*, activity: Any = None, locks: Any = None, catalog: Any = None) -> list[dict[str, Any]]:
    activity = {"sessions": []} if activity is None else activity
    locks = {"conflicts": []} if locks is None else locks
    catalog = _d3_catalog() if catalog is None else catalog
    return [
        {"match": ["pg_stat_activity"], "stdout": json.dumps(activity) + "\n"},
        {"match": ["pg_locks"], "stdout": json.dumps(locks) + "\n"},
        {"match": ["timescaledb_information.hypertables"], "stdout": json.dumps(catalog) + "\n"},
    ]


def _systemctl_responses(invocation_id: str = PROBE_INVOCATION_ID, *, require_xdg: bool = True) -> list[dict[str, Any]]:
    replay = "".join(
        line + "\n"
        for line in [
            "FragmentPath=/home/nwm/.config/systemd/user/nhms-node27-timeseries-compression-replay.service",
            "ActiveState=activating",
            "SubState=start",
            "MainPID=__PPID__",
            f"InvocationID={invocation_id}",
            "ExecMainStartTimestamp=Thu 2026-07-16 00:00:00 UTC",
            "ExecMainStartTimestampMonotonic=999",
        ]
    )
    recurring = "".join(
        line + "\n"
        for line in [
            "FragmentPath=/home/nwm/.config/systemd/user/nhms-node27-timeseries-compression.service",
            "ActiveState=inactive",
            "SubState=dead",
            "MainPID=0",
            "InvocationID=",
            # MEASURED node-27 contract (#1069 gap G6): systemd renders the
            # never-started recurring unit's unset start timestamp as literal
            # "n/a", not empty.
            f"ExecMainStartTimestamp={supervisor.SYSTEMD_UNSET_TIMESTAMP}",
            "ExecMainStartTimestampMonotonic=0",
        ]
    )
    responses: list[dict[str, Any]] = []
    if require_xdg:
        # Measured node-27 contract: with $XDG_RUNTIME_DIR unset, `systemctl --user`
        # cannot reach the user bus and exits non-zero. Forwarding XDG alone is
        # sufficient; the gate is skipped once it is present.
        responses.append(
            {
                "require_env": "XDG_RUNTIME_DIR",
                "stderr": "Failed to connect to bus: $DBUS_SESSION_BUS_ADDRESS and $XDG_RUNTIME_DIR not defined\n",
                "exit": 1,
            }
        )
    responses.extend(
        [
            {"match": ["compression-replay.service"], "stdout": replay},
            {"match": ["compression.service"], "stdout": recurring},
        ]
    )
    return responses


def _journalctl_responses(
    *, window_rows: list[dict[str, Any]] | None = None, boundary_cursor: str | None = BOUNDARY_CURSOR
) -> list[dict[str, Any]]:
    rows = window_rows or []
    window_stdout = "".join(json.dumps(row) + "\n" for row in rows)
    # Measured node-27 contract (Round-5 gate §G1): the `-n 0 --show-cursor`
    # boundary probe is always positioned at the tail and emits exactly one
    # cursor line (exit 0) even over an empty tail.
    boundary_stdout = "-- No entries --\n"
    if boundary_cursor is not None:
        boundary_stdout += f"-- cursor: {boundary_cursor}\n"
    responses: list[dict[str, Any]] = []
    # Measured node-27 contract (Round-5 gate §G1): the SAME governed-unit
    # `--after-cursor` window argv exits 1 WITH `--show-cursor` on an empty match
    # (0 rows) but exits 0 WITHOUT it. Encoding both is the regression lock: if
    # the vestigial `--show-cursor` is re-added to the window query, every silent
    # steady-state checkpoint aborts (RED), exactly as it does live. The token
    # match is order-sensitive, so the `--show-cursor` variant must precede the
    # plain `--after-cursor` response.
    if rows:
        # A non-empty match WITH --show-cursor emits its rows plus a trailing
        # cursor line and exits 0.
        cursor_tail = f"-- cursor: {boundary_cursor}\n" if boundary_cursor is not None else ""
        responses.append({"match": ["--after-cursor", "--show-cursor"], "stdout": window_stdout + cursor_tail})
    else:
        responses.append({"match": ["--after-cursor", "--show-cursor"], "stdout": "", "exit": 1})
    # The current window query omits --show-cursor: an empty match emits ZERO
    # bytes and exits 0 (no cursor line); a non-empty match emits its rows.
    responses.append({"match": ["--after-cursor"], "stdout": window_stdout})
    responses.append({"match": ["-n 0"], "stdout": boundary_stdout})
    return responses


def _docker_responses(
    *,
    dump_path: str,
    image: str = "sha256:" + "a" * 64,
    # Measured node-27 contract (Round-5 gate §G2): `readlink -f /usr/bin/pg_restore`
    # inside nhms-db resolves to the pg_wrapper dispatcher, NOT /usr/bin/pg_restore.
    realpath: str = "/usr/share/postgresql-common/pg_wrapper",
    binary_sha: str = "b" * 64,
    dump_sha: str = "c" * 64,
) -> list[dict[str, Any]]:
    return [
        {"match": ["inspect"], "stdout": image + "\n"},
        {"match": ["readlink"], "stdout": realpath + "\n"},
        {"match": ["sha256sum"], "stdout": f"{binary_sha}  {realpath}\n{dump_sha}  {dump_path}\n"},
    ]


def _git_responses(
    *,
    status: str = "",
    head: str = "a" * 40,
    reviewed: str = "a" * 40,
    remote: str = "https://github.com/DankerMu/SHUD-NWM.git",
) -> list[dict[str, Any]]:
    return [
        {"match": ["status"], "stdout": status},
        {"match": ["rev-parse", "HEAD"], "stdout": head + "\n"},
        {"match": ["rev-parse"], "stdout": reviewed + "\n"},
        {"match": ["remote", "get-url"], "stdout": remote + "\n"},
    ]


@pytest.fixture
def probe_bin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Repoint SUPERVISOR_BIN_DIR at a fixture directory of stub executables."""
    bindir = tmp_path / "probe-bin"
    bindir.mkdir()
    monkeypatch.setattr(supervisor, "SUPERVISOR_BIN_DIR", bindir)

    def install(name: str, responses: list[dict[str, Any]]) -> None:
        _write_stub(bindir, name, responses)

    return install


def _ledger(path: Path, *, invocation_id: str = PROBE_INVOCATION_ID) -> supervisor.AppendOnlyLedger:
    return supervisor.AppendOnlyLedger(path, run_id="run", run_plan_id="plan", invocation_id=invocation_id)


def _checkpoint(checkpoint_id: str = "preflight", phase: str = "preflight") -> dict[str, Any]:
    return {"checkpoint_id": checkpoint_id, "phase": phase, "command_id": None}


def test_supervisor_bin_dir_defaults_to_the_pinned_system_path() -> None:
    # The seam MUST default to the production location, or a live run would look
    # for its probes in a test directory that does not exist on node-27.
    assert supervisor.SUPERVISOR_BIN_DIR == Path("/usr/bin")
    assert supervisor._host_bin("systemctl") == "/usr/bin/systemctl"


# --- I-F1: systemd user-bus reachability -----------------------------------


def test_child_environment_never_carries_the_systemd_bus_locator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Authorized DB/compression children must stay maximally scrubbed: the bus
    # locator is a supervisor-probe concern only.
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1005")
    assert "XDG_RUNTIME_DIR" not in supervisor._child_environment()


def test_probe_environment_forwards_only_the_bus_locator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1005")
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/run/user/1005/bus")
    probe_env = supervisor._probe_environment()
    # Measured contract: XDG alone is load-bearing; DBUS must stay out.
    assert probe_env["XDG_RUNTIME_DIR"] == "/run/user/1005"
    assert "DBUS_SESSION_BUS_ADDRESS" not in probe_env


def test_checkpoint_reaches_the_user_bus_only_because_the_probe_env_forwards_xdg(
    probe_bin, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run-user"))
    probe_bin("psql", _psql_responses())
    probe_bin("systemctl", _systemctl_responses())
    probe_bin("journalctl", _journalctl_responses())
    with _ledger(tmp_path / "ledger.jsonl") as ledger:
        end_cursor = supervisor.capture_checkpoint(
            _checkpoint(),
            wall=supervisor.HardWall.start(30),
            ledger=ledger,
            artifact_dir=tmp_path,
            journal_cursor="s=stub;i=start;b=stub;m=0;t=0;x=0",
            invocation_id=PROBE_INVOCATION_ID,
        )
    assert end_cursor == BOUNDARY_CURSOR


def test_checkpoint_dies_at_the_first_systemd_probe_without_xdg(
    probe_bin, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    probe_bin("psql", _psql_responses())
    probe_bin("systemctl", _systemctl_responses())
    probe_bin("journalctl", _journalctl_responses())
    with _ledger(tmp_path / "ledger.jsonl") as ledger:
        with pytest.raises(supervisor.SupervisorError, match="systemd show"):
            supervisor.capture_checkpoint(
                _checkpoint(),
                wall=supervisor.HardWall.start(30),
                ledger=ledger,
                artifact_dir=tmp_path,
                journal_cursor="s=stub;i=start;b=stub;m=0;t=0;x=0",
                invocation_id=PROBE_INVOCATION_ID,
            )


# --- I-F2: empty governed-unit journal window is the steady state ----------


def test_checkpoint_accepts_empty_journal_window_and_carries_the_boundary_cursor(
    probe_bin, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run-user"))
    probe_bin("psql", _psql_responses())
    probe_bin("systemctl", _systemctl_responses())
    # Empty window (no governed activation) + positioned boundary cursor: the
    # exact steady state that aborted all 11 checkpoints before the fix.
    probe_bin("journalctl", _journalctl_responses(window_rows=[]))
    ledger_path = tmp_path / "ledger.jsonl"
    with _ledger(ledger_path) as ledger:
        end_cursor = supervisor.capture_checkpoint(
            _checkpoint(),
            wall=supervisor.HardWall.start(30),
            ledger=ledger,
            artifact_dir=tmp_path,
            journal_cursor="s=stub;i=start;b=stub;m=0;t=0;x=0",
            invocation_id=PROBE_INVOCATION_ID,
        )
    assert end_cursor == BOUNDARY_CURSOR
    event = json.loads(ledger_path.read_text().strip())
    assert event["journal_start_cursor"] == "s=stub;i=start;b=stub;m=0;t=0;x=0"
    assert event["journal_end_cursor"] == BOUNDARY_CURSOR
    journal_bytes = Path(event["journal"]["artifact"]["path"]).read_bytes()
    assert journal_bytes == b"-- cursor: " + BOUNDARY_CURSOR.encode() + b"\n"


def test_checkpoint_fails_when_the_positioned_boundary_probe_loses_its_cursor(
    probe_bin, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run-user"))
    probe_bin("psql", _psql_responses())
    probe_bin("systemctl", _systemctl_responses())
    # A boundary probe with no cursor line is a genuine "cursor lost" -- distinct
    # from an empty governed-unit window, which is normal.
    probe_bin("journalctl", _journalctl_responses(boundary_cursor=None))
    with _ledger(tmp_path / "ledger.jsonl") as ledger:
        with pytest.raises(supervisor.SupervisorError, match="did not retain its ending cursor"):
            supervisor.capture_checkpoint(
                _checkpoint(),
                wall=supervisor.HardWall.start(30),
                ledger=ledger,
                artifact_dir=tmp_path,
                journal_cursor="s=stub;i=start;b=stub;m=0;t=0;x=0",
                invocation_id=PROBE_INVOCATION_ID,
            )


def test_checkpoint_window_query_is_user_scoped_without_show_cursor(
    probe_bin, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Direct argv lock for the §G1 fix: the governed-unit window query is --user
    # scoped and MUST NOT carry the vestigial --show-cursor (which exits 1 on an
    # empty match live). The `-n 0` boundary probe keeps --show-cursor -- it is
    # the sole end-cursor source.
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run-user"))
    probe_bin("psql", _psql_responses())
    probe_bin("systemctl", _systemctl_responses())
    probe_bin("journalctl", _journalctl_responses())
    seen: list[list[str]] = []
    real = supervisor._run_capture_argv

    def recording(argv: list[str], **kwargs: Any) -> bytes:
        seen.append(list(argv))
        return real(argv, **kwargs)

    monkeypatch.setattr(supervisor, "_run_capture_argv", recording)
    with _ledger(tmp_path / "ledger.jsonl") as ledger:
        supervisor.capture_checkpoint(
            _checkpoint(),
            wall=supervisor.HardWall.start(30),
            ledger=ledger,
            artifact_dir=tmp_path,
            journal_cursor="s=stub;i=start;b=stub;m=0;t=0;x=0",
            invocation_id=PROBE_INVOCATION_ID,
        )
    window = next(argv for argv in seen if "--after-cursor" in argv)
    assert "--user" in window
    assert "--output=json" in window
    assert "--show-cursor" not in window
    boundary = next(argv for argv in seen if "-n" in argv and "--after-cursor" not in argv)
    assert "--user" in boundary and "0" in boundary and "--show-cursor" in boundary


def test_checkpoint_survives_empty_window_because_the_show_cursor_regression_is_locked(
    probe_bin, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # §G1 behavioral lock: the journalctl stub exits 1 for an empty --after-cursor
    # window that carries --show-cursor (the measured live behavior). Because the
    # fixed window query omits --show-cursor the checkpoint completes; re-adding
    # --show-cursor would make the stub abort this checkpoint (RED).
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run-user"))
    probe_bin("psql", _psql_responses())
    probe_bin("systemctl", _systemctl_responses())
    responses = _journalctl_responses(window_rows=[])
    # Guard the guard: the stub must really encode the exit-1 contract, so a
    # future refactor of _journalctl_responses cannot silently defang the lock.
    show_cursor_empty = next(r for r in responses if r["match"] == ["--after-cursor", "--show-cursor"])
    assert show_cursor_empty["exit"] == 1 and show_cursor_empty["stdout"] == ""
    probe_bin("journalctl", responses)
    with _ledger(tmp_path / "ledger.jsonl") as ledger:
        end_cursor = supervisor.capture_checkpoint(
            _checkpoint(),
            wall=supervisor.HardWall.start(30),
            ledger=ledger,
            artifact_dir=tmp_path,
            journal_cursor="s=stub;i=start;b=stub;m=0;t=0;x=0",
            invocation_id=PROBE_INVOCATION_ID,
        )
    assert end_cursor == BOUNDARY_CURSOR


def test_checkpoint_rejects_recurring_activation_in_the_journal_window(
    probe_bin, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run-user"))
    probe_bin("psql", _psql_responses())
    probe_bin("systemctl", _systemctl_responses())
    probe_bin(
        "journalctl",
        _journalctl_responses(
            window_rows=[
                {
                    "_SYSTEMD_USER_UNIT": "nhms-node27-timeseries-compression.service",
                    "_SYSTEMD_INVOCATION_ID": "deadbeefdeadbeefdeadbeefdeadbeef",
                }
            ]
        ),
    )
    with _ledger(tmp_path / "ledger.jsonl") as ledger:
        with pytest.raises(supervisor.SupervisorError, match="recurring compression activation"):
            supervisor.capture_checkpoint(
                _checkpoint(),
                wall=supervisor.HardWall.start(30),
                ledger=ledger,
                artifact_dir=tmp_path,
                journal_cursor="s=stub;i=start;b=stub;m=0;t=0;x=0",
                invocation_id=PROBE_INVOCATION_ID,
            )


def test_checkpoint_rejects_a_conflicting_database_session(
    probe_bin, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run-user"))
    probe_bin("psql", _psql_responses(activity={"sessions": [{"pid": 42, "state": "active"}]}))
    probe_bin("systemctl", _systemctl_responses())
    probe_bin("journalctl", _journalctl_responses())
    with _ledger(tmp_path / "ledger.jsonl") as ledger:
        with pytest.raises(supervisor.SupervisorError, match="conflicting database session"):
            supervisor.capture_checkpoint(
                _checkpoint(),
                wall=supervisor.HardWall.start(30),
                ledger=ledger,
                artifact_dir=tmp_path,
                journal_cursor="s=stub;i=start;b=stub;m=0;t=0;x=0",
                invocation_id=PROBE_INVOCATION_ID,
            )


def test_checkpoint_rejects_drifted_d3_catalog(
    probe_bin, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run-user"))
    drifted = _d3_catalog()
    drifted["hypertables"]["hydro.river_timeseries"] = False
    probe_bin("psql", _psql_responses(catalog=drifted))
    probe_bin("systemctl", _systemctl_responses())
    probe_bin("journalctl", _journalctl_responses())
    with _ledger(tmp_path / "ledger.jsonl") as ledger:
        with pytest.raises(supervisor.SupervisorError, match="current D3 state"):
            supervisor.capture_checkpoint(
                _checkpoint(),
                wall=supervisor.HardWall.start(30),
                ledger=ledger,
                artifact_dir=tmp_path,
                journal_cursor="s=stub;i=start;b=stub;m=0;t=0;x=0",
                invocation_id=PROBE_INVOCATION_ID,
            )


def test_producer_checkpoint_artifacts_satisfy_the_verifier(
    probe_bin, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The producer/consumer seam, closed end to end: the bytes capture_checkpoint
    # emits must pass the verifier's own _validate_checkpoint_artifacts.
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run-user"))
    probe_bin("psql", _psql_responses())
    probe_bin("systemctl", _systemctl_responses())
    probe_bin("journalctl", _journalctl_responses())
    ledger_path = tmp_path / "ledger.jsonl"
    with _ledger(ledger_path) as ledger:
        supervisor.capture_checkpoint(
            _checkpoint(),
            wall=supervisor.HardWall.start(30),
            ledger=ledger,
            artifact_dir=tmp_path,
            journal_cursor="s=stub;i=start;b=stub;m=0;t=0;x=0",
            invocation_id=PROBE_INVOCATION_ID,
        )
    event = json.loads(ledger_path.read_text().strip())
    result = evidence._validate_checkpoint_artifacts(
        event,
        "supervisor checkpoint[0]",
        invocation_id=PROBE_INVOCATION_ID,
        supervisor_pid=os.getpid(),
    )
    assert result["journal_end_cursor"] == BOUNDARY_CURSOR
    assert result["replay_activation"]["MainPID"] == os.getpid()


# --- resolve_container_pg_restore_identity ---------------------------------


def test_resolve_container_pg_restore_identity_reads_real_docker_probes(
    probe_bin, tmp_path: Path
) -> None:
    dump_path = "/var/lib/postgresql/evidence/schema.dump"
    probe_bin("docker", _docker_responses(dump_path=dump_path))
    identity = supervisor.resolve_container_pg_restore_identity(
        wall=supervisor.HardWall.start(30), dump_path=dump_path
    )
    assert identity == {
        "dump_sha256": "c" * 64,
        "container_image_id": "sha256:" + "a" * 64,
        # Measured (Round-5 gate §G2): the resolver binds the pg_wrapper realpath.
        "binary_realpath": "/usr/share/postgresql-common/pg_wrapper",
        "binary_sha256": "b" * 64,
    }


def test_resolve_container_pg_restore_identity_rejects_out_of_mount_dump() -> None:
    with pytest.raises(supervisor.SupervisorError, match="outside the DB container data mount"):
        supervisor.resolve_container_pg_restore_identity(
            wall=supervisor.HardWall.start(30), dump_path="/tmp/schema.dump"
        )


@pytest.mark.parametrize(
    "relocated",
    [
        "/opt/pg_restore",
        # The old assumption the gate refuted: re-pinning /usr/bin/pg_restore (the
        # symlink, not its wrapper realpath) must now fail closed as drift.
        "/usr/bin/pg_restore",
    ],
)
def test_resolve_container_pg_restore_identity_rejects_relocated_binary(
    probe_bin, tmp_path: Path, relocated: str
) -> None:
    dump_path = "/var/lib/postgresql/evidence/schema.dump"
    probe_bin("docker", _docker_responses(dump_path=dump_path, realpath=relocated))
    with pytest.raises(supervisor.SupervisorError, match="container pg_restore identity differs"):
        supervisor.resolve_container_pg_restore_identity(
            wall=supervisor.HardWall.start(30), dump_path=dump_path
        )


# --- _verify_checkout_lineage ----------------------------------------------


def _lineage_plan() -> dict[str, Any]:
    return {
        "mutation_head_sha": "a" * 40,
        "reviewed_remote_ref": supervisor.EXPECTED_REVIEWED_REMOTE_REF,
    }


def test_verify_checkout_lineage_accepts_clean_reviewed_origin(probe_bin) -> None:
    probe_bin("git", _git_responses())
    supervisor._verify_checkout_lineage(_lineage_plan(), wall=supervisor.HardWall.start(30))


@pytest.mark.parametrize(
    "override",
    [
        {"status": " M scripts/x.py\n"},
        {"head": "b" * 40},
        {"reviewed": "b" * 40},
        {"remote": "https://github.com/evil/other.git"},
    ],
)
def test_verify_checkout_lineage_rejects_broken_lineage(probe_bin, override: dict[str, str]) -> None:
    probe_bin("git", _git_responses(**override))
    with pytest.raises(supervisor.SupervisorError, match="lineage differs"):
        supervisor._verify_checkout_lineage(_lineage_plan(), wall=supervisor.HardWall.start(30))


# --- S-F1: truncation accounting is a function of dropped bytes -------------


def _finite_writer(total: int) -> list[str]:
    # `bytes(total)` yields `total` NUL bytes and avoids `*`, which
    # `_assert_concrete_argv` forbids as a shell-template token.
    return [
        sys.executable,
        "-c",
        f"import os,sys\nb=bytes({total})\nwhile b:\n b=b[os.write(1,b):]\nsys.exit(0)",
    ]


def test_drain_child_reports_truncation_from_dropped_bytes_not_termination(tmp_path: Path) -> None:
    limit = 1024
    process = subprocess.Popen(
        _finite_writer(limit + 512),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    process.wait()  # the child has fully exited before we drain the pipe
    stdout, stderr, terminated, dropped = supervisor._drain_child(
        process,
        wall=supervisor.HardWall.start(5),
        stdout_limit=limit,
        stderr_limit=limit,
        term_grace=0.05,
    )
    assert process.returncode == 0
    assert len(stdout) == limit
    # The supervisor never had to intervene, yet bytes were dropped: the truthful
    # truncation signal is `dropped`, independent of `terminated`.
    assert terminated is False
    assert dropped["stdout"] == 512


def test_finite_over_limit_child_is_ledgered_as_truncated_even_after_exit(tmp_path: Path) -> None:
    ledger_path = tmp_path / "ledger.jsonl"
    limit = 1024
    command = {
        "command_id": "finite-flood",
        "kind": "pg_dump",
        "argv": _finite_writer(limit + 512),
        "artifact_associations": {},
    }
    with _ledger(ledger_path) as ledger:
        with pytest.raises(supervisor.HardWallExpired):
            supervisor.run_child(
                command,
                wall=supervisor.HardWall.start(5),
                ledger=ledger,
                mutation_head_sha="a" * 40,
                database="nhms",
                stdout_limit=limit,
                term_grace=0.05,
            )
    event = json.loads(ledger_path.read_text().strip())
    assert event["stdout"]["bytes"] == limit
    assert event["stdout"]["truncated"] is True


def test_capture_step_fails_closed_when_dropped_without_termination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # S-F1 for run_capture_step: a probe whose stdout overran the ceiling AFTER
    # the child already exited (terminated=False, dropped>0 -- the exact state
    # `test_drain_child_reports_truncation_from_dropped_bytes_not_termination`
    # proves _drain_child can return) MUST still fail closed. This deterministically
    # kills the supervisor.py:903 mutant `... or dropped[...]` -> `if terminated:`,
    # which would let the truncated capture publish + ledger instead of aborting.
    output = tmp_path / "capture-dropped.json"
    capture = {
        "capture_id": "capture-dropped",
        "kind": "preflight_evidence",
        "argv": [sys.executable, "-c", "pass"],
        "output_path": str(output),
    }

    def fake_drain(process: Any, **_: Any) -> tuple[bytes, bytes, bool, dict[str, int]]:
        process.wait()  # keep process.returncode == 0 so the mutant reaches publish
        return (b"x" * 10, b"", False, {"stdout": 5, "stderr": 0})

    monkeypatch.setattr(supervisor, "_drain_child", fake_drain)
    ledger_path = tmp_path / "capture-dropped-ledger.jsonl"
    with supervisor.AppendOnlyLedger(
        ledger_path, run_id="run", run_plan_id="plan", invocation_id="1" * 32
    ) as ledger:
        with pytest.raises(supervisor.HardWallExpired):
            supervisor.run_capture_step(
                capture, wall=supervisor.HardWall.start(5), ledger=ledger, artifact_dir=tmp_path
            )
    # Fail closed BEFORE publish or ledger: unlike run_child (which ledgers the
    # truncated child then raises), the capture writer raises on `dropped` ahead
    # of any write, so a truncated probe leaks neither artifact nor ledger row.
    assert not output.exists()
    assert ledger_path.read_text() == ""


def test_capture_step_over_limit_finite_writer_fails_closed(tmp_path: Path) -> None:
    # The live-child companion (finite writer, limit + N, child exits): a real
    # over-limit capture aborts and publishes/ledgers nothing.
    output = tmp_path / "capture-over.json"
    limit = 1024
    capture = {
        "capture_id": "capture-overlimit",
        "kind": "preflight_evidence",
        "argv": _finite_writer(limit + 512),
        "output_path": str(output),
    }
    ledger_path = tmp_path / "capture-over-ledger.jsonl"
    with supervisor.AppendOnlyLedger(
        ledger_path, run_id="run", run_plan_id="plan", invocation_id="1" * 32
    ) as ledger:
        with pytest.raises(supervisor.HardWallExpired):
            supervisor.run_capture_step(
                capture,
                wall=supervisor.HardWall.start(5),
                ledger=ledger,
                artifact_dir=tmp_path,
                stdout_limit=limit,
                term_grace=0.05,
            )
    assert not output.exists()
    assert ledger_path.read_text() == ""


def test_capture_step_at_the_ceiling_publishes_untruncated_stdout(tmp_path: Path) -> None:
    # Complement to the over-limit lock: a child at exactly the ceiling is NOT
    # truncated, so it publishes cleanly and the ledger records truncated=False.
    output = tmp_path / "capture-exact.json"
    limit = 1024
    capture = {
        "capture_id": "capture-exact",
        "kind": "preflight_evidence",
        "argv": _finite_writer(limit),
        "output_path": str(output),
    }
    ledger_path = tmp_path / "capture-exact-ledger.jsonl"
    with supervisor.AppendOnlyLedger(
        ledger_path, run_id="run", run_plan_id="plan", invocation_id="1" * 32
    ) as ledger:
        event = supervisor.run_capture_step(
            capture,
            wall=supervisor.HardWall.start(5),
            ledger=ledger,
            artifact_dir=tmp_path,
            stdout_limit=limit,
            term_grace=0.05,
        )
    assert output.stat().st_size == limit
    assert event["stdout"]["bytes"] == limit
    assert event["stdout"]["truncated"] is False


# --- C-F1: decompress argv guard order -------------------------------------


def test_decompress_argv_rejects_wrong_association_label_as_supervisor_error() -> None:
    argv = [
        "/home/nwm/NWM/.venv/bin/python",
        "/home/nwm/NWM/scripts/node27_timeseries_decompression_replay.py",
        "--database",
        "nhms",
        "--mutation-head-sha",
        "a" * 40,
    ]
    with pytest.raises(supervisor.SupervisorError, match="ownership differs"):
        supervisor._assert_exact_argv(
            argv, kind="decompress", associations={"dry_run_receipt": "/tmp/x.json"}
        )


# --- F3: full producer state machine drives the real producers -------------


def test_full_state_machine_executes_real_producers_against_stub_binaries(
    probe_bin, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run-user"))
    probe_bin("psql", _psql_responses())
    probe_bin("systemctl", _systemctl_responses())
    probe_bin("journalctl", _journalctl_responses())
    probe_bin("docker", _docker_responses(dump_path="/var/lib/postgresql/evidence/schema.dump"))
    plan = _plan()
    for command in plan["commands"]:
        associations: dict[str, str] = {}
        for name in command["artifact_associations"]:
            path = tmp_path / f"child-{command['command_id']}-{name}.json"
            associations[name] = str(path)
        command["artifact_associations"] = associations
        writes = "; ".join(
            f"Path({path!r}).write_text('{{\"owner\":\"{name}\"}}\\n')" for name, path in associations.items()
        )
        command["argv"] = [sys.executable, "-c", f"from pathlib import Path; {writes or 'pass'}"]
    for capture in plan["captures"]:
        path = tmp_path / f"capture-{capture['kind']}.json"
        capture["output_path"] = str(path)
        capture["argv"] = [sys.executable, "-c", f"print('{{\"owner\":\"{capture['kind']}\"}}')"]
    # The real resolver reads the dump path from the list command's argv[-1].
    # Append it as an EXTRA arg the python stub ignores (sys.argv[1:]), so the
    # child stays a runnable no-op while argv[-1] is the mount-internal path the
    # resolver validates.
    for command in plan["commands"]:
        if command["kind"] == "pg_restore_list":
            command["argv"] = [sys.executable, "-c", "pass", "/var/lib/postgresql/evidence/schema.dump"]

    ledger_path = tmp_path / "state-machine-ledger.jsonl"
    cursor = {"value": "s=stub;i=start;b=stub;m=0;t=0;x=0"}
    checkpoints_by_phase = {
        (str(item["phase"]), item["command_id"]): item for item in plan["checkpoints"]
    }
    with _ledger(ledger_path) as ledger:

        def live_checkpoint(phase: str, command_id: str | None) -> None:
            cursor["value"] = supervisor.capture_checkpoint(
                checkpoints_by_phase[(phase, command_id)],
                wall=supervisor.HardWall.start(60),
                ledger=ledger,
                artifact_dir=tmp_path,
                journal_cursor=cursor["value"],
                invocation_id=PROBE_INVOCATION_ID,
            )

        supervisor.execute_producer_state_machine(
            plan,
            wall=supervisor.HardWall.start(60),
            ledger=ledger,
            artifact_dir=tmp_path,
            checkpoint_runner=live_checkpoint,
            restore_identity_resolver=lambda probe_wall, dump: supervisor.resolve_container_pg_restore_identity(
                wall=probe_wall, dump_path=dump
            ),
        )

    events = [json.loads(line) for line in ledger_path.read_text().splitlines()]
    checkpoint_events = [event for event in events if event["event_type"] == "checkpoint"]
    assert len(checkpoint_events) == len(plan["checkpoints"])
    for index, event in enumerate(checkpoint_events):
        result = evidence._validate_checkpoint_artifacts(
            event,
            f"supervisor checkpoint[{index}]",
            invocation_id=PROBE_INVOCATION_ID,
            supervisor_pid=os.getpid(),
        )
        assert result["journal_end_cursor"] == BOUNDARY_CURSOR
    # The real docker resolver's identity is bound onto the pg_restore children.
    restore_event = next(event for event in events if event.get("kind") == "pg_restore_version")
    assert restore_event["artifact_associations"]["dump_sha256"] == "c" * 64
    assert restore_event["artifact_associations"]["container_image_id"] == "sha256:" + "a" * 64
