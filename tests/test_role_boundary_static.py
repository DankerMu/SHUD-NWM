from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

from fastapi.routing import APIRoute
from starlette.routing import Mount

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
    REPO_ROOT / "services",
    REPO_ROOT / "workers",
)
API_IMPORT_SCAN_FILES: tuple[Path, ...] = ()

GATEWAY_FRAMEWORK_ROUTE_PATHS = frozenset(
    {
        "/openapi.json",
        "/docs",
        "/docs/oauth2-redirect",
        "/redoc",
    }
)
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


@dataclass(frozen=True)
class GatewayRouteInventoryEntry:
    path: str
    route_type: str
    is_api_route: bool
    is_mount: bool


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
    inventory = _route_inventory(app)
    paths = {entry.path for entry in inventory}

    assert "/api/v1/slurm/health" in paths
    assert any(_is_slurm_gateway_route_path(path) for path in paths)
    forbidden_paths = _forbidden_gateway_route_paths(inventory)
    assert not forbidden_paths
    for forbidden_prefix in GATEWAY_FORBIDDEN_ROUTE_PREFIXES:
        assert not any(_is_route_namespace_path(path, forbidden_prefix) for path in paths), forbidden_prefix
    joined_paths = " ".join(paths)
    for marker in GATEWAY_FORBIDDEN_ROUTE_MARKERS:
        assert marker not in joined_paths, marker


def test_gateway_route_scope_rejects_sibling_prefixes_and_mounts() -> None:
    assert _is_allowed_gateway_route_path("/health")
    assert _is_allowed_gateway_route_path("/api/v1/slurm")
    assert _is_allowed_gateway_route_path("/api/v1/slurm/health")
    assert _is_allowed_gateway_route_path("/docs")
    assert not _is_allowed_gateway_route_path("/api/v1/slurmish")
    assert not _is_allowed_gateway_route_path("/api/v1/slurm-admin")

    app = create_gateway_app(SlurmGatewaySettings(backend="mock"))
    app.mount("/static", _NoopAsgiApp(), name="frontend")
    inventory = _route_inventory(app)
    paths = {entry.path for entry in inventory}

    assert "/static" in paths
    assert "/static" in _forbidden_gateway_route_paths(inventory)


def test_gateway_route_scope_rejects_same_namespace_mounts() -> None:
    app = create_gateway_app(SlurmGatewaySettings(backend="mock"))
    app.mount("/api/v1/slurm/extra", _NoopAsgiApp(), name="same-namespace")
    inventory = _route_inventory(app)

    forbidden_mounts = [
        entry
        for entry in _forbidden_gateway_route_entries(inventory)
        if entry.path == "/api/v1/slurm/extra"
    ]
    assert forbidden_mounts
    assert all(entry.is_mount for entry in forbidden_mounts)


def test_production_orchestrator_excludes_qhh_diagnostic_tokens() -> None:
    sources = _production_orchestrator_sources()
    assert sources, "expected services/orchestrator production Python modules to scan"

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


def test_production_orchestrator_source_scan_is_recursive(tmp_path: Path) -> None:
    root = tmp_path / "services" / "orchestrator"
    nested = root / "submission" / "driver.py"
    cache = root / "__pycache__" / "ignored.py"
    nested.parent.mkdir(parents=True)
    cache.parent.mkdir(parents=True)
    (root / "scheduler.py").write_text("SAFE_TOKEN = 'production'\n", encoding="utf-8")
    nested.write_text("SAFE_TOKEN = 'nested production'\n", encoding="utf-8")
    cache.write_text("create_qhh_shud_manifest\n", encoding="utf-8")

    sources = {path.relative_to(root).as_posix() for path in _production_orchestrator_sources(root)}

    assert sources == {"scheduler.py", "submission/driver.py"}


def test_non_api_runtime_sources_do_not_import_apps_api_layer() -> None:
    assert _observed_apps_api_imports() == frozenset()


