## Context

Only #3032 needs a design. The other six issues carry their exact edits in their issue bodies and in `tasks.md`. This design incorporates the fixture review of 2026-10-19 (verdict revise → amended). That review measured the base `e038cf4b9` in a scratch export, trying each mechanism module by module.

## #3032: first-import cycles in `services/orchestrator`

### Measured state (base `e038cf4b9`)

- 119 modules were each imported first in a fresh interpreter. 17 fail: the 16 named in the issue plus the `services.orchestrator.scheduler_config` package `__init__`.
- Three facades close cycles:
  - `chain`, at `chain.py:147-149`, `:521-522` and `:887-889`;
  - `scheduler`, at `scheduler.py:43`, `:374-377`, `:380`, `:410` and `:738-742`;
  - `scheduler_evidence`, at its tail imports `:520` and `:737`. The `scheduler_evidence_{payload,proofs}` modules import it back.
- `scheduler_adapters` has no back-import. Its cycle is adapters `:18` → `scheduler_types` `:9` → `scheduler.py:43`.

### Decision (the parent workflow chose the hybrid)

Fix only what a pure import move or a single lazy name fixes, with no function body, default argument, or class body change. Pin everything else in an exact, ratcheting allowlist inside the guard test, and file a follow-up issue for the rest. This is the issue's own "备选" for the remainder. Making the other 8 import cleanly first needs call-time rewrites of class bases, default arguments and copied namespaces (`globals().update(<facade>.__dict__)`). That is a compat-layer redesign for a p2 that only tests, scripts and the REPL hit. It is out of proportion here.

Allowed mechanisms:
- **(A) Move the back-import.** In a split module whose top-level code reads the facade only at call time, move `from services.orchestrator import chain as _chain` / `scheduler as _scheduler` / `scheduler_evidence as _scheduler_evidence` to just after the last definition the facade takes, with `# noqa: E402`. Any top-level statement that reads that alias at import time moves with it, in its original order.
- **(A′) Move the cycle-closing import, for a module with no back-import.** Move the cross-module import that closes the cycle through a facade to after the definitions the facade takes. The case here is `scheduler_adapters.py:18`, `from services.orchestrator.scheduler_types import SchedulerCandidate`.
- **(B) PEP 562 lazy binding, for `scheduler.ProductionSchedulerConfig` only** (replacing `scheduler.py:374-377`).
  - A module `__getattr__` resolves the name and sets `__module__ = "services.orchestrator.scheduler"` exactly once.
  - It stores the object in `globals()` before returning, so later `monkeypatch.setattr` and `from … import` behave as now.
  - No code in `scheduler.py`, and no module that copies the scheduler namespace (`globals().update(_scheduler.__dict__)` in `scheduler_candidate_runtime`, `scheduler_compat_runtime`, `chain_compat_runtime`), may read `ProductionSchedulerConfig` as a bare global. The implementer greps this and records it in the PR.
  - The PR also proves `scheduler.ProductionSchedulerConfig.__module__ == "services.orchestrator.scheduler"` after first access, in both import orders.
- Anything else is out of bounds: B for any other name, sys.modules tricks, try/except ImportError fallbacks, and edits to function bodies, default arguments or class bodies.

Expected outcome. The implementer confirms each row on the branch.

| Module | Fix |
|---|---|
| `chain_forecast_orchestrator_runtime`, `scheduler_types`, `scheduler_gateway`, `scheduler_evidence_proofs` | A |
| `scheduler_adapters` | A′, together with A on `scheduler_types` |
| `scheduler_config` package plus `.config`, `.path_modes`, `.db_free` | B |
| `chain_compat_static`, `chain_compat_runtime`, `chain_analysis_orchestrator`, `chain_forecast_orchestrator_cycle`, `scheduler_core`, `scheduler_candidate_runtime`, `scheduler_evidence_payload`, `scheduler_compat_runtime` | allowlist (8), one-line reason each, from the fixture review's per-module findings |

If a row marked A, A′ or B turns out not to work, that module moves to the allowlist with its reason. A row never moves the other way by a new mechanism.

### Must preserve

- For `chain` and `scheduler`: the non-dunder key set of `vars(facade)`, after forcing the lazy name with `getattr`, and per-key object identity within one interpreter.
- `chain.ForecastOrchestrator.__mro__[1:3] == (ForecastOrchestratorCycleMixin, ForecastOrchestratorRuntimeMixin)`.
- `scheduler.ProductionScheduler.__module__ == "services.orchestrator.scheduler"`, and the same for `ProductionSchedulerConfig`.
- Every monkeypatch target on the facades. Split modules keep reading facade globals as attribute reads on the module object.
- Fresh-interpreter import of `services.orchestrator.{cli,chain,scheduler,monitoring}`, and through them the production entry points:
  - `infra/systemd/node22/nhms-compute-scheduler.service:13`;
  - `scripts/ops/node22-run-cycle-once.sh:95`;
  - `infra/docker/entrypoint.sh:6`;
  - the `pyproject.toml` console scripts.
