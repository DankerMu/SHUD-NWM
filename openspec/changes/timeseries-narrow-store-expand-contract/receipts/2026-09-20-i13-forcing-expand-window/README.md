# I13 / task 8.1 — `000061` forcing expand production window on node-27

**Part 1 of 8.1.** This receipt covers the window itself and everything provable the moment it
closed. The items 8.1 also owes — QHH fallback before/after `EXPLAIN` on the **narrow** leg,
station-series API field identity for a legacy **and a narrow** version, and compression /
retention ticks covering **both** tables — are not in here and cannot be: the narrow table is
empty until production writes into it. They belong to part 2, after the plane has data.

- **Window**: `2026-09-20T00:27:36+08:00` .. `2026-09-20T00:27:54+08:00`
- **Quiet window (display API down)**: `00:27:39` .. `00:27:54` — **15 s**
- **`migrate` itself**: `00:27:42` .. `00:27:46` — **4 s**, `rc=0`, `1 applied, 54 skipped, 55 total`
- **Deployed SHA**: `40153e051`; migration sha256 `b2c32c8fd5ddc2e8ebc0d07dbff627b016f92fc2eb95fc187187efbd26b93991`, pinned in the window script and re-checked after the pull
- **Raw log**: [`window.log`](window.log). **Pre-window gate log**: [`gate-a-40153e051.log`](gate-a-40153e051.log)

## The window was gated on two things, both done before anything irreversible

**Gate A — real-DB pytest on the 2.10.2 oracle, on the SHA that was deployed.**
`PostgreSQL 15.2 … TimescaleDB 2.10.2`, detached worktree at `40153e051` so the live tree and the
running API were untouched. `-m integration` → **247 passed, 25 skipped, 28:47, rc=0**. Collection
was asserted non-zero first (271 integration / 31 `timescaledb_210`) because `tests/conftest.py:217-226`
skips this lane **silently** when the opt-in env is absent — a green run with zero collected tests
would otherwise read as a pass.

Every one of the 25 skips is an explicit opt-in, not a silent one — counted from the `-rs` report
rather than asserted:

- **23** in `tests/test_node27_pgdata_migrate_oracle.py`, which carries
  `[integration, timescaledb_210, node27_docker]` and is gated behind `NHMS_RUN_NODE27_DOCKER=1`,
  whose own reason string is *"run only on the node-27 disposable Docker oracle, never against
  nhms-db"*. A deliberate safety gate about pgdata migration, unrelated to `000061`. So **8 of the
  31 `timescaledb_210` cases ran** on their only oracle.
- **1** `tests/test_basins_registry_import_db.py:302` — the pre-existing `data/Basins` import
  smoke, opt-in.
- **1** `tests/test_loop_log_audit_attribution.py:37` — tracked skill asset absent; that module is
  untracked and gitignored, which CI skips too (already recorded in `tasks.md:336`).

Closing checks: no leftover `nhms_it_%` databases, `schema_migrations WHERE version LIKE '000061%'`
= 0, worktree removed.

An earlier full run on the **parent** `4400f3730` also passed (247/25, 28:47). It is not the gate:
the parent does not contain the step-11 predicate that executes in production.

