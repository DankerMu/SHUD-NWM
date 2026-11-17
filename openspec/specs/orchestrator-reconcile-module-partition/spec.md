# orchestrator-reconcile-module-partition Specification

## Purpose
Keep `services/orchestrator/reconcile.py` under the large-file guard by splitting it into owning leaf modules
while the facade keeps the full import surface, so callers, monkeypatch targets and the logger name stay unchanged.

## Requirements

### Requirement: The restart-reconcile module SHALL be partitioned under the large-file guard with no exclusion and without changing its import surface

`services/orchestrator/reconcile.py` SHALL be a compatibility facade over cohesive leaf modules
`services/orchestrator/reconcile_<topic>.py`. Every module of the partition SHALL be at or under the large-file guard's
1000-line limit without a guard exclusion. `reconcile_reserved_unbound_jobs` SHALL delegate each reserved row to a
per-row function composed of ordered phase helpers, so that no single function body forces an exclusion. That
decomposition SHALL preserve, for every input, the ordered outcome list, the order of store and journal writes, the
per-row journal quarantine, the escaping exceptions and the lazy at-most-once comment-capability probe. No
`services/orchestrator/reconcile*.py` entry SHALL exist in `.large-file-guard.json`.

Splitting SHALL NOT remove from `services.orchestrator.reconcile` any top-level name that `reconcile.py` itself
defined before the split, unless the change retires that name explicitly. Names that `reconcile.py` merely imported are
not part of that surface, except where What 2 of #3155 keeps them. Classes SHALL be single objects regardless of
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

#### Scenario: No partition module is exempt
- **WHEN** `.large-file-guard.json` and the line counts of `services/orchestrator/reconcile*.py` are inspected
- **THEN** no partition module is excluded, because `reconcile_reserved_unbound_jobs` has been decomposed
- **AND** every partition module, including the one that holds `reconcile_reserved_unbound_jobs`, is at or under 1000
  lines

#### Scenario: The decomposed reserved reconcile behaves as the monolithic body did
- **WHEN** a reserved-unbound pass runs over rows that reach each of the per-row exits, including a journal fault
  after a fallback classification and an unavailable commit or bind API
- **THEN** the ordered outcomes, the ordered store and journal calls, the quarantine outcome's `fallback_match_basis`
  and the escaping exception are the same as before the decomposition
- **AND** the comment-capability probe runs at most once and never for an empty selection
