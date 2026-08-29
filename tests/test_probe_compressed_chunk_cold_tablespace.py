"""Local contract tests for the isolated-cluster probe CLI (#1892).

These tests never start Docker or touch live PostgreSQL. The isolated-cluster
integration path is marked ``integration`` + ``timescaledb_210``.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from packages.common.compressed_chunk_cold_residency import (
    ACCEPTED_SEQUENCE_NAME,
    LIVE_CONTAINER_NAME,
    LIVE_PORT,
    PINNED_IMAGE_ID,
    REJECTED_SEQUENCE_NAMES,
    ColdResidencyError,
)
from scripts import probe_compressed_chunk_cold_tablespace as probe


def _completed(code: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=["docker"], returncode=code, stdout=stdout, stderr=stderr)


def test_config_refuses_live_container_name() -> None:
    args = probe.parse_args(["--container-name", LIVE_CONTAINER_NAME, "--host-port", "55492"])
    with pytest.raises(ColdResidencyError, match="live container"):
        probe.config_from_args(args)


def test_config_refuses_live_port() -> None:
    args = probe.parse_args(["--container-name", "nhms-1892-probe-abcdef123456", "--host-port", str(LIVE_PORT)])
    with pytest.raises(ColdResidencyError, match="55432"):
        probe.config_from_args(args)


def test_config_refuses_live_pgdata_and_checkout_paths() -> None:
    args = probe.parse_args(
        [
            "--container-name",
            "nhms-1892-probe-abcdef123456",
            "--host-port",
            "55492",
            "--work-root",
            "/home/nwm/nhms-pgdata/nhms-1892-probe-abcdef123456",
        ]
    )
    with pytest.raises(ColdResidencyError, match="live/production path"):
        probe.config_from_args(args)
    args = probe.parse_args(
        [
            "--container-name",
            "nhms-1892-probe-abcdef123456",
            "--host-port",
            "55492",
            "--work-root",
            "/home/nwm/NWM/tmp/nhms-1892-probe-abcdef123456",
        ]
    )
    with pytest.raises(ColdResidencyError, match="live/production path"):
        probe.config_from_args(args)


def test_config_refuses_unowned_container_name() -> None:
    args = probe.parse_args(["--container-name", "random-pg", "--host-port", "55492"])
    with pytest.raises(ColdResidencyError, match="identity-bound|must match"):
        probe.config_from_args(args)


def test_unit_plan_is_shell_first_and_rejects_direct_alter() -> None:
    report = probe.unit_plan_report()
    assert report["accepted_sequence"] == ACCEPTED_SEQUENCE_NAME == "shell_first_decompress_recompress_atomic"
    assert set(report["rejected_sequences"]) == set(REJECTED_SEQUENCE_NAMES)
    assert report["lock_oids"] == [10, 20]
    assert report["shell_move_oids"] == [10, 15, 16]
    assert report["sql"][0] == "BEGIN"
    assert report["sql"][1] == "SET LOCAL lock_timeout = '2s'"
    assert report["sql"][2] == "SET LOCAL statement_timeout = '30s'"
    assert report["sql"][3].startswith("LOCK TABLE")
    joined = "\n".join(report["sql"])
    assert "SELECT decompress_chunk" in joined
    assert "SELECT compress_chunk" in joined
    assert "ALTER TABLE" in joined and 'SET TABLESPACE "nhms_cold"' in joined
    assert not any(s.startswith("ALTER TABLE") and "compress_hyper" in s for s in report["sql"])
    assert not any("pg_toast" in s and s.startswith("ALTER") for s in report["sql"])
    assert report["sql"][-1] == "COMMIT"
    assert report["already_cold_sql_empty"] is True
    assert report["image"]["image_id"] == PINNED_IMAGE_ID


def test_docker_run_argv_never_binds_live_paths(tmp_path: Path) -> None:
    work = tmp_path / "nhms-1892-probe-abcdef123456"
    work.mkdir()
    config = probe.config_from_args(
        probe.parse_args(
            [
                "--container-name",
                "nhms-1892-probe-abcdef123456",
                "--host-port",
                "55492",
                "--work-root",
                str(work),
            ]
        )
    )
    argv = probe.docker_run_argv(config)
    joined = " ".join(argv)
    assert LIVE_CONTAINER_NAME not in argv
    assert "55432" not in joined
    assert "/home/nwm/nhms-pgdata" not in joined
    assert "/data/GHDC" not in joined
    assert config.password not in joined
    assert "--env-file" in argv
    assert "--user" in argv
    assert argv[argv.index("--user") + 1] == f"{os.getuid()}:{os.getgid()}"
    assert config.container_name in argv
    assert f"127.0.0.1:{config.host_port}:5432" in argv
    env_file = work / "postgres.env"
    assert env_file.exists()
    assert oct(env_file.stat().st_mode & 0o777) == "0o600"


def test_cleanup_refuses_unowned_identities(tmp_path: Path) -> None:
    foreign = tmp_path / "not-owned"
    foreign.mkdir()
    proof = probe.cleanup_owned(
        probe.OwnedResources(container_name="nhms-db", work_root=foreign, created_work_root=True),
        docker_bin="docker",
        runner=lambda *_args, **_kwargs: _completed(0),
    )
    assert proof["refused"] == "unowned identity"
    assert foreign.exists()


def test_cleanup_removes_only_identity_bound_resources(tmp_path: Path) -> None:
    work = tmp_path / "nhms-1892-probe-abcdef123456"
    work.mkdir()
    calls: list[list[str]] = []

    def runner(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(list(argv))
        if argv[1] == "inspect":
            return _completed(1, stderr="No such object")
        return _completed(0)

    proof = probe.cleanup_owned(
        probe.OwnedResources(
            container_name="nhms-1892-probe-abcdef123456",
            work_root=work,
            created_work_root=True, created_container=True,
        ),
        docker_bin="docker",
        runner=runner,
    )
    assert proof["container_removed"] is True
    assert proof["work_root_removed"] is True
    assert not work.exists()
    assert calls[0][:3] == ["docker", "rm", "-f"]
    assert "nhms-db" not in calls[0]


_WINDOW_PARITY = {
    "count": 24,
    "value_sum": 138.0,
    "checksum": "44e88875287a81d598d28044dc7e605e",
    "range_start": "2026-06-25T00:00:00Z",
    "range_end": "2026-07-02T00:00:00Z",
}
_FAIL_PARITY = {
    "count": 24,
    "value_sum": 138.0,
    "checksum": "aabbccddeeff00112233445566778899",
    "range_start": "2026-06-18T00:00:00Z",
    "range_end": "2026-06-25T00:00:00Z",
}
_REPLAY_PARITY = {
    "count": 25,
    "value_sum": 180.0,
    "checksum": "243da337af54d8cfddaafde44f6e409a",
    "range_start": "2026-06-25T00:00:00Z",
    "range_end": "2026-07-02T00:00:00Z",
}
_COLD_EXPANDED_MEMBERS = [
    {"kind": "origin_heap", "tablespace": "nhms_cold", "bytes": 65536},
    {"kind": "index", "tablespace": "nhms_cold", "bytes": 8192},
    {"kind": "toast_heap", "tablespace": "nhms_cold", "bytes": 16384},
]
_HOT_EXPANDED_MEMBERS = [
    {"kind": "origin_heap", "tablespace": "pg_default", "bytes": 65536},
    {"kind": "index", "tablespace": "pg_default", "bytes": 8192},
]
_SOURCE_EXPANDED_MEMBERS = [
    {"kind": "origin_heap", "tablespace": "pg_default", "bytes": 65536},
    {"kind": "index", "tablespace": "pg_default", "bytes": 8192},
    {"kind": "toast_heap", "tablespace": "pg_default", "bytes": 16384},
]
_MIXED_EXPANDED_MEMBERS = [
    {"kind": "origin_heap", "tablespace": "nhms_cold", "bytes": 65536},
    {"kind": "index", "tablespace": "pg_default", "bytes": 8192},
]


def _after_decompress_row(
    *,
    target: str,
    members: list[dict[str, object]],
    pg_default_bytes: int,
) -> dict[str, object]:
    return {
        "phases": {"after_decompress": {"members": members, "residency": "already_target"}},
        "after_decompress_proof": {
            "target": target,
            "all_requested_target": True,
            "pg_default_bytes": pg_default_bytes,
            "member_count": len(members),
        },
    }


def _restored(**extra: object) -> dict[str, object]:
    row: dict[str, object] = {
        "reconciliation": "complete_source",
        "original_sibling": True,
        "before_parity": dict(_FAIL_PARITY),
        "after_parity": dict(_FAIL_PARITY),
    }
    row.update(extra)
    return row


def _required_row_report(**overrides: object) -> dict[str, object]:
    report: dict[str, object] = {
        "status": "passed",
        "false_success": False,
        "accepted_sequence": ACCEPTED_SEQUENCE_NAME,
        "image_pin_ok": True,
        "pg_matches_pin": True,
        "ts_matches_pin": True,
        "engine_gate": {
            "image_pin_ok": True,
            "pg_matches_pin": True,
            "ts_matches_pin": True,
            "live_matches_pin": True,
            "requested_matches_pin": True,
            "used_matches_requested": True,
        },
        "image_live_readonly": {
            "image_id": "sha256:ad39c4fbc5c44557db1e16af10ec11e3ab12d0a472374f39aaba06ad9ca2640e",
            "image_ref": "sha256:ad39c4fbc5c44557db1e16af10ec11e3ab12d0a472374f39aaba06ad9ca2640e",
            "config_image": "sha256:ad39c4fbc5c44557db1e16af10ec11e3ab12d0a472374f39aaba06ad9ca2640e",
            "live_ref_alias": "digest_image_id",
            "repo_tags": ["timescale/timescaledb-ha:pg15-latest"],
            "repo_digests": [
                "timescale/timescaledb-ha@sha256:a8e3322e1cf936828698cb4de2a9c4b59acae1b123909f023bb15f42270af95d"
            ],
        },
        "sequence": {
            "accepted": ACCEPTED_SEQUENCE_NAME,
            "rejected": sorted(REJECTED_SEQUENCE_NAMES),
        },
        "candidates": {
            "move_chunk": {
                "ok": False,
                "error": "function must be run on the access node only",
            },
            "direct_compressed_heap_alter": {
                "ok": False,
                "error": "changing tablespace of compressed chunk is not supported",
            },
            "direct_toast_alter": {"ok": False, "error": "is a system catalog"},
            "decompress_first": {"complete": False},
            "internal_attach": {
                "complete": False,
                "new_group_residency": "all_source",
                "business_attached": [],
            },
            "two_transaction": {"atomic": False},
            "shell_first_rollback": {
                "reconciliation": "complete_source",
                "original_sibling": True,
                "before": {"compressed": {"oid": 20989, "name": "compress_hyper_2_12_chunk"}},
                "after": {"compressed": {"oid": 20989, "name": "compress_hyper_2_12_chunk"}},
                "before_parity": dict(_WINDOW_PARITY),
                "after_parity": dict(_WINDOW_PARITY),
                **_after_decompress_row(
                    target="nhms_cold",
                    members=_COLD_EXPANDED_MEMBERS,
                    pg_default_bytes=0,
                ),
                "phases": {
                    "after_decompress": {"members": _COLD_EXPANDED_MEMBERS, "residency": "already_target"},
                    "after_recompress": {
                        "residency": "already_target",
                        "compressed": {"oid": 21196},
                    },
                },
            },
        },
        "lifecycle": {
            "committed_move": {
                "reconciliation": "complete_target",
                "before_parity": dict(_WINDOW_PARITY),
                "after_parity": dict(_WINDOW_PARITY),
                "before": {"compressed": {"oid": 20989, "name": "compress_hyper_2_12_chunk"}},
                "after": {"compressed": {"oid": 21403, "name": "compress_hyper_2_22_chunk"}},
                **_after_decompress_row(
                    target="nhms_cold",
                    members=_COLD_EXPANDED_MEMBERS,
                    pg_default_bytes=0,
                ),
            },
            "cold": {"residency": "already_target"},
            "before_parity": dict(_WINDOW_PARITY),
            "cold_parity": dict(_WINDOW_PARITY),
            "parity_unchanged_until_replay": True,
            "already_cold": {"outcome": "already_cold", "reason": "already_cold"},
            "decompressed": {"residency": "already_target", "is_compressed": False},
            "replay_parity": dict(_REPLAY_PARITY),
            "recompressed": {
                "residency": "already_target",
                "is_compressed": True,
                "compressed": {"oid": 21491, "name": "compress_hyper_2_24_chunk"},
            },
            "move_back": {
                "reconciliation": "complete_target",
                **_after_decompress_row(
                    target="pg_default",
                    members=_SOURCE_EXPANDED_MEMBERS,
                    pg_default_bytes=90112,
                ),
            },
            "move_back_residency": "all_source",
            "drop_remaining": [],
            "drop_before_oids": [10, 15, 16, 20, 25, 30, 31, 40, 41],
            "drop_oids_absent": True,
        },
        "boundaries": {
            "exact_cutoff_eligibility": "eligible",
            "same_window_disjoint": True,
            "attach_tablespace": [],
            "empty_chunk": {"members": [{"kind": "origin_heap", "bytes": 0}]},
            "no_index_group": {"members": [{"kind": "origin_heap", "heap_oid": 10, "tablespace": "pg_default"}]},
            "no_index_origin_index_count": 0,
            "quoted_numeric_leading_index": True,
            "owned_toast_present": True,
            "new_chunk_tablespace": "pg_default",
        },
        "failures": {
            "missing_target": _restored(
                after={"compressed": {"oid": 1}},
                plan_kind="migrate",
                target="nhms_cold_missing",
                sql='ALTER TABLE "_timescaledb_internal"."_hyper_1_1_chunk" SET TABLESPACE "nhms_cold_missing"',
                exec={
                    "ok": False,
                    "error_type": "UndefinedObject",
                    "error": 'tablespace "nhms_cold_missing" does not exist',
                    "sql": 'ALTER TABLE "_timescaledb_internal"."_hyper_1_1_chunk" SET TABLESPACE "nhms_cold_missing"',
                },
            ),
            "mid_shell": _restored(error={"error_type": "UndefinedTable"}),
            "mid_decompress": _restored(error={"error_type": "UndefinedTable"}),
            "mid_recompress": _restored(error={"error_type": "UndefinedTable"}),
            "statement_timeout": _restored(
                sleep={"ok": False, "error_type": "QueryCanceled"},
                after={"compressed": {"oid": 1}},
            ),
            "lock_conflict": _restored(
                block={"ok": False, "error_type": "LockNotAvailable"},
                after={"compressed": {"oid": 1}},
            ),
            "pre_commit_interrupt": _restored(
                after={"compressed": {"oid": 1}},
                error={"error_type": "OperationalError", "error": "server closed the connection unexpectedly"},
            ),
            "lost_commit_ack": {
                "committed": True,
                "reconciliation": "complete_target",
                "replayed": False,
                "outcome": "committed_ack_lost",
                "commit_ack_lost": True,
                "error": {"error_type": "CommitAckLost", "error": "commit acknowledgement lost after server commit"},
                "before": {"compressed": {"oid": 1, "name": "compress_hyper_2_10_chunk"}},
                "after": {"compressed": {"oid": 99, "name": "compress_hyper_2_20_chunk"}},
                "before_parity": dict(_FAIL_PARITY),
                "after_parity": dict(_FAIL_PARITY),
                **_after_decompress_row(
                    target="nhms_cold",
                    members=_COLD_EXPANDED_MEMBERS,
                    pg_default_bytes=0,
                ),
            },
            "permission": _restored(
                ok=False,
                error_type="InsufficientPrivilege",
                error="permission denied for tablespace nhms_cold",
                plan_kind="migrate",
                target="nhms_cold",
                sql='ALTER TABLE "_timescaledb_internal"."_hyper_1_1_chunk" SET TABLESPACE "nhms_cold"',
            ),
            "full_target": _restored(
                genuine_enospc=True,
                plan_kind="migrate",
                target="probe_full",
                sql='ALTER TABLE "_timescaledb_internal"."_hyper_1_1_chunk" SET TABLESPACE "probe_full"',
                move={
                    "ok": False,
                    "error_type": "DiskFull",
                    "error": "No space left on device",
                    "sql": 'ALTER TABLE "_timescaledb_internal"."_hyper_1_1_chunk" SET TABLESPACE "probe_full"',
                },
                after={"compressed": {"oid": 1}},
            ),
            "catalog_path_mismatch": {
                "refused": True,
                "relation_oids_unchanged": True,
                "residency_unchanged": True,
                "parity_unchanged": True,
                "before_parity": dict(_FAIL_PARITY),
                "after_parity": dict(_FAIL_PARITY),
            },
            "injected_missing_relation_error": _restored(
                error={"error_type": "UndefinedTable"},
                selected_relation_disappeared=False,
            ),
            "selection_disappearance": {
                "stale_blocked": True,
                "sacrificed_group_gone": True,
                "unrelated_unchanged": True,
                "before_oids": [50, 51, 52],
                "after_oids_absent": True,
            },
            "false_success": False,
            "capacity_preflight": {
                "positive": {"approved": True, "required_cold_bytes": 1101, "required_hot_bytes": 1},
                "equality": {
                    "approved": True,
                    "required_cold_bytes": 1100,
                    "required_hot_bytes": 1,
                    "cold_headroom_bytes": 0,
                    "hot_headroom_bytes": 0,
                },
                "cold_short": {
                    "approved": False,
                    "shell_sql_executed": False,
                    "oids_unchanged": True,
                    "residency_unchanged": True,
                    "original_sibling": True,
                    "parity_unchanged": True,
                },
                "hot_short": {
                    "approved": False,
                    "shell_sql_executed": False,
                    "oids_unchanged": True,
                    "residency_unchanged": True,
                    "original_sibling": True,
                    "parity_unchanged": True,
                },
                "before_compression_total_bytes": 1000,
                "retained_source_bytes": 8192,
            },
        },
        "failure_chunk_parity": dict(_FAIL_PARITY),
        "live_ref_alias": "digest_image_id",
        "live_repo_digest": (
            "timescale/timescaledb-ha@"
            "sha256:a8e3322e1cf936828698cb4de2a9c4b59acae1b123909f023bb15f42270af95d"
        ),
        "parity_sentinel": {
            "target_mutation_changes_checksum": True,
            "sibling_compensation_does_not_hide": True,
        },
        "wal": {"limitation": "instance-level pg_wal_lsn_diff from 0/0, not per-group WAL volume"},
        "cleanup": {"container_absent": True, "work_root_absent": True, "identity_bound": True},
    }
    report.update(overrides)
    return report

def test_parse_probe_report_rejects_unknown_status_and_false_success() -> None:
    with pytest.raises(probe.ProbeError, match="not reconcilable"):
        probe.parse_probe_report({"status": "ok"})
    with pytest.raises(probe.ProbeError, match="accepted sequence"):
        probe.parse_probe_report({"status": "passed", "sequence": {"accepted": "alter_tablespace_oid_order"}})
    with pytest.raises(probe.ProbeError, match="claimed success"):
        probe.parse_probe_report({"status": "failed", "false_success": True})
    parsed = probe.parse_probe_report(json.dumps({"status": "refused", "error": "refusing live container nhms-db"}))
    assert parsed["status"] == "refused"


def test_all_passed_requires_every_task_row_and_cleanup() -> None:
    report = _required_row_report()
    assert probe._all_passed(report) is True
    missing_cleanup = dict(report)
    missing_cleanup["cleanup"] = {"container_absent": False, "work_root_absent": True}
    assert probe._all_passed(missing_cleanup) is False
    missing_engine = dict(report)
    missing_engine["engine_gate"] = {**report["engine_gate"], "pg_matches_pin": False}  # type: ignore[dict-item]
    assert probe._all_passed(missing_engine) is False
    new_source_sib = dict(report)
    candidates = dict(report["candidates"])  # type: ignore[arg-type]
    rollback = dict(candidates["shell_first_rollback"])  # type: ignore[arg-type]
    rollback["after"] = {"compressed": {"oid": 21196, "name": "compress_hyper_2_20_chunk"}}
    candidates["shell_first_rollback"] = rollback
    new_source_sib["candidates"] = candidates
    assert probe._all_passed(new_source_sib) is False
    deleted_failure = dict(report)
    failures = dict(report["failures"])  # type: ignore[arg-type]
    del failures["catalog_path_mismatch"]
    deleted_failure["failures"] = failures
    assert probe._all_passed(deleted_failure) is False
    fake_enospc = dict(report)
    failures = dict(report["failures"])  # type: ignore[arg-type]
    failures["full_target"] = {
        "genuine_enospc": False,
        "move": {"ok": False, "error": "permission denied"},
        "reconciliation": "complete_source",
    }
    fake_enospc["failures"] = failures
    assert probe._all_passed(fake_enospc) is False
    table_wide = dict(report)
    table_wide["parity_sentinel"] = {
        "target_mutation_changes_checksum": False,
        "sibling_compensation_does_not_hide": False,
    }
    assert probe._all_passed(table_wide) is False


def test_all_passed_requires_well_formed_window_parity_and_error_semantics() -> None:
    report = _required_row_report()
    assert probe._all_passed(report) is True

    missing_both = dict(report)
    candidates = dict(report["candidates"])  # type: ignore[arg-type]
    rollback = dict(candidates["shell_first_rollback"])  # type: ignore[arg-type]
    rollback["before_parity"] = None
    rollback["after_parity"] = None
    candidates["shell_first_rollback"] = rollback
    missing_both["candidates"] = candidates
    assert probe._all_passed(missing_both) is False

    empty_checksum = dict(report)
    candidates = dict(report["candidates"])  # type: ignore[arg-type]
    rollback = dict(candidates["shell_first_rollback"])  # type: ignore[arg-type]
    rollback["before_parity"] = {**_WINDOW_PARITY, "checksum": ""}
    rollback["after_parity"] = {**_WINDOW_PARITY, "checksum": ""}
    candidates["shell_first_rollback"] = rollback
    empty_checksum["candidates"] = candidates
    assert probe._all_passed(empty_checksum) is False

    zero_fail_count = dict(report)
    zero_fail_count["failure_chunk_parity"] = {**_FAIL_PARITY, "count": 0}
    assert probe._all_passed(zero_fail_count) is False

    missing_error = dict(report)
    failures = dict(report["failures"])  # type: ignore[arg-type]
    failures["missing_target"] = {
        "reconciliation": "complete_source",
        "original_sibling": True,
        "before_parity": dict(_FAIL_PARITY),
        "after_parity": dict(_FAIL_PARITY),
    }
    missing_error["failures"] = failures
    assert probe._all_passed(missing_error) is False

    lost_same_sib = dict(report)
    failures = dict(report["failures"])  # type: ignore[arg-type]
    lost = dict(failures["lost_commit_ack"])  # type: ignore[arg-type]
    lost["after"] = {"compressed": {"oid": 1, "name": "compress_hyper_2_10_chunk"}}
    lost["outcome"] = "committed"
    failures["lost_commit_ack"] = lost
    lost_same_sib["failures"] = failures
    assert probe._all_passed(lost_same_sib) is False

    move_back_source = dict(report)
    lifecycle = dict(report["lifecycle"])  # type: ignore[arg-type]
    lifecycle["move_back"] = {"reconciliation": "complete_source"}
    move_back_source["lifecycle"] = lifecycle
    assert probe._all_passed(move_back_source) is False

    no_identity = dict(report)
    no_identity["cleanup"] = {"container_absent": True, "work_root_absent": True, "identity_bound": False}
    assert probe._all_passed(no_identity) is False

    no_wal = dict(report)
    no_wal["wal"] = {"limitation": ""}
    assert probe._all_passed(no_wal) is False

    no_disappearance = dict(report)
    failures = dict(report["failures"])  # type: ignore[arg-type]
    del failures["selection_disappearance"]
    no_disappearance["failures"] = failures
    assert probe._all_passed(no_disappearance) is False

    no_index_lie = dict(report)
    boundaries = dict(report["boundaries"])  # type: ignore[arg-type]
    boundaries["no_index_origin_index_count"] = 1
    no_index_lie["boundaries"] = boundaries
    assert probe._all_passed(no_index_lie) is False

    drop_not_bound = dict(report)
    lifecycle = dict(report["lifecycle"])  # type: ignore[arg-type]
    lifecycle["drop_oids_absent"] = False
    drop_not_bound["lifecycle"] = lifecycle
    assert probe._all_passed(drop_not_bound) is False

    no_capacity = dict(report)
    failures = dict(report["failures"])  # type: ignore[arg-type]
    del failures["capacity_preflight"]
    no_capacity["failures"] = failures
    assert probe._all_passed(no_capacity) is False

    skipped_sql = dict(report)
    failures = dict(report["failures"])  # type: ignore[arg-type]
    cap = dict(failures["capacity_preflight"])  # type: ignore[arg-type]
    cold = dict(cap["cold_short"])  # type: ignore[arg-type]
    cold["shell_sql_executed"] = True
    cap["cold_short"] = cold
    failures["capacity_preflight"] = cap
    skipped_sql["failures"] = failures
    assert probe._all_passed(skipped_sql) is False

    equality_fail = dict(report)
    failures = dict(report["failures"])  # type: ignore[arg-type]
    cap = dict(failures["capacity_preflight"])  # type: ignore[arg-type]
    cap["equality"] = {"approved": False}
    failures["capacity_preflight"] = cap
    equality_fail["failures"] = failures
    assert probe._all_passed(equality_fail) is False


def test_parse_probe_report_rejects_passed_report_missing_required_rows() -> None:
    with pytest.raises(probe.ProbeError, match="required row"):
        probe.parse_probe_report(_required_row_report(failures={}))
    with pytest.raises(probe.ProbeError, match="cleanup"):
        probe.parse_probe_report(_required_row_report(cleanup={"container_absent": False}))
    parsed = probe.parse_probe_report(_required_row_report())
    assert parsed["status"] == "passed"


def test_engine_gate_does_not_overwrite_requested_image_or_run_bootstrap() -> None:
    calls: list[str] = []

    def bootstrap() -> None:
        calls.append("bootstrap")

    with pytest.raises(probe.ProbeError, match="engine identity"):
        probe.assert_engine_identity(
            requested_image_id="sha256:ad39c4fbc5c44557db1e16af10ec11e3ab12d0a472374f39aaba06ad9ca2640e",
            requested_image_ref="timescale/timescaledb-ha:pg15-latest",
            live_image_id="sha256:0000000000000000000000000000000000000000000000000000000000000000",
            live_image_ref="timescale/timescaledb-ha:pg15-latest",
            used_image_id="sha256:ad39c4fbc5c44557db1e16af10ec11e3ab12d0a472374f39aaba06ad9ca2640e",
            used_image_ref="timescale/timescaledb-ha:pg15-latest",
        )
        bootstrap()
    assert calls == []


def test_window_parity_sql_on_probe_seam_is_chunk_scoped() -> None:
    sql = probe.fixture_window_parity_sql("hydro", "river_timeseries")
    assert "valid_time >= %s AND valid_time < %s" in sql
    assert '"hydro"."river_timeseries"' in sql
    assert "payload IS NULL" in sql
    assert "CASE WHEN payload IS NULL THEN 'N' ELSE 'P' || payload END" in sql
    assert probe.PROBE_FIXTURE_PARITY_COLUMNS == ("id", "valid_time", "value", "payload")
    assert not hasattr(probe, "window_parity_sql")


def test_ack_loss_wrapper_commits_then_makes_the_moving_connection_unusable() -> None:
    from packages.common.compressed_chunk_cold_probe.shell import _AckLossConnection

    class _FakeConnection:
        def __init__(self) -> None:
            self.committed = False
            self.closed = False
            self.rolled_back = False

        def commit(self) -> None:
            self.committed = True

        def close(self) -> None:
            self.closed = True

        def rollback(self) -> None:
            self.rolled_back = True

        def cursor(self) -> object:
            return object()

    fake = _FakeConnection()
    wrapper = _AckLossConnection(fake)
    with pytest.raises(probe.CommitAckLost, match="acknowledgement lost"):
        wrapper.commit()
    assert fake.committed is True
    assert fake.closed is True
    assert wrapper.commit_ack_lost is True
    with pytest.raises(probe.ProbeError, match="unusable"):
        wrapper.rollback()
    with pytest.raises(probe.ProbeError, match="unusable"):
        wrapper.cursor()
    wrapper.close()
    assert fake.rolled_back is False


def test_require_migrate_plan_fails_closed_when_the_plan_is_not_executable() -> None:
    from packages.common.compressed_chunk_cold_probe.catalog import (
        require_migrate_plan,
        synthetic_complete_group,
    )

    plan = require_migrate_plan(synthetic_complete_group(), target="nhms_cold")
    assert plan.kind == "migrate"
    assert plan.shell_move_sql
    assert "SET TABLESPACE" in plan.shell_move_sql[0]
    assert '"nhms_cold"' in plan.shell_move_sql[0]
    with pytest.raises(probe.ProbeError, match="required migrate plan"):
        require_migrate_plan(synthetic_complete_group(tablespace="nhms_cold"), target="nhms_cold")


def test_all_passed_rejects_clean_commit_relabelled_as_lost_ack() -> None:
    report = _required_row_report()
    failures = dict(report["failures"])  # type: ignore[arg-type]
    lost = dict(failures["lost_commit_ack"])  # type: ignore[arg-type]
    lost["commit_ack_lost"] = False
    lost["error"] = None
    failures["lost_commit_ack"] = lost
    report["failures"] = failures
    assert probe._all_passed(report) is False
    lost["commit_ack_lost"] = True
    lost["error"] = {"error_type": "CommitAckLost", "error": "commit acknowledgement lost after server commit"}
    failures["lost_commit_ack"] = lost
    report["failures"] = failures
    assert probe._all_passed(report) is True


def test_all_passed_rejects_lost_ack_without_commit_ack_lost_error() -> None:
    report = _required_row_report()
    failures = dict(report["failures"])  # type: ignore[arg-type]
    lost = dict(failures["lost_commit_ack"])  # type: ignore[arg-type]
    lost["error"] = {"error_type": "OperationalError", "error": "server closed the connection unexpectedly"}
    failures["lost_commit_ack"] = lost
    report["failures"] = failures
    assert probe._all_passed(report) is False


def test_all_passed_rejects_select_one_and_wrong_fault_bindings() -> None:
    report = _required_row_report()
    failures = dict(report["failures"])  # type: ignore[arg-type]
    missing = dict(failures["missing_target"])  # type: ignore[arg-type]
    missing["sql"] = "SELECT 1"
    missing["exec"] = {"ok": False, "error_type": "UndefinedObject", "error": "does not exist", "sql": "SELECT 1"}
    failures["missing_target"] = missing
    report["failures"] = failures
    assert probe._all_passed(report) is False

    report = _required_row_report()
    failures = dict(report["failures"])  # type: ignore[arg-type]
    permission = dict(failures["permission"])  # type: ignore[arg-type]
    permission["target"] = "nhms_cold_missing"
    permission["sql"] = 'ALTER TABLE x SET TABLESPACE "nhms_cold_missing"'
    failures["permission"] = permission
    report["failures"] = failures
    assert probe._all_passed(report) is False

    report = _required_row_report()
    failures = dict(report["failures"])  # type: ignore[arg-type]
    full = dict(failures["full_target"])  # type: ignore[arg-type]
    full["genuine_enospc"] = True
    full["error_type"] = "DiskFull"
    full["error"] = "No space left on device"
    full["sql"] = "SELECT 1"
    full["move"] = {"ok": True, "sql": "SELECT 1"}
    failures["full_target"] = full
    report["failures"] = failures
    assert probe._all_passed(report) is False
    report = _required_row_report()
    assert probe._all_passed(report) is True


def test_all_passed_rejects_missing_no_index_group_and_none_attach() -> None:
    report = _required_row_report()
    boundaries = dict(report["boundaries"])  # type: ignore[arg-type]
    boundaries["no_index_group"] = None
    report["boundaries"] = boundaries
    assert probe._all_passed(report) is False

    report = _required_row_report()
    boundaries = dict(report["boundaries"])  # type: ignore[arg-type]
    boundaries["attach_tablespace"] = None
    report["boundaries"] = boundaries
    assert probe._all_passed(report) is False


def _with_after_decompress(
    report: dict[str, object],
    path: tuple[str, ...],
    *,
    members: list[dict[str, object]],
    proof: dict[str, object] | None,
) -> dict[str, object]:
    current: dict[str, object] = report
    copies: list[tuple[dict[str, object], str, dict[str, object]]] = []
    for key in path:
        child = dict(current[key])  # type: ignore[arg-type]
        copies.append((current, key, child))
        current = child
    if proof is None:
        current.pop("after_decompress_proof", None)
        current["phases"] = {}
    else:
        current["phases"] = {"after_decompress": {"members": members}}
        current["after_decompress_proof"] = proof
    for parent, key, child in reversed(copies):
        parent[key] = child
    return report


def test_all_passed_rejects_mixed_or_hot_after_decompress_and_inverse_cold_classifier() -> None:
    mixed_proof = {
        "target": "nhms_cold",
        "all_requested_target": False,
        "pg_default_bytes": 8192,
        "member_count": 2,
    }
    report = _with_after_decompress(
        _required_row_report(),
        ("candidates", "shell_first_rollback"),
        members=_MIXED_EXPANDED_MEMBERS,
        proof=mixed_proof,
    )
    assert probe._all_passed(report) is False
    report = _with_after_decompress(
        _required_row_report(),
        ("lifecycle", "committed_move"),
        members=_HOT_EXPANDED_MEMBERS,
        proof={**mixed_proof, "pg_default_bytes": 73728},
    )
    assert probe._all_passed(report) is False
    report = _with_after_decompress(
        _required_row_report(),
        ("lifecycle", "move_back"),
        members=_COLD_EXPANDED_MEMBERS,
        proof={"target": "nhms_cold", "all_requested_target": True, "pg_default_bytes": 0, "member_count": 3},
    )
    assert probe._all_passed(report) is False
    report = _with_after_decompress(
        _required_row_report(),
        ("candidates", "shell_first_rollback"),
        members=_MIXED_EXPANDED_MEMBERS,
        proof=None,
    )
    assert probe._all_passed(report) is False
    report = _with_after_decompress(
        _required_row_report(),
        ("candidates", "shell_first_rollback"),
        members=_MIXED_EXPANDED_MEMBERS,
        proof={"target": "nhms_cold", "all_requested_target": True, "pg_default_bytes": 0, "member_count": 2},
    )
    assert probe._all_passed(report) is False


def test_catalog_path_preflight_refuses_wrong_expected_path() -> None:
    ok = probe.validate_catalog_path_preflight(
        catalog_location="/home/postgres/pgdata/tablespaces/nhms_cold",
        expected_location="/home/postgres/pgdata/tablespaces/nhms_cold",
    )
    assert ok["ok"] is True
    refused = probe.validate_catalog_path_preflight(
        catalog_location="/home/postgres/pgdata/tablespaces/nhms_cold",
        expected_location="/tmp/wrong-cold-path",
    )
    assert refused["ok"] is False
    assert refused["refused"] is True


def test_select_sequence_accepts_only_shell_first() -> None:
    selected = probe.select_sequence(
        {
            "shell_first": {
                "complete": True,
                "rolled_back_fresh": True,
                "reconciliation": "complete_source",
                "original_sibling": True,
                "before_parity": {"count": 1, "checksum": "a"},
                "after_parity": {"count": 1, "checksum": "a"},
                "before": {"compressed": {"oid": 1, "name": "compress_hyper_2_12_chunk"}},
                "after": {"compressed": {"oid": 1, "name": "compress_hyper_2_12_chunk"}},
            },
            "move_chunk": {"complete": False, "error": "function must be run on the access node only"},
            "direct_compressed_alter": {
                "complete": False,
                "error": "changing tablespace of compressed chunk is not supported",
            },
        }
    )
    assert selected["accepted"] == ACCEPTED_SEQUENCE_NAME
    assert "alter_tablespace_oid_order" in selected["rejected"]
    blocked = probe.select_sequence(
        {
            "shell_first": {"complete": False, "error": "boom"},
            "move_chunk": {"complete": False},
            "direct_compressed_alter": {"complete": False},
        }
    )
    assert blocked["accepted"] is None
    assert blocked["blocker"] is True


def test_main_refuse_only_writes_refused_status(tmp_path: Path) -> None:
    output = tmp_path / "report.json"
    code = probe.main(
        [
            "--mode",
            "refuse-only",
            "--container-name",
            LIVE_CONTAINER_NAME,
            "--output",
            str(output),
        ]
    )
    assert code == 2
    document = json.loads(output.read_text(encoding="utf-8"))
    assert document["status"] == "refused"
    assert "nhms-db" in document["error"]


def test_main_unit_plan_is_local_and_does_not_require_docker(tmp_path: Path) -> None:
    output = tmp_path / "plan.json"
    code = probe.main(["--mode", "unit-plan", "--output", str(output)])
    assert code == 0
    document = json.loads(output.read_text(encoding="utf-8"))
    assert document["accepted_sequence"] == ACCEPTED_SEQUENCE_NAME
    assert document["status"] == "passed"


@pytest.mark.integration
@pytest.mark.timescaledb_210
def test_isolated_cluster_probe_is_opt_in() -> None:
    pytest.importorskip("psycopg2")
    if not Path("/.dockerenv").exists() and not Path("/var/run/docker.sock").exists():
        pytest.skip("isolated-cluster probe requires Docker on the node-27 oracle")
    output = Path("/tmp") / "nhms-1892-probe-pytest-report.json"
    code = probe.main(
        [
            "--mode",
            "isolated-cluster",
            "--host-port",
            "55494",
            "--output",
            str(output),
        ]
    )
    document = probe.parse_probe_report(output.read_text(encoding="utf-8"))
    assert document["cleanup"]["container_absent"] is True
    assert document["cleanup"]["work_root_absent"] is True
    assert document["status"] == "passed"
    assert document["sequence"]["accepted"] == ACCEPTED_SEQUENCE_NAME
    assert document["engine_gate"]["image_pin_ok"] is True
    assert document["engine_gate"]["pg_matches_pin"] is True
    assert document["engine_gate"]["ts_matches_pin"] is True
    assert document["failures"]["catalog_path_mismatch"]["refused"] is True
    assert document["failures"]["pre_commit_interrupt"]["reconciliation"] == "complete_source"
    assert document["failures"]["lost_commit_ack"]["reconciliation"] == "complete_target"
    assert document["failures"]["lost_commit_ack"]["replayed"] is False
    assert document["failures"]["lost_commit_ack"]["outcome"] == "committed_ack_lost"
    assert document["failures"]["lost_commit_ack"]["commit_ack_lost"] is True
    assert document["failures"]["lost_commit_ack"]["error"]["error_type"] == "CommitAckLost"
    assert document["failures"]["missing_target"]["target"] == "nhms_cold_missing"
    assert document["failures"]["missing_target"]["exec"]["error_type"] == "UndefinedObject"
    assert document["failures"]["permission"]["target"] == "nhms_cold"
    assert document["failures"]["permission"]["error_type"] == "InsufficientPrivilege"
    assert document["failures"]["full_target"]["genuine_enospc"] is True
    assert document["failures"]["full_target"]["move"]["error_type"] == "DiskFull"
    assert "SELECT 1" not in str(document["failures"]["full_target"].get("sql") or "")
    assert document["boundaries"]["attach_tablespace"] == []
    assert document["boundaries"]["no_index_group"]["members"]
    assert "injected_missing_relation_error" in document["failures"]
    assert document["failures"]["selection_disappearance"]["stale_blocked"] is True
    assert document["failures"]["selection_disappearance"]["sacrificed_group_gone"] is True
    assert int((document.get("failure_chunk_parity") or {}).get("count") or 0) > 0
    assert document["boundaries"]["no_index_origin_index_count"] == 0
    assert document["lifecycle"]["drop_oids_absent"] is True
    assert document["cleanup"]["identity_bound"] is True
    cap = document["failures"]["capacity_preflight"]
    assert cap["positive"]["approved"] is True
    assert cap["equality"]["approved"] is True
    assert cap["cold_short"]["approved"] is False
    assert cap["cold_short"]["shell_sql_executed"] is False
    assert cap["hot_short"]["approved"] is False
    assert cap["hot_short"]["shell_sql_executed"] is False
    assert document["parity_sentinel"]["target_mutation_changes_checksum"] is True
    assert document["cleanup"]["container_absent"] is True
    assert document["cleanup"]["work_root_absent"] is True
    assert code == 0
