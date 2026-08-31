"""T5b delegated-connect audit for node-27 production attribution.

Issue #1714 T5b lives here so the per-file AST registry in
``tests/test_node27_connection_attribution.py`` stays under the 1000-line
budget. Shared helpers/constants are imported from that module by alias so
pytest does not collect this file's helpers as extra tests, and so the
registered-component table stays a single source of truth.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

import tests.test_node27_connection_attribution as attribution
from packages.common import display_coverage, display_watermark
from scripts import (
    node27_cold_residency,
    node27_raw_retention,
    node27_refresh_coverage,
    node27_timeseries_compression,
    node27_timeseries_retention,
)

REGISTERED_COMPONENTS = attribution.REGISTERED_COMPONENTS
REPO_ROOT = attribution.REPO_ROOT
DSN = attribution.DSN
_ConnectIntercepted = attribution._ConnectIntercepted
_probe_psycopg2_connect = attribution._probe_psycopg2_connect
_is_psycopg2_connect = attribution._is_psycopg2_connect
_is_create_engine = attribution._is_create_engine

# --------------------------------------------------------------------------- #
# T5b -- delegated connect surfaces: discovery + classification
#
# The per-file guard above walks ONLY the registered files, so a connect site a
# registered component delegates into an imported helper is structurally
# invisible to it. That blindness is what let two unattributed production
# connect sites ship green. The guard below closes the class in two halves:
#
#   discovery     -- walk each registered component's transitive first-party
#                    import closure and collect every module that owns a DB
#                    connect surface. A new delegation shows up here whether or
#                    not anyone remembered to register it.
#   classification-- DELEGATED_CONNECT_CLOSURE must classify each discovered
#                    module as ``attributed`` (the component injects its own
#                    attributed connect callable) or ``unreachable`` (in the
#                    import closure, but no call path from this entrypoint
#                    reaches its connect surface -- with the reason recorded).
#
# The registry is the single source of truth: adding a delegation, dropping an
# attribution, or removing the helper's injection seam all turn this red.
#
# Honest limits -- what this guard still cannot catch:
#   * discovery is over the STATIC import graph, so ``importlib``/plugin-style
#     dynamic imports are invisible;
#   * ``unreachable`` verdicts are human call-path judgements pinned as text,
#     not proofs -- a later edit that makes an ``unreachable`` module genuinely
#     reachable keeps this green (the registry row must be re-read by a human);
#   * only ``psycopg2.connect`` and ``create_engine`` are recognised connect
#     surfaces; psycopg3 (``psycopg.connect``, e.g.
#     ``services/orchestrator/file_orchestration_migration.py``), asyncpg or raw
#     libpq would slip through;
#   * inside a registered file, aliasing (``c = psycopg2.connect; c(dsn)``)
#     defeats the per-file call-node scan above;
#   * subprocess-spawned components (autopipe -> ingest_run / output_parser /
#     refresh_coverage) are not import edges; they are covered only because each
#     is separately registered here.
# --------------------------------------------------------------------------- #
FIRST_PARTY_ROOTS = ("scripts", "workers", "packages", "apps", "services")

ATTRIBUTED = "attributed"
UNREACHABLE = "unreachable"

# (registered component, connect-owning module in its import closure, verdict,
#  detail). For ``attributed`` the detail is the helper function the component
# must call with an attributed ``connect=``; for ``unreachable`` it is the
# recorded reason no call path reaches that module's connect surface.
DELEGATED_CONNECT_CLOSURE: tuple[tuple[str, str, str, str], ...] = (
    (
        "scripts/node27_autopipeline.py",
        "workers/model_registry/basins_registry_import.py",
        UNREACHABLE,
        "autopipeline imports only _backfill_output_segment_geometry(cursor, ...) and hands it a "
        "cursor from its own attributed _connect; basins_registry_import._transaction is never called",
    ),
    (
        "scripts/node27_refresh_coverage.py",
        "packages/common/display_coverage.py",
        ATTRIBUTED,
        "refresh_all_run_display_coverage",
    ),
    (
        "scripts/node27_refresh_coverage.py",
        "packages/common/forecast_store.py",
        UNREACHABLE,
        "display_coverage imports only the constants MVP_STATION_VARIABLES / "
        "QHH_LATEST_EXPECTED_HORIZON_HOURS; PsycopgForecastStore is never constructed on this path",
    ),
    (
        "apps/api/routes/hydro_display.py",
        "apps/api/routes/pipeline.py",
        UNREACHABLE,
        "hydro_display imports only the _ok response helper; pipeline._engine belongs to the "
        "control-plane routes and is never reached from a display route",
    ),
    (
        "scripts/node27_timeseries_retention.py",
        "packages/common/display_watermark.py",
        ATTRIBUTED,
        "fetch_display_watermark",
    ),
    (
        "scripts/node27_timeseries_compression.py",
        "packages/common/display_watermark.py",
        ATTRIBUTED,
        "fetch_display_watermark",
    ),
    (
        "scripts/node27_raw_retention.py",
        "packages/common/display_watermark.py",
        ATTRIBUTED,
        "fetch_display_watermark",
    ),
    (
        "scripts/node27_cold_residency.py",
        "packages/common/display_watermark.py",
        ATTRIBUTED,
        "fetch_display_watermark",
    ),
)

# The keyword every delegated helper exposes so a caller can inject its own
# attributed connect callable, and the module-level wrapper each registered
# component passes through it.
DELEGATED_CONNECT_KEYWORD = "connect"
ATTRIBUTED_CONNECT_WRAPPER = "_attributed_connect"


def _first_party_imports(path: Path) -> set[str]:
    """Dotted first-party module names imported by ``path`` (any nesting)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                package = path.parent.relative_to(REPO_ROOT).as_posix().replace("/", ".")
                for _ in range(node.level - 1):
                    package = package.rsplit(".", 1)[0]
                module = f"{package}.{node.module}" if node.module else package
            else:
                module = node.module or ""
            if not module:
                continue
            names.add(module)
            # ``from pkg.mod import name`` may name a submodule, not an object.
            names.update(f"{module}.{alias.name}" for alias in node.names)
    return {name for name in names if name.split(".")[0] in FIRST_PARTY_ROOTS}