- Runtime behaviour. Only import statements move, plus the single `__getattr__`.

Governing invariant: every module under `services/orchestrator`, discovered from disk, imports cleanly first in a fresh interpreter, except the modules in the guard's allowlist. The allowlist is exact: a listed module that starts importing cleanly fails the guard until it is removed.

### Sibling surfaces

- The 29 top-level back-importers of `chain`/`scheduler`, plus the `scheduler_evidence` back-importers. The 15 that pass today must still pass.
- `tests/test_select_ci_tests.py` `INTENTIONAL_RULE_GAP_EXCLUSIONS` (`:11606-11624`): rows that exist only because of the `test_preserve_final_component_loop_spelling.py` workaround import. Its stale-entry guard (`:12078`/`:12116`) fails if those rows outlive the import.
- The `# import order` workarounds in `tests/test_gateway_reconcile_identity_release.py:492` and `tests/test_orchestrator_demote_reclaim_lifecycle.py:349` concern `chain_forecast_orchestrator_cycle`, which stays allowlisted. They stay.
- CI selector routing for the new guard file. See task 2.5.

### Seams under test

`tests/test_orchestrator_first_import.py`:
- **Discovery:** from the filesystem only. `Path("services/orchestrator").rglob("*.py")` (excluding `__pycache__`) is mapped to dotted names, with `__init__.py` mapped to its package and `services.orchestrator` itself included. Nothing is imported in the pytest process. Assert the list is non-empty. `pkgutil.walk_packages` is not allowed: at base it raises the `scheduler_config` AttributeError, which is not an ImportError, and at head it imports the facades into the pytest process.
- **One fresh-interpreter subprocess per module**, run on a thread pool capped at `min(8, os.cpu_count() or 1)` workers. The review measured 4.2 s wall time for all 119 modules at 8 workers.
- **Failures** report the module name and the first 20 traceback lines.
- **Allowlist assertion:** the set of failing modules equals the allowlist exactly.
- **Identity case:** in one subprocess, import a fixed split module first (`scheduler_types`, then `scheduler_config.path_modes`) and then the facade. Compare against a facade-first subprocess on the non-dunder `vars()` key set after forcing the lazy name, the `__module__` values, and the MRO.

### Required evidence

- Guard red at base, listing the 17 failing modules.
- Guard green at head, with exactly the allowlisted modules failing.
- Drill 1: move one A-fixed module's back-import back to the top. The guard goes red and names that module.
- Drill 2: remove one allowlist entry. The guard goes red. Restore both drills.
- Fresh-interpreter import of `cli`/`chain`/`scheduler`/`monitoring`.
- Targeted suites and the selector union are green, plus one `workflow_dispatch` full sharded unit run. See `tasks.md`.

### Residual risk (accepted)

Under B, a bare `import services.orchestrator.scheduler` that does not go through `cli` leaves `ProductionSchedulerConfig` out of `vars(scheduler)`, and its `__module__` stays `…scheduler_config.config` until the name is first accessed. Base sets both at import time. Through `cli` (every scheduler-loading production entry point; `nhms-monitor` never loads `scheduler`), the name is resolved during import, so behaviour is unchanged. Test 2.3 pins the post-access `__module__`.

A second consequence, found in the PR #3041 review: `scheduler_candidate_runtime` and `scheduler_compat_runtime` copy the scheduler namespace with `globals().update(_scheduler.__dict__)` before the name is resolved, so the copy does not hold it. `typing.get_type_hints` on three helpers whose string annotations name it therefore raises `NameError`, while master resolves them. The helpers are `_now`, `_scheduler_resolved_runtime_roots` and `_scheduler_runtime_config_evidence`. No caller introspects these annotations, and both modules use `from __future__ import annotations`, so runtime behaviour does not change. #3040 tracks the fix together with the namespace-copy rewrite.

### Non-goals

- The 8 allowlisted modules. They are the follow-up issue, filed in the same PR.
- Removing the back-import convention or the namespace-copy compat modules.
- More facade shrinking.
- The `packages/common` → `workers` cycle (the #2920 line).

### Review focus

1. Each fix is A, A′ or the single-name B, with no body, default-argument or class-body edits.
2. B's bare-global grep covers namespace-copying modules.
3. Moved statements keep their relative order, and facade identity is unchanged.
4. The allowlist is exact and ratchets, and discovery is non-empty.
5. Selector routing reaches the guard from every `services/orchestrator` path through one non-stop row at index 0 with the distinct pattern `services/orchestrator/*.py`, and the exact-equality selection pins are updated only by adding the guard file.
