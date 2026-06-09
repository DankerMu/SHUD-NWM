from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from collections.abc import Callable
from pathlib import Path

import pytest

from scripts.governance import audit_repo_entropy

REPO_ROOT = Path(__file__).resolve().parents[1]
BASELINE = REPO_ROOT / ".entropy-baseline" / "latest.json"


def test_entropy_audit_json_schema_is_stable() -> None:
    report = audit_repo_entropy.build_report(REPO_ROOT)

    assert set(report) == {"metadata", "module_heatmap", "findings", "high_spread_patterns"}
    metadata = report["metadata"]
    assert isinstance(metadata, dict)
    assert metadata["mode"] == "report-only"
    assert metadata["baseline_written"] is False
    assert metadata["baseline_path"] == ".entropy-baseline/latest.json"
    assert "summary_counts" in metadata
    summary_counts = metadata["summary_counts"]
    assert isinstance(summary_counts, dict)
    assert {
        "by_check_id",
        "by_priority",
        "by_role",
        "by_allowlist_state",
        "by_gate_eligibility",
        "by_budget_count",
    } <= set(summary_counts)
    assert metadata["budget_counted_count"] == summary_counts["by_budget_count"]["budget_counted"]
    assert metadata["gate_eligible_count"] == summary_counts["by_gate_eligibility"]["gate_eligible"]
    assert metadata["max_scanned_text_file_bytes"] == audit_repo_entropy.MAX_SCANNED_TEXT_FILE_BYTES
    assert metadata["max_artifact_fingerprint_bytes"] == audit_repo_entropy.MAX_ARTIFACT_FINGERPRINT_BYTES
    assert ".venv" in metadata["skipped_path_families"]
    assert "node_modules" in metadata["skipped_path_families"]
    assert {
        "role-env-boundary",
        "qhh-diagnostic-token",
        "paused-workflow-condition",
        "broad-e2e-api-mock",
        "stale-display-route-token",
        "placeholder-path-token",
        "makefile-toolchain-discipline",
        "openapi-frontend-types-delegated",
        "openapi-frontend-types-presence",
        "openapi-frontend-types-signal",
        "slurm-gateway-route-leakage",
        "agent-artifact-ownership-policy",
        "apps-api-layer-inversion",
    } <= set(metadata["executed_check_families"])

    heatmap = report["module_heatmap"]
    assert isinstance(heatmap, list)
    assert heatmap, "expected at least one module row from known governance signals"
    heatmap_fields = {
        "module",
        "structure",
        "semantics",
        "behavior",
        "context",
        "protocol",
        "control",
        "priority",
        "finding_count",
    }
    assert heatmap_fields <= set(heatmap[0])

    findings = report["findings"]
    assert isinstance(findings, list)
    assert findings, "expected at least one finding from report-only baseline signals"
    finding_fields = {
        "id",
        "title",
        "axis",
        "axis_scores",
        "governance_face",
        "role",
        "evidence_path",
        "severity",
        "priority",
        "owner_area",
        "allowlist_reason",
        "allowlist_key",
        "allowlist_state",
        "budget_counted",
        "gate_eligible",
        "description",
        "recommendation",
    }
    assert finding_fields <= set(findings[0])
    for finding in findings:
        assert finding["allowlist_state"] in {"allowlisted", "unallowlisted"}
        assert isinstance(finding["budget_counted"], bool)
        assert isinstance(finding["gate_eligible"], bool)
        if finding["allowlist_state"] == "allowlisted":
            assert isinstance(finding["allowlist_key"], str)
            assert finding["budget_counted"] is False
            assert finding["gate_eligible"] is False
        else:
            assert finding["allowlist_key"] is None
            assert finding["budget_counted"] is True
    assert {"broad-e2e-api-mock", "stale-display-route-token", "placeholder-path-token"} <= {
        finding["check_id"] for finding in findings
    }


def test_entropy_audit_report_mode_metadata_excludes_hard_gate_fields() -> None:
    report = audit_repo_entropy.build_report(REPO_ROOT, mode="report")
    metadata = report["metadata"]

    assert isinstance(metadata, dict)
    assert metadata["mode"] == "report-only"
    assert metadata["baseline_written"] is False
    assert "hard_gate_status" not in metadata
    assert "hard_gate_gated_check_ids" not in metadata
    assert "hard_gate_failing_count" not in metadata
    assert audit_repo_entropy._exit_code_for_report(report) == 0