def _module_path(dotted: str) -> Path | None:
    module = REPO_ROOT / (dotted.replace(".", "/") + ".py")
    if module.is_file():
        return module
    package = REPO_ROOT / dotted.replace(".", "/") / "__init__.py"
    return package if package.is_file() else None


def _owns_connect_surface(path: Path) -> bool:
    """True if the module names ``psycopg2.connect`` / ``create_engine`` at all.

    Deliberately matches bare attribute REFERENCES, not just calls:
    ``display_watermark.py`` assigns ``connect = psycopg2.connect`` and calls it
    later, which a call-only scan misses entirely -- precisely the shape this
    guard exists to catch.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and (_is_psycopg2_connect(node.func) or _is_create_engine(node.func)):
            return True
        if isinstance(node, ast.Attribute) and _is_psycopg2_connect(node):
            return True
        if isinstance(node, ast.Attribute) and node.attr == "create_engine":
            return True
    return False


def _connect_owning_closure(relative_path: str) -> set[str]:
    """Modules with a connect surface reachable BY IMPORT from a component.

    Registered components are excluded: each is covered by its own per-file
    guard above, so re-reporting them here would be noise.
    """
    registered = {path for path, _name in REGISTERED_COMPONENTS}
    entry = REPO_ROOT / relative_path
    seen_modules: set[str] = set()
    visited: set[Path] = set()
    pending = [entry]
    owners: set[str] = set()
    while pending:
        current = pending.pop()
        if current in visited:
            continue
        visited.add(current)
        current_relative = current.relative_to(REPO_ROOT).as_posix()
        if current is not entry and current_relative not in registered and _owns_connect_surface(current):
            owners.add(current_relative)
        for dotted in _first_party_imports(current):
            if dotted in seen_modules:
                continue
            seen_modules.add(dotted)
            resolved = _module_path(dotted)
            if resolved is not None:
                pending.append(resolved)
    return owners


@pytest.mark.parametrize(
    ("relative_path", "expected_name"),
    REGISTERED_COMPONENTS,
    ids=[path for path, _ in REGISTERED_COMPONENTS],
)
def test_every_delegated_connect_surface_is_classified(relative_path: str, expected_name: str) -> None:
    """Discovery half: nothing may connect on a component's behalf unregistered."""
    discovered = _connect_owning_closure(relative_path)
    classified = {
        module
        for component, module, _verdict, _detail in DELEGATED_CONNECT_CLOSURE
        if component == relative_path
    }
    assert discovered == classified, (
        f"{relative_path} ({expected_name}): the set of connect-owning modules in its import "
        f"closure moved. Unclassified: {sorted(discovered - classified)}; "
        f"stale registry rows: {sorted(classified - discovered)}. Add each new module to "
        "DELEGATED_CONNECT_CLOSURE as 'attributed' (inject an attributed connect callable) or "
        "'unreachable' (with the reason no call path reaches it)."
    )


