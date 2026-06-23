## Context

The current report-only entropy audit on `master` emits 463 findings, 259
budget-counted findings, and zero gate-eligible findings. Most findings are
document or OpenSpec drift, but the manual heatmap highlights structural risks
in large files that act as compatibility or evidence aggregators:

- `services/orchestrator/scheduler.py` has more than 6000 lines, many imported
  owner families, and scheduler-state compatibility monkeypatch bindings.
- `services/orchestrator/chain.py` has more than 6900 lines and aggregates
  stage, manifest, reservation, retry, tile publisher, worker, and persistence
  behavior.
- `services/production_closure/two_node_e2e_evidence.py` has more than 9000
  lines and holds many evidence lanes, alias matrices, path-safety checks, and
  final aggregation rules.
- `services/production_closure/readiness_validation.py` has more than 3500
  lines and mixes dependency proof, scheduler evidence, live proof, and final
  readiness aggregation.
- `apps/api/main.py` and `apps/frontend/src/components/map/M11MapLibreSurface.tsx`
  are smaller but still above the proposed 1000-line governance threshold.

Only root `AGENTS.md` and `CLAUDE.md` currently exist, and
`openspec/glossary.md` is absent. That means future agents see the whole
repository through a broad root instruction file instead of local ownership
rules for the highest-entropy directories.

## Goals / Non-Goals

**Goals:**

- Stop structural entropy from growing while preserving compatibility and
  production behavior.
- Make large-file governance measurable with line-count, import-family,
  compatibility-symbol, and active budget-count metrics.
- Split implementation work into small, issue-sized PRs with clear module
  ownership.
- Lower active document entropy without erasing historical/audit evidence.
- Give future agents scoped context for the directories most likely to spread
  shallow patterns.

**Non-Goals:**

- No blanket rule that every file must immediately be below 1000 lines.
- No deletion of archive/history just to reduce finding totals.
- No removal of compatibility shims until callers/tests are migrated or a
  recorded compatibility decision permits removal.
- No production topology, Slurm, SHUD, DB schema, API contract, or frontend
  behavior change unless a later implementation issue explicitly owns it.
- No CI hard-gate enablement in this change by itself.

## Decisions

### 1. Treat line count as an entry criterion, not the whole diagnosis

Files over 1000 lines must enter the structural governance inventory. Files
between 500 and 1000 lines are reviewed when they show responsibility mixing,
many import families, frequent conflict churn, or compatibility logic. The
budget is intentionally paired with responsibility and import-family metrics so
single-purpose generated/data/fixture files are not split mechanically.

Alternative considered: fail every file above 1000 lines immediately. Rejected
because it would force large, risky rewrites of compatibility facades and
production-closure validators before the project has caller inventories and
lane contracts.

### 2. Freeze facade growth before removing facade code

`scheduler.py` and `chain.py` still protect downstream imports and monkeypatch
paths. The first governance step is an inventory: symbol, real owner, caller,
reason retained, removal condition, and verification. Guard tests then prevent
new non-forwarding implementation, new re-export groups, or new cross-domain
import families unless the inventory and budget are updated.

Alternative considered: immediately delete private re-exports. Rejected because
existing tests and downstream code still rely on those compatibility surfaces.

### 3. Decompose production closure by lane contracts

Production-closure files should move toward lane modules such as Docker
security, readonly DB, API/browser proof, published logs, producer identity,
dependency proof, scheduler evidence, live proof, manual ops receipt, and final
aggregation. Aggregators remain stable entrypoints but only compose structured
lane results.

Alternative considered: split by arbitrary line ranges. Rejected because it
would reduce file length while preserving mixed ownership and increasing
navigation cost.

### 4. Burn down active document budget, not total historical mentions

The 36 non-archive budget-counted findings are the active cleanup target.
Current OpenSpec/docs should either use canonical path/route language or mark
old terms with machine-readable historical/compatibility semantics. Archived
evidence remains visible but gains status metadata so the audit can narrow
allowlists.

Alternative considered: suppress `openspec/changes/archive/**` entirely.
Rejected because archive contents still provide evidence and can contain
misleading current-looking text unless status is explicit.

### 5. Add local agent context where shallow patterns are most contagious

Scoped `AGENTS.md` files should be added first under
`services/orchestrator/`, `services/production_closure/`, `apps/api/`, and
`apps/frontend/`. Each should contain only local invariants: dependency
direction, state ownership, error/evidence model, compatibility rules, docs
freshness, and verification commands. `openspec/glossary.md` should define
canonical governance terms used by these controls.

Alternative considered: expand root `AGENTS.md`. Rejected because the root file
is generated and already broad; adding local rules there makes context harder to
apply and maintain.

## Risks / Trade-offs

- **Risk: mechanical file splitting creates more entropy.** Mitigation: require
  owner/lane contracts and behavior-preserving tests before line-count
  reduction is claimed.
- **Risk: facade freezes block necessary fixes.** Mitigation: allow bugfixes
  that do not add ownership surface; require inventory updates only for new
  compatibility or import-family growth.
- **Risk: active document cleanup weakens historical evidence.** Mitigation:
  require canonical update or machine-readable marker, not deletion.
- **Risk: scoped instructions drift from code.** Mitigation: include freshness
  checks and verification commands in the new instruction files.
- **Risk: audit numbers improve without real architecture improvement.**
  Mitigation: issue acceptance must include measured budget deltas plus
  structural evidence such as reduced aggregator-owned logic, bounded import
  families, or guarded compatibility surfaces.

## Migration Plan

1. Add budget/reporting support for large source files and responsibility
   signals without failing CI.
2. Inventory `scheduler.py` and `chain.py`, then add guard tests to stop facade
   growth.
3. Define production-closure lane contracts, then extract one lane at a time
   behind stable entrypoints.
4. Clean active OpenSpec/docs budget-counted route/path drift and adjust
   detector allowlists only where text is already explicitly retired.
5. Add archive status metadata and audit support.
6. Add scoped instructions and glossary entries.
7. Re-run report-only entropy audit, focused tests, and OpenSpec validation;
   write no new baseline unless explicitly requested by a maintainer.

Rollback is by reverting the specific issue PR. Guard-only issues should fail
closed in report-only mode and must not delete compatibility code or archive
evidence.
