from __future__ import annotations

import ast
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from starlette.routing import Mount

from apps.api import runtime_mode
from services.slurm_gateway.app import create_gateway_app
from services.slurm_gateway.config import SlurmGatewaySettings
from tests.production_ops_runbook import combined_text as production_ops_text
from tests.test_env_templates import parse_env_template

REPO_ROOT = Path(__file__).resolve().parents[1]
ROLE_BOUNDARY_DOC = REPO_ROOT / "docs/governance/ROLE_BOUNDARY.md"
# #1103: the topology sentences moved into `production-ops/overview.md`; the
# pin is over the index page plus every sub-runbook.
TWO_NODE_DEPLOYMENT_OVERVIEW_DOC = REPO_ROOT / "docs/runbooks/two-node-deployment-overview.md"
PROJECT_PROFILE_DOC = REPO_ROOT / "openspec/project-profile.md"
NODE22_RETIREMENT_RECEIPT = REPO_ROOT / "docs/runbooks/receipts/2026-06-29-node22-db-retirement-stop.md"

DISPLAY_RUNTIME_FORBIDDEN_ENV_KEYS = frozenset(
    {
        "SLURM_GATEWAY_URL",
        "SLURM_GATEWAY_BACKEND",
        *runtime_mode._DISPLAY_FORBIDDEN_COMPUTE_PATH_ENVS,
    }
)
DISPLAY_ENV_TEMPLATE = REPO_ROOT / "infra/env/display.example"
SYSTEMD_TREE = REPO_ROOT / "infra/systemd"
ENV_README = REPO_ROOT / "infra/env/README.md"
REFUSED_KEYS_BULLET = "- Keys the display role refuses at startup (owner: `apps/api/runtime_mode.py`):"

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


def test_display_env_template_is_a_configuration_the_runtime_guard_accepts() -> None:
    """The display env template sets the display role and trips no display boundary blocker.

    #3231: this replaced a comparison with the docker lane validator's key lists. What
    stays worth pinning is that ``infra/env/display.example`` is a configuration the
    runtime guard (``apps/api/runtime_mode.py``) accepts, with the role, auth and
    file-log switches written out here.
    """

    template = parse_env_template(DISPLAY_ENV_TEMPLATE.read_text(encoding="utf-8"))

    assert runtime_mode.display_boundary_blockers(template) == ()
    assert template["NHMS_SERVICE_ROLE"] == "display_readonly"
    assert template["NHMS_REQUIRE_SERVICE_ROLE"] == "true"
    assert template["NHMS_AUTH_MODE"] == "production"
    assert template["NHMS_DISPLAY_DISABLE_CONTROL_MUTATIONS"] == "true"
    assert template["NHMS_DISPLAY_ALLOW_LOCAL_FILE_LOGS"] == "false"
    assert template["NHMS_DISPLAY_API_PORT"] == "8080"
    # #3231: the storage roots are node-27 host paths, not paths inside a container.
    assert template["OBJECT_STORE_ROOT"] == "/home/ghdc/nwm/object-store"
    assert template["NHMS_PUBLISHED_ARTIFACT_ROOT"] == "/home/ghdc/nwm/published"
    assert DISPLAY_RUNTIME_FORBIDDEN_ENV_KEYS.isdisjoint(template)

    # The guard bites: the same assignments plus any one forbidden key yield exactly its blocker.
    for key in sorted(DISPLAY_RUNTIME_FORBIDDEN_ENV_KEYS):
        blockers = runtime_mode.display_boundary_blockers({**template, key: "/forbidden"})
        assert [blocker.env_var for blocker in blockers] == [key], key
    reopened = runtime_mode.display_boundary_blockers({**template, "NHMS_DISPLAY_DISABLE_CONTROL_MUTATIONS": "false"})
    assert [blocker.code for blocker in reopened] == ["DISPLAY_CONTROL_MUTATIONS_FORBIDDEN"]


def _readme_refused_display_keys(text: str) -> list[str]:
    """The backticked keys of the README bullet that opens with ``REFUSED_KEYS_BULLET``."""

    lines = text.splitlines()
    starts = [index for index, line in enumerate(lines) if line.startswith(REFUSED_KEYS_BULLET)]
    assert len(starts) == 1, f"expected one refused-keys bullet, found {len(starts)}"
    bullet = [lines[starts[0]][len(REFUSED_KEYS_BULLET) :]]
    for line in lines[starts[0] + 1 :]:
        if not line.startswith("  "):
            break
        bullet.append(line)
    return re.findall(r"`([^`]+)`", "\n".join(bullet))


def test_env_readme_lists_exactly_the_keys_the_display_role_refuses() -> None:
    """#3231 D5: the runtime guard owns the refused-key list; the README states the same set."""

    keys = _readme_refused_display_keys(ENV_README.read_text(encoding="utf-8"))

    assert len(keys) == len(set(keys)), keys
    assert set(keys) == DISPLAY_RUNTIME_FORBIDDEN_ENV_KEYS


