## Why

Selective-cold rollout is withdrawn. #1891/#1895 now require removal of the
obsolete merged selective-cold implementation, not indefinite dormant retention
and not a new general storage acceptance project. Shared functionality was
placed in cold modules but is still used by PGDATA relocation, ordinary resource
governance and manual verification; those consumers must survive the removal.

This is a proposed target revision only. Cold runtime remains present; no runtime
removal, production operation, issue closure or archive is performed here.
The obsolete bringup-checklist wording test and its private helper are removed:
they required the withdrawn #2137-before-#1895 cold rollout as current guidance.
The completed delivery ledger and immutable evidence retain their historical
meaning and cannot authorize the new retirement implementation.

## What Changes

The sole executable contract is [tasks.md](tasks.md), in five mandatory groups:

- **R1 — Transfer minimal surviving consumers to their actual owners:** PGDATA
  command/container/evidence helpers, governance sampling and genuine manual
  workload/evidence consumers migrate to existing domain owners. Independent C4,
  generic readonly validation and actual PGDATA capacity binding survive.
- **R2 — Detach normal compression while preserving safety:** eliminate cold env,
  paired budgets and the second launcher leg; preserve inert descriptor-bound
  config parsing, origin validation, bounded argv execution, lifecycle exclusion,
  ordinary compression/retention and discovery behavior.
- **R3 — Delete cold-only runtime and old G0-G8 delivery surfaces:** after R1/R2,
  remove runtime/CLIs/config/schemas/examples/tests/CI references and the cold SQL
  grant/audit. Tests/docs migrate with each source slice, never as a broken tail.
- **R4 — Correct active authority and preserve history:** withdraw pending rollout,
  update actual surviving contracts with implementation, obtain fresh retirement
  fixture/review gates and preserve immutable completed evidence.
- **R5 — Verify survivors, authorize deployment handoff, then close:** prove actual
  deletion and survivor regression, separately authorize effective-unit/config
  handoff and only then close the epic with evidence.

R1/R2 preparation may be concurrent. Publish destructive changes with their
actual dependency closure: R2 must include the R3 cold runner/wrapper and affected
caller/test/config/selector removals, not unrelated owner transfers or deletions.
Cold samples/I9/I8/#2162/#2017 are not blanket retirement dependencies. Effective
handoff still coordinates owners and foreign holds. Existing unrelated upgrade,
recovery, capacity and autovacuum duties are neither cancelled nor new gates.
No new RPO/RTO or storage-construction acceptance project is introduced.

## Capabilities

### New Capabilities

- `compressed-chunk-cold-residency`: repurposed here solely as a proposed
  retirement/closeout contract, NOT an enabled cold storage capability. The old
  never-promoted cold-enabling ADDED delta is withdrawn, not a canonical
  requirement removal. No installation, movement or cold convergence is required.

### Modified Capabilities

- `node27-pgdata-relocation`: preserve placement-aware observation and relocation
  safety without requiring a retired installer or cold module ownership.
- `hypertable-compression`: retain owner-role maintenance, remove cold grant/move
  obligations and add a compression-only safe launch contract.
- `runtime-service-role-boundary`: remove cold runtime/grant acceptance while
  preserving every unrelated provisioning/security boundary.
- `ci-contract-baseline`: retain surviving owner/dependent closure without
  selecting removed cold suites.
- `c4-live-display-evidence`: transfer the existing outer reviewed-SHA and exact
  C4-byte/file-identity recheck to Bringup-C4 production acceptance, without
  changing the local C4 closed protocol or retaining the cold G0/C3 owner.
- `explicit-cycle-query-binding`: remove only the retiring #1895/G7 recorder
  requirements, its #2227 test and selector edges; the actual shipping forecast
  owner and native named query bindings remain unchanged.

Canonical `openspec/specs/**` remains the implemented authority until the matching
implementation cutover. These deltas do not claim retirement has shipped.

## Impact

Concrete source families, consumer edges, schemas, CI and verification evidence
are enumerated once in tasks.md R1-R5. No whole-PR revert, row-schema migration,
changes to migrations 000058/000059, current data, ordinary retention windows,
PostgreSQL/TimescaleDB upgrade or archive-lane restoration is authorized.

Issues #2293/#2298/#1938 can be disposed as capability retired only after their affected
paths and effective deployed references are gone. Extracted defects follow their
new owner. Unexpected deployed cold state requires STOP and dedicated safe
disposition; source deletion authorizes no live DROP, revoke or data deletion.

## Historical authority and final disposition

Completed #1892/#1893/#1894/#1929/#2137/#2224/#2290/#2291 delivery remains in
tasks.md and existing evidence/fixtures. Original outstanding tasks 4.1-4.8 are
withdrawn, not executed. Their reviews are not retirement fixture approval.

Final archive requires a reviewed disposition that preserves surviving-spec
updates and the G7-only REMOVED delta while excluding withdrawn cold additions.
Ordinary archive promotes deltas; `--skip-specs` is appropriate only after those
updates and removals have been explicitly applied and validated, including removal
of the empty G7 capability. Never use `--no-validate` or silently drop deltas.
Never archive this revision into a newly enabled cold capability.
No archive is executed here.
