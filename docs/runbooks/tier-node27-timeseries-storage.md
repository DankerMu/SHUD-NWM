# Runbook: Tier Node-27 Timeseries Storage

Operation, rollback, and cadence rationale for the node-27 archive lane
(product mover + storage inventory audit) delivered under
`openspec/changes/tier-node27-timeseries-storage`.

- Design record: `openspec/changes/tier-node27-timeseries-storage/design.md`
- Architecture record: `docs/adr/0002-node27-timeseries-hot-cold-tiering.md`
- Display carve-out: `docs/adr/0001-display-timeseries-carveout.md` (the
  archive resolver is never imported by `apps/api/**` or `apps/frontend/**`).

The mover and audit share the 1.7 TB volume that also backs
`/home/ghdc/nwm/object-store` and `/home/ghdc/nwm/archive`. Free-space
watermarks defend that shared volume — the mover refuses enforce before
touching any source when free bytes fall below the configured refuse
threshold.

## Install (node-27, `nwm` user)

All operations run as the `nwm` user under systemd `--user`. Do NOT install
system-level (root) units for this lane.

1. Create the runbook receipt directories:

   ```
   mkdir -p ~/node27-product-archive-logs ~/node27-storage-inventory-audit-logs ~/node27-timeseries-compression-logs ~/node27-archive-rebuild-drill-logs
   ```

2. Copy the env examples into place and lock them down:

   ```
   cp /home/nwm/NWM/infra/env/node27-product-archive.example \
      /home/nwm/NWM/infra/env/node27-product-archive.env
   cp /home/nwm/NWM/infra/env/node27-storage-inventory-audit.example \
      /home/nwm/NWM/infra/env/node27-storage-inventory-audit.env
   chmod 0600 /home/nwm/NWM/infra/env/node27-product-archive.env \
              /home/nwm/NWM/infra/env/node27-storage-inventory-audit.env
   ```

   Fill in `DATABASE_URL` for the audit env with a read-only role
   (`nhms_display_ro` or equivalent). A superuser or write-capable role in
   this env is a documented rollback / lint finding — the audit does not
   need write access and the receipt runbook must reject the file if the
   role is not read-only.

   The governance env now shares three archive vars with the mover and
   audit envs — `NHMS_ARCHIVE_ROOT`, `NHMS_ARCHIVE_FREE_SPACE_WARN_BYTES`,
   and `NHMS_ARCHIVE_FREE_SPACE_REFUSE_BYTES`. All three env files MUST
   declare identical values; the wrappers source only their own env file,
   so drift means governance reports a different band than the mover
   actually enforces. See "Free-space watermark tuning" below for the
   band semantics.

3. Install the four user units and enable the timers. Order matters only
   because the audit timer must see the mover's latest final leaves:

   ```
   systemctl --user daemon-reload
   systemctl --user enable --now nhms-node27-product-archive.timer
   systemctl --user enable --now nhms-node27-storage-inventory-audit.timer
   ```

4. Copy or update `infra/env/node27-resource-governance.env` from the
   extended `.example` so it declares `NHMS_ARCHIVE_ROOT` and the two
   `NHMS_ARCHIVE_FREE_SPACE_*_BYTES` vars — matching the mover and audit
   env files verbatim. Without this the governance receipt will report
   `archive_root.status = "skipped"` and the archive band will not
   appear. Then register the four new units with
   `nhms-node27-resource-governance` via the shared `DEFAULT_SERVICES`
   list — no code action required beyond deploying the updated
   `scripts/node27_resource_governance.py`. The next governance audit
   tick will report their service/timer state, and — once the governance
   env carries the shared archive vars — the archive root free-space
   band as well. See "Free-space watermark tuning" for band semantics
   and "Refuse-threshold behavior" for what a `refuse` band triggers.

## Timer cadence order (UTC)

The four related timers are staggered so each receipt is fresh when the
next tick runs:

| Order | Timer                                        | OnCalendar         | Rationale |
|-------|----------------------------------------------|--------------------|-----------|
| 1     | `nhms-node27-product-archive.timer`          | `03:20:00 UTC` daily | Mover finalizes leaves before audit scans them. |
| 2     | `nhms-node27-storage-inventory-audit.timer`  | `03:40:00 UTC` daily | Audit reads the mover's committed final leaves and emits the completeness receipt. |
| 3     | `nhms-node27-resource-governance.timer`      | `04:10:00 UTC` daily | Governance audit reports the four new units + archive-root free-space band. |
| 4     | `nhms-node27-timeseries-compression.timer`   | `04:25:00 UTC` daily | Terminal-chunk compression runs after governance so the previous-day receipt is already captured. Enablement is task §4.5 (requires migration `000047` applied first). |