def test_api_import_scan_includes_layer_inversion_runtime_surfaces() -> None:
    relative_sources = {
        path.relative_to(REPO_ROOT).as_posix()
        for path in _python_sources(API_IMPORT_SCAN_ROOTS, API_IMPORT_SCAN_FILES)
    }

    assert "services/slurm_gateway/models.py" in relative_sources
    assert "services/production_closure/ops_validation.py" in relative_sources
    assert "services/tiles/mvt.py" in relative_sources
    assert "services/production_closure/readonly_db_validation.py" in relative_sources


def test_apps_api_import_normalization_covers_parent_and_wildcard_forms() -> None:
    source = """
import apps.api
import apps.api.auth
import apps.api.auth.policy as api_policy
from apps import api
from apps import api as api_layer
from apps import *
from apps.api import auth
from apps.api import auth as api_auth
from apps.api import runtime_mode
from apps.api import *
from apps.api.auth import PolicyDecision
from apps.api.auth import *
"""

    assert _normalized_apps_api_import_modules(ast.parse(source)) == frozenset(
        {
            "apps.api",
            "apps.api.*",
            "apps.api.auth",
            "apps.api.auth.*",
            "apps.api.auth.policy",
            "apps.api.runtime_mode",
        }
    )


def test_apps_api_import_fixture_fails_hard_gate() -> None:
    source = """
import apps.api
from apps.api import auth as api_auth
from apps import api
"""
    relative_path = "workers/non_allowlisted_fixture.py"
    observed = _observed_apps_api_imports_for_tree(ast.parse(source), relative_path)

    assert observed == frozenset(
        {
            (relative_path, "apps.api"),
            (relative_path, "apps.api.auth"),
        }
    )
    assert observed


def test_shared_contract_file_apps_api_import_fixture_fails_hard_gate() -> None:
    source = """
from apps.api import auth
"""
    relative_path = "services/slurm_gateway/models.py"
    observed = _observed_apps_api_imports_for_tree(ast.parse(source), relative_path)

    assert observed == frozenset({(relative_path, "apps.api.auth")})
    assert observed


