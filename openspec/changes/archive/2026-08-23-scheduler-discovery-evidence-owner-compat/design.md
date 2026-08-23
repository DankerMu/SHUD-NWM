## Context

This is Child A of the breadth split selected after PR #1753 Round 3. It is a
behavior-neutral prerequisite for #1185, not a vehicle for that issue's cohort
identity semantics. The current owner module is 1,049 lines; ordinary commits
that leave a changed source file over the repository's 1,000-line threshold are
rejected. Existing code also exposes discovery helpers through
`services.orchestrator.scheduler`, and tests/callers replace symbols on
`services.orchestrator.scheduler_discovery`.

**Fixture level:** expanded

**Repair intensity:** high

**Project profile:** NHMS/SHUD

## Goals / Non-Goals

**Goals:**

- Reduce `scheduler_discovery.py` to at most 1,000 lines without a guard
  exemption or deletion of state-machine constraints.
- Extract only pure source-discovery evidence implementations.
- Preserve historical owner names, runtime signatures, return values, typed
  errors/details, facade object identities, import seams, and owner-module
  monkeypatch behavior.
- Commit a durable, data-driven mutation matrix that fails if any moved
  dependency becomes a direct re-export, static capture, or extracted-module
  lookup.

**Non-Goals:**

- No #1185 full/string init-state accessor, completion verdict identity,
  terminal-first, §8.7, quarantine, breaker, or file-journal behavior.
- No accepted-submit writer/reader/schema/digest change.
- No broader scheduler-discovery redesign, consumer migration, alias removal,
  guard threshold/configuration change, or new compatibility layer.
- No DB, Slurm, sbatch, SHUD, display, frontend, or production deployment work.

## Decisions

### D1: Extract pure implementations, not compatibility ownership

A new `scheduler_discovery_evidence.py` owns implementation functions with their
external dependencies passed as arguments. `scheduler_discovery.py` remains the
compatibility owner of the historical public/private names.

Rejected alternatives:

- A guard exemption hides rather than repairs the oversized module.
- Moving the completion state machine would couple this neutral prerequisite to
  #1185 product semantics.
- Copying constants or behavior into both modules creates two truth sources.

### D2: Bind dependencies at call time

Composite helpers remain defined in `scheduler_discovery.py` so Python global
lookup continues to observe replacements of sibling owner helpers and
constants. A dependency-bearing leaf retains its old name/signature as a thin
wrapper and passes the current owner symbols to the pure implementation on every
call. Only leaf functions with no external-global dependency may be direct
aliases.

The moved-global inventory is closed and testable:

- `_cycle_hour_not_allowed_evidence` -> owner `_source_cycle_evidence`.
- `_source_cycle_evidence` -> owner `_source_secret_text_safe`,
  `_source_cycle_status_candidate`, `_source_discovery_evidence_safe`, time
  formatters, and evidence sanitizer through its owner-defined composition.
- `_source_discovery_evidence_safe` -> owner sensitive-key regex, recursive
  self-call, secret-text helper, and evidence sanitizer.
- `_source_secret_text_safe` -> owner `redact_payload` and sensitive-text regex.
- `_filter_allowed_cycle_hours` -> owner `_ensure_utc`.
- `_duplicate_cycle_evidence` and `_backfill_deferred_evidence` -> owner
  `_ensure_utc`, `_format_utc`, and `cycle_id_for`.
- `source_horizon_metadata` -> owner `_ensure_utc` and `normalize_source_id`.
- `discover_source_window` -> owner `MAX_DISCOVERED_CYCLES` and replacement
  `SchedulerResourceLimitError`.

Rejected alternative: direct re-export of all moved functions. A real probe has
shown that the moved function's `__globals__` then points at the extracted
module, silently bypassing owner-module monkeypatches.

### D3: Prove both parity and binding

Existing/default behavior assertions prove output compatibility but cannot
identify which global a helper used. Tests therefore include:

1. default output/schema, redaction, horizon, cycle ordering, resource-limit
   type/details, signatures, alias identity, and import-seam parity; and
2. one call-time mutation row per independent dependency family above, with a
   sentinel result or exception that is impossible under the unpatched default.

The matrix is committed pytest, not an ad-hoc probe. Each row names the owner
symbol it mutates and the wrapper/composite expected to observe it.

### D4: Preserve consumer surfaces without migrating them

`services.orchestrator.scheduler` remains an identity facade for its discovery
alias inventory. `scheduler_candidates` retains its historical error,
secret/evidence aliases; `scheduler_candidate_runtime` retains its horizon
alias; and `scheduler_compat_runtime` continues validating owner/facade object
identity. `scheduler_runtime` and `scheduler_backfill_predecessor` retain their
import-time `SchedulerResourceLimitError` aliases, `scheduler_core` retains its
runtime `discover_source_window` call through the owner module, and
`scheduler_models` retains its use of the facade error alias. Consumers change
only if mechanically required to preserve that exact contract and a
discriminating test proves the need.

## Must-Preserve Behavior

- Source-cycle evidence keys, reason/status/classifier/retryable values,
  redaction, and nested evidence sanitization.
- Allowed-cycle filtering order and UTC handling.
- Duplicate/deferred evidence shape and cycle identity formatting.
- GFS/IFS horizon defaults and adapter-config override precedence.
- Inclusive per-day discovery order, legacy one-argument/two-argument adapter
  fallback, and exact resource-limit reason/details/exception class.
- Existing owner attributes, signatures, facade alias identities, import seams,
  and downstream consumers.

## Risk Packs

Core packs considered:

- Public API / CLI / script entry: **selected** — shared scheduler owner and
  facade/import compatibility surfaces remain observable.
- Config / project setup: **not selected** — no config field, environment,
  default path, or setup change.
