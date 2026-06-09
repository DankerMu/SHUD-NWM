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
        "description",
        "recommendation",
    }
    assert finding_fields <= set(findings[0])
    assert {"broad-e2e-api-mock", "stale-display-route-token", "placeholder-path-token"} <= {
        finding["check_id"] for finding in findings
    }


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
    assert findings["apps/frontend/e2e/visual-preview.spec.ts"]["severity"] == "medium"
    assert findings["apps/frontend/e2e/visual-preview.spec.ts"]["priority"] == "P2"
    assert (
        findings["apps/frontend/e2e/visual-preview.spec.ts"]["allowlist_reason"]
        == "deterministic mocked/preview/visual e2e broad mock"
    )


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
    assert len(signal_findings) == 1
    assert signal_findings[0]["evidence_path"] == "apps/frontend/src/api/types.ts"
    assert str(signal_findings[0]["allowlist_reason"]).startswith("report-only fingerprint ")


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
