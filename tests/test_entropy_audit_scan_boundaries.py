"""Scan boundaries, role/env and toolchain families (#1823 partition).

Instruction-agent sources, the bounded context extraction and its
credential-safe output, the runtime-tree and oversized-file scan skips, the
OpenAPI fingerprint caps, the role/env boundary scanner, the Makefile toolchain
family, the broad-e2e-mock classifier and the allowlist-key normalizer.

The shared constants, the memoized ``build_report`` accessor, the finding
selectors and the fixture builders live in ``tests/entropy_audit_helpers.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.governance import audit_repo_entropy
from tests.entropy_audit_helpers import (
    _assert_unallowlisted_budget_counted_gate_eligible_finding,
    _findings_by_check,
    _write,
)


def test_entropy_audit_topology_guardrails_scan_instruction_agent_sources(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "instructions" / "agents" / "shared.md",
        "Current NHMS production says node-22 is the active database writer.\n",
    )

    findings = _findings_by_check(tmp_path, "production-topology-node22-db-writer")

    assert [(finding["evidence_path"], finding["line"]) for finding in findings] == [
        ("instructions/agents/shared.md", 1)
    ]
    _assert_unallowlisted_budget_counted_gate_eligible_finding(findings[0])


def test_entropy_audit_topology_guardrails_do_not_treat_iso_date_suffix_as_bare_node22(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "docs/runbooks/current-production-ops.md",
        "2026-06-22 is when node-27 hosts active primary PostgreSQL :55432 for display readiness.\n",
    )

    topology_findings = [
        finding
        for finding in audit_repo_entropy.build_report(tmp_path, mode="hard-gate")["findings"]
        if str(finding["check_id"]).startswith("production-topology-")
    ]

    assert topology_findings == []


def test_entropy_audit_topology_guardrails_extract_context_only_for_candidate_lines(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write(
        tmp_path / "docs/runbooks/current-production-ops.md",
        """
        This line is ordinary operational prose.
        node-22 writes DB state.
        Another irrelevant topology sentence.
        Use node-22 local PostgreSQL on :55433 for current checks.
        More unrelated prose.
        """,
    )
    _write(
        tmp_path / "scripts/run-ingest.sh",
        """
        echo no-op
        source infra/env/display.env
        uv run python scripts/node27_autopipeline.py
        """,
    )

    original_line_context = audit_repo_entropy._topology_line_context
    original_contract_context = audit_repo_entropy._topology_contract_context
    original_display_context = audit_repo_entropy._topology_display_env_context
    line_context_calls: list[int] = []
    contract_context_calls: list[int] = []
    display_context_calls: list[int] = []

    def guarded_line_context(
        lines: list[str],
        line_no: int,
        *,
        before: int = 7,
        after: int = 7,
    ) -> str:
        assert "node-22 writes DB state" in lines[line_no - 1]
        line_context_calls.append(line_no)
        return original_line_context(lines, line_no, before=before, after=after)

    def guarded_contract_context(lines: list[str], line_no: int) -> str:
        assert ":55433" in lines[line_no - 1]
        contract_context_calls.append(line_no)
        return original_contract_context(lines, line_no)

    def guarded_display_context(lines: list[str], line_no: int) -> str:
        assert "display.env" in lines[line_no - 1]
        display_context_calls.append(line_no)
        return original_display_context(lines, line_no)

    monkeypatch.setattr(audit_repo_entropy, "_topology_line_context", guarded_line_context)
    monkeypatch.setattr(audit_repo_entropy, "_topology_contract_context", guarded_contract_context)
    monkeypatch.setattr(audit_repo_entropy, "_topology_display_env_context", guarded_display_context)

    topology_findings = [
        finding
        for finding in audit_repo_entropy.build_report(tmp_path)["findings"]
        if str(finding["check_id"]).startswith("production-topology-")
    ]

    assert {finding["check_id"] for finding in topology_findings} == {
        "production-topology-node22-db-writer",
        "production-topology-node22-local-postgres",
        "production-topology-display-env-writer",
    }
    assert line_context_calls == [2]
    assert contract_context_calls == [4]
    assert display_context_calls == [2]


def test_entropy_audit_topology_guardrails_keep_output_credential_safe(tmp_path: Path) -> None:
    _write(
        tmp_path / "docs/runbooks/current-production-ops.md",
        """
        Current NHMS production says connect to node-22 local PostgreSQL at
        postgresql://writer:super-secret-password@210.77.77.22:55433/nhms?token=secret-token
        for active DB writes.
        """,
    )

    report = audit_repo_entropy.build_report(tmp_path, mode="hard-gate")
    rendered = json.dumps(report, ensure_ascii=False, sort_keys=True)

    assert audit_repo_entropy._exit_code_for_report(report) == 1
    assert "production-topology-node22-local-postgres" in rendered
    assert "super-secret-password" not in rendered
    assert "secret-token" not in rendered
    assert "postgresql://writer" not in rendered


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


def test_broad_e2e_mock_detects_multiline_live_and_unallowlisted_registrations(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "apps" / "frontend" / "e2e" / "live.spec.ts",
        """
        await page.route(
          '**/api/v1/**',
          route => route.abort(),
        )
        """,
    )
    _write(
        tmp_path / "apps" / "frontend" / "e2e" / "route-authority.spec.ts",
        """
        await page.route(
          "**/api/v1/**",
          route => route.abort(),
        )
        """,
    )

    findings = {
        str(finding["evidence_path"]): finding
        for finding in _findings_by_check(tmp_path, "broad-e2e-api-mock")
    }

    assert set(findings) == {
        "apps/frontend/e2e/live.spec.ts",
        "apps/frontend/e2e/route-authority.spec.ts",
    }
    assert findings["apps/frontend/e2e/live.spec.ts"]["severity"] == "high"
    assert findings["apps/frontend/e2e/live.spec.ts"]["priority"] == "P1"
    _assert_unallowlisted_budget_counted_gate_eligible_finding(findings["apps/frontend/e2e/live.spec.ts"])
    assert findings["apps/frontend/e2e/route-authority.spec.ts"]["severity"] == "medium"
    assert findings["apps/frontend/e2e/route-authority.spec.ts"]["priority"] == "P2"
    _assert_unallowlisted_budget_counted_gate_eligible_finding(
        findings["apps/frontend/e2e/route-authority.spec.ts"]
    )


def test_broad_e2e_mock_ignores_route_calls_on_non_page_identifiers(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "apps" / "frontend" / "e2e" / "live.spec.ts",
        """
        await homepage.route('**/api/v1/**', route => route.abort())
        await page.route('**/api/v1/**', route => route.abort())
        """,
    )

    findings = _findings_by_check(tmp_path, "broad-e2e-api-mock")

    assert len(findings) == 1
    finding = findings[0]
    assert finding["evidence_path"] == "apps/frontend/e2e/live.spec.ts"
    assert finding["line"] == 2
    _assert_unallowlisted_budget_counted_gate_eligible_finding(finding)


def test_broad_e2e_mock_skips_frontend_generated_artifacts(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "apps" / "frontend" / "artifacts" / "live.spec.ts",
        """
        await page.route(
          '**/api/v1/**',
          route => route.abort(),
        )
        """,
    )

    findings = _findings_by_check(tmp_path, "broad-e2e-api-mock")

    assert findings == []


def test_broad_e2e_mock_detects_multiline_mocked_preview_visual_allowlist(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "apps" / "frontend" / "e2e" / "mocked-preview-visual.spec.ts",
        """
        await page.route(
          '**/api/v1/**',
          route => route.abort(),
        )
        """,
    )

    findings = _findings_by_check(tmp_path, "broad-e2e-api-mock")

    assert len(findings) == 1
    finding = findings[0]
    assert finding["evidence_path"] == "apps/frontend/e2e/mocked-preview-visual.spec.ts"
    assert finding["severity"] == "medium"
    assert finding["priority"] == "P2"
    assert finding["allowlist_reason"] == "deterministic mocked/preview/visual e2e broad mock"
    assert finding["allowlist_key"] == "broad-e2e-api-mock:deterministic-mocked-preview-visual"
    assert finding["allowlist_state"] == "allowlisted"
    assert finding["budget_counted"] is False
    assert finding["gate_eligible"] is False

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
