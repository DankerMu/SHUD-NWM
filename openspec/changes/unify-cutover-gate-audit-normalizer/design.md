# Design: unify-cutover-gate-audit-normalizer

## Placement decision

`packages/scheduler/registry_audit.py` (new package `packages/scheduler/` with
`__init__.py`), not `services/orchestrator/scheduler_file_providers.py`:

- Import topology: `scripts/publish_scheduler_file_registry.py` already
  imports from both `packages.common` and
  `services.orchestrator.scheduler_file_providers`; `services/` must not
  import from `scripts/` (wrong layering). A `packages/` home lets both
  consumers import downward.
- #1100 (split epic sub-2) already plans `packages/scheduler/` modules
  (`model_version.py`, and names `registry_audit.py` explicitly, deferring to
  this issue). Pre-seeding the package here converges the two issues instead
  of forking directions.
- No name collision: `scheduler-registry-refresh` exists in the repo only as
  an OpenSpec capability name, not as any directory or Python package.
  `packages/` already ships `__init__.py` and pyproject's
  `[tool.setuptools.packages.find]` includes `packages*`, so adding
  `packages/scheduler/__init__.py` needs no pyproject change (fixture-review
  verified).

## Error-type decision

Move `SchedulerRegistryPublishError` itself into the shared module and
re-export from the CLI script (module-level alias). Alternatives rejected:

- New shared error type raised by the normalizer while the CLI keeps its own
  `SchedulerRegistryPublishError`: the CLI's error serialization maps the
  class to exit codes / structured JSON — two types for one failure code
  reintroduces the exact divergence this change removes.
- Subclassing in the CLI: adds a layer with no behavioral need (YAGNI).

`try/except SchedulerRegistryPublishError` sites and
`isinstance` checks keep working because there is exactly one class object,
re-exported. The public error code string `SCHEDULER_REGISTRY_CUTOVER_AUDIT_INVALID`
is unchanged.

## Manifest-channel behavior change (intended, fail-closed)

`publish_scheduler_registry_manifest` currently embeds the lenient inline
mirror AFTER the commit: the commit happens via `_write_json_bytes(...)`
(`services/orchestrator/scheduler_file_providers.py:595`, which reaches
`atomic_replace_provider_bytes` internally at :1762), `commit_observer` fires
at :602, and the receipt with the inline mirror is assembled at :604-624.
Ordering decision: call the strict normalizer BEFORE
`_validate_registry_manifest`/`_write_json_bytes` (:594-595), so malformed
audit input fails the publish entirely rather than leaving a committed
manifest whose receipt assembly then fails halfway — receipt and committed
bytes stay atomic-in-effect. Function signature and contract unchanged.

`cutover_gate is None` semantics differ by entry point (both pinned by
tests, neither changed):
- `publish_all_basin_scheduler_registry(cutover_gate=None)` normalizes at its
  own boundary (`scripts/publish_scheduler_file_registry.py:163`) and passes
  the resulting `not_wired` block down (:375) — so the manifest receipt
  CARRIES the `not_wired` block. The passthrough MECHANISM is pinned by
  `tests/test_publish_scheduler_file_registry.py:1368` (a bypass-mode run);
  the None→not_wired INSTANCE has no existing assertion — task 2.4(ii) adds
  it.
- Direct `publish_scheduler_registry_manifest(cutover_gate=None)` callers
  (`scripts/provision_direct_grid_scheduler_registry.py:507`,
  `scripts/scheduler_file_provider_refresh.py:795` worker mirror, `:835`
  require-direct-grid) keep omitting the receipt key.

## Error-code visibility boundary (explicit decision)

`SCHEDULER_REGISTRY_CUTOVER_AUDIT_INVALID` is observable ONLY on direct
`publish_scheduler_registry_manifest` calls. The CLI aggregate entry wraps the
manifest publish in `except Exception → _publish_failure(...)`
(`scripts/publish_scheduler_file_registry.py:377-383`), which rebuilds the
error with the call site's fixed error code and collapses the reason to
`"provider_invalid"` (`:483-522` allowlist). Decision: keep that re-wrap
unchanged — extending the CLI's `allowed_reasons` is a CLI error-contract
change, out of scope for this unification (and the CLI channel already
normalizes at :163, so a malformed block cannot reach the manifest layer via
the aggregate entry anyway). Tests target the code at the direct-call
boundary only.

