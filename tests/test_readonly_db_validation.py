from __future__ import annotations

import json
import os
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
    assert all(operation["reason"].endswith("_denied_before_commit") for operation in operations)


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
        return RouteHttpResponse(status_code=200, body={"status": "ok", "data": {}})

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


def test_display_route_smoke_forces_safe_env_and_bounded_database_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_env: list[dict[str, str | None]] = []

    def fake_create_app(env: dict[str, str] | None = None) -> FastAPI:
        app = FastAPI()
        app.state.create_app_env = env

        @app.api_route("/{path:path}", methods=["GET"])
        def catch_all(path: str) -> dict[str, Any]:
            del path
            observed_env.append(
                {
                    "local_logs": os.environ.get("NHMS_DISPLAY_ALLOW_LOCAL_FILE_LOGS"),
                    "database_url": os.environ.get("DATABASE_URL"),
                    "pgoptions": os.environ.get("PGOPTIONS"),
                    "service_role": os.environ.get("NHMS_SERVICE_ROLE"),
                }
            )
            return {"status": "ok"}

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

    assert '--evidence-root "artifacts/two-node-e2e"' in runbook
    assert '--evidence-root "artifacts/two-node-e2e/$EVIDENCE_RUN_ID"' not in runbook


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


class _FakeReadonlyAdapter:
    def __init__(
        self,
        *,
        privileges: dict[str, dict[str, bool]] | None = None,
        column_privileges: dict[str, dict[str, list[str]]] | None = None,
        sequence_privileges: dict[str, list[dict[str, Any]]] | None = None,
        schema_privileges_by_schema: dict[str, dict[str, bool]] | None = None,
        absent_tables: set[str] | None = None,
        successful_operations: set[tuple[str, str]] | None = None,
        role_overrides: dict[str, Any] | None = None,
        reachable_role_findings: list[dict[str, Any]] | None = None,
        no_probe_column_targets: set[str] | None = None,
    ) -> None:
        self.privileges = privileges or {}
        self.column_privilege_overrides = column_privileges or {}
        self.sequence_privilege_overrides = sequence_privileges or {}
        self.schema_privilege_overrides = schema_privileges_by_schema or {}
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
    del method, path
    return RouteHttpResponse(status_code=200, body={"status": "ok", "data": {}})


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
    del method
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
    return RouteHttpResponse(status_code=200, body={"status": "ok", "data": {}})


def _bare_404_route_requester(method: str, path: str) -> RouteHttpResponse:
    del method
    if path.startswith("/api/v1/mvp/qhh/latest-product"):
        return RouteHttpResponse(status_code=404, body={"detail": "Not Found"})
    parsed_path = urlsplit(path).path
    if parsed_path.startswith("/api/v1/jobs/") and parsed_path.endswith("/logs"):
        return RouteHttpResponse(
            status_code=404,
            body={"error": {"code": "JOB_LOG_NOT_PUBLISHED", "message": "published log fixture unavailable"}},
        )
    return RouteHttpResponse(status_code=200, body={"status": "ok", "data": {}})


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
        }
        for action in ("retry", "cancel")
    ]


def _evidence_root() -> Path:
    return REPO_ROOT / "artifacts" / "test-readonly-db-validation"


def _run_id(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:12]}"


def _evidence_text(lane_dir: Path) -> str:
    return "\n".join(path.read_text(encoding="utf-8") for path in sorted(lane_dir.glob("*.json")))
