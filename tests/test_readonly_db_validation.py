from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from services.production_closure.readonly_db_validation import (
    ProbeExecution,
    ProbeTarget,
    ReadonlyDbValidationConfig,
    ReadonlyDbValidationError,
    RouteHttpResponse,
    build_arg_parser,
    run_display_manual_action_probes,
    run_display_route_smoke,
    run_permission_probe_matrix,
    validate_readonly_db_boundary,
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
    evidence = _evidence_text(config.lane_dir)
    assert "supersecret" not in evidence
    assert "token=secret" not in evidence
    assert ":supersecret@" not in evidence
    assert "[redacted]" in evidence or "postgresql://db.example:5432/nhms" in evidence


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
        absent_tables: set[str] | None = None,
        successful_operations: set[tuple[str, str]] | None = None,
        role_overrides: dict[str, Any] | None = None,
    ) -> None:
        self.privileges = privileges or {}
        self.absent_tables = absent_tables or set()
        self.successful_operations = successful_operations or set()
        self.role_overrides = role_overrides or {}
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
        return {"insert": False, "update": False, "delete": False, **self.privileges.get(target.qualified_name, {})}

    def schema_privileges(self, schema: str) -> dict[str, bool]:
        return {"create": False}

    def first_updatable_column(self, target: ProbeTarget) -> str | None:
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
