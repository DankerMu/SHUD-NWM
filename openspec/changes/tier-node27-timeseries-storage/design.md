# Design: Tier Node-27 Timeseries Storage

## Context

Node-27 runs the active primary PostgreSQL (`nhms-db` container, TimescaleDB
2.10.2 / PG 15.2) plus ingest, display API, and frontend. Two detail
hypertables (`hydro.river_timeseries` 98 GB / 132M rows,
`met.forcing_station_timeseries` 48 GB / 91M rows) are effectively the entire
146 GB database; ~70% of their footprint is btree indexes replicated per
7-day chunk. Compression and retention are entirely absent. The hot
object-store on the shared 1.7 TB volume retains `runs/` since 2026-05-31 and
`forcing/` only since 2026-06-16 (ad-hoc reset; nothing rotates these
routinely), while `raw/` is pruned at 14 days by the existing user-level
`nhms-node27-raw-retention.timer`. Display paths never scan the hypertables
(coverage materialization + object-store CSV per ADR 0001). Full context and
the decision record live in `docs/adr/0002-node27-timeseries-hot-cold-tiering.md`.

Existing operational patterns to reuse: `scripts/node27_raw_retention.py`
(dry-run default, JSON summary, bounded deletes, cycle-name parsing) and
`scripts/node27_resource_governance.py` + `_once.sh` wrapper (env-file
hygiene checks, flock, receipts directory, systemd user timer).

## Goals / Non-Goals

**Goals**

- Make node-22-produced cycle products the durable, checksum-verified,
  rotation-exempt full-history source of truth (archive tier).
- Salvage the DB-only forcing/river timeseries windows (sole copies) into the
  archive before any deletion machinery exists; missing state artifacts are
  non-salvageable and keep retention fail-closed.
- Cut the two hypertables' footprint with native TimescaleDB compression on
  terminal chunks.
- Bound DB size permanently with gated, receipted `drop_chunks` retention
  (30-day window).
- Prove DB rebuildability from archive via the existing ingest path before
  retention can enforce.

**Non-Goals**

- No v2 star schema / surrogate-key fact tables (deferred, ADR 0002 §6).
- No parquet derived products, no online historical-query engine (YAGNI until
  a consumer exists; ADR 0001 owns the history API boundary).
- No change to display API contracts, MVT paths, or coverage materialization.
- No archiving of `raw/` GRIB (refetchable; existing 14-day prune stays).
- No TimescaleDB extension upgrade in this change.

## Decisions

**D1 — Archive source of truth = products, not DB export.** The DB is derived
from node-22 products via ingest; archiving products keeps exactly one
automated restore path (existing reingest) instead of building a parallel
COPY-FROM lane. The one-time `db-export` salvage objects (D6) are the sole
exception to product provenance, and they get no automated restore lane
either: their only restore path is a documented manual `COPY FROM` runbook
procedure, and the drill verifies them by checksum + manifest row count
rather than reingest (consistent with ADR 0002 decision 3). *Alternative
rejected:* steady-state DB `COPY` export — duplicates the restore path and
archives derived data instead of source data. *Alternative deferred:*
parquet derived products — adds a pyarrow dependency with no current
consumer.

**D2 — Archive object = per-cycle `tar.zst` + manifest.** One tarball per
(source, cycle[, basin/run]) directory with a sidecar `manifest.json` (file
list, per-file sha256, sizes, tarball sha256, identity fields — deliberately
no row counts: rebuild-drill parity for product cycles derives expected
counts by parsing the restored files, so the mover never needs to parse
products). Same-volume staging + atomic rename after verification keeps
moves atomic; checksums are verified before any source deletion, and a
final-path object that fails verification on a later run is quarantined and
re-archived rather than trusted. Candidates are cycles older than
`NHMS_ARCHIVE_MIN_AGE_DAYS` (default 45 d; config-validated ≥ the 30-day DB
retention window), so the hot object-store — and therefore the ADR 0001
display disk window — is never shorter than the DB hot window.
*Alternatives rejected:* bare directory move (no integrity story,
inode-heavy), per-file zstd (object explosion), cross-volume copy (no second
volume exists).

The producer product remains the source of truth inside that archive object:
forcing `files` checksums and run output presence are validated from the same
pinned source snapshot before eligibility. The sidecar also preserves the
producer-manifest digest and stable subject/window/model/basin provenance so
the DB-aware inventory audit can bind a filesystem-created archive back to the
exact DB subject. The mover itself stays filesystem-only; provenance capture is
not a hidden DB lookup.
Node-27 forcing finalization has two valid package shapes: older leaves contain
only the forcing manifest and declared products, while newer leaves add one
complete domain-handoff/version bundle. That five-file bundle is validated as
an independent checksum/identity contract rather than inserted into the
forcing manifest's output list (the version record itself binds the forcing
manifest digest, so folding it into that manifest would create a hash cycle).
Source retirement keeps a same-volume durable reference to the exact verified
tar/manifest inodes until all source deletion steps finish. This closes the
gap that descriptor rechecks alone cannot close: a canonical-name replacement
may make the terminal indeterminate, but it cannot erase the only valid archive
copy after the hot source is retired.

**D3 — Compression settings must cover the existing primary keys.**
TimescaleDB 2.10 requires unique-constraint columns to appear in
`compress_segmentby` + `compress_orderby`:

- `hydro.river_timeseries`: segmentby `run_id, river_network_version_id,
  river_segment_id`; orderby `variable, valid_time`.
- `met.forcing_station_timeseries`: segmentby `forcing_version_id,
  station_id`; orderby `variable, valid_time`.

Segmentby columns are exactly the equality filters of the curve/MVT query
shapes, so compressed-chunk reads stay index-driven. Compression is applied
by a receipted runner on its own user-level systemd timer per D7
(`compress_chunk` on chunks whose `range_end` is older than a configurable
lag, default 7 days = one chunk width); the active chunk is never
compressed. *Alternative rejected:* `add_compression_policy` —
background job with no receipts, no bounds, invisible to the governance
audit trail this node runs on.

**D4 — Retention = script-driven `drop_chunks`, hard-gated.** A runner drops
chunks fully older than 30 days on the two detail hypertables only, with
dry-run default, explicit enforce env, flock, per-tick chunk bound, and JSON
receipts. Enforce refuses to run unless (a) the archive completeness receipt
and (b) the rebuild-drill PASS receipt cover the window being dropped and are
fresh. *Alternatives rejected:* `add_retention_policy` (cannot express the
archive gate or receipts), row-level DELETE by cycle count (GPT proposal —
abandons O(1) chunk drops, creates bloat, needs vacuum babysitting).
Chunk-granular drops mean effective retention is 30–37 days; acceptable and
documented.

**D5 — Reingest vs compressed chunks fails closed.** TimescaleDB 2.10 cannot
write into compressed chunks. Any ingest/reingest write targeting a
compressed chunk must abort with an explicit error pointing at the documented
`decompress_chunk` procedure. The guard is centralized in one shared
pre-write helper called by all three hypertable write paths
(`workers/output_parser/parser.py`, `workers/forcing_producer/store.py`,
`packages/common/forcing_domain_handoff_apply.py`). The 7-day compress lag
makes this a rare, deliberate operation (bulk historical rewrites), never a
silent data loss. The rebuild drill never trips the guard: it writes only
its isolated staging schema (see Migration Plan step 5).

**D6 — Salvage scope is audit-derived, not hardcoded.** A recurring
inventory audit compares DB coverage (`hydro_run` cycles, `forcing_version`
windows, `state_snapshot.state_uri` references) against checksum-verified
archive objects + hot object-store presence, and emits the
**archive-completeness receipt**: per-window verdicts
(`complete`/`pending-archive`/`gap`), coverage bounds, `generated_at`, and
the exact salvageable forcing/river timeseries selector list (expected:
forcing station series before
2026-06-16; river only if gaps are found). That one receipt is both the
salvage scope source and retention gate (a); it runs from its own systemd
timer so freshness holds at every retention tick. The exporter consumes the
receipt's salvage list. Export format: `COPY` to `csv.zst` per selector with
manifest (`provenance: db-export`, row counts, sha256) — distinguishable
from product-derived archives forever.