def test_role_boundary_document_mentions_required_inventory_and_hard_gate() -> None:
    text = ROLE_BOUNDARY_DOC.read_text(encoding="utf-8")

    required_terms = {
        "compute_control",
        "display_readonly",
        "slurm_gateway",
        "shared_contract",
        "node-22",
        "node-27",
        "#361",
        "hard gate",
        "packages/common/auth_policy.py",
        "packages/common",
        "services/orchestrator",
        "services/production_closure/ops_validation.py",
        "services/slurm_gateway/models.py",
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

    retired_temporary_terms = {
        "Temporary #361 Allowlist",
        "not permanent",
        "`packages/common/model_registry.py` | `apps.api.auth`",
        "`services/orchestrator/retry.py` | `apps.api.auth`",
        "`workers/flood_frequency/cli.py` | `apps.api.auth`",
        "`workers/flood_frequency/frequency.py` | `apps.api.auth`",
        "`workers/flood_frequency/hindcast.py` | `apps.api.auth`",
        "`workers/model_registry/basins_registry_import.py` | `apps.api.auth`",
        "`workers/model_registry/cli.py` | `apps.api.auth`",
    }
    present_retired_terms = sorted(term for term in retired_temporary_terms if term in text)
    assert not present_retired_terms

    required_section_titles = (
        "Representative active paths",
        "Allowed mutations",
        "Forbidden capabilities",
        "Verification oracle",
        "Current guard tests",
    )
    for section in required_section_titles:
        assert text.count(section) >= 4, section


def _route_inventory(app: object) -> set[GatewayRouteInventoryEntry]:
    entries: set[GatewayRouteInventoryEntry] = set()
    for route in getattr(app, "routes", []):
        route_type = type(route).__name__
        is_api_route = isinstance(route, APIRoute)
        is_mount = isinstance(route, Mount)
        for attribute in ("path", "path_format"):
            path = getattr(route, attribute, None)
            if isinstance(path, str) and path:
                entries.add(
                    GatewayRouteInventoryEntry(
                        path=path,
                        route_type=route_type,
                        is_api_route=is_api_route,
                        is_mount=is_mount,
                    )
                )
    return entries


def _route_paths(app: object) -> set[str]:
    return {entry.path for entry in _route_inventory(app)}


def _is_slurm_gateway_route_path(path: str) -> bool:
    return _is_route_namespace_path(path, "/api/v1/slurm")


def _is_allowed_gateway_route_path(path: str) -> bool:
    return (
        path == "/health"
        or _is_slurm_gateway_route_path(path)
        or path in GATEWAY_FRAMEWORK_ROUTE_PATHS
    )


def _is_route_namespace_path(path: str, namespace: str) -> bool:
    return path == namespace or path.startswith(f"{namespace}/")


def _is_allowed_gateway_route_entry(entry: GatewayRouteInventoryEntry) -> bool:
    if entry.is_mount:
        return False
    if entry.path in GATEWAY_FRAMEWORK_ROUTE_PATHS:
        return True
    return entry.is_api_route and (
        entry.path == "/health" or _is_slurm_gateway_route_path(entry.path)
    )


def _forbidden_gateway_route_entries(
    entries: set[GatewayRouteInventoryEntry],
) -> list[GatewayRouteInventoryEntry]:
    return sorted(
        (entry for entry in entries if not _is_allowed_gateway_route_entry(entry)),
        key=lambda entry: (entry.path, entry.route_type),
    )


def _forbidden_gateway_route_paths(entries: set[GatewayRouteInventoryEntry]) -> list[str]:
    return sorted({entry.path for entry in _forbidden_gateway_route_entries(entries)})


class _NoopAsgiApp:
    async def __call__(self, scope: object, receive: object, send: object) -> None:
        del scope, receive, send


def _observed_apps_api_imports() -> frozenset[tuple[str, str]]:
    imports: set[tuple[str, str]] = set()
    for source_path in _python_sources(API_IMPORT_SCAN_ROOTS, API_IMPORT_SCAN_FILES):
        tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
        relative_path = source_path.relative_to(REPO_ROOT).as_posix()
        imports.update(_observed_apps_api_imports_for_tree(tree, relative_path))
    return frozenset(imports)


def _observed_apps_api_imports_for_tree(
    tree: ast.AST,
    relative_path: str,
) -> frozenset[tuple[str, str]]:
    return frozenset((relative_path, module) for module in _normalized_apps_api_import_modules(tree))


def _normalized_apps_api_import_modules(tree: ast.AST) -> frozenset[str]:
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "apps.api" or alias.name.startswith("apps.api."):
                    modules.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            module = _import_from_module(node)
            modules.update(_normalized_apps_api_import_from_modules(module, node.names))
    return frozenset(modules)


def _normalized_apps_api_import_from_modules(
    module: str,
    aliases: list[ast.alias],
) -> set[str]:
    modules: set[str] = set()
    if module == "apps":
        for alias in aliases:
            if alias.name == "api":
                modules.add("apps.api")
            elif alias.name == "*":
                modules.add("apps.api.*")
    elif module == "apps.api":
        for alias in aliases:
            if alias.name == "*":
                modules.add("apps.api.*")
            else:
                modules.add(f"apps.api.{alias.name}")
    elif module.startswith("apps.api."):
        if any(alias.name == "*" for alias in aliases):
            modules.add(f"{module}.*")
        else:
            modules.add(module)
    return modules


def _production_orchestrator_sources(root: Path = PRODUCTION_ORCHESTRATOR_ROOT) -> list[Path]:
    return sorted(path for path in root.rglob("*.py") if "__pycache__" not in path.parts)


def _python_sources(roots: tuple[Path, ...], files: tuple[Path, ...]) -> list[Path]:
    sources: set[Path] = set()
    for root in roots:
        sources.update(path for path in root.rglob("*.py") if "__pycache__" not in path.parts)
    sources.update(path for path in files if path.is_file())
    return sorted(sources)


def _import_from_module(node: ast.ImportFrom) -> str:
    module = node.module or ""
    if node.level == 0:
        return module
    return "." * node.level + module
