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
- Regression: salvage traversal is bounded to 10,000 manifests/100,000 total entries/eight levels, and per-run output traversal is bounded to 10,000 entries/eight levels; both inspect every bounded sibling and preserve the prior receipt on overflow, while run-output list/stat/child-open stays on one held directory-FD tree across pathname swaps.
- Regression: salvage enumeration/stat/child-open/manifest-read/object-hash stays on one held `db-export` FD tree; real-directory swaps cannot mix evidence namespaces or bypass the global entry cap.
- Regression: archive age shares the >=30-day foundation invariant without truthiness fallback, and all readable mismatch evidence survives coverage fallback precedence.
- Regression: missing archive namespaces are ordinary absence; existing unsafe/unreadable/malformed/conflicting evidence blocks publication, while a fully readable size/checksum mismatch is recorded and treated as absent coverage so the safe pending/gap receipt can still publish.
- Regression: readable hot forcing manifest/member and state checksum mismatches are retained as absent-coverage evidence even when product/salvage wins; unsafe, malformed, permission and I/O failures remain blockers.
- Regression: final completeness receipts are deterministic, schema-valid, atomically replaced, cover every subject exactly once, and enforce an exact forcing/run gap-selector bijection; pre-replace blockers preserve the previous valid receipt, while directory-fsync or observed post-replace parent-identity failure is reported as indeterminate and never as `published`.
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
- Publish boundary: validated receipts explicitly opt into same-directory mode-0600 temporary files plus atomic replace, mandatory directory fsync, and post-replace parent-FD identity verification. Pre-replace blockers preserve the previous receipt and clean temporary residue; after-replace durability/namespace failures make the producer indeterminate/non-zero and never `published`, but a file-fsynced payload may already be visible. #855 independently validates the currently configured two receipt contents and does not add producer status, a sidecar, or systemd state as a third gate. The configured parent is operator-controlled and non-rotating during publication. The shared atomic helper keeps its legacy default for unmigrated non-receipt callers. Product/archive deletion and other mutations remain out of scope.
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
- Error handling / rollback / partial outputs: selected — refusal preserves sources; recurring audit failure preserves the previous receipt (already locked in #847).
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
- Failure paths/rollback/stale state: enforce below refuse threshold → refusal + WARN + non-zero, sources untouched; audit failure → prior completeness receipt preserved byte-identical (#847).
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
- Flock lock-holder-only publish: contender receives structured JSON skip on stderr; **does not** touch the shared receipt path.
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

## Workflow Fixture: Issue #850 DB-Export Salvage Exporter + Manual Restore Runbook + Live Salvage Run

Fixture level: expanded. Repair intensity: high. Project profile: NHMS.

Change surface:

- `scripts/node27_db_export_salvage.py` (new): reads the archive-completeness receipt (schema `schemas/archive_completeness_receipt.schema.json`), consumes the `salvage_selectors` array verbatim, runs `COPY (SELECT <fixed column list> FROM met.forcing_station_timeseries | hydro.river_timeseries WHERE <PK-scoped selector predicate>) TO STDOUT WITH (FORMAT CSV, HEADER)` per selector, zstd-compresses the CSV, publishes the object plus a per-object `manifest.json` sibling under `NHMS_ARCHIVE_ROOT/db-export/<lane>/<identity>/{data.csv.zst,manifest.json}` (`exports` array length 1 per manifest) via `atomic_write_bytes_no_follow(mode=0o600, require_durable_replace=True)`, and emits a receipt outside the archive root. Reads: `met.forcing_station_timeseries`, `hydro.river_timeseries` via display_ro or explicitly-scoped read role. Writes: filesystem only under `NHMS_ARCHIVE_ROOT/db-export/`. Never runs DDL, never deletes DB rows, never deletes archive objects. `tasks.md §3.1` and §3.2 checkboxes flipped in this PR; §3.3 flipped in the follow-up live-receipt commit.
- `scripts/node27_db_export_salvage_once.sh` (new, 0755): systemd oneshot wrapper — preflight absolute paths + non-symlink + env-file mode 0600, `set -a; . "$ENV_FILE"; set +a; exec uv run python …` per `#849` and `#851` convention.
- `infra/env/node27-db-export-salvage.example` (new): DB URL (display_ro or read-scoped role), `NHMS_ARCHIVE_ROOT`, `NHMS_ARCHIVE_COMPLETENESS_RECEIPT_PATH` (input scope source), receipt output path, lock path, `NODE27_DB_EXPORT_SALVAGE_PER_TICK_BOUND` (per-tick selector bound), `NODE27_DB_EXPORT_SALVAGE_ZSTD_LEVEL` (default `3`, matches sibling raw retention + archive discipline), `NODE27_DB_EXPORT_SALVAGE_STATEMENT_TIMEOUT_MS` (default `300000`, mirrors #851's compress-timeout convention), `NODE27_DB_EXPORT_SALVAGE_SOURCE_INSTANCE_ID` (env-configured literal that stamps `source_database.instance_id` in the manifest, matches the schema example's `"node27-primary-pg15"`). Header explicitly states "no automated restore lane exists — restore is the manual `COPY FROM` procedure documented in the archive runbook".
- `tests/test_node27_db_export_salvage.py` (new): unit tests covering selector-consumption invariants (receipt schema-validated on load, refuse hardcoded selector lists, refuse malformed selectors, idempotency skip on verified existing objects, dry-run isolation, per-selector failure isolation, manifest row-count parity, safe-relative-path enforcement, DSN masking, wrapper 6-case parametrized shell contract, receipt `outcome` enum coverage — `clean` / `partial` / `refused_lock` / `refused_config` / `refused_role` — and per-table column-list constants pinned by test asserting the SELECT column list matches the migration DDL columns for both hypertables).
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
- Failure paths/rollback/stale state: per-selector failure → skipped in receipt with descriptor `error`, other selectors continue; refusal → stderr JSON refusal + non-zero exit + no receipt touch. Receipt `outcome` enum is one of `clean` (all selectors exported or all verified-skipped), `partial` (at least one per-selector failure but at least one success), `refused_lock` (LOCK_EX contention at boot), `refused_config` (env / receipt-file / receipt-schema / hardcoded-list refusal), or `refused_role` (write-privilege preflight tripped by `has_table_privilege` OR rolled-back sentinel INSERT). Idempotent re-run skips selectors whose object exists with matching sha256 + manifest row count.
- Evidence/audit/readiness: dry-run default; enforce writes objects + manifest + receipt atomically; live task 3.3 receipt covers every audit-emitted salvage selector; follow-up audit shows those subjects `complete` and empty salvage list.

Regression rows:

- Input: receipt with two selectors, one already exported (object + manifest present + sha256 verifies + manifest row count matches DB). Expected: only the missing selector is exported; existing object untouched; receipt records both descriptors (one `skipped_verified`, one exported).
- Input: completed enforce export for a selector. Expected: manifest `exported_row_count` equals the DB row count for that selector at export time; per-object sha256 recorded; column list recorded verbatim.
- Input: invocation with a hardcoded selector list flag and no receipt. Expected: refused with structured stderr JSON diagnostic; exit non-zero; no receipt written.
- Input: receipt file missing OR schema-invalid OR `salvage_selectors` array missing/malformed. Expected: fail-closed refusal; no partial export; no receipt touch.
- Input: enforce request with the exporter DSN resolving to a role that can WRITE (`has_table_privilege(current_user, 'met.forcing_station_timeseries' | 'hydro.river_timeseries', 'INSERT')` returns `true` for at least one target, or the rolled-back sentinel `INSERT` succeeds against either target). Expected: refusal (`outcome=refused_role`); no export; no receipt written; stderr JSON refusal captured; test parametrizes both preflight legs so either alone fires the refusal.
- Input: dry-run mode. Expected: no filesystem writes to `NHMS_ARCHIVE_ROOT/db-export/`; receipt is written to receipt path with `mode: "dry-run"`; no `COPY` executed for enforce-only side effects.
- Input: per-selector `COPY` failure mid-run (e.g., statement timeout). Expected: the failing selector's descriptor records `error`; other selectors continue; per_selector_totals arithmetic reflects only the successfully-exported set (evidence-fidelity: no misleading aggregated totals); outcome=partial; exit non-zero.
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
- Staging/publish/rollback surfaces: same-directory temp + atomic rename via `require_durable_replace=True`; partial-write is impossible; rollback = re-run (idempotent skip).
- Producer/consumer evidence boundaries: input = audit's `salvage_selectors`; output = manifest with `provenance: "db-export"`; downstream drill (#854) verifies salvage objects by sha256 + manifest row-count parity, not by reingest.
- Stale-state/idempotency boundaries: re-run over verified existing objects skips them; a stale receipt (older than audit's next scheduled tick) is not a runtime hazard because the operation is one-time and the audit refresh cadence is already gated in #849.
- Unchanged downstream consumers: display API/frontend/read paths (ADR 0001), archive mover (#848), storage inventory audit (#847), resource governance (#849 extension), raw retention (pre-existing), hypertable compression (#851), write-guard (#852 owns), retention gate (#855 owns). None touched.