**D7 — All new machinery is node-27 user-level systemd**, cloned from the
`raw-retention` / `resource-governance` patterns (env-file mode checks,
flock, bounded work per tick, receipts under `/home/nwm/...-logs/`, units
registered in the governance audit's unit list). Node-22 is untouched.

## Risks / Trade-offs

- [Reingest needs a compressed window] → fail-closed guard + runbook
  decompress procedure; compress lag configurable if reingest cadence grows.
- [Archive shares the 1.7 TB volume with pgdata] → governance watermark:
  archive mover refuses enforce below a free-space threshold; volume growth
  visible in every governance receipt.
- [Inventory audit finds river product gaps] → salvage lane already covers
  arbitrary selectors; scope grows without design change.
- [tar.zst CPU/IO pressure on 27] → bounded cycles per tick, off-peak timer
  schedule, zstd default level.
- [Ad-hoc resets recur before retention gates land] → archive lane ships
  first in task order; ADR 0002 records the "no deletion without archive
  receipt" invariant as the operative rule for operators too.
- [Compression breaks an unanticipated query path] → compression is
  per-chunk reversible (`decompress_chunk`); initial run receipt includes
  before/after query timings for the curve/MVT representative queries.

## Migration Plan

Order is load-bearing (each step gates the next):

1. Inventory audit live (read-only) → first archive-completeness receipt;
   fixes salvage scope. The audit then runs on its own timer so a fresh
   receipt exists at every later gate check.
2. Archive mover live for `forcing/` + `runs/` + `states/` (state products
   enumerated via the audit's `state_snapshot.state_uri` inventory, per ADR
   0002 decision 1); first enforce receipt.
3. One-time salvage export of DB-only forcing/river timeseries windows;
   receipt. Salvage coverage
   folds into the audit's completeness verdicts — there is no separate
   salvage gate.
4. Compression: migration for settings + initial terminal-chunk compression;
   before/after receipt. (Independent of 1–3 and of the drill in 5 — the
   drill writes only its isolated staging schema, so compression state never
   blocks it; compression is NOT a retention gate.)
5. Rebuild drill: restore sample archive cycles → reingest into an isolated
   staging schema (same DDL, no compression, production hypertables never
   written) → parity: per-(run, variable) staging counts vs counts parsed
   from the restored files; `db-export` objects verified by checksum +
   manifest row count (no reingest) → PASS receipt declaring covered
   (source, window) tuples.
6. Retention dry-run receipts; then enforce — gated on exactly two receipts:
   the fresh archive-completeness receipt from step 1's recurring audit
   (which already folds in steps 2–3) and step 5's drill PASS receipt whose
   declared coverage includes the drop window. Compression receipts are not
   part of this gate.

Rollback: archive mover and salvage are additive (sources deleted only after
checksum verification; salvage deletes nothing). Compression rolls back per
chunk via `decompress_chunk`. Retention rollback is restore-from-archive via
the drilled reingest path for product-derived cycles, and via the documented
manual `COPY FROM` procedure for `db-export` salvage windows;
metadata/coverage rows are never dropped.

## Open Questions

- Free-space watermark values (initial proposal: warn < 300 GB free, refuse
  archive enforce < 150 GB) — tune with first receipts.
- Whether `forcing_station_timeseries_qhh_latest_window_idx` (20 GB) is still
  load-bearing after compression lands — candidate for a follow-up prune
  receipt, out of scope here.
- TimescaleDB upgrade (2.10 → 2.13+ would allow compressed-chunk DML and
  lighten D5) — revisit before national scale.

## Workflow Fixture: Issue #846 Storage Foundation

Fixture level: expanded. Repair intensity: high. Project profile: NHMS.

Change surface:

- `packages/common/storage.py`, focused unit tests, five JSON Schemas, and their examples.
- No scripts, systemd, migration, display route, or production mutation in this PR.

Must preserve:

- Existing object-path validation and the raw-retention/resource-governance env override convention.
- ADR 0001 display routes remain disk-only and do not import or call archive resolution.

Risk packs considered:

- Public API / CLI / script entry: selected — shared helper contract is consumed by later scripts; no CLI is added here.
- Config / project setup: selected — env precedence, minimum-age, and root-overlap validation are the feature.
- File IO / path safety / overwrite: selected — archive and cleanup roots must be disjoint before any later mutation.
- Schema / columns / units / field names: selected — five schemas are cross-script format authorities.
- Auth / permissions / secrets: not selected — no credentials, roles, or permission boundary changes.
- Concurrency / shared state / ordering: not selected — no runner or mutation is implemented in #846.
- Resource limits / large input / discovery: not selected — no archive scanning or receipt ingestion is implemented in #846.
- Legacy compatibility / examples: selected — existing env aliases and object-path callers must remain compatible.
- Error handling / rollback / partial outputs: selected — invalid overlap/age must fail before mutation-capable callers proceed.
- Release / packaging / dependency compatibility: not selected — no runtime dependency or packaging change.
- Documentation / migration notes: not selected — runbooks and environment examples belong to later issues.
- Geospatial / CRS / basin geometry: not selected — no geometry surface.
- Hydro-met time series / forcing windows: selected — receipt coverage bounds and selectors describe forcing/river windows.
- SHUD numerical runtime / conservation / NaN: not selected — no solver behavior.
- PostGIS / TimescaleDB domain behavior: not selected — schemas describe evidence only; no DB access/migration.
- Slurm production lifecycle / mock-vs-real parity: not selected — node-22 remains untouched.
- External hydro-met providers / snapshot reproducibility: not selected — no provider discovery or fetch.
- Run manifest / QC provenance: selected — archive/salvage manifests must bind checksums, identities, selectors, and counts.
- Published NHMS artifacts / display identity: selected — archive provenance is non-display-only and must not alter display lookup identity.

Invariant Matrix:

- Governing invariant: later archive/retention tools can act only with a valid, non-overlapping archive configuration and schema-conformant provenance, while display remains disk-only.
- Source of truth: resolved archive root/minimum age plus the five schemas under `schemas/`.
- Producers: schema examples only in #846; runtime producers are later issues.
- Validators/preflight: `packages/common/storage.py` configuration and provenance lookup helpers.
- Storage/cache/query: filesystem path values only; no DB/cache access.
- Public routes/entrypoints: later node-27 scripts consume the helper; display routes are unchanged siblings.
- Frontend/downstream consumers: later audit/archive/salvage/drill/retention scripts; display is explicitly excluded.
- Failure paths/rollback/stale state: overlap and too-small age fail before action; lookup of a cycle returns deterministic archive object/manifest paths.
- Evidence/audit/readiness: focused pytest, schema examples plus negative documents, ruff, and strict OpenSpec validation.
- Regression: shared root only and shared+override -> shared resolution then override precedence.
- Regression: archive root contains/is contained by cleanup target, or age 20 with retention 30 -> named validation error before mutation.
- Regression: equal/aliased/symlink-resolved archive and cleanup roots -> normalized overlap rejection; caller supplies its complete cleanup-root set.
- Regression: source-qualified, lane-typed forcing/runs/states identity with bound ISO `cycle_time` + compact `cycle_identity` -> deterministic sibling `archive.tar.zst` + `manifest.json`; shared source aliases normalize to canonical manifest IDs and lowercase object-store/archive path segments, different providers remain distinct, and unknown sources or unsafe/missing/cross-lane/time-mismatched identity or manifest/path mismatch fail before access.
- Regression: every completeness verdict binds a lane-discriminated stable inventory subject (`forcing_version_id`, `run_id`, or `state_id`) independently of its coverage mechanism; equal-window sibling subjects remain distinguishable, while missing/cross-lane subjects fail schema validation and later inventory runtime must reject duplicate or omitted subjects.
- Regression: `db-export` completeness is legal only for forcing/runs timeseries subjects; state subjects require product/hot-object coverage or remain a non-salvageable `gap` that blocks retention.
- Regression: persisted product manifests accept only canonical source IDs at both schema and semantic-binding boundaries; alias normalization remains available only before manifest production.
- Regression: valid source-less legacy state references (`source_id` NULL or the existing equivalent empty string) map explicitly to the same states-only reserved `legacy-unqualified` identity, using required `valid_time` as canonical cycle time; their archive paths are deterministic and disjoint from provider-qualified states, while forcing/runs reject the sentinel, whitespace/unknown sources fail, and no provider is invented.
- Regression: salvage object paths are safe root-relative `db-export/.../*.csv.zst`; other suffixes fail schema validation.
- Regression: #847 DB inventory is repeatable-read/read-only with a 20-second timeout and one captured audit time; non-decorrelated correlated probes include only forcing/run metadata with detail rows without full hypertable scans; authoritative metadata `[start_time,end_time]` is the exact inclusive selector window without recomputing detail bounds, and `window.end` owns age classification.
- Regression: forcing/run cycle identity accepts only exact UTC-hour metadata; non-zero minute/second/microsecond blocks instead of silently aliasing a neighboring archive cycle.
- Regression: strict forcing/run/state URI parsing binds row identity to physical artifacts; forcing manifests may extend beyond but must contain the authoritative DB subject window, run manifests bind exactly, and clone states may share only provenance-declared physical artifacts while retaining distinct `state_id` subjects.
- Regression: forcing basin identity comes from `core.model_instance`, presence probes never project arbitrary detail identity, and clone rows self-join a real origin whose model/source/time/URI/checksum plus canonical fingerprint all bind the shared artifact.
- Regression: provider/legacy/clone state product archives prove the exact physical state member path and origin-bound checksum; lane/model/time tarball identity alone cannot satisfy state coverage.
- Regression: legacy clone provenance canonicalizes source `NULL` and empty string to the same `legacy-unqualified` identity in both directions while provider/legacy drift still blocks.
- Regression: every evidence read and receipt replace is root-dirfd anchored with no-follow component opens; missing leaves behind symlinks, inode swaps, unsafe siblings and parse/hash cross-inode races fail closed.
- Regression: salvage traversal is bounded to 10,000 manifests/100,000 total entries/eight levels, and per-run output traversal is bounded to 10,000 entries/eight levels; both inspect every bounded sibling and publish a schema-valid `blocked` terminal receipt on overflow when the bootstrapped destination is safe, while run-output list/stat/child-open stays on one held directory-FD tree across pathname swaps.
- Regression: salvage enumeration/stat/child-open/manifest-read/object-hash stays on one held `db-export` FD tree; real-directory swaps cannot mix evidence namespaces or bypass the global entry cap.
- Regression: archive age shares the >=30-day foundation invariant without truthiness fallback, and all readable mismatch evidence survives coverage fallback precedence.
- Regression: missing archive namespaces are ordinary absence; existing unsafe/unreadable/malformed/conflicting evidence terminates with a schema-valid `blocked` receipt, while a fully readable size/checksum mismatch is recorded and treated as absent coverage so the safe `incomplete` coverage receipt can still publish.
- Regression: readable hot forcing manifest/member and state checksum mismatches are retained as absent-coverage evidence even when product/salvage wins; unsafe, malformed, permission and I/O failures remain blockers.
- Regression: `schema_version=1.1` terminal receipts are deterministic, exact-`oneOf`, and atomically replaced; success branches cover every subject exactly once and enforce the complete/incomplete aggregate plus forcing/run gap-selector bijection, while pre-publication audit blockers publish `blocked`. Once the single publication attempt starts, pre-replace failure preserves prior bytes and post-replace failure leaves content unknown; both are stderr-only, never retried, and never reported as `published`.
- Regression: #848 discovers forcing leaf, strict-prefix manifest-bound flat run tree, and provider/legacy physical state valid-time units without DB access or clone-target fabrication; forcing/run eligibility uses authoritative window end (state uses point valid-time), while exact-cutoff, malformed, ambiguous and unreadable candidates fail closed without hiding valid siblings.
- Regression: archive tar verification proves fail-fast exact safe regular-member path/size/sha bijection in addition to tarball sha; staged tar+manifest publish and only typed deterministic corrupt-final quarantine move whole leaf directories on one device, while operational verification failure preserves canonical evidence.
- Regression: source retirement fully re-verifies the pinned final pair and complete source/tombstone preimage before same-device rename/unlink, then deletes only exact allowlisted inodes; observed final-pair/path/content drift preserves source/tombstone, while post-final-check open-FD writes remain an explicit immutable-producer contract violation outside the rename protocol.
- Regression: mover dry-run mutates only safe lock metadata + its mode-0600 receipt, direct Python invocation owns flock, valid selection/deferred ordering is deterministic and bounded, locator-keyed discovery failures remain disjoint, and any failure makes the overall outcome non-zero without stopping independent bounded candidates.
- Regression: valid examples -> schema PASS; missing completeness verdict or salvage row count -> schema FAIL.
- Regression: product manifest row count/unsafe paths, invalid table-selector key, incomplete drill verdict details, or incomplete retention outcome details -> schema FAIL.
- Regression: product-only drill with empty selector list -> schema PASS; clean default test environment executes all schema negatives with zero skip.
- Regression: unchanged display import/call graph -> no archive resolver dependency and existing disk-only not-found semantics.
- Regression: unchanged `validate_object_path` and raw-retention/governance env aliases -> existing results and precedence remain stable.

Boundary-surface checklist:

- Shared helper root: `packages/common/storage.py`; read-only path derivation and validation only.
- Public entrypoints: #847 adds `scripts/node27_storage_inventory_audit.py`, a DB/filesystem read-only audit whose only write is its configured gate receipt; #848 adds the filesystem-only archive mover + wrapper, with explicit dry-run/enforce and internal flock. Display entrypoints remain unchanged.
- Producer/consumer evidence boundary: the audit is the sole archive-completeness receipt producer; #850 salvage consumes its exact selectors and #855 retention consumes its subject coverage. Product archive and `db-export` provenance remain distinguishable.
- Publish boundary: validated receipts explicitly opt into same-directory mode-0600 temporary files plus atomic replace, mandatory directory fsync, and post-replace parent-FD identity verification. Audit/config/evidence failures reached before publication starts publish `blocked`/`indeterminate`. Once the one publication attempt starts, a pre-replace write failure preserves the previous receipt and cleans temporary residue; an after-replace durability/namespace failure leaves target content unknown. Both are stderr-only, non-zero, never retried, and never `published`; a file-fsynced payload may already be visible after replace. #855 independently validates the currently configured two receipt contents and does not add producer status, a sidecar, or systemd state as a third gate. The configured parent is operator-controlled and non-rotating during publication. The shared atomic helper keeps its legacy default for unmigrated non-receipt callers. Product/archive deletion and other mutations remain out of scope.
- Mover mutation boundary: dry-run writes only safe lock metadata and its receipt. Enforce publishes a fully re-read staging leaf, may quarantine an invalid final leaf, then retires only a revalidated source preimage through a held-FD tombstone. Failures before tombstone rename preserve the source path; later uncertainty is non-zero and records complete/partial tombstone residue without falsely promising rollback.

## Workflow Fixture: Issue #849 Archive/Audit Systemd + Capacity + First Live Receipts

Fixture level: expanded. Repair intensity: high. Project profile: NHMS.

Change surface:

- `infra/systemd/nhms-node27-product-archive.{service,timer}` (new): oneshot service + daily timer running `scripts/node27_product_archive_once.sh`.
- `infra/systemd/nhms-node27-storage-inventory-audit.{service,timer}` (new): oneshot service + daily timer running the audit `_once.sh`; cadence must be strictly shorter than the retention gate's receipt validity window (design decision D6).
- `infra/env/node27-product-archive.example` (new): `NHMS_ARCHIVE_ROOT`, `NHMS_ARCHIVE_MIN_AGE_DAYS`, per-tick bound, free-space warn/refuse watermarks, tool path.
- `infra/env/node27-storage-inventory-audit.example` (new): DB URL, `NHMS_ARCHIVE_ROOT`, receipt path.
- `scripts/node27_resource_governance.py` extension: append the four new units to `DEFAULT_SERVICES`; add archive-root size + shared-volume free-space measurements (warn/refuse thresholds, initial values from Open Questions) to the governance receipt; existing receipt fields remain unchanged.
- `scripts/node27_product_archive.py` extension: enforce refuses (non-zero, receipt WARN, sources untouched) below the free-space refuse threshold; dry-run reports the same evaluation without action.
- `docs/runbooks/tier-node27-timeseries-storage.md` new section: archive mover + audit operation, rollback, timer cadence rationale.
- Node-27 live (task 2.5): committed schema-valid completeness receipt (recurring audit) + first enforce archive receipt covering aged `forcing/` + `runs/` + `states/` cycles, both under runbook receipts.

Must preserve:

- Existing `nhms-node27-*` user-unit installation pattern (`nwm` user, `WorkingDirectory=/home/nwm/NWM`, append log path, `OnCalendar` UTC, `Persistent=true`).
- Existing `scripts/node27_resource_governance.py` schema `nhms.node27_resource_governance.audit.v1` and its unit-status/free-space output shapes for existing consumers (extend, do not renumber or reshape).
- The mover's ADR 0002 invariant "no deletion without archive receipt": the free-space refusal must gate enforce **before** any source mutation, and must not weaken any existing verify-before-delete guarantee.
- ADR 0001 display carve-out: no display API/frontend/read code path may import the archive resolver or reference the archive root through env.
- Existing env aliasing conventions (`NHMS_ARCHIVE_ROOT` + `NODE27_<SCRIPT>_ARCHIVE_ROOT`) already pinned in #846 remain the sole configuration surface.

Must add/change:

- Two new systemd user-unit pairs (mover + inventory audit) and the two env examples above.
- Governance-receipt-visible archive root size + free-space measurement with warn/refuse thresholds; thresholds must be parseable from the env with strict validation, no truthiness fallback (matches #846/#848 discipline).
- Mover free-space refusal at enforce start (before candidate selection or source mutation) with structured receipt outcome — refusal is a first-class terminal, not an ad-hoc exit.
- Runbook section describing operation, rollback, refuse-threshold tuning, and the cadence-vs-retention-gate invariant.
- Live receipts committed under `docs/runbooks/receipts/<date>/` (or existing convention) capturing both the recurring-audit completeness receipt and the first enforce archive run.

Risk packs considered (core):

- Public API / CLI / script entry: selected — governance and mover scripts gain new env-driven behavior; unit files are a new operator surface.
- Config / project setup: selected — env examples + watermark parsing + unit installation are the feature.
- File IO / path safety / overwrite: selected — free-space refusal must fire before mover discovery/mutation; runbook receipts are new write locations under `NODE27_PRODUCT_ARCHIVE_LOG_ROOT`.
- Schema / columns / units / field names: selected — governance receipt gains archive-root fields; existing consumers must remain compatible.
- Auth / permissions / secrets: selected — env files are mode-0600 on node-27; DB URL in the audit env is display_ro only.
- Concurrency / shared state / ordering: selected — timer cadence must guarantee a fresh completeness receipt at every retention gate tick (D6).
- Resource limits / large input / discovery: selected — per-tick bound + free-space watermark bound production impact of first enforce.
- Legacy compatibility / examples: selected — existing governance receipt consumers and unit-installation runbooks must remain functional.
- Error handling / rollback / partial outputs: selected — mover refusal preserves sources; recurring audit failures reached before publication starts replace stale success with a `blocked`/`indeterminate` receipt, while publication-attempt failure follows the stderr-only pre/post-replace contract.
- Release / packaging / dependency compatibility: not selected — no new runtime or Python dependency.
- Documentation / migration notes: selected — runbook section is part of the deliverable.

Domain packs:

- Geospatial / CRS / basin geometry: not selected — no geometry, CRS, or basin-shape surface touched.
- Hydro-met time series / forcing windows: selected — first live enforce/audit fold pre-2026-06-16 forcing gap into the salvage selector list.
- SHUD numerical runtime / conservation / NaN: not selected — no solver, numerical, or SHUD-output surface touched.
- PostGIS / TimescaleDB domain behavior: not selected — no schema/query change.
- Slurm production lifecycle / mock-vs-real parity: not selected — node-22 untouched.
- External hydro-met providers / snapshot reproducibility: not selected — no provider discovery/fetch; archive lane consumes already-produced products.
- Run manifest / QC provenance: selected — audit and enforce receipts feed the change's evidence chain.
- Published NHMS artifacts / display identity: not selected — ADR 0001 carve-out preserved; display never reads archive.

Invariant Matrix:

- Governing invariant: enforce archive runs only when node-27 free space is above the configured refuse threshold; a fresh, schema-valid archive-completeness receipt must be present at every retention gate tick.
- Source-of-truth identity/contract: `NHMS_ARCHIVE_ROOT` + free-space watermarks + governance receipt schema `nhms.node27_resource_governance.audit.v1` (extended) + audit timer cadence < retention receipt validity window.
- Producers: extended `scripts/node27_product_archive.py` enforce entry (adds free-space refusal); extended `scripts/node27_resource_governance.py` (adds archive-root size + free-space measurements).
- Validators/preflight: env watermark parser + free-space measurement; existing #846 archive-root/overlap/min-age preflight remains upstream.
- Storage/cache/query: `scripts/node27_resource_governance.py` `DEFAULT_SERVICES` extended with 4 new units; no DB behavior added.
- Public routes/entrypoints: 4 new systemd user units + 2 env examples; installation via existing user-timer pattern.
- Frontend/downstream consumers: display API and frontend unchanged (ADR 0001 carve-out) — regression-check the display code path imports zero archive references.
- Failure paths/rollback/stale state: enforce below refuse threshold → refusal + WARN + non-zero, sources untouched; pre-publication audit failure → current `blocked`/`indeterminate` completeness receipt; publication-attempt failure → stderr-only with old bytes preserved before replace and target content unknown after replace.
- Evidence/audit/readiness: governance receipt lists 4 new units + archive-root free-space; first live receipts (recurring completeness + first enforce archive) committed under runbook receipts.

Regression rows:

- Env-file mode + DB role preflight → mover and audit `_once.sh` wrappers reuse the mode-0600 env-file check inherited from #847/#848 (loosen-mode env is refused before Python entrypoint); the audit env DB URL must resolve to `nhms_display_ro` (or another intentionally read-only role) per the runbook, and a superuser/write-capable DBURL used against the audit env is a documented rollback/lint finding, not a silent success. The audit itself is `REPEATABLE READ READ ONLY` (locked in #847), so no permission gate is added to the audit code path in #849.
- Governance audit with 4 new units enabled (systemctl mocked) → receipt includes archive + inventory-audit `service` + `timer` states beside existing entries; existing consumer fields unchanged.
- Governance audit measures archive root size + shared-volume free space → receipt reports both under a stable field name; thresholds evaluated deterministically; existing thresholds remain visible.
- Free space `<` refuse threshold with enforce requested → mover refuses at entry, no source mutation, receipt records refusal terminal, exit non-zero.
- Free space `>=` refuse threshold and `<` warn threshold with enforce requested → mover proceeds; receipt WARN.
- Free space `>=` warn threshold with enforce requested → mover proceeds; receipt clean.
- Invalid watermark env (empty, negative, non-numeric, truthiness `"0"`) → fail closed before mutation; no receipt lie.
- Audit timer OnCalendar cadence < retention receipt validity window (documented in runbook + reflected in the timer file) → retention tick always finds a fresh receipt.
- Display API / frontend import graph → zero references to archive resolver, `NHMS_ARCHIVE_ROOT`, or archive receipt path (ADR 0001 carve-out compatibility).
- Live: committed schema-valid recurring completeness receipt whose `salvage_selectors` covers the known pre-2026-06-16 forcing gap.
- Live: committed enforce archive receipt covering ≥1 verified object per source lane (`forcing/`, `runs/`, `states/`) in rotation scope, 0 checksum failures, source removal only for verified objects.

Boundary-surface checklist:

- Shared helper roots: `scripts/node27_product_archive.py` (mover enforce entry gains refusal), `scripts/node27_resource_governance.py` (governance audit gains archive-root capacity fields + registers 4 new units).
- Public entrypoints: 4 new systemd user units + 2 env examples; installation and rollback documented in runbook.
- Read surfaces: shared-volume `statvfs`, archive root `du`-equivalent walk (bounded), unit enumeration; no DB writes.
- Write/delete/overwrite surfaces: unchanged — free-space refusal is a gate on top of existing mover mutation boundary (#848); no new write path added.
- Staging/publish/rollback surfaces: unchanged — refusal precedes staging; if refusal fires mid-run (rare) it is treated as a failure preserving all source/staging state.
- Producer/consumer evidence boundaries: audit → completeness receipt is the sole gate for #855 retention; extended governance receipt keeps existing schema+consumers intact.
- Stale-state/idempotency boundaries: refusal is stateless (evaluated per tick); missing/stale receipt handled by #847 already.
- Unchanged downstream consumers: display API/frontend/read paths, `nhms_display_ro` DB role, node-22 (all out of scope; regression rows above enforce).

### Reopened task 2.5 closure (2026-07-15)

Issue #849 is reopened only to close the still-unchecked node-27 live evidence
task 2.5. Tasks 2.3 and 2.4 are already implemented and remain closed; this
closure does not redesign their units, configuration, capacity gates, or
archive/audit code.

- The committed 2026-07-15 controlled enforce receipt from #1065 selected only
  the `runs/` lane. It is useful prior evidence but is not the qualifying task
  2.5 enforce receipt and must not be combined with a later partial receipt to
  weaken the task's per-receipt lane-coverage requirement.
- The qualifying bounded enforce receipt must itself record at least one
  verified object for every aged source lane actually present in the current
  rotation scope, with zero checksum failures and retirement only after object
  verification. The qualifying enforce receipt's own complete discovery is the
  final lane-presence oracle: it must have `outcome=success`, an empty
  `discovery_failures` list, and committed counts derived from
  `candidates[].identity.lane`. A zero count proves only that no aged candidate
  for that lane existed at that receipt's cutoff; it does not prove the lane is
  globally absent. A separate dry-run is an authorization preview, never the
  final absence oracle.
- A fresh read-only dry-run precedes authorization and enforce. From its full
  ordered `candidates` list, compute the smallest `per_tick_bound` whose
  `selected` prefix contains at least one object from every lane with a nonzero
  candidate count. Commit the candidate counts, selected lane counts, selected
  total source bytes, exact UTC cutoff/age/bound, non-secret command/config
  fingerprint, and dry-run receipt SHA-256. If that minimum bound exceeds the
  deployed bound `8`, stop for a human-go that names the exact larger count and
  selected-byte ceiling. Only one enforce using the approved age/bound and no
  larger selected-byte total is authorized. Candidate drift that breaks those
  limits or lane coverage stops the run; it does not authorize a wider retry.
  Multiple enforce receipts must not be combined to satisfy lane coverage.
- Before enforce, node-27 must repeat the selected-source preflight for the
  exact candidates: current and future writer access remains valid, the
  configured archive root and free-space refuse/warn gates pass, and the
  existing verify-before-retire invariant is active. The controlled archive
  tick is the only source mutation authorized by task 2.5; database mutation,
  salvage export, compression, restore drill, retention, and manual source
  deletion remain out of scope.
- The accepted immutable completeness baseline is
  `storage-inventory-audit/completeness-incomplete-live-20260713T155314Z.json`:
  receipt SHA-256
  `e2d4f08150943f09af87d3e53e79cff26728fb438aabb545dabff07842497d04`,
  228 selectors, normalized selector-set SHA-256
  `ad5da1c51e1e90ec7bf2912d204186d21879be4e69536cc24a469520a486d0c6`.
  Its terminal envelope SHA-256 is
  `6964b13e0e7df187d4877a3f71d315f928e554fde69bd7524f354c4f63de39a7`
  and records node-27 head `bf9124aea6667fc116c872614d92de0e74a6cab1`.
  A replacement audit receipt is acceptable only if it is schema-valid and its
  normalized selector set is a superset of this baseline. Its overall outcome
  may remain `incomplete`: #1070 owns executing salvage and the follow-up audit
  that proves complete coverage. Task 2.5 must not claim that later state
  early.
- Both live receipts, the lane-presence/preflight evidence, exact deployed Git
  SHA, command/config fingerprints without secrets, receipt SHA-256 values,
  and post-enforce source/archive verification are committed under the
  existing runbook receipt convention. Immutable earlier receipts are not
  edited or replaced.

Reopened closure invariants:

- Governing invariant: a qualifying enforce tick is bounded, passes all
  existing fail-closed preflights, and covers every aged lane proven present
  in that tick's rotation scope without checksum failure or unverified source
  retirement.
- Source-of-truth identity: the enforce receipt itself is the lane-presence
  oracle and is bound to the exact deployed SHA, receipt UTC cutoff/window,
  effective non-secret configuration, archive root identity, selected source
  identities, and receipt SHA-256. The preceding dry-run controls authorization
  scope but cannot suppress a lane discovered by enforce.
- Failure/rollback: any ambiguous lane inventory, failed writer-access or
  capacity preflight, checksum mismatch, publish uncertainty, or source/archive
  post-check fails task 2.5; no follow-on issue is started and no manual cleanup
  is used to manufacture PASS.
- Evidence boundary: #849 commits the live audit/archive evidence only; #1070
  consumes the selectors for salvage and owns the later complete audit, while
  #1069/#1071/#1072 retain compression, drill, and retention ownership.

## Workflow Fixture: Issue #851 Hypertable Compression Migration + Receipted Runner

Fixture level: expanded. Repair intensity: high. Project profile: NHMS.

Migration slot deviation from issue body: issue #851 and `tasks.md` line 920 pin task 4.1 to migration `000043`; that slot is already occupied (`000043_canonical_grid_snapshot.sql`, and `000044`–`000046` are also occupied by later work). This issue lands at the next free slot `000047`. `tasks.md` line 920 is corrected in the same PR to cite `000047` so future readers do not chase a stale slot.

Change surface:

- `db/migrations/000047_hypertable_compression_settings.sql` (new): `ALTER TABLE ... SET (timescaledb.compress = true, timescaledb.compress_segmentby = ..., timescaledb.compress_orderby = ...)` for `hydro.river_timeseries` and `met.forcing_station_timeseries`. No `add_compression_policy` — script-driven only (D3 rejects background policy jobs: no receipts, no bounds, invisible to governance audit).
- `scripts/node27_timeseries_compression.py` (new): compression runner selecting terminal chunks whose `range_end < (now_utc - lag)`, default lag 7 d = one chunk width; per-chunk `compress_chunk`; per-tick chunk bound (deferred remainder listed); dry-run default + `--enforce` flag; in-process `fcntl.flock` LOCK_EX|LOCK_NB on mode-0600 O_CREAT|O_EXCL|O_NOFOLLOW lock file; receipt via `atomic_write_bytes_no_follow(require_durable_replace=True)` with per-chunk + per-table before/after bytes.
- `scripts/node27_timeseries_compression_once.sh` (new, 0755): mode-0600 env-file preflight + absolute-path guards + tool availability check, mirroring mover/audit wrappers (#848 / #849 shape).
- `schemas/timeseries_compression_receipt.schema.json` (new): pinned receipt JSON contract (top-level shape below under "Must add/change") — CI json-schema-validate loop consumes it via basename pairing with the sibling positive example.
- `schemas/examples/timeseries_compression_receipt.example.json` (new): schema-valid positive example. Filename is exactly `timeseries_compression_receipt.example.json` so `.github/workflows/ci.yml` `check-jsonschema` loop (lines 115-136) auto-pairs it via `basename(example, .example.json) → schemas/<base>.schema.json`; any other filename would silently `WARNING: No schema found` and skip.
- `infra/env/node27-timeseries-compression.example` (new): documents runner env vars; header comment states verbatim `NHMS_ARCHIVE_ROOT` is NOT read by this runner (compression is DB-side only) so operators do not attempt to sync it against the mover/audit/governance archive-root trio.
- `tests/test_node27_timeseries_compression.py` (new): chunk-selection classification (recent skipped, terminal eligible, active never), per-tick bound + deferred remainder, dry-run vs enforce semantics, flock contention, config parse fail-closed (invalid lag / bound / DB URL), wrapper shell-contract parametrized cases, receipt schema/semantic contract.
- `openspec/changes/tier-node27-timeseries-storage/tasks.md` line 920 edit: `000043` → `000047`.

Not touched (out of scope):

- `scripts/node27_timeseries_compression_once.sh` **is** touched (wrapper is the runner's operator surface). Systemd units + governance registration (task 4.4) belong to #853, NOT #851.
- Fail-closed compressed-chunk write guard belongs to #852 (task 4.3). Compression itself is safe without the write guard as long as no ingest hits a compressed chunk during the tests; production compression on node-27 is task 4.5 (#853).
- Initial live compression + representative-query timing receipts are task 4.5 (#853).
- No touching of `scripts/node27_product_archive.py`, `scripts/node27_storage_inventory_audit.py`, `scripts/node27_resource_governance.py` — this issue is chunk-writer only.
- No touching of `apps/api/**`, `apps/frontend/**`, `workers/output_parser/**`, `workers/forcing_producer/**` — ADR 0001 display carve-out; ingest write paths owned by #852.

Must preserve:

- The chunk-select query must not full-scan detail hypertables. Reuse the existing `timescaledb_information.chunks` lookup pattern from `scripts/node27_resource_governance.py:454-513` (identity-leading index-only style); the compression runner MUST NOT hit `hydro.river_timeseries` / `met.forcing_station_timeseries` rows directly.
- ADR 0002 hot/cold tiering invariant: compression is a "cold" operation on terminal chunks; the active chunk is never compressed (chunk selection filter must exclude it).
- Existing `node27_product_archive.py` runner patterns for lock / atomic receipt publication / dry-run default / per-tick bound / env aliasing (`NODE27_<SCRIPT>_<KEY>` overriding `NHMS_<KEY>` when applicable) — do not fork a new discipline.
- TimescaleDB 2.10 catalog surface: on 2.10 the `timescaledb_information.hypertables` view does not expose segmentby/orderby; verification MUST use `timescaledb_information.compression_settings` (rows with `segmentby_column_index` set for segmentby columns; rows with `orderby_column_index` set for orderby columns).
- Compression segmentby columns MUST cover the primary-key columns of each hypertable (TimescaleDB 2.10 unique-constraint requirement); D3 already enumerates the mapping.
- The runner MUST NOT decompress anything. `decompress_chunk` is operator-only (documented in the compression runbook section, but #851 does not author that runbook — task 7.1 owns it).

Must add/change:

- Migration `000047` adding compression settings, matching the house style (no BEGIN/COMMIT wrap; `--` prose header citing #845 / #851 / OpenSpec change; idempotent enough that a re-apply on already-compressed table does not error — TimescaleDB `SET (...)` is idempotent, but the migration must handle the case where a previous partial apply left settings on one table but not the other).
- Compression runner emitting receipt with:
  - `schema_version: "1.0"` (matches #848 mover schema-version discipline).
  - Top-level: `now_utc`, `lag_seconds`, `per_tick_bound`, `mode` (`dry-run` | `enforce`), `outcome` (`clean` | `partial` | `refused_lock` | `refused_config`), `selected` (list of chunk descriptors with before/after bytes), `deferred` (list of chunk descriptors beyond bound), `skipped` (list of chunk descriptors inside lag window), `per_table_totals` (`{table_name → {before_bytes, after_bytes, chunks_compressed}}`).
  - Per-chunk descriptor: `hypertable_schema`, `hypertable_name`, `chunk_schema`, `chunk_name`, `range_start`, `range_end`, `before_bytes`, `after_bytes` (null on dry-run or failure).
  - Receipt validates against a new `schemas/timeseries_compression_receipt.schema.json` (this schema pins JSON contract; example + negative test rows land in the same PR).
- Env example `infra/env/node27-timeseries-compression.example` documenting `DATABASE_URL`, `NODE27_TIMESERIES_COMPRESSION_LAG_SECONDS` (default 604800 = 7 d), `NODE27_TIMESERIES_COMPRESSION_PER_TICK_BOUND` (default 5), `NODE27_TIMESERIES_COMPRESSION_RECEIPT_PATH`. Header comment (pinned wording): `# This runner does NOT read NHMS_ARCHIVE_ROOT / archive watermarks — compression is DB-side only. Do not sync this env against node27-product-archive.env / node27-storage-inventory-audit.env / node27-resource-governance.env.`

Risk packs considered (core):

- Public API / CLI / script entry: **selected** — new mutation script; `--enforce` flag; new env vars.
- Config / project setup: **selected** — new env example + strict lag/bound parsing (no truthiness fallback, matches #846/#848 discipline).
- File IO / path safety / overwrite: **selected** — receipt publication (atomic write, dirfd, no-follow); lock file mode/permissions.
- Schema / columns / units / field names: **selected** — new receipt schema + example + negative tests.
- Auth / permissions / secrets: **selected** — env file mode 0600; DB URL is a write-capable superuser role (compression is DDL/DML, not read-only).
- Concurrency / shared state / ordering: **selected** — in-process flock; active chunk never compressed; deferred remainder ordering.
- Resource limits / large input / discovery: **selected** — per-tick chunk bound; compression is CPU-and-IO intensive.
- DB migration / DDL: **selected** — migration `000047` is production DDL.
- Error handling / retries / backoff: **selected** — `compress_chunk` failure per candidate must not corrupt the receipt (per-candidate outcome, not aborting the run).
- Auth / secrets in logs: **selected** — DSN must never appear in receipt or stderr.
- Testing / evidence rigor: **selected** — chunk-selection classification must be unit-testable without a real DB (monkeypatch DB query function, consistent with `test_node27_resource_governance.py`).
- Publish/delete/deletion safety: not selected as its own category — compression is not deletion; write-guard is #852.

Risk packs considered (domain):

- Geospatial / grid CRS: not selected — compression is opaque to grid semantics.
- SHUD / hydrology model: not selected — compression preserves row values; SHUD reads are downstream of hot data anyway.
- External providers / GRIB / weather feeds: not selected — compression is DB-side only.
- Auth regression: not selected explicitly (inherits mode-0600 env-file discipline from #847/#848 — reused by wrapper preflight test, but no new auth surface).

Six-reviewer high-risk escalation triggered by: production DDL migration + `compress_chunk` mutation + new receipt schema + shared retention gate consumer (task 6.3 will consume this via governance registration in #853; #851 must leave contracts clean).

Invariant Matrix:

- Terminal-chunk-only: chunk-select query filter `range_end < now - lag` yields only chunks strictly older than the lag window; active chunk (whose `range_end` >= now) never selected. Test: two chunks — one `range_end = now - 3d` (skipped), one `range_end = now - 10d` (eligible).
- Per-tick bound respected: `SELECT ... LIMIT bound` (or explicit slice after ordering) — no more than `bound` chunks fully compressed; deferred remainder listed in receipt with reason "beyond per-tick bound".
- Dry-run isolation: `mode=dry-run` writes only the receipt; no `compress_chunk` call; per-chunk `after_bytes` = null.
- Flock lock-holder-only mutation: contender performs no DB call or compression,
  emits a redacted structured stderr diagnostic, and atomically replaces the
  configured receipt with a schema-valid `refused_lock` receipt so the required
  lock-skip state is governance-visible rather than leaving stale evidence.
- Strict config parse: invalid lag (empty, negative, non-numeric, `"0"`) → exit non-zero before any DB call; no stale receipt overwrite.
- Migration idempotent on partial state: re-applying after only one table's ALTER succeeded must fix the second table without erroring on the first.
- Compression `segmentby` covers PK columns (TimescaleDB 2.10 unique-constraint requirement) — asserted via test that reads the migration text and cross-references the expected PK column list per table.
- Compressed-chunk catalog verifiable: after migration, `timescaledb_information.compression_settings` rows for both hypertables list exactly the D3-specified columns (segmentby + orderby); this is task 4.1 acceptance and is unit-testable by parsing the migration file plus a real-DB smoke marker (deferred to #853 for the live oracle).
- Ingest write-guard NOT weakened: this issue delivers zero coupling to `workers/output_parser/parser.py`, `workers/forcing_producer/store.py`, `packages/common/forcing_domain_handoff_apply.py` — #852 owns that. Runner has no import graph reaching those modules.
- Display carve-out: `grep` of `apps/api`, `apps/frontend` for `timeseries_compression`, `compress_chunk`, `NODE27_TIMESERIES_COMPRESSION` → zero hits (ADR 0001).
- Env-file mode + DSN privilege preflight → compression `_once.sh` wrapper reuses the mode-0600 env-file check inherited from #847/#848 and refuses a loosened env. DB URL is superuser (compression is DDL/DML) and must remain out of the audit env; audit still uses `nhms_display_ro`.
- Receipt schema fresh contract: new `schemas/timeseries_compression_receipt.schema.json` includes positive example (validates via `check-jsonschema` CI loop) + Python-side negative tests for missing per-chunk fields, missing per-table totals, wrong mode enum, etc. No stale `warn_bytes`/`refuse_bytes` fields leak from mover schema (independent contract).
- No accidental compression policy: migration MUST NOT call `add_compression_policy`; explicit test grepping the migration file for `add_compression_policy` finds zero matches.

Boundary-surface checklist:

- Shared helper roots: none forked (new `scripts/node27_timeseries_compression.py` is a fresh script, reuses `packages/common/safe_fs.py:atomic_write_bytes_no_follow` and the flock pattern from `node27_product_archive.py` inline without extracting).
- Public entrypoints: 1 new user-facing runner (`node27_timeseries_compression.py`) + 1 wrapper + 1 env example + 1 migration + 1 schema.
- Read surfaces: `timescaledb_information.chunks`, `timescaledb_information.hypertables`, `timescaledb_information.compression_settings` — all catalog-only, no detail-hypertable row reads.
- Write/delete/overwrite surfaces: `compress_chunk` calls (DDL/DML); receipt publication (atomic, dirfd, no-follow); lock file creation.
- Staging/publish/rollback surfaces: no staging (compression is direct on chunk); rollback = operator-only `decompress_chunk` (documented in task 7.1 runbook, not authored here).
- Producer/consumer evidence boundaries: receipt → task 4.5 live receipt (#853 consumes); `timescaledb_information.compression_settings` → task 4.1 verification (real-DB oracle in #853).
- Stale-state/idempotency boundaries: re-running the runner over already-compressed chunks must skip them (chunk-select query filters on `is_compressed = false` from `timescaledb_information.chunks`, which on TimescaleDB 2.10 exposes an `is_compressed` boolean column on this view — do NOT reach into `_timescaledb_catalog.chunk.compressed_chunk_id` for the same signal).
- Unchanged downstream consumers: display API/frontend/read paths (ADR 0001), ingest write paths (#852 owns write-guard coupling), retention gate (#855 owns receipt consumption).

## Workflow Fixture: Issue #1069 Node-27 Live Initial Compression Closure

Fixture level: expanded. Repair intensity: high. Project profile: NHMS.

This fixture closes only task 4.5 on the node-27 real-DB oracle. It consumes
the migration/runner/schema delivered by #851, the fail-closed write guard
delivered by #852, and the systemd/governance wiring delivered by #853. Ground
truth inspection found one adjacent wiring defect that the earlier #853 issue
body did not close: the committed compression service invokes the wrapper
without `--enforce`, so its timer can publish dry-run receipts but can never
apply D3/D7 compression. #1069 therefore includes the smallest committed fix
needed to make the installed timer operational; a host-only drop-in or an
untracked unit edit is forbidden.

### Exact change surface

- `infra/systemd/nhms-node27-timeseries-compression.service`: append the
  literal `--enforce` argument to `ExecStart`. The timer-owned service is the
  recurring mutation entrypoint; an operator dry-run continues to invoke
  `scripts/node27_timeseries_compression_once.sh` directly without the flag.
- `scripts/node27_timeseries_compression.py`: close the already-specified task
  4.2 lock contract by atomically publishing a schema-valid `refused_lock`
  receipt when flock is contended. The receipt records no selected/deferred/
  skipped chunks and no mutation; stderr remains a redacted diagnostic.
- `schemas/timeseries_compression_receipt.schema.json` and its example: make
  the lock-refusal shape reachable and unambiguous without weakening the
  existing dry-run/enforce shapes.
- `tests/test_node27_timeseries_compression.py`: extend the unit-file wiring
  invariant so the committed service must call the wrapper with the literal
  `--enforce`, while the wrapper remains dry-run by default when invoked
  manually; add exact lock-receipt publication, schema, no-mutation, and
  pre-existing-receipt replacement tests.
- `schemas/timeseries_compression_live_evidence.schema.json` plus a positive
  example: pin the task-4.5 terminal envelope described below.
- `scripts/node27_timeseries_compression_live_evidence.py` plus focused tests:
  an independent verifier that consumes the two runner receipts and captured
  pre/post/catalog/benchmark evidence, recomputes hashes/counts/arithmetic and
  thresholds without importing the runner's receipt builder, then atomically
  publishes the terminal envelope. It may read live catalogs for post-checks
  but never migrates, compresses, decompresses, drops, or changes roles.
- `scripts/node27_timeseries_compression_benchmark.py` plus focused tests: a
  read-only production-source capture helper. Curve SQL and all eight binds
  come from `PsycopgForecastStore`; MVT SQL/params come from
  `postgis_tile_sql("hydro")` and `_postgis_tile_params`. It records cold,
  adaptive warmups, seven full plans, activity and raw result identity, then
  merges immutable before/after slices without accepting source/query/bind
  drift. `DATABASE_URL` is environment-only and output is atomic mode 0600.
- `docs/runbooks/tier-node27-timeseries-storage.md`: record the controlled
  migration/install/first-run procedure, the benchmark SQL and acceptance
  threshold below, timer activation order, and the truthful partial-compression
  recovery boundary.
- `docs/runbooks/receipts/tier-node27-timeseries-storage/timeseries-compression/`:
  add the schema-valid dry-run/enforce receipts plus one terminal envelope and
  README. The terminal envelope references node-27-local backup/preflight
  artifacts by absolute path + sha256; credentials and the backup payload are
  not committed.
- `openspec/changes/tier-node27-timeseries-storage/tasks.md`: tick 4.2 after
  the lock-receipt blocker is fixed and verified; tick 4.1 only after the live
  migration/catalog proof; tick 4.5 only after every terminal acceptance below
  passes. Each tick links its evidence.
- `openspec/changes/tier-node27-timeseries-storage/design.md`: this fixture.

Runner/schema semantics change only for the missing lock-refusal receipt above;
migration, chunk selection, compression, and ordinary dry-run/enforce receipt
semantics remain unchanged. Do not add a new selector flag, compression policy,
alternate host-only receipt format, or host-only script. The live operator uses
the exact branch head under review; node-27 is ff-only synchronized and its
existing unrelated generated evidence remains untouched. The stale #851
fixture sentence that says a contender does not touch the receipt is corrected
to require the new immutable `refused_lock` receipt, matching tasks/spec.

### Node-27 preflight, recoverability, and secret boundary

Before any migration or compression mutation:

1. Bind the host, UTC time, repository absolute path, branch head SHA,
   TimescaleDB/PostgreSQL versions, database name, database instance identity,
   and container/service state into a mode-0600 preflight JSON under
   `/home/nwm/NWM/.nhms-issue1069-live/`. Refuse a dirty tracked worktree,
   wrong DB/host, missing extension, or branch-head mismatch.
2. Copy `infra/env/node27-timeseries-compression.example` to the canonical
   untracked env path, source the existing node-27 ingest writer credential
   without printing it, replace placeholders, and `chmod 0600`. Evidence may
   record only `current_user`, host/port/dbname, booleans for required
   privileges, env path/mode, and a redacted DSN. It MUST NOT contain the
   password, full `DATABASE_URL`, environment dump, shell tracing, or process
   argv containing credentials.
3. Use the existing `nhms` writer identity only. Do not create a role, grant
   privileges, promote a role, or reuse `nhms_display_ro`. Truthfully record
   `current_user=nhms`, `rolsuper`, `rolcreaterole`, `rolcreatedb`, both target
   relations' `relowner = current_user`, and EXECUTE on the exact installed
   `compress_chunk(regclass,boolean)` signature. Current node-27 truth is that
   `nhms` is a superuser; the envelope must not call this least privilege or
   omit it. The run nevertheless executes no CREATE ROLE, GRANT, role mutation,
   or table outside the migration/runner's fixed two-table allowlist.
4. Create a mode-0600, custom-format, schema-only `pg_dump` of the live `nhms`
   DB before migration, run `pg_restore --list` successfully, and bind its
   path, byte count and sha256 in the preflight receipt. Also capture the two
   target tables' pre-migration catalog state as canonical JSON + sha256. This
   is a schema forensic snapshot / DDL inventory only: it is not a restore
   test, data backup, recoverability proof, or compressed-chunk rollback proof.
5. Record the currently enabled/active/sub states, `MainPID`, result, and a
   bounded journal tail for
   `nhms-node27-autopipe.{timer,service}` and the compression service/timer.
   Stop only the autopipe timer for the controlled window. Quiescence requires
   `MainPID=0`, no activating/running autopipe process, and no non-idle
   `pg_stat_activity` write statement or conflicting relation lock touching
   either target hypertable or selected chunk. `ActiveState=failed` with
   `MainPID=0` is allowed and preserved as prior evidence; do not call
   `reset-failed` merely to manufacture `inactive`. Restore only the timer's
   exact prior enabled/active state in terminal cleanup and record the service's
   original failed/result state without claiming #1069 fixed it. Manual
   historical reingest remains prohibited during the window.

The write guard in all three #852 wire sites must be present at the deployed
SHA before migration. It remains the fail-closed boundary after compression,
but it does not remove the need to quiesce ingestion: the guard/catalog check
and `compress_chunk` are not one transaction and therefore do not eliminate a
race with a simultaneous historical write.

### Migration idempotency and D3 catalog proof

Apply `db/migrations/000047_hypertable_compression_settings.sql` with
`ON_ERROR_STOP=1`, capture the redacted exit/result, then apply the same file a
second time with `ON_ERROR_STOP=1` **only if the first apply exited zero**.
Both successful invocations must exit zero and the canonical catalog JSON after
the first and second apply must be byte-identical. If the first apply exits
nonzero, preserve its exit/catalog evidence and stop. Any partial-apply recovery
reapply is a separately authorized production mutation; it is not disguised as
the routine idempotency proof and never edits Timescale catalogs directly.

The real-DB catalog proof is exact, not merely "non-empty": both rows in
`timescaledb_information.hypertables` have `compression_enabled = true`, and
`timescaledb_information.compression_settings` has exactly the following
indexed settings (no missing or additional segment/order column):

| Hypertable | segmentby `(index: column)` | orderby `(index: column)` |
|---|---|---|
| `hydro.river_timeseries` | `1: run_id`, `2: river_network_version_id`, `3: river_segment_id` | `1: variable`, `2: valid_time` |
| `met.forcing_station_timeseries` | `1: forcing_version_id`, `2: station_id` | `1: variable`, `2: valid_time` |

The catalog query records `attname`, `segmentby_column_index`,
`orderby_column_index`, `orderby_asc`, and `orderby_nullsfirst`, ordered by
schema/table and the non-null index. Its canonical JSON and sha256 are included
in the terminal envelope. No `add_compression_policy` job may exist for either
table before or after the run.

### Timer installation without an accidental first mutation

Copy the committed service/timer byte-for-byte to
`~/.config/systemd/user/`, prove both sha256 pairs match the deployed repository
files, create the log directory, and run `systemctl --user daemon-reload`.
Run `systemctl --user enable nhms-node27-timeseries-compression.timer` without
`--now`; require the timer and service to remain inactive through migration,
dry-run, the explicit enforce, query benchmarking, and terminal validation.
Because the service now contains `--enforce` and the timer has
`Persistent=true`, starting it before the controlled run is a mutation race.

Task 4.5 follows #1069's stated acceptance exactly: the timer is enabled but
remains inactive. Capture `is-enabled=enabled`, `is-active=inactive`, the
service's resolved `ExecStart`, and the absence of any service activation after
installation. Do **not** start the `Persistent=true` timer in this issue; a
first start can catch up the missed 04:25 UTC event and cause an unauthorized
second compression batch. Timer activation is a later, separately controlled
operation after its catch-up behavior is protected by the schema-valid
`refused_lock` path. This issue never uses "start and see whether it mutated"
as a safety test.

### Dry-run scope and controlled enforce

Use lag `604800` seconds and **exact live bound `1`**. Node-27 discovery before
fixture review found five eligible chunks in each table; the runner's stable
schema/name ordering would make the example default bound `5` select only
hydro and mutate about 106.47 GB in one invocation. That scope is not
authorized here. The qualifying single selected chunk must have independently
measured pre-compression `pg_total_relation_size <= 8589934592` (8 GiB), the
same selected-total cap, at least 300 GiB filesystem free-space headroom, and a
wrapper-level external timeout of 900 seconds. Otherwise stop before enforce.
The independent catalog preflight reproduces the runner predicate and order:
uncompressed chunks from only the two D3 hypertables, strict
`range_end < now_utc - interval '604800 seconds'`, ordered by
`hypertable_schema, hypertable_name, range_end ASC`, with the first `bound`
selected and the rest deferred. Refuse the live run unless at least one
selected `hydro.river_timeseries` chunk exists, all selected chunks are
terminal, none intersects the active/lag window, and no candidate lies within
ten minutes of the cutoff (avoids dry-run/enforce scope drift as wall time
advances).

Run the wrapper once without `--enforce` to a task-specific receipt path.
Require `mode=dry-run`, `outcome=clean`, `selected length = bound = 1`, every
`after_bytes=null`, zero DB catalog mutation, and no timer activation. Freeze
the ordered selected identity tuple
`(hypertable_schema,hypertable_name,chunk_schema,chunk_name,range_start,range_end)`
as canonical compact JSON and record its sha256. Immediately before enforce,
repeat the independent catalog query and require the exact selected tuple hash
to match.

The single authorized first enforce is the direct wrapper invocation with
literal `--enforce`, the same env, lag, bound, lock, and a distinct receipt
path. Its permitted mutation set is exactly the dry-run selected tuple list;
the enforce receipt must name the same ordered list. A mismatch is failed
evidence even if some chunks were already compressed; it does not authorize a
second enforce. No background timer, manual `compress_chunk`, retention job,
or other compressor may run concurrently.

### Size, compressed-count, and receipt evidence

Immediately before dry-run and immediately after enforce, collect one
canonical relation snapshot for both hypertables with:

- `hypertable_size('<schema>.<table>'::regclass)` as the acceptance size that
  includes TimescaleDB chunks;
- `pg_total_relation_size('<schema>.<table>'::regclass)` as the parent-relation
  diagnostic only (it MUST NOT substitute for `hypertable_size`);
- counts from `timescaledb_information.chunks` grouped by hypertable and
  `is_compressed`, plus the exact compressed sibling relation names and their
  `pg_total_relation_size` values.

Acceptance requires: enforce `mode=enforce`, `outcome=clean`, no selected
descriptor has `error` or null `after_bytes`; `per_table_totals` arithmetic
recomputes exactly from selected descriptors; every selected chunk is now
`is_compressed=true`; compressed-chunk count increases by exactly the number
of successfully selected chunks; total selected `after_bytes` is strictly
less than total selected `before_bytes`; and the combined post-enforce
`hypertable_size` of the two tables is strictly less than its pre-enforce
value. Both tables must have before/after size and compressed-count rows even
when this first bounded batch selects chunks from only one table.

Validate both runner receipts against
`schemas/timeseries_compression_receipt.schema.json`, then apply the semantic
checks above independently of the runner. New runner receipts use version
`2.0` and freeze `head_sha` before any DB call; version `1.0` remains readable
as historical operational evidence but cannot satisfy the live terminal v3
contract. Record mode-0600 receipt paths, byte counts, full sha256 values,
deployed head, catalog hashes, size snapshots, selection hash, command exit
codes, and final verdict in a separate atomic terminal envelope. A
schema-valid receipt with wrong scope, partial outcome, bad arithmetic, or
failed post-catalog proof is not acceptance evidence.

The terminal envelope conforms to
`schemas/timeseries_compression_live_evidence.schema.json`, qualifying schema
version `3.0`, with required top-level keys: `schema_version`,
`qualifies_task_4_5`, `issue`, `generated_at`,
`node`, `mutation_head_sha`, `verifier_head_sha`, `database_identity`,
`authorization`, `execution`, `recovery`, `preflight`, `migration`, `selection`,
`receipts`, `sizes`, `catalog`, `benchmarks`, `cleanup`, `chronology`,
`source_manifest`, `out_of_scope`, and `verdict`. Nested
required fields bind the DB
instance/version and truthful role flags; forensic dump/hash; first/second
migration exit/catalog hashes; bound=1, selector hash/bytes/caps; dry-run and
enforce paths/hashes/schema+semantic verdicts; pre/post table sizes and chunk
counts; D3/policy verdict; per-query source/query/result hashes, raw samples,
cache class and thresholds; autopipe timer restoration; compression timer
enabled/inactive state; and explicit false flags for retention/drill/node-22.
Canonicalization for all embedded JSON hashes is UTF-8
`jq -cS <expression>` including its trailing newline. A verifier independent
of the compression runner recomputes every derivable count/hash/arithmetic and
validates this schema before it may emit `PASS_TASK_4_5`; unit tests cover each
required-field omission and a semantically inconsistent but schema-valid
envelope.

The mutation and verifier SHAs are distinct provenance fields. The immutable
pre-mutation preflight binds `mutation_head_sha`; the verifier may run at a
later reviewed `verifier_head_sha` but cannot rewrite historical preflight.
For the separately authorized evidence replay, `authorization` additionally
freezes `replay_decompression=true` and exactly one decompression invocation.
The required `recovery.preflight` and `recovery.receipt` are two distinct
hashed artifacts bound to node-27, the same mutation SHA/database identity,
and the exact six-field target
`hydro.river_timeseries` /
`_timescaledb_internal._hyper_3_7_chunk` /
`[2026-05-28T00:00:00Z, 2026-06-04T00:00:00Z)`. The preflight proves the
same complete safety boundary as the compression preflight — clean worktree,
container/role/database identity, mode-0600 env, write guards, quiescent
autopipe and DB writers/locks, inactive compression units, four unit states
and their bounded journals — plus compressed target state, row count and at
least 300 GiB free space. The receipt proves exit zero, the exact returned
relation, decompressed post-state and the same positive row count. Chronology
is fail-closed:
recovery preflight <= decompression start <= decompression finish <= fresh
compression preflight. Only that complete proof permits the truthful
`out_of_scope.decompress_run=true`; omission, mismatch or `false` blocks PASS.
Selection has two different timestamped artifact refs (post-dry-run and
pre-enforce), each with the complete ordered candidate set, cutoff and selected
tuple. Both observations and the new dry-run/enforce receipts must reselect
that same exact recovered target. The second observation is at most 60 seconds
before enforce. Benchmark
evidence stores every actual SQL bind, the cold execution, two to five
warmups, activity samples, and seven measured plans per phase; all seven after
plans must bind the selected `DecompressChunk`.

#### Invariant-closure amendment after full cross-review

The 2026-07-15 v2 replay remains immutable database history, but its terminal
is not accepted by the expanded fixture. A passing terminal now additionally
requires all of the following producer-independent proof:

- Every artifact is opened through one no-follow descriptor, rejected against
  its type-specific byte ceiling before allocation, and the same bytes are
  hashed, UTF-8 decoded, JSON parsed, and complexity-bounded. Receipt/evidence
  schemas, migration, production source, and repository unit refs bind the
  canonical checkout paths and reviewed Git blobs; copies, symlinks, weak
  schemas, and credential-bearing fields fail closed.
- Migration apply 1/apply 2, authorized decompression, dry-run, and enforce each
  have distinct ordered invocation artifacts with sanitized exact argv,
  `ON_ERROR_STOP`, mutation SHA, 900-second timeout, start/finish/exit, and
  receipt/catalog hashes. Invocation counts are derived from these records,
  never from authorization scalars. The streamed dump descriptor is inspected
  by the pinned PG15 `pg_restore --list`; its bounded raw output/version/exit
  must match the persisted listing and exact pre-migration catalog shape.
- Size and catalog snapshots carry distinct identities, mutation SHA, capture
  times, and enforce chronology. The selected origin is absent before and
  present after, the selected table count changes by exactly +1, and the
  sibling table by exactly +0. Both dry-run and enforce totals are recomputed.
- Cleanup binds reviewed repository and installed service/timer bytes, resolved
  `ExecStart` with exactly one `--enforce`, all four final unit states and
  journals, the governed activation window, zero compression-service
  activations, and exact restoration of the preflight-recorded prior autopipe
  timer/service states.
- Benchmark capture exercises the public `PsycopgForecastStore.forecast_series`
  owner through a recording adapter and the public MVT owner/route parameter
  construction. Frozen requests must lie inside the selected chunk, curve rows
  must be non-empty, each statement has fixed statement/lock timeouts, the
  phase has a wall deadline, and an independent autocommit monitor records
  session/query-start signatures before cold, between phases, mid-measurement,
  and after result capture. Count-stable identity drift blocks publication.
  A qualifying after plan binds `DecompressChunk` and the selected
  origin/sibling in exact structural fields on the same Custom Scan node;
  direct-child, Filter, alias and unrelated branches cannot satisfy it, and
  before plans cannot already use that selected decompression path.
- A compression exception after possible commit triggers fresh exact-target
  catalog reconciliation. Receipts distinguish `failed_before_mutation`,
  `committed`, and `indeterminate`; every failure with a known destination
  atomically replaces stale success and never copies exception credentials.

#### Round-2 invariant-closure amendment

- Current task-4.5 acceptance is a strict version-3 branch with
  `qualifies_task_4_5=true`. Historical version-2 terminals remain readable as
  superseded facts but cannot enter current acceptance. Version-1 runner
  receipts keep only historical outcomes/fields; new failure markers are
  version 2 with explicit provenance state.
- Evidence files are descriptor-pinned and bounded. Large dump hashing is
  streamed with retained canonical path/dev/inode identity; Git blobs are
  sized by timed `git cat-file -s` before bounded reads. JSON recursion errors,
  node/depth/array ceilings and shared all-string secret scanning fail closed.
  Normal publication reopens every retained reference. Output is disjoint from
  the bundle and all inputs by normalized path and inode; known-safe failures
  replace stale success with a versioned tombstone, while unsafe/unknown/input-
  alias paths remain untouched.
- The terminal retains both migration invocations, recovery, dry-run and
  enforce invocations, real dump-list proof, execution audit, chronology and
  source manifest. The locked audit namespace derives exact counts and proves
  no direct DB mutation bypass. Each invocation binds resolved repository/
  interpreter/script/wrapper/env paths, actual timeout launcher argv, exit and
  receipt/catalog association. Repo provenance is exactly `/home/nwm/NWM`,
  `DankerMu/SHUD-NWM`, and the authorization-pinned origin remote-tracking SHA
  with a clean tracked worktree.
- One non-overlapping chronology covers dump/catalog-before, both migration/
  catalog pairs, recovery, compression preflight, dry-run, post-dry selector,
  before benchmark, pre-enforce selector/size, enforce, post-size/catalog,
  after benchmark, cleanup and audit capture. Producer-owned timestamps and
  unique snapshot IDs are mandatory; activity checkpoints stay ordered inside
  each phase.
- Every compression and benchmark connection has a finite connect timeout.
  One benchmark-wide monotonic 900-second deadline begins before connection
  acquisition and bounds every connection, SQL/activity operation and result;
  acquisition is exception-safe and producer-side rows/result bytes/plans are
  capped. The wrapper executes `/usr/bin/timeout --signal=TERM --kill-after=30s
  900s`; the oneshot service uses finite consistent `TimeoutStartSec` while
  preserving manual dry-run default and one service-owned literal `--enforce`.
- Dump validity comes from timed pinned PG15 `pg_restore --list` against the
  retained descriptor, with bounded stdout/stderr hashes, version, exit and
  dump SHA. PostgreSQL 15 and TimescaleDB 2.10 identities come from a pinned
  producer query artifact rather than arbitrary strings.
- The curve is exactly seven days but uses half-open overlap with the selected
  chunk. Starting at the exclusive end or remaining wholly outside fails. A
  qualifying plan matches exact structural `Custom Scan` provider and
  `Relation Name`/`Schema` fields on the same node; substring, Filter, alias
  and child-node decoys fail. Snapshot counts and unique origin/sibling
  relations are bijective, table-owned and cross-table disjoint.

Current source/unit bytes, dump readability, catalog state, and production
query construction are read-only-recapturable. Ordered historical migration
and command execution, prior/final activation state, before-compression plans,
and storage transition chronology are not. Closing task 4.5 therefore needs a
newly authorized controlled replay; the invariant-closure implementation
performs no node-27, retention, node-22, drill, or role mutation.

### Reproducible representative curve/MVT timing

Benchmark only the bound-1 selected hydro chunk that enforce will compress.
Before compression, deterministically freeze one non-empty production-valid
`q_down` request identity in that chunk. The benchmark harness MUST use the
deployed production code rather than a handwritten approximation:

- curve: call `PsycopgForecastStore.forecast_series` with a frozen
  `basin_version_id`, translated public `river_segment_id`,
  `river_network_version_id`, historical `issue_time`, run-type/scenario
  filters and seven-day window. Capture the exact SQL/params actually sent by
  `_fetch_forecast_segment_rows` in `packages/common/forecast_store.py`,
  including the `hydro.hydro_run` join and cycle/run-type/scenario predicates,
  and run EXPLAIN on that captured statement. The source-file SHA and captured
  query-text SHA are evidence.
- MVT: import `services.tiles.mvt.postgis_tile_sql` and execute
  `postgis_tile_sql("hydro")` with the same parameter construction as
  `apps/api/routes/hydro_display.py::_postgis_tile_params`, using a frozen
  `run_id`, basin/network identity, `valid_time`, and deterministic z=9 tile
  containing a non-null source segment. This preserves production source
  identity/property/budget columns, `ST_MakeValid`, simplification and final
  tile row shape. Record the source-file SHA and generated query-text SHA.

Store all bound request/SQL parameters (never credentials). Curve output uses
a documented canonical JSON serialization before sha256; MVT hashes the raw
`bytea`. Before and after row/byte counts and result hashes must be identical.
For each query/phase use a new read-only connection, record the first execution
as informational cold-biased evidence, run two warmups, then seven measured
`EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)` executions in fixed curve/MVT order.
If warmup 2 still has `shared_read_blocks > 0`, allow warmups 3--5. Remaining
reads after warmup 5 classify the phase as `mixed-cache`; before and after must
both have the same classification, otherwise performance comparison is
BLOCKED. Do not flush OS/Postgres caches, restart DB, or use the cold-biased
first execution for the gate.

Median is the fourth value of seven sorted samples. p95 is nearest-rank
`sorted[ceil(0.95*n)-1]`, therefore the seventh/max value for n=7. For each
query, acceptance is both
`after_median_ms <= max(1.50 * before_median_ms, before_median_ms + 100)` and
`after_p95_ms <= max(2.00 * before_p95_ms, before_p95_ms + 250)`. Record all
samples, median/p95, planning/execution time, shared hit/read counts, returned
rows/bytes, cache classification and calculated thresholds. Recursively walk
the TimescaleDB 2.10 JSON plan; after compression it must contain a Custom Scan
whose provider/node is `DecompressChunk` and whose relation/chunk predicate is
bound to the selected original/compressed chunk identity. Otherwise the query
did not exercise changed storage and evidence fails. Sample `pg_stat_activity`
before each phase; material concurrent-load drift blocks PASS.

### Failure, recovery, and truthful rollback

- Preflight/migration/dry-run failure: keep both compression units inactive,
  restore the autopipe timer's prior state, preserve artifacts, and stop.
- Enforce `partial`, selected-scope mismatch, missing post-measurement, or
  query regression: disable/stop the compression timer and preserve the exact
  receipt/catalog state. Chunks already compressed remain compressed; do not
  relabel the run as rolled back and do not rerun enforce.
- Compression has no transactional batch rollback. The schema-only dump does
  not restore row storage and is not recoverability proof. A later, separately
  authorized recovery may invoke
  `decompress_chunk` only on the receipt-proven successfully compressed chunk
  list, one chunk at a time, then re-run catalog, size, result-hash, and query
  checks. Until that completes, the terminal outcome remains failed/partial.
- Migration settings may remain enabled with the timer disabled; reverting
  settings or decompressing chunks is a distinct production mutation and is
  not inferred from task 4.5 authorization.
- The 2026-07-15 evidence replay is separately authorized for one exact
  decompression of `_timescaledb_internal._hyper_3_7_chunk`, followed by one
  bound-1 recompression. Its terminal must preserve both recovery artifacts;
  it may not claim decompression was out of scope or reuse the incomplete
  historical selector/benchmark artifacts.

### Risk packs and non-goals

Core risk packs:

- Public CLI/script entry: selected — manual wrapper remains dry-run default;
  committed service alone adds literal `--enforce`.
- Config/project setup: selected — canonical mode-0600 env, exact lag/bound,
  canonical receipt/lock paths, and installed user units.
- File IO/path safety/overwrite: selected — lock plus runner/live receipts and
  forensic dump are mode-0600, no-follow/atomic where published, and hashed.
- Schema/fields/contracts: selected — reachable `refused_lock` shape and the
  terminal-envelope 2.0 contract with negative semantic/recovery tests.
- Auth/permissions/secrets: selected — live `nhms` is truthfully superuser;
  credentials remain only in the untracked env and never enter argv/evidence.
- Concurrency/shared-state/ordering: selected — autopipe quiescence, DB activity
  and lock gates, flock refusal receipt, exact selected ordering, inactive
  Persistent timer.
- Resource limits/large mutation: selected — bound=1, 8 GiB selected cap,
  300 GiB free-space headroom, 900 s external timeout.
- Legacy compatibility: selected — ordinary dry-run/enforce receipt shapes and
  wrapper argument passthrough remain unchanged while lock behavior is fixed.
- DB migration/DDL: selected — live double-apply-on-success and exact D3 catalog
  proof on TimescaleDB 2.10.2.
- Error/retry/rollback: selected — no automatic retry after a nonzero first
  migration or partial compression; compressed chunks remain truthful state.
- Testing/evidence rigor: selected — independent schema/semantic verifier,
  source/query/result hashes and cache-aware performance gates.
- Documentation/migration notes: selected — runbook gains the exact operator
  sequence and failure boundary.
- Release/dependency compatibility: not selected — no dependency or packaging
  change.

NHMS domain risk packs:

- Geospatial/CRS/basin geometry: selected for evidence only — the production
  MVT benchmark exercises existing PostGIS transforms/tile geometry; no geometry
  or CRS contract changes.
- Hydro-met timeseries/windows: selected — exact terminal chunk ranges and
  production curve/MVT identities must preserve rows and bytes.
- PostGIS/TimescaleDB behavior: selected — compression settings, chunk catalog,
  compressed sibling sizes and `DecompressChunk` plan semantics are central.
- Published/display identity: selected for regression evidence — live display
  SQL/result identity must remain byte-stable; API/frontend code is unchanged.
- SHUD numerical/conservation: not selected — no model execution or numerical
  output mutation.
- Slurm/mock-vs-real lifecycle: not selected — node-22 is untouched.
- External providers/snapshot reproducibility: not selected — no fetch or GRIB
  processing.
- Run manifest/QC provenance: not selected — no run/product manifest changes.

The six-perspective review floor is correctness, DB/catalog integration,
security/secret handling, systemd/concurrency, test/evidence rigor, and
spec/invariant compliance.

Invariant matrix:

| Invariant | Producer / validator / evidence |
|---|---|
| Governing scope | Independent chunk query + dry-run produce one ordered tuple; selector hash and bound=1 gate the sole enforce. |
| Terminal-only | Runner predicate and verifier require strict range-end cutoff, ten-minute drift margin, and no active/lag chunk. |
| Lock contention is observable and mutation-free | Runner atomically publishes `refused_lock`; tests prove empty selections, no DB call, and replacement of stale receipt. |
| Migration truth | First apply must succeed before the idempotency apply; exact catalog JSON is identical and no policy job exists. |
| Ingest/compression exclusion | Autopipe timer stopped, MainPID/process/DB-write/lock gates clear; prior failed service status preserved. |
| Secret/authority truth | Envelope records superuser/owner/function facts but never DSN/password; role/grant mutation flags remain false. |
| Bounded mutation | One selected chunk, <=8 GiB, >=300 GiB free headroom, <=900 s external wall limit. |
| Receipt arithmetic | Independent verifier recomputes selected, deferred, per-table totals, chunk count delta, size delta and hashes. |
| Query identity | Production forecast-store and `postgis_tile_sql("hydro")` source/query hashes fixed; before/after result hashes identical. |
| Performance | Same cache class; seven-sample median/nearest-rank-p95 gates; after plan proves selected compressed chunk via `DecompressChunk`. |
| Timer safety | Committed/installed unit hashes match; timer enabled but inactive; zero service activation in #1069. |
| Failure truth | Partial mutation is immutable failed evidence; no second enforce, auto-decompress, fake rollback, or receipt relabel. |
| Authorized recovery truth | Two distinct artifacts bind one exact compressed-to-decompressed chunk transition, row parity, space, SHA/database/node and chronology before the fresh compression preflight; final selectors/receipts reselect that target. |

Regression rows:

- Flock held with a stale prior receipt. Expected: zero DB/compression calls and
  atomic schema-valid `refused_lock` receipt replacing the stale receipt.
- Manual wrapper without `--enforce`. Expected: dry-run receipt and zero
  `compress_chunk`; systemd service text includes exactly one `--enforce`.
- First migration apply exits nonzero after any partial catalog effect.
  Expected: preserve catalog/exit evidence, no routine second apply or runner.
- First and second successful migration applies. Expected: identical exact D3
  catalog JSON and no compression policy.
- Autopipe service is `failed` with MainPID 0. Expected: preserve failure,
  accept process quiescence after stopping timer, never `reset-failed`.
- Dry-run candidate list with ten eligible chunks and bound=1. Expected: exact
  oldest hydro tuple selected, nine deferred, selector hash stable at enforce.
- Selected chunk exceeds 8 GiB, free headroom falls below 300 GiB, or tuple is
  within ten minutes of cutoff. Expected: refuse before enforce.
- Enforce receipt is schema-valid but partial, has null after bytes, wrong tuple,
  wrong totals, or catalog count mismatch. Expected: terminal FAIL and no retry.
- Bound-1 hydro compression leaves met compressed count at zero. Expected:
  evidence says met settings-only for this batch while still recording both
  tables' before/after size/count rows; it never claims both tables compressed.
- Curve/MVT output hash changes, cache class differs, threshold regresses, or
  after plan lacks selected-chunk `DecompressChunk`. Expected: terminal FAIL.
- `systemctl enable` leaves timer inactive. Expected: PASS installation gate;
  any attempted start/service activation is out-of-scope failure.
- Evidence contains a credential pattern or claims least privilege while
  `rolsuper=true`. Expected: schema/semantic verifier refusal.
- Recovery preflight/receipt is omitted or tampered, carries a stale SHA/wrong
  target, has less than 300 GiB free space, changes row count, or crosses the
  fresh compression-preflight time. Expected: terminal FAIL before PASS.
- Recovery proof is complete but `replay_decompression` or
  `out_of_scope.decompress_run` says false, or either selector/new receipt
  chooses another chunk. Expected: terminal FAIL; no historical “not run” flag
  can hide the authorized mutation.

Out of scope: retention or any `drop_chunks`; retention dry-run/enforce;
archive rebuild drill; product archive or DB-export salvage mutation; any
node-22/Slurm action; TimescaleDB upgrade; v2/star-schema or index changes;
display API/frontend behavior changes; automated `decompress_chunk`; and any
manual deletion. #1071/#1072 and the node-22 epic retain those owners.

## Workflow Fixture: Issue #850 DB-Export Salvage Exporter + Manual Restore Runbook + Live Salvage Run

Fixture level: expanded. Repair intensity: high. Project profile: NHMS.

Change surface:

- `scripts/node27_db_export_salvage.py` (new): reads the archive-completeness receipt (schema `schemas/archive_completeness_receipt.schema.json`), consumes the `salvage_selectors` array verbatim, runs `COPY (SELECT <fixed column list> FROM met.forcing_station_timeseries | hydro.river_timeseries WHERE <PK-scoped selector predicate>) TO STDOUT WITH (FORMAT CSV, HEADER)` per selector, zstd-compresses the CSV, publishes the object plus a per-object `manifest.json` sibling under `NHMS_ARCHIVE_ROOT/db-export/<lane>/<identity>/{data.csv.zst,manifest.json}` (`exports` array length 1 per manifest) via `atomic_write_bytes_no_follow(mode=0o600, require_durable_replace=True)`, and emits a receipt outside the archive root. Reads: `met.forcing_station_timeseries`, `hydro.river_timeseries` via display_ro or explicitly-scoped read role. Writes: filesystem only under `NHMS_ARCHIVE_ROOT/db-export/`. Never runs DDL, never deletes DB rows, never deletes archive objects. `tasks.md §3.1` and §3.2 checkboxes flipped in this PR; §3.3 flipped in the follow-up live-receipt commit.
- `scripts/node27_db_export_salvage_once.sh` (new, 0755): systemd oneshot wrapper — preflight absolute paths + non-symlink + env-file mode 0600, `set -a; . "$ENV_FILE"; set +a; exec uv run python …` per `#849` and `#851` convention.
- `infra/env/node27-db-export-salvage.example` (new): DB URL (display_ro or read-scoped role), `NHMS_ARCHIVE_ROOT`, `NHMS_ARCHIVE_COMPLETENESS_RECEIPT_PATH` (input scope source), receipt output path, lock path, `NODE27_DB_EXPORT_SALVAGE_PER_TICK_BOUND` (per-tick selector bound), `NODE27_DB_EXPORT_SALVAGE_ZSTD_LEVEL` (default `3`, matches sibling raw retention + archive discipline), `NODE27_DB_EXPORT_SALVAGE_STATEMENT_TIMEOUT_MS` (default `300000`, mirrors #851's compress-timeout convention), `NODE27_DB_EXPORT_SALVAGE_SOURCE_INSTANCE_ID` (env-configured literal that stamps `source_database.instance_id` in the manifest, matches the schema example's `"node27-primary-pg15"`). Header explicitly states "no automated restore lane exists — restore is the manual `COPY FROM` procedure documented in the archive runbook".
- `tests/test_node27_db_export_salvage.py` (new): unit tests covering selector-consumption invariants (receipt schema-validated on load, refuse hardcoded selector lists, refuse malformed selectors, idempotency skip on verified existing objects, dry-run isolation, per-selector failure isolation, manifest row-count parity, safe-relative-path enforcement, DSN masking, wrapper 6-case parametrized shell contract, receipt `outcome` enum coverage — `clean` / `partial` / `all_failed` / `refused_lock` / `refused_config` / `refused_role` — and per-table column-list constants pinned by test asserting the SELECT column list matches the migration DDL columns for both hypertables).
- `docs/runbooks/tier-node27-timeseries-storage.md` (edit): new section 3.2 — checksum pre-check + manual `COPY FROM` sequence as the ONLY restore path for salvage objects, explicitly states no automated restore lane exists (ADR 0002 decision 3). Cross-link direction: this PR authors section 3.2 with a forward reference to the retention runbook section 6.2 anchor (owned by #855); this PR does not create section 6.2. When #855 lands section 6.2 it MUST include the reverse link back to 3.2.
- Node-27 live (task 3.3, deferred to a follow-up commit under this same issue per PR body): committed salvage receipt covering every `gap`/salvage selector in the live completeness receipt (expected: forcing before 2026-06-16); per-selector manifest row count equals DB row count at export time; follow-up audit run marks those subjects `complete` and emits an empty salvage list.

Must preserve:

- ADR 0002 decision 3: `db-export` provenance is the sole exception to product-provenance and gets NO automated restore lane. Manual `COPY FROM` runbook is the only restore path.
- Design D6: salvage selectors are audit-derived (from the archive-completeness receipt); hardcoded date lists MUST be refused. The exporter is a downstream consumer of the audit contract, not a scope authority.
- Design D1: the archive is derived from products, not from DB export; salvage is one-time, not steady-state.
- ADR 0001 display carve-out: no display API/frontend/read code path may import the salvage script or reference `NHMS_ARCHIVE_ROOT/db-export/`.
- Salvage manifest schema (`schemas/salvage_manifest.schema.json`) contract from #846 foundation — provenance MUST be the const `"db-export"`, path pattern MUST match `^db-export/(?:[^/]+/)*[^/]+\.csv\.zst$`, sha256 hex-64.
- Existing `nhms_display_ro` role write-refusal semantics — the exporter MUST fail closed if the role can write.
- Existing #846/#849 env-file mode-0600 discipline and `_once.sh` preflight pattern.
- Existing `packages/common/safe_fs.py::atomic_write_bytes_no_follow(require_durable_replace=True)` publication surface — reuse verbatim, do not fork.
- fcntl.flock LOCK_EX|LOCK_NB pattern from `scripts/node27_product_archive.py::acquire_lock` — reuse inline (sibling script convention, matches #851).

Must add/change:

- One receipted, mutation-free, dry-run-default exporter with strict receipt-scoped selector input, mode-0600 atomic receipt publication, per-tick selector bound, safe-relative-path enforcement on all filesystem writes, and structured refusal outcomes.
- One systemd oneshot wrapper and env example matching the #849/#851 shape.
- One runbook section pinning the manual `COPY FROM` restore procedure and stating no automated lane exists, cross-linked from the retention runbook.
- A unit-test suite covering every requirement scenario in `openspec/changes/tier-node27-timeseries-storage/specs/db-export-salvage/spec.md` plus the fixture-mandated regression rows.

Risk packs considered (core):

- Public API / CLI / script entry: selected — new operator script + wrapper + env-file surface + runbook section.
- Config / project setup: selected — env example, receipt scope source, DB role scoping are the configuration surface.
- File IO / path safety / overwrite: selected — new writes under `NHMS_ARCHIVE_ROOT/db-export/…`; safe-relative-path pattern MUST be enforced on every write; receipt publication uses `atomic_write_bytes_no_follow`.
- Schema / columns / units / field names: selected — manifest schema (foundation) MUST NOT be silently reshaped; upstream archive-completeness receipt MUST be schema-validated on load.
- Auth / permissions / secrets: selected — DB role scoping (display_ro or explicit read-only role), DSN masking on all diagnostic surfaces, env-file mode-0600 preflight.
- Concurrency / shared state / ordering: selected — flock LOCK_EX|LOCK_NB; second-invocation contender emits stderr JSON refusal without touching the receipt.
- Resource limits / large input / discovery: selected — per-tick selector bound + statement timeout + zstd level bound production impact of first run.
- Legacy compatibility / examples: selected — no change to mover/audit/compression scripts; sibling regression must show 321+ unchanged.
- Error handling / rollback / partial outputs: selected — per-selector failure MUST NOT corrupt the receipt; verified partially-completed objects MUST be skipped on re-run (idempotency); refusal preserves all state.
- Release / packaging / dependency compatibility: not selected — zstd is already a runtime dependency (via product archive + raw retention); no new Python dependency.
- Documentation / migration notes: selected — runbook section is part of the deliverable.

Domain packs:

- Geospatial / CRS / basin geometry: not selected — no geometry surface.
- Hydro-met time series / forcing windows: selected — salvage covers pre-2026-06-16 forcing gap; forcing_version_id + window selector identity is domain-critical.
- SHUD numerical runtime / conservation / NaN: not selected — no solver or numerical surface.
- PostGIS / TimescaleDB domain behavior: selected — CHUNK-boundary reads MUST NOT bypass the RLS/view boundary or accidentally hit compressed-chunk internals; `SELECT` from the hypertable view + PK-scoped WHERE.
- Slurm production lifecycle / mock-vs-real parity: not selected — node-22 untouched.
- External hydro-met providers / snapshot reproducibility: not selected — salvage is a one-time historical operation, not a provider fetch.
- Run manifest / QC provenance: selected — manifest `provenance: "db-export"` is the permanent producer distinguisher; downstream drill (#854) MUST see this const to route verification correctly.
- Published NHMS artifacts / display identity: not selected — ADR 0001 carve-out preserved.

Invariant Matrix:

- Governing invariant: exporter scope = archive-completeness receipt `salvage_selectors` list verbatim; no other scope source may be invoked; the receipt schema is validated on load and its selector shape is passed through unchanged into the manifest.
- Source-of-truth identity/contract: `archive_completeness_receipt.schema.json` on input side + `salvage_manifest.schema.json` on output side. Provenance const `"db-export"`. Manifest path pattern `^db-export/(?:[^/]+/)*[^/]+\.csv\.zst$`.
- Producers: new `scripts/node27_db_export_salvage.py`. Downstream consumers: archive rebuild drill (#854) verifies salvage objects by sha256 + manifest row-count parity; retention (#855) reads audit-derived `complete` verdicts (not the manifest directly).
- Validators/preflight: receipt schema validation; hardcoded selector-list refusal; env-file mode-0600 preflight; DSN-writable role refusal via `SELECT has_table_privilege(current_user, 'met.forcing_station_timeseries', 'INSERT') OR has_table_privilege(current_user, 'hydro.river_timeseries', 'INSERT')` returning `true` **or** a rolled-back sentinel `INSERT` against either target hypertable succeeding (both must fail closed — the belt-and-braces mirrors that `has_table_privilege` alone can miss column-level GRANTs); safe-relative-path enforcement per manifest write.
- Storage/cache/query: filesystem-only writes under `NHMS_ARCHIVE_ROOT/db-export/`; no DB writes; SELECT reads via `COPY (SELECT …) TO STDOUT` with PK-scoped WHERE per selector.
- Public routes/entrypoints: 1 script + 1 wrapper + 1 env example + 1 runbook section; installation is one-time (not a systemd timer — salvage is a one-time historical operation triggered manually via the wrapper).
- Frontend/downstream consumers: display API/frontend unchanged (ADR 0001 grep must be zero hits); retention gate consumes audit-derived `complete` verdicts, not the manifest.
- Failure paths/rollback/stale state: per-selector failure → skipped in receipt with descriptor `error`, other selectors continue; refusal → stderr JSON refusal + non-zero exit + no receipt touch. Receipt `outcome` enum is one of `clean` (all selectors exported or all verified-skipped), `partial` (at least one per-selector failure AND at least one success), `all_failed` (every selector failed, no success — distinct from `partial` so operators can see at a glance that no selector completed), `refused_lock` (LOCK_EX contention at boot), `refused_config` (env / receipt-file / receipt-schema / hardcoded-list refusal), or `refused_role` (write-privilege preflight tripped by `has_table_privilege` OR rolled-back sentinel INSERT). Idempotent re-run skips selectors whose object exists with matching sha256 + manifest row count.
- Evidence/audit/readiness: dry-run default; enforce writes objects + manifest + receipt atomically; live task 3.3 receipt covers every audit-emitted salvage selector; follow-up audit shows those subjects `complete` and empty salvage list.

Regression rows:

- Input: receipt with two selectors, one already exported (object + manifest present + sha256 verifies + manifest row count matches DB). Expected: only the missing selector is exported; existing object untouched; receipt records both descriptors (one `skipped_verified`, one exported).
- Input: completed enforce export for a selector. Expected: manifest `exported_row_count` equals the DB row count for that selector at export time; per-object sha256 recorded; column list recorded verbatim.
- Input: invocation with a hardcoded selector list flag and no receipt. Expected: refused with structured stderr JSON diagnostic; exit non-zero; no receipt written.
- Input: receipt file missing OR schema-invalid OR `salvage_selectors` array missing/malformed. Expected: fail-closed refusal; no partial export; no receipt touch.
- Input: enforce request with the exporter DSN resolving to a role that can WRITE (`has_table_privilege(current_user, 'met.forcing_station_timeseries' | 'hydro.river_timeseries', 'INSERT')` returns `true` for at least one target, or the rolled-back sentinel `INSERT` succeeds against either target). Expected: refusal (`outcome=refused_role`); no export; no receipt written; stderr JSON refusal captured; test parametrizes both preflight legs so either alone fires the refusal.
- Input: dry-run mode. Expected: no filesystem writes to `NHMS_ARCHIVE_ROOT/db-export/`; receipt is written to receipt path with `mode: "dry-run"`; no `COPY` executed for enforce-only side effects.
- Input: per-selector `COPY` failure mid-run (e.g., statement timeout). Expected: the failing selector's descriptor records `error`; other selectors continue; per_selector_totals arithmetic reflects only the successfully-exported set (evidence-fidelity: no misleading aggregated totals); `outcome=partial` **only when at least one selector succeeded** (mixed failure/success mix); when EVERY selector fails and no selector succeeded, `outcome=all_failed`; exit non-zero in both cases.
- Input: manifest.json write path leaves the archive root via a symlink or `..` traversal (test injects a malicious selector). Expected: `safe_relative_path` refusal via manifest schema validation and independently via runtime path-safety check; no write; refusal descriptor.
- Input: env-file mode is 0644 instead of 0600. Expected: `_once.sh` refuses at preflight; Python entrypoint never starts.
- Input: LOCK_EX|LOCK_NB contention — a second invocation runs while the first holds the lock. Expected: contender emits stderr JSON refusal ("lock held", `outcome=refused_lock`), exits non-zero, does NOT touch the receipt.
- Input: DSN in an exception message routed through the outer diagnostic. Expected: `_mask_dsn` applied before stderr emit; no cleartext password.
- Input: negative jsonschema tests on manifest — drop `provenance`, set `provenance: "product-archive"`, drop `exports[0].object.sha256`, inject unknown top-level key. Expected: `jsonschema.ValidationError` per case.
- Input: negative jsonschema tests on receipt — drop `salvage_selectors`, inject unknown top-level key. Expected: refusal on load.
- Migration idempotency (no DDL): the exporter runs no DDL — assert via test that the runner has zero `ALTER TABLE|CREATE TABLE|DROP TABLE|TRUNCATE|BEGIN|COMMIT|SAVEPOINT` textual occurrences (mirrors #851 migration guard shape).
- ADR 0001 display carve-out: `grep -rn "db_export_salvage\|NODE27_DB_EXPORT_SALVAGE\|db-export/" apps/api apps/frontend` → zero hits.
- Sibling regression: existing archive mover / storage inventory audit / resource governance / raw retention / compression pytest suites remain green with no line changes to their code.

Boundary-surface checklist:

- Shared helper roots reused (not forked): `packages/common/safe_fs.py::atomic_write_bytes_no_follow` (receipt + manifest publication), `fcntl.flock` pattern inline from `scripts/node27_product_archive.py::acquire_lock`, `_parse_positive_int` / `_mask_dsn` convention inline from `scripts/node27_timeseries_compression.py`.
- Public entrypoints added: 1 exporter + 1 wrapper + 1 env example + 1 runbook section.
- Read surfaces: archive-completeness receipt file (schema-validated); `met.forcing_station_timeseries`, `hydro.river_timeseries` via `COPY (SELECT … WHERE PK-scoped) TO STDOUT WITH (FORMAT CSV, HEADER)`; sentinel write preflight against a scratch table (or `SELECT has_table_privilege`).
- Write/delete/overwrite surfaces: per exported selector, one `data.csv.zst` object + one `manifest.json` sibling published under `NHMS_ARCHIVE_ROOT/db-export/<lane>/<identity>/` (one directory per selector, `manifest.exports` length 1); receipt publication outside the archive root. All via `atomic_write_bytes_no_follow` at mode 0600. Zero DB writes. Zero deletes anywhere.
- Staging/publish/rollback surfaces: each individual object or manifest uses a
  same-directory temp + atomic replace via `require_durable_replace=True`, but
  the two-file pair is not transactionally atomic. Object-only, manifest-only,
  checksum/selector mismatch, or uncertain publication is failed evidence and
  provides no audit coverage. Retry may re-export only the same exact selector
  path; a run becomes qualifying only after every pair independently verifies.
  Rollback/recovery is idempotent re-run, not deletion or relabeling.
- Producer/consumer evidence boundaries: input = audit's `salvage_selectors`; output = manifest with `provenance: "db-export"`; downstream drill (#854) verifies salvage objects by sha256 + manifest row-count parity, not by reingest.
- Stale-state/idempotency boundaries: re-run over verified existing objects skips them; a stale receipt (older than audit's next scheduled tick) is not a runtime hazard because the operation is one-time and the audit refresh cadence is already gated in #849.
- Unchanged downstream consumers: display API/frontend/read paths (ADR 0001), archive mover (#848), storage inventory audit (#847), resource governance (#849 extension), raw retention (pre-existing), hypertable compression (#851), write-guard (#852 owns), retention gate (#855 owns). None touched.

### Issue #1070 live task 3.3 closure (2026-07-15)

Issue #1070 closes only the deferred node-27 live task 3.3. The exporter and
manual restore runbook from tasks 3.1/3.2 remain unchanged. Upstream issues
#1066/#1067 and #849 are complete, so the stale issue-body hard-coded-window
fallback is forbidden: design D6 remains authoritative and the live exporter
must consume the audit receipt's selector list verbatim.

- The immutable input baseline is
  `storage-inventory-audit/completeness-incomplete-live-20260713T155314Z.json`
  at SHA-256
  `e2d4f08150943f09af87d3e53e79cff26728fb438aabb545dabff07842497d04`.
  It has `outcome=incomplete`, exactly 228 selectors, all for
  `met.forcing_station_timeseries`, and normalized selector-set SHA-256
  `ad5da1c51e1e90ec7bf2912d204186d21879be4e69536cc24a469520a486d0c6`.
  Before any runner invocation, install those committed bytes mode `0600` at a
  task-specific path outside every timer's write target (for example
  `/home/nwm/node27-db-export-salvage-inputs/completeness-incomplete-live-20260713T155314Z.json`).
  Dry-run and enforce use only that frozen path. Verify the full receipt hash
  before and after both invocations. The normalized selector-set hash algorithm
  is exactly `jq -cS '.salvage_selectors' <receipt>` (including jq's trailing
  newline) piped to SHA-256. The dry-run/enforce terminal envelope also records
  the ordered selected-descriptor SHA-256. Selector injection, omission,
  reordering, live timer-path replacement, or a hard-coded list blocks
  execution.
- The role guard is authoritative. The live env uses `nhms_display_ro` (or an
  equivalently explicit read-only role) only after proving SELECT access to
  both target hypertables and proving INSERT/UPDATE/DELETE refusal. A writer
  DSN is never an operational workaround. The mode-0600 env and captured
  evidence must redact the DSN password.
- Because the runner slices the input list to `per_tick_bound` and does not
  emit a deferred-selector list, the qualifying dry-run and enforce receipt
  both use `per_tick_bound=228` and must contain all 228 baseline selectors in
  exact order. The default bound 32 cannot satisfy task 3.3. This count increase
  does not weaken resource controls. Before enforce, a separate read-only,
  streaming preflight executes the same fixed-column COPY query and exact
  selector predicate while discarding chunks after counting them; it records
  exact CSV bytes, row count, and elapsed time per selector without buffering a
  selector or publishing an object. Every row count must be `>0`. The live env
  tightens `NODE27_DB_EXPORT_SALVAGE_MAX_SELECTOR_BYTES` to 512 MiB. Enforce is
  blocked unless: every exact streamed selector size is below that cap;
  `4 * max_selector_bytes_observed` is below the smaller of host
  `MemAvailable` and any finite remaining cgroup memory; the sum of uncompressed
  CSV bytes is no more than `free_bytes - 322122547200` (preserving the 300 GiB
  archive warning headroom even under a no-compression bound); and
  `2 * observed_preflight_seconds + 228 * 5 seconds <= 4 hours`. The wrapper is
  then bounded by an external four-hour timeout. The existing per-selector
  statement timeout remains active. If the exact streaming preflight cannot
  establish these bounds, first replace the buffered exporter with a streaming
  hard-limit implementation in a separate blocker issue/PR; do not run live.
- The dry-run must be `outcome=clean`, contain 228 descriptors with no `error`,
  and write no `db-export` object. Enforce is the only authorized archive-side
  mutation; it may publish only the 228 selector-derived `data.csv.zst` and
  manifest pairs plus its receipt. DB mutation, hard-coded scope, object
  deletion, automated restore, compression, drill, retention, timer enablement,
  and node-22 activity remain out of scope.
- A qualifying enforce receipt has `outcome=clean`, covers all 228 selectors,
  contains only `exported` or `skipped_verified` states, and has zero errors.
  Every descriptor's exported/preflight row count must be `>0`; a zero-row
  selector is not salvage and blocks publication/closure. Every committed
  object is re-verified against the manifest schema, SHA-256, selector
  identity, fixed columns, and exported row count. Because object and manifest
  publication are individually atomic but not pair-atomic, object-only,
  manifest-only, mismatch, and uncertain pairs remain failed evidence and
  provide no completeness coverage. A partial or
  all-failed attempt is immutable failed evidence and cannot be relabeled; an
  idempotent retry may close only the remaining selectors and the final
  qualifying receipt must still enumerate all 228.
- After object verification, run a fresh storage inventory audit. Task 3.3
  closes only when every baseline selector has verified `db-export` coverage,
  the follow-up receipt has an empty `salvage_selectors` list, and all formerly
  salvageable forcing gaps are `complete`. A zero-row manifest must never
  provide coverage; if the live preflight finds a zero-row selector, stop and
  fix both exporter publication and audit coverage semantics in a blocker PR
  before resuming. Any remaining non-salvageable gap keeps the receipt truthful
  and retention fail-closed; it must not be hidden to manufacture a global
  `complete` outcome.
- Commit the input baseline reference, role/row-count/free-space preflight,
  dry-run, enforce, object post-verification, follow-up completeness audit, and
  a terminal envelope tied to the exact deployed Git SHA. Secrets and the live
  env file remain uncommitted.

Live closure invariants:

- Governing invariant: input selector-set identity is byte- and set-bound to
  the accepted audit baseline; output coverage is one verified DB-export pair
  per selector, with no DB write capability.
- Failure/rollback: any role ambiguity, selector drift, dry-run error,
  zero-row selector, per-selector oversize/timeout, total memory/disk/wall-clock
  budget failure, partial pair publication, checksum/row-count
  mismatch, or follow-up audit gap blocks closure. Published verified salvage
  is additive and retained for idempotent retry; no cleanup is used to convert
  failed evidence into PASS.
- Evidence boundary: #1070 owns salvage plus the follow-up audit only. #1069
  retains compression ownership, #1071 owns drill/retention dry-run, and #1072
  retains the separately authorized irreversible retention enforce.

## Workflow Fixture: Issue #852 Fail-Closed Compressed-Chunk Write Guard

Fixture level: expanded. Repair intensity: high. Project profile: NHMS.

**High-risk-surface note**: This PR touches THREE production write paths (`workers/output_parser/parser.py`, `workers/forcing_producer/store.py`, `packages/common/forcing_domain_handoff_apply.py`). The golden-path ingest must remain byte-identical for uncompressed chunks; the guard MUST NOT slow the hot path by more than an amortized ~1 ms per batch; the guard MUST NOT cause false positives (blocking a legitimate uncompressed write); the guard MUST fail closed when the catalog lookup itself errors (network hiccup, catalog view unavailable). Six-reviewer escalation is mandated by the production-write-path risk.

Change surface:

- `packages/common/timescale_write_guard.py` (new, single shared helper): `check_batch_targets_uncompressed(cursor, *, hypertable_schema, hypertable_name, valid_time_min, valid_time_max)` runs one catalog lookup against `timescaledb_information.chunks WHERE hypertable_schema=%s AND hypertable_name=%s AND is_compressed = true AND range_start <= %s AND range_end > %s` (batch-time-range OVERLAP semantics: `range_start <= batch_max AND range_end > batch_min`, note TimescaleDB chunk intervals are `[range_start, range_end)` — `range_start` INCLUSIVE, `range_end` EXCLUSIVE, per #851). Before the catalog query, run `SET LOCAL statement_timeout = '5s'` (transaction-scoped, no session leak). If any compressed chunk overlaps, raise `CompressedChunkWriteError(chunk_schema, chunk_name, hypertable, decompress_runbook_anchor)` naming the chunk and pointing at the runbook decompress procedure. If the catalog lookup itself errors (exception), fail-closed: raise `CompressedChunkGuardError(reason)` — the guard NEVER silently permits a write. The guard also refuses at entry (before any SQL) any `(hypertable_schema, hypertable_name)` pair NOT in `HYPERTABLES_GUARDED` (runtime enforcement so a wire-site typo cannot silently permit writes).
- **Guard-precedes-DELETE ordering (critical)**: All three write paths do `DELETE FROM <table> WHERE <identity clause>` FOLLOWED BY `INSERT INTO … execute_values(…)`. Placing the guard between DELETE and INSERT is INSUFFICIENT because TimescaleDB 2.10 rejects DELETE on compressed chunks with its own raw error before the guard would fire. The guard MUST run BEFORE the DELETE at every wire point.
- **Guard semantic scope (batch-time-range only)**: The guard checks compressed chunks overlapping `[min(batch.valid_time), max(batch.valid_time)]`. Identity-scoped DELETE can still hit compressed chunks OUTSIDE the batch time window if the identity has historical data older than the batch (rare edge case in normal reingest, which rewrites the same time window as its source cycle). That residual case falls through to TimescaleDB's raw error rather than the guard's structured error — this is a documented non-goal for #852; the runbook section 4.2 ("Residual reingest window mismatch") covers it explicitly. Adding an identity-existence probe was rejected as too expensive (would require a `MIN(valid_time)` scan against potentially-compressed data) and too broad (blocking every reingest when any chunk of the hypertable is compressed would break the golden path for all forcing_version_ids).
- `workers/output_parser/parser.py`: wire the guard at the pre-write moment inside `upsert_river_timeseries` (`parser.py:645`) **BEFORE** the identity-scoped DELETE loop that starts around `parser.py:655`. Compute `min/max valid_time` from the batch; call `check_batch_targets_uncompressed(..., "hydro", "river_timeseries", min, max)`. Cursor sourcing: use the fresh cursor the surrounding `_fetch_all` / `_execute_values` methods already open (do NOT introduce a new cursor lifecycle). If the parser's per-op cursor pattern requires a dedicated guard cursor, use the same connection/transaction as the DELETE that follows.
- `workers/forcing_producer/store.py`: wire at `store.py:749` in the pre-write moment of `_replace_values(...)` so the guard runs BEFORE the DELETE. Compute `min/max valid_time` from the batch; call the guard on `("met", "forcing_station_timeseries", min, max)`. Implementation deviation (see "Implementation deviations" subsection below): the guard call is passed via the new `pre_write_cursor_hook` parameter on `_replace_values`, not injected literally before the call.
- `packages/common/forcing_domain_handoff_apply.py`: wire at `forcing_domain_handoff_apply.py:693` BEFORE the DELETE at line 694 inside `_replace_forcing_station_timeseries`. Same guard call.
- `tests/test_timescale_write_guard.py` (new): shared helper unit tests — allow / block / catalog-error / boundary overlap semantics / statement-timeout / empty-batch skip / DSN mask.
- `tests/test_write_guard_output_parser.py` (new OR extend `tests/test_output_parser.py` OR extend the existing `workers/output_parser` test file — pick whichever fits the current test layout; do not fork test infrastructure): one wired-path test per write path (3 total) covering (a) compressed chunk overlap raises `CompressedChunkWriteError` BEFORE `execute_values` is invoked; (b) uncompressed batch passes through unchanged.
- `docs/runbooks/tier-node27-timeseries-storage.md` (edit): new section 4.3 — `decompress_chunk(<chunk>::regclass)` procedure + reingest re-run guidance + explicit anchor referenced by the guard error message.
- `openspec/changes/tier-node27-timeseries-storage/tasks.md` §4.3 checkbox ticked.

Must preserve:

- **Golden-path ingest behavior for uncompressed chunks byte-identical** — no test in the existing 372-test sibling regression suite (product_archive / storage_inventory_audit / resource_governance / raw_retention / timeseries_compression) may fail; existing workers/* test suites must remain green; existing hydro/met table row-count and shape invariants unchanged.
- Design D5: the guard is centralized in ONE shared helper. Divergent per-path copies are forbidden. All three call sites import from the SAME module.
- Design D5 exemption: the archive rebuild drill (#854) writes to an isolated staging schema and MUST NOT trip the guard — the guard is bound to specific `hypertable_schema`/`hypertable_name` values (`hydro.river_timeseries`, `met.forcing_station_timeseries`); staging schema is another schema entirely.
- Spec Requirement "Reingest fails closed on compressed chunks": abort BEFORE any row mutation; error names the chunk and references the runbook.
- ADR 0001 display carve-out: no display API/frontend code path may import the new helper.
- ADR 0002 decision 3: no automated restore lane; the runbook decompress procedure is the manual escape hatch.
- Existing psycopg2 patterns at each call site (`execute_values`, cursor discipline, transaction boundaries).
- `_mask_dsn` convention if any DSN surfaces through error messages.

Must add/change:

- One shared pre-write helper detecting compressed-chunk overlap for a batch time window.
- Three call-site wirings (minimal diff, one guard call each, positioned strictly before the `execute_values` call).
- Runbook section 4.3 pinning the decompress procedure with a stable anchor.
- Unit tests: shared helper allow/block/error paths; one wired-path test per write path (3 total).

Risk packs considered (core):

- Public API / CLI / script entry: not selected — no new operator surface.
- Config / project setup: not selected — no new env vars; guard uses the existing DB connection.
- File IO / path safety / overwrite: not selected — no filesystem writes.
- Schema / columns / units / field names: selected — helper queries `timescaledb_information.chunks` catalog; new exception types are a public contract for the three call sites.
- Auth / permissions / secrets: selected — helper runs under the ingest role's privileges; no new secret surface; DSN in exception must be masked if reached.
- **Concurrency / shared state / ordering: selected** — guard runs inside the same transaction as `execute_values`; if the guard commits to abort, the transaction rolls back (no partial write). Verify no `SET SESSION` state leaks across calls.
- **Resource limits / large input / discovery: selected** — catalog lookup runs once per batch (amortized over batch_size rows); statement timeout bounds latency.
- Legacy compatibility / examples: selected — 372-test sibling regression + existing workers/* tests must remain green.
- **Error handling / rollback / partial outputs: selected** — the guard's raise MUST propagate before `execute_values`; on catalog error, fail-closed (raise `CompressedChunkGuardError`), never silently permit.
- Release / packaging / dependency compatibility: not selected — no new dependency.
- Documentation / migration notes: selected — runbook section 4.3 is a deliverable.

Domain packs:

- Geospatial / CRS / basin geometry: not selected.
- **Hydro-met time series / forcing windows: selected** — helper reasons about `valid_time` overlap against chunk `range_start`/`range_end`; timezone handling MUST match ingest's UTC discipline.
- SHUD numerical runtime: not selected.
- **PostGIS / TimescaleDB domain behavior: selected** — `timescaledb_information.chunks` semantics; `range_end` is exclusive (per #851 fixture note); on TimescaleDB 2.10 catalog visibility considerations.
- Slurm production lifecycle: not selected.
- External hydro-met providers: not selected.
- Run manifest / QC provenance: not selected.
- Published NHMS artifacts / display identity: not selected — ADR 0001 preserved.

Invariant Matrix:

- Governing invariant: any batch whose `[min(valid_time), max(valid_time)]` overlaps ANY compressed chunk of the target hypertable MUST be rejected before `execute_values` runs; a batch that touches ZERO compressed chunks MUST proceed unchanged; a catalog lookup failure MUST fail-closed (raise, do not permit).
- Source-of-truth identity/contract: `timescaledb_information.chunks.is_compressed` boolean + `range_start`/`range_end` interval. Caller-observable contract has TWO surfaces:
  - Exception TYPES: `CompressedChunkWriteError` (compressed chunk detected) and its base `CompressedChunkGuardError` (catalog lookup failure OR unregistered `(schema, table)` OR partial batch range).
  - Wire-format string CODES emitted downstream (one per caller-observable route; every code routes to runbook §4.3 decompress procedure):
    - `HANDOFF_APPLY_COMPRESSED_CHUNK_BLOCKED` — attached to `apply_forcing_domain_handoff` `unavailable_report.unavailable_reasons[].code`.
    - `OUTPUT_PARSE_COMPRESSED_CHUNK_BLOCKED` — stamped on `hydro.hydro_run.error_code` by parser + emitted as parser CLI stderr prefix.
    - `FORCING_PRODUCE_COMPRESSED_CHUNK_BLOCKED` — forcing producer CLI stderr prefix.
    - `FORCING_COMPRESSED_CHUNK_BLOCKED` — stamped on `met.forecast_cycle.error_code` by `ForcingProducer._mark_failed` on the dedicated arm.
- Runtime registry enforcement (caller-observable): `HYPERTABLES_GUARDED = frozenset({("hydro", "river_timeseries"), ("met", "forcing_station_timeseries")})` — any other `(schema, table)` pair passed to the guard raises `CompressedChunkGuardError` BEFORE any SQL runs, so a wire-site typo cannot silently permit writes.
- Producers: extended `workers/output_parser/parser.py::upsert_river_timeseries`; extended `workers/forcing_producer/store.py` forcing-timeseries write; extended `packages/common/forcing_domain_handoff_apply.py` forcing-timeseries write.
- Validators/preflight: `check_batch_targets_uncompressed(cursor, ...)` before every `execute_values` for the two target hypertables; batch-empty short-circuit (no query when 0 rows to write).
- Storage/cache/query: guard runs one catalog lookup per batch; NO caching (a stale cache would enable a silent partial write).
- Public routes/entrypoints: no new operator entrypoints; call sites are the three existing ingest paths.
- Frontend/downstream consumers: display API/frontend unchanged (ADR 0001 grep zero hits); drill (#854) untouched because it writes to a staging schema, not `hydro.river_timeseries` or `met.forcing_station_timeseries`.
- Failure paths/rollback/stale state: exception → transaction rollback via the caller's existing psycopg2 `with connection:` block; no partial write ever; on catalog error, exception raised, no permit-and-warn.
- Evidence/audit/readiness: guard exception message names the specific chunk (`_timescaledb_internal._hyper_<N>_<M>_chunk`) and points at the runbook section 4.3 anchor; error type stable for downstream monitoring.

Regression rows:

- Input: batch whose `valid_time` range fully falls inside a compressed chunk. Expected: `CompressedChunkWriteError` raised BEFORE the DELETE runs (thus before any `execute_values`); error message contains the chunk name; error message contains the runbook anchor `docs/runbooks/tier-node27-timeseries-storage.md#43-decompress-procedure`.
- Input: batch whose `valid_time` range partially overlaps a compressed chunk. Expected: same as above (any overlap is a block).
- Input: batch whose `valid_time` range is fully outside any compressed chunk. Expected: DELETE + `execute_values` invoked once with the batch unchanged; behavior byte-identical to pre-guard code path.
- Input: batch whose `valid_time` range touches the boundary of a compressed chunk (equal to `range_end` exclusive). Expected: allowed (does not overlap — `range_end` is exclusive per #851).
- **Guard-precedes-DELETE regression** (test row): assert via a callable-spy fake connection that at each of the three write paths the guard function is called BEFORE any DELETE cursor.execute, using an ordered call log; if the guard raises, no DELETE cursor.execute is invoked at all.
- **Identity-scoped compressed rows outside batch time window** (residual non-goal doc): batch `valid_time` range is fully outside compressed chunks BUT the identity has historical data in compressed chunks outside the batch window. Expected: the guard passes (batch-time-range semantic); the subsequent DELETE raises TimescaleDB's raw error; this is a documented residual per runbook §4.2 "Residual reingest window mismatch". Test asserts guard does not raise for this case; TimescaleDB raw-error handling is out of guard scope.
- Input: catalog lookup query raises (`OperationalError`, `QueryCanceled`). Expected: `CompressedChunkGuardError` raised; caller transaction rolls back; NO batch write occurs.
- Input: empty batch (`len(rows) == 0`). Expected: guard short-circuits (no catalog query), existing caller behavior unchanged.
- Input: guard called with `hypertable_schema="ops"` (e.g. drill's isolated staging schema). Expected: because the wiring at the three production call sites only passes the two production `(schema, table)` pairs, the drill's writes never reach the guard — asserted by test that greps the wired calls' arguments.
- **`SET LOCAL statement_timeout` non-leak** (test row): after the guard runs on a fresh cursor, subsequent statements on the same session are NOT affected by the guard's short timeout (SET LOCAL is transaction-scoped). Test verifies via a fake cursor that only `SET LOCAL` and not `SET SESSION` is used.
- Input: guard exception message contains a DSN accidentally embedded. Expected: `_mask_dsn` applied (defense-in-depth); test enforces no cleartext DSN.
- Input: existing `workers/output_parser` tests, `workers/forcing_producer` tests, and `packages/common/forcing_domain_handoff_apply` tests run at head with the guard wired. Expected: all previously-passing tests remain passing (no false-positive block on uncompressed windows).
- Input: sibling regression pytest (`test_node27_product_archive.py` + `test_node27_storage_inventory_audit.py` + `test_node27_resource_governance.py` + `test_node27_raw_retention.py` + `test_node27_timeseries_compression.py` + `test_node27_db_export_salvage.py`). Expected: all pass (baseline established at HEAD before wiring; the count is captured in the PR body evidence block, not pinned in the fixture).
- Input: `grep -rn "timescale_write_guard\|CompressedChunkWriteError\|CompressedChunkGuardError" apps/api apps/frontend packages/common | grep -v timescale_write_guard.py`. Expected: zero hits (ADR 0001) — extended to include `packages/common/` to catch a future cross-import that would re-expose the guard on the display side.
- Input: divergent per-path guard implementation drift — search for any second `timescaledb_information.chunks` chunk-lookup in workers/*.py or packages/common/*.py that isn't the shared helper. Expected: only one implementation exists (the new module).
- Input: no DDL added (`ALTER TABLE|CREATE TABLE|DROP TABLE|TRUNCATE`). Expected: verified by grep.
- **`db/seeds/seed_demo.py` intentional non-wiring** (test row): seed_demo populates fresh empty databases from a known-good demo state; it never targets compressed chunks in production; not wired to the guard. Test asserts seed_demo does NOT import `timescale_write_guard` (grep) and this is called out in seed_demo docstring as intentional.
- **Partial-None batch range fails closed (AND-vs-OR semantic)** (primary regression row): Input: guard called with `(valid_time_min=None, valid_time_max=<t>)` or `(valid_time_min=<t>, valid_time_max=None)`. Expected: `CompressedChunkGuardError` raised BEFORE any SQL — the empty-batch short-circuit uses AND (both endpoints `None`), not OR (either endpoint `None`), so a partial `None` (caller bug) never permits a silent write. Callers producing `(None, None)` via `min(..., default=None)` on an empty iterable naturally short-circuit; every other `None` shape fails closed. Tested by `test_partial_none_range_refuses`.
- **`(schema, table)` registry enforcement at guard entry** (primary regression row): Input: guard called with any `(hypertable_schema, hypertable_name)` NOT in `HYPERTABLES_GUARDED` (e.g. `("hydro", "not_a_real_table")`). Expected: `CompressedChunkGuardError` raised BEFORE any SQL runs; error message contains `"unregistered"`. This is a caller-observable behavior (a wire-site typo cannot silently permit writes). Tested by `test_unknown_pair_refuses_at_guard_entry` and `test_partial_range_refuses_before_registry_check`.

Boundary-surface checklist:

- Shared helper roots reused (not forked): existing psycopg2 patterns at the three call sites; `_mask_dsn` convention inline; NO reliance on `packages/common/safe_fs.py` (no filesystem writes).
- Public entrypoints added: 0 (helper is a module-private-per-caller function; no CLI, no systemd unit, no env var).
- Read surfaces: `timescaledb_information.chunks` (catalog view); no new hypertable row reads.
- Write/delete/overwrite surfaces: 0 new. The guard is a PRE-write validator; it does not itself write.
- Staging/publish/rollback surfaces: caller's existing psycopg2 transaction; guard raise causes rollback via existing `with connection:` block.
- Producer/consumer evidence boundaries: guard exception type is the sole caller-observable contract; no new receipt.
- Stale-state/idempotency boundaries: NO cache. Catalog query runs per batch; the correctness invariant would break under caching.
- Unchanged downstream consumers: display API/frontend/read paths (ADR 0001), archive mover / audit / governance / retention / compression / salvage scripts (untouched), drill (#854 staging-schema exempt), retention gate (#855 owns).

### Implementation deviations

The delivered PR (#1058, feat/issue-852-write-guard) departs from the fixture text above in three shape-preserving ways. Each is fully recovered by the wired-path tests and the shared-helper unit tests; none weaken the invariant matrix.

- **`pre_write_cursor_hook` on `_replace_values` (forcing_producer/store.py)** — The design directed "wire BEFORE `_replace_values(...)` call" as a literal source-order injection. The wire actually threads the guard through `_replace_values` via a new `pre_write_cursor_hook: Callable[[cursor], None] | None` keyword. This preserves the "guard runs on the same cursor as the DELETE, in the same transaction" invariant more strictly than an external call would (which would need to either open its own cursor or plumb one through). The wired test `test_forcing_producer_guard_runs_before_delete_on_uncompressed_batch` asserts execution ordering by inspecting the `_RecordingConnection` execution log — the shape check is byte-identical to the "literal before the call" formulation.
- **`SET LOCAL statement_timeout = DEFAULT` reset in `finally:`** — The design directed a plain "reset after the catalog lookup" sequence. The implementation moves the reset into a `finally:` block so the session default is restored even when the catalog SELECT raises. Rationale: if the SELECT raises, the plain sequence leaves the caller's transaction still bound to the guard's 5s cap, silently clipping downstream DELETE + INSERT statements. The `finally:` reset is best-effort (suppressed if it itself raises against an aborted transaction). Unit-tested by `test_set_local_default_resets_even_when_select_raises`.
- **`.large-file-guard.json` exclude-list extension** — The design did not touch large-file plumbing. The PR extended the large-file guard's exclude list for four files total (initial: `packages/common/forcing_domain_handoff_apply.py`, `workers/output_parser/parser.py`; R2/F1 additions: `workers/forcing_producer/producer.py`, `tests/test_forcing_producer.py` — both were pre-existing large files that the R2 fix edited to add the dedicated `except CompressedChunkGuardError` arm + regression test). Verified as documented exceptions, not bypasses; orthogonal to the guard semantics; does not affect the invariant matrix. Recorded here for evidence completeness.

Caller-observable SHAPE asymmetry across the three-plus-one paths (design cheatsheet — the "why does each path have a different error shape" answer):

- **`packages/common/forcing_domain_handoff_apply.py::apply_forcing_domain_handoff` — dict return.** The apply helper returns a structured `unavailable_report` dict (`status="failed"`, `unavailable_reasons=[{"code": "HANDOFF_APPLY_COMPRESSED_CHUNK_BLOCKED", ...}]`); the guard exception is caught inside the helper and translated into the report. Callers already inspect this dict shape; adding a raise would break the contract.
- **`workers/output_parser/parser.py::OutputParser.parse_run` — re-raise + DB `error_code`.** The parser stamps `hydro.hydro_run.error_code = "OUTPUT_PARSE_COMPRESSED_CHUNK_BLOCKED"` (via `_mark_run_failed_preserving_error`) AND re-raises the `CompressedChunkGuardError` un-wrapped. The DB column feeds the operator dashboard; the re-raise feeds the CLI's `OUTPUT_PARSE_COMPRESSED_CHUNK_BLOCKED:` stderr prefix.
- **`workers/forcing_producer/producer.py::ForcingProducer.produce` — re-raise + DB `error_code` (post-R2/F1).** Same shape as the parser: stamps `met.forecast_cycle.error_code = "FORCING_COMPRESSED_CHUNK_BLOCKED"` (via `_mark_failed(..., error_code=)`) AND re-raises the `CompressedChunkGuardError` un-wrapped so the forcing CLI's `FORCING_PRODUCE_COMPRESSED_CHUNK_BLOCKED:` stderr prefix is reachable.
- **`packages/common/timescale_write_guard.py::check_batch_targets_uncompressed` (helper) — raise only.** No dict return, no DB write, no CLI concern. The helper raises `CompressedChunkGuardError` / `CompressedChunkWriteError` and the caller decides the shape.

A future 4th write path MUST pick a shape by matching its use case (report-return vs. raise-plus-DB-stamp vs. bare raise), NOT at random. The wire-site invariant (see below) catches an unwired 4th path; picking the wrong shape is a design decision each new caller documents in its own module docstring.

Additional post-review corrections applied on top of head `830218fb` (R1 fix pass):

- **Boundary predicate `range_start <= %s`** — The catalog query is `range_start <= batch_max AND range_end > batch_min`. TimescaleDB chunks are `[range_start, range_end)`; the earlier `range_start < %s` missed the boundary case where a batch's max valid_time equals a compressed chunk's range_start (in which case the INSERT lands inside that chunk). Regression-tested by `test_boundary_range_start_inclusive_blocks_write` with a predicate-aware fake cursor. The PRIMARY design surface prose above (line ~605 area) now reads `range_start <= %s` — the earlier correction bullet is no longer needed.
- **`HYPERTABLES_GUARDED` runtime enforcement** — The guard now refuses any `(hypertable_schema, hypertable_name)` not in `HYPERTABLES_GUARDED` before any SQL runs, so a wire-site typo cannot silently permit a write. Unit-tested by `test_unknown_pair_refuses_at_guard_entry` and `test_partial_range_refuses_before_registry_check` (the ordering-lock test that asserts partial-None fires BEFORE the registry check).
- **Wire-format string codes for the caller-observable contract (four total)** — The compressed-chunk write guard surfaces via a dedicated caller-observable contract at all four routes (three wire sites plus the forcing CLI). Explicit string values (routed on by operators via the runbook §4.3 triage table):
  - `"HANDOFF_APPLY_COMPRESSED_CHUNK_BLOCKED"` — `REASON_APPLY_COMPRESSED_CHUNK_BLOCKED` on the apply report.
  - `"OUTPUT_PARSE_COMPRESSED_CHUNK_BLOCKED"` — `hydro.hydro_run.error_code` + parser CLI stderr prefix.
  - `"FORCING_PRODUCE_COMPRESSED_CHUNK_BLOCKED"` — forcing producer CLI stderr prefix.
  - `"FORCING_COMPRESSED_CHUNK_BLOCKED"` — `met.forecast_cycle.error_code` set by `ForcingProducer._mark_failed` on the dedicated `except CompressedChunkGuardError` arm (R2/F1).
  Tested by `test_compressed_chunk_write_error_produces_dedicated_reason_code`, `test_compressed_chunk_guard_error_sets_dedicated_error_code`, `test_compressed_chunk_guard_error_sets_dedicated_forcing_error_code`, `test_forcing_cli_emits_compressed_chunk_blocked_prefix_and_exit_1`, `test_output_parser_cli_emits_compressed_chunk_blocked_prefix_and_exit_1`.
- **Empty-batch AND semantics** — `if valid_time_min is None and valid_time_max is None` (not `or`), with a partial-`None` fail-closed branch that raises `CompressedChunkGuardError`. Callers that use `min(..., default=None)` on an empty iterable naturally produce `(None, None)` and short-circuit; a partial `None` indicates a caller bug. Tested by `test_partial_none_range_refuses`.
- **AST-scan wire-site invariant** — `tests/test_timescale_write_guard_wire_site_invariant.py` enforces "every module in workers/**, packages/common/**, scripts/**, db/** that DELETEs from a guarded hypertable MUST also call `check_batch_targets_uncompressed` in the same enclosing function" — parametrized over `sorted(HYPERTABLES_GUARDED)` — so a fourth wire site cannot slip in unwired. Also asserts `pre_write_cursor_hook=` in `workers/forcing_producer/store.py::replace_forcing_timeseries` is bound to `_guard` (not `None`), locking the silent-disable scenario.
- **Constant rename** — `_STATEMENT_TIMEOUT_MS` renamed to `_STATEMENT_TIMEOUT_LITERAL` (the value is a Postgres duration literal `'5s'`, not a millisecond integer). Closes MINOR C-cor-2.



## Workflow Fixture: Issue #854 Archive Rebuild Drill

Fixture level `expanded` · Repair intensity `high` · NHMS project profile · Reuses the shared change (`tier-node27-timeseries-storage`); no new capability required. Task scope: §5.1 (drill script + fixture unit tests). §5.2 (live PASS receipt on node-27) is a follow-up commit under the same issue.

### Pre-implementation hazards resolved (Phase 0.5)

The Phase 0.5 fixture review surfaced four hazards. Resolutions pinned here so Phase 1 implementation does not drift.

**H1 — Ingest schema is hardcoded (CONFIRMED).**
`workers/output_parser/parser.py::PsycopgOutputParserRepository` accepts only `database_url`; every SQL literal is `hydro.`/`met.`/`core.`/`ops.`-qualified. Same for `packages/common/forcing_domain_handoff_apply.py` (all `met.`-qualified) and `HYPERTABLES_GUARDED = frozenset({("hydro","river_timeseries"), ("met","forcing_station_timeseries")})` (name-matched, not DB-matched).

Consequence: same-DB-different-Postgres-schema isolation is NOT achievable without forking every ingest SQL literal. The only viable isolation is a **separate physical Postgres database** with the standard `core`/`met`/`hydro`/`ops`/`map` schemas provisioned via `apply_migrations_from_zero`. The compressed-chunk write guard stays silent in staging because staging has no compression enabled (guard fires on `is_compressed = true`, never matches in a fresh-migrated DB) — isolation is by-DB and by-data-state, not by-schema-namespace.

**H2 — Receipt `staging_database` field semantics (CONFIRMED, example fixed).**
`schemas/archive_rebuild_drill_receipt.schema.json` requires `staging_database{database, schema, instance_id}` (three free strings, no coupling). The prior example JSON set `"database": "nhms"` which literally implies same-DB-different-schema — unrealizable per H1. Fixed by updating `schemas/examples/archive_rebuild_drill_receipt.example.json` to `"database": "nhms_archive_drill_20260711"`.

Canonical field semantics (pinned for implementer):

- `staging_database.database` = isolated physical Postgres database name; MUST NOT equal the production database name. Drill refuses to run if identical.
- `staging_database.schema` = semantic drill-run label (NOT a Postgres CREATE SCHEMA). The isolated DB actually contains all five canonical schemas `core/met/hydro/ops/map`; this field is a run-tag for the receipt (e.g., `archive_drill_20260711_forcing_gfs`).
- `staging_database.instance_id` = cluster/host identifier (e.g., `node27-primary-pg15`).

**H3 — Missing extract-to-disk helper (PARTIAL).**
`scripts/node27_product_archive.py::_decompressed_tar_stream` + `verify_archive_pair` decompress + iterate for checksum only; no extract-to-disk symbol. The drill implements a `_extract_archive_to_disk(manifest, tar_zst_path, dest_dir)` helper (~50-100 LOC) that reuses `_decompressed_tar_stream` as the read primitive, applies bounded per-file (`MAX_FILE_BYTES`) + per-tree (`MAX_TREE_ENTRIES`) + per-source (`MAX_SOURCE_BYTES`) limits symmetric with the mover's guards, verifies each file's sha256 against the manifest as it writes, and refuses any path escape (`..`, absolute paths, symlink targets).

**H4 — Registry closure NOT synthesizable from manifest alone (CONFIRMED).**
`OutputParser.parse_run` requires: `hydro.hydro_run` → `core.model_instance` → (`core.mesh_version`, `core.river_network_version` → `core.river_segment` × N) + `met.data_source` → `met.forecast_cycle` + `met.forcing_version` + `met.met_station` × M. Archive manifest identity carries the SELECT-key set (`basin_version_id`, `model_id`, `run_id`, `cycle_time`, `source`) but not the full row shapes for these 11 tables.

Pinned strategy: **hybrid — drill lifts registry closure from prod readonly DB using manifest identity as SELECT keys, into staging DB before parse_run/apply_forcing_domain_handoff**. Pure "operator pre-seeds" is fragile; pure "manifest synthesis" is impossible.

The drill uses two connections:

- `prod_ro_conn` — read-only SELECT-only against production (`nhms_display_ro` role or the same); NEVER writes.
- `staging_conn` — full CRUD against the isolated staging DB (`staging_database.database`); writes registry closure + receives ingest output.

Registry lifter walks: `manifest.identity` → forcing lane closure (`met.data_source`, `met.forecast_cycle`, `met.forcing_version`, `met.met_station × M`, `core.basin`, `core.basin_version`, `core.mesh_version`, `core.model_instance`) or runs lane closure (`hydro.hydro_run` → same via `model_id`, plus `core.river_network_version` → `core.river_segment × N`). Lifter is idempotent (checks existence before INSERT); staging DB is dropped + recreated per run so idempotency is defense-in-depth.

Fail-closed on incomplete closure: if any required ancestor row cannot be lifted (missing in prod), drill emits FAIL receipt with `differences[]` naming the missing ancestor and exits non-zero — no vacuous PASS.

### Deliverables

- `scripts/node27_archive_rebuild_drill.py` — drill orchestrator with the four sub-components above (extract, lift, ingest, verify) + receipt emitter matching `schemas/archive_rebuild_drill_receipt.schema.json`.
- `scripts/node27_archive_rebuild_drill_once.sh` (optional per §4.5 pattern; defer to §5.2 or bundle here — implementer choice with recorded deviation).
- `infra/env/node27-archive-rebuild-drill.example` — `PROD_DATABASE_URL_RO`, `STAGING_DATABASE_URL` (must be distinct from prod URL's dbname), `ARCHIVE_ROOT`, `SALVAGE_MANIFEST_PATH`, `RECEIPT_PATH`, drill window bounds.
- `tests/test_node27_archive_rebuild_drill.py` — unit tests covering the 5 test rows in tasks.md §5.1.
- `tests/fixtures/archive-rebuild-drill/` — `.tar.zst` sample archives + salvage `.csv.zst` samples + manifest JSONs (crafted per manifest schema).

### Invariant matrix

| Invariant | Enforcement |
|---|---|
| Staging DB name ≠ prod DB name | Drill entry: parses both DSNs, refuses if `dbname` equal; unit test asserts refusal. |
| No writes to prod DB | prod_ro_conn opened with role scope; unit test uses `SELECT current_user, session_user` assertions + rejects any INSERT/UPDATE/DELETE on the mocked prod cursor. |
| Staging DB dropped + recreated per run | Drill entry: `DROP DATABASE IF EXISTS <staging>` + `CREATE DATABASE <staging>` + `apply_migrations_from_zero`; unit test asserts sequence. |
| Registry closure lifted before ingest | Drill orchestrator sequence: lift → ingest; unit test asserts lift runs before OutputParser call. |
| Fail-closed on incomplete closure | Lifter raises `RegistryClosureIncompleteError`; drill catches → FAIL receipt with `differences[]`; unit test covers. |
| Extract-to-disk bounded | Extract helper caps enforced with `TarPathEscapeError` / `TarBoundExceededError`; unit test uses malicious tarball fixture. |
| Product parity via file-parsed expected counts | Verifier parses restored files via same `parse_rivqdown_file` logic + compares to staging `COUNT(*)`; unit test asserts count derivation from file, not manifest. |
| Salvage verified sha256 + decompressed row count = manifest | No reingest; unit test covers sha256 mismatch → FAIL and row-count mismatch → FAIL. |
| Receipt coverage tuples attributed only to actually-restored manifests | Verifier accumulates coverage tuples as each restore succeeds; unit test asserts an unrestored manifest does NOT appear in coverage. |
| Coverage rule per spec (§5.1 test row 4 + spec.md coverage requirement) | Coverage evaluator function referenced by receipt emitter; unit test with pre-seeded prod + various coverage tuple combinations. |
| Compressed prod chunks unaffected | staging_conn writes staging DB only; unit test uses TimescaleDB integration marker + real prod-mirror with force-compressed chunk covering drill window + asserts prod chunk `is_compressed` unchanged post-drill. |

### Wire-format codes

Drill emits structured `differences[]` on FAIL. Code strings (byte-identical across code / runbook / this fixture):

- `ARCHIVE_MANIFEST_MISMATCH` — manifest sha256/size does not match restored file.
- `ARCHIVE_TAR_CORRUPTED` — tarball truncated or extract-to-disk fails.
- `SALVAGE_SHA256_MISMATCH` — `db-export` object sha256 does not match manifest.
- `SALVAGE_ROW_COUNT_MISMATCH` — decompressed row count ≠ manifest `exported_row_count`.
- `REGISTRY_CLOSURE_INCOMPLETE` — missing ancestor row in prod DB, or a prod row column absent from the staging table (schema-drift guard, D2).
- `STAGING_COUNT_MISMATCH` — staging `COUNT(*)` ≠ file-derived expected count.
- `DRILL_UNCAUGHT_ERROR` — any downstream fault outside the enumerated codes lands here (psycopg2 / OSError / OutputParsingError / ...); receipt carries `differences[].actual.cause_type` = exception class name. Added by Round 1 fix pass (B1 / C-is-4).
- `DRILL_CONCURRENT_INVOCATION` — non-blocking `fcntl.flock` on the drill lock file is already held. Added by Round 1 fix pass (C2 / C-is-3). Round 2 NEW-3: FAIL receipt actual carries `cause_type = "DrillConcurrentInvocationError"` (symmetric with `DRILL_UNCAUGHT_ERROR`) so operators reading the receipt file — the sole oracle — can distinguish this race from a generic uncaught error without stderr.

### Single-instance lock path (Round 2)

The lock file backing `DRILL_CONCURRENT_INVOCATION` MUST be byte-identical across code, `.example`, and runbook so operators reading either surface find the same absolute path:

- Env override: `NHMS_ARCHIVE_REBUILD_DRILL_LOCK_PATH` (absolute path required at boot; parity with `NHMS_ARCHIVE_REBUILD_DRILL_RECEIPT_PATH`).
- Default (env unset): `~/node27-archive-rebuild-drill-logs/drill.lock`.
- Runbook cross-references: `docs/runbooks/tier-node27-timeseries-storage.md` §7.2 (wire-code entry) + §7.6 step 1 (stuck-lock `rm -f` recovery). Both cite the default path verbatim; the drill code returns the same string.

Round 2 NEW-1: prior to this pin, `_default_lock_path(receipt_path)` co-located the lock next to the receipt file, so the shipped example put the lock at `/home/nwm/NWM/artifacts/receipts/drill.lock` while the runbook said `~/node27-archive-rebuild-drill-logs/drill.lock` — the documented recovery `rm` was a no-op. Fixed by making the default a fixed absolute path and adding the env override.

### Explicit deviations from prior sub-issue patterns

- **Two DB connections** — no prior sub-issue opens two Postgres connections. Documented here as inherent to H4 hybrid lifter; drill orchestrator must own connection lifecycle for both.
- **DROP + CREATE DATABASE per run** — `apply_migrations_from_zero` is the same helper `tests/conftest.py::integration_database_url` uses; running against a real Postgres cluster in a scripted (not pytest) context is new. Drill must accept `--dry-run` that skips CREATE DATABASE + logs planned actions; enforce path (default OFF, matching §5.1 unit-test-only surface) actually creates + drops.
- **prod readonly credential** — the `nhms_display_ro` role suffices for SELECT lift closure. Drill entry validates the prod DSN's user has SELECT on the target tables and FAILs closed otherwise.

### Task §5.2 boundary

Live PASS receipt on node-27 covering ≥1 forcing cycle + ≥1 runs cycle + ≥1 db-export selector for the planned 30-day drop window; committed as a follow-up commit under this same issue, not part of the §5.1 PR. §5.2 unlocks retention enforce in §6.3.

## Workflow Fixture: Issue #855 Gated Retention Runner + Systemd Wiring

Fixture level `expanded` · Repair intensity `high` · NHMS project profile · Reuses the shared change (`tier-node27-timeseries-storage`); capability `timeseries-db-retention`. Task scope: §6.1 (runner + wrapper + unit tests) + §6.2 (systemd + env + governance registration + runbook §8). §6.3 (live dry-run receipt review + first enforce) is a follow-up commit under the same issue, wired to #856.

### Pre-implementation hazards resolved (Phase 0.5)

Phase 0.5 fixture review surfaced **4 BLOCKING + 6 MODERATE + 3 MINOR CONFIRMED**. Resolutions pinned so Phase 1 does not drift; wire-format codes and env-name catalogue are canonical here.

**H1 — Completeness-receipt gate scope (BLOCKING).**
Spec `timeseries-db-retention/spec.md:13-19` says every subject "with rows or products in the drop window" must carry `verdict = complete`, but `schemas/archive_completeness_receipt.schema.json` is subject-list keyed; the runner MUST NOT re-query the DB to enumerate in-window subjects (would introduce a shadow oracle bypassing D6). Pinned rule: the receipt is the sole authority. Runner refuses if (a) `coverage_bounds` does not fully contain the drop window (`bounds.start <= drop.start ∧ bounds.end >= drop.end`), or (b) any subject whose `window` overlaps the drop window has `verdict != complete`. Distinct wire codes per case (see §Wire-format codes).

**H2 — Drill per-source coverage rule (BLOCKING).**
Retention drops chunks from `hydro.river_timeseries` (source=`runs`) and `met.forcing_station_timeseries` (source=`forcing`); the drill receipt PASS branch declares `coverage[]` tuples `(source ∈ {forcing, runs, db-export}, window)`. Runbook §7.5 already declares the rule the runner MUST byte-for-byte enforce: for BOTH `source=forcing` AND `source=runs` the UNION of coverage tuples must span the drop window (the drill emits per-cycle 24 h tuples, so a 30 d drop window is normally covered by ~30 daily tuples whose union spans it — no single tuple is expected to individually contain the drop window); `db-export` coverage is required iff the completeness receipt reports any `coverage=db-export` verdict overlapping the drop window, and the same union rule applies. Refusal is per-shortfall — distinct wire codes so operators see which source blocked. §7.5 uses UNION wording aligned with §8.2 wire codes (`DRILL_COVERAGE_FORCING_MISSING`, `DRILL_COVERAGE_RUNS_MISSING`, `DRILL_COVERAGE_DB_EXPORT_MISSING`) so all three surfaces — H2 here, runbook §7.5, and runbook §8.2 — share the same byte-identical semantic.

**H3 — Chunk enumeration to honour per-tick bound (BLOCKING).**
`SELECT drop_chunks(older_than := X, hypertable := 'schema.table'::regclass)` cannot bound cardinality (server picks all matching chunks). Runner MUST reuse the #851 pattern: catalog-enumerate via `timescaledb_information.chunks` for the two D3 hypertables, `ORDER BY hypertable_schema, hypertable_name, range_end ASC`, take `per_tick_bound`, then invoke `drop_chunks` per selected chunk (`older_than := chunk.range_end + INTERVAL '1 microsecond'` — the smallest strict-greater step). Remaining eligible chunks are recorded in `deferred_remainder[]`.

Divergence from #851 sibling: retention MUST NOT filter `is_compressed = false`. Compressed chunks older than 30 d are exactly the retention target; the enumeration includes both `is_compressed IN (true, false)`. Code comment MUST cite this divergence.

**H4 — `freed_bytes` measured BEFORE drop (BLOCKING).**
Receipt schema requires `dropped_chunks[]{name, freed_bytes: integer, minimum: 0}`. Measurement path: `pg_total_relation_size(<schema>.<chunk_name>::regclass)` per selected chunk BEFORE the corresponding `drop_chunks` call; recorded in a local dict keyed by fully-qualified chunk name; attached to `dropped_chunks[]` on success. Post-drop measurement is impossible (relation gone). Reuse compression `_default_measure_chunk_bytes` pattern minus the `after=True` branch.

**H5 — No partial-outcome shape in the schema (MODERATE).**
`schemas/timeseries_retention_receipt.schema.json:40-68` `oneOf` is exactly `{dry-run | refused | enforced}`; no `partial` outcome. Pinned policy: fail-closed. If any per-chunk `drop_chunks` raises, the whole tick refuses — subsequent chunks are NOT attempted; the receipt outcome is `refused` with `refusal_reason = RETENTION_DROP_FAILED:<schema>.<chunk>` and the runner exits non-zero. Alternative (extend schema for `partial`) rejected: retention drops on healthy chunks should not happen mid-failure without operator inspection.

**H6 — Wire-format refusal codes (MODERATE).**
Established byte-identity discipline from #854 (wire codes byte-identical across code / runbook / design / tests). Retention codes pinned here (see §Wire-format codes below).

**H7 — Chunk-boundary predicate (MODERATE, #852-class).**
Spec: "chunks are dropped only when their entire range is older than the window". TimescaleDB catalog `range_end` is exclusive (half-open `[range_start, range_end)`; max row time is `range_end - ε`). Correct predicate is `chunk.range_end <= cutoff` (non-strict); a chunk with `range_end == cutoff` has all row times strictly less than `cutoff` and therefore satisfies "entire range older than window". #851 compression uses strict `<` (which is safer for compression but wrong for retention). Divergence MUST be cited in a code comment.

**H8 — Per-gate freshness defaults (MODERATE).**
Spec: "configurable validity window". Pinned defaults:
- `NODE27_TIMESERIES_RETENTION_COMPLETENESS_MAX_AGE_HOURS` default `26` (audit runs daily; 26h absorbs one late run).
- `NODE27_TIMESERIES_RETENTION_DRILL_MAX_AGE_DAYS` default `30` (matches drill cadence; an expiring receipt forces a re-run per tasks §6.3 steady state).
Both compared against `generated_at` in each receipt; distinct refusal codes for missing vs stale.

**H9 — `salvage_backed_windows[]` provenance (MODERATE).**
Populated from the completeness receipt's subject windows where `coverage == "db-export"` AND `verdict == "complete"` AND the subject `window` overlaps the drop window. NOT synthesized from chunk ranges — chunk boundaries do not carry lane/subject identity; the recovery path (§3.2 manual `COPY FROM`) is completeness-selector scoped. Deduplicate identical `{start,end}` pairs and sort ascending.

**H10 — Lock-path byte-identity (MODERATE, #854-R2 same-class).**
Default absolute path: `/tmp/nhms-node27-timeseries-retention.lock` (`nhms-` prefix parity with `/tmp/nhms-node27-timeseries-compression.lock`). Env override: `NODE27_TIMESERIES_RETENTION_LOCK_PATH` (must be absolute). Runbook §8, `.example`, and code default MUST be byte-identical.

**H11 — Governance DEFAULT_SERVICES (MODERATE).**
`scripts/node27_resource_governance.py` DEFAULT_SERVICES tuple gains BOTH `nhms-node27-timeseries-retention.service` and `nhms-node27-timeseries-retention.timer`, alphabetically after the compression pair. Unit test asserts membership + presence in the governance receipt when systemctl is mocked.

**H12 — `statement_timeout` reuse (MINOR).**
Reuse the compression per-connection `SET statement_timeout` pattern (each catalog/DDL op opens its own connection). Catalog enumeration: 60 000 ms. `drop_chunks` per chunk: 300 000 ms.

**H13 — Env prefix (MINOR).**
`NODE27_TIMESERIES_RETENTION_*` (parity with `NODE27_TIMESERIES_COMPRESSION_*`).

**H14 — Runbook §8 placement (MINOR).**
New §8 immediately after §7.7. Sub-sections: install, wire-format codes, metadata-table exemption + row-count invariant, run recipe (dry-run first), reading the receipt, recovery from stuck lock / partial drop, salvage-backed windows → cross-link §3.2 manual restore + §7.5 drill coverage rule. Retention units added to §Rollback list.

**H15, H16 — REFUTED.** Metadata tables are regular (`drop_chunks` accepts only hypertables); chunk-interval divergence is per-chunk-agnostic.

**H17 — Zero-eligible enforce (MINOR, PLAUSIBLE).**
Add explicit test row: enforce mode when the catalog enumeration returns 0 eligible chunks yields `outcome=enforced`, `dropped_chunks=[]`, `deferred_remainder=[]`, `salvage_backed_windows=[]`, exit 0. Prevents miscoding as `refused`.

### Deliverables

- `scripts/node27_timeseries_retention.py` — 4-phase runner (config → gate → enumerate/measure → drop) + receipt emitter matching `schemas/timeseries_retention_receipt.schema.json` + wire-code frozenset + jsonschema self-validation.
- `scripts/node27_timeseries_retention_once.sh` — env-file `_once.sh` mirror of `node27_timeseries_compression_once.sh` (mode/no-symlink checks; python bin resolve).
- `infra/systemd/nhms-node27-timeseries-retention.{service,timer}` — cloned from compression siblings, `OnCalendar` slot `05:15:00 UTC` (after audit ~03:xx + compression 04:25).
- `infra/env/node27-timeseries-retention.example` — envs enumerated in H13 + H8 + H10.
- `scripts/node27_resource_governance.py` — DEFAULT_SERVICES gains 2 entries (H11).
- `tests/test_node27_timeseries_retention.py` — unit tests covering §6.1 test rows + H1-H17 pins + 2 governance test rows.
- `tests/test_node27_resource_governance.py` — assertion that new units are included (H11 test row).
- `docs/runbooks/tier-node27-timeseries-storage.md` §8 (H14).

### Invariant matrix

| Invariant | Enforcement |
|---|---|
| Completeness receipt authority (H1) | Runner reads only from the receipt; no DB probe for in-window subjects; unit test asserts refusal on subject `verdict != complete` in drop window and on `coverage_bounds` shortfall. |
| Drill per-source coverage (H2) | Runner requires that for BOTH `source=forcing` AND `source=runs` the UNION of coverage tuples spans the drop window (per-cycle 24 h tuples merge into a single covering interval); `db-export` required iff completeness reports `coverage=db-export` overlap; unit test per shortfall and per union-gap. |
| Chunk enumeration honours per-tick bound (H3) | Catalog query + ORDER BY range_end ASC + `[:per_tick_bound]`; unit test asserts (a) selected count == bound when eligible > bound, (b) `deferred_remainder[]` = remaining eligible chunks. |
| Compressed chunks are retention-eligible | Catalog filter includes `is_compressed IN (true, false)`; unit test with mixed compressed/uncompressed eligible chunks. |
| Boundary predicate `range_end <= cutoff` (H7) | Predicate in enumeration query + code comment citing spec; unit test with chunk at boundary + chunk straddling boundary. |
| `freed_bytes` measured BEFORE drop (H4) | Per-chunk measurement precedes `drop_chunks` call; unit test asserts measurement call happens before drop call via mock ordering assertion. |
| Fail-closed on per-chunk drop failure (H5) | Try/except around each `drop_chunks`; on failure → `outcome=refused`, `refusal_reason=RETENTION_DROP_FAILED:<schema>.<chunk>`, non-zero exit; subsequent chunks NOT attempted; unit test asserts abort ordering. |
| Freshness gates (H8) | `generated_at` compared to `now`; unit test per gate at boundary + past. |
| `salvage_backed_windows[]` from completeness (H9) | Derived only from completeness receipt subjects; unit test asserts absence when no `db-export` subject overlaps. |
| Lock path byte-identity (H10) | `_default_lock_path()` returns the literal string; test asserts against runbook §8 + `.example`. |
| Metadata tables untouched (spec test row 4) | Structural (drop_chunks only accepts hypertables) + belt-and-braces unit test row asserting `hydro_run`/`run_display_coverage`/`forcing_version`/`state_snapshot` row counts unchanged pre/post enforce (fixture-level assertion in integration marker; §6.3 covers live proof). |
| Governance registration (H11) | `DEFAULT_SERVICES` membership test + governance receipt inclusion test with `systemctl` mocked. |
| Zero-eligible enforce (H17) | Unit test row per §6.1 with catalog empty → `outcome=enforced`, exit 0. |

### Wire-format codes

Retention emits structured refusal codes; byte-identical across code (`scripts/node27_timeseries_retention.py` WIRE_CODES frozenset) / runbook §8.2 / this fixture / unit tests.

- `COMPLETENESS_RECEIPT_MISSING` — env-declared path missing / not a regular file.
- `COMPLETENESS_RECEIPT_STALE` — `generated_at` older than `NODE27_TIMESERIES_RETENTION_COMPLETENESS_MAX_AGE_HOURS`.
- `COMPLETENESS_RECEIPT_BOUNDS_INSUFFICIENT` — `coverage_bounds` does not contain the drop window.
- `COMPLETENESS_RECEIPT_GAP_IN_DROP_WINDOW` — any in-window subject has `verdict = gap`.
- `COMPLETENESS_RECEIPT_PENDING_IN_DROP_WINDOW` — any in-window subject has `verdict = pending-archive`.
- `DRILL_RECEIPT_MISSING` — env-declared path missing / not a regular file.
- `DRILL_RECEIPT_STALE` — `generated_at` older than `NODE27_TIMESERIES_RETENTION_DRILL_MAX_AGE_DAYS`.
- `DRILL_RECEIPT_FAIL` — drill receipt `verdict = FAIL`.
- `DRILL_COVERAGE_FORCING_MISSING` — no set of `source=forcing` tuples whose UNION covers the drop window.
- `DRILL_COVERAGE_RUNS_MISSING` — no set of `source=runs` tuples whose UNION covers the drop window.
- `DRILL_COVERAGE_DB_EXPORT_MISSING` — completeness shows `db-export` overlap but no set of drill `source=db-export` tuples whose UNION covers the drop window.
- `RETENTION_CONFIG_INVALID` — absolute-path / positive-int / env-parse failure before any DB call. Emitted to stderr as a single JSON line `{status: "failed", code: "RETENTION_CONFIG_INVALID", reason: <detail>}`; the runner exits with code 2 and NEVER publishes a file receipt (the receipt path itself may be part of what failed to parse).
- `RETENTION_CONCURRENT_INVOCATION` — non-blocking `fcntl.flock` on the lock path is already held.
- `RETENTION_DROP_FAILED` — per-chunk `drop_chunks` raised; suffix `:<schema>.<chunk_name>`. Whole tick refuses (H5).
- `RETENTION_UNCAUGHT_ERROR` — catch-all top-level exception; receipt carries `refusal_reason = "RETENTION_UNCAUGHT_ERROR:<ClassName>: <str(exc)>"`; runner exits non-zero. Symmetric with #854 `DRILL_UNCAUGHT_ERROR`.

### Environment variables

Byte-identical across code default lookup, `infra/env/node27-timeseries-retention.example`, runbook §8.1, this fixture.

- `DATABASE_URL` — Postgres writer role DSN for the retention runner.
- `NODE27_TIMESERIES_RETENTION_WINDOW_DAYS` — default `30`.
- `NODE27_TIMESERIES_RETENTION_PER_TICK_BOUND` — default `5` (matches compression sibling).
- `NODE27_TIMESERIES_RETENTION_COMPLETENESS_RECEIPT_PATH` — absolute.
- `NODE27_TIMESERIES_RETENTION_DRILL_RECEIPT_PATH` — absolute.
- `NODE27_TIMESERIES_RETENTION_COMPLETENESS_MAX_AGE_HOURS` — default `26`.
- `NODE27_TIMESERIES_RETENTION_DRILL_MAX_AGE_DAYS` — default `30`.
- `NODE27_TIMESERIES_RETENTION_RECEIPT_PATH` — absolute.
- `NODE27_TIMESERIES_RETENTION_LOCK_PATH` — default `/tmp/nhms-node27-timeseries-retention.lock`.
- `NODE27_TIMESERIES_RETENTION_ENFORCE` — presence toggles enforce mode; absent → dry-run.

### Explicit deviations from prior sub-issue patterns

- **Retention includes compressed chunks** — divergence from #851 compression `_CHUNK_QUERY` which filters `is_compressed = false`. Runner filter is `is_compressed IN (true, false)`; code comment cites this pin. Compressed chunks older than 30 d are exactly the retention target.
- **Predicate `range_end <= cutoff` (non-strict)** — divergence from #851 compression's strict `<`. Retention semantics: chunk with `range_end == cutoff` has all row times strictly < cutoff → satisfies "entire range older than window". Code comment cites spec sentence.
- **Fail-closed whole-tick refusal on per-chunk drop failure** — no `partial` outcome. Alternative rejected due to schema `oneOf` strictness + operator-inspection principle (drops on healthy chunks should not proceed mid-failure).
- **`drop_chunks` per selected chunk (not per hypertable bulk)** — required by per-tick bound (H3). Two per-chunk calls with `older_than := chunk.range_end + INTERVAL '1 microsecond'` per selected chunk.
- **Byte-identity discipline scope extension** — when correcting a runbook section for a gate-mechanism semantic (§8.5 dry-run behavior in R1; §7.5 union coverage in R2), the fix MUST (a) sweep every runbook section referencing the same mechanism (§7.5 mirrors §8.2 wire-code definitions; §8.5 mirrors the dry-run gate-eval semantics), (b) add a behavior-lock test for the corrected claim. R2 fix pass adds §7.5 union alignment + `test_dry_run_evaluates_gates_before_dryrun_branch` to close the pattern. This extends the discipline established for wire codes in #854 R2 to prose corrections of gate mechanisms, so a section-level rewrite cannot silently leave a sibling section out of sync.

### Task §6.3 boundary

Live dry-run receipt review + first enforce receipt on node-27 (row-count of metadata/coverage tables unchanged pre/post; DB size delta reported) is a follow-up commit under a distinct issue (#856), not part of the §6.1 + §6.2 PR. Steady state: timer-driven enforce keeps passing gates via recurring audit receipts; drill re-run required when the drill receipt exceeds its validity window or archive tooling/format changes.

## Workflow Fixture: Issue #1067 Node-27 Wrapper Import Contract

Fixture level `expanded` · Repair intensity `high` · NHMS project profile · Reuses the shared change (`tier-node27-timeseries-storage`). Scope is the seven issue-named node-27 `*_once.sh` wrappers and their systemd execution contract; Python archive/audit/retention semantics remain unchanged.

### Must preserve / must change

- Preserve each wrapper's existing env loading, argument forwarding, validation, and final Python entrypoint semantics.
- Every governed wrapper MUST snapshot the caller-inherited `PYTHONPATH` before sourcing its env file, then prepend its parameterized repository root to that snapshot before launching Python; an env-file `PYTHONPATH` assignment cannot discard the caller entries.
- The resolved root MUST be absolute, contain no `:` path-list delimiter, and identify the same checkout as the default interpreter and Python entrypoint. Explicit interpreter/script overrides remain supported.
- Because `scripts/` is intentionally a namespace directory without `__init__.py`, wrapper preflight MUST fail closed if the effective file-launch search path would make a regular `scripts` package outside the resolved root win module resolution. The preflight MUST model the actual `python "$SCRIPT"` directory and launch cwd, and MUST remain correct when `PYTHONSAFEPATH=1` / `-P` removes the unsafe command-directory entry.
- Exact governed set and root source: `node27_storage_inventory_audit_once.sh` / `NODE27_STORAGE_INVENTORY_AUDIT_REPO_ROOT`; `node27_product_archive_once.sh` / `NODE27_PRODUCT_ARCHIVE_REPO_ROOT`; `node27_timeseries_compression_once.sh` / `NODE27_TIMESERIES_COMPRESSION_REPO_ROOT`; `node27_timeseries_retention_once.sh` / existing `NODE27_TIMESERIES_RETENTION_REPO`; `node27_db_export_salvage_once.sh` / `NODE27_DB_EXPORT_SALVAGE_REPO_ROOT`; `node27_archive_rebuild_drill_once.sh` / `NODE27_ARCHIVE_REBUILD_DRILL_REPO_ROOT`; `node27_raw_retention_once.sh` / existing `NODE27_RAW_RETENTION_REPO`. Every variable defaults to `/home/nwm/NWM` when unset or empty.
- `node27_archive_rebuild_drill_once.sh` does not exist on the baseline branch; issue #1067 explicitly names it in the required sibling set, so this PR adds a complete wrapper using the drill's existing env/CLI contract rather than silently reducing coverage.
- Do not add `scripts/__init__.py` or rewrite Python imports; do not address URI-prefix or mover-discovery defects tracked by #1066/#1065.

### Risk packs considered

- Public API / CLI / script entry: selected — systemd invokes the wrappers as production entrypoints.
- Config / project setup: selected — repository-root overrides and inherited `PYTHONPATH` are environment contracts.
- File IO / path safety / overwrite: selected — the configurable repository root becomes a module-search path; empty values fall back to the default and relative roots must be refused before Python launch.
- Schema / columns / units / field names: not selected — no payload or receipt schema changes.
- Auth / permissions / secrets: not selected — no credential or privilege behavior changes.
- Concurrency / shared state / ordering: not selected — wrappers remain single-process `exec` launchers.
- Resource limits / large input / discovery: not selected — no discovery or data processing behavior changes.
- Legacy compatibility / examples: selected — all seven wrappers must preserve existing launch behavior and inherited `PYTHONPATH` entries.
- Error handling / rollback / partial outputs: selected — import startup must succeed; downstream failures and receipts remain owned by existing Python code.
- Release / packaging / dependency compatibility: selected — the fix defines import resolution for the non-package `scripts/` source tree without adding `__init__.py`.
- Documentation / migration notes: selected — commit node-27 journal evidence proving the systemd path crossed the former import failure; the completeness receipt is deferred until #1066/#1065 remove the independent downstream blockers.
- Geospatial / CRS / basin geometry: not selected — untouched.
- Hydro-met time series / forcing windows: not selected — untouched.
- SHUD numerical runtime / conservation / NaN: not selected — untouched.
- PostGIS / TimescaleDB domain behavior: not selected — this issue must not alter or require DB behavior.
- Slurm production lifecycle / mock-vs-real parity: not selected — node-22 scheduling is untouched.
- External hydro-met providers / snapshot reproducibility: not selected — untouched.
- Run manifest / QC provenance: not selected — untouched.
- Published NHMS artifacts / display identity: not selected — no artifact identity change; only live wrapper evidence is published.

### Invariant Matrix

- Governing invariant: every governed systemd wrapper binds root, default interpreter, entrypoint, and `scripts` import origin to one checkout; encodes that root as exactly one first `PYTHONPATH` entry; and preserves the caller's safe inherited entries byte-for-byte and in order.
- Source-of-truth contract: wrapper-specific `NODE27_*_REPO_ROOT` value, otherwise `/home/nwm/NWM`; default interpreter/entrypoint derive from the final post-env root.
- Producers: seven `scripts/node27_*_once.sh` wrappers.
- Validators/preflight: shell root/delimiter checks, import-origin preflight, and wrapper contract tests.
- Storage/cache/query: none — no persistent state or DB access is added.
- Public routes/entrypoints: seven wrapper Python-launch boundaries and `nhms-node27-storage-inventory-audit.service`.
- Frontend/downstream consumers: audit/archive/compression/retention/salvage/drill/raw-retention Python scripts, unchanged.
- Failure paths/rollback/stale state: missing `scripts` import must disappear; downstream #1066/#1065 failures remain distinct and observable.
- Evidence/audit/readiness: focused pytest, ruff, strict OpenSpec validation, and node-27 systemd journal evidence; archive-completeness receipt follow-up after #1066/#1065.
- Regression rows:
  - unset or empty root override -> governed wrapper uses `/home/nwm/NWM` as the first `PYTHONPATH` entry;
  - absolute custom root -> custom root becomes the first entry; relative or colon-bearing root -> stable pre-launch refusal;
  - empty inherited `PYTHONPATH` + test repo root -> `from scripts import node27_product_archive` succeeds through the wrapper launch contract;
  - existing two-entry caller `PYTHONPATH` + env-file empty/non-empty assignment -> caller entries remain byte-for-byte and in order after the resolved root;
  - later inherited regular `scripts` package -> governed module origin or stable refusal before the audit entrypoint;
  - `PYTHONSAFEPATH=1` with a safe governed checkout -> all seven wrappers reach their entrypoints without false refusal;
  - regular `scripts` package in the actual entrypoint directory, including explicit script override outside the root -> stable refusal before entrypoint side effects;
  - retention/raw caller path with an empty segment -> preflight and post-`cd` file launch resolve the same effective path;
  - custom root without interpreter/script overrides -> defaults derive from the same custom checkout;
  - all six sibling wrappers across unset/empty/absolute/relative/delimiter roots -> same root-prepend contract while retaining original arguments, Python entrypoint, and downstream exit code.

### Boundary-surface checklist

- Shared helper roots: no helper exists; keep the prelude text mechanically consistent across all seven wrappers.
- Public entrypoints: the exact seven issue-named wrappers above are in scope; `node27_download_once.sh` and `node27_resource_governance_once.sh` are explicit non-goals because #1067 does not name those independent service lanes and they do not launch the affected archive/audit module family.
- Producer/consumer evidence boundary: systemd environment -> pre-source caller-path snapshot -> env file -> resolved root/interpreter/entrypoint -> import-origin preflight -> audit journal/receipt.
- Unchanged downstream consumers: Python script arguments, entrypoints, downstream exit codes, and receipt semantics are unchanged.

## Workflow Fixture: Issue #1066 Audit Prefix and Terminal Receipts

Fixture level `expanded` · Repair intensity `high` · NHMS project profile ·
Reuses the shared change (`tier-node27-timeseries-storage`). Scope is the
node-27 inventory audit's object-URI identity, terminal receipt state machine,
schema, two drifting archive-lane examples, and focused regression/live
evidence. Retention remains an unchanged downstream consumer.

### Source of truth / must preserve / must change

- The canonical prefix for URIs persisted by the node-27 producer/ingest lane
  is `s3://nhms`: `infra/env/node27-download.example` writes the shared NFS
  object-store with it; `infra/env/node27-ingest.example` supplies it to the
  ingest process; `scripts/node27_ingest_run.py` uses the same prefix for its
  default run manifest/output URIs; and node-27 live DB rows are observed as
  `s3://nhms/...`. The audit and product-archive examples MUST therefore use
  `s3://nhms`, not the unproduced `s3://nhms-object-store` bucket identity.
- `infra/env/compute.example` and `infra/env/display.example` retain
  `s3://nhms-prod`: those values are consumer/config identities and are not
  producers of the node-27 DB URI rows audited here. This fixture does not
  assert a separate physical store or mount. Clarifying comments MUST state
  producer-vs-consumer identity without inventing topology; this issue MUST
  NOT globally rewrite every `OBJECT_STORE_PREFIX` to one value.
- Preserve the audit's repeatable-read/read-only DB snapshot, subject/verdict/
  selector semantics, URI containment checks, secret redaction, resource
  bounds, and the existing mode-0600 durable atomic publication protocol.
- Migrate `archive_completeness_receipt.schema.json` from `schema_version=1.0`
  to `schema_version=1.1`. The top-level exact `oneOf` has four mutually
  exclusive branches, all with `additionalProperties=false` and common required
  fields `schema_version`, `generated_at`, and `outcome`:
  - `complete`: requires coverage-only fields `coverage_bounds`, `windows`, and
    `salvage_selectors`; forbids `refusal_reason`, `error_reason`, and `detail`;
    every subject verdict is `complete` and `salvage_selectors` is empty;
  - `incomplete`: requires the same coverage-only fields and forbids all reason/
    detail fields; at least one subject is `pending-archive` or `gap`, and the
    existing forcing/run gap-selector bijection remains exact;
  - `blocked`: requires a stable non-secret `refusal_reason`, permits optional
    sanitized `detail`, and forbids `coverage_bounds`, `windows`,
    `salvage_selectors`, and `error_reason`;
  - `indeterminate`: requires a stable non-secret `error_reason`, permits
    optional sanitized `detail`, and forbids every coverage field plus
    `refusal_reason`.
  Runtime semantic validation MUST enforce success aggregate semantics that
  JSON Schema alone cannot express. Empty inventory is `blocked` with stable
  `refusal_reason=EMPTY_INVENTORY`, never an empty success receipt.
- Stable blocked reason codes are
  `CONFIG_INVALID`, `EMPTY_INVENTORY`, `OBJECT_URI_PREFIX_MISMATCH`,
  `EVIDENCE_BLOCKED`, `RESOURCE_BOUND_EXCEEDED`, and `RECEIPT_INVALID`;
  unexpected pre-publication exceptions use
  `error_reason=UNEXPECTED_AUDIT_ERROR`. Raw exception text belongs only in
  optional sanitized `detail`. Publication failures use stderr codes
  `RECEIPT_PUBLICATION_FAILED` (pre-replace) or
  `RECEIPT_PUBLICATION_INDETERMINATE` (post-replace), never receipt outcomes.
- Once a safe receipt destination is available, any audit/config/evidence
  terminal reached before publication starts MUST replace the previous success
  with a schema-valid `blocked` or `indeterminate` receipt. This prevents a
  consumer from mistaking an old success for the current invocation. The
  capability requirement/scenarios and historical fixture/task language in
  this change are updated now, not deferred to implementation.

### Early config and atomic-publication contract

- Receipt destination discovery is a two-phase config boundary. A bootstrap
  parser recognizes `--receipt-path VALUE` and `--receipt-path=VALUE`
  independently of argument order, unknown options, or later argparse type
  errors, then applies one-CLI-value-over-env precedence and captures one UTC
  terminal timestamp. Full-parser unknown-option/type failures reached after a
  safe destination was bootstrapped map to an on-disk `blocked` receipt.
  Missing option value, multiple/ambiguous receipt-path occurrences, missing
  CLI+env value, or an unsafe destination itself is an unwriteable exception.
- Before the first atomic publication call begins, expected `AuditBlocked`
  failures map to on-disk `blocked`, unexpected exceptions map to on-disk
  `indeterminate`, and a coverage run maps to `complete`/`incomplete`. Each
  branch is schema + runtime validated, then gets exactly one publication
  attempt. The process exits zero only for published success branches.
- Once that first publication attempt begins, its own failures are NOT receipt
  outcomes: they emit sanitized structured stderr and MUST NOT trigger a second
  publish. A pre-replace publication error preserves the old bytes; a
  post-replace durability/namespace error leaves target content unknown. Both
  exit non-zero and neither may claim `published`; post-replace stderr uses an
  indeterminate-publication diagnostic, not an on-disk `outcome=indeterminate`
  claim.
- The destination/unwriteable exceptions above cannot truthfully yield an
  on-disk receipt. They emit one sanitized structured stderr diagnostic and
  exit non-zero without exposing DB URL/credentials or fabricating a terminal
  receipt.

### Risk packs considered

- Public API / CLI / script entry: selected — `main()` and the systemd wrapper
  define exit status, stderr, and the terminal receipt contract.
- Config / project setup: selected — prefix reconciliation and receipt-path-
  first config parsing change production examples and startup behavior.
- File IO / path safety / overwrite: selected — failure receipts deliberately
  replace a previous gate receipt through the existing no-follow atomic lane.
- Schema / columns / units / field names: selected — the completeness receipt
  gains a top-level four-branch terminal `oneOf` contract.
- Auth / permissions / secrets: selected — DB URLs remain redacted and the
  audit remains read-only; terminal messages cannot leak credentials.
- Concurrency / shared state / ordering: selected — config discovery, audit,
  semantic validation, and one final publication attempt have a fixed order;
  the stable receipt is shared state consumed by retention/salvage.
- Resource limits / large input / discovery: selected — existing inventory,
  traversal, manifest, and timeout bounds must hold on every new branch.
- Legacy compatibility / examples: selected — successful coverage payload
  meaning and pinned examples remain compatible while env drift is corrected.
- Error handling / rollback / partial outputs: selected — pre-publication
  failures map to one terminal outcome; publication-attempt failures remain
  stderr-only and preserve/mark uncertainty without a partial-success claim.
- Release / packaging / dependency compatibility: not selected — no new
  dependency, packaging surface, or runtime version requirement.
- Documentation / migration notes: selected — env-plane comments and committed
  node-27 live receipt document the operational contract.
- Geospatial / CRS / basin geometry: not selected — geometry is untouched.
- Hydro-met time series / forcing windows: selected — real DB-shaped forcing/
  run/state URIs and their coverage windows drive the receipt regression.
- SHUD numerical runtime / conservation / NaN: not selected — no model run.
- PostGIS / TimescaleDB domain behavior: selected — the node-27 oracle uses the
  real read-only DB shape; no schema or mutation is introduced.
- Slurm production lifecycle / mock-vs-real parity: not selected — node-22 and
  scheduling are untouched.
- External hydro-met providers / snapshot reproducibility: not selected —
  provider retrieval is untouched.
- Run manifest / QC provenance: selected — real DB URI/manifest identity must
  pass the audit rather than a prefix-normalized fake oracle.
- Published NHMS artifacts / display identity: selected — DB URI, hot-object
  path, terminal receipt bytes, and receipt consumer must remain attributable.

### Invariant Matrix

- Governing invariant: one audit invocation binds node-27 DB URI identity to
  the actual producer prefix and ends in exactly one of three states: (1) a
  safe destination contains the invocation's schema-valid terminal receipt;
  (2) destination bootstrap itself is impossible/unsafe and stderr states
  receipt unavailability; or (3) the one publication attempt fails, stderr
  reports the publish phase, old bytes are preserved pre-replace, and target
  content is unknown post-replace. States (2)/(3) never claim publication.
- Source-of-truth contract: producer prefix `s3://nhms`; terminal receipt
  `schema_version=1.1`; exact `outcome`; stable `refusal_reason`/
  `error_reason`; pinned
  `archive_completeness_receipt.schema.json`; configured absolute receipt path.
- Producers: node-27 download/ingest + `LocalObjectStore.uri_for_key` and
  `scripts/node27_ingest_run.py`; audit `build_receipt()`/terminal builder.
- Validators/preflight: two-phase config loader, `_object_key`, receipt runtime
  semantic validation, Draft 7 + format checking, safe output-path validation.
- Storage/cache/query: node-27 read-only repeatable-read DB snapshot, shared NFS
  object-store, and the single stable receipt file.
- Public routes/entrypoints: `node27_storage_inventory_audit.py main()` and
  `nhms-node27-storage-inventory-audit.service`.
- Frontend/downstream consumers: DB-export salvage and timeseries retention;
  salvage may consume selectors only from a `complete`/`incomplete` coverage
  branch and MUST fail closed before DB/export work on `blocked`/
  `indeterminate`; retention is inspected only for static
  outcome-vs-missing distinguishability and receives no behavior change in
  this issue.
- Failure paths/rollback/stale state: early config failure, expected evidence
  blocker, unexpected exception, schema/semantic rejection, pre-replace
  failure, and post-replace uncertainty.
- Evidence/audit/readiness: focused local pytest/schema/ruff/OpenSpec checks,
  real DB-shaped disk-receipt regression, and node-27 live schema-valid receipt.
- Regression rows:
  - producer-shaped `s3://nhms/forcing/...` URI + canonical config -> URI binds
    to the expected hot key and audit reaches coverage classification;
  - mismatched configured bucket + real DB-shaped URI -> non-zero exit and a
    schema-valid on-disk `blocked` receipt with stable refusal reason;
  - all subjects complete -> `complete`; any pending/gap -> `incomplete`, with
    unchanged subject windows/selectors and aggregate semantic validation;
  - invalid later config/unknown option/type error + bootstrapped safe receipt
    path -> on-disk `blocked`; missing-value/duplicate/ambiguous/unsafe receipt
    path itself -> sanitized stderr only and no fabricated publish;
  - unexpected audit error before publication -> on-disk `indeterminate`;
  - injected first-publish pre-replace failure -> prior bytes unchanged,
    stderr-only, no second publish; injected post-replace failure -> target
    content unknown, stderr indeterminate-publication, no second publish;
  - existing successful receipt example -> validates under exactly one branch
    after the recorded contract migration;
  - blocked/indeterminate receipt presented to DB-export salvage -> stable
    pre-export refusal with no DB read or archive write;
  - blocked receipt file vs absent receipt path -> statically distinguishable
    to the unchanged retention consumer; runtime refusal mapping remains #856.
  - node-27 before #1065 closure -> success receipt has at least one `windows`
    subject, or `blocked/EVIDENCE_BLOCKED` with sanitized #1065-attributable
    detail; `indeterminate` is not accepted as live readiness evidence.

### Boundary-surface checklist and non-goals

- Public entry/read surface: audit CLI/env parsing and read-only DB/object-store
  inventory only; no write-capable DB role or query change.
- Write/overwrite/publish surface: exactly one stable receipt path and one
  publish attempt, existing safe atomic helper, explicit prior-byte and
  post-replace unknown-content rules.
- Producer/consumer evidence boundary: producer prefix -> persisted DB URI ->
  `_object_key` -> coverage payload -> schema branch -> retention/salvage read.
- Stale-state/idempotency boundary: each new controlled invocation replaces a
  prior terminal receipt with its own timestamp/outcome; repeating the same
  input yields semantically identical terminal classification.
- Unchanged downstream consumer: no edits to
  `scripts/node27_timeseries_retention.py`; static inspection only proves a
  blocked receipt is distinguishable from no file. #856 owns live cascade and
  any downstream outcome-specific refusal behavior.
- Adjacent salvage consumer: its selector loader remains fail-closed; focused
  compatibility evidence proves only coverage branches can supply salvage
  scope and terminal failure branches cannot trigger export work.
- Non-goals: #1065 mover discovery/manifest/EACCES repair; #1067 wrapper import
  repair; DB URI migration or producer rewrite; node-22 compute prefix rewrite;
  retention enforce/dry-run or any #856 live cascade action.

## Workflow Fixture: Issue #1065 Product-Archive Live Shape and Access Failure

Fixture level `expanded` · Repair intensity `high` · NHMS project profile ·
Reuses the shared change (`tier-node27-timeseries-storage`). Scope is the
node-27 product-archive mover's real forcing/run manifest shape, historical
prefix-mismatch regression, states discovery permission diagnostics, focused
tests, runbook operations, and the live receipts that unblock the upstream
archive-completeness audit. Retention, compression, rebuild-drill execution,
and every #856 cascade action remain unchanged downstream consumers.

### Source of truth / must preserve / must change

- The first live receipt's 592 forcing and 852 run failures were produced with
  `OBJECT_STORE_PREFIX=s3://nhms-object-store`, while the real manifests use
  `s3://nhms/...`. Current node-27 env and the producer examples use
  `s3://nhms`. Real forcing manifests bind every declared file below the exact
  package leaf. Real GFS/IFS run manifests bind `run_id` to the run directory,
  `outputs.run_manifest_uri` to `runs/<run_id>/input/manifest.json`, and
  `outputs.output_uri` to `runs/<run_id>/output` modulo the already-canonical
  directory trailing slash. Therefore the existing exact-leaf and run-output
  validators are security boundaries and MUST NOT be loosened to accept the
  historical mismatched bucket or cross-leaf URIs.
- The live-shape fixture MUST exercise the production discovery and validation
  path without replacing the validator under test. It covers forcing and runs
  for both GFS and IFS, includes qhh and heihe identities, proves canonical
  `s3://nhms` shapes pass, and proves the historical
  `s3://nhms-object-store` configuration reproduces the two pinned failure
  reasons. It also covers inaccessible GFS and IFS qhh/heihe state leaves.
- Preserve safe relative-path enforcement, exact package containment, manifest
  identity, checksum/tree/provenance validation, discovery/tree/resource
  bounds, deterministic ordering, dry-run write limits, enforce
  verify-before-delete semantics, flocking, and mode-0600 atomic receipt
  publication.
- A states subtree access denial reached during locator discovery or bounded
  full validation is an operational precondition failure, not a malformed
  independent state identity. All such inaccessible state leaves from one
  pre-selection invocation MUST collapse into exactly one existing-schema
  `discovery_failures` item:
  `{"lane_hint":"states","locator":"states","reason":
  "STATES_ACCESS_DENIED count=<decimal> euid=<decimal> egid=<decimal>"}`.
  `count` is positive and counts denied state leaves only. No receipt-schema
  version or field is added. After the receipt is durably published, `main()`
  emits exactly one compact structured stderr line
  `{"count":<decimal>,"egid":<decimal>,"euid":<decimal>,
  "exit_reason":"STATES_ACCESS_DENIED","status":"failed"}` and exits `2`;
  other receipt failures keep exit `1`. Raw absolute paths and exception text
  MUST NOT be copied into the receipt or stderr. Runtime semantic validation
  recognizes this exact lane-level shape. The invocation does not enter
  candidate processing, source deletion, or archive mutation and does not claim
  a passing receipt. Before candidate processing, every selected candidate
  participates in one batch source-retirement capability gate. One failed
  source-parent or tree-directory check aborts the selected batch before
  staging, publication, quarantine, durable-guard creation, or source
  mutation. After the gate passes and candidate processing begins, later
  permission/race changes retain the existing independent-candidate
  failed/indeterminate terminal semantics; #1065 does not add rollback across
  published candidates.
- The runbook MUST state the complete operator repair for selected products in
  all physical lanes (`forcing`, `runs`, and `states`). Adding `nwm` to
  `nfsdata` alone is insufficient when leaves are mode `0700`: existing and
  future directories need effective read/write/search because enforce performs
  verified sibling rename, claim-directory creation, and recursive tombstone
  retirement; files need read. An equivalent named-user plus default ACL MAY
  be used, with its file-write inheritance tradeoff or an explicit writer-side
  post-create ACL documented. A new login/user-manager restart is required
  only after supplementary-group changes. Verification covers `id`, `namei`,
  `getfacl`, directory `test -x`/`test -w`, file `test -r`, and a complete
  logged `find` as `nwm`. The PR does not execute `usermod`, `chmod`, `chgrp`,
  or ACL mutation.
- Dry-run performs a descriptor-bound, no-follow effective-identity access
  check without creating probe paths: each source parent needs write/search,
  every directory in the selected source tree needs read/write/search, and
  source files retain the existing read/identity verification. A failed check
  produces a sanitized non-zero batch receipt rather than a false `planned`
  result. Sticky-bit parents/directories require a conservative ownership proof
  for every rename they govern; `os.access` success alone is insufficient.
  The blocker terminal's selected identity binds the source; wire reasons carry
  only a closed check token and never interpolate the locator, so legal spaces
  remain unambiguous. Runtime semantics reject unknown tokens and reject
  enforce-only probe tokens in dry-run receipts. Enforce repeats the read-only
  gate for the entire selected batch and, before candidate one, performs one
  randomized hidden mkdir/fsync/rmdir/fsync capability probe per unique opened
  source parent. The deduplication key and probe both derive from the same held
  parent fd; a namespace rebound after the probe fails closed.
  Any probe failure aborts the batch with zero archive publication and zero
  source mutation; uncertain cleanup is indeterminate and records the safe
  root-relative probe residue.
- Before a new retirement mutation, the existing-archive idempotent path
  boundedly inventories `.archive-guards`. It automatically removes only an
  exact two-file guard whose children are the same inode/signature pair as the
  currently verified canonical tar/manifest, using held parent/guard fds and
  mount checks. Foreign, malformed, extra-entry, and ambiguous guards remain
  untouched. Matching-guard cleanup uncertainty blocks source mutation and
  reports the safe guard-relative residue; a successful retry cannot leave a
  matching stale hard-link guard while claiming empty terminal residue.
- Live proof is staged: before operator permission repair, a direct mover run
  produces the single `STATES_ACCESS_DENIED` terminal diagnostic; after the
  operator repair, a default-env direct dry-run proves current production
  configuration succeeds even when its eligible queue is empty. The authorized
  archive proof then uses the existing allowed minimum of 30 days with an
  explicit `--minimum-age-days 30 --enforce` override while leaving production
  env at 45 days. It produces a schema-valid non-failed receipt with non-empty
  candidates, `bytes.source > 0`, `bytes.archived > 0`, successful terminals,
  zero pinned forcing/run discovery reasons, and verify-before-delete source
  retirement. The immutable first failure receipt and the new passing mover
  receipt remain committed. The 228 DB-only forcing gaps, task 3.3 salvage,
  and follow-up complete audit are owned by the already-open #1070 Step A2
  issue and are not executed while closing #1065.
- The first authorized 30-day live attempt exposed the missing batch gate:
  discovery found 320 candidates and selected eight, but the old enforce path
  published eight verified archives before all eight source tombstone renames
  failed because the effective `nwm` identity could not write the selected
  source parents. All eight sources remained, while verified archives and
  durable guards were preserved as explicit residue. This is failed live
  evidence, never a passing receipt. The repaired mover MUST stop the same
  permission shape before publishing candidate one.

### Risk packs considered

- Public API / CLI / script entry: selected — direct and systemd execution must
  expose a stable non-zero states-access reason.
- Config / project setup: selected — canonical producer prefix versus the
  historical mismatched prefix is the primary live-shape regression.
- File IO / path safety / overwrite: selected — discovery walks untrusted NFS
  paths and the mover may later delete verified source trees in enforce mode.
- Schema / columns / units / field names: selected — receipt diagnostics and
  planned byte accounting must remain schema-valid and semantically exact.
- Auth / permissions / secrets: selected — effective uid/gid and NFS mode/ACL
  determine reachability; diagnostics must not leak credentials or unsafe
  absolute paths.
- Concurrency / shared state / ordering: selected — one invocation aggregates
  access failures deterministically while preserving flock and bounded queue
  ordering.
- Resource limits / large input / discovery: selected — the real object-store
  has over a thousand leaves; fixtures and production traversal retain all
  existing caps.
- Legacy compatibility / examples: selected — the first-live receipt remains a
  red baseline and current producer-shaped manifests remain accepted.
- Error handling / rollback / partial outputs: selected — access denial fails
  before mutation, is aggregated once, and cannot be confused with a malformed
  manifest or a successful partial archive.
- Release / packaging / dependency compatibility: not selected — no dependency
  or runtime-version change is required.
- Documentation / migration notes: selected — the operator permission repair
  and verification procedure are an explicit acceptance condition.
- Geospatial / CRS / basin geometry: not selected — basin geometry is not read
  or transformed.
- Hydro-met time series / forcing windows: selected — GFS/IFS forcing package
  identities and their authoritative windows are discovered from live-shaped
  manifests.
- SHUD numerical runtime / conservation / NaN: not selected — no model runtime
  or numerical output changes.
- PostGIS / TimescaleDB domain behavior: selected only for the final read-only
  inventory-audit completeness oracle; no DB mutation or schema change.
- Slurm production lifecycle / mock-vs-real parity: not selected — node-22 and
  scheduling are untouched.
- External hydro-met providers / snapshot reproducibility: selected — GFS/IFS
  provider source segments must remain distinct and canonical.
- Run manifest / QC provenance: selected — run directory, run ID, manifest URI,
  output URI, and producer prefix stay exactly bound.
- Published NHMS artifacts / display identity: selected — hot-object identity,
  archive receipt, and completeness receipt must all refer to the same source
  bytes; display behavior remains untouched.

### Invariant Matrix

- Governing invariant: every discovered forcing/run leaf is accepted only when
  its canonical producer URI and manifest identity bind the exact hot-store
  leaf; a state namespace that cannot be traversed terminates once with a
  stable access diagnostic before any archive mutation; every selected
  forcing/run/state batch is also rejected before candidate one when any source
  cannot be retired by the effective mover identity.
- Source-of-truth contract: producer prefix `s3://nhms`; forcing exact package
  leaf; run `run_id` plus exact manifest/output locations; states filesystem
  mode/ACL as the access oracle; product-archive receipt schema as the output
  contract.
- Producers: node-27 download/ingest and SHUD run/state writers.
- Validators/preflight: mover locator discovery, forcing/run manifest loaders,
  canonical URI/relative-path validators, state dirfd walk, selected-batch
  descriptor-bound effective-access checks, enforce-only unique-parent
  capability probes, receipt runtime and JSON-Schema validation.
- Storage/cache/query: `/home/ghdc/nwm/object-store`, archive root, stable mover
  receipt, and read-only inventory-audit snapshot/receipt.
- Public routes/entrypoints: `scripts/node27_product_archive.py` and
  `nhms-node27-product-archive.service`; no HTTP route changes.
- Frontend/downstream consumers: inventory audit consumes hot/archive evidence;
  retention consumes only a later complete audit receipt and is not run here.
- Failure paths/rollback/stale state: mismatched bucket, cross-leaf forcing URI,
  run identity/output drift, one or many inaccessible state leaves, partial
  traversal, sticky ownership ambiguity, parent namespace replacement,
  enforce-probe cleanup uncertainty, stale matching durable guards, and receipt
  publication failure.
- Evidence/audit/readiness: focused live-shape pytest, full existing mover
  suite, ruff, strict OpenSpec validation, pre-repair access receipt, and a
  post-repair passing mover receipt tied to the deployed commit.
- Regression rows:
  - canonical `s3://nhms` GFS/IFS forcing packages for qhh/heihe -> accepted;
    historical mismatched configured bucket or a cross-leaf file URI -> the
    pinned exact-package failure;
  - canonical GFS/IFS runs for qhh/heihe, including output URI with a trailing
    slash -> accepted; mismatched configured bucket or drifted run/output
    identity -> the pinned run-binding failure;
  - inaccessible GFS and IFS state leaves during discovery/full validation ->
    exactly one safe lane-level
    receipt diagnostic, one exact structured stderr line, and exit code `2`,
    regardless of leaf count; non-access discovery failures retain exit `1`;
  - accessible state leaves plus canonical forcing/runs -> non-failed enforce,
    non-empty candidates under the explicitly authorized 30-day evidence
    override, `bytes.source > 0`, `bytes.archived > 0`, successful terminals,
    verify-before-delete retirement, and no pinned discovery reasons; a prior
    default 45-day dry-run may validly produce an empty queue;
  - any selected parent/tree permission or sticky-ownership blocker -> one
    identity-bound closed-token failure plus constant batch-aborted terminals,
    with no locator interpolation, publication, or source mutation; legal
    space-bearing forcing/run/state locators remain schema-valid;
  - existing verified archive plus a prior exact matching durable guard ->
    reconcile that guard before source mutation and retire successfully;
    foreign/ambiguous guards remain untouched, while cleanup failure preserves
    source and reports safe residue;
  - prior first-live failure receipt remains byte-identical and the new mover
    receipt validates and identifies the deployed commit.

### Boundary-surface checklist and non-goals

- Shared helper boundary: no producer URI or manifest contract is rewritten;
  validators remain mover-owned and strict.
- Public/operational boundary: code reports the access precondition; the
  operator alone changes NFS group/mode/ACL state.
- Producer/consumer evidence boundary: producer manifest -> mover discovery ->
  archive receipt -> inventory audit -> complete receipt.
- Stale-state/idempotency boundary: failed access cannot leave a prior passing
  receipt looking current; the 30-day enforce run is bounded and retires only
  sources whose staged archive has been re-read and verified.
- Non-goals: no retention dry-run/enforce, compression, rebuild drill, source
  deletion outside the explicitly authorized product-archive enforce run,
  node-22 code/config change, DB mutation, display change, task 3.3 salvage,
  follow-up complete audit, or any #856 live-cascade issue (#1069–#1072).
  The operator may apply the documented ACL precondition through the node-22
  file owner because both nodes see the same states filesystem; that operational
  repair is not a node-22 application change.