def test_entropy_audit_cli_outputs_json_and_markdown_without_writing_baseline() -> None:
    before_exists = BASELINE.exists()
    before_content = BASELINE.read_bytes() if before_exists else None

    json_result = subprocess.run(
        [sys.executable, "scripts/governance/audit_repo_entropy.py", "--format", "json"],
        cwd=REPO_ROOT,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    )
    report = json.loads(json_result.stdout)
    assert report["metadata"]["baseline_written"] is False

    markdown_result = subprocess.run(
        [sys.executable, "scripts/governance/audit_repo_entropy.py", "--format", "markdown"],
        cwd=REPO_ROOT,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    )
    assert "## Entropy Heatmap" in markdown_result.stdout
    assert "## Prioritized Cleanup Targets" in markdown_result.stdout

    assert BASELINE.exists() is before_exists
    if before_exists:
        assert BASELINE.read_bytes() == before_content


def test_entropy_audit_hard_gate_json_failure_is_parseable_and_counts_only_gated_findings(
    tmp_path: Path,
) -> None:
    _setup_clean_hard_gate_fixture(tmp_path)
    _write(tmp_path / "Makefile", "test:\n\tpython -m pytest\n")
    _write(tmp_path / "docs" / "active.md", "Historical token /hydro-met remains in docs.\n")

    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "governance" / "audit_repo_entropy.py"),
            "--format",
            "json",
            "--mode",
            "hard-gate",
        ],
        cwd=tmp_path,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
    )
    report = json.loads(result.stdout)
    metadata = report["metadata"]

    assert result.returncode == 1
    assert not (tmp_path / ".entropy-baseline" / "latest.json").exists()
    assert metadata["mode"] == "hard-gate"
    assert metadata["hard_gate_status"] == "fail"
    assert metadata["hard_gate_gated_check_ids"] == sorted(audit_repo_entropy.HARD_GATE_CHECK_IDS)
    assert metadata["hard_gate_failing_count"] == 1
    assert metadata["hard_gate_failing_count"] == metadata["gate_eligible_count"]
    assert {finding["check_id"] for finding in report["findings"]} >= {
        "makefile-toolchain-discipline",
        "stale-display-route-token",
    }
    stale_finding = next(
        finding for finding in report["findings"] if finding["check_id"] == "stale-display-route-token"
    )
    assert stale_finding["budget_counted"] is True
    assert stale_finding["gate_eligible"] is False


def test_entropy_audit_hard_gate_json_passes_with_no_gated_findings(tmp_path: Path) -> None:
    _setup_clean_hard_gate_fixture(tmp_path)
    _write(tmp_path / "docs" / "active.md", "Historical token /hydro-met remains in docs.\n")

    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "governance" / "audit_repo_entropy.py"),
            "--format",
            "json",
            "--mode",
            "hard-gate",
        ],
        cwd=tmp_path,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
    )
    report = json.loads(result.stdout)
    metadata = report["metadata"]

    assert result.returncode == 0
    assert metadata["mode"] == "hard-gate"
    assert metadata["hard_gate_status"] == "pass"
    assert metadata["hard_gate_failing_count"] == 0
    assert metadata["gate_eligible_count"] == 0
    assert not any(
        finding["check_id"] in audit_repo_entropy.HARD_GATE_CHECK_IDS for finding in report["findings"]
    )
    assert "stale-display-route-token" in {finding["check_id"] for finding in report["findings"]}
    stale_finding = next(
        finding for finding in report["findings"] if finding["check_id"] == "stale-display-route-token"
    )
    assert stale_finding["allowlist_state"] == "unallowlisted"
    assert stale_finding["allowlist_key"] is None
    assert stale_finding["budget_counted"] is True
    assert stale_finding["gate_eligible"] is False


def test_entropy_audit_hard_gate_markdown_includes_status_and_report_sections(tmp_path: Path) -> None:
    _setup_clean_hard_gate_fixture(tmp_path)

    report = audit_repo_entropy.build_report(tmp_path, mode="hard-gate")
    markdown = audit_repo_entropy.render_markdown(report)

    assert "- Mode: `hard-gate`" in markdown
    assert "- Hard gate status: `pass`" in markdown
    assert "- Hard gate failing findings: `0`" in markdown
    assert "## Entropy Heatmap" in markdown
    assert "## High-Spread Patterns" in markdown
    assert "## Prioritized Cleanup Targets" in markdown


