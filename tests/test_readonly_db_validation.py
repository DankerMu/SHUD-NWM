from __future__ import annotations

import json
import subprocess
import tempfile
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest

from services.production_closure import readonly_db_validation
from services.production_closure.readonly_db_validation import (
    APPROVED_EVIDENCE_ROOTS,
    ProbeExecution,
    ProbeTarget,
    ReadonlyDbValidationConfig,
    ReadonlyDbValidationError,
    RouteHttpResponse,
    build_arg_parser,
    validate_readonly_db_boundary,
)
from services.production_closure.readonly_db_validation import (
    main as validation_main,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

# Canonical (published, GNU-coreutils) stat invocation -> BSD equivalent.  One
# table shared by the executed-snippet shim below and by
# ``test_stat_dialect_substitution_table_is_guard_equivalent``.
STAT_DIALECT_SUBSTITUTIONS = (
    ("stat -c '%a'", "stat -f '%Lp'"),
    ("stat -c '%U'", "stat -f '%Su'"),
    ("stat -c '%A'", "stat -f '%Sp'"),
)


@lru_cache(maxsize=1)
def _gnu_stat_available() -> bool:
    """Probe once per session whether the platform's ``stat`` accepts ``-c FORMAT``."""
    with tempfile.NamedTemporaryFile(prefix="nhms-stat-dialect-probe-") as probe:
        result = subprocess.run(
            ["stat", "-c", "%a", probe.name],
            text=True,
            capture_output=True,
            check=False,
        )
    return result.returncode == 0 and result.stdout.strip().isdigit()


@lru_cache(maxsize=1)
def _bsd_stat_available() -> bool:
    """Probe once per session whether the platform's ``stat`` accepts ``-f FORMAT``."""
    with tempfile.NamedTemporaryFile(prefix="nhms-stat-dialect-probe-") as probe:
        result = subprocess.run(
            ["stat", "-f", "%Lp", probe.name],
            text=True,
            capture_output=True,
            check=False,
        )
    return result.returncode == 0 and result.stdout.strip().isdigit()


def _portable_stat_script(script: str) -> str:
    """Return the EXECUTED copy of a guard snippet in the running platform's stat dialect.

    The guard snippets executed by this module are verbatim copies of the
    node-27 runbook guards, which use the GNU-coreutils-only ``stat -c``
    dialect by design.  BSD stat (macOS) rejects ``-c``, which diverts every
    guard into its "cannot stat" fail-closed branch instead of the mode/owner
    branch each test names.  When the probe finds no GNU ``stat``, exactly the
    tool invocations listed in ``STAT_DIALECT_SUBSTITUTIONS`` are rewritten;
    the guard's control flow, comparisons and BLOCKED messages stay
    byte-identical.

    Only the ``%a``→``%Lp`` pair has repo precedent: seven scripts under
    ``scripts/`` already ship ``stat -c '%a' … 2>/dev/null || stat -f '%Lp' …``
    fallbacks, two of which are pinned by
    tests/test_scheduler_file_provider_refresh.py.  The ``%U``→``%Su`` and
    ``%A``→``%Sp`` pairs have no prior occurrence in the repo and are new here,
    justified by the equivalence unit test named below rather than by
    convention.

    Equivalence is conditional and holds inside these guards' input domain:
    ``%U``↔``%Su`` (owner name) and ``%A``↔``%Sp`` (symbolic mode, whose
    positional characters — the guard reads ``${perms:5:1}`` / ``${perms:8:1}``
    — sit at the same indices in both dialects) are exact.  ``%a``↔``%Lp``
    agrees on the permission bits ONLY: BSD ``%Lp`` DROPS setuid/setgid/sticky
    (a 04600 file yields GNU ``%a``=4600 but ``%Lp``=600, which would slip past
    a fail-closed ``!= "600"`` comparison in the unsafe direction).  That is
    immaterial for the high-bit-free modes (0600/0644/0664) these guards chmod
    themselves, and the boundary is pinned as a recorded fact by
    ``test_stat_dialect_substitution_table_is_guard_equivalent``.

    Substitution applies ONLY to the executed script copy: the neighbouring
    runbook doc-equality assertions contain the same ``stat -c '%a'`` substring
    and must keep comparing the canonical GNU text byte-identically.
    """
    if _gnu_stat_available():
        return script
    for gnu_invocation, bsd_invocation in STAT_DIALECT_SUBSTITUTIONS:
        script = script.replace(gnu_invocation, bsd_invocation)
    return script


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


def _stat_output(flag: str, fmt: str, path: Path) -> str:
    result = subprocess.run(["stat", flag, fmt, str(path)], text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


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
                "qualified_name": str(sequence.get("qualified_name") or f"{target.schema}.validation_probe_seq"),
                "columns": [str(column) for column in sequence.get("columns", [])],
                "usage": bool(sequence.get("usage", False)),
                "update": bool(sequence.get("update", False)),
                "mutating_privilege_allowed": bool(sequence.get("usage", False)) or bool(sequence.get("update", False)),
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
                "mutating_privilege_allowed": bool(sequence.get("usage", False)) or bool(sequence.get("update", False)),
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
            field: query[field][0] for field in ("source", "cycle_time", "run_id", "model_id") if query.get(field)
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


def _is_under_approved_evidence_root(path: Path) -> bool:
    """Mirror the production containment test against ``APPROVED_EVIDENCE_ROOTS``."""
    resolved = path.expanduser().resolve()
    for root in APPROVED_EVIDENCE_ROOTS:
        try:
            resolved.relative_to(root.expanduser().resolve())
        except ValueError:
            continue
        return True
    return False


def _run_id(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:12]}"


def _evidence_text(lane_dir: Path) -> str:
    return "\n".join(path.read_text(encoding="utf-8") for path in sorted(lane_dir.glob("*.json")))


def _deep_nested_json(depth: int) -> str:
    return "{" + '"x":{' * depth + '"status":"PASS"' + "}" * depth + "}"