- File IO / path safety / overwrite: **not selected** — no file access or
  mutation is introduced.
- Schema / columns / units / field names: **selected** — evidence dictionaries,
  UTC fields, horizon fields, and typed error details must remain byte-for-value
  compatible.
- Auth / permissions / secrets: **selected** — redaction helper extraction must
  not expose credential-bearing text or nested sensitive keys.
- Concurrency / shared state / ordering: **not selected** — helpers are
  synchronous and no mutable persisted/shared state changes.
- Resource limits / large input / discovery: **selected** — daily iteration,
  ordering, limit boundary, exact count/details, and exception replacement are
  preserved.
- Legacy compatibility / examples: **selected** — historical owner/facade
  aliases, runtime signatures, import seams, and monkeypatch paths are the main
  invariant.
- Error handling / rollback / partial outputs: **selected** — adapter TypeError
  fallback and resource-limit exception behavior must be unchanged; there are
  no partial writes or rollback paths.
- Release / packaging / dependency compatibility: **not selected** — one
  internal module is added with no package metadata or dependency change.
- Documentation / migration notes: **not selected** — behavior and consumer
  imports do not migrate; the fixture is the durable design record.

NHMS domain packs considered:

- Geospatial / CRS / basin geometry: **not selected** — no spatial data.
- Hydro-met time series / forcing windows: **not selected** — timestamps are
  discovery metadata only; no forcing-window semantics change.
- SHUD numerical runtime / conservation / NaN: **not selected**.
- PostGIS / TimescaleDB domain behavior: **not selected**.
- Slurm production lifecycle / mock-vs-real parity: **not selected**.
- External hydro-met providers / snapshot reproducibility: **selected** — GFS/
  IFS source discovery evidence and horizon defaults must remain unchanged.
- Run manifest / QC provenance: **not selected** — no manifest/QC data.
- Published NHMS artifacts / display identity: **not selected**.

## Invariant Matrix

**Governing invariant:** after extraction, every historical discovery helper
observes the current symbol on `scheduler_discovery` at the same call boundary
as before, while all default outputs, errors, ordering, and aliases remain
unchanged.

**Source-of-truth contract:** the existing function signatures and owner-module
global lookup behavior in `scheduler_discovery.py`, plus the current product
specs/tests for discovery evidence, redaction, horizon, allowed cycles, and
resource limits.

Surfaces:

- Producers: discovery adapters produce `CycleDiscovery`; unchanged.
- Validators/preflight: owner wrappers/composites and extracted pure
  implementations; changed only by relocation.
- Storage/cache/query: none — no persistence or cache.
- Public routes/entrypoints: `discover_source_window`,
  `source_horizon_metadata`, scheduler facade aliases and discovery forwarders.
- Frontend/downstream consumers: `scheduler.py`, `scheduler_candidates.py`,
  `scheduler_candidate_runtime.py`, `scheduler_compat_runtime.py`,
  `scheduler_runtime.py`, `scheduler_backfill_predecessor.py`,
  `scheduler_core.py`, and `scheduler_models.py` remain unchanged consumers.
- Failure paths/rollback/stale state: adapter TypeError fallback and
  `SchedulerResourceLimitError`; no rollback/stale state.
- Evidence/audit/readiness: committed mutation matrix, default behavior tests,
  signature/alias/import checks, line count and guard-config audit.

Regression rows:

- Default representative discoveries -> exactly the pre-extraction evidence,
  redaction, horizon, ordering, and resource-limit behavior.
- Each owner dependency replaced with a distinguishing sentinel -> the matching
  historical helper observes that sentinel at call time.
- Any dependency-bearing wrapper replaced by direct alias/static capture in a
  bite proof -> its mutation row fails.
- Existing scheduler facade and sibling consumers -> same owner object identity,
  callable signatures, and importability.
- Limit boundary at `MAX_DISCOVERED_CYCLES` -> allowed at the limit; replacement
  owner exception with exact details raised above it.

## Boundary-Surface Checklist

- Shared helper roots: all functions in the closed moved-global inventory.
- Public entrypoints: `discover_source_window`, `source_horizon_metadata`, and
  scheduler facade discovery aliases/forwarders.
- Read/write/delete/publish/rollback: none.
- Producer/consumer evidence boundary: `CycleDiscovery` -> evidence/horizon ->
  scheduler facade/candidate consumers.
- Stale/idempotency: none; repeated pure calls remain deterministic.
- Unchanged downstream consumers: all eight modules listed in D4, including
  direct owner imports, facade aliases, import-time exception aliases, and the
  runtime owner call.

## Risks / Trade-offs

- [Risk] A wrapper captures one dependency at import time. -> One committed
  mutation row per dependency family, plus deliberate static-binding bite proof.
- [Risk] Extraction changes evidence schema or redaction despite passing binding
  tests. -> Retain default output/security regression tests as independent
  parity evidence.
- [Risk] Direct aliases drift from facade inventories. -> Assert exact object
  identity and complete inventory membership.
- [Risk] Line-count pressure causes deletion of meaningful state-machine
  comments or a guard bypass. -> Extract the coherent pure helper family, audit
  both files at <=1,000 lines, and diff guard configuration against master.
- [Trade-off] Thin wrappers add indirection. -> Keep them only where dynamic
  owner lookup is an existing compatibility contract; direct-alias truly pure
  leaves.

## Migration Plan

1. Add pure implementation module and owner wrappers/composites in one commit.
2. Add/retain committed parity and mutation tests before merge.
3. Run local focused/adjacent tests, full ruff, strict OpenSpec, structural and
   guard audits. No production migration or remote receipt applies.
4. Roll back by reverting this isolated refactor; no data/config transition is
   involved.

## Open Questions

None. The issue body fixes the minimal mergeable slice and successor boundary.
