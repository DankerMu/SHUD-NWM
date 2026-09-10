"""Permission-matrix and merge-source contracts for readonly DB validation. Helpers imported from the core suite."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

import pytest

from packages.common.node27_issue1895_readonly_accept import accept_c2_evidence
from services.production_closure.readonly_db_validation import (
    ProbeTarget,
    PsycopgReadonlyDbProbeAdapter,
    ReadonlyDbValidationConfig,
    ReadonlyDbValidationError,
    merge_readonly_db_source_evidence,
    run_permission_probe_matrix,
    validate_readonly_db_boundary,
)
from tests.test_readonly_db_validation import (
    REPO_ROOT,
    _deep_nested_json,
    _evidence_root,
    _FakeReadonlyAdapter,
    _is_under_approved_evidence_root,
    _passing_manual_actions,
    _passing_route_requester,
    _promote_simulated_summary_to_live,
    _run_id,
    _seed_live_readonly_source,
    _write_json,
)


def test_writer_privilege_marks_validation_fail_even_when_probe_denied() -> None:
    target = "hydro.hydro_run"
    adapter = _FakeReadonlyAdapter(privileges={target: {"insert": True, "update": False, "delete": False}})
    config = ReadonlyDbValidationConfig.from_env(
        evidence_root=_evidence_root(),
        run_id=_run_id("writer"),
        database_url="postgresql://writer:secret@db.example/nhms",
        force=True,
    )

    summary = validate_readonly_db_boundary(
        config,
        adapter=adapter,
        route_requester=_passing_route_requester,
        manual_action_probe_runner=_passing_manual_actions,
    )

    assert summary["status"] == "FAIL"
    assert summary["role"]["role_type"] == "writer_or_mutating"
    hydro = next(item for item in summary["permission_probes"] if item["target"] == target)
    insert = next(item for item in hydro["operations"] if item["operation"] == "INSERT")
    assert insert["status"] == "FAIL"
    assert insert["reason"] == "tested_credential_has_mutating_table_privilege"
    assert insert["execution_outcome"] == "not_executed_due_to_catalog_mutating_privilege"
    assert not any(spec.target and spec.target.qualified_name == target for spec in adapter.executed_specs)


@pytest.mark.parametrize(
    ("operation", "privilege", "ddl_suffix"),
    [
        ("TRUNCATE", "truncate", "truncate-grant"),
        ("REFERENCES", "references", "references-grant"),
        ("TRIGGER", "trigger", "trigger-grant"),
        ("MAINTAIN", "maintain", "maintain-grant"),
    ],
)
def test_table_catalog_only_privilege_marks_validation_fail_without_executing_any_probe(
    operation: str,
    privilege: str,
    ddl_suffix: str,
) -> None:
    target = "hydro.hydro_run"
    adapter = _FakeReadonlyAdapter(privileges={target: {privilege: True}})

    probes = run_permission_probe_matrix(adapter, ddl_suffix=ddl_suffix)

    target_result = next(item for item in probes if item["target"] == target)
    operation_probe = next(item for item in target_result["operations"] if item["operation"] == operation)
    assert target_result["status"] == "FAIL"
    assert operation_probe["status"] == "FAIL"
    assert operation_probe["table_privilege_allowed"] is True
    assert operation_probe["table_privilege"] == privilege
    assert operation_probe["reason"] == "tested_credential_has_mutating_table_privilege"
    assert operation_probe["execution_outcome"] == "not_executed_due_to_catalog_mutating_privilege"
    assert adapter.executed_specs == []


def test_psycopg_reachable_role_discovery_has_no_silent_depth_cap() -> None:
    class FakeCursor:
        executed_query = ""

        def execute(self, query: str) -> None:
            self.executed_query = query

        def fetchall(self) -> list[dict[str, Any]]:
            return []

    cursor = FakeCursor()
    adapter = PsycopgReadonlyDbProbeAdapter("postgresql://readonly:secret@db.example/nhms", ddl_suffix="roles")

    assert adapter._reachable_roles(cursor, membership_columns={"set_option", "inherit_option"}) == []

    assert "NOT m.roleid = ANY(reachable.path)" in cursor.executed_query
    assert "reachable.depth <" not in cursor.executed_query


def test_psycopg_adapter_checks_current_database_create_for_current_user_and_reachable_role() -> None:
    class FakeCursor:
        def __init__(self) -> None:
            self.calls: list[tuple[str, tuple[Any, ...]]] = []

        def execute(self, query: str, params: tuple[Any, ...] = ()) -> None:
            self.calls.append((query, params))

        def fetchone(self) -> dict[str, Any]:
            return {"database_name": "nhms", "create": True}

    cursor = FakeCursor()
    adapter = PsycopgReadonlyDbProbeAdapter("postgresql://readonly:secret@db.example/nhms", ddl_suffix="db-create")

    current_user_result = adapter._database_privileges_for_current_user(cursor)
    reachable_role_result = adapter._database_privileges_for_role(cursor, "readonly_parent")

    current_user_query = " ".join(cursor.calls[0][0].split())
    reachable_role_query = " ".join(cursor.calls[1][0].split())
    assert current_user_result == {"database_name": "nhms", "create": True}
    assert reachable_role_result == {"database_name": "nhms", "create": True}
    assert "has_database_privilege(current_user, current_database(), 'CREATE')" in current_user_query
    assert "has_database_privilege(%s, current_database(), 'CREATE')" in reachable_role_query
    assert cursor.calls[1][1] == ("readonly_parent",)


def test_psycopg_adapter_audited_schema_sequence_inventory_scans_all_sequences() -> None:
    class FakeCursor:
        def __init__(self) -> None:
            self.calls: list[tuple[str, tuple[Any, ...]]] = []

        def execute(self, query: str, params: tuple[Any, ...] = ()) -> None:
            self.calls.append((query, params))

        def fetchall(self) -> list[dict[str, Any]]:
            return [
                {
                    "sequence_schema": "ops",
                    "sequence_name": "readonly_escape_seq",
                    "qualified_name": "ops.readonly_escape_seq",
                    "usage": True,
                    "update": False,
                }
            ]

    cursor = FakeCursor()
    adapter = PsycopgReadonlyDbProbeAdapter("postgresql://readonly:secret@db.example/nhms", ddl_suffix="seq")

    result = adapter._audited_schema_sequence_privileges_for_current_user(cursor, ("hydro", "met", "ops"))

    query = " ".join(cursor.calls[0][0].split())
    assert "FROM pg_class seq" in query
    assert "seq.relkind = 'S'" in query
    assert "seq_ns.nspname = ANY(%s)" in query
    assert "pg_depend" not in query
    assert cursor.calls[0][1] == (["hydro", "met", "ops"],)
    assert result[0]["qualified_name"] == "ops.readonly_escape_seq"
    assert result[0]["mutating_privilege_allowed"] is True


def test_deep_reachable_writer_role_membership_fails_without_set_role_or_probes() -> None:
    adapter = _FakeReadonlyAdapter(
        reachable_role_findings=[
            {
                "role_name": "nhms_writer",
                "reachable_via": ["set_role"],
                "membership_depth": 9,
                "unsafe_role_attributes": {},
                "mutating_privilege_findings": [
                    {
                        "target": "ops.pipeline_job",
                        "operation": "UPDATE",
                        "reason": "reachable_role_has_mutating_table_privilege",
                    }
                ],
                "reason": "reachable_role_has_mutating_capability",
            }
        ]
    )
    config = ReadonlyDbValidationConfig.from_env(
        evidence_root=_evidence_root(),
        run_id=_run_id("reachable-role"),
        database_url="postgresql://display_ro:secret@db.example/nhms",
        force=True,
    )

    summary = validate_readonly_db_boundary(
        config,
        adapter=adapter,
        route_requester=_passing_route_requester,
        manual_action_probe_runner=_passing_manual_actions,
    )

    assert summary["status"] == "FAIL"
    assert summary["role"]["role_type"] == "writer_or_mutating"
    assert summary["role"]["reachable_role_findings"][0]["role_name"] == "nhms_writer"
    assert summary["role"]["reachable_role_findings"][0]["membership_depth"] > 8
    role_probe = next(item for item in summary["permission_probes"] if item["target"] == "reachable_roles")
    assert role_probe["status"] == "FAIL"
    operation = role_probe["operations"][0]
    assert operation["operation"] == "REACHABLE_ROLE_MEMBERSHIP"
    assert operation["execution_outcome"] == "not_executed_role_membership_catalog_only"
    assert adapter.executed_specs == []


def test_mutating_probe_success_is_fail_and_rollback_is_cleanup_only() -> None:
    target = "ops.pipeline_event"
    adapter = _FakeReadonlyAdapter(successful_operations={(target, "DELETE")})

    first = run_permission_probe_matrix(adapter, ddl_suffix="first")
    second = run_permission_probe_matrix(adapter, ddl_suffix="second")

    first_event = next(item for item in first if item["target"] == target)
    delete_probe = next(item for item in first_event["operations"] if item["operation"] == "DELETE")
    assert delete_probe["execution_outcome"] == "succeeded"
    assert delete_probe["rolled_back"] is True
    assert delete_probe["status"] == "FAIL"
    assert delete_probe["reason"] == "mutating_probe_executed_successfully_before_rollback"
    assert adapter.persisted_mutations == 0
    assert len(first) == len(second)


def test_column_level_mutating_grant_fails_without_executing_dml() -> None:
    target = "hydro.river_timeseries"
    adapter = _FakeReadonlyAdapter(column_privileges={target: {"insert": [], "update": ["q_cms"]}})

    probes = run_permission_probe_matrix(adapter, ddl_suffix="column-grant")

    target_result = next(item for item in probes if item["target"] == target)
    update_probe = next(item for item in target_result["operations"] if item["operation"] == "UPDATE")
    assert target_result["status"] == "FAIL"
    assert update_probe["status"] == "FAIL"
    assert update_probe["reason"] == "tested_credential_has_mutating_column_privilege"
    assert update_probe["column_privilege_allowed"] is True
    assert update_probe["column_privilege_columns"] == ["q_cms"]
    assert update_probe["execution_outcome"] == "not_executed_due_to_catalog_mutating_privilege"
    assert not any(spec.target and spec.target.qualified_name == target for spec in adapter.executed_specs)


@pytest.mark.parametrize(
    "grant",
    [
        {"usage": True, "update": False},
        {"usage": False, "update": True},
    ],
)
def test_sequence_mutating_grant_fails_without_executing_probes(grant: dict[str, bool]) -> None:
    target = "ops.pipeline_event"
    adapter = _FakeReadonlyAdapter(
        sequence_privileges={
            target: [
                {
                    "sequence_schema": "ops",
                    "sequence_name": "pipeline_event_event_id_seq",
                    "qualified_name": "ops.pipeline_event_event_id_seq",
                    "columns": ["event_id"],
                    **grant,
                }
            ]
        }
    )
    config = ReadonlyDbValidationConfig.from_env(
        evidence_root=_evidence_root(),
        run_id=_run_id("sequence-grant"),
        database_url="postgresql://writer:secret@db.example/nhms",
        force=True,
    )

    summary = validate_readonly_db_boundary(
        config,
        adapter=adapter,
        route_requester=_passing_route_requester,
        manual_action_probe_runner=_passing_manual_actions,
    )

    assert summary["status"] == "FAIL"
    assert summary["role"]["role_type"] == "writer_or_mutating"
    role_finding = next(
        finding
        for finding in summary["role"]["mutating_privilege_findings"]
        if finding["target"] == target and finding["operation"] == "SEQUENCE_USAGE_UPDATE"
    )
    assert role_finding["reason"] == "tested_credential_has_mutating_sequence_privilege"
    assert role_finding["sequences"][0]["qualified_name"] == "ops.pipeline_event_event_id_seq"
    event_probe = next(item for item in summary["permission_probes"] if item["target"] == target)
    sequence_probe = next(item for item in event_probe["operations"] if item["operation"] == "SEQUENCE_USAGE_UPDATE")
    assert event_probe["status"] == "FAIL"
    assert event_probe["sequence_privileges"][0]["mutating_privilege_allowed"] is True
    assert sequence_probe["status"] == "FAIL"
    assert sequence_probe["sequence_privilege_allowed"] is True
    assert sequence_probe["execution_outcome"] == "not_executed_due_to_catalog_mutating_privilege"
    assert sequence_probe["reason"] == "tested_credential_has_mutating_sequence_privilege"
    for operation in event_probe["operations"]:
        if operation["operation"] in {"INSERT", "UPDATE", "DELETE"}:
            assert operation["execution_outcome"] == "not_executed_due_to_target_catalog_mutating_privilege"
    ddl_probe = next(item for item in summary["permission_probes"] if item["target"] == "ops.*")
    ddl_operation = ddl_probe["operations"][0]
    assert ddl_operation["operation"] == "DDL_CREATE_TABLE"
    assert ddl_operation["execution_outcome"] == "not_executed_due_to_sequence_mutating_privilege"
    assert adapter.executed_specs == []


def test_late_target_sequence_grant_prevents_dml_and_ddl_across_whole_matrix() -> None:
    adapter = _FakeReadonlyAdapter(
        sequence_privileges={
            "ops.pipeline_event": [
                {
                    "sequence_schema": "ops",
                    "sequence_name": "pipeline_event_event_id_seq",
                    "qualified_name": "ops.pipeline_event_event_id_seq",
                    "columns": ["event_id"],
                    "usage": True,
                    "update": False,
                }
            ]
        }
    )

    probes = run_permission_probe_matrix(adapter, ddl_suffix="late-sequence")

    assert adapter.executed_specs == []
    mutating_operations = [
        operation
        for item in probes
        for operation in item["operations"]
        if operation["operation"] in {"INSERT", "UPDATE", "DELETE", "DDL_CREATE_TABLE"}
    ]
    assert mutating_operations
    assert all(str(operation["execution_outcome"]).startswith("not_executed") for operation in mutating_operations)


def test_database_create_grant_fails_without_executing_dml_or_ddl() -> None:
    adapter = _FakeReadonlyAdapter(database_privileges={"create": True})
    config = ReadonlyDbValidationConfig.from_env(
        evidence_root=_evidence_root(),
        run_id=_run_id("database-create"),
        database_url="postgresql://writer:secret@db.example/nhms",
        force=True,
    )

    summary = validate_readonly_db_boundary(
        config,
        adapter=adapter,
        route_requester=_passing_route_requester,
        manual_action_probe_runner=_passing_manual_actions,
    )

    assert summary["status"] == "FAIL"
    assert summary["role"]["role_type"] == "writer_or_mutating"
    db_probe = next(
        item for item in summary["permission_probes"] if item["surface"] == "current_database_create_catalog"
    )
    operation = db_probe["operations"][0]
    assert operation["operation"] == "DATABASE_CREATE"
    assert operation["status"] == "FAIL"
    assert operation["database_privilege_allowed"] is True
    assert operation["reason"] == "tested_credential_has_database_create_privilege"
    assert operation["execution_outcome"] == "not_executed_due_to_catalog_mutating_privilege"
    mutating_operations = [
        probe_operation
        for item in summary["permission_probes"]
        for probe_operation in item["operations"]
        if probe_operation["operation"] in {"INSERT", "UPDATE", "DELETE", "DDL_CREATE_TABLE"}
    ]
    assert mutating_operations
    assert all(
        str(probe_operation["execution_outcome"]).startswith("not_executed") for probe_operation in mutating_operations
    )
    assert adapter.executed_specs == []


def test_reachable_role_database_create_grant_fails_without_executing_dml_or_ddl() -> None:
    adapter = _FakeReadonlyAdapter(
        reachable_role_findings=[
            {
                "role_name": "readonly_parent",
                "reachable_via": ["inherit"],
                "membership_depth": 1,
                "unsafe_role_attributes": {},
                "mutating_privilege_findings": [
                    {
                        "target": "nhms",
                        "operation": "DATABASE_CREATE",
                        "reason": "reachable_role_has_database_create_privilege",
                        "database_name": "nhms",
                    }
                ],
                "reason": "reachable_role_has_mutating_capability",
            }
        ]
    )

    probes = run_permission_probe_matrix(adapter, ddl_suffix="reachable-db-create")

    role_probe = next(item for item in probes if item["target"] == "reachable_roles")
    assert role_probe["status"] == "FAIL"
    assert role_probe["operations"][0]["mutating_privilege_findings"][0]["operation"] == "DATABASE_CREATE"
    assert adapter.executed_specs == []


def test_standalone_audited_schema_sequence_grant_fails_without_executing_probes() -> None:
    adapter = _FakeReadonlyAdapter(
        audited_schema_sequence_privileges=[
            {
                "sequence_schema": "ops",
                "sequence_name": "readonly_escape_seq",
                "qualified_name": "ops.readonly_escape_seq",
                "usage": True,
                "update": False,
            }
        ]
    )
    config = ReadonlyDbValidationConfig.from_env(
        evidence_root=_evidence_root(),
        run_id=_run_id("standalone-sequence"),
        database_url="postgresql://writer:secret@db.example/nhms",
        force=True,
    )

    summary = validate_readonly_db_boundary(
        config,
        adapter=adapter,
        route_requester=_passing_route_requester,
        manual_action_probe_runner=_passing_manual_actions,
    )

    assert summary["status"] == "FAIL"
    assert summary["role"]["role_type"] == "writer_or_mutating"
    sequence_probe = next(item for item in summary["permission_probes"] if item["target"] == "audited_schema_sequences")
    operation = sequence_probe["operations"][0]
    assert operation["operation"] == "AUDITED_SCHEMA_SEQUENCE_USAGE_UPDATE"
    assert operation["status"] == "FAIL"
    assert operation["sequence_privilege_allowed"] is True
    assert operation["sequence_privilege_sequences"][0]["qualified_name"] == "ops.readonly_escape_seq"
    assert operation["reason"] == "tested_credential_has_mutating_sequence_privilege"
    ddl_probe = next(item for item in summary["permission_probes"] if item["target"] == "ops.*")
    assert ddl_probe["operations"][0]["execution_outcome"] == "not_executed_due_to_sequence_mutating_privilege"
    assert adapter.executed_specs == []


def test_reachable_role_standalone_sequence_grant_fails_without_executing_dml_or_ddl() -> None:
    adapter = _FakeReadonlyAdapter(
        reachable_role_findings=[
            {
                "role_name": "readonly_parent",
                "reachable_via": ["set_role"],
                "membership_depth": 1,
                "unsafe_role_attributes": {},
                "mutating_privilege_findings": [
                    {
                        "target": "audited_schema_sequences",
                        "operation": "AUDITED_SCHEMA_SEQUENCE_USAGE_UPDATE",
                        "reason": "reachable_role_has_mutating_sequence_privilege",
                        "sequences": [
                            {
                                "sequence_schema": "met",
                                "sequence_name": "sibling_escape_seq",
                                "qualified_name": "met.sibling_escape_seq",
                                "columns": [],
                                "usage": False,
                                "update": True,
                            }
                        ],
                    }
                ],
                "reason": "reachable_role_has_mutating_capability",
            }
        ]
    )

    probes = run_permission_probe_matrix(adapter, ddl_suffix="reachable-sequence")

    role_probe = next(item for item in probes if item["target"] == "reachable_roles")
    assert role_probe["status"] == "FAIL"
    finding = role_probe["operations"][0]["mutating_privilege_findings"][0]
    assert finding["operation"] == "AUDITED_SCHEMA_SEQUENCE_USAGE_UPDATE"
    assert finding["sequences"][0]["qualified_name"] == "met.sibling_escape_seq"
    assert adapter.executed_specs == []


@pytest.mark.parametrize("schema", ["hydro", "met", "ops"])
def test_schema_create_grant_fails_without_executing_dml_or_ddl(schema: str) -> None:
    adapter = _FakeReadonlyAdapter(schema_privileges_by_schema={schema: {"create": True}})

    probes = run_permission_probe_matrix(adapter, ddl_suffix="schema-grant")

    ddl = next(item for item in probes if item["target"] == f"{schema}.*")
    operation = ddl["operations"][0]
    assert ddl["status"] == "FAIL"
    assert operation["status"] == "FAIL"
    assert operation["reason"] == "tested_credential_has_schema_create_privilege"
    assert operation["execution_outcome"] == "not_executed_due_to_catalog_mutating_privilege"
    assert adapter.executed_specs == []


def test_denied_dml_and_ddl_without_mutating_grants_pass_permission_probes() -> None:
    adapter = _FakeReadonlyAdapter()

    probes = run_permission_probe_matrix(adapter, ddl_suffix="denied")

    assert all(item["status"] == "PASS" for item in probes)
    operations = [operation for item in probes for operation in item["operations"]]
    assert operations
    assert all(operation["status"] == "PASS" for operation in operations)
    denial_operations = [
        operation
        for operation in operations
        if operation["operation"] in {"INSERT", "UPDATE", "DELETE", "DDL_CREATE_TABLE"}
    ]
    assert denial_operations
    assert all(operation["reason"].endswith("_denied_before_commit") for operation in denial_operations)


def test_merge_readonly_db_source_evidence_writes_source_complete_final_lane() -> None:
    evidence_root = _evidence_root()
    run_id = _run_id("merge-sources")
    gfs_config = ReadonlyDbValidationConfig.from_env(
        evidence_root=evidence_root,
        run_id=f"{run_id}-gfs",
        database_url="postgresql://display:secret@db.example/nhms",
        source="GFS",
        cycle_time="2026-05-03T00:00:00+00:00",
        strict_run_id="run-gfs",
        model_id="model-gfs",
        job_id="job-gfs",
        force=True,
    )
    ifs_config = ReadonlyDbValidationConfig.from_env(
        evidence_root=evidence_root,
        run_id=f"{run_id}-ifs",
        database_url="postgresql://display:secret@db.example/nhms",
        source="IFS",
        cycle_time="2026-05-04T00:00:00+00:00",
        strict_run_id="run-ifs",
        model_id="model-ifs",
        job_id="job-ifs",
        force=True,
    )
    gfs_summary = validate_readonly_db_boundary(
        gfs_config,
        adapter=_FakeReadonlyAdapter(role_overrides={"current_user": "nhms_display_ro"}),
        route_requester=_passing_route_requester,
        manual_action_probe_runner=_passing_manual_actions,
    )
    ifs_summary = validate_readonly_db_boundary(
        ifs_config,
        adapter=_FakeReadonlyAdapter(role_overrides={"current_user": "nhms_display_ro"}),
        route_requester=_passing_route_requester,
        manual_action_probe_runner=_passing_manual_actions,
    )
    _promote_simulated_summary_to_live(gfs_config, gfs_summary)
    _promote_simulated_summary_to_live(ifs_config, ifs_summary)

    summary = merge_readonly_db_source_evidence(
        evidence_root=evidence_root,
        run_id=run_id,
        source_dirs=(gfs_config.lane_dir, ifs_config.lane_dir),
        force=True,
    )

    assert summary["status"] == "PASS"
    provenance = summary["validation_provenance"]
    assert provenance["merged_source_evidence"] is True
    assert provenance["declared_sources"] == ["GFS", "IFS"]
    assert provenance["reduced_scope"] is False
    assert provenance["source_bundle_count"] == 2
    assert set(summary["display_identity"]) == {"GFS", "IFS"}
    assert {route.get("source") for route in summary["route_smoke"] if route.get("source")} == {"GFS", "IFS"}
    assert summary["validation_provenance"]["source_artifacts"]
    source_artifact = summary["validation_provenance"]["source_artifacts"][0]
    assert source_artifact["validation_provenance"]["mode"] == "live"
    assert source_artifact["validation_provenance"]["live_readonly_proof"] is True
    assert source_artifact["parent_binding"] == "run_id_prefix"
    lane = evidence_root / run_id / "db" / "readonly-db-boundary"
    assert (lane / "summary.json").is_file()
    for filename in ("summary.json", "role.json", "route_smoke.json", "permission_probes.json"):
        assert (lane / filename).stat().st_mode & 0o777 == 0o600
    receipt_parent = evidence_root / f"c2-receipt-{run_id}"
    receipt_parent.mkdir(mode=0o700)
    os.chmod(receipt_parent, 0o700)
    accepted = accept_c2_evidence(
        evidence_root=evidence_root,
        run_id=run_id,
        receipt_path=receipt_parent / "c2.json",
        head_sha="a" * 40,
        reviewed_sha="a" * 40,
        now=lambda: "2026-09-06T12:00:00Z",
    )
    assert accepted["checks"]["summary_full_gfs_ifs_scope"] == {
        "merged_source_evidence": True,
        "declared_sources": ["GFS", "IFS"],
        "reduced_scope": False,
        "source_bundle_count": 2,
        "source_artifact_sources": [["GFS"], ["IFS"]],
        "display_identity_sources": ["GFS", "IFS"],
    }


@pytest.mark.parametrize(
    ("mutator", "error_code"),
    [
        ("forged_summary_no_siblings", "READONLY_DB_MERGE_SOURCE_MISSING"),
        ("sibling_mismatch", "READONLY_DB_MERGE_SOURCE_SIBLING_MISMATCH"),
        ("simulated_schema", "READONLY_DB_MERGE_SOURCE_SCHEMA_INVALID"),
        ("simulated_provenance", "READONLY_DB_MERGE_SOURCE_LIVE_PROOF_MISSING"),
        ("false_live_proof", "READONLY_DB_MERGE_SOURCE_LIVE_PROOF_MISSING"),
        ("missing_provenance", "READONLY_DB_MERGE_SOURCE_PROVENANCE_MISSING"),
        ("stale_unrelated_source", "READONLY_DB_MERGE_SOURCE_PARENT_RUN_MISMATCH"),
        ("duplicate_source", "READONLY_DB_MERGE_DUPLICATE_SOURCE"),
        ("missing_source", "READONLY_DB_MERGE_SOURCE_MISSING"),
        ("outside_root", "READONLY_DB_EVIDENCE_ROOT_UNAPPROVED"),
        ("symlink_component", "READONLY_DB_EVIDENCE_PATH_UNSAFE"),
    ],
)
def test_merge_readonly_db_source_evidence_rejects_untrusted_sources(
    mutator: str,
    error_code: str,
    tmp_path: Path,
) -> None:
    evidence_root = _evidence_root()
    run_id = _run_id(f"merge-{mutator}")
    gfs_config = _seed_live_readonly_source(
        evidence_root=evidence_root,
        run_id=f"{run_id}-gfs",
        source="GFS",
    )
    ifs_config = _seed_live_readonly_source(
        evidence_root=evidence_root,
        run_id=f"{run_id}-ifs",
        source="IFS",
    )
    source_dirs = [gfs_config.lane_dir, ifs_config.lane_dir]
    if mutator == "forged_summary_no_siblings":
        forged = evidence_root / f"{run_id}-forged" / "db" / "readonly-db-boundary"
        forged.mkdir(parents=True, exist_ok=True)
        forged_summary = json.loads((gfs_config.lane_dir / "summary.json").read_text(encoding="utf-8"))
        _write_json(forged / "summary.json", forged_summary)
        source_dirs[0] = forged
    elif mutator == "sibling_mismatch":
        role = json.loads((gfs_config.lane_dir / "role.json").read_text(encoding="utf-8"))
        role["current_user"] = "forged_display_ro"
        _write_json(gfs_config.lane_dir / "role.json", role)
    elif mutator == "simulated_schema":
        gfs_summary = json.loads((gfs_config.lane_dir / "summary.json").read_text(encoding="utf-8"))
        gfs_summary["schema"] = "nhms.readonly_db_boundary.evidence.simulated.v1"
        _write_json(gfs_config.lane_dir / "summary.json", gfs_summary)
    elif mutator == "simulated_provenance":
        gfs_summary = json.loads((gfs_config.lane_dir / "summary.json").read_text(encoding="utf-8"))
        gfs_summary["validation_provenance"] = {
            "mode": "simulated",
            "live_readonly_proof": False,
            "injected_components": ["adapter"],
        }
        _write_json(gfs_config.lane_dir / "summary.json", gfs_summary)
    elif mutator == "false_live_proof":
        gfs_summary = json.loads((gfs_config.lane_dir / "summary.json").read_text(encoding="utf-8"))
        gfs_summary["validation_provenance"]["live_readonly_proof"] = False
        _write_json(gfs_config.lane_dir / "summary.json", gfs_summary)
    elif mutator == "missing_provenance":
        gfs_summary = json.loads((gfs_config.lane_dir / "summary.json").read_text(encoding="utf-8"))
        gfs_summary.pop("validation_provenance", None)
        _write_json(gfs_config.lane_dir / "summary.json", gfs_summary)
    elif mutator == "stale_unrelated_source":
        stale_config = _seed_live_readonly_source(
            evidence_root=evidence_root,
            run_id=f"{run_id}-older-gfs",
            source="GFS",
        )
        source_dirs[0] = stale_config.lane_dir
    elif mutator == "duplicate_source":
        ifs_summary = json.loads((ifs_config.lane_dir / "summary.json").read_text(encoding="utf-8"))
        ifs_summary["display_identity"]["source"] = "GFS"
        for route in ifs_summary["route_smoke"]:
            if isinstance(route, dict) and route.get("source") == "IFS":
                route["source"] = "GFS"
                if isinstance(route.get("strict_identity"), dict):
                    route["strict_identity"]["source"] = "GFS"
        _write_json(ifs_config.lane_dir / "summary.json", ifs_summary)
        _write_json(ifs_config.lane_dir / "route_smoke.json", ifs_summary["route_smoke"])
    elif mutator == "missing_source":
        source_dirs = [gfs_config.lane_dir]
    elif mutator == "outside_root":
        # resolve() flattens the platform's tempdir symlinks (macOS /var -> private/var)
        # so this row reaches the approved-root gate instead of the symlink gate.
        forged = Path(tempfile.gettempdir()).resolve() / "nhms-readonly-db-forged"
        if _is_under_approved_evidence_root(forged):
            # A Slurm-style $TMPDIR can resolve under /scratch/frd_muziyao; this
            # row's oracle must never depend on where the host puts its tempdir.
            forged = Path("/nhms-readonly-db-forged-outside-approved-roots")
        assert not _is_under_approved_evidence_root(forged)
        source_dirs[0] = forged
    elif mutator == "symlink_component":
        # A REAL symlink component pointing at an EXISTING directory: the gate
        # fires on ``component.exists() and component.is_symlink()``, so a
        # dangling link would not trigger it.
        symlink_free_base = tmp_path.resolve()
        real_lane = symlink_free_base / "real" / "db" / "readonly-db-boundary"
        real_lane.mkdir(parents=True)
        linked_root = symlink_free_base / "linked"
        linked_root.symlink_to(symlink_free_base / "real", target_is_directory=True)
        source_dirs[0] = linked_root / "db" / "readonly-db-boundary"

    if mutator in {"duplicate_source", "missing_source"}:
        summary = merge_readonly_db_source_evidence(
            evidence_root=evidence_root,
            run_id=run_id,
            source_dirs=source_dirs,
            force=True,
        )
        assert summary["status"] == "BLOCKED"
        assert {blocker["code"] for blocker in summary["blockers"]} >= {error_code}
    else:
        with pytest.raises(ReadonlyDbValidationError) as exc_info:
            merge_readonly_db_source_evidence(
                evidence_root=evidence_root,
                run_id=run_id,
                source_dirs=source_dirs,
                force=True,
            )
        assert exc_info.value.error_code == error_code


def test_merge_readonly_db_source_evidence_rejects_deep_nested_source_json() -> None:
    evidence_root = _evidence_root()
    run_id = _run_id("merge-deep-json")
    gfs_config = _seed_live_readonly_source(
        evidence_root=evidence_root,
        run_id=f"{run_id}-gfs",
        source="GFS",
    )
    ifs_config = _seed_live_readonly_source(
        evidence_root=evidence_root,
        run_id=f"{run_id}-ifs",
        source="IFS",
    )
    (gfs_config.lane_dir / "summary.json").write_text(_deep_nested_json(320), encoding="utf-8")

    with pytest.raises(ReadonlyDbValidationError) as exc_info:
        merge_readonly_db_source_evidence(
            evidence_root=evidence_root,
            run_id=run_id,
            source_dirs=(gfs_config.lane_dir, ifs_config.lane_dir),
            force=True,
        )

    assert exc_info.value.error_code == "READONLY_DB_MERGE_SOURCE_JSON_TOO_DEEP"


def test_merge_readonly_db_source_evidence_accepts_explicit_parent_bundle_binding() -> None:
    evidence_root = _evidence_root()
    run_id = _run_id("merge-explicit-parent")
    gfs_config = _seed_live_readonly_source(
        evidence_root=evidence_root,
        run_id=f"{run_id}-external-gfs",
        source="GFS",
    )
    ifs_config = _seed_live_readonly_source(
        evidence_root=evidence_root,
        run_id=f"{run_id}-external-ifs",
        source="IFS",
    )
    for config in (gfs_config, ifs_config):
        summary = json.loads((config.lane_dir / "summary.json").read_text(encoding="utf-8"))
        summary["validation_provenance"]["parent_evidence_run_id"] = run_id
        summary["validation_provenance"]["parent_evidence_root"] = str(evidence_root)
        _write_json(config.lane_dir / "summary.json", summary)

    summary = merge_readonly_db_source_evidence(
        evidence_root=evidence_root,
        run_id=run_id,
        source_dirs=(gfs_config.lane_dir, ifs_config.lane_dir),
        force=True,
    )

    assert summary["status"] == "PASS"
    assert {artifact["parent_binding"] for artifact in summary["validation_provenance"]["source_artifacts"]} == {
        "validation_provenance.parent_evidence_run_id"
    }


def test_merge_readonly_db_source_evidence_rejects_prefix_source_under_different_parent() -> None:
    evidence_root = _evidence_root()
    alternate_root = REPO_ROOT / "artifacts" / "test-readonly-db-validation-alt"
    run_id = _run_id("merge-prefix-parent")
    gfs_config = _seed_live_readonly_source(
        evidence_root=alternate_root,
        run_id=f"{run_id}-gfs",
        source="GFS",
    )
    ifs_config = _seed_live_readonly_source(
        evidence_root=evidence_root,
        run_id=f"{run_id}-ifs",
        source="IFS",
    )

    with pytest.raises(ReadonlyDbValidationError) as exc_info:
        merge_readonly_db_source_evidence(
            evidence_root=evidence_root,
            run_id=run_id,
            source_dirs=(gfs_config.lane_dir, ifs_config.lane_dir),
            force=True,
        )

    assert exc_info.value.error_code == "READONLY_DB_MERGE_SOURCE_PARENT_ROOT_MISMATCH"


def test_merge_readonly_db_source_evidence_external_source_requires_root_binding() -> None:
    evidence_root = _evidence_root()
    run_id = _run_id("merge-external-root")
    gfs_config = _seed_live_readonly_source(
        evidence_root=evidence_root,
        run_id=f"{run_id}-external-gfs",
        source="GFS",
    )
    ifs_config = _seed_live_readonly_source(
        evidence_root=evidence_root,
        run_id=f"{run_id}-ifs",
        source="IFS",
    )
    gfs_summary = json.loads((gfs_config.lane_dir / "summary.json").read_text(encoding="utf-8"))
    gfs_summary["validation_provenance"]["parent_evidence_run_id"] = run_id
    _write_json(gfs_config.lane_dir / "summary.json", gfs_summary)

    with pytest.raises(ReadonlyDbValidationError) as exc_info:
        merge_readonly_db_source_evidence(
            evidence_root=evidence_root,
            run_id=run_id,
            source_dirs=(gfs_config.lane_dir, ifs_config.lane_dir),
            force=True,
        )

    assert exc_info.value.error_code == "READONLY_DB_MERGE_SOURCE_PARENT_ROOT_MISSING"


def test_merge_readonly_db_source_evidence_accepts_declared_reduced_scope() -> None:
    evidence_root = _evidence_root()
    run_id = _run_id("merge-reduced-gfs")
    gfs_config = _seed_live_readonly_source(
        evidence_root=evidence_root,
        run_id=f"{run_id}-gfs",
        source="GFS",
    )

    summary = merge_readonly_db_source_evidence(
        evidence_root=evidence_root,
        run_id=run_id,
        source_dirs=(gfs_config.lane_dir,),
        declared_sources=("GFS",),
        reduced_scope=True,
        force=True,
    )

    assert summary["status"] == "PASS"
    assert summary["validation_provenance"]["declared_sources"] == ["GFS"]
    assert summary["validation_provenance"]["reduced_scope"] is True


def test_merge_readonly_db_source_evidence_default_full_scope_blocks_missing_ifs() -> None:
    evidence_root = _evidence_root()
    run_id = _run_id("merge-full-missing-ifs")
    gfs_config = _seed_live_readonly_source(
        evidence_root=evidence_root,
        run_id=f"{run_id}-gfs",
        source="GFS",
    )

    summary = merge_readonly_db_source_evidence(
        evidence_root=evidence_root,
        run_id=run_id,
        source_dirs=(gfs_config.lane_dir,),
        force=True,
    )

    assert summary["status"] == "BLOCKED"
    assert "READONLY_DB_MERGE_SOURCE_MISSING" in {blocker["code"] for blocker in summary["blockers"]}


def test_successful_ddl_execution_despite_no_catalog_grant_fails_with_rollback_cleanup_only() -> None:
    adapter = _FakeReadonlyAdapter(successful_operations={("ops.*", "DDL_CREATE_TABLE")})

    probes = run_permission_probe_matrix(adapter, ddl_suffix="ddl-success")

    ddl = next(item for item in probes if item["target"] == "ops.*")
    operation = ddl["operations"][0]
    assert ddl["status"] == "FAIL"
    assert operation["status"] == "FAIL"
    assert operation["reason"] == "ddl_probe_executed_successfully_before_rollback"
    assert operation["rolled_back"] is True


def test_permission_matrix_covers_required_targets_and_blocks_absent_reduced_fixture_table() -> None:
    missing = ProbeTarget("met", "forcing_station_timeseries", "met_station_timeseries")
    adapter = _FakeReadonlyAdapter(absent_tables={missing.qualified_name})

    probes = run_permission_probe_matrix(adapter, ddl_suffix="matrix")

    targets = {item["target"] for item in probes}
    assert {
        "hydro.hydro_run",
        "hydro.river_timeseries",
        "met.forecast_cycle",
        "met.forcing_station_timeseries",
        "ops.pipeline_job",
        "ops.pipeline_event",
        "ops.*",
    } <= targets
    missing_probe = next(item for item in probes if item["target"] == missing.qualified_name)
    assert missing_probe["status"] == "BLOCKED"
    assert missing_probe["reason"] == "required_table_absent_in_fixture"
    assert missing_probe["operations"] == []
