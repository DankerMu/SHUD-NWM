"""First-import guard for ``services/orchestrator`` (#3032).

Every module under ``services/orchestrator`` must import cleanly as the FIRST
import of a fresh interpreter, except the modules in
:data:`FIRST_IMPORT_ALLOWLIST`.  The split modules import their facade
(``chain`` / ``scheduler`` / ``scheduler_evidence``) back; when that
back-import runs before the definitions the facade takes, a first import of the
split module dies in a partially initialised facade.  Production never imports
a split module first, so these cycles only bite tests, scripts and the REPL --
which is exactly why nothing else catches them.

Discovery is from the filesystem only and nothing is imported into the pytest
process: ``pkgutil.walk_packages`` would itself trip the cycles (and leave the
facades imported in this process, masking the very order it is meant to test).

The allowlist is exact and ratcheting: a listed module that starts importing
cleanly fails this guard until it is removed from the list.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
ORCHESTRATOR_DIR = REPO_ROOT / "services" / "orchestrator"
TRACEBACK_LINES = 20
SUBPROCESS_TIMEOUT_SECONDS = 120

# Modules that still cannot be imported first.  Making them import cleanly needs
# call-time rewrites (class bases, default arguments, copied namespaces), which
# #3032 deliberately left to a follow-up issue -- see
# openspec/changes/entropy-followups-2026-10/design.md.
FIRST_IMPORT_ALLOWLIST: dict[str, str] = {
    "services.orchestrator.chain_compat_static": (
        "builds CHAIN_COMPAT_STATIC_EXPORTS from _chain at import time, and chain.py:149 reads it mid-import"
    ),
    "services.orchestrator.chain_compat_runtime": (
        "namespace copy of _chain.__dict__ at line 6; chain.py:889 calls install_chain_runtime_compat() mid-import"
    ),
    "services.orchestrator.chain_analysis_orchestrator": (
        "class base _chain.ForecastOrchestrator is read at definition time, and chain import takes the class"
    ),
    "services.orchestrator.chain_forecast_orchestrator_cycle": (
        "chain.py:889 calls the mixin's _chain_stage_execution_dependencies() mid-import, which reads _chain"
    ),
    "services.orchestrator.scheduler_core": (
        "default argument _scheduler._CANONICAL_READINESS_PROVIDER_UNSET in ProductionScheduler.__init__"
    ),
    "services.orchestrator.scheduler_candidate_runtime": (
        "namespace copy globals().update(_scheduler.__dict__) at line 6; scheduler.py reads its EXPORTS mid-import"
    ),
    "services.orchestrator.scheduler_evidence_payload": (
        "default argument _scheduler_evidence.MAX_EVIDENCE_BYTES in bounded_evidence_payload"
    ),
    "services.orchestrator.scheduler_compat_runtime": (
        "namespace copy globals().update(_scheduler.__dict__) at line 6; scheduler.py reads its EXPORTS mid-import"
    ),
}


def _discover_modules() -> list[str]:
    names: set[str] = set()
    for path in ORCHESTRATOR_DIR.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        parts = path.relative_to(REPO_ROOT).with_suffix("").parts
        if parts[-1] == "__init__":
            parts = parts[:-1]
        names.add(".".join(parts))
    return sorted(names)


def _run_python(code: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", code, *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT_SECONDS,
        check=False,
    )


def _first_import(module: str) -> tuple[str, str | None]:
    completed = _run_python(f"import {module}")
    if completed.returncode == 0:
        return module, None
    head = (completed.stderr or completed.stdout).splitlines()[:TRACEBACK_LINES]
    return module, "\n".join(head)


def test_discovery_finds_orchestrator_modules_from_disk() -> None:
    modules = _discover_modules()

    assert modules, f"no modules discovered under {ORCHESTRATOR_DIR}"
    assert "services.orchestrator" in modules
    assert "services.orchestrator.scheduler_config" in modules
    assert "services.orchestrator.scheduler_config.config" in modules
    assert not any(name.endswith(".__init__") for name in modules)
    assert set(FIRST_IMPORT_ALLOWLIST) <= set(modules), sorted(set(FIRST_IMPORT_ALLOWLIST) - set(modules))


def test_every_orchestrator_module_imports_first_in_a_fresh_interpreter() -> None:
    modules = _discover_modules()
    assert modules, f"no modules discovered under {ORCHESTRATOR_DIR}"

    with ThreadPoolExecutor(max_workers=min(8, os.cpu_count() or 1)) as pool:
        failures = {module: tb for module, tb in pool.map(_first_import, modules) if tb is not None}

    unexpected = sorted(set(failures) - set(FIRST_IMPORT_ALLOWLIST))
    now_clean = sorted(set(FIRST_IMPORT_ALLOWLIST) - set(failures))
    report: list[str] = []
    for module in unexpected:
        report.append(f"--- {module} fails as first import:\n{failures[module]}")
    for module in now_clean:
        report.append(f"--- {module} now imports cleanly first; remove it from FIRST_IMPORT_ALLOWLIST")
    assert set(failures) == set(FIRST_IMPORT_ALLOWLIST), f"failing modules: {sorted(failures)}\n" + "\n".join(report)


_SCHEDULER_FACADE_PROBE = """
import importlib, json, sys
for name in sys.argv[1:]:
    importlib.import_module(name)