### Cadence vs. retention-receipt validity window (design D6)

`#855` will pin the retention runner's completeness-receipt validity
window at 24 h. The storage inventory audit fires every 24 h with the audit
tick preceding every planned retention tick (audit at 03:40 UTC, retention
by construction after the audit), so a fresh, schema-valid
archive-completeness receipt is always present when the retention gate
consults it. **Do not lengthen the audit cadence beyond 24 h without
extending the retention receipt validity window first.**

TODO(#855): cross-link the retention runbook and the retention receipt
validity constant once the retention runner lands.

## Operation

### Reading receipts

- Product archive mover receipt:
  `/home/nwm/node27-product-archive-logs/receipt.json` (mode 0600).
  The receipt binds one `outcome`:
  - `success` — every selected candidate archived + verified + retired
    (enforce) or planned (dry-run).
  - `failed` / `indeterminate` — see per-candidate `terminals` and top-level
    `discovery_failures`.
  - `refused_free_space` — mover refused enforce because free space fell
    below the configured refuse watermark; sources untouched, `candidates`
    / `selected` / `deferred` / `terminals` / `events` /
    `discovery_failures` all empty. `free_space.band == "refuse"` and
    `free_space.free_bytes < free_space.refuse_bytes`. Non-zero exit.
- Storage inventory audit receipt (archive completeness):
  `/home/nwm/node27-storage-inventory-audit-logs/completeness-receipt.json`.
  Consumed byte-for-byte by the future retention gate (#855).

### Free-space watermark tuning

Initial values (in `infra/env/node27-product-archive.example`):

- `NHMS_ARCHIVE_FREE_SPACE_WARN_BYTES=322122547200` (300 GiB)
- `NHMS_ARCHIVE_FREE_SPACE_REFUSE_BYTES=161061273600` (150 GiB)

Config validation enforces `refuse < warn`, both `> 0`, both integer bytes.
If either env var is set, both must be set; empty/negative/non-integer
values fail closed at startup with no truthiness fallback.

Tune by watching the governance receipt `archive_root.band` field after
each daily tick:

- `clean` — `free_bytes >= warn_bytes` — steady state; no action.
- `warn` — `refuse_bytes <= free_bytes < warn_bytes` — review retention or
  archive backlog before the refuse gate fires. Governance receipt emits
  `ARCHIVE_FREE_BELOW_WARN` recommendation.
- `refuse` — `free_bytes < refuse_bytes` — mover WILL refuse next tick.
  Free space (drop retention, add capacity) before the next mover tick.
  Governance receipt emits `ARCHIVE_FREE_BELOW_REFUSE` recommendation.

If both watermark envs are unset, the mover runs without free-space
enforcement (backwards compatible) and the governance receipt reports
`archive_root.band = "unconfigured"`.

### Refuse-threshold behavior

When `free_bytes < refuse_bytes` at mover start:

1. Mover exits non-zero after publishing the receipt with
   `outcome = "refused_free_space"`.
2. No candidate discovery runs; no source is mutated; no staging tarball
   is created; the mover flock is released cleanly.
3. Receipt records exact measured `free_bytes`, configured `warn_bytes`
   and `refuse_bytes`, and `archive_root` path.

Dry-run mode still evaluates and reports the refusal terminal — the refuse
band is a governance signal, not a mutation-only gate.

## 3. DB-export salvage

The salvage lane covers a one-time historical operation: forcing / river
timeseries windows whose upstream product cycles never made it into either
the hot object-store or the archive (typical case: `forcing/` before
2026-06-16, where the object-store was reset before an archive lane
existed). The salvage exporter reads those rows straight out of the two
detail hypertables via `COPY (SELECT ... WHERE ...) TO STDOUT WITH
(FORMAT CSV, HEADER)`, compresses the CSV with zstd, and publishes the
object plus a `manifest.json` sidecar under
`NHMS_ARCHIVE_ROOT/db-export/<lane>/<identity>/`.

Runner: `scripts/node27_db_export_salvage.py` (wrapper
`scripts/node27_db_export_salvage_once.sh`, env example
`infra/env/node27-db-export-salvage.example`).

### 3.1 Scope + provenance invariants

- Selector scope is the archive-completeness receipt's `salvage_selectors`
  array (design D6). Hardcoded selector lists are refused — the exporter
  is a downstream consumer of the audit contract, not a scope authority.
- Manifests carry `provenance: "db-export"` so downstream consumers
  (drill #854, retention gate #855) can permanently distinguish salvage
  objects from product-derived archive objects.
- The runner refuses at boot if its DSN maps to a role that can INSERT
  into either `met.forcing_station_timeseries` or `hydro.river_timeseries`
  (both `has_table_privilege` AND a rolled-back sentinel INSERT are
  checked). Wire the runner with `nhms_display_ro` or an equivalent
  explicit read-only role.

### 3.2 Salvage restore is manual — no automated restore lane

**Per ADR 0002 decision 3, `db-export` salvage objects have no automated
or steady-state restore lane.** The only restore path is the manual
`COPY FROM` procedure below. Retention (#855, forward cross-link to
section 6.2 of the retention runbook when authored) MAY drop
salvage-covered windows only with this documented manual recovery path
in place.

The archive rebuild drill (#854) verifies salvage objects by checksum
and manifest row-count parity — it does NOT reingest them.

#### 3.2.1 Checksum pre-check

Before any restore attempt, confirm the on-disk object matches the
manifest's recorded sha256:

```
cd ${NHMS_ARCHIVE_ROOT}/db-export/<lane>/<identity>/
manifest_sha=$(jq -r '.exports[0].object.sha256' manifest.json)
disk_sha=$(sha256sum data.csv.zst | awk '{print $1}')
[ "$manifest_sha" = "$disk_sha" ] || { echo "ABORT: checksum mismatch"; exit 1; }
```

If the pre-check fails, **do not restore** — treat the object as
corrupted evidence and escalate; the archive rebuild drill (#854)
verifies the same digest and will surface the corruption.

Also confirm the manifest matches the schema:

```
uv run python -c "
import json, jsonschema
schema = json.load(open('schemas/salvage_manifest.schema.json'))
manifest = json.load(open('manifest.json'))
jsonschema.validate(manifest, schema)
print('OK')
"
```

#### 3.2.2 Manual `COPY FROM` procedure

Salvage manifests record the exact column list used at export time (the
full DDL column set for the hypertable). The restore MUST reuse that same
list — do not paste a hand-typed column list.

For a forcing salvage object
(`db-export/forcing/<forcing_version_id>/data.csv.zst`):

```
zstd -q -d -c data.csv.zst | psql \
  -h 127.0.0.1 -p 55432 -U <writer_role> -d nhms \
  -c "\copy met.forcing_station_timeseries (
        forcing_version_id, basin_version_id, station_id, valid_time,
        source_id, variable, value, unit, native_resolution, quality_flag
      ) FROM STDIN WITH (FORMAT CSV, HEADER)"
```

For a river salvage object
(`db-export/runs/<run_id>/data.csv.zst`):

```
zstd -q -d -c data.csv.zst | psql \
  -h 127.0.0.1 -p 55432 -U <writer_role> -d nhms \
  -c "\copy hydro.river_timeseries (
        run_id, basin_version_id, river_network_version_id,
        river_segment_id, valid_time, lead_time_hours, variable, value,
        unit, quality_flag, created_at
      ) FROM STDIN WITH (FORMAT CSV, HEADER)"
```

The writer role MUST have INSERT on the target hypertable — the salvage
exporter's read-only role will not be able to restore. Verify the
restored row count against `manifest.exports[0].exported_row_count`
after the load:

```
psql -h 127.0.0.1 -p 55432 -U <writer_role> -d nhms -c "
  SELECT COUNT(*) FROM met.forcing_station_timeseries
   WHERE forcing_version_id = '<forcing_version_id>'
     AND valid_time >= '<manifest.exports[0].selector.window.start>'
     AND valid_time <  '<manifest.exports[0].selector.window.end>';"
```

#### 3.2.3 No pipeline code path performs automated CSV import

`apps/api/**` and `apps/frontend/**` MUST NOT reference the salvage
runner or the `db-export/` prefix (ADR 0001 display carve-out; enforced
by `tests/test_node27_db_export_salvage.py::test_display_carve_out`).
The reingest pipeline covers only product-provenance archive rebuilds
(design D1). Salvage restore requires a human operator, deliberately.

Related documents:

- ADR 0002 decision 3 (salvage restore is manual): see
  [`docs/adr/0002-node27-timeseries-hot-cold-tiering.md`](../adr/0002-node27-timeseries-hot-cold-tiering.md#decisions).
- Retention runbook section 6.2 (forward reference; to be authored by
  #855): retention MAY drop a salvage-covered window only if the
  manifest here validates and the checksum pre-check above succeeds.
  When #855 lands section 6.2, it MUST cross-link back to this section
  3.2.

## 4. Hypertable compression

Native TimescaleDB compression is the sole mechanism this milestone applies
to shrink the two hot hypertables (`hydro.river_timeseries` and
`met.forcing_station_timeseries`). Compression is applied to terminal chunks
only (age older than the configurable lag, default 7 d) by the receipted
runner (`scripts/node27_timeseries_compression.py`, `#851`), never to the
active write-target chunk. This section covers the fail-closed write guard
and the manual decompress procedure that pairs with it.

### 4.1 Write guard overview

The three ingest write paths —
`workers/output_parser/parser.py::upsert_river_timeseries`,
`workers/forcing_producer/store.py::replace_forcing_timeseries`, and
`packages/common/forcing_domain_handoff_apply.py::
_replace_forcing_station_timeseries` — each call the shared helper
`packages.common.timescale_write_guard.check_batch_targets_uncompressed`
BEFORE their identity-scoped DELETE. The guard runs one catalog lookup
against `timescaledb_information.chunks` bounded by
`SET LOCAL statement_timeout = '5s'`, checking whether any compressed chunk
overlaps the batch's `[min(valid_time), max(valid_time)]` window. On
overlap, the guard raises `CompressedChunkWriteError` naming the chunk and
this runbook's decompress anchor. On catalog error, it fails closed with
`CompressedChunkGuardError` — no silent permit.

The guard is intentionally scoped to `hydro.river_timeseries` and
`met.forcing_station_timeseries` only. The archive rebuild drill (`#854`)
writes to an isolated staging schema and never trips the guard.

### 4.2 Residual reingest window mismatch

The guard's semantic scope is the batch time window, not the identity's
full history. A batch whose `valid_time` range is fully outside compressed
chunks BUT whose identity-scoped DELETE (`WHERE forcing_version_id = %s`
or `WHERE run_id = %s AND river_network_version_id = %s AND variable = %s`)
would touch older compressed rows falls through to TimescaleDB's raw
`cannot update/delete rows from chunk … as it is compressed` error. This
is a documented residual — not a guard bug. The response is identical to
the guarded case: use the decompress procedure below on the specific
chunk(s) TimescaleDB names.

### 4.3 Decompress procedure

#### 4.3.1 Operator triage codes

The compressed-chunk write guard surfaces via four caller-observable string
codes. Every one of them routes to the decompress procedure below. Grep
the DB / stderr / receipt surface for these literals when triaging a
reingest failure:

| Code (literal string) | Where produced | How to observe |
|---|---|---|
| `HANDOFF_APPLY_COMPRESSED_CHUNK_BLOCKED` | `packages/common/forcing_domain_handoff_apply.py::apply_forcing_domain_handoff` — attached as `unavailable_report.unavailable_reasons[].code` (from `REASON_APPLY_COMPRESSED_CHUNK_BLOCKED`) when the guard raises inside `_replace_forcing_station_timeseries`. | Persisted on the apply report (DB or API response) that the caller inspects. |
| `OUTPUT_PARSE_COMPRESSED_CHUNK_BLOCKED` | `workers/output_parser/parser.py::OutputParser.parse_run` — stamped on `hydro.hydro_run.error_code` via `mark_run_failed`, and emitted as the stderr prefix by `workers/output_parser/cli.py` when the guard escapes. | `hydro.hydro_run.error_code` column; parser CLI stderr line `OUTPUT_PARSE_COMPRESSED_CHUNK_BLOCKED: ...`. |
| `FORCING_PRODUCE_COMPRESSED_CHUNK_BLOCKED` | `workers/forcing_producer/cli.py` stderr prefix — emitted when `ForcingProducer.produce()` re-raises a `CompressedChunkGuardError` un-wrapped. | Forcing producer CLI stderr line `FORCING_PRODUCE_COMPRESSED_CHUNK_BLOCKED: ...`. |
| `FORCING_COMPRESSED_CHUNK_BLOCKED` | `workers/forcing_producer/producer.py::ForcingProducer._mark_failed` — stamped on `met.forecast_cycle.error_code` when the dedicated `except CompressedChunkGuardError` arm fires. | `met.forecast_cycle.error_code` column (with `status = 'failed_forcing'`). |

For every code above, the operator response is the decompress procedure
in §4.3.2 below (identify chunk from the structured error message → run
`decompress_chunk(...)` → re-run ingest). Route on the code; do NOT paper
over with a generic ingest retry.

#### 4.3.2 Manual decompress steps

When a reingest surfaces `CompressedChunkWriteError` or TimescaleDB's raw
compressed-chunk error, follow this manual procedure. Do NOT introduce an
automated decompress-on-demand lane (ADR 0002 decision 3 — the manual
escape hatch is intentional; automated decompress-on-demand would
re-introduce write amplification the compression tier is meant to
prevent).

1. Identify the offending chunk from the error message. Example structured
   error:

   ```
   Reingest targets compressed chunk _timescaledb_internal._hyper_1_1_chunk
   in hydro.river_timeseries; run decompress procedure per
   docs/runbooks/tier-node27-timeseries-storage.md#43-decompress-procedure
   before retrying.
   ```

2. On node-27, connect to the active primary PG as a role authorized to
   decompress. Example:

   ```
   psql "postgres://nhms_owner@127.0.0.1:55432/nhms"
   ```

3. Confirm the chunk is currently compressed (belt-and-suspenders — the
   error already asserted this, but a manual re-run may already have
   decompressed it):

   ```
   SELECT chunk_schema, chunk_name, hypertable_schema, hypertable_name,
          is_compressed, range_start, range_end
   FROM timescaledb_information.chunks
   WHERE chunk_schema = '_timescaledb_internal'
     AND chunk_name = '_hyper_1_1_chunk';
   ```

4. Decompress the chunk:

   ```
   SELECT decompress_chunk('_timescaledb_internal._hyper_1_1_chunk'::regclass);
   ```

   `decompress_chunk` returns the fully-qualified chunk relation on
   success. If it errors with "chunk … is not compressed", the chunk was
   already decompressed by a prior manual step; move on.

5. Re-run the ingest / reingest that failed. The guard's next lookup on the
   same chunk range will now find `is_compressed = false` and permit the
   DELETE + INSERT.

6. After the reingest succeeds, plan a re-compression pass. The scheduled
   compression runner (`nhms-node27-timeseries-compression.timer`, cadence
   documented in `#851`) will pick the chunk up on its next tick provided
   the chunk's `range_end` is older than the configured lag. If the chunk
   is inside the lag window (i.e. still "warm"), let it age; do not force
   an out-of-cadence compression.

Rollback: none required — `decompress_chunk` is idempotent and can be
undone by the scheduled compression runner. If the reingest itself
completes but the operator wants to abandon the decompress state, force a
compression pass with the runner's enforce flag once the chunk falls
outside the lag window.

## 7. Archive rebuild drill (`archive-rebuild-drill`)

The drill (`scripts/node27_archive_rebuild_drill.py`, issue #854) proves that
products archived by the mover and salvage objects published by
`scripts/node27_db_export_salvage.py` are round-trippable back into an
ingest-shaped Postgres/TimescaleDB — without ever writing the production
hypertables (design D5, ADR 0002).

### 7.1 Isolation invariants (never bypass)

- **Staging DB is a SEPARATE PHYSICAL DATABASE.** Same-DB same-schema
  isolation is unachievable because every ingest SQL literal is
  `core.` / `met.` / `hydro.` / `ops.` qualified (`workers/output_parser/parser.py`,
  `packages/common/forcing_domain_handoff_apply.py`). The drill refuses at
  entry if the parsed `STAGING_DATABASE_URL` dbname equals
  `PROD_DATABASE_URL_RO` dbname.
- **Prod connection is SELECT-only.** The drill opens the prod DSN with
  `default_transaction_read_only = on` and asserts the setting via
  `SHOW default_transaction_read_only`. Even a role that accidentally has
  INSERT privilege cannot mutate prod inside a read-only transaction.
- **Staging DB is DROPped + CREATEd + migrated from zero per run.** Uses
  `POSTGRES_ADMIN_URL` (a superuser DSN whose dbname is `postgres`).
  Cleanup runs in a `finally:` block on both PASS and FAIL paths.
- **Archive files are read-only inputs.** The drill never rewrites, moves,
  or deletes tar.zst / manifest.json / db-export .csv.zst objects.

### 7.2 Wire-format codes

The drill emits structured `differences[]` on FAIL. Codes are
byte-identical across the code (`scripts/node27_archive_rebuild_drill.py`),
this runbook, and design.md #854 fixture block:

- `ARCHIVE_MANIFEST_MISMATCH` — manifest sha256/size does not match
  restored file.
- `ARCHIVE_TAR_CORRUPTED` — tarball truncated or extract-to-disk fails
  (includes malicious `../` path escape).
- `SALVAGE_SHA256_MISMATCH` — db-export object sha256 does not match its
  salvage manifest.
- `SALVAGE_ROW_COUNT_MISMATCH` — decompressed row count differs from
  manifest `exported_row_count`.
- `REGISTRY_CLOSURE_INCOMPLETE` — the ancestor row(s) needed for
  ingest FK checks are missing in prod (fail-closed; no vacuous PASS).
  Also fires on staging schema-drift: if any prod row carries a column
  the staging table lacks (e.g. a not-yet-migrated staging DB).
- `STAGING_COUNT_MISMATCH` — staging `COUNT(*)` differs from the
  file-derived expected count (archive manifests carry no row counts;
  parity oracle is the restored file itself).
- `DRILL_UNCAUGHT_ERROR` — any downstream fault outside the enumerated
  codes (psycopg2 disconnect, filesystem I/O error, unexpected
  AttributeError, ...) is packaged as a schema-valid FAIL receipt with
  `differences[].expected.code = DRILL_UNCAUGHT_ERROR` +
  `differences[].actual.cause_type = <ExceptionClassName>`. Never emit
  a raw stack trace to the receipt lane; operators consume the receipt
  file as the sole oracle.
- `DRILL_CONCURRENT_INVOCATION` — an existing drill holds
  `~/node27-archive-rebuild-drill-logs/drill.lock` (single-instance
  guard via `fcntl.flock`, non-blocking). Wait for the first drill to
  finish or investigate the stuck process.

### 7.3 How to run

```
# 1. Prime env
cp /home/nwm/NWM/infra/env/node27-archive-rebuild-drill.example \
   /home/nwm/NWM/infra/env/node27-archive-rebuild-drill.env
chmod 0600 /home/nwm/NWM/infra/env/node27-archive-rebuild-drill.env
# fill in real PROD_DATABASE_URL_RO, STAGING_DATABASE_URL,
# POSTGRES_ADMIN_URL, and NHMS_ARCHIVE_ROOT.

# 2. Source + invoke against real archive + salvage manifests
set -a; source /home/nwm/NWM/infra/env/node27-archive-rebuild-drill.env; set +a
uv run python scripts/node27_archive_rebuild_drill.py \
  --archive-manifest "${NHMS_ARCHIVE_ROOT}/runs/<run_id>/manifest.json" \
  --archive-manifest "${NHMS_ARCHIVE_ROOT}/forcing/gfs/<cycle>/<basin>/<model>/manifest.json" \
  --salvage-manifest "${NHMS_ARCHIVE_ROOT}/db-export/forcing/<forcing_version_id>/manifest.json"
```

Exit code semantics: `0` = PASS, `1` = FAIL (per-item differences), `2` =
configuration refusal (missing env var, DSN parity, unsafe path). The
receipt path is announced to stdout on success.

### 7.4 Reading the receipt

Receipts match `schemas/archive_rebuild_drill_receipt.schema.json`.
Every receipt carries:

- `staging_database.database` — the isolated physical database name that
  was DROPped after the run. MUST differ from prod dbname.
- `staging_database.schema` — semantic drill-run label (e.g.
  `archive_drill_20260711_forcing_gfs`); NOT a Postgres CREATE SCHEMA.
- `staging_database.instance_id` — cluster/host identifier stamped by
  `NHMS_ARCHIVE_REBUILD_DRILL_INSTANCE_ID`.
- `coverage[]` — the validated `(source, window)` tuples the drill
  actually restored + verified. Coverage tuples are attributed ONLY to
  successfully verified manifests (per-source: `forcing` / `runs` from
  product cycles, `db-export` from salvage selectors).
- On PASS: `comparisons.cycles[]`, `comparisons.selectors[]`,
  `comparisons.counts[]`.
- On FAIL: `differences[]` where each entry names the failing item, the
  wire-format code, and the expected/actual values.

### 7.5 How the coverage rule maps to the retention gate

Per the `archive-rebuild-drill` spec: a drill PASS receipt covers a
candidate retention drop window only when its declared `coverage[]`
tuples include, sampled within or older than that window:

- ≥1 product-derived cycle for each timeseries-bearing source lane
  (`forcing/`, `runs/`) that has DB rows in the drop window; PLUS
- ≥1 `db-export` selector whenever verified salvage objects cover any
  part of the drop window.

The drill declares its tuples faithfully; the retention runner (#855)
evaluates them against its candidate drop window. A FAIL receipt, a
stale receipt, or a PASS receipt whose coverage is insufficient blocks
retention enforcement.

### 7.6 Recovery (post-fault operator playbook)

When a drill run leaves side effects (stale staging DB, stale workspace,
held lock), recover in this order — every step is safe to run against
a clean environment (no-op if already recovered):

1. **Stuck lock (`DRILL_CONCURRENT_INVOCATION`).** Confirm no live
   drill is running (`ps -ef | grep node27_archive_rebuild_drill`);
   if none, remove the lock file:
   `rm -f ~/node27-archive-rebuild-drill-logs/drill.lock`.
2. **Staging DB left over.** The drill's `finally:` teardown drops
   the staging DB even on FAIL. If a hard kill (SIGKILL, OOM) skipped
   the finally, drop by hand: connect via `POSTGRES_ADMIN_URL` and run
   `DROP DATABASE IF EXISTS "<staging_dbname>"`. Staging dbname is in
   the last receipt at `staging_database.database`.
3. **Workspace tree left over.** The drill removes
   `NHMS_ARCHIVE_REBUILD_DRILL_WORKSPACE` on both PASS and FAIL. If a
   hard kill skipped cleanup, remove the tree manually:
   `rm -rf "$NHMS_ARCHIVE_REBUILD_DRILL_WORKSPACE"`. Do NOT touch
   `NHMS_ARCHIVE_ROOT` (the archive source is read-only per ADR 0002).
4. **Prod DB never needs recovery.** The drill only ever holds the prod
   connection in `default_transaction_read_only = on`; there is no
   prod-side state to unwind. If a receipt disagrees, that is a bug —
   file it against #854.

### 7.7 Live receipts (§5.2 boundary)

Live PASS receipts on node-27 covering the planned 30-day drop window
are the domain of task §5.2 (follow-up commit under issue #854, not part
of the §5.1 PR that introduced this section). Once §5.2 lands, the live
receipts will be committed under
`docs/runbooks/receipts/node27_archive_rebuild_drill/` — mirroring the
mover and audit receipt directories.

## Rollback (unit-level, not data-level)

Both units are read-mostly (audit is read-only; mover's writes are already
gated by ADR 0002 "no deletion without archive receipt"). Rollback is
disabling the timers; the receipts stay on disk as historical evidence.

```
systemctl --user disable --now nhms-node27-product-archive.timer
systemctl --user disable --now nhms-node27-storage-inventory-audit.timer
systemctl --user disable --now nhms-node27-timeseries-compression.timer
```

Notes:

- Do **not** delete `~/node27-product-archive-logs/receipt.json` or
  `~/node27-storage-inventory-audit-logs/completeness-receipt.json`; they
  are the historical evidence chain and are consumed byte-for-byte by
  #855 retention.
- ADR 0002 makes archive+delete atomic per-cycle — there is no per-run
  data-level rollback because a completed archive terminal has already
  fsynced a verified pair before source retirement. To roll back a
  specific archived pair back to the object store, follow the salvage
  restore procedure in the (future) `db-export-salvage` runbook section
  or the archive rebuild drill (#5.x).
