# Tasks

Fixture level: `standard`. Risk packs: production scheduler import graph
(the facade imports about a hundred names from this module at its end;
several of this module's dependencies import the facade), latent `NameError`
on rarely executed paths.

Scope: #3335 item 5 only. Items 6 (chain.py dead imports) and 7 (test
renames) are a separate PR.

Must preserve:

- No function body in `scheduler_candidate_runtime.py` changes: only the
  import block, the `noqa` line, the seven deleted values and, if task 1.5's
  set check holds, the quality-forwarder loop, its `del` line and its tuple
  change.
- Every name the facade imports from this module
  (`from services.orchestrator.scheduler_candidate_runtime import (...)` in
  `scheduler.py`) still exists and is the same object kind; `scheduler.py` is
  not edited.
- `sorted(vars(services.orchestrator.scheduler))` and
  `sorted(vars(services.orchestrator.chain))` are identical before and after.
- The forwarder helper functions stay as they are.
- `tests/test_orchestrator_first_import.py`, `tests/test_facade_name_resolution.py`,
  `tests/test_production_scheduler.py`, `tests/test_scheduler_dbfree_only_mode.py`,
  `tests/test_scheduler_pass_status_without_dispatch.py`,
  `tests/test_operator_action_status_closure.py` pass; existing tests are not
  edited. The `FIRST_IMPORT_ALLOWLIST` entry for this module in
  `tests/test_orchestrator_first_import.py` is expected to stay valid (a
  first import still re-enters the facade through `scheduler_gateway`); if
  the measured outcome differs, stop and report instead of editing it.
- `tests/status_literal_allowlist.json` entries for this file stay valid.
- No comment added by this change carries a `path:line` reference.

## 1. Code (`services/orchestrator/scheduler_candidate_runtime.py`)

- [x] 1.1 Remove `from services.orchestrator import scheduler as _scheduler`
  and `globals().update(_scheduler.__dict__)`.
- [x] 1.2 Import explicitly, never from the facade. Exact sources:
  - `import importlib`; `from collections.abc import Callable, Mapping,
    Sequence`; `from pathlib import Path`; `from typing import TYPE_CHECKING,
    Any`;
  - `from services.orchestrator.chain import ForecastOrchestrator`;
  - `from services.orchestrator.scheduler_state_common import _ensure_utc,
    _evidence_safe` (the defining module; many other modules define a
    different `_ensure_utc`);
  - `from services.orchestrator import scheduler_candidates as
    _scheduler_candidates`, and likewise `scheduler_discovery`,
    `scheduler_evidence`, `scheduler_preflight`;
  - under `if TYPE_CHECKING:` only, `ProductionSchedulerConfig` and
    `SchedulerCandidate` from the modules that define them. If either turns
    out to be used at run time, stop and report instead of importing the
    facade.
- [x] 1.3 `# ruff: noqa` line: drop `F821` and `F401`; drop any other code
  on that line that no longer fires. No line-level `# noqa: F821` / `F401`
  and no `pyproject.toml` per-file ignore for this file replaces them.
  `uv run ruff check --no-cache services/orchestrator/scheduler_candidate_runtime.py`
  is clean.
- [x] 1.4 Delete the seven unread values named in the proposal. Re-check
  with `git grep -w` over `services apps workers scripts packages tests`
  that none has a reader before deleting it; a value that does have one
  stays and is reported.
- [x] 1.5 The `for ... in _SCHEDULER_CANDIDATE_QUALITY_FORWARDER_NAMES:`
  loop that installs forwarders through `globals()[...]`: if every name in
  the tuple is rebound by an explicit `name = _scheduler_candidate_quality_forwarder("name")`
  assignment below the loop (compare the two sets exactly), delete the loop,
  its `del` line and the tuple. If the sets differ, keep all three and
  report the difference.

## 2. Tests

