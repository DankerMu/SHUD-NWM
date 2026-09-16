# #2290 physical-parent admission fixture

Fixture level: expanded. Repair intensity/effective tier: high. Upstream suggested
level: expanded (retained; high intensity reflects shared DB identity/evidence).
Project profile: NHMS. Depends on merged #2224. Parent: #1895 / #1891.

## Contract and non-goals

The cold lane admits canonical river only when it is the post-expand narrow
hypertable, and canonical forcing in its existing form. The closed cold allowlist
never inherits the lifecycle discovery set's legacy members. Live catalog/schema
truth decides the side of the transition; migration-file presence or a ledger row
alone does not. A valid narrow table remains admissible after legacy is absent.

One expected parent `(schema, name, pg_class OID, Timescale hypertable ID)` binds
inventory, candidate membership, origin reload, locked revalidation and fresh
reconciliation. Required internal `HypertableInventory` parent fields have no
name-only/zero/default fallback. The opaque inventory digest includes these
identities and all current physical-order column descriptors. Existing inventory
payload, receipt schema/version, durable origin keys and parity checksum algorithm
remain unchanged; adding raw identity fields to closed wire schemas is not needed.
Old cross-revision or pre-expand artifacts cannot satisfy fresh comparisons.
Parent identity and descriptors must come from one consistent catalog observation
(prefer extending the existing column query with a parent join on `attrelid`),
not separate name-only reads that could combine one parent's OID with another's
columns during a rename. The same-row identity tuple is validated before trust;
existing locked/fresh comparisons and origin-only data plans remain load-bearing.

Narrow discrimination requires the narrow key/enum contract and absence of the
legacy text-key projection; a wide table containing added surrogate keys is NOT
narrow. Supported extra user columns remain included in inventory/parity, not
silently discarded. Canonical forcing is identity-bound without requiring its
future expand. No external deployment ledger permission is introduced.

Round1 clarification: the narrow enum predicate uses same-row catalog type
namespace/name, not `format_type` display qualification. The same physical narrow
parent must admit and support bound selection under both default and hydro-visible
`search_path`. Restore the session setting after the isolated proof. Existing
descriptor/digest/parity rendering stays unchanged; cross-session canonicalization
is a separate pre-existing concern, not an identical-digest requirement here.
Readiness adapters translate known cold admission errors to their stable CLI
refusal before publication; unrelated programming errors must still propagate.

