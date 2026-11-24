# Explicit imports for scheduler_candidate_runtime

## Why

`services/orchestrator/scheduler_candidate_runtime.py` starts with

```python
from services.orchestrator import scheduler as _scheduler
globals().update(_scheduler.__dict__)
```

(#3335 item 5). The copy is an import-time snapshot of the whole facade
namespace:

- It overwrites the module's own `__name__`, `__file__`, `__spec__`,
  `__package__` and `__doc__` with the facade's, so every function defined in
  the module reports `__module__ == "services.orchestrator.scheduler"`.
- It hides which names the module needs. Measured on master: 15 names come
  only from the copy (`Any`, `Callable`, `Mapping`, `Sequence`, `Path`,
  `importlib`, `ForecastOrchestrator`, `ProductionSchedulerConfig`,
  `SchedulerCandidate`, `_ensure_utc`, `_evidence_safe`,
  `_scheduler_candidates`, `_scheduler_discovery`, `_scheduler_evidence`,
  `_scheduler_preflight`). The file silences ruff's undefined-name rule
  (`F821`) to allow it, so a missing name would be a `NameError` on the
  code path that uses it.
- Seven module-level values have no reader anywhere:
  `_SCHEDULER_CANDIDATE_EXECUTION_EVIDENCE_FORWARDER_NAMES`,
  `_SCHEDULER_GATEWAY_FORWARDER_NAMES`,
  `_SCHEDULER_RUNTIME_ROOTS_FORWARDER_NAMES`, `_GATEWAY_SELF_HOSTS`,
  `_GATEWAY_HEALTH_PATH`, `_GATEWAY_REQUIRED_BINARIES`,
  `_GATEWAY_PROBE_TIMEOUT_SECONDS`.

## What Changes

- The namespace copy and the facade import are removed. Every name the
  module uses is imported explicitly from the module that owns it; the two
  classes used only in annotations are imported under `TYPE_CHECKING`.
- `F821` and `F401` leave the file's `ruff: noqa` line, so ruff checks the
  imports for undefined and unused names. Ruff treats a `TYPE_CHECKING`
  binding as bound, so a test pins that the two classes are used only in
  annotations, and another pins that each run-time name is the facade's
  object (a same-named helper from the wrong module would pass ruff).
- The seven unread values are deleted, and with them the
  `_SCHEDULER_CANDIDATE_QUALITY_FORWARDER_NAMES` loop if every name it
  installs is rebound by the explicit assignments right below it (it then
  has no effect).
- The module keeps its own identity; functions defined in it report it as
  their `__module__`.

## Behaviour

No function body changes. The objects the module binds are the same objects
the snapshot bound. A patch on the facade was not visible inside this module
before (snapshot) and is not visible now. Observable differences:
`scheduler_candidate_runtime.__name__` / `__file__` / `__spec__` are its own,
its functions' `__module__` is its own name (forwarders bound by assignment
included), and facade names the module never used are no longer attributes
of it. Two consequences of that last point, neither with a caller today:

- `typing.get_type_hints` on the functions annotated with
  `SchedulerCandidate` or `ProductionSchedulerConfig` no longer resolves
  those names from the module's globals (they are `TYPE_CHECKING` imports).
- The facade's module-level `__getattr__` was part of the copy, so
  `scheduler_candidate_runtime.ProductionSchedulerConfig` resolved lazily;
  it no longer does.

A first import of this module before the facade still fails as today: its
module-level `importlib.import_module` of `scheduler_gateway` re-enters the
facade. The first-import allowlist entry stays.

## Impact

- Code: `services/orchestrator/scheduler_candidate_runtime.py`.
- Tests: new identity test. `tests/test_orchestrator_first_import.py` is not
  edited; its allowlist entry for this module stays.
- Spec: `scheduler-facade-deepening` (one added requirement).
- Operations: production scheduler code; compute-node evidence after the
  next deploy.
