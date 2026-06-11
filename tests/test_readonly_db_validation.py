from __future__ import annotations

import json
import os
import shlex
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest
from fastapi import FastAPI

from services.production_closure import readonly_db_validation
from services.production_closure.readonly_db_validation import (
    ProbeExecution,
    ProbeTarget,
    PsycopgReadonlyDbProbeAdapter,
    ReadonlyDbValidationConfig,
    ReadonlyDbValidationError,
    RouteHttpResponse,
    build_arg_parser,
    merge_readonly_db_source_evidence,
    run_display_manual_action_probes,
    run_display_route_smoke,
    run_permission_probe_matrix,
    validate_readonly_db_boundary,
)
from services.production_closure.readonly_db_validation import (
    main as validation_main,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_absent_readonly_database_url_writes_blocked_evidence_without_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NHMS_DISPLAY_READONLY_DATABASE_URL", raising=False)
    monkeypatch.delenv("NHMS_READONLY_DB_VALIDATION_DATABASE_URL", raising=False)
    config = ReadonlyDbValidationConfig.from_env(
        evidence_root=_evidence_root(),
        run_id=_run_id("missing-db"),
        force=True,
    )

    summary = validate_readonly_db_boundary(config)

    assert summary["status"] == "BLOCKED"
    assert summary["status"] != "PASS"
    assert summary["blockers"][0]["code"] == "READONLY_DB_URL_MISSING"
    evidence = _evidence_text(config.lane_dir)
    assert "READONLY_DB_URL_MISSING" in evidence


def test_cli_missing_readonly_database_url_exits_blocked_without_pass(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("NHMS_DISPLAY_READONLY_DATABASE_URL", raising=False)
    monkeypatch.delenv("NHMS_READONLY_DB_VALIDATION_DATABASE_URL", raising=False)
    run_id = _run_id("missing-db-cli")

    exit_code = validation_main(
        [
            "--evidence-root",
            str(_evidence_root()),
            "--run-id",
            run_id,
            "--force",
        ]
    )

    captured = capsys.readouterr()
    summary = json.loads(captured.out)
    assert exit_code == 2
    assert summary["status"] == "BLOCKED"
    assert summary["status"] != "PASS"
    assert summary["blockers"][0]["code"] == "READONLY_DB_URL_MISSING"


def test_forced_rerun_missing_db_removes_stale_authoritative_sibling_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("NHMS_DISPLAY_READONLY_DATABASE_URL", raising=False)
    monkeypatch.delenv("NHMS_READONLY_DB_VALIDATION_DATABASE_URL", raising=False)
    config = ReadonlyDbValidationConfig.from_env(
        evidence_root=_evidence_root(),
        run_id=_run_id("force-missing-db"),
        force=True,
    )
    _seed_stale_pass_evidence(config)

    summary = validate_readonly_db_boundary(config)

    on_disk = json.loads((config.lane_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["status"] == "BLOCKED"
    assert on_disk["status"] == "BLOCKED"
    assert on_disk["blockers"][0]["code"] == "READONLY_DB_URL_MISSING"
    _assert_no_stale_authoritative_sibling_evidence(config)


def test_unapproved_evidence_root_is_rejected() -> None:
    with pytest.raises(ReadonlyDbValidationError) as exc_info:
        ReadonlyDbValidationConfig.from_env(
            evidence_root=Path("/tmp/nhms-readonly-db-validation-unapproved"),
            run_id=_run_id("bad-root"),
            database_url="postgresql://readonly:secret@db.example/nhms",
        )
    assert exc_info.value.error_code == "READONLY_DB_EVIDENCE_ROOT_UNAPPROVED"


def test_evidence_run_id_stays_distinct_from_business_hydro_run_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NHMS_READONLY_DB_VALIDATION_RUN_ID", "business-hydro-run")

    config = ReadonlyDbValidationConfig.from_env(
        evidence_root=_evidence_root(),
        run_id="evidence-bundle",
        database_url="postgresql://readonly:secret@db.example/nhms",
    )

    assert config.run_id == "evidence-bundle"
    assert config.strict_run_id == "business-hydro-run"
    assert config.lane_dir == _evidence_root() / "evidence-bundle" / "db" / "readonly-db-boundary"


def test_cli_help_distinguishes_evidence_bundle_id_from_business_hydro_run_id() -> None:
    help_text = " ".join(build_arg_parser().format_help().split())

    assert "Evidence bundle ID" in help_text
    assert "not the business hydro.hydro_run.run_id" in help_text
    assert "NHMS_READONLY_DB_VALIDATION_RUN_ID" in help_text


def test_evidence_redacts_database_url_and_secret_shaped_values() -> None:
    adapter = _FakeReadonlyAdapter()
    config = ReadonlyDbValidationConfig.from_env(
        evidence_root=_evidence_root(),
        run_id=_run_id("redact"),
        database_url="postgresql://display_ro:supersecret@db.example:5432/nhms?token=secret#frag",
        force=True,
    )

    summary = validate_readonly_db_boundary(
        config,
        adapter=adapter,
        route_requester=_passing_route_requester,
        manual_action_probe_runner=_passing_manual_actions,
    )

    assert summary["database_url"] == "postgresql://db.example:5432/nhms"
    assert summary["role"]["current_user"] == "display_ro"
    assert summary["role"]["role_type"] == "readonly_candidate"
    assert summary["status"] == "BLOCKED"
    assert summary["schema"] == "nhms.readonly_db_boundary.evidence.simulated.v1"
    assert summary["validation_provenance"]["mode"] == "simulated"
    assert summary["validation_provenance"]["live_readonly_proof"] is False
    evidence = _evidence_text(config.lane_dir)
    assert "supersecret" not in evidence
    assert "token=secret" not in evidence
    assert ":supersecret@" not in evidence
    assert "[redacted]" in evidence or "postgresql://db.example:5432/nhms" in evidence
    assert "READONLY_DB_VALIDATION_SIMULATED" in evidence


def test_injected_validation_cannot_emit_normal_live_pass_evidence() -> None:
    config = ReadonlyDbValidationConfig.from_env(
        evidence_root=_evidence_root(),
        run_id=_run_id("simulated"),
        database_url="postgresql://display_ro:secret@db.example/nhms",
        force=True,
    )

    summary = validate_readonly_db_boundary(
        config,
        adapter=_FakeReadonlyAdapter(),
        route_requester=_passing_route_requester,
        manual_action_probe_runner=_passing_manual_actions,
    )

    assert summary["status"] == "BLOCKED"
    assert summary["schema"] == "nhms.readonly_db_boundary.evidence.simulated.v1"
    assert summary["validation_provenance"] == {
        "mode": "simulated",
        "live_readonly_proof": False,
        "injected_components": ["adapter", "route_requester", "manual_action_probe_runner"],
    }
    assert summary["blockers"][0]["code"] == "READONLY_DB_VALIDATION_SIMULATED"
    summary_file = json.loads((config.lane_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary_file["status"] != "PASS"
    assert summary_file["schema"] == "nhms.readonly_db_boundary.evidence.simulated.v1"


def test_forced_rerun_adapter_failure_removes_stale_authoritative_sibling_evidence() -> None:
    class FailingCatalogAdapter(_FakeReadonlyAdapter):
        def table_privileges(self, target: ProbeTarget) -> dict[str, bool]:
            del target
            raise RuntimeError("catalog adapter failed with password=secret")

    config = ReadonlyDbValidationConfig.from_env(
        evidence_root=_evidence_root(),
        run_id=_run_id("force-adapter-failure"),
        database_url="postgresql://display_ro:secret@db.example/nhms",
        force=True,
    )
    _seed_stale_pass_evidence(config)

    summary = validate_readonly_db_boundary(
        config,
        adapter=FailingCatalogAdapter(),
        route_requester=_passing_route_requester,
        manual_action_probe_runner=_passing_manual_actions,
    )

    on_disk = json.loads((config.lane_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["status"] == "BLOCKED"
    assert on_disk["status"] == "BLOCKED"
    assert on_disk["status"] != "PASS"
    assert on_disk["blockers"][0]["code"] == "READONLY_DB_VALIDATION_UNEXPECTED_ERROR"
    assert "secret" not in json.dumps(on_disk)
    _assert_no_stale_authoritative_sibling_evidence(config)


def test_forced_rerun_route_failure_removes_stale_authoritative_sibling_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failing_route_smoke(*args: object, **kwargs: object) -> list[dict[str, Any]]:
        del args, kwargs
        raise RuntimeError("display route startup failed")

    config = ReadonlyDbValidationConfig.from_env(
        evidence_root=_evidence_root(),
        run_id=_run_id("force-route-failure"),
        database_url="postgresql://display_ro:secret@db.example/nhms",
        force=True,
    )
    _seed_stale_pass_evidence(config)
    monkeypatch.setattr(readonly_db_validation, "run_display_route_smoke", failing_route_smoke)

    summary = validate_readonly_db_boundary(
        config,
        adapter=_FakeReadonlyAdapter(),
        route_requester=_passing_route_requester,
        manual_action_probe_runner=_passing_manual_actions,
    )

    on_disk = json.loads((config.lane_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["status"] == "BLOCKED"
    assert on_disk["status"] == "BLOCKED"
    assert on_disk["status"] != "PASS"
    assert on_disk["blockers"][0]["code"] == "READONLY_DB_VALIDATION_UNEXPECTED_ERROR"
    _assert_no_stale_authoritative_sibling_evidence(config)


def test_forced_rerun_manual_action_failure_removes_stale_authoritative_sibling_evidence() -> None:
    def failing_manual_actions(run_id: str) -> list[dict[str, Any]]:
        del run_id
        raise RuntimeError("manual action validation failed")

    config = ReadonlyDbValidationConfig.from_env(
        evidence_root=_evidence_root(),
        run_id=_run_id("force-manual-action-failure"),
        database_url="postgresql://display_ro:secret@db.example/nhms",
        force=True,
    )
    _seed_stale_pass_evidence(config)

    summary = validate_readonly_db_boundary(
        config,
        adapter=_FakeReadonlyAdapter(),
        route_requester=_passing_route_requester,
        manual_action_probe_runner=failing_manual_actions,
    )

    on_disk = json.loads((config.lane_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["status"] == "BLOCKED"
    assert on_disk["status"] == "BLOCKED"
    assert on_disk["blockers"][0]["code"] == "READONLY_DB_VALIDATION_UNEXPECTED_ERROR"
    _assert_no_stale_authoritative_sibling_evidence(config)


def test_existing_evidence_lane_without_force_preserves_no_overwrite_behavior() -> None:
    config = ReadonlyDbValidationConfig.from_env(
        evidence_root=_evidence_root(),
        run_id=_run_id("no-force-existing"),
        database_url="postgresql://display_ro:secret@db.example/nhms",
        force=False,
    )
    _seed_stale_pass_evidence(config)

    with pytest.raises(ReadonlyDbValidationError) as exc_info:
        validate_readonly_db_boundary(
            config,
            adapter=_FakeReadonlyAdapter(),
            route_requester=_passing_route_requester,
            manual_action_probe_runner=_passing_manual_actions,
        )

    assert exc_info.value.error_code == "READONLY_DB_EVIDENCE_EXISTS"
    on_disk = json.loads((config.lane_dir / "summary.json").read_text(encoding="utf-8"))
    assert on_disk["status"] == "PASS"
    _assert_stale_authoritative_evidence_preserved(config)


def test_forced_rerun_stale_sibling_path_error_raises_without_blocked_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("NHMS_DISPLAY_READONLY_DATABASE_URL", raising=False)
    monkeypatch.delenv("NHMS_READONLY_DB_VALIDATION_DATABASE_URL", raising=False)
    config = ReadonlyDbValidationConfig.from_env(
        evidence_root=_evidence_root(),
        run_id=_run_id("force-symlink-sibling"),
        force=True,
    )
    config.lane_dir.mkdir(parents=True, exist_ok=True)
    (config.lane_dir / "role.json").symlink_to("stale-role.json")

    with pytest.raises(ReadonlyDbValidationError) as exc_info:
        validate_readonly_db_boundary(config)

    assert exc_info.value.error_code == "READONLY_DB_EVIDENCE_PATH_UNSAFE"
    assert not (config.lane_dir / "summary.json").exists()


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
    sequence_probe = next(
        item for item in event_probe["operations"] if item["operation"] == "SEQUENCE_USAGE_UPDATE"
    )
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
        str(probe_operation["execution_outcome"]).startswith("not_executed")
        for probe_operation in mutating_operations
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
        adapter=_FakeReadonlyAdapter(),
        route_requester=_passing_route_requester,
        manual_action_probe_runner=_passing_manual_actions,
    )
    ifs_summary = validate_readonly_db_boundary(
        ifs_config,
        adapter=_FakeReadonlyAdapter(),
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
    assert set(summary["display_identity"]) == {"GFS", "IFS"}
    assert {route.get("source") for route in summary["route_smoke"] if route.get("source")} == {"GFS", "IFS"}
    assert summary["validation_provenance"]["source_artifacts"]
    source_artifact = summary["validation_provenance"]["source_artifacts"][0]
    assert source_artifact["validation_provenance"]["mode"] == "live"
    assert source_artifact["validation_provenance"]["live_readonly_proof"] is True
    assert source_artifact["parent_binding"] == "run_id_prefix"
    assert (evidence_root / run_id / "db" / "readonly-db-boundary" / "summary.json").is_file()


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
    ],
)
def test_merge_readonly_db_source_evidence_rejects_untrusted_sources(mutator: str, error_code: str) -> None:
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
    else:
        source_dirs[0] = Path("/tmp/nhms-readonly-db-forged")

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
    assert {
        artifact["parent_binding"] for artifact in summary["validation_provenance"]["source_artifacts"]
    } == {"validation_provenance.parent_evidence_run_id"}


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


def test_display_route_smoke_pass_requires_success_and_fixture_misses_are_blocked_not_pass() -> None:
    config = ReadonlyDbValidationConfig.from_env(
        evidence_root=_evidence_root(),
        run_id=_run_id("routes"),
        database_url="postgresql://readonly:secret@db.example/nhms",
    )
    identity = {
        "source": "GFS",
        "cycle_time": "2026-05-03T00:00:00+00:00",
        "run_id": "run_routes",
        "model_id": "model_routes",
        "job_id": "job_routes",
    }

    results = run_display_route_smoke(config, identity, route_requester=_mixed_route_requester)

    latest = next(item for item in results if item["name"] == "latest_product")
    models = next(item for item in results if item["name"] == "models")
    assert latest["status"] == "BLOCKED"
    assert latest["http_status"] == 404
    assert models["status"] == "FAIL"
    assert models["http_status"] == 500


def test_bare_missing_route_404_is_fail_but_allowlisted_fixture_error_is_blocked() -> None:
    config = ReadonlyDbValidationConfig.from_env(
        evidence_root=_evidence_root(),
        run_id=_run_id("route-404"),
        database_url="postgresql://readonly:secret@db.example/nhms",
    )
    identity = {
        "source": "GFS",
        "cycle_time": "2026-05-03T00:00:00+00:00",
        "run_id": "run_routes",
        "model_id": "model_routes",
        "job_id": "job_routes",
    }

    results = run_display_route_smoke(config, identity, route_requester=_bare_404_route_requester)

    latest = next(item for item in results if item["name"] == "latest_product")
    logs = next(item for item in results if item["name"] == "job_logs")
    assert latest["status"] == "FAIL"
    assert latest["http_status"] == 404
    assert logs["status"] == "BLOCKED"
    assert logs["error_code"] == "JOB_LOG_NOT_PUBLISHED"


def test_display_route_smoke_constructs_strict_identity_paths() -> None:
    config = ReadonlyDbValidationConfig.from_env(
        evidence_root=_evidence_root(),
        run_id=_run_id("route-strict"),
        database_url="postgresql://readonly:secret@db.example/nhms",
    )
    identity = {
        "source": "GFS",
        "cycle_time": "2026-05-03T00:00:00+00:00",
        "run_id": "run_routes",
        "model_id": "model_routes",
        "job_id": "job_routes",
    }
    observed_paths: dict[str, str] = {}

    def strict_route_requester(method: str, path: str) -> RouteHttpResponse:
        del method
        name = _route_name_for_path(path)
        if name:
            observed_paths[name] = path
        body: dict[str, Any] = {"status": "ok", "data": {}}
        if name in {"latest_product", "pipeline_status", "pipeline_stages", "jobs", "job_logs"}:
            response_identity = {key: identity[key] for key in ("source", "cycle_time", "run_id", "model_id")}
            if name == "job_logs":
                response_identity["job_id"] = identity["job_id"]
            body["data"] = {"identity": response_identity}
        return RouteHttpResponse(status_code=200, body=body)

    results = run_display_route_smoke(config, identity, route_requester=strict_route_requester)

    assert all(item["status"] == "PASS" for item in results)
    for name in ("latest_product", "pipeline_status", "pipeline_stages", "jobs", "job_logs"):
        assert name in observed_paths
        query = parse_qs(urlsplit(observed_paths[name]).query)
        assert query["source"] == ["GFS"]
        assert query["cycle_time"] == ["2026-05-03T00:00:00+00:00"]
        assert query["run_id"] == ["run_routes"]
        assert query["model_id"] == ["model_routes"]
    assert urlsplit(observed_paths["jobs"]).path == "/api/v1/jobs"
    assert parse_qs(urlsplit(observed_paths["jobs"]).query)["limit"] == ["1"]
    assert urlsplit(observed_paths["job_logs"]).path == "/api/v1/jobs/job_routes/logs"


def test_display_route_smoke_blocks_2xx_identity_mismatch() -> None:
    config = ReadonlyDbValidationConfig.from_env(
        evidence_root=_evidence_root(),
        run_id=_run_id("route-response-mismatch"),
        database_url="postgresql://readonly:secret@db.example/nhms",
    )
    identity = {
        "source": "GFS",
        "cycle_time": "2026-05-03T00:00:00+00:00",
        "run_id": "run_routes",
        "model_id": "model_routes",
        "job_id": "job_routes",
    }

    def mismatched_route_requester(method: str, path: str) -> RouteHttpResponse:
        del method
        name = _route_name_for_path(path)
        body: dict[str, Any] = {"status": "ok", "data": {}}
        if name in {"latest_product", "pipeline_status", "pipeline_stages", "jobs", "job_logs"}:
            response_identity = {key: identity[key] for key in ("source", "cycle_time", "run_id", "model_id")}
            response_identity["model_id"] = "wrong-model"
            if name == "job_logs":
                response_identity["job_id"] = identity["job_id"]
            body["data"] = {"identity": response_identity}
        return RouteHttpResponse(status_code=200, body=body)

    results = run_display_route_smoke(config, identity, route_requester=mismatched_route_requester)

    latest = next(item for item in results if item["name"] == "latest_product")
    assert latest["status"] == "BLOCKED"
    assert latest["reason"] == "display_read_route_response_identity_invalid"
    assert latest["identity_blockers"][0]["code"] == "READONLY_DB_ROUTE_RESPONSE_IDENTITY_MISMATCH"


def test_display_route_smoke_blocks_fragmented_identity_across_response_objects() -> None:
    config = ReadonlyDbValidationConfig.from_env(
        evidence_root=_evidence_root(),
        run_id=_run_id("route-fragmented-object"),
        database_url="postgresql://readonly:secret@db.example/nhms",
    )
    identity = {
        "source": "GFS",
        "cycle_time": "2026-05-03T00:00:00+00:00",
        "run_id": "run_routes",
        "model_id": "model_routes",
        "job_id": "job_routes",
    }

    def fragmented_route_requester(method: str, path: str) -> RouteHttpResponse:
        del method
        name = _route_name_for_path(path)
        body: dict[str, Any] = {"status": "ok", "data": {}}
        if name in {"latest_product", "pipeline_status", "pipeline_stages", "jobs", "job_logs"}:
            body["data"] = {
                "identity": {"source_id": identity["source"]},
                "item": {"cycle_time": identity["cycle_time"], "run_id": identity["run_id"]},
                "metadata": {"model_id": identity["model_id"], "job_id": identity["job_id"]},
            }
        return RouteHttpResponse(status_code=200, body=body)

    results = run_display_route_smoke(config, identity, route_requester=fragmented_route_requester)

    latest = next(item for item in results if item["name"] == "latest_product")
    assert latest["status"] == "BLOCKED"
    assert latest["reason"] == "display_read_route_response_identity_invalid"
    assert "response_identity" not in latest
    assert {blocker["code"] for blocker in latest["identity_blockers"]} == {
        "READONLY_DB_ROUTE_RESPONSE_IDENTITY_MISSING"
    }


def test_display_route_smoke_blocks_fragmented_identity_across_list_rows() -> None:
    config = ReadonlyDbValidationConfig.from_env(
        evidence_root=_evidence_root(),
        run_id=_run_id("route-fragmented-list"),
        database_url="postgresql://readonly:secret@db.example/nhms",
    )
    identity = {
        "source": "GFS",
        "cycle_time": "2026-05-03T00:00:00+00:00",
        "run_id": "run_routes",
        "model_id": "model_routes",
        "job_id": "job_routes",
    }

    def fragmented_list_requester(method: str, path: str) -> RouteHttpResponse:
        del method
        name = _route_name_for_path(path)
        body: dict[str, Any] = {"status": "ok", "data": {}}
        if name in {"latest_product", "pipeline_status", "pipeline_stages", "jobs", "job_logs"}:
            body["data"] = [
                {"source_id": identity["source"], "cycle_time": identity["cycle_time"]},
                {"run_id": identity["run_id"], "model_id": identity["model_id"], "job_id": identity["job_id"]},
            ]
        return RouteHttpResponse(status_code=200, body=body)

    results = run_display_route_smoke(config, identity, route_requester=fragmented_list_requester)

    jobs = next(item for item in results if item["name"] == "jobs")
    assert jobs["status"] == "BLOCKED"
    assert jobs["reason"] == "display_read_route_response_identity_invalid"
    assert "response_identity" not in jobs
    assert {blocker["code"] for blocker in jobs["identity_blockers"]} == {
        "READONLY_DB_ROUTE_RESPONSE_IDENTITY_MISSING"
    }


def test_display_route_smoke_blocks_identity_bound_routes_when_strict_identity_missing() -> None:
    config = ReadonlyDbValidationConfig.from_env(
        evidence_root=_evidence_root(),
        run_id=_run_id("route-missing-strict"),
        database_url="postgresql://readonly:secret@db.example/nhms",
    )
    observed_paths: list[str] = []

    def route_requester(method: str, path: str) -> RouteHttpResponse:
        del method
        observed_paths.append(path)
        return RouteHttpResponse(status_code=200, body={"status": "ok", "data": {}})

    results = run_display_route_smoke(
        config,
        {
            "source": "GFS",
            "cycle_time": "2026-05-03T00:00:00+00:00",
            "run_id": "run_routes",
        },
        route_requester=route_requester,
    )

    blocked_by_name = {
        item["name"]: item
        for item in results
        if item["name"] in {"latest_product", "pipeline_status", "pipeline_stages", "jobs", "job_logs"}
    }
    assert set(blocked_by_name) == {"latest_product", "pipeline_status", "pipeline_stages", "jobs", "job_logs"}
    assert all(item["status"] == "BLOCKED" for item in blocked_by_name.values())
    assert blocked_by_name["jobs"]["missing_identity_fields"] == ["model_id"]
    assert blocked_by_name["job_logs"]["missing_identity_fields"] == ["model_id", "job_id"]
    assert "/api/v1/jobs?limit=1" not in observed_paths


def test_default_api_probe_adapter_uses_fixed_api_owned_module(monkeypatch: pytest.MonkeyPatch) -> None:
    imported_modules: list[str] = []
    sentinel = object()

    def fake_import_module(module_name: str) -> object:
        imported_modules.append(module_name)
        return sentinel

    monkeypatch.setattr(readonly_db_validation.importlib, "import_module", fake_import_module)

    assert readonly_db_validation._default_api_probe_adapter() is sentinel
    assert imported_modules == [readonly_db_validation.DEFAULT_API_PROBE_ADAPTER_MODULE]
    assert readonly_db_validation.DEFAULT_API_PROBE_ADAPTER_MODULE == "apps.api.readonly_validation_probe"


def test_display_route_smoke_forces_safe_env_and_bounded_database_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_env: list[dict[str, str | None]] = []

    def fake_create_app(env: dict[str, str] | None = None) -> FastAPI:
        app = FastAPI()
        app.state.create_app_env = env

        @app.api_route("/{path:path}", methods=["GET"])
        def catch_all(path: str) -> dict[str, Any]:
            observed_env.append(
                {
                    "local_logs": os.environ.get("NHMS_DISPLAY_ALLOW_LOCAL_FILE_LOGS"),
                    "database_url": os.environ.get("DATABASE_URL"),
                    "pgoptions": os.environ.get("PGOPTIONS"),
                    "service_role": os.environ.get("NHMS_SERVICE_ROLE"),
                }
            )
            name = _route_name_for_path(f"/{path}")
            body: dict[str, Any] = {"status": "ok"}
            if name in {"latest_product", "pipeline_status", "pipeline_stages", "jobs", "job_logs"}:
                identity = {
                    "source": "GFS",
                    "cycle_time": "2026-05-03T00:00:00+00:00",
                    "run_id": "run_routes",
                    "model_id": "model_routes",
                }
                if name == "job_logs":
                    identity["job_id"] = "job_routes"
                body["data"] = {"identity": identity}
            return body

        return app

    monkeypatch.setenv("NHMS_DISPLAY_ALLOW_LOCAL_FILE_LOGS", "true")
    monkeypatch.setattr("apps.api.main.create_app", fake_create_app)
    config = ReadonlyDbValidationConfig.from_env(
        evidence_root=_evidence_root(),
        run_id=_run_id("safe-env"),
        database_url="postgresql://readonly:secret@db.example/nhms?connect_timeout=0&sslmode=require",
    )

    results = run_display_route_smoke(
        config,
        {
            "source": "GFS",
            "cycle_time": "2026-05-03T00:00:00+00:00",
            "run_id": "run_routes",
            "model_id": "model_routes",
            "job_id": "job_routes",
        },
    )

    assert all(item["status"] == "PASS" for item in results)
    assert observed_env
    assert {item["local_logs"] for item in observed_env} == {"false"}
    assert {item["service_role"] for item in observed_env} == {"display_readonly"}
    assert all("connect_timeout=5" in str(item["database_url"]) for item in observed_env)
    assert all("statement_timeout%3D10000" in str(item["database_url"]) for item in observed_env)
    assert {item["pgoptions"] for item in observed_env} == {
        "-c statement_timeout=10000 -c lock_timeout=2000 -c idle_in_transaction_session_timeout=10000"
    }


def test_runbook_command_uses_evidence_root_without_double_nested_run_id() -> None:
    runbook = (REPO_ROOT / "docs" / "runbooks" / "two-node-production-e2e-plan.md").read_text(encoding="utf-8")

    assert 'EVIDENCE_PARENT="$(dirname "$EVIDENCE_ROOT")"' in runbook
    assert 'EVIDENCE_RUN_ID="$(basename "$EVIDENCE_ROOT")"' in runbook
    assert '--evidence-root "$EVIDENCE_PARENT"' in runbook
    assert '--run-id "$EVIDENCE_RUN_ID"' in runbook
    assert '--evidence-root "artifacts/two-node-e2e/$EVIDENCE_RUN_ID"' not in runbook

    create_command = 'install -m 0600 /dev/null "$READONLY_SECRET_SOURCE"'
    mode_check = 'if [ "$readonly_secret_mode" != "600" ]; then'
    source_command = '. "$READONLY_SECRET_SOURCE"'
    validator_command = "uv run python scripts/validate_readonly_db_boundary.py"
    assert "READONLY_SECRET_SOURCE=infra/env/display-readonly-secrets.env" in runbook
    assert "BLOCKED: $READONLY_SECRET_SOURCE must be mode 0600 before sourcing" in runbook
    assert create_command in runbook
    assert mode_check in runbook
    assert runbook.index(create_command) < runbook.index(source_command)
    assert runbook.index(mode_check) < runbook.index(source_command)
    assert runbook.index(source_command) < runbook.index(validator_command)


def test_readonly_secret_source_guard_blocks_readable_file_before_source_or_validator(tmp_path: Path) -> None:
    secret_source = tmp_path / "display-readonly-secrets.env"
    secret_source.write_text("touch sourced-sentinel\n", encoding="utf-8")
    secret_source.chmod(0o644)
    tmp_path_q = shlex.quote(str(tmp_path))
    secret_source_q = shlex.quote(str(secret_source))

    script = f"""
set -u
cd {tmp_path_q}
READONLY_SECRET_SOURCE={secret_source_q}
if [ ! -e "$READONLY_SECRET_SOURCE" ]; then
  install -m 0600 /dev/null "$READONLY_SECRET_SOURCE"
elif [ ! -f "$READONLY_SECRET_SOURCE" ]; then
  echo "BLOCKED: $READONLY_SECRET_SOURCE must be a regular 0600 file before sourcing" >&2
  exit 1
fi
readonly_secret_mode="$(stat -c '%a' "$READONLY_SECRET_SOURCE")" || {{
  echo "BLOCKED: cannot stat $READONLY_SECRET_SOURCE before sourcing" >&2
  exit 1
}}
if [ "$readonly_secret_mode" != "600" ]; then
  echo "BLOCKED: $READONLY_SECRET_SOURCE must be mode 0600 before sourcing" >&2
  exit 1
fi
set -a
. "$READONLY_SECRET_SOURCE"
set +a
touch validator-sentinel
"""

    result = subprocess.run(["bash", "-c", script], cwd=tmp_path, text=True, capture_output=True, check=False)

    assert result.returncode == 1
    assert "BLOCKED:" in result.stderr
    assert not (tmp_path / "sourced-sentinel").exists()
    assert not (tmp_path / "validator-sentinel").exists()


def test_readonly_secret_source_guard_preserves_existing_secret_file(tmp_path: Path) -> None:
    secret_source = tmp_path / "display-readonly-secrets.env"
    secret_content = "NHMS_DISPLAY_READONLY_DATABASE_URL=postgresql://readonly:secret@db.example/nhms\n"
    secret_source.write_text(secret_content, encoding="utf-8")
    secret_source.chmod(0o600)
    secret_source_q = shlex.quote(str(secret_source))

    script = f"""
set -u
READONLY_SECRET_SOURCE={secret_source_q}
if [ ! -e "$READONLY_SECRET_SOURCE" ]; then
  install -m 0600 /dev/null "$READONLY_SECRET_SOURCE"
elif [ ! -f "$READONLY_SECRET_SOURCE" ]; then
  echo "BLOCKED: $READONLY_SECRET_SOURCE must be a regular 0600 file before sourcing" >&2
  exit 1
fi
readonly_secret_mode="$(stat -c '%a' "$READONLY_SECRET_SOURCE")" || {{
  echo "BLOCKED: cannot stat $READONLY_SECRET_SOURCE before sourcing" >&2
  exit 1
}}
if [ "$readonly_secret_mode" != "600" ]; then
  echo "BLOCKED: $READONLY_SECRET_SOURCE must be mode 0600 before sourcing" >&2
  exit 1
fi
"""

    result = subprocess.run(["bash", "-c", script], text=True, capture_output=True, check=False)

    assert result.returncode == 0
    assert secret_source.read_text(encoding="utf-8") == secret_content


def test_operator_auth_source_guard_blocks_readable_file_before_source_or_header(tmp_path: Path) -> None:
    env_dir = tmp_path / "infra" / "env"
    env_dir.mkdir(parents=True)
    operator_auth = env_dir / "operator-auth.env"
    operator_auth.write_text("touch sourced-auth-sentinel\nOPERATOR_AUTH_TOKEN=secret\n", encoding="utf-8")
    operator_auth.chmod(0o644)
    secret_dir = tmp_path / "operator-secret"
    tmp_path_q = shlex.quote(str(tmp_path))
    secret_dir_q = shlex.quote(str(secret_dir))

    script = f"""
set -u
cd {tmp_path_q}
OPERATOR_SECRET_DIR={secret_dir_q}
block_operator_auth_source() {{
  echo "BLOCKED: $*" >&2
  exit 1
}}

if [ -f infra/env/operator-auth.env ]; then
  operator_auth_mode="$(stat -c '%a' infra/env/operator-auth.env)" || \\
    block_operator_auth_source "cannot stat infra/env/operator-auth.env before sourcing"
  if [ "$operator_auth_mode" != "600" ]; then
    block_operator_auth_source "infra/env/operator-auth.env must be mode 0600 before sourcing"
  fi
  . infra/env/operator-auth.env
else
  OPERATOR_AUTH_TOKEN=interactive-token
fi
: "${{OPERATOR_AUTH_TOKEN:?operator auth token required}}"

mkdir -p "$OPERATOR_SECRET_DIR"
OPERATOR_CURL_HEADER="$(mktemp "$OPERATOR_SECRET_DIR/operator-auth-header.XXXXXX")"
touch header-sentinel
"""

    result = subprocess.run(["bash", "-c", script], cwd=tmp_path, text=True, capture_output=True, check=False)

    assert result.returncode == 1
    assert "BLOCKED:" in result.stderr
    assert not (tmp_path / "sourced-auth-sentinel").exists()
    assert not secret_dir.exists()
    assert not (tmp_path / "header-sentinel").exists()


def test_docs_secret_source_snippets_use_fail_closed_guards() -> None:
    runbook = (REPO_ROOT / "docs" / "runbooks" / "two-node-production-e2e-plan.md").read_text(encoding="utf-8")
    docker_readme = (REPO_ROOT / "infra" / "README.two-node-docker.md").read_text(encoding="utf-8")
    env_readme = (REPO_ROOT / "infra" / "env" / "README.md").read_text(encoding="utf-8")

    assert "READONLY_SECRET_SOURCE=infra/env/display-readonly-secrets.env" in runbook
    assert 'readonly_secret_mode="$(stat -c \'%a\' "$READONLY_SECRET_SOURCE")" || {' in runbook
    assert 'if [ "$readonly_secret_mode" != "600" ]; then' in runbook
    assert "BLOCKED: $READONLY_SECRET_SOURCE must be mode 0600 before sourcing" in runbook
    assert "test -f infra/env/display-readonly-secrets.env || install -m 0600" not in runbook
    assert 'test "$(stat -c \'%a\' infra/env/display-readonly-secrets.env)" = "600"' not in runbook

    assert "READONLY_SECRET_SOURCE=infra/env/display-readonly-secrets.env" in docker_readme
    assert "block_operator_auth_source()" in docker_readme
    assert 'operator_auth_mode="$(stat -c \'%a\' infra/env/operator-auth.env)" || \\' in docker_readme
    assert 'if [ "$operator_auth_mode" != "600" ]; then' in docker_readme
    assert 'block_operator_auth_source "infra/env/operator-auth.env must be mode 0600 before sourcing"' in docker_readme
    assert 'test "$(stat -c \'%a\' infra/env/operator-auth.env)" = "600"' not in docker_readme
    assert docker_readme.index("block_operator_auth_source()") < docker_readme.index(". infra/env/operator-auth.env")
    assert docker_readme.index(". infra/env/operator-auth.env") < docker_readme.index("OPERATOR_CURL_HEADER")

    assert "use a fail-closed guard" in env_readme
    assert "prints `BLOCKED:`" in env_readme
    assert "exits before `source`" in env_readme


def test_systemd_source_trust_preflight_is_checked_in_and_authoritative() -> None:
    docker_readme = (REPO_ROOT / "infra" / "README.two-node-docker.md").read_text(encoding="utf-8")

    assert "scripts/validate_two_node_docker_source_trust.py" in docker_readme
    assert "--trusted-owner root --trusted-owner nhms-deploy" in docker_readme
    assert "--role compute" in docker_readme
    assert "--role display" in docker_readme
    assert "$CHECKOUT_ROOT/infra/systemd/nhms-compute-compose.service" in docker_readme
    assert "$CHECKOUT_ROOT/infra/systemd/nhms-display-compose.service" in docker_readme
    assert "sudo install -m 0644 \"$CHECKOUT_ROOT/infra/systemd/nhms-compute-compose.service\"" in docker_readme
    assert "sudo install -m 0644 \"$CHECKOUT_ROOT/infra/systemd/nhms-display-compose.service\"" in docker_readme
    assert "source-trust preflight 是 authoritative gate" in docker_readme
    assert "namei -l \"$CHECKOUT_ROOT/infra\" | tee \"$NAMEI_EVIDENCE\"" not in docker_readme
    assert "block_systemd_preflight()" not in docker_readme
    assert "check_systemd_source_path \"$path\"" not in docker_readme
    assert "test -z \"$(find" not in docker_readme
    assert "sudo install -m 0644 infra/systemd/" not in docker_readme


def test_systemd_namei_awk_rejects_untrusted_owner_and_group_writable_components(tmp_path: Path) -> None:
    awk_script = r'''
BEGIN {
  split(trusted, trusted_users, /[[:space:]]+/)
  for (i in trusted_users) {
    if (trusted_users[i] != "") {
      allowed[trusted_users[i]] = 1
    }
  }
}
$1 ~ /^[bcdlps-]/ {
  owner = $2
  if (substr($1, 1, 1) == "l") {
    printf "BLOCKED: symlink path component rejected: %s\n", $0 > "/dev/stderr"
    bad = 1
  }
  if (!(owner in allowed)) {
    printf "BLOCKED: untrusted owner on path component: %s\n", $0 > "/dev/stderr"
    bad = 1
  }
  if (substr($1, 6, 1) == "w" || substr($1, 9, 1) == "w") {
    printf "BLOCKED: group/world-writable path component: %s\n", $0 > "/dev/stderr"
    bad = 1
  }
}
END { exit bad }
'''
    namei_evidence = tmp_path / "systemd-checkout-namei.txt"
    namei_evidence.write_text(
        "\n".join(
            [
                "f: /opt/SHUD-NWM/infra",
                "drwxr-xr-x root root /",
                "drwxr-xr-x alice staff opt",
                "drwxrwxr-x root root SHUD-NWM",
                "drwxr-xr-x nhms-deploy docker infra",
            ]
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        ["awk", "-v", "trusted=root nhms-deploy", awk_script, str(namei_evidence)],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "BLOCKED: untrusted owner on path component" in result.stderr
    assert "alice" in result.stderr
    assert "BLOCKED: group/world-writable path component" in result.stderr


def test_systemd_source_path_guard_rejects_group_world_writable_sources(tmp_path: Path) -> None:
    unit_source = tmp_path / "infra" / "systemd" / "nhms-display-compose.service"
    unit_source.parent.mkdir(parents=True)
    unit_source.write_text("[Service]\n", encoding="utf-8")
    unit_source.chmod(0o664)
    unit_source_q = shlex.quote(str(unit_source))
    sentinel_q = shlex.quote(str(tmp_path / "systemctl-sentinel"))

    script = f"""
set -euo pipefail
TRUSTED_DOCKER_OPERATORS="$(id -un)"
block_systemd_preflight() {{
  echo "BLOCKED: $*" >&2
  exit 1
}}
is_trusted_docker_operator() {{
  case " $TRUSTED_DOCKER_OPERATORS " in
    *" $1 "*) return 0 ;;
    *) return 1 ;;
  esac
}}
check_systemd_source_path() {{
  path="$1"
  owner="$(stat -c '%U' "$path")" || block_systemd_preflight "cannot stat owner for $path"
  perms="$(stat -c '%A' "$path")" || block_systemd_preflight "cannot stat permissions for $path"
  if ! is_trusted_docker_operator "$owner"; then
    block_systemd_preflight "untrusted owner $owner on systemd source $path"
  fi
  if [ "${{perms:5:1}}" = "w" ] || [ "${{perms:8:1}}" = "w" ]; then
    block_systemd_preflight "group/world-writable systemd source $path has permissions $perms"
  fi
}}
check_systemd_source_path {unit_source_q}
touch {sentinel_q}
"""

    result = subprocess.run(["bash", "-c", script], text=True, capture_output=True, check=False)

    assert result.returncode == 1
    assert "BLOCKED: group/world-writable systemd source" in result.stderr
    assert not (tmp_path / "systemctl-sentinel").exists()


def test_psycopg_adapter_uses_bounded_validation_timeouts(monkeypatch: pytest.MonkeyPatch) -> None:
    connect_calls: list[dict[str, Any]] = []

    class FakeCursor:
        rowcount = 0

        def __enter__(self) -> FakeCursor:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def execute(self, query: object) -> None:
            del query

    class FakeConnection:
        def cursor(self) -> FakeCursor:
            return FakeCursor()

        def rollback(self) -> None:
            return None

        def close(self) -> None:
            return None

    def fake_connect(*args: object, **kwargs: Any) -> FakeConnection:
        del args
        connect_calls.append(kwargs)
        return FakeConnection()

    monkeypatch.setattr(readonly_db_validation.psycopg2, "connect", fake_connect)
    adapter = PsycopgReadonlyDbProbeAdapter("postgresql://readonly:secret@db.example/nhms", ddl_suffix="timeouts")

    result = adapter.execute_probe(
        readonly_db_validation.PermissionProbeSpec(
            operation="DELETE",
            target=ProbeTarget("ops", "pipeline_event", "pipeline_event_audit"),
            command="DELETE FROM ops.pipeline_event WHERE FALSE",
        )
    )

    assert result.outcome == "succeeded"
    assert connect_calls
    assert connect_calls[0]["connect_timeout"] == 5
    assert connect_calls[0]["options"] == (
        "-c statement_timeout=10000 -c lock_timeout=2000 -c idle_in_transaction_session_timeout=10000"
    )


def test_display_retry_cancel_manual_action_ordering_does_not_construct_write_dependencies() -> None:
    results = run_display_manual_action_probes("run-display-ordering")

    assert {item["name"] for item in results} == {"display_retry_manual_action", "display_cancel_manual_action"}
    assert all(item["status"] == "PASS" for item in results)
    assert all(item["http_status"] == 409 for item in results)
    assert all(item["observed_error_code"] == "CONTROL_PLANE_MANUAL_ACTION_REQUIRED" for item in results)
    assert all(item["write_dependency_constructed"] is False for item in results)
    assert all(item["write_executed"] is False for item in results)


class _FakeReadonlyAdapter:
    def __init__(
        self,
        *,
        privileges: dict[str, dict[str, bool]] | None = None,
        column_privileges: dict[str, dict[str, list[str]]] | None = None,
        sequence_privileges: dict[str, list[dict[str, Any]]] | None = None,
        audited_schema_sequence_privileges: list[dict[str, Any]] | None = None,
        schema_privileges_by_schema: dict[str, dict[str, bool]] | None = None,
        database_privileges: dict[str, Any] | None = None,
        absent_tables: set[str] | None = None,
        successful_operations: set[tuple[str, str]] | None = None,
        role_overrides: dict[str, Any] | None = None,
        reachable_role_findings: list[dict[str, Any]] | None = None,
        no_probe_column_targets: set[str] | None = None,
    ) -> None:
        self.privileges = privileges or {}
        self.column_privilege_overrides = column_privileges or {}
        self.sequence_privilege_overrides = sequence_privileges or {}
        self.audited_schema_sequence_privilege_overrides = audited_schema_sequence_privileges or []
        self.schema_privilege_overrides = schema_privileges_by_schema or {}
        self.database_privilege_overrides = database_privileges or {}
        self.absent_tables = absent_tables or set()
        self.successful_operations = successful_operations or set()
        self.role_overrides = role_overrides or {}
        self.reachable_role_findings = reachable_role_findings or []
        self.no_probe_column_targets = no_probe_column_targets or set()
        self.executed_specs: list[Any] = []
        self.persisted_mutations = 0

    def current_role(self) -> dict[str, Any]:
        return {
            "current_user": "display_ro",
            "session_user": "display_ro",
            "rolname": "display_ro",
            "rolsuper": False,
            "rolcreatedb": False,
            "rolcreaterole": False,
            "rolreplication": False,
            "rolbypassrls": False,
            "transaction_read_only": "off",
            **self.role_overrides,
        }

    def discover_display_identity(self) -> dict[str, Any]:
        return {
            "source": "GFS",
            "cycle_time": "2026-05-03T00:00:00+00:00",
            "run_id": "run_readonly_validation",
            "model_id": "model_readonly_validation",
            "job_id": "job_readonly_validation",
        }

    def schema_exists(self, schema: str) -> bool:
        return schema in {"hydro", "met", "ops"}

    def table_exists(self, target: ProbeTarget) -> bool:
        return target.qualified_name not in self.absent_tables

    def table_privileges(self, target: ProbeTarget) -> dict[str, bool]:
        return {
            "insert": False,
            "update": False,
            "delete": False,
            "truncate": False,
            "references": False,
            "trigger": False,
            "maintain": False,
            "maintain_supported": True,
            **self.privileges.get(target.qualified_name, {}),
        }

    def column_privileges(self, target: ProbeTarget) -> dict[str, list[str]]:
        return {"insert": [], "update": [], **self.column_privilege_overrides.get(target.qualified_name, {})}

    def sequence_privileges(self, target: ProbeTarget) -> list[dict[str, Any]]:
        return [
            {
                "sequence_schema": str(sequence.get("sequence_schema") or target.schema),
                "sequence_name": str(sequence.get("sequence_name") or "validation_probe_seq"),
                "qualified_name": str(
                    sequence.get("qualified_name") or f"{target.schema}.validation_probe_seq"
                ),
                "columns": [str(column) for column in sequence.get("columns", [])],
                "usage": bool(sequence.get("usage", False)),
                "update": bool(sequence.get("update", False)),
                "mutating_privilege_allowed": bool(sequence.get("usage", False))
                or bool(sequence.get("update", False)),
            }
            for sequence in self.sequence_privilege_overrides.get(target.qualified_name, [])
        ]

    def schema_privileges(self, schema: str) -> dict[str, bool]:
        return {"create": False, **self.schema_privilege_overrides.get(schema, {})}

    def database_privileges(self) -> dict[str, Any]:
        return {"database_name": "nhms", "create": False, **self.database_privilege_overrides}

    def audited_schema_sequence_privileges(self, schemas: tuple[str, ...]) -> list[dict[str, Any]]:
        audited_schemas = set(schemas)
        return [
            {
                "sequence_schema": str(sequence.get("sequence_schema") or "ops"),
                "sequence_name": str(sequence.get("sequence_name") or "validation_probe_seq"),
                "qualified_name": str(sequence.get("qualified_name") or "ops.validation_probe_seq"),
                "columns": [str(column) for column in sequence.get("columns", [])],
                "usage": bool(sequence.get("usage", False)),
                "update": bool(sequence.get("update", False)),
                "mutating_privilege_allowed": bool(sequence.get("usage", False))
                or bool(sequence.get("update", False)),
            }
            for sequence in self.audited_schema_sequence_privilege_overrides
            if str(sequence.get("sequence_schema") or "ops") in audited_schemas
        ]

    def reachable_role_privileges(
        self,
        targets: tuple[ProbeTarget, ...],
        schemas: tuple[str, ...],
    ) -> list[dict[str, Any]]:
        del targets, schemas
        return self.reachable_role_findings

    def first_updatable_column(self, target: ProbeTarget) -> str | None:
        if target.qualified_name in self.no_probe_column_targets:
            return None
        return "validation_probe_column"

    def execute_probe(self, spec: Any) -> ProbeExecution:
        self.executed_specs.append(spec)
        target = spec.target.qualified_name if spec.target is not None else f"{spec.ddl_schema}.*"
        key = (target, spec.operation)
        if key in self.successful_operations:
            return ProbeExecution(
                outcome="succeeded",
                message="probe succeeded before rollback",
                rowcount=0,
                rolled_back=True,
            )
        return ProbeExecution(
            outcome="denied",
            sqlstate="42501",
            message="permission denied for readonly validation probe",
            rolled_back=True,
        )


def _passing_route_requester(method: str, path: str) -> RouteHttpResponse:
    del method
    name = _route_name_for_path(path)
    body: dict[str, Any] = {"status": "ok", "data": {}}
    if name in {"latest_product", "pipeline_status", "pipeline_stages", "jobs", "job_logs"}:
        query = parse_qs(urlsplit(path).query)
        identity = {
            field: query[field][0]
            for field in ("source", "cycle_time", "run_id", "model_id")
            if query.get(field)
        }
        if name == "job_logs":
            parts = [part for part in urlsplit(path).path.split("/") if part]
            if len(parts) >= 4 and parts[-1] == "logs":
                identity["job_id"] = parts[-2]
        body["data"] = {"identity": identity}
    return RouteHttpResponse(status_code=200, body=body)


def _route_name_for_path(path: str) -> str | None:
    parsed = urlsplit(path)
    if parsed.path == "/api/v1/mvp/qhh/latest-product":
        return "latest_product"
    if parsed.path == "/api/v1/pipeline/status":
        return "pipeline_status"
    if parsed.path == "/api/v1/pipeline/stages":
        return "pipeline_stages"
    if parsed.path == "/api/v1/jobs":
        return "jobs"
    if parsed.path.endswith("/logs"):
        return "job_logs"
    return None


def _mixed_route_requester(method: str, path: str) -> RouteHttpResponse:
    if path.startswith("/api/v1/mvp/qhh/latest-product"):
        return RouteHttpResponse(
            status_code=404,
            body={"error": {"code": "QHH_LATEST_PRODUCT_UNAVAILABLE", "message": "fixture unavailable"}},
        )
    if path.startswith("/api/v1/models"):
        return RouteHttpResponse(
            status_code=500,
            body={"error": {"code": "DATABASE_WRITE_ATTEMPT", "message": "unexpected write"}},
        )
    return _passing_route_requester(method, path)


def _bare_404_route_requester(method: str, path: str) -> RouteHttpResponse:
    if path.startswith("/api/v1/mvp/qhh/latest-product"):
        return RouteHttpResponse(status_code=404, body={"detail": "Not Found"})
    parsed_path = urlsplit(path).path
    if parsed_path.startswith("/api/v1/jobs/") and parsed_path.endswith("/logs"):
        return RouteHttpResponse(
            status_code=404,
            body={"error": {"code": "JOB_LOG_NOT_PUBLISHED", "message": "published log fixture unavailable"}},
        )
    return _passing_route_requester(method, path)


def _passing_manual_actions(run_id: str) -> list[dict[str, Any]]:
    return [
        {
            "name": f"display_{action}_manual_action",
            "method": "POST",
            "path": f"/api/v1/runs/{run_id}/{action}",
            "status": "PASS",
            "http_status": 409,
            "observed_error_code": "CONTROL_PLANE_MANUAL_ACTION_REQUIRED",
            "write_dependency_constructed": False,
            "write_executed": False,
        }
        for action in ("retry", "cancel")
    ]


def _promote_simulated_summary_to_live(config: ReadonlyDbValidationConfig, summary: dict[str, Any]) -> None:
    payload = dict(summary)
    payload["schema"] = "nhms.readonly_db_boundary.evidence.v1"
    payload["status"] = "PASS"
    payload["run_id"] = config.run_id
    payload["validation_provenance"] = {"mode": "live", "live_readonly_proof": True}
    payload.pop("blockers", None)
    _write_json(config.lane_dir / "summary.json", payload)


def _seed_live_readonly_source(
    *,
    evidence_root: Path,
    run_id: str,
    source: str,
) -> ReadonlyDbValidationConfig:
    config = ReadonlyDbValidationConfig.from_env(
        evidence_root=evidence_root,
        run_id=run_id,
        database_url="postgresql://display:secret@db.example/nhms",
        source=source,
        cycle_time="2026-05-03T00:00:00+00:00",
        strict_run_id=f"run-{source.lower()}",
        model_id=f"model-{source.lower()}",
        job_id=f"job-{source.lower()}",
        force=True,
    )
    summary = validate_readonly_db_boundary(
        config,
        adapter=_FakeReadonlyAdapter(),
        route_requester=_passing_route_requester,
        manual_action_probe_runner=_passing_manual_actions,
    )
    _promote_simulated_summary_to_live(config, summary)
    return config


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _seed_stale_pass_evidence(config: ReadonlyDbValidationConfig) -> None:
    config.lane_dir.mkdir(parents=True, exist_ok=True)
    stale_files: dict[str, Any] = {
        "summary.json": {
            "status": "PASS",
            "run_id": config.run_id,
            "stale_marker": "stale_prior_summary_pass",
        },
        "role.json": {
            "current_user": "stale_display_ro",
            "role_name": "stale_display_ro",
            "role_type": "readonly_candidate",
            "stale_marker": "stale_prior_role_pass",
        },
        "route_smoke.json": [
            {
                "name": "stale_prior_latest_product_route",
                "status": "PASS",
                "http_status": 200,
                "stale_marker": "stale_prior_route_pass",
            }
        ],
        "permission_probes.json": [
            {
                "target": "hydro.hydro_run",
                "surface": "hydro_run_terminal_state",
                "status": "PASS",
                "operations": [
                    {
                        "operation": "INSERT",
                        "status": "PASS",
                        "reason": "stale_prior_insert_denied_before_commit",
                    }
                ],
                "stale_marker": "stale_prior_permission_pass",
            }
        ],
    }
    for filename, payload in stale_files.items():
        (config.lane_dir / filename).write_text(json.dumps(payload), encoding="utf-8")


def _assert_no_stale_authoritative_sibling_evidence(config: ReadonlyDbValidationConfig) -> None:
    for filename in ("role.json", "route_smoke.json", "permission_probes.json"):
        path = config.lane_dir / filename
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        assert "stale_prior_" not in text
        assert "stale_display_ro" not in text


def _assert_stale_authoritative_evidence_preserved(config: ReadonlyDbValidationConfig) -> None:
    for filename in ("summary.json", "role.json", "route_smoke.json", "permission_probes.json"):
        text = (config.lane_dir / filename).read_text(encoding="utf-8")
        assert "stale_prior_" in text


def _evidence_root() -> Path:
    return REPO_ROOT / "artifacts" / "test-readonly-db-validation"


def _run_id(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:12]}"


def _evidence_text(lane_dir: Path) -> str:
    return "\n".join(path.read_text(encoding="utf-8") for path in sorted(lane_dir.glob("*.json")))


def _deep_nested_json(depth: int) -> str:
    return "{" + '"x":{' * depth + '"status":"PASS"' + "}" * depth + "}"
