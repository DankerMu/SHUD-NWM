from __future__ import annotations

import ast
from pathlib import Path

from fastapi.routing import APIRoute

from apps.api import runtime_mode
from scripts import validate_two_node_docker_runtime as docker_runtime
from services.slurm_gateway.app import create_gateway_app
from services.slurm_gateway.config import SlurmGatewaySettings

REPO_ROOT = Path(__file__).resolve().parents[1]
ROLE_BOUNDARY_DOC = REPO_ROOT / "docs/governance/ROLE_BOUNDARY.md"

DISPLAY_RUNTIME_FORBIDDEN_ENV_KEYS = frozenset(
    {
        "SLURM_GATEWAY_URL",
        "SLURM_GATEWAY_BACKEND",
        *runtime_mode._DISPLAY_FORBIDDEN_COMPUTE_PATH_ENVS,
    }
)
DISPLAY_REQUIRED_CONFIG_KEYS = frozenset(docker_runtime.DISPLAY_REQUIRED_ENV)

QHH_DIAGNOSTIC_TOKENS = (
    "run_qhh_cycle",
    "run_qhh_continuous",
    "create_qhh_shud_manifest",
    "scripts/run_qhh_cycle.sh",
    "scripts/run_qhh_continuous.py",
    "scripts/create_qhh_shud_manifest.py",
)
PRODUCTION_ORCHESTRATOR_ROOT = REPO_ROOT / "services/orchestrator"

API_IMPORT_SCAN_ROOTS = (
    REPO_ROOT / "packages/common",
    REPO_ROOT / "services/orchestrator",
    REPO_ROOT / "workers",
)
TEMPORARY_361_API_AUTH_ALLOWLIST = frozenset(
    {
        ("packages/common/model_registry.py", "apps.api.auth"),
        ("services/orchestrator/retry.py", "apps.api.auth"),
        ("workers/flood_frequency/cli.py", "apps.api.auth"),
        ("workers/flood_frequency/frequency.py", "apps.api.auth"),
        ("workers/flood_frequency/hindcast.py", "apps.api.auth"),
        ("workers/model_registry/basins_registry_import.py", "apps.api.auth"),
        ("workers/model_registry/cli.py", "apps.api.auth"),
    }
)

GATEWAY_ALLOWED_PREFIXES = ("/health", "/api/v1/slurm")
GATEWAY_FORBIDDEN_ROUTE_PREFIXES = (
    "/api/v1/forecast",
    "/api/v1/models",
    "/api/v1/runs",
    "/api/v1/pipeline",
    "/api/v1/hindcast",
    "/api/v1/data-sources",
    "/api/v1/best-available",
    "/api/v1/layers",
    "/api/v1/tiles",
    "/api/v1/mvp",
    "/api/v1/runtime",
)
GATEWAY_FORBIDDEN_ROUTE_MARKERS = (
    "forecast",
    "model",
    "pipeline",
    "hindcast",
    "flood",
    "data-source",
    "static",
    "frontend",
)


def test_display_env_blockers_align_with_compute_only_static_inventory() -> None:
    """Runtime and two-node static guards must reject the same display control-plane env keys."""

    assert DISPLAY_RUNTIME_FORBIDDEN_ENV_KEYS == docker_runtime.DISPLAY_FORBIDDEN_ENV_KEYS
    assert docker_runtime.COMPUTE_ONLY_PATH_ENV_KEYS <= docker_runtime.DISPLAY_FORBIDDEN_ENV_KEYS
    assert {"SLURM_GATEWAY_URL", "SLURM_GATEWAY_BACKEND"} <= docker_runtime.DISPLAY_FORBIDDEN_ENV_KEYS

    allowed_display_required = {
        "NHMS_SERVICE_ROLE",
        "NHMS_REQUIRE_SERVICE_ROLE",
        "NHMS_AUTH_MODE",
        "NHMS_DISPLAY_DISABLE_CONTROL_MUTATIONS",
        "NHMS_DISPLAY_ALLOW_LOCAL_FILE_LOGS",
    }
    assert DISPLAY_REQUIRED_CONFIG_KEYS == allowed_display_required
    shared_required_role_keys = DISPLAY_REQUIRED_CONFIG_KEYS & set(docker_runtime.COMPUTE_REQUIRED_ENV)
    assert shared_required_role_keys == {"NHMS_SERVICE_ROLE", "NHMS_REQUIRE_SERVICE_ROLE"}
    assert docker_runtime.DISPLAY_REQUIRED_ENV["NHMS_SERVICE_ROLE"] == "display_readonly"
    assert docker_runtime.COMPUTE_REQUIRED_ENV["NHMS_SERVICE_ROLE"] == "compute_control"
    assert docker_runtime.DISPLAY_REQUIRED_ENV["NHMS_REQUIRE_SERVICE_ROLE"] == "true"
    assert docker_runtime.COMPUTE_REQUIRED_ENV["NHMS_REQUIRE_SERVICE_ROLE"] == "true"
    assert (
        DISPLAY_REQUIRED_CONFIG_KEYS - shared_required_role_keys
    ).isdisjoint(docker_runtime.COMPUTE_REQUIRED_ENV)
    assert DISPLAY_REQUIRED_CONFIG_KEYS.isdisjoint(docker_runtime.COMPUTE_ONLY_PATH_ENV_KEYS)
    assert DISPLAY_REQUIRED_CONFIG_KEYS.isdisjoint(docker_runtime.DISPLAY_FORBIDDEN_ENV_KEYS)