@pytest.mark.parametrize(
    ("unit", "role"),
    [
        ("infra/systemd/node22/nhms-slurm-gateway.service", runtime_mode.ServiceRole.SLURM_GATEWAY),
    ],
)
def test_compute_units_set_an_explicit_service_role(unit: str, role: runtime_mode.ServiceRole) -> None:
    """#3231: the tracked Slurm gateway unit fixes ``NHMS_SERVICE_ROLE`` itself.

    The compute API unit that also had a row here is retired (#3394).
    """

    lines = (REPO_ROOT / unit).read_text(encoding="utf-8").splitlines()
    assignments = [line.strip() for line in lines if line.strip().startswith("Environment=NHMS_SERVICE_ROLE=")]

    assert assignments == [f"Environment=NHMS_SERVICE_ROLE={role.value}"]


def starts_api_in_compute_control_role(unit_text: str) -> bool:
    """Whether one unit file both starts ``apps.api.main`` and sets ``NHMS_SERVICE_ROLE=compute_control``.

    The API counts as started only from an ``ExecStart=`` line; the role counts
    wherever a directive line assigns it (quotes ignored). Comment lines (``#`` or
    ``;``) are skipped and ``\\`` continuations are joined first, as systemd does.
    """

    logical: list[str] = []
    pending = ""
    for raw in unit_text.splitlines():
        if raw.lstrip().startswith(("#", ";")):
            continue
        if raw.endswith("\\"):
            pending += raw[:-1] + " "
            continue
        logical.append(pending + raw)
        pending = ""
    if pending:
        logical.append(pending)

    starts_api = sets_role = False
    for line in logical:
        key, sep, value = line.partition("=")
        if sep and key.strip() == "ExecStart" and "apps.api.main" in value:
            starts_api = True
        if "NHMS_SERVICE_ROLE=compute_control" in line.replace('"', "").replace("'", ""):
            sets_role = True
    return starts_api and sets_role


def _tracked_systemd_files() -> list[Path]:
    return sorted(path for path in SYSTEMD_TREE.rglob("*") if path.is_file() and not path.name.startswith("."))


def test_no_tracked_unit_starts_the_api_in_the_compute_control_role() -> None:
    """#3394: the compute API unit is retired; the scheduler unit sets ``compute_control`` but starts no API."""

    files = _tracked_systemd_files()
    relative = {path.relative_to(REPO_ROOT).as_posix() for path in files}
    # The scan reaches units at every depth and the drop-ins, and the two units that
    # carry one half of the rule each are in it.
    assert {
        "infra/systemd/node22/nhms-compute-scheduler.service",
        "infra/systemd/node22/nhms-compute-scheduler.service.d/10-slurm-gateway-token.conf",
        "infra/systemd/nhms-display-api.service",
        "infra/systemd/compute-system/w3/etc/systemd/system/slurmd.service.d/10-nhms-mounts.conf",
    } <= relative

    offenders = [
        path.relative_to(REPO_ROOT).as_posix()
        for path in files
        if starts_api_in_compute_control_role(path.read_text(encoding="utf-8"))
    ]

    assert offenders == []


def test_compute_control_api_rule_on_real_and_synthetic_units() -> None:
    scheduler = (SYSTEMD_TREE / "node22/nhms-compute-scheduler.service").read_text(encoding="utf-8")
    display = (SYSTEMD_TREE / "nhms-display-api.service").read_text(encoding="utf-8")
    # The scheduler sets the role but starts the scheduler; the display unit starts the API without it.
    assert "Environment=NHMS_SERVICE_ROLE=compute_control" in scheduler
    assert starts_api_in_compute_control_role(scheduler) is False
    assert "apps.api.main" in display
    assert starts_api_in_compute_control_role(display) is False

    api = "ExecStart=/x/.venv/bin/python -m uvicorn apps.api.main:app --port 8001\n"
    role = "Environment=NHMS_SERVICE_ROLE=compute_control\n"
    assert starts_api_in_compute_control_role("[Service]\n" + role + api) is True
    assert starts_api_in_compute_control_role('[Service]\nEnvironment="NHMS_SERVICE_ROLE=compute_control"\n' + api)
    assert starts_api_in_compute_control_role(
        "[Service]\n" + role + "ExecStart=/x/.venv/bin/python -m uvicorn \\\n  apps.api.main:app\n"
    )
    assert starts_api_in_compute_control_role("[Service]\n" + role + "ExecStart = /x/python -m apps.api.main\n")
    # Half the rule, or the other half only in a comment, passes.
    assert starts_api_in_compute_control_role("[Service]\n" + api) is False
    gateway = "ExecStart=/x/python -m services.slurm_gateway\n"
    assert starts_api_in_compute_control_role("[Service]\n" + role + gateway) is False
    assert starts_api_in_compute_control_role("[Service]\n# " + role + api) is False
    assert starts_api_in_compute_control_role("[Service]\n" + role + "; " + api) is False


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
        "compute role",
        "node-27",
        "#361",
        "hard gate",
        "packages/common/auth_policy.py",
        "packages/common",
        "services/orchestrator",
        "services/slurm_gateway/models.py",
        "workers/",
        "apps/api/main.py",
        "services/slurm_gateway/app.py",
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