scheduler = importlib.import_module("services.orchestrator.scheduler")
config_cls = getattr(scheduler, "ProductionSchedulerConfig")
scheduler_config = importlib.import_module("services.orchestrator.scheduler_config")
print(json.dumps({
    "keys": sorted(k for k in vars(scheduler) if not (k.startswith("__") and k.endswith("__"))),
    "scheduler_module": scheduler.ProductionScheduler.__module__,
    "config_module": config_cls.__module__,
    "config_is_owner": config_cls is scheduler_config.ProductionSchedulerConfig,
    "config_is_global": vars(scheduler)["ProductionSchedulerConfig"] is config_cls,
}))
"""

_CHAIN_FACADE_PROBE = """
import importlib, json, sys
for name in sys.argv[1:]:
    importlib.import_module(name)
chain = importlib.import_module("services.orchestrator.chain")
cycle = importlib.import_module("services.orchestrator.chain_forecast_orchestrator_cycle")
runtime = importlib.import_module("services.orchestrator.chain_forecast_orchestrator_runtime")
mixins = chain.ForecastOrchestrator.__mro__[1:3]
print(json.dumps({
    "keys": sorted(k for k in vars(chain) if not (k.startswith("__") and k.endswith("__"))),
    "mixins": [[cls.__module__, cls.__qualname__] for cls in mixins],
    "mixins_are_owners": [
        mixins[0] is cycle.ForecastOrchestratorCycleMixin,
        mixins[1] is runtime.ForecastOrchestratorRuntimeMixin,
    ],
}))
"""


def _probe(code: str, *preludes: str) -> dict[str, object]:
    completed = _run_python(code, *preludes)
    assert completed.returncode == 0, "\n".join(completed.stderr.splitlines()[-TRACEBACK_LINES:])
    return json.loads(completed.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize(
    "prelude",
    ["services.orchestrator.scheduler_types", "services.orchestrator.scheduler_config.path_modes"],
)
def test_scheduler_facade_is_identical_when_a_split_module_is_imported_first(prelude: str) -> None:
    facade_first = _probe(_SCHEDULER_FACADE_PROBE)
    split_first = _probe(_SCHEDULER_FACADE_PROBE, prelude)

    assert split_first["keys"] == facade_first["keys"]
    assert "ProductionSchedulerConfig" in facade_first["keys"]
    for probe in (facade_first, split_first):
        assert probe["scheduler_module"] == "services.orchestrator.scheduler"
        assert probe["config_module"] == "services.orchestrator.scheduler"
        assert probe["config_is_owner"] is True
        assert probe["config_is_global"] is True


def test_chain_facade_is_identical_when_the_runtime_mixin_is_imported_first() -> None:
    facade_first = _probe(_CHAIN_FACADE_PROBE)
    split_first = _probe(_CHAIN_FACADE_PROBE, "services.orchestrator.chain_forecast_orchestrator_runtime")

    assert split_first["keys"] == facade_first["keys"]
    expected_mixins = [
        ["services.orchestrator.chain_forecast_orchestrator_cycle", "ForecastOrchestratorCycleMixin"],
        ["services.orchestrator.chain_forecast_orchestrator_runtime", "ForecastOrchestratorRuntimeMixin"],
    ]
    for probe in (facade_first, split_first):
        assert probe["mixins"] == expected_mixins
        assert probe["mixins_are_owners"] == [True, True]