def test_entropy_audit_skips_root_runtime_trees_without_skipping_source_packages(
    tmp_path: Path,
) -> None:
    root = tmp_path
    root_runtime_artifact = root / "artifacts" / "runtime.py"
    root_runtime_data = root / "data" / "runtime.py"
    source_artifact = root / "services" / "artifacts" / "model.py"
    source_data = root / "services" / "data" / "loader.py"

    for path in (root_runtime_artifact, root_runtime_data, source_artifact, source_data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("VALUE = 1\n", encoding="utf-8")

    scanned = {
        path.relative_to(root).as_posix()
        for path in audit_repo_entropy._iter_text_files(root, [root])
    }

    assert "artifacts/runtime.py" not in scanned
    assert "data/runtime.py" not in scanned
    assert "services/artifacts/model.py" in scanned
    assert "services/data/loader.py" in scanned


def test_entropy_audit_skips_oversized_scanned_text_files(tmp_path: Path) -> None:
    oversized = tmp_path / "apps" / "frontend" / "e2e" / "live.spec.ts"
    oversized.parent.mkdir(parents=True, exist_ok=True)
    oversized.write_text(
        "x" * (audit_repo_entropy.MAX_SCANNED_TEXT_FILE_BYTES + 1)
        + "\nawait page.route('**/api/v1/**', route => route.abort())\n",
        encoding="utf-8",
    )

    scanned = {
        path.relative_to(tmp_path).as_posix()
        for path in audit_repo_entropy._iter_text_files(tmp_path, [tmp_path])
    }
    findings = _findings_by_check(tmp_path, "broad-e2e-api-mock")

    assert "apps/frontend/e2e/live.spec.ts" not in scanned
    assert findings == []


def test_openapi_frontend_type_fingerprint_skips_large_contract_files(tmp_path: Path) -> None:
    _write(
        tmp_path / "tests" / "test_openapi_drift.py",
        """
        def test_openapi_generated_types_are_current() -> None:
            assert True
        """,
    )
    openapi = tmp_path / "openapi" / "nhms.v1.yaml"
    frontend_types = tmp_path / "apps" / "frontend" / "src" / "api" / "types.ts"
    openapi.parent.mkdir(parents=True, exist_ok=True)
    frontend_types.parent.mkdir(parents=True, exist_ok=True)
    openapi.write_text(
        "openapi: 3.1.0\n" + "x" * (audit_repo_entropy.MAX_SCANNED_TEXT_FILE_BYTES + 1),
        encoding="utf-8",
    )
    frontend_types.write_text(
        "export interface paths {}\n" + "y" * (audit_repo_entropy.MAX_SCANNED_TEXT_FILE_BYTES + 1),
        encoding="utf-8",
    )

    findings = _findings_by_check(tmp_path, "openapi-frontend-types-signal")

    assert len(findings) == 1
    assert findings[0]["evidence_path"] == "apps/frontend/src/api/types.ts"
    assert str(findings[0]["allowlist_reason"]).startswith("report-only fingerprint skipped ")
    assert "exceeds-" in str(findings[0]["allowlist_reason"])
    assert findings[0]["allowlist_key"] == "openapi-frontend-types-signal:report-only-fingerprint-skipped"
    assert findings[0]["budget_counted"] is False
    assert findings[0]["gate_eligible"] is False


def test_openapi_frontend_type_fingerprint_skips_symlink_artifacts(tmp_path: Path) -> None:
    _write(
        tmp_path / "tests" / "test_openapi_drift.py",
        """
        def test_openapi_generated_types_are_current() -> None:
            assert True
        """,
    )
    openapi_target = tmp_path / "external-openapi.yaml"
    openapi_target.write_text("openapi: 3.1.0\n", encoding="utf-8")
    openapi = tmp_path / "openapi" / "nhms.v1.yaml"
    frontend_types = tmp_path / "apps" / "frontend" / "src" / "api" / "types.ts"
    openapi.parent.mkdir(parents=True, exist_ok=True)
    frontend_types.parent.mkdir(parents=True, exist_ok=True)
    openapi.symlink_to(openapi_target)
    frontend_types.write_text("export interface paths {}\n", encoding="utf-8")

    findings = _findings_by_check(tmp_path, "openapi-frontend-types-signal")

    assert len(findings) == 1
    assert str(findings[0]["allowlist_reason"]).startswith("report-only fingerprint skipped ")
    assert "openapi/nhms.v1.yaml:symlink" in str(findings[0]["allowlist_reason"])
    assert findings[0]["allowlist_key"] == "openapi-frontend-types-signal:report-only-fingerprint-skipped"
    assert findings[0]["budget_counted"] is False
    assert findings[0]["gate_eligible"] is False


def test_role_env_boundary_finds_display_service_in_generic_compose_without_compute_false_positive(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "infra" / "docker-compose.runtime.yml",
        """
        services:
          compute:
            environment:
              WORKSPACE_ROOT: /workspace
          display:
            image: nginx:alpine
            environment:
              SLURM_GATEWAY_URL: http://gateway:8000
        """,
    )

    findings = _findings_by_check(tmp_path, "role-env-boundary")

    assert len(findings) == 1
    assert findings[0]["evidence_path"] == "infra/docker-compose.runtime.yml"
    assert findings[0]["line"] == 8


def test_role_env_boundary_finds_frontend_env_example_without_compute_false_positive(tmp_path: Path) -> None:
    _write(
        tmp_path / "apps" / "frontend" / ".env.example",
        """
        VITE_PUBLIC_NAME=nhms
        SLURM_GATEWAY_URL=http://gateway:8000
        """,
    )
    _write(
        tmp_path / "infra" / "compute.env.example",
        """
        WORKSPACE_ROOT=/workspace
        SLURM_GATEWAY_URL=http://gateway:8000
        """,
    )

    findings = _findings_by_check(tmp_path, "role-env-boundary")

    assert [finding["evidence_path"] for finding in findings] == ["apps/frontend/.env.example"]
    assert findings[0]["line"] == 2


@pytest.mark.parametrize("env_name", [".env", ".env.local"])
def test_role_env_boundary_scans_frontend_extensionless_env_dotfiles(
    tmp_path: Path,
    env_name: str,
) -> None:
    _write(
        tmp_path / "apps" / "frontend" / env_name,
        """
        VITE_PUBLIC_NAME=nhms
        SLURM_GATEWAY_URL=http://gateway:8000
        """,
    )

    findings = _findings_by_check(tmp_path, "role-env-boundary")

    assert [finding["evidence_path"] for finding in findings] == [f"apps/frontend/{env_name}"]
    assert findings[0]["line"] == 2


def test_makefile_toolchain_detects_unmanaged_python_after_uv_run_segment(tmp_path: Path) -> None:
    _write(
        tmp_path / "Makefile",
        """
        test:
        \tuv run python -m compileall scripts && python -m pytest
        """,
    )

    findings = _findings_by_check(tmp_path, "makefile-toolchain-discipline")

    assert len(findings) == 1
    assert findings[0]["evidence_path"] == "Makefile"
    assert findings[0]["line"] == 2


def test_makefile_toolchain_skips_symlink_to_outside_file(
    tmp_path: Path,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    outside = tmp_path_factory.mktemp("entropy-outside") / "Makefile"
    outside.write_text("test:\n\tpython -m pytest\n", encoding="utf-8")
    (tmp_path / "Makefile").symlink_to(outside)

    findings = _findings_by_check(tmp_path, "makefile-toolchain-discipline")

    assert findings == []


@pytest.mark.parametrize(
    "command",
    [
        "uv run python -m compileall scripts && uv run python -m pytest",
        "uv run pytest -q ; uv run ruff check .",
        "uv run python -m pip install -e .",
    ],
)
def test_makefile_toolchain_allows_fully_uv_run_protected_compound_commands(
    tmp_path: Path,
    command: str,
) -> None:
    _write(
        tmp_path / "Makefile",
        f"""
        test:
        \t{command}
        """,
    )

    findings = _findings_by_check(tmp_path, "makefile-toolchain-discipline")

    assert findings == []


def test_broad_e2e_mock_classifies_live_label_as_high_and_mocked_e2e_as_medium(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "apps" / "frontend" / "e2e" / "live.spec.ts",
        "await page.route('**/api/v1/**', route => route.abort())\n",
    )
    _write(
        tmp_path / "apps" / "frontend" / "e2e" / "visual-preview.spec.ts",
        "await page.route('**/api/v1/**', route => route.abort())\n",
    )

    findings = {
        str(finding["evidence_path"]): finding
        for finding in _findings_by_check(tmp_path, "broad-e2e-api-mock")
    }

    assert findings["apps/frontend/e2e/live.spec.ts"]["severity"] == "high"
    assert findings["apps/frontend/e2e/live.spec.ts"]["priority"] == "P1"
    assert findings["apps/frontend/e2e/live.spec.ts"]["allowlist_reason"] is None
    assert findings["apps/frontend/e2e/live.spec.ts"]["allowlist_state"] == "unallowlisted"
    assert findings["apps/frontend/e2e/live.spec.ts"]["allowlist_key"] is None
    assert findings["apps/frontend/e2e/live.spec.ts"]["budget_counted"] is True
    assert findings["apps/frontend/e2e/live.spec.ts"]["gate_eligible"] is True
    assert findings["apps/frontend/e2e/visual-preview.spec.ts"]["severity"] == "medium"
    assert findings["apps/frontend/e2e/visual-preview.spec.ts"]["priority"] == "P2"
    assert (
        findings["apps/frontend/e2e/visual-preview.spec.ts"]["allowlist_reason"]
        == "deterministic mocked/preview/visual e2e broad mock"
    )
    assert findings["apps/frontend/e2e/visual-preview.spec.ts"]["allowlist_state"] == "allowlisted"
    assert (
        findings["apps/frontend/e2e/visual-preview.spec.ts"]["allowlist_key"]
        == "broad-e2e-api-mock:deterministic-mocked-preview-visual"
    )
    assert findings["apps/frontend/e2e/visual-preview.spec.ts"]["budget_counted"] is False
    assert findings["apps/frontend/e2e/visual-preview.spec.ts"]["gate_eligible"] is False


def test_allowlist_key_normalizes_equivalent_broad_mock_wording() -> None:
    base = audit_repo_entropy.FindingSpec(
        check_id="broad-e2e-api-mock",
        title="Deterministic frontend E2E path uses broad API mock",
        axis="behavior",
        governance_face="docs alignment",
        role="display_readonly",
        evidence_path="apps/frontend/e2e/visual-preview.spec.ts",
        line=1,
        severity="medium",
        priority="P2",
        owner_area="frontend e2e",
        module="apps/frontend",
        allowlist_reason="deterministic mocked/preview/visual e2e broad mock",
        description="Broad API mocks can be mistaken for live display evidence.",
        recommendation="Keep broad API mocks in deterministic mocked regressions.",
    )
    equivalent = audit_repo_entropy.FindingSpec(
        check_id=base.check_id,
        title=base.title,
        axis=base.axis,
        governance_face=base.governance_face,
        role=base.role,
        evidence_path="apps/frontend/e2e/mock.visual.spec.ts",
        line=1,
        severity=base.severity,
        priority=base.priority,
        owner_area=base.owner_area,
        module=base.module,
        allowlist_reason="visual preview deterministic API mock evidence",
        description=base.description,
        recommendation=base.recommendation,
    )

    base_record = audit_repo_entropy._finding_record(1, base)
    equivalent_record = audit_repo_entropy._finding_record(2, equivalent)

    assert base_record["allowlist_key"] == "broad-e2e-api-mock:deterministic-mocked-preview-visual"
    assert equivalent_record["allowlist_key"] == base_record["allowlist_key"]
    assert base_record["allowlist_reason"] != equivalent_record["allowlist_reason"]
    assert base_record["allowlist_state"] == "allowlisted"
    assert equivalent_record["budget_counted"] is False
    assert equivalent_record["gate_eligible"] is False


@pytest.mark.parametrize("allowlist_reason", [None, "", " \t\n "])
def test_empty_allowlist_reason_does_not_allowlist_gated_check(
    allowlist_reason: str | None,
) -> None:
    record = audit_repo_entropy._finding_record(
        1,
        audit_repo_entropy.FindingSpec(
            check_id="broad-e2e-api-mock",
            title="Live-labeled frontend E2E path uses broad API mock",
            axis="behavior",
            governance_face="docs alignment",
            role="display_readonly",
            evidence_path="apps/frontend/e2e/live.spec.ts",
            line=1,
            severity="high",
            priority="P1",
            owner_area="frontend e2e",
            module="apps/frontend",
            allowlist_reason=allowlist_reason,
            description="Broad API mocks can be mistaken for live display evidence.",
            recommendation="Keep live evidence specs on real API calls or narrowly scoped mocks.",
        ),
    )

    assert record["allowlist_key"] is None
    assert record["allowlist_state"] == "unallowlisted"
    assert record["budget_counted"] is True
    assert record["gate_eligible"] is True


def test_non_empty_unknown_allowlist_reason_uses_stable_slug_and_skips_budget() -> None:
    record = audit_repo_entropy._finding_record(
        1,
        audit_repo_entropy.FindingSpec(
            check_id="broad-e2e-api-mock",
            title="Frontend E2E path uses broad API mock",
            axis="behavior",
            governance_face="docs alignment",
            role="display_readonly",
            evidence_path="apps/frontend/e2e/contract.spec.ts",
            line=1,
            severity="medium",
            priority="P2",
            owner_area="frontend e2e",
            module="apps/frontend",
            allowlist_reason="Approved QA fixture exception",
            description="Broad API mocks can be mistaken for live display evidence.",
            recommendation="Keep broad API mocks in deterministic mocked regressions.",
        ),
    )

    assert record["allowlist_key"] == "broad-e2e-api-mock:approved-qa-fixture-exception"
    assert record["allowlist_state"] == "allowlisted"
    assert record["budget_counted"] is False
    assert record["gate_eligible"] is False


def test_archived_retired_path_tokens_are_allowlisted_without_budget_count(
    tmp_path: Path,
) -> None:
    _write(tmp_path / "docs" / "archived" / "m22.md", "Historical evidence mentions apps/web.\n")
    _write(
        tmp_path / "docs" / "governance" / "LEGACY_DEAD_CODE_INVENTORY.md",
        "Inventory keeps workers/sbatch_templates as retired evidence.\n",
    )

    findings = {
        str(finding["evidence_path"]): finding
        for finding in _findings_by_check(tmp_path, "placeholder-path-token")
    }

    archived = findings["docs/archived/m22.md"]
    assert archived["allowlist_reason"] == "governed archived evidence documents retired placeholder paths"
    assert archived["allowlist_key"] == (
        "placeholder-path-token:governed-archived-retired-placeholder-evidence"
    )
    assert archived["allowlist_state"] == "allowlisted"
    assert archived["budget_counted"] is False
    assert archived["gate_eligible"] is False

    inventory = findings["docs/governance/LEGACY_DEAD_CODE_INVENTORY.md"]
    assert inventory["allowlist_key"] == "placeholder-path-token:governance-retired-placeholder-inventory"
    assert inventory["budget_counted"] is False


def test_completed_governance_2_openspec_retired_path_tokens_are_allowlisted_without_budget_count(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path
        / "openspec"
        / "changes"
        / "governance-2-legacy-dead-code-retirement"
        / "tasks.md",
        "Completed evidence keeps apps/web as a retired placeholder path.\n",
    )
    _write(tmp_path / "docs" / "active.md", "Current docs still mention apps/web.\n")

    findings = {
        str(finding["evidence_path"]): finding
        for finding in _findings_by_check(tmp_path, "placeholder-path-token")
    }

    governed = findings["openspec/changes/governance-2-legacy-dead-code-retirement/tasks.md"]
    assert (
        governed["allowlist_reason"]
        == "governed completed OpenSpec evidence documents retired placeholder paths"
    )
    assert governed["allowlist_key"] == (
        "placeholder-path-token:governed-completed-openspec-retired-placeholder-evidence"
    )
    assert governed["allowlist_state"] == "allowlisted"
    assert governed["budget_counted"] is False
    assert governed["gate_eligible"] is False

    active_doc = findings["docs/active.md"]
    assert active_doc["allowlist_reason"] is None
    assert active_doc["allowlist_key"] is None
    assert active_doc["allowlist_state"] == "unallowlisted"
    assert active_doc["budget_counted"] is True
    assert active_doc["gate_eligible"] is False


def test_active_doc_retired_path_tokens_remain_budget_counted(
    tmp_path: Path,
) -> None:
    _write(tmp_path / "docs" / "active.md", "Current docs still mention apps/web.\n")

    findings = _findings_by_check(tmp_path, "placeholder-path-token")

    assert len(findings) == 1
    assert findings[0]["evidence_path"] == "docs/active.md"
    assert findings[0]["allowlist_reason"] is None
    assert findings[0]["allowlist_key"] is None
    assert findings[0]["allowlist_state"] == "unallowlisted"
    assert findings[0]["budget_counted"] is True
    assert findings[0]["gate_eligible"] is False


def test_slurm_gateway_route_leakage_finds_direct_business_route_decorators_and_path_literals(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "services" / "slurm_gateway" / "app.py",
        """
        from fastapi import FastAPI

        app = FastAPI()

        @app.get("/api/v1/models")
        def list_models():
            return []

        FRONTEND_PATH = "/static/assets"
        FORECAST_SERIES = "/basin-versions/{basin_version_id}/river-segments/{segment_id}/forecast-series"
        """,
    )

    descriptions = {
        str(finding["description"])
        for finding in _findings_by_check(tmp_path, "slurm-gateway-route-leakage")
    }

    assert any("direct route decorator" in description for description in descriptions)
    assert any("path literal `/static/assets`" in description for description in descriptions)
    assert any("forecast-series" in description for description in descriptions)


def test_openapi_frontend_type_drift_emits_delegated_and_fingerprint_signals(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "openapi" / "nhms.v1.yaml",
        """
        openapi: 3.1.0
        info:
          title: NHMS API
          version: 1.0.0
        paths: {}
        """,
    )
    _write(
        tmp_path / "apps" / "frontend" / "src" / "api" / "types.ts",
        """
        export interface paths {}
        export interface components {}
        """,
    )
    _write(
        tmp_path / "tests" / "test_openapi_drift.py",
        """
        def test_openapi_generated_types_are_current() -> None:
            assert True
        """,
    )

    findings = _findings_by_check(tmp_path, "openapi-frontend-types-delegated")
    signal_findings = _findings_by_check(tmp_path, "openapi-frontend-types-signal")

    assert len(findings) == 1
    assert findings[0]["evidence_path"] == "tests/test_openapi_drift.py"
    assert findings[0]["allowlist_reason"] == "existing OpenAPI drift tests are the enforced contract oracle"
    assert findings[0]["allowlist_key"] == "openapi-frontend-types-delegated:existing-contract-oracle-delegation"
    assert findings[0]["budget_counted"] is False
    assert findings[0]["gate_eligible"] is False
    assert len(signal_findings) == 1
    assert signal_findings[0]["evidence_path"] == "apps/frontend/src/api/types.ts"
    assert str(signal_findings[0]["allowlist_reason"]).startswith("report-only fingerprint ")
    assert signal_findings[0]["allowlist_key"] == "openapi-frontend-types-signal:report-only-fingerprint-record"
    assert signal_findings[0]["budget_counted"] is False
    assert signal_findings[0]["gate_eligible"] is False


def test_openapi_frontend_type_drift_emits_presence_signal_when_artifact_missing(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "openapi" / "nhms.v1.yaml",
        """
        openapi: 3.1.0
        info:
          title: NHMS API
          version: 1.0.0
        paths: {}
        """,
    )

    findings = _findings_by_check(tmp_path, "openapi-frontend-types-presence")

    assert len(findings) == 1
    assert findings[0]["evidence_path"] == "openapi/nhms.v1.yaml"
    assert findings[0]["severity"] == "high"
    assert findings[0]["priority"] == "P1"


def test_agent_artifact_ownership_skips_doc_status_symlink_to_outside_file(
    tmp_path: Path,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    outside = tmp_path_factory.mktemp("entropy-outside") / "DOC_STATUS.md"
    outside.write_text(
        "\n".join(
            [
                ".agents/skills/**",
                ".codex/tmp/",
                ".codex/cache/",
                ".codex/evidence/",
                "apps/frontend/artifacts/**",
                "Root `artifacts/`",
                ".dockerignore",
            ]
        ),
        encoding="utf-8",
    )
    doc_status = tmp_path / "docs" / "governance" / "DOC_STATUS.md"
    doc_status.parent.mkdir(parents=True, exist_ok=True)
    doc_status.symlink_to(outside)

    findings = _findings_by_check(tmp_path, "agent-artifact-ownership-policy")

    assert {finding["description"] for finding in findings} == {
        f"`DOC_STATUS.md` does not mention expected ownership term `{term}`."
        for term in (
            ".agents/skills/**",
            ".codex/tmp/",
            ".codex/cache/",
            ".codex/evidence/",
            "apps/frontend/artifacts/**",
            "Root `artifacts/`",
            ".dockerignore",
        )
    }


@pytest.mark.parametrize(
    ("check_ids", "setup"),
    [
        (("qhh-diagnostic-token",), lambda root: _write(root / "services/orchestrator/run.py", "run_qhh_cycle()\n")),
        (
            ("paused-workflow-condition",),
            lambda root: _write(
                root / ".github/workflows/check.yml",
                "if: github.event_name == 'pull_request' && false\n",
            ),
        ),
        (
            ("broad-e2e-api-mock",),
            lambda root: _write(
                root / "apps/frontend/e2e/live.spec.ts",
                "await page.route('**/api/v1/**', route => route.abort())\n",
            ),
        ),
        (
            ("stale-display-route-token",),
            lambda root: _write(root / "apps/frontend/src/routes.ts", 'const oldRoute = "/hydro-met";\n'),
        ),
        (
            ("placeholder-path-token", "placeholder-path-exists"),
            lambda root: (
                _write(root / "docs/active.md", "Still mentions apps/web.\n"),
                _write(root / "apps/web/README.md", "retired placeholder\n"),
            ),
        ),
        (
            ("makefile-toolchain-discipline",),
            lambda root: _write(root / "Makefile", "test:\n\tpython -m pytest\n"),
        ),
        (
            ("slurm-gateway-route-leakage",),
            lambda root: _write(
                root / "services/slurm_gateway/app.py",
                "from fastapi.staticfiles import StaticFiles\n"
                "from apps.api.routes.forecast import router as forecast_router\n"
                "def attach(app):\n"
                "    app.include_router(forecast_router)\n",
            ),
        ),
        (
            (
                "agent-artifact-ownership-policy",
                "agent-artifact-ignore-policy",
                "tracked-generated-artifact",
            ),
            lambda root: _setup_agent_artifact_drift(root),
        ),
        (
            ("apps-api-layer-inversion",),
            lambda root: _write(root / "packages/common/bad_import.py", "from apps.api.main import create_app\n"),
        ),
    ],
)
def test_entropy_audit_required_families_emit_positive_signals(
    tmp_path: Path,
    check_ids: tuple[str, ...],
    setup: Callable[[Path], object],
) -> None:
    setup(tmp_path)

    emitted = {str(finding["check_id"]) for finding in audit_repo_entropy.build_report(tmp_path)["findings"]}

    assert set(check_ids) <= emitted


@pytest.mark.parametrize(
    ("check_id", "setup"),
    [
        (
            "role-env-boundary",
            lambda root: _write(
                root / "apps" / "frontend" / ".env.example",
                "SLURM_GATEWAY_URL=http://gateway:8000\n",
            ),
        ),
        ("qhh-diagnostic-token", lambda root: _write(root / "services/orchestrator/run.py", "run_qhh_cycle()\n")),
        (
            "broad-e2e-api-mock",
            lambda root: _write(
                root / "apps/frontend/e2e/live.spec.ts",
                "await page.route('**/api/v1/**', route => route.abort())\n",
            ),
        ),
        (
            "slurm-gateway-route-leakage",
            lambda root: _write(
                root / "services/slurm_gateway/app.py",
                "from fastapi import FastAPI\n"
                "app = FastAPI()\n"
                "@app.get('/api/v1/models')\n"
                "def list_models():\n"
                "    return []\n",
            ),
        ),
        ("openapi-frontend-types-presence", lambda root: (root / "apps/frontend/src/api/types.ts").unlink()),
        (
            "paused-workflow-condition",
            lambda root: _write(
                root / ".github/workflows/check.yml",
                "if: github.event_name == 'pull_request' && false\n",
            ),
        ),
        (
            "makefile-toolchain-discipline",
            lambda root: _write(root / "Makefile", "test:\n\tpython -m pytest\n"),
        ),
        (
            "agent-artifact-ownership-policy",
            lambda root: _write(root / "docs/governance/DOC_STATUS.md", "Governed docs placeholder.\n"),
        ),
        (
            "agent-artifact-ignore-policy",
            lambda root: _write(root / ".gitignore", "# missing generated artifact ignores\n"),
        ),
        ("tracked-generated-artifact", lambda root: _track_generated_artifact(root)),
    ],
)
def test_entropy_audit_hard_gate_fails_for_each_gated_check_id(
    tmp_path: Path,
    check_id: str,
    setup: Callable[[Path], object],
) -> None:
    _setup_clean_hard_gate_fixture(tmp_path)
    setup(tmp_path)

    report = audit_repo_entropy.build_report(tmp_path, mode="hard-gate")
    metadata = report["metadata"]
    gated_findings = [finding for finding in report["findings"] if finding["check_id"] == check_id]

    assert gated_findings
    assert metadata["hard_gate_status"] == "fail"
    assert metadata["hard_gate_failing_count"] == len(
        [
            finding
            for finding in report["findings"]
            if finding["gate_eligible"]
        ]
    )
    assert all(
        finding["check_id"] in audit_repo_entropy.HARD_GATE_CHECK_IDS
        for finding in report["findings"]
        if finding["gate_eligible"]
    )
    assert audit_repo_entropy._exit_code_for_report(report) == 1


def test_entropy_audit_hard_gate_keeps_delegated_and_fingerprint_openapi_signals_report_only(
    tmp_path: Path,
) -> None:
    _setup_clean_hard_gate_fixture(tmp_path)

    report = audit_repo_entropy.build_report(tmp_path, mode="hard-gate")
    metadata = report["metadata"]

    assert {
        "openapi-frontend-types-delegated",
        "openapi-frontend-types-signal",
    } <= {finding["check_id"] for finding in report["findings"]}
    assert metadata["hard_gate_status"] == "pass"
    assert metadata["hard_gate_failing_count"] == 0
    assert "openapi-frontend-types-delegated" not in metadata["hard_gate_gated_check_ids"]
    assert "openapi-frontend-types-signal" not in metadata["hard_gate_gated_check_ids"]
    for finding in report["findings"]:
        if finding["check_id"] in {"openapi-frontend-types-delegated", "openapi-frontend-types-signal"}:
            assert finding["allowlist_state"] == "allowlisted"
            assert finding["budget_counted"] is False
            assert finding["gate_eligible"] is False


def _findings_by_check(root: Path, check_id: str) -> list[dict[str, object]]:
    return [
        finding
        for finding in audit_repo_entropy.build_report(root)["findings"]
        if finding["check_id"] == check_id
    ]


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text).lstrip(), encoding="utf-8")


def _setup_agent_artifact_drift(root: Path) -> None:
    _write(root / "docs/governance/DOC_STATUS.md", "Governed docs placeholder.\n")
    _write(root / ".gitignore", "# intentionally incomplete\n")
    _write(root / ".dockerignore", "# intentionally incomplete\n")
    _write(root / "artifacts/leaked.txt", "generated\n")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "artifacts/leaked.txt"], cwd=root, check=True)