**Gate B — the step-11 predicate defect, found and fixed before the window (PR #2512).**
The merged `000061` shipped `WHERE EXISTS (…)`. Measured read-only on this primary, the planner
flattens that into a semi-join and picks a `HashAggregate` over an estimated 838 174 864 rows
behind `DecompressChunk` on both compressed chunks — total cost 10 328 441 — inside a single
`DO $$` block that holds `AccessExclusiveLock` from the `RENAME` to the end. The migration's own
lock budget cited receipt §4 as if it described that statement; §4's `existence-probe.sql` puts
`EXISTS` in the **target list**, where `pull_up_sublinks` does not fire, so its numbers never
applied to a `WHERE EXISTS`. Rewritten to a correlated scalar sublink with `LIMIT 1`
(**`LIMIT 1` is correctness** — without it: `ERROR: more than one row returned by a subquery used
as an expression`). Full account in `fixtures/I12-1991.md` **E5**.

The 4 s `migrate` below is the measured consequence.

## Pre-window reconnaissance, all read-only, all confirmed by the window

| checked before | predicted | observed in the window |
|---|---|---|
| pending migration set | exactly `000061` | `1 applied, 54 skipped` |
| routing classification | **legacy 4 289 / narrow 4 592** of 8 881 | **legacy 4 289 / narrow 4 592** |
| three enums vs production vocabulary | value-for-value equal | `{PRCP,TEMP,RH,wind,Rn,Press}` / `{mm/day,degC,0-1,m/s,W/m2,Pa}` / `{ok}` |
| `nhms_display_ro` on the new table | no in-window `GRANT` needed — inherited via `pg_default_acl` for role `nhms` in schema `met`, then re-granted through `OWNER TO` | `relacl = {nhms_ingest_rw=arwdDxt/nhms_ingest_rw,nhms_display_ro=r/nhms_ingest_rw,nhms_download_rw=arwd/nhms_ingest_rw}` |
| lock hygiene on the three target relations | no holders, no `idle in transaction`, no autovacuum; **no native TimescaleDB compression/retention job on this hypertable** (the systemd timers are the mechanism, so stopping them suffices) | gate open, all three empty |
| object names `000061` creates | all five free in `met`; the three enum types absent | migration applied without a name collision |
| lock budget | ~24 s cold / ~3 s warm | **4 s** (warm) |

The classification prediction is the strongest of these: 4 289 / 4 592 was computed read-only
before the window and reproduced exactly by the migration's own `UPDATE`.

## Post-state, verified inside the window on the superuser lane in a READ ONLY session

- ledger head `000061_forcing_station_timeseries_narrow_expand.sql`
- both relations present: `met.forcing_station_timeseries` (narrow) and `…_legacy`
- narrow columns, in order, = `specs/forcing-narrow-store/spec.md:5`, **including `native_resolution text NULL`** — the column a river transcription drops:
  `forcing_version_key`, `station_key`, `valid_time`, `variable_e`, `value`, `unit_e`, `quality_flag_e`, `native_resolution`
- compression `segmentby (forcing_version_key 1, station_key 2)`, `orderby (variable_e 1, valid_time 2)` — matches `supervisor.py:1748-1757`
- chunk interval: narrow **1 day**, legacy **3 days** (000058's setting travelled with the renamed relation, as designed)
- both FKs enforced: `…_forcing_version_key_fkey`, `…_station_key_fkey`
- exactly two indexes: `forcing_station_timeseries_narrow_pkey`, `forcing_ts_version_variable_time_key_idx`
- owner `nhms_ingest_rw`
- `scripts/node27_provision_write_roles.sh` (mandatory after any migrate, runbook 9.6 / #1774): `audit clean`, no owner drift, `CREATE on any schema` = none for both write roles

**`display_ro` readability, checked after the migrate and *before* the API restarted** — a
`permission denied` here would have 500'd the public site on restore:
`SET ROLE nhms_display_ro; SET TRANSACTION READ ONLY;` → narrow **OK**, legacy **OK**, routing
column **OK**, enum USAGE **OK**.

## Read-path evidence

Both probes byte-identical across the window. The forcing probe is the load-bearing one: every
forcing reader previously hard-coded the literal `"legacy"`, so this is the statement about M2
that a river probe cannot make.

| probe | route | result |
|---|---|---|
| forcing | `GET /api/v1/mvp/qhh/latest-product?source=GFS` → `forecast.py:133` → `latest_qhh_display_product` → `forecast_store.py:2086` | **byte-identical**, sha16 `bb690c7fec6c9fcd`, len 4342 — **M2** |
| river | `…/forecast-series?…` (the I9 probe) | **byte-identical**, sha16 `a48605688b2e09c4`, 6075 bytes — **M8** |

Two probes were rejected during reconnaissance and the reasons are recorded so they are not
retried: `/api/v1/met/best-available` returns `[]` on the current window (an empty body is
byte-identical for a broken reader too), and `apps/api/routes/data_sources.py:140` serves station
series from the **object store**, not the database.

The forcing baseline drifted between the pre-window measurement (`1524c97ec9418671`, 2026-09-19)
and the window (`bb690c7fec6c9fcd`) because another cycle landed in between. The script detected
the drift, took a fresh baseline while the API was still up, and compared against that — which is
why the comparison is still a real one. `request_id` is stripped and the JSON key-sorted before
hashing; it changes on every call.

## Restore

- `nhms-display-api.service` `active (running)` from `00:27:48`; `root=200`, `ops=200`
- all **9** `nhms-node27-*` timers restarted (`nhms-node27-resource-governance.service` left in its
  pre-existing `failed` state — **no `reset-failed`**)
- **`yd-display-api.service` MainPID `3163766` before and after — unchanged.** The four
  `yd-node27-*` timers were never touched. ("yd 的不停")
- disk after: `/` 51%, `/home` 33%, `/data/GHDC` 9%

## First pipeline tick after the window — green, but it does NOT yet prove M5

`nhms-node27-autopipe.service` fired at `00:37:48`, ten minutes after the window closed, and was
observed without intervention:

```
[2026-09-19T16:38:28Z] autopipe: done rc=0 elapsed_sec=12
… "refused": 0, "skipped": 0
```

`Result=success`, `ExecMainStatus=0`. **The pipeline stayed green across the expand** — which is
the precondition M5 cares about (`scripts/node27_autopipeline.py:68,214,1128` drives the forcing
handoff apply every tick, so a refusal escalating to a tick failure would redden production
continuously).

**It is not proof of M5, and is not recorded as such.** No refusal occurred, because no
legacy-routed version was re-applied and no new cycle arrived: both sources are still on cycle
`2026-09-18T12:00:00Z`, and `met.forcing_station_timeseries` (narrow) still holds **0 rows**. The
narrow write path and the refusal branch are unexercised in production. Both belong to part 2.

**Observation, reported not diagnosed, and it predates this window.** Forcing cycles arrive on a
12-hourly cadence at 76 versions each, written with roughly 19 h of lag
(`2026-09-18 12:00` → written `2026-09-19 07:48`). The `2026-09-19 00:00` cycle has not appeared.
That gap opened well before the window (last write `2026-09-19 07:48`; window `2026-09-20 00:27`),
the `download` and `frontier-alert` units both last exited `0`, and nothing here was touched to
investigate it. It matters only because **8.1 part 2 cannot start until a new cycle lands** — the
first narrow write is what gives the QHH narrow-leg `EXPLAIN` and the station-series legacy-vs-narrow
identity check something to read.

## State the plane is now in

`met.forcing_station_timeseries` (narrow) holds **0 rows**; `…_legacy` holds all 6 chunks and
~226M rows. Every pre-existing version routes to `legacy` and renders the legacy template, which
is why both probes are byte-identical. Production starts writing narrow on the next cycle.

8.2's entry gate is fourteen daily receipts **and** `legacy_chunks = 0`; at window close
`legacy_chunks = 6`. Nothing about that is accelerable, and compression must **not** be
hand-triggered to manufacture evidence.

## Owed by 8.1, not in this receipt

1. QHH fallback `EXPLAIN (ANALYZE, BUFFERS)` before/after with the coverage-loss list, on the
   **narrow** leg. `scripts/node27_timeseries_compression_live_evidence.py:2881` is river-only and
   `_validate_benchmarks` at `:2912` pins the benchmark names to exactly `["curve","mvt"]`, so
   adding a forcing benchmark is an evidence-contract change — as `tasks.md:308` already assigns
   to 8.1.
2. Station-series API field identity for a legacy **and** a narrow version.
3. Compression and retention ticks covering both tables.
4. Governance receipt.

All four need narrow data. `pg_stat_statements` is **not** installed (`shared_preload_libraries`
is `timescaledb` only) and was not enabled for this — that would need a cluster restart. Per-statement
wall time therefore stays sourced from the I10 receipt's per-step measurements plus this window's
total.

## Carried findings, reported not fixed

- `packages/common/forecast_store.py:4264,4762` — static index-diagnostic payloads still name the
  canonical table while describing indexes now on `_legacy`. Spec assigns index pins to 8.3.
- `nhms-node27-resource-governance.service` in `failed`, pre-existing, untouched.
- The river compression timer's post-contract behaviour is still unverified; next natural tick
  `2026-09-20 12:25`. Must not be hand-triggered.
