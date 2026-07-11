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
- Regression: valid examples -> schema PASS; missing completeness verdict or salvage row count -> schema FAIL.
- Regression: product manifest row count/unsafe paths, invalid table-selector key, incomplete drill verdict details, or incomplete retention outcome details -> schema FAIL.
- Regression: product-only drill with empty selector list -> schema PASS; clean default test environment executes all schema negatives with zero skip.
- Regression: unchanged display import/call graph -> no archive resolver dependency and existing disk-only not-found semantics.
- Regression: unchanged `validate_object_path` and raw-retention/governance env aliases -> existing results and precedence remain stable.

Boundary-surface checklist:

- Shared helper root: `packages/common/storage.py`; read-only path derivation and validation only.
- Public entrypoints: none added; later scripts are consumers, and display entrypoints remain unchanged.
- Producer/consumer evidence boundary: each example names one schema; manifest fields distinguish product archive from `db-export` salvage.
- Stale-state/idempotency and write/delete/publish boundaries: out of scope until the corresponding runner issues.