def _setup_clean_hard_gate_fixture(root: Path) -> None:
    _write(
        root / "openapi" / "nhms.v1.yaml",
        """
        openapi: 3.1.0
        info:
          title: NHMS API
          version: 1.0.0
        paths: {}
        """,
    )
    _write(
        root / "apps" / "frontend" / "src" / "api" / "types.ts",
        """
        export interface paths {}
        export interface components {}
        """,
    )
    _write(
        root / "tests" / "test_openapi_drift.py",
        """
        def test_openapi_generated_types_are_current() -> None:
            assert True
        """,
    )
    _write(
        root / "docs" / "governance" / "DOC_STATUS.md",
        """
        .agents/skills/**
        .codex/tmp/
        .codex/cache/
        .codex/evidence/
        apps/frontend/artifacts/**
        Root `artifacts/`
        .dockerignore
        """,
    )
    _write(
        root / ".gitignore",
        """
        .codex/
        artifacts/
        apps/frontend/artifacts/
        """,
    )
    _write(
        root / ".dockerignore",
        """
        .agents
        .codex
        apps/frontend/artifacts
        """,
    )
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)


def _track_generated_artifact(root: Path) -> None:
    _write(root / "artifacts" / "leaked.txt", "generated\n")
    subprocess.run(["git", "add", "-f", "artifacts/leaked.txt"], cwd=root, check=True)
