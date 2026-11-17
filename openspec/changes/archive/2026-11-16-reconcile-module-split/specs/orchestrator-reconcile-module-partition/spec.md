## ADDED Requirements

### Requirement: The restart-reconcile module SHALL be partitioned under the large-file guard without changing its import surface

`services/orchestrator/reconcile.py` SHALL be a compatibility facade over cohesive leaf modules
`services/orchestrator/reconcile_<topic>.py`. Every module of the partition SHALL be at or under the large-file guard's
1000-line limit without a guard exclusion, except the single module that holds `reconcile_reserved_unbound_jobs`.
That function is one indivisible body over 1000 lines. Apart from imports and `LOGGER`, its module SHALL hold no other
top-level definitions except helpers whose only caller is that function and that tests patch as module attributes. It SHALL be the only
`services/orchestrator/reconcile*.py` entry in `.large-file-guard.json`.

Splitting SHALL NOT remove from `services.orchestrator.reconcile` any top-level name that `reconcile.py` itself
defined before the split, unless the change retires that name explicitly. Names that `reconcile.py` merely imported are
not part of that surface, except where What 2 of the change keeps them. Classes SHALL be single objects regardless of
import path.

Leaf modules SHALL NOT import the facade. Every name that is patched or reassigned as a module attribute SHALL be
patched on the module where its callers look it up:
- A name whose production readers resolve it through the facade at call time SHALL stay patchable on the facade.
- Every other patched name that the partition defines SHALL live in the same module as all of its in-module callers,
  and its patches SHALL target that module.
- A patched name that the partition imports from another package may have callers in several leaves. Its patches
  SHALL target the leaf whose caller the patching test actually drives.

Every module of the partition that logs SHALL use the logger named `services.orchestrator.reconcile`.

#### Scenario: Importers after the split
- **WHEN** any existing importer imports a name from `services.orchestrator.reconcile` that the change did not retire
- **THEN** the import succeeds when the name was defined by `reconcile.py` before the split
- **AND** the imported object is the object the leaf module defines

#### Scenario: A patched sacct bound
- **WHEN** a test patches `MAX_COMMENT_SACCT_ROWS` or `_bounded_sacct_stdout` on the module that owns the bounded sacct
  scan, and runs a comment-accounting query
- **THEN** the query observes the patched value, as before the split

#### Scenario: A facade-resolved default querier
- **WHEN** a test patches `services.orchestrator.reconcile.default_comment_sacct_querier`, and the scheduler runtime
  resolves the querier through its call-time import of the facade
- **THEN** the runtime uses the patched querier

#### Scenario: Reconcile log records keep their logger name
- **WHEN** any partition module emits a warning
- **THEN** the record's logger name is `services.orchestrator.reconcile`

#### Scenario: Only the indivisible function is exempt
- **WHEN** `.large-file-guard.json` and the line counts of `services/orchestrator/reconcile*.py` are inspected
- **THEN** exactly one partition module is excluded and it is the one that holds `reconcile_reserved_unbound_jobs`
- **AND** every other partition module is at or under 1000 lines
