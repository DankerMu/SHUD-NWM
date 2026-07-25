# Tasks: unify-cutover-gate-audit-normalizer

Fixture level: expanded (publish/evidence receipt surface; shared-module
extraction across scripts/services/packages)
Repair intensity: high (evidence-chain surface → Invariant Matrix in design.md
is a hard gate for implementation and review)

Risk packs considered (core):
- Public API / CLI / script entry: selected - CLI args/exit codes/error
  serialization and `publish_scheduler_registry_manifest` signature must stay
  frozen; re-export aliases keep import paths working
- Config / project setup: selected - new `packages/scheduler/` package must be
  importable in all environments (plain `__init__.py`; no pyproject change
  expected — verify `packages.*` namespace already ships)
- File IO / path safety / overwrite: not selected - atomic write/path logic
  untouched; only the receipt dict content and raise-ordering change
- Schema / columns / units / field names: selected - the three-field audit
  block shape is the contract; byte-for-byte passthrough pinned by e2e
- Auth / permissions / secrets: not selected - not touched
- Concurrency / shared state / ordering: not selected - no locking/state
  machine changes; raise-before-commit ordering covered under Error handling
- Resource limits / large input / discovery: not selected - not touched
- Legacy compatibility / examples: selected - existing publish CLI + refresh
  runner tests must pass unmodified (except added assertions); import-path
  re-exports pinned
- Error handling / rollback / partial outputs: selected - manifest channel
  becomes fail-closed; malformed audit raises BEFORE manifest commit (no
  committed-manifest-with-failed-receipt half state)
- Release / packaging / dependency compatibility: not selected - stdlib-only
  module move
- Documentation / migration notes: not selected - in-tree contract move;
  issue + OpenSpec change are the record

## 1. Shared module extraction

- [x] 1.1 Create `packages/scheduler/__init__.py` +
  `packages/scheduler/registry_audit.py` holding `CUTOVER_GATE_MODES`,
  `SchedulerRegistryPublishError`, and `normalize_cutover_gate_audit` (moved
  verbatim from `scripts/publish_scheduler_file_registry.py:74,122,419-466`;
  public name loses the leading underscore).
  Evidence floor: module imports cleanly; `uv run ruff check .` clean.
- [x] 1.2 `scripts/publish_scheduler_file_registry.py`: delete local
  definitions, import the three names, keep module-level re-export aliases
  (`SchedulerRegistryPublishError = ...` etc.) and the internal
  `_normalize_cutover_gate_audit = normalize_cutover_gate_audit` alias if
  call sites keep the old name.
  Evidence floor: `uv run pytest -q tests/test_publish_scheduler_file_registry.py`
  green unmodified (import-path compatibility proven by existing suite).
- [x] 1.3 `services/orchestrator/scheduler_file_providers.py`: replace the
  inline lenient mirror (if-guard `:618`, dict `:619-623`) with the shared
  normalizer, invoked BEFORE `_write_json_bytes` commits (see design.md ordering
  decision); `cutover_gate is None` keeps omitting the receipt key.
  Evidence floor: red-proof — a malformed-mode manifest publish currently
  succeeds with `"not_wired"` (red), raises
  `SCHEDULER_REGISTRY_CUTOVER_AUDIT_INVALID` after (green), and the
  destination manifest is NOT committed.

## 2. Test coverage (issue acceptance (a)-(d) + e2e)

- [x] 2.1 Four normalizer unit tests (new, in
  `tests/test_publish_scheduler_file_registry.py` or a new
  `tests/test_registry_audit.py` — implementer picks, stating reason):
  (a) non-Mapping → raise with code `SCHEDULER_REGISTRY_CUTOVER_AUDIT_INVALID`;
  (b) `mode` ∉ `CUTOVER_GATE_MODES` → same code;
  (c) `declaration_env` non-str/non-None → same code;
  (d) `None` → `{"mode": "not_wired", "declaration_env": None,
  "declaration_present": False}`.
  Evidence floor: 4 tests red against pre-change manifest channel semantics
  where applicable (b/c via manifest path) or against absent shared module,
  green after.
- [x] 2.2 Manifest-channel fail-closed test (DIRECT call boundary — the only
  place the code is observable, see design.md Error-code visibility
  boundary): `publish_scheduler_registry_manifest` called directly with
  malformed `cutover_gate` raises `SchedulerRegistryPublishError` with code
  `SCHEDULER_REGISTRY_CUTOVER_AUDIT_INVALID` and leaves no committed manifest
  (assert destination absent/unchanged).
  Evidence floor: test red on master (silent `"not_wired"` receipt, manifest
  committed), green after.
- [x] 2.3 Runner e2e passthrough assertion: after a full
  `publish_all_basin_scheduler_registry` run, the manifest receipt embeds
  `cutover_gate` byte-for-byte equal to the producer block
  (`mode`/`declaration_env`/`declaration_present`).
  Evidence floor: assertion added to the existing runner-path test in
  `tests/test_scheduler_file_provider_refresh.py` (T2 family) or the publish
  suite's runner-integration test. MANDATORY mutation-check (not optional):
  locally break the receipt mirror (e.g. drop a field or rewrite mode) and
  show the assertion goes red, then restore — record both outputs; an
  assertion that cannot go red is not evidence.
- [x] 2.4 None-semantics pins (spec Scenarios 3+4, currently unasserted
  anywhere in the repo): (i) direct
  `publish_scheduler_registry_manifest(cutover_gate=None)` →
  `"cutover_gate" not in receipt`; (ii) aggregate
  `publish_all_basin_scheduler_registry(cutover_gate=None)` → summary
  `cutover_gate` AND `summary["registry"]["cutover_gate"]` both equal the
  `not_wired` block. Guards task 1.3 against the unconditional-embed
  regression (`if cutover_gate is not None:` at
  `services/orchestrator/scheduler_file_providers.py:618` must survive).
  Evidence floor: both assertions green on the fixed head; (i) red under a
  local unconditional-embed mutation (record output, restore).

## 3. Change-level verification floor

- [x] 3.1 `uv run pytest -q tests/test_publish_scheduler_file_registry.py
  tests/test_scheduler_file_provider_refresh.py` green.
- [x] 3.2 `uv run ruff check .` clean.
- [x] 3.3 `openspec validate unify-cutover-gate-audit-normalizer --strict
  --no-interactive` PASS.
- [x] 3.4 Sibling-caller sweep: run every test file surfaced by BOTH greps —
  `grep -rln "SchedulerRegistryPublishError\|CUTOVER_GATE_MODES" tests/`
  (moved-name consumers) and
  `grep -rln "publish_scheduler_registry_manifest" tests/ scripts/ services/`
  (changed-function callers; includes `tests/test_production_scheduler.py`
  via the `services/orchestrator/scheduler.py:448` re-export) — all green.
  Non-test production callers (`scripts/provision_direct_grid_scheduler_registry.py`,
  refresh worker-mirror/require-direct-grid paths) are exercised through
  these suites; each passes `cutover_gate=None` so the new raise is
  unreachable for them (design.md Invariant Matrix row).
