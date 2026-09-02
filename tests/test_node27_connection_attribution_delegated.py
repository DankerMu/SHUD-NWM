"""T5b delegated-connect audit for node-27 production attribution.

Two guards live here, both about connect surfaces a registered component
DELEGATES into an imported helper:

* T5b (#1714) -- module-level discovery + classification
  (``DELEGATED_CONNECT_CLOSURE``);
* T5c (#1726) -- function-level closure inside an ``attributed`` module, so a
  SECOND connect-opening function cannot inherit the module's verdict.

Shared helpers/constants (the import-graph walk, the verdict vocabulary, the
registered-component table) are aliased from
``tests/test_node27_connection_attribution.py``, which owns them because the
unit-level guard added for #1728 needs the same walk and this module already
imports that one. Aliasing (rather than re-defining) also keeps pytest from
collecting this file's helpers as extra tests.
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
# Owned by tests/test_node27_connection_attribution.py since #1728: the
# unit-level closure guard there needs the same vocabulary and the same
# import-graph walk, and this module already imports that one.
FIRST_PARTY_ROOTS = attribution.FIRST_PARTY_ROOTS
ATTRIBUTED = attribution.ATTRIBUTED
UNREACHABLE = attribution.UNREACHABLE
_first_party_imports = attribution._first_party_imports
_module_path = attribution._module_path
_owns_connect_surface = attribution._owns_connect_surface

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


# --------------------------------------------------------------------------- #
# T5c (#1726) -- function-level closure inside an ATTRIBUTED delegated module
#
# The classification half above is per MODULE: once
# ``packages/common/display_watermark.py`` was classified ``attributed``,
# because ``fetch_display_watermark`` exposes a ``connect=`` seam, a SECOND
# connection-opening function added to the same file inherited that verdict and
# shipped unattributed with every guard green.
#
# The rule closed here: in an attributed delegated module, EVERY function that
# names a connect surface must expose the keyword-only ``connect`` seam through
# which its caller injects an attributed connect. Functions are read from the
# AST, not from ``inspect``, so a function added tomorrow is covered without
# anyone registering it.
#
# Limits inherited from the discovery half: static analysis only, and a helper
# that opens a connection through an alias this file does not recognise
# (psycopg3, asyncpg) is still invisible.
# --------------------------------------------------------------------------- #
ATTRIBUTED_HELPER_MODULES: tuple[str, ...] = tuple(
    sorted({module for _c, module, verdict, _d in DELEGATED_CONNECT_CLOSURE if verdict == ATTRIBUTED})
)

_FUNCTION_NODES = (ast.FunctionDef, ast.AsyncFunctionDef)


def _enclosing_function(tree: ast.Module, target: ast.AST) -> ast.AST | None:
    """Innermost function containing ``target``, or None at module level.

    Innermost, so a nested worker that merely CALLS an injected callable is not
    charged with its parent's connect reference (``display_coverage``'s
    ``refresh_one`` calls ``open_connection``; the seam belongs to the enclosing
    ``refresh_all_run_display_coverage``).
    """
    enclosing: ast.AST | None = None
    for node in ast.walk(tree):
        if not isinstance(node, _FUNCTION_NODES):
            continue
        if any(child is target for child in ast.walk(node)):
            if enclosing is None or any(child is node for child in ast.walk(enclosing)):
                enclosing = node
    return enclosing


def _names_a_connect_surface(node: ast.AST) -> bool:
    if isinstance(node, ast.Call) and (_is_psycopg2_connect(node.func) or _is_create_engine(node.func)):
        return True
    if isinstance(node, ast.Attribute) and (_is_psycopg2_connect(node) or node.attr == "create_engine"):
        return True
    return False


def _exposes_connect_seam(function: ast.AST) -> bool:
    return isinstance(function, _FUNCTION_NODES) and any(
        argument.arg == DELEGATED_CONNECT_KEYWORD for argument in function.args.kwonlyargs
    )


def _connect_opening_functions_without_a_seam(helper_module: str) -> tuple[int, list[str]]:
    """(connect-opening functions found, names of those missing the seam)."""
    tree = ast.parse((REPO_ROOT / helper_module).read_text(encoding="utf-8"))
    owners: dict[str, bool] = {}
    for node in ast.walk(tree):
        if not _names_a_connect_surface(node):
            continue
        # An Attribute inside a Call node is the same site; count it once.
        function = _enclosing_function(tree, node)
        if function is None:
            owners["<module-level>"] = False
            continue
        owners[function.name] = _exposes_connect_seam(function)
    return len(owners), sorted(name for name, has_seam in owners.items() if not has_seam)


@pytest.mark.parametrize("helper_module", ATTRIBUTED_HELPER_MODULES, ids=lambda value: value)
def test_every_connect_opening_function_in_an_attributed_module_exposes_the_seam(helper_module: str) -> None:
    found, without_seam = _connect_opening_functions_without_a_seam(helper_module)
    assert found >= 1, f"{helper_module} no longer opens any connection; its 'attributed' rows are stale"
    assert without_seam == [], (
        f"{helper_module} opens a database connection in "
        f"{[f'{helper_module}::{name}' for name in without_seam]} without a keyword-only "
        f"{DELEGATED_CONNECT_KEYWORD}= parameter, so the calling component cannot attribute it and "
        "that connection lands in pg_stat_activity unattributed"
    )


def test_function_level_guard_rejects_a_second_unattributed_connect_function() -> None:
    """Meta-proof: the predicate itself, on the exact escape shape from #1726."""
    drifted = ast.parse(
        "import psycopg2\n"
        "def seamed(url, *, connect=None):\n"
        "    open_connection = connect if connect is not None else psycopg2.connect\n"
        "    return open_connection(url)\n"
        "def unseamed(url):\n"
        "    return psycopg2.connect(url)\n"
        "def positional_only_seam(url, connect=None):\n"
        "    return psycopg2.connect(url)\n"
    )
    verdicts = {
        node.name: _exposes_connect_seam(node)
        for node in ast.walk(drifted)
        if isinstance(node, _FUNCTION_NODES)
    }
    assert verdicts == {"seamed": True, "unseamed": False, "positional_only_seam": False}
    # And the enclosing-function attribution charges the right function.
    connect_nodes = [node for node in ast.walk(drifted) if _names_a_connect_surface(node)]
    charged = {
        getattr(_enclosing_function(drifted, node), "name", "<module-level>") for node in connect_nodes
    }
    assert charged == {"seamed", "unseamed", "positional_only_seam"}