Non-goals: cardinality/default-six cutover and G3 count alignment (#2291);
production G0-G8, production DB/active checkout access, services/timers/install;
any other issue; migration/lifecycle/retention/lag changes; C4/display/API changes;
legacy migration; receipt schema expansion. Only owned node27 isolated resources
may run tests. Both #2290 and #2291 must merge before a fresh production G0 is
eligible for separate external readiness/authorization.

## Seams and implementation ownership

- `derive_hypertable_inventory` / `derive_bound_inventories`: bounded parent
  catalog identity plus actual columns -> admitted identity-bound inventories or
  stable `ColdRuntimeError`; pre-expand/incomplete/ambiguous state refuses.
- `load_eligible_chunks`, `load_catalog_chunk`,
  `ranked_candidates_from_execute`: expected bound inventory -> origins belonging
  to those physical parents only. Migrate all callers; remove name-only overloads.
  Returned catalog membership must match expected parent OID/ID, not merely
  satisfy a name predicate or a caller-supplied origin name.
- Runtime first reload, `_revalidate_locked`, `_fresh_observer` and
  `reconcile_named_group`: reuse the same expected binding; parent replacement
  must not be adopted as a new selection. Preserve #2224's durable-vs-mutable
  compressed-sibling distinction, real non-empty failure receipts and no-write
  complete-target replay.
- Census, post-target and intersecting-group observation owners consume the same
  admission contract. Their test injection seams must carry the expected binding,
  not silently create fresh authority from observed chunks.
- Existing same-schema closed inventory payload plus digest is the wire boundary;
  locked/fresh payload comparisons and persisted parity/intent equality carry the
  binding without a new receipt version.

## Risk packs considered

| Pack | Selection and evidence |
|---|---|
| Public API/CLI/script entry | selected: census/runtime/post-target refusal and migrated caller seams |
| Config/project setup | selected: direct CI owner routing/removal; no new production config |
| File IO/path safety/overwrite | selected: stale baseline/intent cannot authorize PASS; existing private publication primitives unchanged and exercised |
| Schema/columns/units/field names | selected: narrow signature, required parent IDs, enum/all-user-column inventory and unchanged wire payload |
| Auth/permissions/secrets | selected: new catalog queries under existing non-superuser ingest/display fixture, no new grants/DSNs in evidence |
| Concurrency/shared state/ordering | selected: parent swap between selection/reload/lock and fresh reconciliation |
| Resource limits/large input/discovery | selected: bounded catalog joins and existing candidate/byte/time ceilings |
| Legacy compatibility/examples | selected: wide refusal, legacy exclusion, post-contract narrow acceptance, old artifact refusal |
| Error handling/rollback/partial outputs | selected: stable refusal before mutation or no false success after uncertain readback |
| Release/packaging/dependency compatibility | not selected: no dependency or package change; pinned engine covered by domain pack |
| Documentation/migration notes | selected: cold G0 STOP, fresh same-revision artifacts and explicit legacy exclusion |
| Geospatial/CRS/basin geometry | not selected: no geometry transformation |
| Hydro-met time series/forcing windows | selected: actual 1/3/7-day catalog bounds and forcing compatibility |
| SHUD numerical runtime/conservation/NaN | not selected: no solver change |
| PostGIS/TimescaleDB domain behavior | selected: actual parent/chunk IDs, rename/replacement, compression and engine oracle |
| Slurm lifecycle/mock-vs-real | not selected: no node22 or scheduler work |
| External providers/snapshot reproducibility | not selected: no provider input change |
| Run manifest/QC provenance | not selected: these payloads unchanged |
| Published artifacts/display identity | selected: inventory/parity/receipt identity binding; C4/frontend unchanged |

## Invariant Matrix

Governing invariant: no cold query or move may adopt a different physical parent
behind a canonical name, and no legacy origin can become a cold candidate or
current proof; uncertainty refuses before mutation or cannot publish success.

| Surface | Owner/reference | Required proof |
|---|---|---|
| Producers | catalog inventory/selection, census | valid narrow+forcing IDs bind once; wide/legacy/incomplete state rejects before parity |
| Validators/preflight | runtime reload/locked revalidation | same name/columns with changed parent OID or hypertable ID refuses before intent/move |
| Storage/query | catalog chunk membership and origin parity | physical-parent-constrained query; actual chunk ranges; no parent scan or legacy fallback |
| Public entrypoints | census/runtime/post-target CLI owners | stable domain refusal, no false GO/receipt publication, caller binding passed explicitly |
| Downstream consumers | intersecting groups, baseline/intent/parity | fresh digest changes on parent replacement; stale before evidence never adopted |
| Failure/rollback/stale state | first reload, fresh observer, reconciliation | existing #2224 race receipts/recompression/no-write replay preserved; new parent drift cannot use sibling exception |
| Evidence/readiness | inventory payload, receipt schema, cold runbook | wire keys unchanged; fresh revision required; production gate remains HOLD |
| Unchanged siblings | forcing shape, probe-private parity, installer/C4 | no forcing-expand dependency or legacy allowance; original independent probe and role/lifecycle oracles remain valid |

## Scenario evidence and commands

- `tests/test_issue2290_cold_parent_admission.py`: real public admission/candidate/
  reload/census/post-target seams; narrow keys+enums accepted, wide+surrogate union
  rejected, explicit legacy rejected, missing/ambiguous/bool/zero parent IDs
  rejected, returned membership mismatch rejected, same-column parent substitution
  changes digest and blocks old evidence, actual range boundaries unchanged.
- Extend existing `tests/cold_residency_fakes.py` to model real parent IDs and a
  genuinely narrow river inventory, not a union fixture that masks the gate.
  Update existing constructors/callers without default IDs or bypass switches.
- Pinned disposable runtime oracle: actual wide->legacy rename, creation of a
  canonical narrow parent with 1-day chunks and distinct compressed legacy
  history, canonical forcing, pre-expand rejection, post-expand selection,
  legacy absence, same-shape parent replacement/digest drift, unchanged legacy
  contents, and existing complete move/inverse/parity/recompression cleanup.
  Reuse the sole three-marker runtime harness with a load-bearing invocation
  contract; a new helper reference without executed proof is insufficient.
- Preserve #2224's selected compressed-target sensitivity, large-sibling
  independence, direct-vs-ONLY verbose plan and shipping-role/deny-write proof;
  update that fixture's business-key shape to narrow where admission applies.
- `tests/test_select_ci_tests.py`: every changed acceptance owner -> asserted
  suite -> explicit route -> independent removal proof, with old legs retained.
- Local: `uv run ruff check .`; `openspec validate
  compressed-chunk-cold-tablespace-tiering --strict --no-interactive`.
- Node27 only, owned isolated checkout/env: focused and default pytest; the exact
  command `NHMS_RUN_NODE27_DOCKER=1 uv run --no-sync pytest -q -m 'integration and
  timescaledb_210 and node27_docker'
  tests/test_compressed_chunk_cold_runtime_integration.py` for the pinned engine.
  Capture tests-first red against pre-change source and final-head green, exact
  SHA, actual principal/engine and owned cleanup. No production DB connection.

Implementation starts only after one read-only fixture review and strict OpenSpec
PASS. Review must check this matrix and the source/selector owner census, not
merely the first changed catalog function. Shared tasks4.1-4.8 stay unexecuted.