- [x] 2.1 New `tests/test_scheduler_candidate_runtime_identity.py`. The test
  module imports `services.orchestrator.scheduler` before
  `scheduler_candidate_runtime` (a first import of the latter fails by
  design):
  - (a) module `__name__` is `services.orchestrator.scheduler_candidate_runtime`,
    `__file__` ends with `scheduler_candidate_runtime.py`, `__spec__.name`
    is the module name (red on master);
  - (b) every function object defined at module level in the file (collect
    the `def` names from the AST of the source) has `__module__` equal to
    the module name (red on master);
  - (c) no source file under `services/orchestrator` contains a call of the
    form `globals().update(...)` (AST check over the directory; red on
    master, where this file is the only hit);
  - (d) every name in the facade's
    `from services.orchestrator.scheduler_candidate_runtime import (...)`
    block (read from the AST of `scheduler.py`) is an attribute of the
    module and is the same object as the facade's attribute of that name;
  - (e) the file carries no `F821` / `F401` exemption: not on its
    `# ruff: noqa` line, not on any line-level `# noqa`, and not in
    `pyproject.toml` per-file ignores for this path (red on master);
  - (f) each of the seven orchestrator names the module imports for
    run-time use (`ForecastOrchestrator`, `_ensure_utc`, `_evidence_safe`,
    `_scheduler_candidates`, `_scheduler_discovery`, `_scheduler_evidence`,
    `_scheduler_preflight`) is the same object as the facade's attribute of
    that name (green on master). The standard-library names are the same
    object everywhere and are not pinned;
  - (g) `ProductionSchedulerConfig` and `SchedulerCandidate` appear in the
    file's AST only inside annotations or under the `TYPE_CHECKING` import
    (green on master apart from the import itself).
- [x] 2.2 `tests/test_orchestrator_first_import.py`: run it unchanged.

## 3. Evidence Floor

- [x] 3.1 Before/after record in `.workplans/3335/namespace-b.log`:
  `sorted(vars(scheduler))` and `sorted(vars(chain))` identical (digest and
  count); the module's own namespace: count before and after; the set of
  top-level `def` names and assignment targets in the file (from the AST)
  before and after, differing only by the deleted values (the seven, plus
  the quality tuple if 1.5 deleted it); and the measured traceback of a
  first import of this module in a fresh interpreter, before and after.
- [x] 3.2 Red run of 2.1 (a), (b), (c), (e) on the pre-change tree; (d),
  (f), (g) are green there.
- [x] 3.3 Mutations, each killed and restored:
  - M1 remove one explicit import that a function body needs (for example
    `_evidence_safe`) -> ruff `F821` red on the file.
  - M2 add an unused import -> ruff `F401` red on the file.
  - M3 restore the namespace copy -> 2.1 (a), (b), (c) red.
  - M4 delete one function the facade imports from the module -> 2.1 (d)
    red (and the facade import fails).
  - M5 put `F821` back on the `noqa` line -> 2.1 (e) red; add a
    `pyproject.toml` per-file ignore of `F821` for this path -> 2.1 (e) red.
  - M6 import `_ensure_utc` from `packages.common.state_manager` instead ->
    2.1 (f) red while ruff stays clean.
  - M7 use `SchedulerCandidate` in a function body (for example an
    `isinstance` check) -> 2.1 (g) red while ruff stays clean.
- [x] 3.4 Local: `uv run pytest -q tests/test_scheduler_candidate_runtime_identity.py
  tests/test_orchestrator_first_import.py tests/test_facade_name_resolution.py
  tests/test_scheduler_dbfree_only_mode.py tests/test_scheduler_pass_status_without_dispatch.py
  tests/test_operator_action_status_closure.py tests/test_production_scheduler.py`;
  `uv run ruff check --no-cache .`;
  `openspec validate explicit-candidate-runtime-imports --strict --no-interactive`.
- [ ] 3.5 node-27: the files of 3.4 plus `tests/test_orchestration_chain.py`,
  zero unexpected skips.

## Post-merge (not a task of this change)

Compute-node evidence after the next deploy: idle pass and submitting pass on
the new code. Recorded on #3335.