@pytest.mark.parametrize(
    ("component", "helper_module", "helper_function"),
    [
        (component, module, detail)
        for component, module, verdict, detail in DELEGATED_CONNECT_CLOSURE
        if verdict == ATTRIBUTED
    ],
    ids=[
        f"{component}->{detail}"
        for component, _module, verdict, detail in DELEGATED_CONNECT_CLOSURE
        if verdict == ATTRIBUTED
    ],
)
def test_delegated_helper_is_called_with_an_attributed_connect(
    component: str, helper_module: str, helper_function: str
) -> None:
    """Classification half, caller side: every call site injects the wrapper."""
    tree = ast.parse((REPO_ROOT / component).read_text(encoding="utf-8"))
    sites = 0
    unattributed: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        called = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None
        if called != helper_function:
            continue
        sites += 1
        injected = next(
            (kw.value for kw in node.keywords if kw.arg == DELEGATED_CONNECT_KEYWORD),
            None,
        )
        if not (isinstance(injected, ast.Name) and injected.id == ATTRIBUTED_CONNECT_WRAPPER):
            unattributed.append(node.lineno)

    assert sites >= 1, f"{component} no longer calls {helper_function}; the registry row is stale"
    assert unattributed == [], (
        f"{component} lines {unattributed} call {helper_function} (which opens its own "
        f"connection in {helper_module}) without {DELEGATED_CONNECT_KEYWORD}="
        f"{ATTRIBUTED_CONNECT_WRAPPER}, so that connection lands in pg_stat_activity unattributed"
    )


@pytest.mark.parametrize(
    ("helper_module", "helper_function"),
    sorted({(module, detail) for _c, module, verdict, detail in DELEGATED_CONNECT_CLOSURE if verdict == ATTRIBUTED}),
    ids=lambda value: value if isinstance(value, str) else str(value),
)
def test_delegated_helper_still_exposes_the_connect_injection_seam(
    helper_module: str, helper_function: str
) -> None:
    """Classification half, helper side: the seam may not be removed."""
    module = {
        "packages/common/display_watermark.py": display_watermark,
        "packages/common/display_coverage.py": display_coverage,
    }[helper_module]
    parameters = inspect.signature(getattr(module, helper_function)).parameters
    assert DELEGATED_CONNECT_KEYWORD in parameters, (
        f"{helper_module}::{helper_function} dropped its {DELEGATED_CONNECT_KEYWORD}= seam; "
        "its callers can no longer attribute the connection it opens"
    )
    parameter = parameters[DELEGATED_CONNECT_KEYWORD]
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    # Bound at call time, never in the signature default: an import-time default
    # would bypass a monkeypatched psycopg2.connect and silently drop callers.
    assert parameter.default is None


@pytest.mark.parametrize(
    ("component", "expected_name"),
    sorted(
        {
            (component, dict(REGISTERED_COMPONENTS)[component])
            for component, _m, verdict, _d in DELEGATED_CONNECT_CLOSURE
            if verdict == ATTRIBUTED
        }
    ),
    ids=lambda value: value,
)
def test_attributed_connect_wrapper_stamps_the_component_identity(
    monkeypatch: pytest.MonkeyPatch, component: str, expected_name: str
) -> None:
    """The injected callable itself: what a helper ends up handing to libpq."""
    module = {
        "scripts/node27_refresh_coverage.py": node27_refresh_coverage,
        "scripts/node27_timeseries_retention.py": node27_timeseries_retention,
        "scripts/node27_timeseries_compression.py": node27_timeseries_compression,
        "scripts/node27_raw_retention.py": node27_raw_retention,
        "scripts/node27_cold_residency.py": node27_cold_residency,
    }[component]
    probe = _probe_psycopg2_connect(monkeypatch)

    with pytest.raises(_ConnectIntercepted):
        getattr(module, ATTRIBUTED_CONNECT_WRAPPER)(DSN, connect_timeout=5)

    assert probe.args == (DSN,)
    assert probe.kwargs == {"fallback_application_name": expected_name, "connect_timeout": 5}


def test_delegated_closure_registry_is_well_formed() -> None:
    """Registry hygiene: no unknown verdicts, no rows for unregistered files."""
    registered = {path for path, _name in REGISTERED_COMPONENTS}
    for component, helper_module, verdict, detail in DELEGATED_CONNECT_CLOSURE:
        assert component in registered, f"{component} is not a registered component"
        assert verdict in {ATTRIBUTED, UNREACHABLE}
        assert (REPO_ROOT / helper_module).is_file()
        assert detail.strip(), f"{component} -> {helper_module} needs a reason/helper name"
