# Unify cutover_gate audit normalization; close manifest-channel silent degradation + 5 test gaps (#1097)

## Why

PR #1091 left two parallel implementations of the `cutover_gate` audit block
with different strictness:

- CLI side: `_normalize_cutover_gate_audit`
  (`scripts/publish_scheduler_file_registry.py:419-466`) — strict, 3 raise
  branches (`SCHEDULER_REGISTRY_CUTOVER_AUDIT_INVALID` on non-Mapping /
  mode ∉ `CUTOVER_GATE_MODES` / non-str `declaration_env`) + `None →
  "not_wired"` fallback.
- Manifest side: inline mirror in `publish_scheduler_registry_manifest`
  (`services/orchestrator/scheduler_file_providers.py:618-623`) — lenient:
  `str(cutover_gate.get("mode") or "not_wired")` silently rewrites an empty /
  wrong mode to `"not_wired"`, no `CUTOVER_GATE_MODES` validation, no
  `declaration_env` type check.

A future caller passing a malformed audit block hard-fails on the CLI channel
but silently degrades on the manifest channel — the operator then reads
contradictory audit facts from the `manifest-last.json` companion receipt vs
the CLI summary. All 4 strict branches and the runner→manifest audit
passthrough have zero test coverage
(`grep -rn "_normalize_cutover_gate_audit\|SCHEDULER_REGISTRY_CUTOVER_AUDIT_INVALID" tests/`
→ no hits, re-verified 2026-07-25), so any refactor can regress silently.

## What Changes

- New shared module `packages/scheduler/registry_audit.py` (new `packages/
  scheduler/` package, aligned with the #1100 split direction): single
  definition point for `CUTOVER_GATE_MODES`, `SchedulerRegistryPublishError`,
  and `normalize_cutover_gate_audit`.
- `scripts/publish_scheduler_file_registry.py`: imports and re-exports the
  three names (module-level aliases so existing
  `from scripts.publish_scheduler_file_registry import SchedulerRegistryPublishError`
  style consumers keep working); deletes the local definitions. CLI arguments,
  exit codes, error serialization, and summary/receipt field semantics
  unchanged for all currently-valid inputs.
- `services/orchestrator/scheduler_file_providers.py`:
  `publish_scheduler_registry_manifest` replaces the inline lenient mirror
  with the shared normalizer — the manifest channel becomes fail-closed on
  malformed audit input (behavior change, intended; all in-tree callers pass
  well-formed dicts so no green path changes).
- Tests: 4 normalizer unit tests (non-Mapping / bad mode / bad
  declaration_env type / None fallback) + 1 runner-side e2e assertion that a
  completed `publish_all_basin_scheduler_registry` run embeds
  `cutover_gate == {"mode": "enforced", "declaration_env": <env>,
  "declaration_present": <bool>}` byte-for-byte in the manifest receipt.

## Out of Scope

- PR #1091's merged audit / reconciliation contract semantics.
- Scheduler warm/cold start and cutover declaration schema semantics.
- Any receipt field other than `cutover_gate`.
- The #1100/#1102 file splits themselves (this change only pre-seeds
  `packages/scheduler/` with the audit module; no other extraction).

## Impact

- Affected specs: `scheduler-registry-refresh` (ADDED requirement).
- Affected code: `scripts/publish_scheduler_file_registry.py`,
  `services/orchestrator/scheduler_file_providers.py`, new
  `packages/scheduler/registry_audit.py`,
  `tests/test_publish_scheduler_file_registry.py`,
  `tests/test_scheduler_file_provider_refresh.py` (runner e2e assertion).
- Re-export compatibility consumers (unchanged files, behavior pinned):
  `scripts/scheduler_file_provider_refresh.py:46` imports
  `SchedulerRegistryPublishError` from the CLI module (its tests use 7×
  `isinstance` checks — class identity must survive the move);
  `services/orchestrator/scheduler.py:448` re-exports
  `publish_scheduler_registry_manifest` consumed by
  `tests/test_production_scheduler.py`.
- No sibling normalizer copies elsewhere (issue #1097 受影响面 sweep,
  re-verified).