## Invariant Matrix

Governing invariant: every persisted `cutover_gate` audit block, on any
channel (CLI summary, runner receipt, manifest companion receipt), has passed
the single strict normalizer — same three fields, same allowed modes, same
failure code; a malformed block can never silently degrade to `"not_wired"`.
Source-of-truth identity/contract: `packages/scheduler/registry_audit.py`
(`CUTOVER_GATE_MODES`, `normalize_cutover_gate_audit`,
`SCHEDULER_REGISTRY_CUTOVER_AUDIT_INVALID`).
Surfaces:
- Producers: `scripts/scheduler_file_provider_refresh.py:811-817` (audit dict) + `:865` (passthrough) runner
  builds `runner_cutover_gate_audit={"mode":"enforced",...}` (unchanged
  in-tree producer); CLI `main` summary paths.
- Validators/preflight: the shared normalizer (only validator).
- Storage/cache/query: `publish_scheduler_registry_manifest` receipt assembly
  (`services/orchestrator/scheduler_file_providers.py:604-624`); CLI summary
  writer (`scripts/publish_scheduler_file_registry.py:163`).
- Public routes/entrypoints: CLI `scripts/publish_scheduler_file_registry.py`
  argparse main; `publish_all_basin_scheduler_registry(...)` kwargs.
- Frontend/downstream consumers: operators reading `manifest-last.json`
  companion receipt and CLI summary JSON (shape unchanged for valid input).
- Failure paths/rollback/stale state: malformed audit → raise before commit
  (manifest) / structured error (CLI); no partial receipt.
- Evidence/audit/readiness: the `cutover_gate` block itself is the evidence
  artifact.
Regression rows:
- CLI channel + valid enforced block -> summary embeds the block verbatim
  (existing tests keep passing).
- Manifest channel + valid enforced block -> receipt `cutover_gate`
  byte-for-byte equals producer's block (new e2e assertion).
- Direct manifest call + non-Mapping / bad mode / non-str declaration_env ->
  `SchedulerRegistryPublishError` code `SCHEDULER_REGISTRY_CUTOVER_AUDIT_INVALID`,
  raised before commit, destination absent/unchanged (new unit tests).
  Via the CLI aggregate entry the same malformed input already fails at
  `scripts/publish_scheduler_file_registry.py:163` with the same code; if it
  ever reached the manifest layer it would surface re-wrapped by
  `_publish_failure` (see Error-code visibility boundary).
- `cutover_gate=None` -> aggregate entry: both channels record the
  `not_wired` block; direct manifest call: receipt omits the key (both
  today's shapes, pinned by tests).
- Unchanged sibling consumers (all pass `cutover_gate=None` or nothing; no
  behavior change, new raise unreachable for them):
  `scripts/provision_direct_grid_scheduler_registry.py:507`;
  `scripts/scheduler_file_provider_refresh.py:795` (worker mirror) and `:835`
  (require-direct-grid) — this file also imports
  `SchedulerRegistryPublishError` at `:46` and its tests do 7×
  `isinstance(error, refresh.SchedulerRegistryPublishError)`, so re-export
  class identity is load-bearing; `services/orchestrator/scheduler.py:448`
  re-export consumed by `tests/test_production_scheduler.py` (6 call sites).

## Boundary-surface checklist (relevant categories)

- Shared helper roots: new `packages/scheduler/registry_audit.py`; no other
  shared helper modified.
- Public entrypoints: CLI argparse surface frozen (args/exit codes/serialized
  errors); `publish_scheduler_registry_manifest` signature unchanged.
- Write surfaces: manifest atomic write + receipt write ordering (see
  behavior-change section); CLI summary write unchanged.
- Producer/consumer evidence boundaries: runner audit dict → manifest receipt
  (new e2e pins it); CLI summary consumers.
- Stale-state/idempotency: none touched (no state machine).
- Unchanged downstream consumers: refresh runner tests, existing publish CLI
  tests must pass unmodified except where they gain assertions.