def test_standalone_slurm_gateway_exposes_only_gateway_routes() -> None:
    app = create_gateway_app(SlurmGatewaySettings(backend="mock"))
    paths = _route_paths(app)

    assert "/api/v1/slurm/health" in paths
    assert any(path.startswith("/api/v1/slurm/") for path in paths)
    assert all(path.startswith(GATEWAY_ALLOWED_PREFIXES) for path in paths)
    for forbidden_prefix in GATEWAY_FORBIDDEN_ROUTE_PREFIXES:
        assert not any(path.startswith(forbidden_prefix) for path in paths), forbidden_prefix
    joined_paths = " ".join(paths)
    for marker in GATEWAY_FORBIDDEN_ROUTE_MARKERS:
        assert marker not in joined_paths, marker


def test_production_orchestrator_excludes_qhh_diagnostic_tokens() -> None:
    sources = sorted(PRODUCTION_ORCHESTRATOR_ROOT.glob("*.py"))
    assert sources, "expected services/orchestrator/*.py production modules to scan"

    for source_path in sources:
        relative_path = source_path.relative_to(REPO_ROOT).as_posix()
        text = source_path.read_text(encoding="utf-8")
        for token in QHH_DIAGNOSTIC_TOKENS:
            assert token not in text, (
                f"production orchestrator module {relative_path} references diagnostic token {token!r}"
            )
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith(("import ", "from ")):
                assert "create_qhh_shud_manifest" not in stripped, (
                    f"production orchestrator module {relative_path} imports diagnostic manifest builder"
                )


def test_shared_worker_orchestrator_api_imports_match_temporary_361_allowlist() -> None:
    assert _observed_apps_api_imports() == TEMPORARY_361_API_AUTH_ALLOWLIST


def test_role_boundary_document_mentions_required_inventory_and_allowlist() -> None:
    text = ROLE_BOUNDARY_DOC.read_text(encoding="utf-8")

    required_terms = {
        "compute_control",
        "display_readonly",
        "slurm_gateway",
        "shared_contract",
        "node-22",
        "node-27",
        "apps.api.auth",
        "#361",
        "not permanent",
        "packages/common",
        "services/orchestrator",
        "workers/",
        "apps/api/main.py",
        "services/slurm_gateway/app.py",
        "infra/env/compute.example",
        "infra/env/display.example",
        "openapi/nhms.v1.yaml",
        "db/migrations",
        "schemas",
        "tests/test_role_boundary_static.py",
    }
    missing_terms = sorted(term for term in required_terms if term not in text)
    assert not missing_terms

    for path, module in TEMPORARY_361_API_AUTH_ALLOWLIST:
        assert path in text
        assert module in text

    required_section_titles = (
        "Representative active paths",
        "Allowed mutations",
        "Forbidden capabilities",
        "Verification oracle",
        "Current guard tests",
    )
    for section in required_section_titles:
        assert text.count(section) >= 4, section


def _route_paths(app: object) -> set[str]:
    return {route.path for route in getattr(app, "routes", []) if isinstance(route, APIRoute)}


def _observed_apps_api_imports() -> frozenset[tuple[str, str]]:
    imports: set[tuple[str, str]] = set()
    for source_path in _python_sources(API_IMPORT_SCAN_ROOTS):
        tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
        relative_path = source_path.relative_to(REPO_ROOT).as_posix()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    module = alias.name
                    if module.startswith("apps.api."):
                        imports.add((relative_path, module))
            elif isinstance(node, ast.ImportFrom):
                module = _import_from_module(node)
                if module.startswith("apps.api."):
                    imports.add((relative_path, module))
    return frozenset(imports)


def _python_sources(roots: tuple[Path, ...]) -> list[Path]:
    sources: list[Path] = []
    for root in roots:
        sources.extend(path for path in root.rglob("*.py") if "__pycache__" not in path.parts)
    return sorted(sources)


def _import_from_module(node: ast.ImportFrom) -> str:
    module = node.module or ""
    if node.level == 0:
        return module
    return "." * node.level + module