def test_current_topology_docs_state_node27_compute_role_and_live_oracle_boundaries() -> None:
    role_boundary = ROLE_BOUNDARY_DOC.read_text(encoding="utf-8")
    current_ops = production_ops_text()
    two_node_overview = TWO_NODE_DEPLOYMENT_OVERVIEW_DOC.read_text(encoding="utf-8")
    project_profile = PROJECT_PROFILE_DOC.read_text(encoding="utf-8")
    project_profile_flat = " ".join(project_profile.split())

    assert "node-27 hosts the active primary PostgreSQL (`:55432`), the ingest workers" in role_boundary
    assert "display API (`:8080`), and the frontend" in role_boundary
    assert "node-27 是当前 active production service host" in current_ops
    assert "本机 PostgreSQL `:55432`" in current_ops
    assert "source download、systemd-driven ingest、display API 和前端公网入口都在 27" in current_ops

    assert "The compute role runs the Slurm/SHUD compute wrapper" in role_boundary
    assert "The compute hosts currently host compute/Slurm/artifact production only" in role_boundary
    assert "node-22 是纯计算 / Slurm / SHUD / artifact producer" in two_node_overview
    assert "Slurm scheduling/runtime receipts run on the compute control host only when sbatch" in project_profile_flat

    lines = role_boundary.splitlines()
    start = next(
        (i for i, line in enumerate(lines) if line.startswith("- The compute role is hosted since 2026-11-23")),
        None,
    )
    assert start is not None, "ROLE_BOUNDARY lacks the bullet '- The compute role is hosted since 2026-11-23'"
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("- ")), len(lines))
    hosted_bullet = " ".join(line.strip() for line in lines[start:end])
    assert "`w3`" in hosted_bullet and "`w2`" in hosted_bullet, (
        f"the hosted-since bullet must name the compute hosts `w3` and `w2`: {hosted_bullet!r}"
    )
    assert re.search(r"`?210\.77\.77\.22`? is retired from NHMS compute[^.]*rollback archive", hosted_bullet), (
        f"the hosted-since bullet must name the retired host 210.77.77.22 as a rollback archive: {hosted_bullet!r}"
    )
    assert re.search(r"\]\([^)\s]*w2-w3-compute-topology\.md(?:#[^)\s]*)?\)", hosted_bullet), (
        f"the hosted-since bullet must link w2-w3-compute-topology.md: {hosted_bullet!r}"
    )

    assert "Local checks are required but do not replace node-27 live DB/display receipts." in role_boundary
    assert "Local lint/unit/OpenSpec checks are necessary but do not substitute for" in project_profile
    assert "required node-27 live DB/display receipts" in project_profile


def test_node22_retirement_cleanup_snippets_fail_closed_on_docker_and_ss_errors() -> None:
    cleanup = _post_drill_cleanup_snippet(NODE22_RETIREMENT_RECEIPT)
    assert "set -euo pipefail" in cleanup
    assert "docker_stop_output=$(docker stop nhms-22-e2e-db 2>&1) || {" in cleanup
    assert "BLOCKED: docker stop failed during rollback cleanup" in cleanup
    assert "docker_ps_output=$(docker ps --filter name=nhms-22-e2e-db" in cleanup
    assert "BLOCKED: docker ps failed during rollback cleanup" in cleanup
    assert 'if [ -n "$docker_ps_output" ]; then' in cleanup
    assert "ss_output=$(ss -ltnp 2>&1) || {" in cleanup
    assert "BLOCKED: ss failed during rollback cleanup" in cleanup
    assert "docker ps --filter name=nhms-22-e2e-db --format '{{.Names}} {{.Status}}' | grep" not in cleanup
    assert "ss -ltnp 2>/dev/null | grep -q 55433" not in cleanup


def _route_inventory(app: object) -> set[GatewayRouteInventoryEntry]:
    entries: set[GatewayRouteInventoryEntry] = set()
    for route in _iter_routes(app):
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


def _iter_routes(app_or_router: object) -> Iterable[object]:
    for route in getattr(app_or_router, "routes", []):
        yield route
        included_router = getattr(route, "original_router", None)
        if included_router is None:
            include_context = getattr(route, "include_context", None)
            included_router = getattr(include_context, "included_router", None)
        if included_router is not None:
            yield from _iter_routes(included_router)


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


def _post_drill_cleanup_snippet(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    marker = "Post-drill cleanup"
    assert marker in text
    tail = text.split(marker, maxsplit=1)[1]
    assert "```bash" in tail
    return tail.split("```bash", maxsplit=1)[1].split("```", maxsplit=1)[0]


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
