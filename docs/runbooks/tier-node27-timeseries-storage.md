# Runbook: Tier Node-27 Timeseries Storage

Operation, rollback, and cadence rationale for the node-27 timeseries storage
tier — **hypertable compression (§4)** and **gated DB retention (§8)** —
delivered under `openspec/changes/tier-node27-timeseries-storage`.

## Retirement record: the cold archive lane is gone (2026-08-11)

This runbook was originally written for a hot/cold tiering design whose cold
side was a product-archive mover, a storage-inventory audit, a DB-export
salvage exporter and a quarterly rebuild drill. After the `/dev/md0`
double-disk failure that tier was **permanently retired**:
[`docs/adr/0002-node27-timeseries-hot-cold-tiering.md`](../adr/0002-node27-timeseries-hot-cold-tiering.md)
**Revision 2026-08-11** records the decision, and #1370 deleted the four
runners, their wrappers, systemd units, env templates and schemas from the
repository.

What that means for an operator today:

- Those sections are **removed from this runbook**, not merely marked stale.
  Their last committed text is recoverable from git history
  (`git log -p -- docs/runbooks/tier-node27-timeseries-storage.md`); nothing
  in it is runnable any more, because the scripts it drove no longer exist.
- Retention runs with **no archive backstop**: `drop_chunks` is irreversible
  and there is no restore lane. The runner accepts exactly one archive-gate
  value — `NODE27_TIMESERIES_RETENTION_ARCHIVE_GATE=disabled` — and refuses
  everything else with `RETENTION_CONFIG_INVALID` (§8).
- The receipts the retired lanes produced stay committed under
  `docs/runbooks/receipts/tier-node27-timeseries-storage/**` as immutable
  historical evidence. Read them as history; none can be regenerated.
- **Section numbers are deliberately non-contiguous** (§4 is followed by §8).
  The surviving numbers are load-bearing: §8.x anchors are cited by the
  retention runner's tests, its env template and the ADR, so the retired §3
  and §7 slots stay empty rather than being reused.

Current policy (effective 2026-07-21): DB retention uses a spec-default
14-day eligibility window. That default is what the committed env template
ships (`NODE27_TIMESERIES_RETENTION_WINDOW_DAYS`); the value that actually
runs is whatever the machine's env file holds — node-27's DB retention window
was `NODE27_TIMESERIES_RETENTION_WINDOW_DAYS=21` as of 2026-08-01. Read the
live value on the box before quoting a day count anywhere (§8.1 step 2 shows
the grep). Compression remains earlier at 7 days. Both ages are measured from
the latest forecast cycle accepted by the node-27 display catalog, not from
the server wall clock. The historical 30/45-day receipts in the committed
evidence tree are audit evidence only, not commands for new runs.

The shared business-time watermark is the UTC result of `MAX(cycle_time)` over
`hydro.hydro_run` forecast rows in `succeeded`, `parsed`, or `published` state.
Every lifecycle runner opens a bounded read-only query before selection and
fails closed when that watermark is absent or unreadable; it MUST NOT fall back
to `datetime.now()`. Wall time remains the receipt generation/freshness clock.
For example, with display watermark `2026-07-11T12:00:00Z`, compression cutoff
is `2026-07-04T12:00:00Z` and DB-retention cutoff is `2026-06-27T12:00:00Z`
even if the host date is later.

- Design record: `openspec/changes/tier-node27-timeseries-storage/design.md`
  (frozen; its cold-tier fixtures describe the retired lane)
- Architecture record: `docs/adr/0002-node27-timeseries-hot-cold-tiering.md`
- Display carve-out: `docs/adr/0001-display-timeseries-carveout.md`

## Recorded exception (2026-08-06): the `ghdc` tablespace shares `/dev/md0`

Part of the database lives on `/dev/md0`, the filesystem that also carried the
now-retired cold archive tier (retirement record above). This is a knowing,
recorded exception to ADR 0002's rule that the hot tier and that device stay
separate — read this before touching either.

| Tablespace | Host path | Container path | Device |
|---|---|---|---|
| `pg_default` | `/home/nwm/nhms-pgdata` | `/home/postgres/pgdata/data` | `/dev/mapper/ubuntu--vg-home` (1.7 TB, shared with the object store) |
| `ghdc` | `/data/GHDC/nwm-archive/nhms-tablespace` | `/home/postgres/pgdata/tablespaces/ghdc` | `/dev/md0` (15 TB) — **also carries `/data/GHDC/nwm-archive`** |

`ghdc` holds `hydro.river_timeseries` chunks `_hyper_3_10_chunk` and
`_hyper_3_14_chunk`, `met.forcing_station_timeseries` chunks
`_hyper_1_12_chunk` and `_hyper_1_13_chunk`, and all 18 of their indexes —
roughly 502 GB decompressed.

**Why.** The six-basin production replay (#1164) had to decompress those four
chunks to lift the compressed-chunk write guard, and `/home` had only ~357 GB
free against a ~502 GB requirement. No sequencing avoided it: each backfilled
cycle's forecast window spans both the 07-02…07-09 and 07-09…07-16 chunks, so
both must be uncompressed at once. On `/data/GHDC` the only `nwm`-writable
location is `nwm-archive/`; everything else under that mount is `root` or
`ghdcadmin` owned, and provisioning a sibling mount point needs root.

**The risk this re-introduces.** The 2026-07-26 deadlock (a full archive
filesystem freezing the only lane able to free it) died with the mover, but so
did that lane's telemetry: since #1370 no runner measures `/dev/md0` at all
and the governance receipt no longer reports it (below). DB growth on the
`ghdc` tablespace is therefore silent until somebody runs `df` — and the
device is shared with `root`/`ghdcadmin` trees that grow independently of
NHMS.

**Required mitigations while the exception stands:**

- **Nothing polices free space on that device any more.** The mover's
  warn/refuse watermarks were deleted with the mover (#1370), and PostgreSQL
  enforces no tablespace quota, so no threshold anywhere reserves or defends
  bytes for `ghdc`. Treat every `/dev/md0` capacity statement as
  operator-measured until a replacement observation lane exists.
- **Bound the tablespace's working set.** That is the only lever that
  actually protects `/dev/md0`: re-compress promptly after a decompress, and
  never hold more than the chunks you are actively reingesting in
  uncompressed form.
- **Read both devices, every time.** `df -h /home /data/GHDC`. See the
  capacity caveat below for exactly what the governance receipt does and does
  not tell you.
- **Do not treat headroom as permanent.** 15 TB with ~19 GB of archive and
  ~502 GB of tablespace is comfortable today; that is the reason the exception
  was accepted, not a reason it is safe indefinitely. `/dev/md0` is also not
  NHMS-exclusive — `/data/GHDC` carries `root`- and `ghdcadmin`-owned trees
  (~1 TB was already in use at the 2026-07-26 migration), so free space can
  fall without any NHMS growth at all. Revisit when either tier changes shape
  or when third-party usage moves, and prefer a dedicated filesystem for
  `ghdc` the next time root-level provisioning is available on node-27.

**What the governance receipt actually shows (read this before quoting it).**
`scripts/node27_resource_governance.py`:

- **Does NOT report `/dev/md0` at all.** The receipt's archive-root block and
  its free-space band were removed with the archive lane (#1370), so current
  receipts carry no such key and no free-space recommendation fires for that
  device. Receipts generated before 2026-08-14 still carry the block — read
  them as history, not as a live alarm.
- **Does not** list `/data/GHDC` in the receipt's `filesystems` block either —
  `collect_filesystem()` enumerates only `/`, `/home`, the repo filesystem
  and the object-store filesystem, and the `df -ih` inode check covers only
  `/` and `/home`.
- **Under-reports the DB.** `path_sizes["pgdata_root"]` is a `du` of
  `NODE27_GOVERNANCE_PGDATA_ROOT` (`/home/nwm/nhms-pgdata`) only, so the ~502 GB
  that moved to `ghdc` vanished from the reported DB footprint with nothing
  deleted. Do not read that drop as retention succeeding.

The only measurement of that device is therefore manual: `df -h /data/GHDC`
for headroom, and `du -s --exclude=nhms-tablespace /data/GHDC/nwm-archive` to
separate the retired archive's residue from the live tablespace. Quote both,
or quote neither.

No open tracker owns this caliber question: #1290 (governance-receipt capacity
caliber) and #1309 (the `/dev/md0` double-disk failure) are both CLOSED and are
historical records only — cite them as background, not as work in flight.

**Establishing the tablespace for the first time** (DR from scratch, or a
brand-new tablespace). Order matters — the container must carry the mount
*before* `CREATE TABLESPACE` runs, because the `LOCATION` is a container
path:

1. Host directory, empty, owned by the container's uid/gid:

   ```bash
   mkdir -p /data/GHDC/nwm-archive/nhms-tablespace
   chown nwm:nwm /data/GHDC/nwm-archive/nhms-tablespace
   chmod 0700 /data/GHDC/nwm-archive/nhms-tablespace
   ```

2. Recreate the container with the bind mount — §4.3.3 below.

3. Create the tablespace (superuser; the target directory must be empty):

   ```sql
   CREATE TABLESPACE ghdc LOCATION '/home/postgres/pgdata/tablespaces/ghdc';
   ```

4. Move chunks and **their indexes** into it — see §4.3.2 step 3a for the
   `format(%I)` + `\gexec` form.

**Relocating the existing tablespace to another filesystem** (the promised
move to a dedicated device) is a *different* procedure — the 502 GB must move
with it, and `CREATE TABLESPACE` must NOT run (the tablespace already exists;
PostgreSQL resolves it through the `pg_tblspc` symlink, which lands wherever
the container mount points). Recreating the container with an empty new
directory bound to the same container path would start a cluster whose
`ghdc` chunks are simply gone — and PostgreSQL would happily write new files
into the empty directory, splitting the tablespace across two host paths.

1. Stop the container cleanly (`docker stop -t 300 nhms-db` — §4.3.3 step 2
   discipline, plus the step 1 timer quiesce).
2. Copy the old host directory to the new device, preserving everything:

   ```bash
   rsync -aHAX --numeric-ids \
     /data/GHDC/nwm-archive/nhms-tablespace/ /new/device/nhms-tablespace/
   diff <(cd /data/GHDC/nwm-archive/nhms-tablespace && find . -type f | sort) \
        <(cd /new/device/nhms-tablespace && find . -type f | sort)
   du -sb /data/GHDC/nwm-archive/nhms-tablespace /new/device/nhms-tablespace
   ```

   File list identical, byte totals equal, ownership `nwm:nwm`, mode `0700`.
3. Recreate the container per §4.3.3 with the **new** host path bound to the
   **same** container path `/home/postgres/pgdata/tablespaces/ghdc`.
4. Verify with the real chunk read from §4.3.3 step 4(a). Only after that
   passes, retire the old directory.

Once a second tablespace exists, any file-level backup or restore must cover
the `pg_tblspc` link targets as well as `PGDATA`; a `PGDATA`-only copy is no
longer a complete backup.

**Tablespace residency is per chunk and is not part of the schema.** Nothing
under `db/` or `packages/common/migrate.py` references a tablespace; `ghdc` is
a node-27 physical placement only. Local dev, CI and
`infra/docker-compose.dev.yml` are unaffected and must NOT gain a `ghdc`
mount. Resolve a chunk's residency before acting on it:

```sql
SELECT c.relname,
       COALESCE(t.spcname, 'pg_default') AS tablespace
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
LEFT JOIN pg_tablespace t ON t.oid = c.reltablespace
WHERE n.nspname = '_timescaledb_internal'
  AND c.relname = '<chunk_name>';
```

## Install (node-27, `nwm` user)

All operations run as the `nwm` user under systemd `--user`. Do NOT install
system-level (root) units for this tier. Two lanes remain installable:

- terminal-chunk compression — §4 (§4.0 covers the controlled first run);
- gated DB retention — §8.1.

Each owns its env file, log directory and unit pair, so there is no shared
install step left. The retired archive lane's install and operation procedure
was deleted with the lane (retirement record at the top of this runbook); its
units, wrappers and env templates no longer exist in the repository, and
node-27 cleanup of any leftover installed units is the one-off step recorded
in #1370's live evidence, not a recurring procedure.

## Timer cadence order (UTC)

| Order | Timer                                        | OnCalendar         | Rationale |
|-------|----------------------------------------------|--------------------|-----------|
| 1     | `nhms-node27-resource-governance.timer`      | `04:10:00 UTC` daily | Governance audit captures unit/timer state and filesystem headroom before the mutating lanes run. |
| 2     | `nhms-node27-timeseries-compression.timer`   | `04:25:00 UTC` daily | Terminal-chunk compression runs after governance so the previous-day receipt is already captured. |
| 3     | `nhms-node27-timeseries-retention.timer`     | `05:15:00 UTC` daily | Irreversible `drop_chunks`; runs last so the day's compression work and the governance snapshot both precede it. |

The ordering is an evidence-ordering and quiet-window choice, not a gate.
Retention consults no receipt produced by the other two timers — the
archive-completeness freshness dependency that once ordered this table died
with the archive lane (#1370). Check what is actually enabled on the box with
`systemctl --user list-timers` before relying on the table.

### Live-state notes (verified 2026-08-01)

Deployed env files live at `/home/nwm/NWM/infra/env/*.env` (gitignored, mode
0600). Read them on the box before quoting any value; these are the deltas
against the committed `.example` templates as of 2026-08-01:

- **Compression per-tick bound.** No longer a drift: the committed template
  and the deployed env both carry `=4` since issue #1237 decided it as a
  capacity target (the box already ran `=4`; the template's stale `=5` was
  the side that moved). Still read the live value off the box before quoting
  it. See §4 "Per-tick capacity (live state 2026-08-14, decided in #1237)".
- **Compression chunk-selection lag.**
  `NODE27_TIMESERIES_COMPRESSION_LAG_SECONDS` reads `172800` (2 days) on the
  box (re-confirmed 2026-08-14) while the committed template ships `604800`
  (7 days). This gap is a **recorded decision, not drift**: the 2026-08-07
  short-lag regime taken after the md0 outage left `/home` carrying the whole
  uncompressed steady state alone (backup `*.bak-lag7d-20260807`; rollback
  condition = md0 recovery restoring a separate device for large chunks). The
  regime, its peak-space arithmetic and the mandatory decompress-first rule
  for outage recovery are §4.3.2.1 — read that section before touching the
  value. #1237 neither judged nor changed the lag.
- **DB retention timer.** Not enabled as of the 2026-08-01 verification;
  enabled on 2026-08-14 (issue #1369 operator decision) at the committed
  05:15 UTC daily cadence with the archive gate `disabled` — since #1370 the
  only value the runner accepts. That bringup's four live receipts ARE
  committed under
  `docs/runbooks/receipts/tier-node27-timeseries-storage/timeseries-retention/`
  (`retention-dryrun-20260814T095619Z.json`,
  `retention-enforce-20260814T095746Z.json`, and the two wrapper receipts
  `retention-20260814T095802Z.json` / `retention-20260814T095832Z.json`).
  As always, re-verify with `systemctl --user list-timers` before quoting the
  live state. See §8.1 "Current bringup state".
- **Retired-lane residue.** The archive tier's directories under
  `/data/GHDC/nwm-archive` and its per-lane log roots under `/home/nwm` are
  no longer written by anything. Removing them is optional operator cleanup
  with no procedure in this runbook; the committed receipts under
  `docs/runbooks/receipts/tier-node27-timeseries-storage/` are the retained
  evidence and stay in the repository.

## 4. Hypertable compression

Native TimescaleDB compression is the sole mechanism this milestone applies
to shrink the two hot hypertables (`hydro.river_timeseries` and
`met.forcing_station_timeseries`). Compression is applied to terminal chunks
only (age older than the configurable lag, default 7 d) by the receipted
runner (`scripts/node27_timeseries_compression.py`, `#851`), never to the
active write-target chunk. This section covers the fail-closed write guard
and the manual decompress procedure that pairs with it.

### Per-tick capacity (live state 2026-08-14, decided in #1237)

`NODE27_TIMESERIES_COMPRESSION_PER_TICK_BOUND` caps how many chunks one timer
tick compresses. **The decided value is 4** — a capacity target derived from
the measured inputs below, not an arbitrary default and not an after-the-fact
blessing of a July retune. Issue #1237 is the decision record. The committed
template (`infra/env/node27-timeseries-compression.example`) now ships `=4`
and a test pins that exact assignment line; the deployed
`/home/nwm/NWM/infra/env/node27-timeseries-compression.env` already ran `=4`
(verified on the box 2026-08-01 and 2026-08-14), so the decision moved the
template only and changed nothing on node-27. The former `5` vs `4`
template/live drift is resolved.

**The relation is dual — both constraints are mandatory.**

1. **Throughput.** `bound × 1 tick/day ≥ steady arrival of 2 terminal
   chunks/week`. The timer fires **once daily** (`OnCalendar=*-*-* 04:25:00
   UTC`, `infra/systemd/nhms-node27-timeseries-compression.timer:5`), so
   `bound=4` buys 28 chunk-slots/week against an arrival of 2 — **14×
   headroom**.
2. **Wall.** `Σ(selected chunk GB × 6.0 s) + ~380 s ≤
   NODE27_TIMESERIES_COMPRESSION_WRAPPER_WALL_SECONDS` (`3900`, the live
   default from `#1156`/`#1352`, no override installed). That wall bounds the
   **whole tick**, not one chunk, and the runner has no in-loop elapsed guard:
   overrunning it means the wrapper's `timeout` sends `TERM` mid-DDL.

   The `380 s` is the **measured 2026-08-14 non-compress residual**: the
   observed 1836 s tick minus the 1456 s the §4.5 estimator gives for that
   tick's pair. Note that §4.5's own "roughly 300 seconds of non-compress
   budget" is a *worst-case estimate* assembled from the per-leg timeout caps,
   and the 2026-08-14 measurement already exceeds it — use `380 s` here, and
   read the §4.5 number as an estimate the box has outgrown, not as a ceiling.
   Part of the residual also scales with chunk *count* (§4.5's enumeration
   includes per-chunk legs — a size measurement before **and** after each
   compress), so treat `380 s` as a floor for ticks selecting more than the
   measured pair, and the `≥281 GB` onset threshold below as correspondingly
   optimistic.

Measured inputs (node-27, read-only, 2026-08-14 unless noted):

- **Arrival rate = 2/week.** Both hot hypertables use a 7-day
  `chunk_time_interval` with aligned range boundaries
  (`timescaledb_information.dimensions`; census 5 river / 6 forcing chunks),
  so two terminal chunks become eligible on the **same day, once a week**. The
  2026-08-14T04:25 tick receipt compressed exactly that pair
  (230 GB + 12.7 GB) in 30m36s — **1836 s measured**. The §4.5 estimator *as
  written* is `GB × 6.0 s` with no overhead term and predicts 1456 s for that
  pair, so the tick ran 380 s longer than the recipe. The `~380 s` residual in
  the wall constraint above is **back-solved from this single observation**
  (1836 − 1456); it is not an independent measurement of the non-compress
  legs, and a second measured pair should be used to confirm or replace it.
- **Backlog ceiling ≤6 chunks — CONDITIONAL, not physical.** The DB retention
  window bounds uncompressed stock at ≤3 per table, hence ≤6 for the lane. The
  window in force is the **live 21 days**, not the committed template's 14
  (that drift is recorded in "Current policy (effective 2026-07-21)" near the
  top of this runbook, the `NODE27_TIMESERIES_RETENTION_WINDOW_DAYS`
  paragraph). The retention timer was only enabled on 2026-08-14 (`#1369`), so
  the convergence to the steady stock is not yet observed. **If that timer is
  disabled or the window is changed, this premise fails** and the bound must
  be re-derived from the formula above.
- **Per-chunk cost.** Steady-state `hydro.river_timeseries` chunks measure
  268–409 GB → 1608–2454 s each at ~6.0 s/GB (§4.5). Therefore `3 river + 1
  forcing ≈ 3 × (1608…2454) + 76 + 380 = 5280–7818 s`, which **exceeds the
  3900 s wall** outright.
- **Live value was already 4**, so no operational change accompanies this
  decision.
- **Chunk count is decoupled from ingest volume.** Chunks are cut on the time
  dimension (7 d), so doubling ingest makes terminal chunks *bigger*, not more
  numerous. Growth pressure lands entirely on the per-chunk timeout budget
  (`#1156`/`#1352`), never on this bound.

**`bound=4` is a throughput ceiling, not redeemable single-tick capacity.** At
river chunk sizes the wall admits roughly **≤2 chunks in one tick**; the
weekly pair (1 river + 1 forcing ≈ 1836 s) clears it comfortably. Do not read
"4" as "four river chunks in one tick" — that selection dies on the wall.

**Catch-up does not depend on this bound.** Draining a real backlog is §4.5
"大 chunk 追赶": set `PER_TICK_BOUND=1` and raise the timeout/wall triple
(systemd drop-in first), one chunk per tick. Raising the bound is not a
catch-up tool and never was.

**No timer frequency change is required.** The main argument is the
decoupling above — a second daily tick would find no extra terminal chunks to
compress, because arrival is set by the 7-day chunk width, not by ingest
volume. The 14× throughput headroom is the secondary confirmation. No cadence
follow-up issue is opened.

Why the bound matters at all: uncompressed chunks piling up on the hot tier
was one of the two inputs to the 2026-07-25/26 `/home`-full incident (the
other being the archive root sharing the hot filesystem — see
`docs/adr/0002-node27-timeseries-hot-cold-tiering.md` "Amendment
(2026-07-26)").

**A backlog by itself invalidates the wall constraint — no config has to
change.** Selection is table-major
(`scripts/node27_timeseries_compression.py:396` orders by
`hypertable_schema, hypertable_name, range_end`, so `hydro` sorts before
`met` and **every** eligible river chunk is taken before any forcing chunk),
so at `bound=4` any unattended tick holding **≥2 eligible river chunks** can
overrun the 3900 s wall while every chunk is still inside the normal
268–409 GB band: `2 river + 2 forcing` already exceeds it once river chunks
reach ≥281 GB. Detection must not wait for the receipt: a wall-`TERM`ed tick
writes **no receipt at all**, and `deferred` is empty in the receipts it does
write, so "`deferred` is non-empty" is **not** a signal for this. The signal
is the *state* — a tick would select ≥2 river chunks, i.e. roughly ≥1 week of
missed ticks (a §4.5 stop+mask override window, a §4.3.2 decompress pause, or
an outage). What an operator can actually check: the unit went `failed`
(`systemctl --user status` / `journalctl --user -u`; `rc=124` is the wall `TERM` — this is the
authority), **or** the receipt at
`NODE27_TIMESERIES_COMPRESSION_RECEIPT_PATH` is **stale** — its `generated_at`
predates the last timer trigger (`systemctl --user list-timers`). Receipt
*absence* is not a checkable signal: the path is a single shared file each
tick overwrites in place (see the §4.0 shared-receipt note below), so a
`TERM`ed tick leaves the previous clean receipt sitting at the path.
**In that state, set `NODE27_TIMESERIES_COMPRESSION_PER_TICK_BOUND=1` per
§4.5 before restarting the timer**, and let the daily timer drain one chunk
per tick.

**Invalidation conditions.** Re-derive from the two constraints above when any
of these change:

- `chunk_time_interval` on either hot hypertable (moves the arrival rate).
- A third hot hypertable joins the lane (moves the arrival rate).
- The DB retention timer is disabled, or its window changes (kills the ≤6
  backlog ceiling premise).
- The wrapper wall / timeout budget changes, including a temporary §4.5
  override window (moves the wall constraint).
- **A backlog forms — ≥2 river chunks eligible in one tick** (paragraph
  above). This one is a *state*, not a config change: it does not move the
  derivation, it defeats the wall constraint at `bound=4` directly. Do not
  re-derive — drop to `PER_TICK_BOUND=1` per §4.5 before the timer runs again.
- Chunk boundaries de-align between the two hypertables. This spreads the two
  weekly terminals over different days and therefore **reduces** per-tick
  load — a safe direction, listed here only so it is not misread as a
  regression.

When retuning, read the live value off the box first and record the new value
with the receipt that justified it.

### 4.0 Controlled initial live run (`#1069`)

The first production compression is a one-chunk controlled operation, not a
normal timer tick. The recurring
`nhms-node27-timeseries-compression.service` remains the bounded wrapper
`--enforce` lane used only by its timer and never consumes the one-off replay
plan. A qualifying replay is instead owned by the separate no-timer
`nhms-node27-timeseries-compression-replay.service`. Its `ExecStart` pins the
reviewed run-plan, ledger, terminal and run-scoped finalizer-state paths; its
`ExecStopPost --finalize-only` is the outer CAS backstop. Direct operator invocation of the
legacy runner wrapper **without** `--enforce` remains dry-run. A contended wrapper
publishes a mode-0600, schema-valid `outcome=refused_lock` receipt with empty
`selected`/`deferred`/`skipped`, null/zero totals, no DB call, and a redacted
stderr diagnostic; this deliberately replaces a stale shared receipt.

Run this sequence only from an ff-only-synchronized, tracked-clean node-27
worktree whose SHA is the reviewed #1069 head. The supervisor and verifier both
bind the terminal to the authorization-pinned lineage by exact equality: they
`git rev-parse --verify refs/remotes/origin/feat/issue-1069-live-compression`
and require it to equal the immutable `mutation_head_sha`. That remote-tracking
ref advances only on `git fetch` (a `git pull --ff-only` fetches first, so it
suffices) — a worktree checked out to the right SHA by any means that leaves the
remote ref stale still fails provenance. This is fail-closed and the replay is
one-shot, so **before starting the replay service, fetch node-27 to the exact
head and confirm the remote ref resolves to it**:

```bash
cd /home/nwm/NWM && git status --porcelain   # must be clean of tracked changes
git fetch origin feat/issue-1069-live-compression
git rev-parse --verify refs/remotes/origin/feat/issue-1069-live-compression
# ^ must equal the run plan's mutation_head_sha before the service starts.
```

Never `git stash pop` and never touch the gitignored `.nhms-issue*-live/`
evidence directories during the sync. Keep all generated evidence under
`/home/nwm/NWM/.nhms-issue1069-live/` mode 0600. Never print or commit the
writer password/full DSN, run shell tracing, dump the environment, or place a
credential in process argv.

1. Capture preflight JSON binding node-27, repository path/SHA, UTC time,
   PostgreSQL/TimescaleDB versions, `dbname=nhms`, instance
   `node27-primary-pg15`, container/service state, exact pre-run unit state,
   and the three deployed #852 write-guard sites. Source the existing ingest
   writer credential into the canonical untracked
   `infra/env/node27-timeseries-compression.env` and require mode 0600. The
   evidence records only host/port/dbname/current user, redacted connection
   identity, and privilege booleans.
2. Record the role truth exactly: `current_user=nhms`, `rolsuper=true`,
   `rolcreaterole=true`, `rolcreatedb=true`, ownership of both target
   hypertables, and EXECUTE on installed
   `compress_chunk(regclass,boolean)`. Do not call it least privilege. Do not
   create/alter/grant a role and do not use `nhms_display_ro`.
3. Before migration, write a custom-format schema-only `pg_dump`. Retain its
   descriptor identity. The supervisor resolves PG15 `pg_restore` inside the
   `nhms-db` container (the host has no `pg_restore`) and streams
   `--version`/`--list` under independent stdout/stderr ceilings. Record the
   container image ID, binary realpath/version/hash, concrete argv, dump
   descriptor digest, exit 0 and bounded output identities.
   Also capture timestamped canonical JSON for the two target tables' exact
   pre-migration catalog. This dump is forensic DDL inventory, not a data
   backup, restore drill, or compressed-storage rollback.

   Before the live replay, run the read-only host-contract dry-probe, so a
   drifted image/realpath/systemd/PG version is caught here rather than burning
   the one-shot replay window at preflight. Until a timer or CI workflow is
   installed (operator-gated, §4.4), **this step is the SOLE pre-mutation
   interception point** for external-contract drift. Run the committed probe
   FIRST and continue only on exit 0:

   ```bash
   set -a; . infra/env/node27-timeseries-compression-replay.env; set +a
   export XDG_RUNTIME_DIR=/run/user/$(id -u)
   uv run python scripts/node27_external_contract_snapshot.py --check; echo "exit=$?"
   # exit 0 -> continue.  Any non-zero exit stops the run: see §4.4 for the
   # exit-code table (2 usage / 3 drift / 4 misalignment / 5 probe failure)
   # and the drift-handling loop.  Never "just update the fixture" here.
   ```

   If the script is unavailable (e.g. an older checkout), fall back to the
   manual trio it replaced — it covers the container leg only, so a systemd /
   server-version drift stays undetected until preflight:

   ```bash
   docker inspect --format='{{.Image}}' nhms-db          # -> sha256:...
   docker exec nhms-db /usr/bin/readlink -f /usr/bin/pg_restore
                                                         # -> /usr/share/postgresql-common/pg_wrapper
   docker exec nhms-db /usr/bin/pg_restore --version     # -> pg_restore (PostgreSQL) 15.2
   ```

   `readlink -f /usr/bin/pg_restore` resolves to the `pg_wrapper` dispatcher,
   NOT `/usr/bin/pg_restore` (which is a symlink to it); the supervisor binds
   that wrapper realpath plus its sha256 and the image ID, and binds the dump
   descriptor digest at run time against the freshly written schema dump. Stop
   before live execution if the realpath, image, or version differ.
4. Capture the original autopipe/compression timer+service enabled/active/sub,
   `MainPID`, result and bounded journal. Stop only the autopipe timer. Require
   `MainPID=0`, no activating/running autopipe process, and no live writer or
   conflicting lock on either target/chosen chunk. A pre-existing failed
   autopipe service with `MainPID=0` is preserved; do not `reset-failed` to
   manufacture a clean state.
5. Apply `db/migrations/000047_hypertable_compression_settings.sql` with
   `ON_ERROR_STOP=1`. Only after exit 0, apply the same file a second time.
   The two canonical post-apply catalog documents must be byte-identical and
   must contain exactly D3's indexed segment/order columns, both hypertables
   compression-enabled, and no compression-policy job. A nonzero first apply
   stops the run; repairing partial DDL is separately authorized.
6. Create this lane's own log directory FIRST — no shared install step creates
   it, and neither unit can create it for itself:

   ```bash
   mkdir -p ~/node27-timeseries-compression-logs
   ```

   Then install the committed recurring service/timer and replay service byte-for-byte under
   `~/.config/systemd/user/`, verify both file hashes, `daemon-reload`. The replay
   unit refuses to start — a clean systemd condition failure — until the run-plan,
   the replay env, AND the `~/node27-timeseries-compression-logs` directory
   created at the start of this step all exist: systemd must open the
   `StandardOutput=append:` log targets BEFORE
   `ExecStartPre` runs, so the log directory can never be created by the unit itself
   (measured on node-27 as `status=209/STDOUT` without the guard). Then run
   `systemctl --user enable nhms-node27-timeseries-compression.timer` **without
   `--now`**. Require `is-enabled=enabled` while timer and service stay
   inactive throughout this issue. Copy
   `infra/env/node27-timeseries-compression-replay.example` to the untracked
   mode-0600 replay env, replace its placeholders, and verify the reviewed
   run-plan plus expected stale-terminal digest before starting the no-timer
   replay service exactly once. The run-plan is not hand-authored: emit it with
   the committed author, whose ten command argvs match the supervisor's
   exact-argv contract and whose twelve captures are real invocations of the
   committed read-only capture-producer (each producing a verifier-content-valid
   evidence document — never a placeholder). Author it, pin its digest into the
   replay env `NODE27_COMPRESSION_RUN_PLAN_SHA256`, and place it at the pinned
   path:

   ```bash
   .venv/bin/python -m scripts.node27_timeseries_compression_plan_author \
     --mutation-head-sha "$(git rev-parse --verify \
       refs/remotes/origin/feat/issue-1069-live-compression)" \
     --output /home/nwm/node27-timeseries-compression-replay/run-plan.json
   # prints run_plan_id + sha256; the printed sha256 is the run-plan digest pin.
   ```

   The command above uses the canonical defaults and is unaffected, but any custom
   `--root`, `--repo` or `--schema-dump-host` must be a canonical absolute path — no
   trailing slash, no duplicate or dot segments; the plan author refuses anything else
   (the verifier compares recorded plan paths verbatim, so a non-canonical root or host
   dump path would author a plan whose bundle fails verification later with an unrelated
   message, and a `..` component aborts inside the one-shot replay window instead).
   `--schema-dump-container` is deliberately **not** canonicality-guarded at authoring
   time: it is a container-internal path checked only by verbatim-symmetric textual
   comparisons — the verifier's containment/shape argv gates, a whole-argv
   exact-equality gate, and on the supervisor side the mirror gate, the pre-spawn
   capture-argv gate and verbatim argv-tail extraction — so it cannot produce that
   false refusal. Those gates do judge containment in the pinned container dump path
   prefix `/var/lib/postgresql/` (that prefix plus no `..` component, one shared
   predicate in `packages/common/node27_container_contract.py`), so a traversal spelling
   such as `/var/lib/postgresql/../../../etc/shadow` is refused — textually, without
   rewriting the recorded value.

   Its own active state and `MainPID` are expected
   while every checkpoint still proves the recurring service/timer inactive.
   `Persistent=true` means starting the timer
   can catch up the missed 04:25 event and create an unauthorized second batch.
   Before the live start, retain one real JSON journal sample from both user
   units and inspect its field shape:

   ```bash
   journalctl --user \
     --user-unit=nhms-node27-timeseries-compression.service \
     --user-unit=nhms-node27-timeseries-compression-replay.service \
     --output=json --no-pager --lines=50 > /tmp/issue1069-user-journal-shape.jsonl
   jq -c '{_SYSTEMD_USER_UNIT,USER_UNIT,_SYSTEMD_UNIT,UNIT,
     _SYSTEMD_INVOCATION_ID,INVOCATION_ID}' \
     /tmp/issue1069-user-journal-shape.jsonl
   ```

   Inspect `_SYSTEMD_USER_UNIT` and `USER_UNIT` independently. Either field may
   exactly identify the governed child, so `_SYSTEMD_USER_UNIT=init.scope`
   cannot hide a governed `USER_UNIT`. If they name different governed units,
   stop on the conflict. `_SYSTEMD_UNIT=user@<uid>.service` is user-manager
   context, not the child service. Only records lacking both user-unit fields
   may use an exact, unambiguous governed `_SYSTEMD_UNIT`/`UNIT` fallback. Stop
   before live execution if the host journal cannot satisfy this contract.
7. Independently reproduce the runner's exact catalog predicate/order with
   lag 604800 and bound 1. Freeze compact sorted JSON for the selected identity
   tuple `(hypertable_schema, hypertable_name, chunk_schema, chunk_name,
   range_start, range_end)` and its sha256. The selection must be one terminal
   `hydro.river_timeseries` chunk, more than ten minutes outside the cutoff,
   `pg_total_relation_size <= 8589934592`, with at least 322122547200 free
   filesystem bytes. Stop on any mismatch.
8. Invoke the wrapper once without `--enforce` using a task-specific receipt.
   Require a clean dry-run, exact bound-1 tuple, every `after_bytes=null`, no
   catalog mutation and no service activation. Immediately repeat the
   independent selector query and require the same selector hash.
9. The sole authorized mutation is one direct wrapper invocation with literal
   `--enforce`, the same env/lag/bound/lock, a distinct receipt, and an external
   900-second timeout. Do not use the timer or call `compress_chunk` manually.
   A timeout, partial result, scope mismatch or null/error `after_bytes` is
   terminal failed evidence and does not authorize a retry.
   **Scope of this ban:** it governs this one-shot gated first-enforce
   evidence protocol only. Incident catch-up on an already-live lane is a
   different situation with its own ordered procedure — see §4.5
   "大 chunk 追赶". Neither section overrides the other: §4.0 stays the
   forensic first-enforce contract, §4.5 stays the catch-up contract, and a
   manual `compress_chunk` remains banned here outright and is the documented
   last resort there.
10. Capture both-table pre/post snapshots with `hypertable_size(regclass)`
    (acceptance size), parent `pg_total_relation_size` (diagnostic only),
    compressed/uncompressed counts, and compressed sibling names/sizes. One
    selected chunk must become compressed; selected and combined hypertable
    bytes must decrease. It is truthful and expected that the met table can
    remain settings-only with compressed count zero in this bounded batch.
    On node-27's TimescaleDB 2.10.2, resolve the sibling by joining origin and
    sibling rows in `_timescaledb_catalog.chunk` through
    `origin.compressed_chunk_id`; the 2.10 information view does not expose
    `compressed_chunk_schema` or `compressed_chunk_name` columns.
    The receipt and post snapshot are separate measurement instants:
    `pg_total_relation_size` includes one-page FSM/VM growth. Require exact
    sibling identity, both measurements below the origin size, and at most
    1 MiB absolute receipt-to-snapshot drift; do not require byte identity or
    rerun compression to chase an 8 KiB auxiliary-page change.

The representative performance proof uses production query construction, not
handwritten lookalikes. For the selected hydro chunk, freeze a nonempty
production-valid `q_down` identity. Curve capture calls the public
`PsycopgForecastStore.forecast_series`, records the exact statement/params
sent by that production owner, and hashes
`packages/common/forecast_store.py`. MVT capture imports
`postgis_tile_sql("hydro")`, uses the same parameter construction as
`hydro_display._postgis_tile_params` at deterministic z=9, and hashes both
source files. Curve result bytes are compact sorted UTF-8 JSON plus a trailing
newline; MVT result bytes are recorded as hex and hashed as decoded raw bytea.

For each query and each phase, use a new read-only connection: retain the first
execution as cold-biased information, perform two warmups (up to five while
reads remain), then record exactly seven `EXPLAIN (ANALYZE, BUFFERS, VERBOSE,
FORMAT JSON)` samples. Before/after cache classes must match. The median is sorted
sample 4; p95 is sample 7. Gates are
`after_median <= max(1.5*before_median, before_median+100)` and
`after_p95 <= max(2*before_p95, before_p95+250)`. Result rows/bytes/hash must be
identical, concurrent-load sampling stable, and each after plan must contain a
`Custom Scan` whose normalized provider is exactly `DecompressChunk` and whose
own nonempty `Schema`, `Relation Name`, and `Alias` exactly bind the selected
origin or compressed sibling on the same node. Filter text, suffixes and
child-node relations do not qualify. The seven-day curve is an overlap probe: its half-open request
must overlap the selected half-open chunk. A request starting at the chunk's
exclusive end, or otherwise wholly outside it, fails.

Use the committed read-only capture helper; do not recreate the benchmark with
ad-hoc SQL or JSON. It accepts the DB credential only through the environment,
derives both statements/binds from production source, and writes mode-0600
artifacts. Both queries, both read-only connections, every statement/activity
probe and result fetch share one absolute 900-second monotonic deadline;
connections use `connect_timeout=10`, statements use the remaining deadline,
and rows, result bytes and plans are capped while being produced. The after
invocation requires the immutable before slice and emits the merged verifier
input:

```bash
set -a
. infra/env/node27-timeseries-compression.env
set +a

uv run python scripts/node27_timeseries_compression_benchmark.py \
  --phase before \
  --output "$RUN/benchmark-before.json" \
  --curve-basin-version-id basins_heihe_vbasins \
  --curve-river-segment-id basins_heihe_shud_reach_000001 \
  --curve-river-network-version-id basins_heihe_rivnet_vbasins \
  --curve-issue-time 2026-05-31T06:00:00Z \
  --curve-end-time 2026-06-07T06:00:00Z \
  --curve-scenario forecast_gfs_deterministic \
  --mvt-run-id fcst_gfs_2026053106_basins_heihe_shud \
  --mvt-basin-version-id basins_heihe_vbasins \
  --mvt-river-network-version-id basins_heihe_rivnet_vbasins \
  --mvt-valid-time 2026-05-31T06:00:00Z \
  --mvt-z 9 --mvt-x 399 --mvt-y 189

uv run python scripts/node27_timeseries_compression_benchmark.py \
  --phase after \
  --before-path "$RUN/benchmark-before.json" \
  --output "$RUN/benchmarks.json" \
  --curve-basin-version-id basins_heihe_vbasins \
  --curve-river-segment-id basins_heihe_shud_reach_000001 \
  --curve-river-network-version-id basins_heihe_rivnet_vbasins \
  --curve-issue-time 2026-05-31T06:00:00Z \
  --curve-end-time 2026-06-07T06:00:00Z \
  --curve-scenario forecast_gfs_deterministic \
  --mvt-run-id fcst_gfs_2026053106_basins_heihe_shud \
  --mvt-basin-version-id basins_heihe_vbasins \
  --mvt-river-network-version-id basins_heihe_rivnet_vbasins \
  --mvt-valid-time 2026-05-31T06:00:00Z \
  --mvt-z 9 --mvt-x 399 --mvt-y 189
```

#### 4.0.1 Independent terminal evidence bundle

`scripts/node27_timeseries_compression_live_evidence.py` has no DB connection,
command-execution, or mutation entrypoint. It reads one supervisor bundle,
resolves the complete transitive artifact graph before any output write,
freezes normalized path and inode aliases, verifies exact byte counts/sha256,
and validates both
runner receipts, recomputes selector hashes, D3 settings, totals, size/count
deltas, raw query/result hashes, median/p95 thresholds, and plan binding, then
atomically publishes the terminal envelope against
`schemas/timeseries_compression_live_evidence.schema.json`. A current
qualifying terminal is version `3.0` with `qualifies_task_4_5=true`. Historical
version `2.0` terminals remain schema-readable only as superseded evidence and
cannot set that discriminator.

The terminal distinguishes immutable `mutation_head_sha` from the later
`verifier_head_sha`. Preflight is captured before mutation and binds the
former; both runner receipts must be version `2.0` and independently bind that
same SHA before any DB call. Version `1.0` receipts remain readable historical
operational evidence but cannot satisfy this terminal contract. A
post-mutation preflight rewrite is invalid. Selection uses two distinct
artifact references: one immediately after dry-run and one within 60 seconds
before enforce. Each contains its observation time, cutoff, complete ordered
candidate list and selected tuple. Benchmark phases persist every actual
positional or named bind, the cold execution, two to five warmups, activity
samples, and seven measured execution/plan records. Every after plan must
independently bind the selected `DecompressChunk`.

Every bundle artifact reference is exactly
`{"path":"/absolute/path","sha256":"<lowercase-64hex>","bytes":N}` and
must name a regular non-symlink file. Canonical embedded JSON hashes are
`jq -cS` UTF-8 including its trailing newline. The bundle has these exact
top-level keys: `schema_version`, `issue`, `generated_at`, `node`,
`mutation_head_sha`, `verifier_head_sha`, `database_identity`,
`authorization`, `execution`, `recovery`, `preflight`, `migration`,
`selection`, `receipts`, `sizes`, `catalog`, `benchmarks`, `cleanup`,
`out_of_scope`.

Referenced JSON contracts are:

- `execution.run_plan` is the immutable concrete command/checkpoint plan and
  `execution.ledger` is the append-only producer truth. The verifier recomputes
  the plan hash, exact event state machine, cursor continuity and every
  produced artifact association from ledger events. The five `*_invocation`
  keys are mandatory v3 bundle contents, not optional legacy leftovers:
  `recovery.invocation`, `migration.first_invocation`,
  `migration.second_invocation`, `receipts.dry_run_invocation` and
  `receipts.enforce_invocation`. Each carries the contract its schema
  `description` states.
  Required — by the verifier's exact-key check on the input bundle, and by
  this schema in a v3 qualifying (non-failure) terminal document. The
  invocation semantics inside the value — argv, exit code, timings — are
  never interpreted, and the verifier re-derives this slot from
  `execution.ledger` rather than copying what was authored here; the
  committed bundle author already writes that same ledger reference into
  this slot, so on its output the authored and terminal values coincide. The
  value is not otherwise inert: when it is exactly a `{path, sha256, bytes}`
  mapping it becomes an artifact-closure node — the file must exist as a
  regular non-symlink whose `sha256`/`bytes` match, and if it parses as JSON
  it is complexity-bounded and its own nested artifact references are
  resolved transitively — and it is retained, deduplicated by normalized
  path, in the terminal `source_manifest`. A value of any other shape is not
  itself a closure node, though any well-formed reference nested inside it
  still is, collected in its own right.
- `recovery.preflight`: separately authorized replay preflight with capture
  time, node-27/mutation-SHA/database identity, at least 300 GiB free space,
  `before_compressed=true`, positive row count, and the exact six-field target
  `_timescaledb_internal._hyper_3_7_chunk` covering
  `[2026-05-28T00:00:00Z, 2026-06-04T00:00:00Z)`;
  `recovery.receipt` is a different artifact with decompression start/finish,
  exit zero, exact returned relation, `after_compressed=false`, and the same
  row count. Both artifacts bind the same mutation SHA/database/node.
- `preflight.evidence`: the facts in steps 1–4, including exact role booleans,
  guard presence, quiescence and inactive compression units;
  `preflight.schema_dump` is streamed under a practical byte cap. The verifier
  validates the supervisor's container-bound PG15 `pg_restore --version/--list`
  identity, bounded stdout/stderr hashes, tool/container identity, exit status
  and entries in `preflight.schema_dump_list`; it does not execute a tool.
  `preflight.catalog_before` is
  its timestamped canonical catalog neighbor.
- `migration.catalog_after_first|second` and `catalog.post`:
  `{"hypertables":{"hydro.river_timeseries":true,
  "met.forcing_station_timeseries":true},"compression_settings":[...],
  "policy_jobs":[]}`. Each setting row has exactly schema/table/`attname`,
  `segmentby_column_index`, `orderby_column_index`, `orderby_asc`, and
  `orderby_nullsfirst`, in the D3 order pinned by the fixture.
- `selection.post_dry_run|pre_enforce`: distinct timestamped
  artifacts containing cutoff, free bytes, complete ordered candidates and
  the bound-1 selected tuple. Their selected identities must match both runner
  receipts; the pre-enforce observation is at most 60 seconds before enforce.
- `sizes.pre|post`: `tables` keyed by both D3 hypertables, plus the selected
  origin's pre-enforce uncompressed index (`-1`). Each row has
  `hypertable_size`, `parent_relation_size`, `compressed_chunks`,
  `uncompressed_chunks`, and `compressed_relations`. Each compressed relation
  binds `origin_chunk_schema`/`origin_chunk_name` to its sibling
  `schema`/`name` and measured `bytes`.
- `benchmarks.evidence`: exactly `curve`, then `mvt`. Each stores source refs,
  exact `query_text` + sha256, every non-secret positional/named bind,
  before/after raw result payload + identity, cold execution, two to five
  warmups, activity samples, and seven measurements with raw plan/timing/buffer
  fields. Curve payload is a JSON row array; MVT payload is nonempty even-
  length hex.
- `cleanup.evidence`: autopipe restored, compression timer enabled/inactive,
  compression service inactive with activation count zero, and installed unit
  hashes matching the repository.

The version-3 supervisor ledger derives exactly two migration applies, one
recovery decompression, one dry-run, one enforce, one dump, two container
`pg_restore` probes and before/after benchmark children. Replay-supervisor
activation is exactly one; recurring compression-service activation,
retention, drill, role and node-22 mutation counts are zero. Each event carries
a unique ID, concrete argv, PID, strict UTC/monotonic interval, bounded output
identity, exit, mutation SHA/database/run-plan/run identity and artifact
associations. Raw activity, relation-lock, canonical user-unit `systemctl show`
and cursor-bounded journal artifacts are captured before/after every mutation
and at preflight/postflight/cleanup. Acceptance means “controlled lane executed
exactly once with no observed conflict”. The operator attests that they are the
sole DB user during the window; this is a trust prerequisite and is not
database-audit proof of absolute direct-SQL bypass absence. The global
chronology is one non-overlapping chain from dump/catalog-before through both
migrations, recovery, compression preflight, dry-run, before benchmark,
pre-enforce selector, enforce, post snapshots/benchmark, cleanup and audit
capture. All boundaries are strict (`<`) and snapshot IDs are unique. Output
paths must be disjoint from the bundle and the complete recursively retained
graph by normalized path and inode; the terminal manifest equals that closure,
and publication is followed by revalidation. A safe known destination is
atomically replaced with a versioned failed/indeterminate tombstone on failure;
an unsafe, unknown, symlinked or input-alias destination is untouched.
After closure/disjointness validation and freezing the old output identity,
an inability to establish bundle provenance invalidates any prior PASS with a
schema-valid version-3 nonqualifying tombstone. That tombstone declares
`provenance_state=unavailable` and records only its safe failure stage/reason,
the expected old output identity, and an independently established verifier
SHA when available; it must not invent run or mutation identity. Failures
before that safety boundary do not create a gate, intent, lock, or terminal.

The replay env must externally pin the descriptor-read run plan with
`NODE27_COMPRESSION_RUN_PLAN_SHA256`; the supervisor checks that digest before
JSON parsing, recomputes `run_plan_id`, then requires a clean
`/home/nwm/NWM` checkout whose `HEAD`, reviewed origin ref, and GitHub origin
lineage all bind the mutation SHA. Every command kind has one canonical argv
contract; changing only its executable to `/bin/true` is a hard failure. Every
semantic output has exactly one planned producer. Child associations are
limited to files their exact argv writes (`pg_dump --file`, the committed
bounded decompression producer `--receipt-path`, runner `--receipt-path`, and
benchmark `--output`). The decompression producer verifies the exact compressed
target and positive row count, executes one timeout-bounded
`decompress_chunk`, reconciles returned relation/uncompressed state/row parity,
and atomically emits the recovery receipt; uncertainty emits indeterminate
evidence and is never automatically retried. Preflight, dump-list,
catalog, selector, size and cleanup documents are separate supervisor capture
steps: each runs its immutable-plan probe, requires its output to be absent,
atomically publishes that probe's stdout, then ledgers path, byte count,
digest, device and inode. Ledger order proves each capture occurred at its
true pre/post state-machine boundary rather than being prewritten or attached
to an unrelated child.
The verifier rechecks that descriptor identity, so replacing a file with the
same bytes still fails. Supervisor checkpoint observations use the same
descriptor-bound form.

Replay activation count is derived, not authored: systemd supplies
`INVOCATION_ID`, every ledger event carries it, and canonical replay
`systemctl show` must report the canonical executing `Type=oneshot` state
`activating/start`, that same non-empty ID, the
current supervisor `MainPID`, and non-empty UTC/nonzero monotonic start
timestamps. This one current manager identity contributes the count of one.
Cursor-bounded journal is negative evidence only: arbitrary rows for that same
ID do not increase the count, while any other replay ID or any recurring-unit
activation fails closed. Failure
finalizer state retains `mutation_head_sha`, and every provenance-bound
failure tombstone must publish that same SHA across normal failure,
`ExecStopPost`, repeated-finalizer, and publish-race paths.
Git lineage probes and every success/failure CAS publication acquire their
process group or publish lock under the same finite wall. The 900-second main
wall creates a shorter operation wall that reserves TERM/KILL/drain plus
terminal lock/CAS/finalizer time; Git, cursor, checkpoints, captures, DB
producer and every child receive only that operation wall. Held locks time out
without overwriting a newer terminal. Finalizer/failure-publication intent is
retained on lock timeout and consumed only after replacement or proof that a
newer inode/digest won, allowing one bounded `ExecStopPost` retry.

The terminal is not authoritative whenever the adjacent
`.terminal.json.failure-intent/` state is pending. Every readiness/failure/
success path follows one bounded lock order: `.terminal.json.intent-gate.lock`
first, then `.terminal.json.publish.lock` when terminal access is needed.
`read_authoritative_terminal()` follows that order and rejects pending or
malformed durable state before reading the terminal.

The gate's lock object and its state document are deliberately separate files.
`.terminal.json.intent-gate.lock` is contentless: it exists only to serialize
the state machine and to give the intent sidecar a stable `(device, inode)`
anchor. Gate state lives in `.terminal.json.intent-gate.json`, which is written
to a temporary sibling, fsynced, and atomically renamed into place under the
held lock, then parent-fsynced. No gate transition is therefore ever partially
durable: a crash leaves either the previous complete document or the next one.
Nothing binds to the state document's inode, which changes on every transition.
An absent state document and a canonical `idle` document mean the same thing.

`packages/common/compression_terminal_state.py` is the single owner of this
state machine. The live verifier, replay supervisor, normal failure path and
systemd `ExecStopPost` finalizer all call that shared API; the supervisor does
not open the publication lock or replace the terminal directly. Finalizer
state freezes the stale terminal's device, inode, byte count and digest plus
run/mutation SHA. A pending unavailable-provenance verifier intent may be
upgraded to a bound supervisor tombstone only when the complete expected
terminal identity agrees. A timeout preserves both retry states; a schema-valid
authoritative newer terminal consumes finalizer state, while an unrelated or
malformed terminal fails closed.

The active intent directory contains only `intent.json` and `identity.json`.
Both are created mode 0600 through an exclusively created no-follow directory
descriptor, file-fsynced, directory-fsynced, and bound to the revalidated
parent identity. The gate state document records the exact sidecar identity;
the sidecar records the exact intent `(device,inode,bytes,sha256)`,
failure-payload digest, schema version, and run/verifier/mutation identity,
and binds back to the stable gate *lock* inode. This cross-binding is
revalidated in every fresh process, so replacing either file—even with
identical bytes—fails closed. Because both files are mode 0600 inside a mode
0700 directory owned by the same uid as the terminal, the binding proves
durable self-consistency, not authorship: an actor that can rewrite the whole
directory consistently can already replace the terminal directly, and gains
nothing beyond a failure tombstone. Parent-fsync failure removes or
quarantines only entries exclusively created by that attempt and leaves no
active authoritative pair.

A crash between `mkdir` and the pending gate transition leaves an idle gate
beside an intent directory. The idle gate is durable proof that no intent
reached its commit point, so a strict create prefix—neither entry, or just one
of them—is provable garbage: it is collected through the anchored directory
descriptor, unlinking only the two known mode-0600 single-link names, and the
loader reports no intent. A complete, fully cross-bound directory is instead a
durable decision, so its interrupted commit is finished rather than dropped. A
complete directory that does not cross-bind is neither: it fails closed and is
left untouched. Refused failure publications log the exact refusal reason, so a
lane that cannot publish never presents as merely "the finalizer did not
replace the receipt".

A held terminal lock does not suppress failure invalidation: the publisher
durably creates the pair under the intent gate, releases that gate, then may
time out trying the ordered gate→terminal CAS while the pending state continues
to block readers. Successful failure/PASS publication consumes both files by
a durable `consuming` gate state, an atomic intent-directory rename, parent
fsync, exact-identity cleanup, and a durable idle gate state. The `consuming`
state is durable *before* the rename, so a renamed directory can never sit
behind an idle gate. The rename alone never authorizes deletion. After the
terminal replacement and parent fsync, the gate atomically persists a
`committed_cleanup` phase containing the published terminal's complete
identity, the prior expected identity, both original entry names and complete
identities, the consumed-directory inode, payload digest, and provenance
context. Only then may cleanup
delete `intent.json`, fsync the child directory, delete `identity.json`, fsync
again, remove the consumed directory, fsync the parent, and finally persist an
idle gate. This fixed order makes an identity-missing/intent-surviving prefix
explicitly unreachable and unsafe.

Every fresh reader or publisher can idempotently finish the legal crash
prefixes: both files, sidecar only, empty consumed directory, or consumed
directory already absent. Each survivor is descriptor-read immediately before
unlink and must retain its device/inode/bytes/SHA-256 plus canonical
cross-binding. Missing entries are accepted only from `committed_cleanup`.
Equal-length tampering, an incompatible replacement terminal, or a foreign
entry fails closed without deletion. Cleanup proceeds across a changed
terminal only when its schema-valid provenance satisfies the explicit
newer-wins relation. The terminal publication lock is
opened by basename through the already anchored parent descriptor, with
no-follow, regular-file, single-link, mode and inode validation; a parent
namespace replacement cannot redirect lock creation and is detected when the
gate releases. Terminal, intent directory/files, intent gate lock, intent gate
state document, and publication lock remain
normalized-path/symlink/inode/hardlink-disjoint from the bundle, canonical
schemas, and complete recursive input closure.

The version-3 schema mirrors the state machine branches. An unavailable-
provenance failure must carry the exact three-key `failure_context` and cannot
carry run/mutation identity. A bound supervisor/finalizer failure must carry
`run_id` and `mutation_head_sha` and cannot carry `failure_context`. A
qualifying version-3 PASS explicitly rejects `outcome`, `failure`,
`failure_context`, and `provenance_state`; historical version-2 receipts remain
schema-readable but nonqualifying.

Example invocation (paths contain no credential):

```
uv run python scripts/node27_timeseries_compression_live_evidence.py \
  --bundle-path /home/nwm/NWM/.nhms-issue1069-live/bundle.json \
  --output-path /home/nwm/NWM/.nhms-issue1069-live/terminal.json
```

`PASS_TASK_4_5` is emitted only after all gates pass. On any failure, keep both
compression units inactive, restore the autopipe timer's exact prior state,
and preserve artifacts. Compression is not a transactional batch: a chunk
already compressed after a partial/timeout/regression remains compressed and
the outcome remains failed/partial. Do not rerun enforce, auto-decompress,
claim rollback from the schema dump, or relabel the evidence. Any later
`decompress_chunk` recovery is a separate authorization bound to the exact
successful receipt list, followed by fresh catalog/size/result/query checks.

The 2026-07-15 bound-1 operation succeeded, but its first terminal attempt was
rejected because these provenance artifacts were incomplete. Its dry-run and
enforce receipts remain historical operational evidence; they do not satisfy
task 4.5 and must not be relabeled. Replaying the evidence requires separate
human authorization for the exact decompression/recompression mutation.

#### 4.0.2 Authorized exact-chunk evidence replay

The user granted that separate authorization on 2026-07-15 for exactly one
decompression and one bound-1 recompression of
`_timescaledb_internal._hyper_3_7_chunk`; it does not authorize retention,
node-22 work, timer activation, another chunk, or an additional retry.

Before decompression, keep both compression units inactive and quiesce
autopipe as in step 4. Capture `recovery.preflight` before mutation: verify the
six-field identity and range above, `is_compressed=true`, a positive chunk-row
count, and at least 322122547200 free filesystem bytes. It must also repeat the
full normal safety preflight: clean worktree; node-27 primary/container/role
identity; mode-0600 env; installed write guards; quiescent autopipe, DB writers
and conflicting locks; inactive compression units; and exact four-unit state
plus bounded journal artifact refs. Invoke the manual procedure only for that
fully-qualified relation, capturing UTC start/finish, exit code and returned
relation in the distinct `recovery.receipt`. Immediately require
`is_compressed=false` and the exact same row count. A missing return relation,
nonzero exit, target/SHA drift, row-count change, or low space blocks the replay
before recompression.

Only after the recovery receipt is complete may the normal compression
preflight be captured. The enforced chronology is recovery preflight <=
decompression start <= decompression finish <= compression preflight. The
bundle authorization records `replay_decompression=true` and one decompression
invocation; `out_of_scope.decompress_run` truthfully records `true` and is
accepted only when both recovery artifacts pass. Run a fresh v2 dry-run, write
both distinct selector snapshots, and require both snapshots plus the v2
enforce receipt to reselect the same exact target. Then perform the sole
bound-1, 900-second recompression and complete the full benchmark/cleanup
contract above. Never overwrite the historical v1 receipts.

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
`met.forcing_station_timeseries` only. (The retired rebuild drill wrote to an
isolated staging schema and never tripped the guard; it no longer exists —
see the retirement record at the top of this runbook.)

#### 4.1.1 Pre-compression checklist (run BEFORE compressing a window, `#1781`)

Compression is one-directional in practice: once a window is compressed, any
run whose products get regenerated into it is permanently unappliable until an
operator decompresses. In 2026-08 a manual tiering pass compressed
`met.forcing_station_timeseries` chunk `_hyper_1_52_chunk` (2026-08-06..08-13)
while that window was still inside the product-regeneration horizon; node-22
then regenerated 88 runs into it, and the ingest tick spent days rejecting
them. Draining afterwards was measured and rejected (met 250 MB → 11.9 GB plus
river 7.0 GB → 196 GB against 521 GB free), so the runs were terminal-stated
instead. **The cheap place to prevent that is here, before compressing.**

Bind the candidate chunk's own bounds first — every check below reuses them:

```sql
\set target_chunk '_timescaledb_internal._hyper_1_52_chunk'

-- No trailing semicolon: \gset must terminate the query itself, and it binds
-- one psql variable per output column (:range_start, :range_end).
SELECT hypertable_schema || '.' || hypertable_name AS target_table,
       range_start, range_end
FROM timescaledb_information.chunks
WHERE format('%I.%I', chunk_schema, chunk_name) = :'target_chunk'
\gset
```

- [ ] **No decline record already points into the window.** A hit means this
      window has ALREADY cost runs their recompute — compressing further inside
      it compounds a known loss rather than creating a new one:

      ```sql
      SELECT d.run_id, d.reason_code, d.declined_at,
             h.status, h.start_time, h.end_time
      FROM ops.ingest_recompute_decline d
      JOIN hydro.hydro_run h USING (run_id)
      WHERE h.start_time < :'range_end'::timestamptz
        AND h.end_time   > :'range_start'::timestamptz
      ORDER BY d.declined_at DESC;
      ```

      Expected before a clean compression: **0 rows.**

- [ ] **Recent ticks are not already blocked on this window.** Both surfaces,
      on node-27:

      ```bash
      grep -o '"declines_active": [0-9]*' /home/nwm/autopipe-logs/autopipe.log | tail -20
      grep -c 'HANDOFF_APPLY_COMPRESSED_CHUNK_BLOCKED' /home/nwm/autopipe-logs/autopipe.log
      grep -n '"stage": "forcing_handoff"' /home/nwm/autopipe-logs/autopipe.log | tail -20
      ```

      Expected: `declines_active` flat (a rising value means runs are being
      terminal-stated right now), and no recent `forcing_handoff` failure whose
      run overlaps the window.

- [ ] **The window is older than the product-regeneration horizon.** node-22
      regenerates products for cycles well after their initial run, so a chunk
      whose `range_end` is still inside that horizon is a live target:

      ```sql
      SELECT :'target_table' AS hypertable, range_end, now() - range_end AS age
      FROM timescaledb_information.chunks
      WHERE format('%I.%I', chunk_schema, chunk_name) = :'target_chunk';
      ```

      ```bash
      # The horizon, measured rather than assumed: on node-27, the newest
      # product write anywhere in the object store. A chunk whose range_end is
      # younger than the oldest run still being regenerated is a live target.
      find /home/ghdc/nwm/object-store/runs -maxdepth 3 -name 'rivqdown*' \
        -printf '%TY-%Tm-%Td %TH:%TM %p\n' | sort -r | head -20
      ```

      Expected: `age` comfortably exceeds the newest regeneration seen for that
      window. If it does not, **do not compress this chunk yet** — pick an older
      one.

**Disposition when any check hits.** Exactly two acceptable answers, and
"compress anyway and see" is not one of them:

1. **Drain first** — decompress the overlapping chunk(s) per §4.3, replay the
   affected runs (`scripts/node27_autopipeline.py --force --only-cycle …`), let
   the tick reach `rc=0`, delete the now-stale rows from
   `ops.ingest_recompute_decline`, then compress. Budget the full decompressed
   size for BOTH `met` and `hydro` (the 2026-08 measurement above is the shape
   of that bill).
2. **Explicitly accept the terminal state** — compress knowing those runs keep
   their pre-compression data forever, and record the decision in the change /
   issue that authorized the compression. The decline table is the audit trail;
   an accepted loss must be readable there, not inferred from a quiet log.

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
codes. Three of them route unconditionally to the decompress procedure below;
`HANDOFF_APPLY_COMPRESSED_CHUNK_BLOCKED` has a second acceptable disposition
since `#1781` (see its row). Grep the DB / stderr / receipt surface for these
literals when triaging a reingest failure:

| Code (literal string) | Where produced | How to observe |
|---|---|---|
| `HANDOFF_APPLY_COMPRESSED_CHUNK_BLOCKED` | `packages/common/forcing_domain_handoff_apply.py::apply_forcing_domain_handoff` — attached as `unavailable_report.unavailable_reasons[].code` (from `REASON_APPLY_COMPRESSED_CHUNK_BLOCKED`) when the guard raises inside `_replace_forcing_station_timeseries`. | Persisted on the apply report (DB or API response) that the caller inspects. Also on the ingest tick: the run's summary entry goes `outcome="declined"` and a row lands in `ops.ingest_recompute_decline`. |
| `OUTPUT_PARSE_COMPRESSED_CHUNK_BLOCKED` | `workers/output_parser/parser.py::OutputParser.parse_run` — stamped on `hydro.hydro_run.error_code` via `mark_run_failed`, and emitted as the stderr prefix by `workers/output_parser/cli.py` when the guard escapes. | `hydro.hydro_run.error_code` column; parser CLI stderr line `OUTPUT_PARSE_COMPRESSED_CHUNK_BLOCKED: ...`. |
| `FORCING_PRODUCE_COMPRESSED_CHUNK_BLOCKED` | `workers/forcing_producer/cli.py` stderr prefix — emitted when `ForcingProducer.produce()` re-raises a `CompressedChunkGuardError` un-wrapped. | Forcing producer CLI stderr line `FORCING_PRODUCE_COMPRESSED_CHUNK_BLOCKED: ...`. |
| `FORCING_COMPRESSED_CHUNK_BLOCKED` | `workers/forcing_producer/producer.py::ForcingProducer._mark_failed` — stamped on `met.forecast_cycle.error_code` when the dedicated `except CompressedChunkGuardError` arm fires. | `met.forecast_cycle.error_code` column (with `status = 'failed_forcing'`). |

For every code above, the operator response is the decompress procedure
in §4.3.2 below (identify chunk from the structured error message → run
`decompress_chunk(...)` → re-run ingest). Route on the code; do NOT paper
over with a generic ingest retry.

`HANDOFF_APPLY_COMPRESSED_CHUNK_BLOCKED` is the one code with a sanctioned
alternative (`#1781`): the ingest tick records the blocked recompute in
`ops.ingest_recompute_decline` and stops retrying it, which is the correct
disposition when the regenerated products are not worth the decompress bill
(§4.1.1 disposition 2). Decompressing is still the way to actually apply them.
Either way the decision is now explicit and queryable — what is NOT acceptable
is leaving the tick to rediscover the block every 10 minutes. To take the
decompress route on a run already declined, decompress the chunk and re-run
with `--force` (which bypasses the exclusion entirely), or delete the run's
rows from `ops.ingest_recompute_decline`; a decompress alone does not reopen
the decision, because nothing about the products changed.

#### 4.3.2 Manual decompress steps

When a reingest surfaces `CompressedChunkWriteError` or TimescaleDB's raw
compressed-chunk error, follow this manual procedure. Do NOT introduce an
automated decompress-on-demand lane (ADR 0002 decision 3 — the manual
escape hatch is intentional; automated decompress-on-demand would
re-introduce write amplification the compression tier is meant to
prevent).

Decompress the `met` AND `hydro` chunks covering the window together: a partial
decompress (met decompressed, river still compressed) lets the forcing handoff
succeed and then trips the parse-stage guard at
`workers/output_parser/parser.py:976` instead, which `#1781` deliberately does
NOT terminal-state — that failure stays a plain `rc=1` retry, so a half-drained
window trades one loop for another.

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

3a. **Resolve the chunk's tablespace and size the check against the right
   device.** Decompression restores the data into the chunk relation's own
   tablespace, so the filesystem that needs room is the chunk's, not
   necessarily pgdata's — see "Recorded exception (2026-08-06)" above. Use the
   residency query there; `pg_default` → `df -h /home`, `ghdc` →
   `df -h /data/GHDC`. The number to compare against is
   `before_compression_total_bytes` from
   `chunk_compression_stats(<hypertable>)`, which is exactly what
   decompression will write back (for the four migrated chunks that ranged
   from 20 GB to 333 GB).

   If the chunk sits on a device without the room, move the chunk **and every
   one of its indexes** to `ghdc` first. A compressed chunk's relation is a
   near-empty shell, so both moves are instant at this point, and
   `ALTER TABLE … SET TABLESPACE` does **not** carry indexes with it:

   ```
   ALTER TABLE _timescaledb_internal._hyper_1_1_chunk SET TABLESPACE ghdc;
   SELECT format('ALTER INDEX %I.%I SET TABLESPACE ghdc;', schemaname, indexname)
   FROM pg_indexes
   WHERE schemaname = '_timescaledb_internal'
     AND tablename = '_hyper_1_1_chunk'
   \gexec
   ```

   TimescaleDB index names can begin with a digit
   (`10_23_river_timeseries_pkey`), which is not a valid bare identifier — use
   the `format(%I)` + `\gexec` form above rather than hand-writing the
   statements.

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

   **Before restarting that timer, check what else it will compress.** The
   runner is not a native TimescaleDB policy — `timescaledb_information.jobs`
   carries no compression job; it is the systemd timer, gated by
   `NODE27_TIMESERIES_COMPRESSION_LAG_SECONDS` (live `172800` = 2 days since
   the 2026-08-07 short-lag decision, §4.3.2.1; the committed template still
   ships `604800` = 7 days — read the box, not the template) and bounded to
   `NODE27_TIMESERIES_COMPRESSION_PER_TICK_BOUND` chunks per tick.
   Every chunk whose `range_end` is older than the lag is a candidate, not
   just the one you decompressed. Because the write guard refuses **any**
   write overlapping a compressed chunk's range — including pure inserts of
   new rows (`packages/common/timescale_write_guard.py`, half-open overlap on
   the incoming batch's `[valid_time_min, valid_time_max]`) — compressing a
   chunk that a pending backlog still needs to write into blocks that backlog
   wholesale. If a catch-up ingest is outstanding (e.g. after a scheduler
   outage), keep the compression timer stopped until the catch-up has
   advanced past those chunk ranges.

   **Unverified as of 2026-08-06:** compressing a chunk creates a new
   `_timescaledb_internal.compress_hyper_*_chunk` relation, and it has not
   been confirmed on this deployment that the new relation inherits the source
   chunk's tablespace rather than landing in `pg_default`. After the first
   compression tick that touches a `ghdc` chunk, re-run the residency query
   against both the chunk and its `compress_hyper_*` counterpart. If the
   compressed relation lands in `pg_default`, move it to `ghdc` explicitly —
   otherwise the compression tier silently refills `/home`, which is the
   pressure this split exists to relieve. Never "restore" a `ghdc` chunk to
   `pg_default`.

Rollback: none required — `decompress_chunk` is idempotent and can be
undone by the scheduled compression runner. If the reingest itself
completes but the operator wants to abandon the decompress state, force a
compression pass with the runner's enforce flag once the chunk falls
outside the lag window.

#### 4.3.2.1 Short-lag regime (2026-08-07, md0 outage): outage recovery MUST decompress first

自 2026-08-07 起生产 lag 从 604800（7 d）降为 **172800（2 d）**（
`infra/env/node27-timeseries-compression.env`，备份
`*.bak-lag7d-20260807`）：md0 故障后大 chunk 无处安放，/home 独自承载
稳态未压缩量，必须把峰值从 ~800 G（本周 + 7 d 缓冲周）压到 ~515 G
（本周 + 2 d 缓冲周）。md0 恢复、DB 大 chunk 重新有独立设备后可回调。

短 lag 的运维铁律：**任何超过 lag 时长（2 d）的 ingest 中断，恢复追赶
之前必须先对受影响的"已关闭周" chunk 执行上文的手工 decompress 流程**
——补算的旧 cycle 会往已压缩周写替换窗口 DELETE + INSERT，直接追会撞
写保护 guard（或在支持 DML 的版本上触发严重膨胀）。判定受影响范围：
补算最早 cycle 的预报时段起点落进哪些周，哪些周就要先解压。解压需要
预留膨胀空间（历史实测 ~400 G/周,先 `df -h /home`），追平后由
compression timer 按 lag 自动重新压缩。

#### 4.3.3 Recreating the `nhms-db` container (mount-critical)

The node-27 primary PostgreSQL runs in a container named `nhms-db` created
with a plain `docker run` — **there is no compose file and no systemd unit for
it anywhere in this repo**. `infra/docker-compose.dev.yml` defines a service
that is also named `nhms-db`, but that is the *local dev* stack (named volume,
port 5432, dev credentials); using it as a recreate template on node-27
produces a database that cannot open production data. Do not.

Since 2026-08-06 the container is mount-critical: **all three** bind mounts
must be present, or PostgreSQL comes up unable to read the four chunks and 18
indexes in the `ghdc` tablespace.

| Host | Container |
|---|---|
| `/home/nwm/nhms-pgdata` | `/home/postgres/pgdata/data` |
| `/home/nwm/nhms-evidence` | `/var/lib/postgresql/evidence` |
| `/data/GHDC/nwm-archive/nhms-tablespace` | `/home/postgres/pgdata/tablespaces/ghdc` |

Never type the DB password: carry the environment over from the running
container into a `0600` file and delete it once `docker run` has read it.

**Never re-resolve the floating `pg15-latest` tag from a registry.**
`packages/common/node27_external_contract_snapshot.json` pins two compared
fields: `host_context.nhms_db_image_id` (a `sha256:` image id — TimescaleDB
2.10.2 / PostgreSQL 15.2) and `host_context.nhms_db_image_ref` (the literal
tag string `timescale/timescaledb-ha:pg15-latest`, measured from
`.Config.Image`, i.e. from whatever argument `docker run` was given).
`scripts/node27_external_contract_snapshot.py` compares both
(`COMPARED_SECTIONS` includes `host_context`), exiting `3` on drift — which
§4.4 classifies as stop-and-full-PR-loop, not a patch bump.

That double pin constrains the recreate in both directions:

- `docker pull` before recreating (or a cold cache resolving the tag anew)
  can move `nhms_db_image_id` — a newer `pg15-latest` ships a TimescaleDB
  library that may not carry `2.10.2`, and the extension fails to load
  against the on-disk catalog, on the production primary, mid-window.
- Passing the digest itself to `docker run` moves `nhms_db_image_ref`
  (`.Config.Image` would become the `sha256:` string instead of the tag) —
  a guaranteed exit-3 that §4.4 then tells you to treat as a full-loop stop.

The procedure below threads both: **verify the local tag still resolves to
the pinned image id, then run the tag.** Both compared fields stay unchanged
and `--check` can genuinely return 0. Fall back to the digest only when the
tag is gone/cold — and then expect and record the benign `_ref`-only drift.

1. Quiesce writers — stop `nhms-node27-autopipe.timer` and any in-flight
   `node27_autopipeline.py`, and wait for `/tmp/autopipe.cron.lock` to free.
   Also stop the other node-27 timers for the duration
   (`nhms-node27-timeseries-compression`, `-timeseries-retention`,
   `-resource-governance`); they will otherwise fire against a stopped DB and
   litter failure receipts.

2. Capture the full prior spec, stop, and **rename rather than remove** the
   old container so it stays available as a rollback:

   Run steps 2-4 in one `tmux`/`screen` session — `$TS`, `$ENVFILE`, `$IMAGE`
   and `$IMAGE_DIGEST` are shell variables and do not survive a dropped ssh
   session. If you must reconnect, re-derive `TS` from the file names before
   continuing.

   ```bash
   TS=$(date -u +%Y%m%dT%H%M%SZ)
   ENVFILE=/home/nwm/.nhms-db.env.$TS
   umask 077
   docker inspect nhms-db --format '{{range .Config.Env}}{{println .}}{{end}}' \
     | grep -v '^$' > "$ENVFILE"
   # HostConfig + a credential-free Config subset, to diff against after
   # recreate (step 4). Do NOT save the full inspect: its .Config.Env carries
   # the DB password and would outlive the 0600-and-delete discipline this
   # section mandates. Healthcheck/Cmd/Entrypoint/Labels live in .Config, not
   # .HostConfig, so both captures are needed for real coverage.
   docker inspect nhms-db | jq -S '.[0].HostConfig' \
     > /home/nwm/nhms-db-hostconfig-$TS.json
   docker inspect nhms-db | jq -S 'del(.[0].Config.Env) | .[0].Config' \
     > /home/nwm/nhms-db-config-$TS.json
   # Pin the engine. RepoDigests is an IMAGE field — querying it on the
   # container is a template error (verified on node-27 2026-08-06), so
   # resolve the image id first, then ask the image for its digest.
   IMAGE=$(docker inspect nhms-db --format '{{.Image}}')
   IMAGE_DIGEST=$(docker image inspect "$IMAGE" \
     --format '{{if .RepoDigests}}{{index .RepoDigests 0}}{{end}}')
   echo "pinned: $IMAGE / ${IMAGE_DIGEST:-<no registry digest>}"
   # Assert the local tag still resolves to the pinned image id. This is what
   # lets step 3 run the tag (keeping .Config.Image == the pinned
   # nhms_db_image_ref) without risking a silently newer engine.
   TAG_ID=$(docker image inspect timescale/timescaledb-ha:pg15-latest \
     --format '{{.Id}}' 2>/dev/null)
   [ "$TAG_ID" = "$IMAGE" ] \
     && echo "tag OK: pg15-latest -> $TAG_ID" \
     || echo "TAG MISMATCH OR ABSENT: use the \$IMAGE / \$IMAGE_DIGEST branch in step 3"
   # If IMAGE_DIGEST is empty the image was never pulled by digest — the
   # cold-cache fallback in step 3 does not exist. Keep the old container
   # until a pull of the pinned digest is proven from this host.
   # -t 300: a multi-hundred-GB instance can exceed docker's 10s default and
   # get SIGKILLed into crash recovery.
   docker stop -t 300 nhms-db
   docker logs --tail 20 nhms-db   # expect "database system is shut down"
   docker rename nhms-db nhms-db-pretbs-$TS
   ```

   Measured on node-27 (2026-08-06): `IMAGE` resolves to the exact digest the
   contract fixture pins (`sha256:ad39c4fb…`), `.Config.Image` is
   `timescale/timescaledb-ha:pg15-latest` (matching the pinned
   `nhms_db_image_ref`), and the registry digest is
   `timescale/timescaledb-ha@sha256:a8e3322e…` (non-empty, so the fallback is
   real on this host).

3. Recreate with the full mount set, from the pinned image:

   ```bash
   docker run -d \
     --name nhms-db \
     --restart unless-stopped \
     --user 1005:1005 \
     --env-file "$ENVFILE" \
     -p 55432:5432 \
     -v /home/nwm/nhms-pgdata:/home/postgres/pgdata/data \
     -v /home/nwm/nhms-evidence:/var/lib/postgresql/evidence \
     -v /data/GHDC/nwm-archive/nhms-tablespace:/home/postgres/pgdata/tablespaces/ghdc \
     timescale/timescaledb-ha:pg15-latest postgres
   rm -f "$ENVFILE"
   ```

   Running the *tag* is deliberate — but only because the assert above proved
   it still resolves to `$IMAGE`. It keeps `.Config.Image` equal to the pinned
   `nhms_db_image_ref`, so a clean recreate leaves **both** compared fields
   untouched and `--check` can genuinely return 0. Two failure branches:

   - Assert fails (tag now resolves elsewhere, e.g. someone pulled): run
     `"$IMAGE"` instead, accept that `--check` will report an
     `nhms_db_image_ref`-only drift, and record it as procedure-induced —
     `nhms_db_image_id` must still match; if *it* differs, stop, §4.4.
   - Cold cache (tag absent entirely): run `"$IMAGE_DIGEST"` — it is
     pullable, whereas the bare image id would turn the cache miss into a
     hard `docker run` failure mid-window. Same `_ref`-only drift applies.
     If step 2 recorded `<no registry digest>`, this branch does not exist;
     restore the renamed container instead.

   A fallback `_ref` drift is **permanent** (`.Config.Image` is fixed at
   container creation), so every later `--check` — including §4.0's
   pre-mutation gate — exits 3 until it is closed. Never leave it standing:
   a check operators learn to look past for one key is a check that does
   nothing. Close it, after step 4 has fully passed, by either
   - **returning to green locally** (preferred, registry-free on the assert
     branch): `docker tag "$IMAGE" timescale/timescaledb-ha:pg15-latest`
     (cold-cache branch: `docker pull "$IMAGE_DIGEST"` first), then repeat
     steps 2-4 once more running the tag — both compared fields match
     again; or
   - **re-baselining via §4.4**: a full-loop PR updating
     `nhms_db_image_ref` in the fixture, with the `--dump` attached.

   `--user 1005:1005` is the host `nwm` uid/gid and must match the ownership
   of all three host paths; the tablespace directory is `nwm:nwm` mode `0700`.

4. Verify before declaring success. Two distinct environments are needed:
   `node27-ingest.env` supplies `DATABASE_URL` for the psql checks in (a);
   the contract check in (b) needs the §4.4 canonical setup instead — repo
   cwd, the *replay* env (PG connection vars, not just a DSN), and
   `XDG_RUNTIME_DIR` for the `systemctl --user` probes. Mixing them up makes
   (b) fail with a probe error (exit 5), not a verdict.

   ```bash
   # (a) The mount is present AND actually serving data. `ls -ld` and
   #     pg_tablespace_location() both succeed even when the third -v is
   #     missing — docker silently creates an empty bind source and
   #     pg_tablespace_location() just reads the pg_tblspc symlink. Only a
   #     real read of a ghdc-resident chunk proves the mount. LIMIT 1 —
   #     it opens the relation files without scanning the multi-hundred-GB
   #     chunk on a cold cache.
   set -a; . /home/nwm/NWM/infra/env/node27-ingest.env; set +a
   docker exec nhms-db ls -ld /home/postgres/pgdata/tablespaces/ghdc
   psql "$DATABASE_URL" -c \
     "SELECT 1 FROM _timescaledb_internal._hyper_3_10_chunk LIMIT 1"
   psql "$DATABASE_URL" -c \
     "SELECT spcname, pg_tablespace_location(oid) FROM pg_tablespace ORDER BY 1"

   # (b) The engine did not drift — §4.4 canonical invocation, verbatim.
   cd /home/nwm/NWM
   set -a; . infra/env/node27-timeseries-compression-replay.env; set +a
   export XDG_RUNTIME_DIR=/run/user/$(id -u)
   uv run python scripts/node27_external_contract_snapshot.py --check; echo "exit=$?"

   # (c) Nothing else in the container spec was silently dropped.
   #     HostConfig covers shm-size/ulimits/network/log-opts; the Config
   #     subset covers healthcheck/cmd/entrypoint/labels/user.
   diff <(jq -S . /home/nwm/nhms-db-hostconfig-$TS.json) \
        <(docker inspect nhms-db | jq -S '.[0].HostConfig')
   diff <(jq -S . /home/nwm/nhms-db-config-$TS.json) \
        <(docker inspect nhms-db | jq -S 'del(.[0].Config.Env) | .[0].Config')

   # (d) Display is live.
   curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8080/healthz
   ```

   Expect: one row (`?column? = 1`) from the `ghdc`-resident chunk — a
   `could not open file` error here means the mount is wrong; `ghdc` at
   `/home/postgres/pgdata/tablespaces/ghdc`; `--check` exit `0` on the
   tag path (on the `$IMAGE`/`$IMAGE_DIGEST` branches an
   `nhms_db_image_ref`-only drift is the expected, recorded outcome — any
   other drifted key: stop, §4.4); the `HostConfig` diff showing only the
   intended third mount; and `200` from the display API. Then restart the
   timers stopped in step 1, and delete
   `/home/nwm/nhms-db-hostconfig-$TS.json` (it is spec-only, no credentials,
   but there is no reason to accumulate them).

Rollback: `docker stop nhms-db && docker rm nhms-db && docker rename
nhms-db-pretbs-$TS nhms-db && docker start nhms-db`. Valid only while no chunk
has been moved to `ghdc`; once data lives there the old container cannot serve
it, and recovery means restoring the mount, not the container.

### 4.4 host-contract snapshot 漂移处置 (`#1089`)

`packages/common/node27_container_contract.py` pins three MEASURED node-27 host
contracts — `CONTAINER_PG_RESTORE_REALPATH`, `SYSTEMD_UNSET_TIMESTAMP`,
`CLIENT_BACKEND_TYPE`. CI cannot observe the host they were measured on, so a
systemd / docker / PostgreSQL / TimescaleDB upgrade (or a moved `pg_wrapper`
symlink) drifts them silently and the drift surfaces first inside an authorized
mutation window as a G-class misjudgment. `scripts/node27_external_contract_snapshot.py`
re-measures them live, READ-ONLY, and diffs against the committed baseline
`packages/common/node27_external_contract_snapshot.json`.

Read-only by construction: the only argvs it can spawn are `systemctl --user
show`, `systemctl --version`, `docker --version`, `docker inspect`,
`docker exec nhms-db /usr/bin/readlink -f ...`, `docker exec nhms-db
/usr/bin/pg_restore --version`, and `psql -c` with SELECT/SHOW-only SQL from a
frozen tuple. `--check` never writes a file; `--dump` writes only stdout or an
explicit `--output`. There is no auto-update path.

**Invocation (out-of-band, any time, no mutation window needed):**

```bash
ssh -p 32099 nwm@210.77.77.27
cd /home/nwm/NWM && git status --porcelain && git pull --ff-only
set -a; . infra/env/node27-timeseries-compression-replay.env; set +a   # PG env, never embedded in the repo
echo "XDG_RUNTIME_DIR=${XDG_RUNTIME_DIR:-<unset>}"                    # must be /run/user/$(id -u)
export XDG_RUNTIME_DIR=/run/user/$(id -u)                             # if unset
uv run python scripts/node27_external_contract_snapshot.py --check; echo "exit=$?"
```

`systemctl --user` locates the user manager through `$XDG_RUNTIME_DIR`; with it
unset the probe exits non-zero with "Failed to connect to bus"
(`scripts/node27_timeseries_compression_supervisor.py:187-194`) and the check
reports a probe-execution failure — that is a broken probe, not a verdict about
the host. Fix the environment and rerun.

|exit|meaning|action|
|---|---|---|
|0|no drift, fixture aligned with the contract module|continue|
|2|usage/input error (bad CLI, missing or malformed fixture)|fix the invocation; not a host verdict|
|4|MISALIGNMENT: fixture `contract` section ≠ `node27_container_contract` constants (decided BEFORE any probe runs, so nothing was measured). The same exit also fires when the contract module cannot be IMPORTED at all — the section is then unverifiable, which fails closed identically|read the report line: a constant-vs-fixture mismatch is a repo-side bug (fix in a PR, never on the node); an import failure is an on-node environment fix — run from the repo root or `PYTHONPATH=/home/nwm/NWM uv run python scripts/node27_external_contract_snapshot.py --check` and rerun, no PR needed|
|3|DRIFT: a compared value moved; the report names `section.key`, expected and observed|stop; run the loop below|
|5|PROBE-EXECUTION FAILURE: a probe could not run, exited non-zero, exited 0 empty, or exited 0 without the property it measures. A zero-exit EMPTY result lands here too (e.g. the `timescaledb` extension row disappearing returns nothing, not a changed value), so a genuine host change can present as a probe failure|the probe is usually broken (env, daemon, container down). Never treated as drift; repair and rerun — but check the probe the report NAMES against the host first, before concluding "probe bug"|

**Drift handling loop (never a silent fixture update):**

1. Capture the `--check` report and a fresh `--dump` verbatim (`uv run python
   scripts/node27_external_contract_snapshot.py --dump --output
   ~/node27-contract-snapshot-$(date -u +%Y%m%dT%H%M%SZ).json`). Both are PR
   evidence.
2. Decide, in PR review, one of exactly two outcomes:
   - **accept the new contract** — update the fixture, the
     `node27_container_contract.py` constants, and the hermetic-lock
     mutation-RED tests bound to them TOGETHER in one PR (the alignment guard
     in `tests/test_node27_external_contract_snapshot.py` reds if any of them
     moves alone), or
   - **roll back the host change** — revert the upgrade / restore the image and
     rerun `--check` until it exits 0.
3. Never commit a fixture update alone to make the check green. A `contract.*`
   drift means a value that supervisor/verifier predicates are bound to has
   moved; the consumers must be re-reviewed in the same PR.

**The patch-version-only drift class.** Ubuntu unattended security upgrades bump
`host_context.docker_version` / `host_context.systemd_version` patch strings
without touching any pinned behaviour. Handling: confirm no semantic change —
the `contract` section values are unchanged, the drift report names ONLY
`host_context.*` version strings, and the version moved by a patch component —
then update the fixture via PR with the `--dump` attached. This exception is
deliberately narrow: any drift naming `contract.*`, `nhms_db_image_id`,
`nhms_db_image_ref` or a MAJOR/MINOR version component is NOT a patch bump and
goes through the full loop above. Do not let "it's just a version bump" become
the default answer — a check operators mindlessly re-baseline is a check that
does nothing (the G9 lesson inverted).

**Limitation (do not over-read a green check).** The unset-timestamp contract is
witnessed through a reserved never-existing unit
(`nhms-external-contract-snapshot-witness-does-not-exist.service`), because the
real recurring compression unit has run this boot (daily 04:25 UTC timer) and so
renders a real timestamp. That witnesses systemd's *rendering* contract only —
it says nothing about the live state of the recurring unit at any checkpoint, so
a green `--check` still does NOT imply the mutation-window checkpoints pass.
What the fixture's `informational.recurring_unit` counter-evidence (the real
unit's live ActiveState/SubState/ExecMainStartTimestamp) used to expose was a
consumer defect: both planes gated the window on a whole-dict equality that
pinned the never-started rendering, so the first checkpoint after any timer tick
aborted. **Resolved in `#1255`**: the gate is now the four-field
current-activity/identity predicate `recurring_unit_idle_divergences`
(`packages/common/node27_container_contract.py`), bound by
`scripts/node27_timeseries_compression_supervisor.py` and
`scripts/node27_timeseries_compression_live_evidence.py`. `InvocationID` and
both `ExecMainStartTimestamp*` fields stay captured in the checkpoint show
document as evidence (key set and types pinned verifier-side) and no longer
gate — so an inactive/dead unit that ticked earlier this boot, exactly what
`informational.recurring_unit` records, is now an admitted checkpoint.

**Scheduling is operator-gated.** #1089 installs no timer and no GitHub Actions
workflow. Until an operator schedules one (a weekly `--check` on the node is the
intended shape), §4.0 step 3 is the sole pre-mutation interception point.
`informational` (measured_at, hostname, backend_type distribution, the real
recurring unit's state) is dump-recorded and NEVER compared, so a scheduled
check cannot flake on autovacuum or parallel-worker noise.

### 4.5 大 chunk 追赶（timeout 墙 override，`#1156`）

A terminal chunk that outgrows the per-chunk statement timeout cannot pass the
automated lane, and because selection is oldest-first (`ORDER BY range_end
ASC`, then `eligible[:per_tick_bound]`) that one chunk is re-selected every
tick and burns the whole tick, blocking everything behind it. Since `#1156` the
three walls are operator-configurable through the single compression env file,
so the fix is a bounded override window rather than a manual
`statement_timeout = 0` DDL.

**The defaults already cover the steady state — check before you override.**
`#1352` resized them to `3600000` ms / `3900` s / `3940` s against measured
node-27 numbers: compressing `hydro.river_timeseries` chunk
`_hyper_3_32_chunk` (268 GB) took 1607 s on 2026-08-10, i.e. **~6.0 s/GB**, and
`chunk_compression_stats` puts steady-state weekly chunks of that hypertable at
268–409 GB (1600–2450 s). The 60-minute default therefore covers ~600 GB, about
1.5× the largest chunk observed to date, and the 2026-07-25/26 incident chunk
(333 GB, roughly 20 minutes) would pass unaided today. Override only for a
chunk that measures beyond that envelope — first estimate it as
`pg_total_relation_size / 1 GB × 6 s` and compare against the 3600 s ceiling.

**Runner wall ≠ supervisor wall.** This section tunes the *recurring runner*
lane: `NODE27_TIMESERIES_COMPRESSION_WRAPPER_WALL_SECONDS`, consumed by
`scripts/node27_timeseries_compression_once.sh` as the `timeout` DURATION, plus
`nhms-node27-timeseries-compression.service`'s `TimeoutStartSec`. The
`--wall-seconds 900` on `nhms-node27-timeseries-compression-replay.service` and
the supervisor's own hard wall (§4.0.1) are a *different* lane with a different
knob; nothing here changes them, and they do not follow this env file.

**The four values** (one env file,
`/home/nwm/NWM/infra/env/node27-timeseries-compression.env`, mode 0600):

|variable|catch-up value|rule|receipt echo (schema 2.1+)|
|---|---|---|---|
|`NODE27_TIMESERIES_COMPRESSION_COMPRESS_TIMEOUT_MS`|measured chunk duration × ~1.5, in ms (e.g. `5400000` for a 900 GB chunk at ~6.0 s/GB)|minimum 1000; must exceed the `3600000` default or there is nothing to override|`budget.compress_timeout_ms`|
|`NODE27_TIMESERIES_COMPRESSION_WRAPPER_WALL_SECONDS`|`ceil(COMPRESS_TIMEOUT_MS/1000) + 60` is the enforced **floor**, not the sizing recipe — budget real headroom, `+300` or more (e.g. `5700`)|leg 1 of the invariant|`budget.wrapper_wall_seconds`|
|`NODE27_TIMESERIES_COMPRESSION_SYSTEMD_WALL_SECONDS`|`WRAPPER_WALL_SECONDS + 40` or more (e.g. `5740`)|leg 2; a **declared** value — it must equal the drop-in you actually installed|`budget.systemd_wall_seconds`|
|`NODE27_TIMESERIES_COMPRESSION_PER_TICK_BOUND`|`1`|leg 3: raising the timeout above `3600000` with a bound above `1` is refused outright; the invariant bounds ONE chunk's budget, so a second chunk in the same tick still dies on the wall|`per_tick_bound`|

The runner refuses to open a database connection if any of the three legs is
violated, and the wrapper refuses to launch on a non-positive-integer wall.
Both refusals are structured JSON on stderr.

**Every tick's receipt records the configuration that tick resolved** (issue
`#1351`, receipt `schema_version` `"2.1"`): the `budget` object plus
`per_tick_bound` are the record of the four values above, so a catch-up tick
and a default tick are distinguishable after the fact — read them, do not
reconstruct them from the env file, which may already have been rolled back.
Read each field for what it is worth:

- `budget.compress_timeout_ms` and `per_tick_bound` were **applied** by that
  tick — the timeout as the per-chunk `SET statement_timeout`, the bound as the
  selection cap.
- `budget.wrapper_wall_seconds` is **parsed and invariant-checked** by the
  Python, but enforced by `scripts/node27_timeseries_compression_once.sh`,
  which reads its own copy of the variable; the receipt shows what the runner
  read, not what `timeout` was actually given.
- `budget.systemd_wall_seconds` is a **declaration echo** — the process cannot
  read the unit file (`scripts/node27_timeseries_compression.py:111-116`), so
  the receipt only proves what the env file declared. Check 2 below
  (`systemctl --user show -p TimeoutStartUSec`) is the only step that queries
  the user unit manager for the installed wall (scope fixed per issue `#1387`).

The only receipt without a `budget` block is the config tombstone
(`outcome: "failed"`, `failure.stage: "config"`), written when the
configuration was refused and no budget was ever in force.

**Why `+60` is a floor and not a size.** Leg 1 is the *minimum* the runner will
accept; it is not a measurement of what a tick costs outside `compress_chunk`.
A single catch-up tick also pays for the display-watermark resolution, two
10-second `git` lineage probes, the catalog enumeration (`fetch_chunks`), a
chunk size measurement before *and* after the compress, and — on the error
path — a reconcile probe. Every one of those DB legs is a 10-second connect
plus a statement capped at 60 seconds (`_QUERY_TIMEOUT_MS`), and those caps are
genuinely reachable under lock contention on this node (§8.6 documents the same
60-second catalog/measurement cap being hit by concurrent
`compress_chunk`/`decompress_chunk` work). Worst case that is roughly 300
seconds of non-compress budget, so a wall sized at `compress + 60` can `TERM`
the tick *after* a successful compress, during measurement or reconcile, and
lose the receipt. Size the wall at `ceil(COMPRESS_TIMEOUT_MS/1000) + 300` or
more for a catch-up; the example row above (`5400000` ms → `5700` → `5740`)
uses that shape.

**Mandatory ordering.** The steps are ordered against a specific failure mode
(`b21e2453`): a tick that passes the Python check against a *declared* systemd
wall and then hits a smaller *real* one, taking `TERM` mid-DDL.

1. **systemd drop-in FIRST, then reload.** Before touching the env file:

   ```bash
   mkdir -p ~/.config/systemd/user/nhms-node27-timeseries-compression.service.d
   printf '[Service]\nTimeoutStartSec=5740\n' > \
     ~/.config/systemd/user/nhms-node27-timeseries-compression.service.d/override.conf
   systemctl --user daemon-reload
   systemctl --user show -p TimeoutStartUSec nhms-node27-timeseries-compression.service
   ```

   The last line must report the new wall; the committed unit ships
   `TimeoutStartSec=3940` and the repository is not edited for a catch-up.
   This tier has **no system-level unit** (see the install section: "Do NOT
   install system-level (root) units for this tier"): a root/system-scope
   variant of any command in this section (`systemctl` without `--user`, or a
   drop-in under `/etc/systemd/system`) succeeds silently against the *system*
   manager while the real user unit is untouched — never use system scope here.
2. **Stop and mask the timer for the whole window**, and keep the other
   compression lanes out of it:

   ```bash
   systemctl --user stop nhms-node27-timeseries-compression.timer
   systemctl --user mask nhms-node27-timeseries-compression.timer
   ```

   A scheduled tick inside the window would read the override env, run to the
   raised wall, and also overwrite the default receipt path. Do **not** start
   `nhms-node27-timeseries-compression-replay.service` or any supervisor
   compression task while the override is in place: those children re-enter the
   *same* wrapper and fall back to this *same* default env file
   (`CHILD_ENV_ALLOWLIST` carries no `..._ENV_FILE`), yet their own
   `--wall-seconds 900` / `TimeoutStartSec=920` are untouched by this
   procedure — that is the `TERM`-mid-DDL shape reproduced in another lane.
3. **Snapshot the env file, then set the four values** (table above), keeping
   mode 0600 and `nwm:nwm` ownership. The content snapshot is what the cleanup
   check in step 4 diffs against, so it must be taken *before* the first edit:

   ```bash
   cp -p /home/nwm/NWM/infra/env/node27-timeseries-compression.env \
     ~/node27-compression-env.pre-catchup
   stat -c '%a %U:%G' ~/node27-compression-env.pre-catchup   # 600 nwm:nwm
   ```

   `cp -p` preserves the 0600/`nwm:nwm` mode — the snapshot holds the same DSN
   as the live file, so it is a secret and stays under `~nwm`. Also record
   `stat -c '%a %U:%G'` on the live file before and after the edit; never print
   the DSN.
4. **Dry-run, then enforce, then clean up — in that order.** Run 4a and 4b
   inside a `tmux`/`screen` session (same convention as §4.3.3): the enforcing
   tick can run ~30 minutes, and an ssh hangup on a bare foreground invocation
   kills the process group mid-DDL and leaves you with no terminal output —
   only whatever receipt was already flushed.

   ```bash
   # a. dry-run tick: confirm the selection set is the intended chunk
   /home/nwm/NWM/scripts/node27_timeseries_compression_once.sh \
     --receipt-path /home/nwm/node27-compression-catchup-dryrun.json
   # b. the single enforcing tick, distinct receipt path (the lock path is
   #    deliberately shared, so a stray timer tick still cannot overlap)
   /home/nwm/NWM/scripts/node27_timeseries_compression_once.sh --enforce \
     --receipt-path /home/nwm/node27-compression-catchup-enforce.json
   ```

   Cleanup order is as hard a requirement as the setup order, and it is the
   mirror image: **delete the env override FIRST**, then unmask and start the
   timer (`systemctl --user unmask/start`), then remove the user-scope drop-in
   and `systemctl --user daemon-reload`, then confirm the
   next default tick writes to the default receipt path (the catch-up receipts
   were per-invocation `--receipt-path` files; nothing restores itself).
   Removing the drop-in or unmasking the timer while the env override is still
   in place leaves exactly the b21e2453 configuration — a tick that passes the
   Python leg-2 check against a declared 5740 and then hits the real 3940
   mid-DDL — and it re-arms the replay-lane hazard from step 2. **The catch-up
   is not finished while any override residue exists**; verify with all three
   checks below — the two configuration checks first, and stamp `CLEANUP_AT`
   as you start them:

   ```bash
   # record the instant cleanup completed — check 3's freshness anchor.
   # Write it down: check 3 runs after the next 04:25 UTC tick, likely in
   # another shell session.
   CLEANUP_AT=$(date -u +%Y-%m-%dT%H:%M:%SZ); echo "$CLEANUP_AT"
   # env file byte-identical to the pre-window snapshot (no output = clean)
   diff ~/node27-compression-env.pre-catchup \
     /home/nwm/NWM/infra/env/node27-timeseries-compression.env
   # real systemd wall back to the committed 3940 s
   systemctl --user show -p TimeoutStartUSec nhms-node27-timeseries-compression.service
   ```

   Both checks read the *intended* configuration. The third check reads what a
   real tick **resolved**: after the first default timer tick following the
   cleanup, confirm its receipt carries the default budget triple and bound
   (issue #1351). Unlike the two checks above it cannot be satisfied by an env
   file that no running tick has picked up yet — but read it for what it
   proves: the runner-side effective `compress_timeout_ms` and
   `per_tick_bound` were actually applied by that tick, while
   `systemd_wall_seconds` is only the declaration that tick read. **Check 2 is
   the only step that queries the unit manager for the installed wall**; check
   3 does not replace it. All §4.5 unit-manager commands — the drop-in
   install, the timer `stop`/`mask`, and check 2 — target the *user* manager
   (`systemctl --user`), matching this tier's user-scope install. The
   historical system-scope variants of these commands (fixed per issue
   `#1387`) succeeded silently against the system manager while the user
   timer kept firing — the dangerous half was `stop`/`mask`, which reported
   success while stopping nothing.

   Check 3 must establish *freshness before budget*, in that order. §4.5's own
   window stopped and masked the timer, and both catch-up ticks wrote
   per-invocation `--receipt-path` files, so the default path still holds the
   **pre-window** receipt until a post-cleanup tick overwrites it in place. Nor
   does absence rescue you: a wall-`TERM`ed first post-cleanup tick (the exact
   shape env residue produces) writes no receipt at all and leaves the old
   clean one sitting there. Asserting the budget against that stale file is a
   false pass.

   The freshness anchor is `CLEANUP_AT` — the UTC instant you recorded when
   cleanup completed — **not** the timer's LAST column. §4 "Per-tick capacity"
   compares `generated_at` against the last trigger from
   `systemctl --user list-timers`; that predicate answers a different question
   and is unsound here, because this window masked the timer and `Persistent=true`
   catches up only on a *missed elapse*, which a same-day window produces none
   of. After the unmask the LAST column therefore still reports the
   **pre-window** 04:25 trigger, and the untouched pre-window receipt
   (`generated_at` 04:25:30 ≥ trigger 04:25:00, default budget) would pass
   every branch with zero post-cleanup ticks having run. `generated_at >=
   CLEANUP_AT` subsumes that comparison — cleanup necessarily completes after
   the last pre-window trigger — so the `list-timers` read stays only as a
   diagnostic: it tells you when the next tick is due.

   ```bash
   # 1. diagnostic only — when is the next default tick due? (NOT the anchor)
   systemctl --user list-timers nhms-node27-timeseries-compression.timer

   # 2. the default-path receipt must be POST-CLEANUP and carry the defaults
   # CLEANUP_AT must be the value you recorded at cleanup completion above;
   # unset (e.g. a fresh shell) aborts here rather than defaulting to anything.
   : "${CLEANUP_AT:?not set — record it at cleanup completion (see the cleanup block above) before running check 3}"
   /home/nwm/NWM/.venv/bin/python - "$CLEANUP_AT" <<'PY'
   import json, sys
   from datetime import datetime

   cleanup_at = datetime.fromisoformat(sys.argv[1].replace("Z", "+00:00"))
   receipt = json.load(open("/home/nwm/NWM/artifacts/receipts/node27_timeseries_compression.json"))
   version = receipt.get("schema_version")
   print(version, receipt.get("generated_at"), receipt.get("outcome"),
         receipt.get("budget"), receipt.get("per_tick_bound"))

   # (1) is there a post-#1351 receipt here at all? Not a budget verdict.
   # Every version that carries `budget` counts — "2.1" (#1351) onwards;
   # an ordered lower bound survives any future bump that has nothing to do
   # with budgets, without needing an edit here.
   assert tuple(map(int, version.split("."))) >= (2, 1), (
       f"no post-#1351 default tick at this path yet (schema_version={version})"
   )

   # (2) freshness — only a receipt written after cleanup completed says
   #     anything about the cleanup.
   generated_at = datetime.fromisoformat(receipt["generated_at"].replace("Z", "+00:00"))
   assert generated_at >= cleanup_at, (
       "receipt predates cleanup — no post-cleanup default tick has run yet; "
       "wait for the next 04:25 UTC tick"   # NOT a pass
   )

   # (3) only now does the budget describe the current configuration.
   budget = receipt.get("budget")
   assert budget is not None, (
       "config tombstone (failure.stage=config): the configuration was REFUSED "
       "and no budget was ever in force — the env file is still broken"
   )
   assert budget == {
       "compress_timeout_ms": 3600000,
       "wrapper_wall_seconds": 3900,
       "systemd_wall_seconds": 3940,
   }, "catch-up budget still in force"
   assert receipt.get("per_tick_bound") == 4, "catch-up budget still in force"
   PY
   ```

   Steps (1) and (2) fail with their own diagnosis on purpose: a `2.0` receipt
   or one whose `generated_at` predates `CLEANUP_AT` is "no post-cleanup tick
   has run yet", **not** "the budget is wrong" and **not** a pass. Re-run the check after the next 04:25 UTC tick;
   if the unit went `failed` instead, that is §4's `rc=124` wall path, not a
   budget question.

   Only delete `~/node27-compression-env.pre-catchup` after the `diff` is
   clean. A `git status`-clean worktree is *not* one of these three checks — the
   env file is gitignored (`.gitignore:18 infra/env/*`), so `git status` can
   never see env residue; it is still worth a glance for the different failure
   of someone having edited the committed unit file instead of installing the
   drop-in.

A manual silent-window `SET statement_timeout = 0; SELECT compress_chunk(...)`
is the **last resort**, not the procedure: it produces no receipt, no
provenance and no bounded lock, and it is what this section exists to avoid.

**What a wall leaves behind, and how to clear it.** Whichever of the three
walls trips — the `statement_timeout` on the DDL, the real systemd
`TimeoutStartSec` taking `TERM` mid-DDL, or `/usr/bin/timeout` inside
`scripts/node27_timeseries_compression_once.sh` — the tick exits nonzero, and
because `nhms-node27-timeseries-compression.service` is `Type=oneshot` with no
`Restart=`, the unit is left `failed/failed` with `MainPID=0`. It stays that way
until the next timer tick overwrites the state, or until an operator runs
`systemctl --user reset-failed nhms-node27-timeseries-compression.service`. That
is a *residue*, not a running job: the mutation-window checkpoint gate (the
replay supervisor of §4.0.2 and the §4.0.1 live-evidence twin, which bind one
shared predicate) refuses such a window with a dedicated message that says the
unit is failed from an earlier tick, not running, and names that `reset-failed`
as the remedy. This does **not** conflict
with the "do not `reset-failed` to manufacture a clean state" rule in §4.0
step 4: that rule guards the ADJACENT `nhms-node27-autopipe.service` during the
gated first-enforce evidence capture, where the failed state is itself the
evidence being preserved — a different unit in a different phase.

**Scope vs §4.0 step 9.** The blanket "do not call `compress_chunk` manually"
in the gated first-enforce protocol (§4.0 step 9) belongs to that one-shot
forensic evidence run. This section governs incident catch-up on a lane that is
already live. The two do not overlap and neither relaxes the other.

### 4.6 River identity normalization: backfill + cutover (`#1339`)

Migration `000050_river_identity_normalization.sql` adds integer surrogate keys
to the four authority tables, three native enums, and seven **nullable** columns
on `hydro.river_timeseries`. It backfills nothing and switches nothing. This
section is the operating procedure for the two things that follow: filling
those columns (routine, repeatable, online) and switching the primary key +
compression settings onto them (a one-shot maintenance window).

All timings below are measured, not estimated — node-27 throwaway database
`nhms_1339_probe`, 2026-08-15, PG 15.2 / TimescaleDB 2.10.2. Full log:
`openspec/changes/river-identity-normalization-backfill/probe-1339-throwaway.md`.

#### 4.6.1 Applying migration 000050 (low peak)

Adding `INTEGER GENERATED ALWAYS AS IDENTITY` cannot use the fast-default path,
so each authority table is fully rewritten under `ACCESS EXCLUSIVE`:

| Table | Rows | Measured | Notes |
|---|---|---|---|
| `core.river_segment` | 209,126 | **~10 s** (extrapolated from 6.6 s at 228 MB) | 355 MB, 7 indexes; on the MVT read path |
| `hydro.hydro_run` | 3,609 | 61 ms | |
| `core.basin_version` | 20 | 46 ms | |
| `core.river_network_version` | 20 | 47 ms | |

The migration sets `lock_timeout = '2s'` before each `ALTER`. That bounds the
**wait** for the lock, not the rewrite: if a long-running reader holds the
table, the statement fails with `canceling statement due to lock timeout`
instead of queueing in front of every subsequent reader. On that failure,
re-run — every block is idempotent (a re-applied `ADD COLUMN IF NOT EXISTS
... IDENTITY` is a 0.563 ms no-op, measured). Apply at low peak.

#### 4.6.2 Backfill (routine, online)

```bash
# Dry-run FIRST, always. Reports per-chunk rows/bytes/pending counts, the batch
# plan, estimated bloat against /home headroom, and the compressed/active skip
# lists. Mutates nothing.
NODE27_RIVER_IDENTITY_BACKFILL_RECEIPT_PATH=/home/nwm/receipts/river-identity-$(date -u +%Y%m%dT%H%M%SZ).json \
NODE27_RIVER_IDENTITY_BACKFILL_LOCK_PATH=/home/nwm/locks/river-identity-backfill.lock \
  uv run python scripts/node27_river_identity_backfill.py

# Optional: one REAL batch, timed, with pg_locks observation, then rolled back.
  uv run python scripts/node27_river_identity_backfill.py --probe

# Enforce. See the mandatory preconditions below.
  uv run python scripts/node27_river_identity_backfill.py --enforce
```

**Mask the compression timer for the whole enforce window.** This is not
advisory. The timer fires daily at 04:25 UTC with `PER_TICK_BOUND=4`; a tick
landing mid-backfill compresses a chunk that still holds NULL rows, and the
only way to reach those rows again is decompressing 200+ GB.

```bash
systemctl --user mask --now nhms-node27-timeseries-compression.timer
# ... backfill ...
systemctl --user unmask nhms-node27-timeseries-compression.timer
systemctl --user start nhms-node27-timeseries-compression.timer
```

The runner records the timer's `is-active` / `is-enabled` in every receipt, so
"was the timer masked?" is answerable after the fact rather than from memory.

**Disk headroom precheck.** A full-row `UPDATE` writes a new tuple version for
every row: the heap roughly doubles, and the indexes churn with it, until
`VACUUM` reclaims the dead versions. The runner reports
`disk.estimated_bloat_bytes` against `disk.avail_bytes` on `/home`. Recompute
at execution time — `/home` was 576 G free on 2026-08-15 and that number moves.

**`VACUUM` each chunk as it completes.** Not optional either. Post-#1338 the
display read shape depends on a `river_timeseries_pkey` Index Only Scan
(000049:20); a full-row rewrite clears the visibility map's all-visible bits and
those scans fall back to heap fetches until the map is rebuilt.

```bash
docker exec nhms-db psql -U nhms -d nhms -c 'VACUUM (ANALYZE) _timescaledb_internal._hyper_3_NN_chunk;'
```

Watch display read latency across the backfill as a regression observation
point.

**Chunks the runner will not touch, and what to do about them.**

- *Compressed* — TimescaleDB 2.10 permits no DML against compressed storage.
  Orchestration is decompress → backfill → recompress, reusing the existing
  runners; this runner deliberately embeds no decompression (single
  responsibility, and decompression is already receipted elsewhere):
  1. decompress the chunk via the decompression-replay procedure (§4.3),
  2. re-run the backfill (the chunk is now eligible),
  3. `VACUUM` it,
  4. recompress via `scripts/node27_timeseries_compression.py --enforce`.
- *Active* — the chunk ingest is currently writing. Backfilling it would
  contend for row locks and never converge, because ingest keeps producing new
  NULL rows. It is picked up automatically on a later round once it becomes
  terminal. The only override is `--final-sweep`, which is a cutover
  precondition, not routine operation — and it refuses unless it first observes
  the chunk's write counters frozen across an observation window.

**Fail-closed stops.** A `stopped` receipt is not a completed run. Five causes,
distinguished in `stop.stage`:

- `shortfall` — the batch found more sentinel candidates than it could update.
  `stop.unmatched_rows` counts rows no authority row resolves (referential rot);
  `stop.unmappable_rows` counts text values outside the enums. Both are data
  decisions: escalate, do not widen the enum or invent an authority row to make
  the runner proceed. **First check the double-zero case:** when
  `stop.unmatched_rows == 0` *and* `stop.unmappable_rows == 0` (the reason text
  says so explicitly), this is almost certainly not rot. The candidate count and
  the UPDATE are two READ COMMITTED snapshots of one transaction, so a DELETE
  that commits between them — or any concurrent write that moves rows out of the
  block range; the output parser's re-parse delete window on a terminal chunk is
  the routine channel — inflates the shortfall while leaving
  both diagnostics at zero, because they run after the UPDATE. Check that
  re-parse window first; if it explains the gap, simply re-run the backfill (the
  batch was rolled back and the cursor rewound, so it is retried intact). Only a
  non-zero diagnostic count is a data-corruption escalation.
- `duration_wall` — a batch exceeded the wall even after one halved-range retry.
  Lower `NODE27_RIVER_IDENTITY_BACKFILL_BATCH_PAGES` or raise
  `..._DURATION_WALL_MS`; do not loop the runner against a struggling database.
- `lock_contention` — the batch UPDATE failed with SQLSTATE `55P03`
  (lock_not_available) or `40P01` (deadlock_detected); the SQLSTATE is in
  `stop.reason`. This is an overlap problem, not a batch-size problem: pause the
  ingest writer, wait out the idle window, then rerun. A plain `--enforce` run
  only ever reaches *terminal* chunks, so on the routine path that pause (plus
  waiting out the output parser's re-parse window on that chunk) is the whole
  remedy — `--final-sweep` is **not** a terminal-chunk remedy; its quiescence
  gate covers the *active* chunk only, and the flag stays what it is elsewhere
  in this runbook, a cutover precondition. Do **not** lower
  `..._BATCH_PAGES` or raise `..._DURATION_WALL_MS` — halving a range shortens
  the scan, not the lock wait — and the runner deliberately does not retry.
  `--probe` does not classify at all: a lock failure under `--probe` surfaces as
  `failure.stage: "runner"` (with the exception class name on stderr), not as a
  `stop.stage` receipt.
  Caveat on coverage: until `SET LOCAL lock_timeout` is adopted (a live-batch
  behaviour change that needs its own node-27 dry-run), a pure lock *wait* still
  runs out the statement timeout and is reported as `duration_wall`; only
  deadlocks (`40P01`) reach this stage today. So a `duration_wall` stop on a
  chunk that ingest may still be touching deserves one look at lock waits before
  it is treated as slowness.
- `ingest_not_quiescent` — `--final-sweep` was asked to touch the active chunk
  while its write counters were still moving. Nothing was written. Complete the
  ingest pause (step 1 of the cutover sequence in 4.6.3), confirm it, and rerun;
  do not drop the flag to make the refusal go away, because that just leaves the
  active chunk unfilled and the cutover's `VALIDATE` will fail on it later.
- `compressed_chunk_guard` — the shared write guard refused a batch. Read
  `stop.reason` to tell the two cases apart: a **compressed chunk** (the
  compression timer fired mid-run — mask the timer, then run the
  decompress → backfill → recompress procedure above for that chunk) versus a
  **guard lookup failure or a chunk missing from the catalog** (infrastructure:
  the chunk was dropped or renamed, or the catalog query failed — investigate
  before rerunning; the guard refuses to certify what it cannot see). Batches
  committed before the refusal are kept, counted, and resumable — the receipt's
  `cursor` points at the batch that was refused.

#### 4.6.3 Cutover (one-shot maintenance window)

There is **no work that can be done ahead of the window.** With
`timescaledb.compress = true` in force, TimescaleDB 2.10.2 rejects
`ADD CONSTRAINT ... CHECK`, `VALIDATE CONSTRAINT`, `SET NOT NULL`,
`CREATE UNIQUE INDEX` and `DROP CONSTRAINT` alike — measured with **zero**
compressed chunks present, so it is the setting and not the data that blocks
them. Disabling the setting requires the whole hypertable decompressed. The
usual escape hatches are all closed too: `ADD CONSTRAINT ... PRIMARY KEY USING
INDEX` is unsupported on hypertables, `CREATE INDEX CONCURRENTLY` is rejected,
and `WITH (timescaledb.transaction_per_chunk)` is incompatible with `UNIQUE`.

Ordered sequence — do not reorder:

1. **Pause ingest.**
2. **`--final-sweep`** to fill the last chunk. It asserts write quiescence
   first and refuses if anything is still writing.
3. **`verify`** — read-only, no locks, run it before committing to the window:

   ```sql
   SELECT * FROM hydro.verify_river_identity_normalization();
   ```

   All ten counts must be zero except `rows_total`. A non-zero
   `equality_audit_divergent` means the ingest writer's `ON CONFLICT DO UPDATE`
   branch refreshed text columns after the surrogate columns were filled; fix it
   with a re-sweep, do not proceed. **This is a human gate** — the cutover
   function does not re-run these counts (that would be a second full scan, and
   the in-transaction `VALIDATE` already guarantees zero NULLs).
4. **Decompress every chunk** via the decompression-replay runner. Budget the
   space: the two compressed chunks alone were 268 GB and 215 GB before
   compression. **Recompute headroom at execution time**, and add two more
   consumers on top: the new integer primary-key index, and the sort space its
   build needs. Compressed chunks accumulate over time, so this requirement
   only grows — this is an argument for doing it sooner rather than later.
5. **Cutover**, one transaction:

   ```sql
   SELECT hydro.cutover_river_identity_normalization();
   ```

   It refuses if any chunk is still compressed. Inside, in order: disable
   compression → drop the text foreign key → seven `CHECK ... NOT VALID` +
   `VALIDATE` + `SET NOT NULL` → drop the old primary key → add the integer
   primary key → re-enable compression with the integer segmentby/orderby.
   Calling it twice is a no-op, not an error.
6. **Recompress** via `scripts/node27_timeseries_compression.py --enforce`, then
   unmask the timer.

**Cost shape inside the window.** `VALIDATE CONSTRAINT` measured ~0.5 s per
column per 3M rows, so budget on the order of ten minutes for seven columns at
460M rows. `SET NOT NULL` is then scan-free (0.59–0.79 ms measured, against
1161 ms without the validated check). The dominant cost is the primary-key index
build, which happens inside the window and holds `ACCESS EXCLUSIVE` — **display
reads are blocked for its duration**. The measured base rate is 2.969 s for
3.024M rows; at 460M this is superlinear (sort and `maintenance_work_mem`
bound), so **re-measure on a larger toy before scheduling the window** rather
than extrapolating linearly from that figure.

**Abort / rollback.** If the transaction fails at any point — most likely at
`VALIDATE` if a NULL slipped through — everything reverts. The table is left
exactly as it was before the call: `compress = true`, zero compressed chunks,
old primary key and text foreign key intact, no leftover check constraints.
That rollback fidelity is pinned by the node-27 throwaway integration test
(`tests/test_river_identity_normalization_integration.py`, negative path:
catalog snapshot before == after), not by the probe log — the probe ran
statement-by-statement in autocommit and never exercised an abort. Recovery is
simply to recompress with the compression runner. There is no half-cut-over
state and no compensating action to write.

**Precondition checklist before opening the window:**

- [ ] Write-path `ON CONFLICT` target adapted to the new primary key (this is
      the follow-up issue's work — the cutover changes the conflict target, and
      `workers/output_parser/parser.py` still names the text columns).
- [ ] Display **read** path switched to the integer keys, or a covering index
      on the text columns built to replace the dropped primary key. 000049
      measured that the old text primary key is the *only* index serving the
      MVT and stats-probe shapes ("no Seq Scan fallback"), and
      `services/tiles/mvt.py`, `apps/api/routes/hydro_display.py` and
      `packages/common/forecast_store.py` still filter on the text columns.
      Cutting over without this turns every display query into a sequential
      scan of a 249 GB table. Belongs to the same follow-up switch issue as the
      write-path item above.
- [ ] Ingest paused and confirmed quiescent.
- [ ] `verify` returns zeros.
- [ ] Disk headroom recomputed for full decompression + index build + sort.
- [ ] Compression timer masked.
- [ ] Low-peak window, display-read blocking announced.

**Retiring the text foreign key early is a real, accepted loss.** TimescaleDB
2.10.2 requires foreign-key columns to be covered by segmentby, and the target
segmentby is integer-only, so
`river_timeseries_river_segment_id_river_network_version_id_fkey` cannot
survive the cutover (measured:
`ERROR: column "river_segment_id" must be used for segmenting`). Between this
cutover and the text-column-retirement issue, the fact table has no
database-enforced referential link to `core.river_segment`. Recorded in the
ADR 0002 amendment.

### 4.7 ingest 前沿 chunk 统计漂移 (`#1378`)

生产 display 的 `GET /api/v1/layers/discharge/valid-times`（named-identity 分支）
在 node-27 上从 0.56 ms 退化到 887 ms。索引全在场且 `indisvalid=t`，查询形状没变——
病灶是**前沿 chunk 的 planner 统计**。

#### 成因：新值不可见

每个新 cycle 写入的 `run_id`/`run_key` 是该 chunk 统计里**不存在的值**。planner 对
未见过的值估行 ≈ 0，于是在 `DISTINCT … ORDER BY valid_time DESC LIMIT` 下判定
"顺 `valid_time` 索引逐行过滤很快就能凑够 LIMIT"，翻转掉 identity 键索引；实际前沿
之外全是别的 run 的行，执行器一路扫（实测 `Rows Removed by Filter: 2,715,324`）。

关键是**翻转由新 cycle 的第一批行触发，与修改行数体量无关**。整条 ingest 链
（`scripts/node27_autopipeline.py`、`workers/output_parser`、
`scripts/node27_timeseries_compression.py`）原本一个 ANALYZE 都没有，统计新鲜度
完全押在 autovacuum 的 10% scale factor 上——250M 行的 chunk 允许 ~25M 行修改才触发
autoanalyze，而 2026-08-20 复采时 chunk 58/62 各挂 ~6.8M `n_mod_since_analyze`
（2.7%/5.5%），远不到线。所以**不能用体量阈值治**：任何高阈值都会在每次刷新后留下
一段"新 cycle 已进、统计未见"的必然复发窗口。

#### 看护：一处显式 ANALYZE

| 位置 | 触发 | 目标 | 上限 / 超时 |
|---|---|---|---|
| autopipe tick phase 3.5（publish 之后、summary 之前） | 本 tick **实际 ingest ≥ 1 个 run**（`already_ingested` 不算——那种 tick 没写行） | 两张 hypertable 的**未压缩** chunk 中 `n_mod_since_analyze >= 10_000` 者 | 每 tick 至多 3 个（按 `n_mod` 降序），逐条 `statement_timeout = 120 s`；被裁掉的进 `stats_guard.deferred`，下个 tick 补 |

`10_000` 的下限不是"攒够才刷"：一个真实 run 写入的行数 = 段数 × 时步 ≫ 10⁴，被本 tick
触及的 chunk 必然过槛；下限只用来跳过仅有零星迟到写入、本 tick 根本没碰的 chunk。
压缩 chunk **不在**目标集合内——初稿曾在压缩 runner 里搭车 ANALYZE，终审否决撤除，
理由见下面的陷阱一节。

看护**不改 tick 的成败判定**：失败分两级——单 chunk 的 ANALYZE
失败逐 chunk 隔离（该条目在 `stats_guard.analyzed` 里记 `status: "failed"` +
`error`，剩余 chunk 照常尝试），guard 级失败（连接/候选查询）才写
`stats_guard.status = "failed"` + `error`；两级都不改 tick 返回码——统计漂移是渐进病，
下个 tick 重试即可，没资格把 unit 染红。停用开关：
`NODE27_AUTOPIPE_STATS_GUARD=off`（summary 记 `skipped`）。

#### 复核（`pg_stat_user_tables`）

```sql
-- 前沿漂移量：哪些未压缩 chunk 攒着未被统计看见的修改
SELECT c.chunk_name, s.n_mod_since_analyze, s.last_analyze, s.last_autoanalyze
FROM timescaledb_information.chunks c
JOIN pg_stat_user_tables s
  ON s.schemaname = c.chunk_schema AND s.relname = c.chunk_name
WHERE (c.hypertable_schema, c.hypertable_name) IN (
    ('hydro', 'river_timeseries'),
    ('met', 'forcing_station_timeseries')
)
  AND c.is_compressed = false
ORDER BY s.n_mod_since_analyze DESC;
```

一个真跑过 guard 的 tick，其 summary JSON 的 `stats_guard.analyzed` 非空（逐**尝试**
一条，`status` 为 `ok`/`warning`/`failed`）；只有 `status: "ok"` 的条目承诺上面 SQL
里对应 chunk 的 `last_analyze` 刷到该 tick 时刻——`warning` 正是"发了 ANALYZE 但
`last_analyze` 未刷"（见下节陷阱），`failed` 是没执行成。

#### 陷阱：PG15 非 owner 的 ANALYZE 是静默跳过

PG 15 没有 `MAINTAIN` 权限位。非 owner（node-27 的 ingest 角色不一定是 chunk 的
owner）执行 `ANALYZE` **只发一条 WARNING 然后"成功"返回**——rc 是 0，统计没动。
所以 guard 在每条 ANALYZE 之后回读 `pg_stat_user_tables.last_analyze` 与执行前比对，
没刷新就把该条目记 `status: "warning"`（如实上报，不改 tick 返回码）。看到 warning
一律按"权限问题"查，别按"ANALYZE 没跑"查。回读走 autocommit 连接：ANALYZE 的累计
统计要到事务提交后才对后续事务可见，同事务内回读只会读到旧值。统计上报还可能有
亚秒级延迟，所以极快的 ANALYZE 偶发一次 warning 属正常——**连续**出现才是权限问题。

#### 陷阱：不要对裸压缩 chunk 名 ANALYZE

TimescaleDB 压缩时**特意保存** origin chunk 的 `pg_class` 统计
（`capture_pgclass_stats` → `restore_pgclass_stats`）：heap 被清空但 `reltuples`
保留——node-27 chunk 55 实证 heap 0 bytes 而 `reltuples = 266,888,192`。而
`ANALYZE <裸 chunk 名>` **绕过 hypertable 重定向**（`process_vacuum` 只在目标是
hypertable 时改道 compressed 兄弟表），落在那个空堆上把保留的统计清零。收益是零：
planner 对压缩 chunk 的成本估算读的是 **compressed 兄弟表**的统计。所以对压缩
chunk 只有一种正确写法——`ANALYZE <hypertable>`（走
`update_compressed_chunk_relstats`）。压缩 chunk 无 DML，autoanalyze 永不触发，
一旦清零就只能靠这条语句修回来。

### 4.8 权威表统计清零与标识列 trigram 等值陷阱 (`#1468`)

§4.7 治的是**前沿 chunk 的统计漂移**（有 churn、阈值太高）。本节治的是两件不同的
事：统计被**整体清零**后再也没有任何阈值能把它救回来；以及**统计新鲜也救不了**的
索引选择陷阱。两者在 #1341 回填战役里叠加，单次等值查找劣化 ~2,900x。

#### 成因一：崩溃恢复清零累计统计（清零型）

2026-08-21 只读诊断（node-27 生产 `nhms`，PG 15.2 / TSDB 2.10.2）：

| 证据 | 值 |
|---|---|
| `pg_stat_database.stats_reset` | NULL —— 从未显式 reset |
| autovacuum / scale_factor / threshold / track_counts | on / 0.1 / 50 / on（全默认） |
| `core.river_segment_crosswalk` | `reltuples` 154,630，`n_live_tup` 0，`last_analyze`/`last_autoanalyze` **双 NULL**，计数器全 0 |
| `met.canonical_grid_cell` | `reltuples` 296,100，同上 |
| `docker logs nhms-db` 第 12 行 | `2026-08-07 05:49:17 UTC … database system was not properly shut down; automatic recovery in progress`（容器 created 2026-08-07T05:49:16Z） |

`pg_class.reltuples` 保有旧值而 `pg_stat_*` 计数器归零，只有"累计统计被整体丢弃"
能解释：**PG15 在崩溃恢复时丢弃全部累计统计，且不写 `stats_reset`**。容器按
ADR 0002 是裸 `docker run` 创建的，重建时走了崩溃恢复。

关键后果：此后**零 churn 的表 `n_mod_since_analyze` 恒为 0**，默认阈值
（50 + 10%）也好、per-table 阈值也好，**永远不会再触发** autoanalyze。所以
per-table scale factor 只能覆盖 churn 型失效（新增 network、单行版本变更），
救不了清零型——两个机制缺一不可。

#### 修复腿：autopipe stats guard 的第二条腿

| 位置 | 触发 | 目标 | 上限 / 超时 |
|---|---|---|---|
| autopipe tick phase 3.5（与 §4.7 前沿腿同一处） | **每个 tick，不论本 tick 是否 ingest**——它的触发条件是统计缺席，不是前沿移动 | `core`/`met`/`hydro` 下 `relkind='r'` 且**非 hypertable** 的表中 `relpages > 0 AND last_analyze IS NULL AND last_autoanalyze IS NULL` 者 | 与前沿腿**共享**上限（每 tick 至多 3 张，按 `relpages` 降序）与 `statement_timeout = 120 s` |

hypertable 根表与 `_timescaledb_internal` 下的 chunk **明确排除**（根表 ANALYZE
会递归到 chunk；裸压缩 chunk 名 ANALYZE 会清零 TimescaleDB 保留的 origin 统计，
见 §4.7 陷阱一节）。回读自检、单表失败隔离、guard 级失败如实记录、**不改 tick
返回码**——全部与前沿腿同语义。停用开关 `NODE27_AUTOPIPE_STATS_GUARD=off`
**一并关闭两腿**（summary 两处都记 `skipped`）。

**怎么读**：tick summary JSON 打到 stdout，落在
`/home/nwm/autopipe-logs/autopipe.log`。修复腿在 `stats_guard.authority`：

```jsonc
"stats_guard": {
  "status": "not_triggered", "reason": "no_run_ingested",   // 前沿腿：本 tick 没 ingest
  "analyzed": [], "deferred": [],
  "authority": {                                            // 修复腿：照常跑
    "status": "completed",
    "analyzed": [
      {"table": "core.river_segment_crosswalk", "relpages": 2180,
       "seconds": 0.42, "last_analyze": "2026-08-21T…Z", "status": "ok"}
    ],
    "deferred": []
  }
}
```

条目语义与前沿腿一致：`analyzed` 逐**尝试**一条；只有 `status: "ok"` 承诺
`last_analyze` 真刷到了；`warning` 是"发了 ANALYZE 但回读没刷新"（PG15 非 owner
静默跳过，见 §4.7），`failed` 是没执行成。

**生产上的权威读法就是上面这段 summary JSON，不是 `[stats-guard]` 进度行。**
`scripts/node27_autopipe_cron.sh` **不传** `--progress`，所以定时 tick 根本不打那
一行；它只在人工 `--progress` 跑时出现，形态与前沿段同构（腿级 status + 四个计数）：

```
[stats-guard] not_triggered: ok 0, warning 0, failed 0, deferred 0, authority completed: ok 1, warning 1, failed 1, deferred 1
```

`authority` 后面的 status 是**腿级**的：guard 级失败（连不上 / 候选查询炸）时
`analyzed` 为空、四个计数全 0，与"这一腿没活干"长得一模一样，只有 status 分得开
（`authority failed: ok 0, warning 0, failed 0, deferred 0`）。该行现在只要**任一
腿**有动作就打印——旧条件在 `not_triggered` 时整行抑制，恰好吞掉修复腿唯一干活的
那种 tick。

#### 复核 SQL（双 NULL 候选，期望 0 行）

```sql
-- 还有哪些普通表的累计统计是空的（修复腿的候选集，与代码同口径）
SELECT s.schemaname, s.relname, c.relpages, s.last_analyze, s.last_autoanalyze
FROM pg_stat_user_tables s
JOIN pg_class c ON c.oid = s.relid
WHERE s.schemaname IN ('core', 'met', 'hydro')
  AND c.relkind = 'r'
  AND c.relpages > 0
  AND s.last_analyze IS NULL
  AND s.last_autoanalyze IS NULL
  AND NOT EXISTS (
      SELECT 1 FROM timescaledb_information.hypertables h
      WHERE h.hypertable_schema = s.schemaname AND h.hypertable_name = s.relname
  )
ORDER BY c.relpages DESC, 1, 2;
```

一个跑过修复腿的 tick 之后这条应返回 **0 行**。持续非空 = 修复腿没跑（开关 off /
guard 级失败）或每条都 `failed`/`warning`——先看 summary，别先怀疑 SQL。

这条 SQL 的**取/舍口径由真库用例钉住**，不是靠字符串比对：
`tests/test_real_database_integration.py::test_stats_guard_repair_leg_analyzes_plain_authority_tables_and_skips_hypertables`
在 throwaway 库里把 `core`/`met`/`public` 三张普通表与一个 hypertable chunk 做成
同样的候选形态（先 ANALYZE 让 `relpages > 0`，再
`pg_stat_reset_single_table_counters` 造双 NULL），直接调修复腿并断言：`core`/`met`
两张被 ANALYZE 且 `status: "ok"`，`public.*`、hypertable 根表、
`_timescaledb_internal.*` chunk **一个都不入选**。

hypertable 根表还多一道：ANALYZE 不给继承父表写 `pg_class.relpages`，根表本来就被
`relpages > 0` 挡在外面，`NOT EXISTS` 删掉也没人发现。用例因此**直接改写根表的
`relpages`**（一次 catalog 写，只在 per-test throwaway 库里做）把它凑成除
`NOT EXISTS` 外每条谓词都满足的候选，再证明该子句是**唯一**排除者：出厂 SQL 不返回
根表、仅删掉 `NOT EXISTS` 块的变异 SQL 返回根表、且修复腿的入选集仍恰好是
`core`/`met` 两张。改这段 SQL 先跑它。

#### per-table autovacuum 参数（覆盖 churn 型）

000052 给四张 core 身份表加了参数，复核：

```sql
SELECT relname, reloptions FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'core'
  AND relname IN ('river_segment', 'river_segment_crosswalk',
                  'river_network_version', 'basin_version');
```

| 表 | 参数 | 为什么 |
|---|---|---|
| `river_segment`、`river_segment_crosswalk` | `autovacuum_analyze_scale_factor=0.01`、`autovacuum_analyze_threshold=500` | 新增一个 ~5k 段的 network 远不到默认 209k×10%，1% + 500 能过槛 |
| `river_network_version`、`basin_version` | `autovacuum_analyze_scale_factor=0`、`autovacuum_analyze_threshold=1` | 20 行的表，单行变更也远不到默认的 flat 50 |

#### 成因二：标识列上的 trigram GIN 等值陷阱（统计新鲜也不管用）

pg_trgm 1.6 起 `gin_trgm_ops` 支持 `=`，planner 因此把 trigram GIN 也算作**等值
查找**的候选。`river_segment_id` 是共享 ~34 字符前缀的长 slug
（`basins_jialingjiang_shud_shud_riv_` 14,673 条、`basins_zhaochen_*` ~9k 条），
这些前缀三元组的 posting list 几乎覆盖全表，GIN 成本模型严重低估。08-19 手工
ANALYZE **之后**实测 2,000 次等值查找：

| | 计划 | wall | buffers | 估算成本 |
|---|---|---|---|---|
| 默认 | Bitmap Index Scan `river_segment_id_trgm_idx` | **51,029 ms** | 2,560,171 | 0.72 |
| `enable_bitmapscan=off` | Index Only Scan `river_segment_pkey` | **17 ms** | 9,718 | 2.28 |

修法是**结构性**的：000052 把该索引改建在表达式 `lower(river_segment_id)` 上，
等值谓词在结构上无法匹配表达式索引，与统计新鲜度和成本估计都无关；search 侧
（`packages/common/model_registry.py` 列表路径 id 臂）改写成
`lower(rs.river_segment_id) LIKE <小写 pattern> ESCAPE '\'`，命中集合不变、
计划仍走该索引。完整机制、备选方案与约定见
[ADR 0004](../adr/0004-identifier-trgm-gin-equality-trap.md)。

**施加 000052 的运维口径**：首遍用 `uv run python -m packages.common.migrate`（autocommit 逐语句执行且写 `public.schema_migrations` 账本；`psql -f` 不写账本，会让下一次 bring-up/`run_qhh_cycle.sh` 静默重放）；重跑验证可 `psql -v ON_ERROR_STOP=1 -f` 直接跑，重跑幂等，**全文
没有任何对 `core.river_segment` 取 ACCESS EXCLUSIVE 的语句**——旧索引与上一次
`CREATE INDEX CONCURRENTLY` 中断留下的 INVALID 残骸都只 **RENAME**（`_legacy` /
`_invalid`，SHARE UPDATE EXCLUSIVE，不阻塞 MVT 读），新索引并发建成后再
`DROP INDEX CONCURRENTLY` 删掉这两个名字。非并发 `DROP INDEX` 即使删的是没有读者
的 INVALID 索引，也会锁**表**、在读流量下排队并被文件里的 `lock_timeout = '2s'`
打断（round-1 审查 C1）。所以中断后的恢复动作只有一个：**重跑该文件**；不要手工
`DROP INDEX`（要手工清也必须带 `CONCURRENTLY`）。看恢复是否干净：
`\di core.river_segment_id_trgm_idx*` 应只剩一个名字，且
`pg_index.indisvalid = true`。

`met.met_station` 的 `met_station_id_trgm_idx` 是 partial（`WHERE active_flag =
true`）：不带该谓词的等值查找结构上不可选它（实测走 `met_station_pkey`，
1.8 ms/500）；**带谓词的等值 join 会中招且随统计翻转**——2026-08-21 05:50Z 实测走
`met_station_active_basin_station_idx` Hash Join（22 ms/500），同日 08:32Z（PR #1666
E4 receipt，autovacuum 刷新统计后、无 schema 变更）翻为 Bitmap Index Scan
`met_station_id_trgm_idx`（0.33 ms/次，174 ms/500，29.6k buffers，~8×）。本 change
不改 met 侧，按 ADR 0004 约定对齐（表达式索引 + `forecast_store` search 臂改写）
路由到 #1669；复核 SQL：`EXPLAIN` 上述两种形态，带谓词形态出现
`met_station_id_trgm_idx` 即为陷阱复现。

#### 两项既有缓解的作用域（都不要再依赖）

- **2026-08-16 / 08-19 的手工 `ANALYZE`**：一次性的，只覆盖当时那三张权威表；
  crosswalk 与 `met.canonical_grid_cell` 当时并未修复。修复腿上线后不需要再手工做。
- **`PGOPTIONS='-c enable_bitmapscan=off'`**：只存在于 node-27 本地
  `run-campaign-v3.sh` 的**会话级**设置（该脚本不在本仓库内），随 #1341 战役结束
  消亡。000052 之后**不再需要**——不要把它固化进任何回填脚本，那只救一个消费者
  并掩盖根因。

## 8. Gated DB retention (`timeseries-db-retention`)

The retention runner
(`scripts/node27_timeseries_retention.py`, issue #855) drops chunks
strictly older than the drop window from the two D3
detail hypertables `hydro.river_timeseries` and
`met.forcing_station_timeseries` via TimescaleDB `drop_chunks`. The window
width is `NODE27_TIMESERIES_RETENTION_WINDOW_DAYS` (spec default 14 d;
21 d on node-27 as of 2026-08-01) — always read the live value off the box
rather than assuming the default.

**The lane runs in exactly one mode: archive gate `disabled` (#1370).**
`NODE27_TIMESERIES_RETENTION_ARCHIVE_GATE=disabled` is REQUIRED; unset, empty,
`enabled`, or any other value refuses with `RETENTION_CONFIG_INVALID`, exit 2,
and no receipt. Enforce was originally hard-gated on two archive receipts, but
neither can ever be produced again — the archive lane is permanently retired
under
[`docs/adr/0002-node27-timeseries-hot-cold-tiering.md`](../adr/0002-node27-timeseries-hot-cold-tiering.md)
**Revision 2026-08-11** — so the gate that consumed them was retired with them
(issue #1369 introduced the explicit `disabled` mode; #1370 made it the only
one). Chunks are therefore dropped with **no archive backstop and no restore
lane**. Every receipt records the mode in its `archive_gate` object, so the
deletion stays deliberate and auditable rather than a silent bypass. See §8.4
for the enforce preconditions and §8.5 for how to read the receipt.
Compression state is
never a gate — compressed chunks older than the window are exactly the
retention target (H3 divergence from #851).

Related documents:

- Design record: `openspec/changes/tier-node27-timeseries-storage/design.md`
  fixture block "Workflow Fixture: Issue #855" (H1–H17 pins). That fixture is
  frozen and still describes the two-receipt gate; for what the runner accepts
  today, this section and #1370 are the authority.
- Architecture record:
  [`docs/adr/0002-node27-timeseries-hot-cold-tiering.md`](../adr/0002-node27-timeseries-hot-cold-tiering.md).
- Display carve-out: `docs/adr/0001-display-timeseries-carveout.md`. The
  runner is never imported by `apps/api/**` or `apps/frontend/**`.

### 8.1 Install (node-27, `nwm` user)

Live enablement of the retention unit was originally a §6.3 follow-up
(issue #856) and stayed deferred while the archive gates could still be
satisfied. **Operator decision 2026-08-14 (issue #1369): the timer is
enabled, running daily at 05:15 UTC** — the committed `OnCalendar` value is
unchanged — with the archive gate set to `disabled` under
[`docs/adr/0002-node27-timeseries-hot-cold-tiering.md`](../adr/0002-node27-timeseries-hot-cold-tiering.md)
Revision 2026-08-11. Step 3 below is therefore a real step now, not a
commented placeholder; run it only after the two manual receipts in §8.4
(dry-run, then a bounded enforce) have been reviewed.

1. Create the retention log directory (same shape as the compression
   sibling):

   ```
   mkdir -p ~/node27-timeseries-retention-logs
   ```

2. Put the env file in place and lock it down. The env file MUST be
   mode `0600` — the wrapper refuses otherwise
   (`ENV_FILE_MODE_UNSAFE`).

   **First-time install only** — run the `cp` ONLY when
   `/home/nwm/NWM/infra/env/node27-timeseries-retention.env` does not exist
   yet:

   ```
   test -e /home/nwm/NWM/infra/env/node27-timeseries-retention.env \
     || cp /home/nwm/NWM/infra/env/node27-timeseries-retention.example \
           /home/nwm/NWM/infra/env/node27-timeseries-retention.env
   chmod 0600 /home/nwm/NWM/infra/env/node27-timeseries-retention.env
   ```

   **If that env file already exists — and it DOES on node-27 — edit it in
   place; do not re-copy the example.** The example ships
   `NODE27_TIMESERIES_RETENTION_WINDOW_DAYS=14` uncommented (the spec
   default), while node-27 runs `21` (as of 2026-08-01 — see this runbook's
   opening policy banner).
   Re-copying therefore silently reverts the live window 21 → 14, and a
   narrower window means an extra 7 days of data become deletion candidates
   — irreversibly, with no refusal to catch it once the archive gate is
   `disabled`. Verify the value after ANY edit and treat a surprise `14` as
   a stop-the-bringup event:

   ```
   grep -n NODE27_TIMESERIES_RETENTION_WINDOW_DAYS \
     /home/nwm/NWM/infra/env/node27-timeseries-retention.env
   ```

   Fill in `DATABASE_URL` with a writer role (retention runs `drop_chunks`
   DDL). Do NOT share the audit env's `nhms_display_ro` role — retention
   requires DML privileges.

   **Comment out `NODE27_TIMESERIES_RETENTION_RECEIPT_PATH` in the deployed
   env file — that line is SET there today, so "leave it unset" would be a
   no-op instruction.** The `.example` shipped that assignment UNCOMMENTED
   until 2026-08-14 and the deployed copy was made from that older template;
   the operator must actively comment the line out. Once it is commented the
   wrapper substitutes a per-tick timestamped `retention-<UTC>.json` under the
   log root, whereas a fixed path makes every daily tick atomically overwrite
   the previous receipt and destroys the per-tick audit trail of irreversible
   deletions. Verify — this grep MUST print nothing (exit 1):

   ```
   grep -n '^NODE27_TIMESERIES_RETENTION_RECEIPT_PATH=' \
     /home/nwm/NWM/infra/env/node27-timeseries-retention.env
   ```

   Keep both anchors. The leading `^` and the trailing `=` are what make the
   check discriminating: an unanchored
   `grep NODE27_TIMESERIES_RETENTION_RECEIPT_PATH` still matches the commented
   line (and the comment prose around it), so it prints hits either way and
   proves nothing. Any output from the anchored form means the variable is
   still live — fix it before enabling the timer. Manual direct-`python` runs
   consequently MUST pass an explicit `--receipt-path` (§8.4 step 2).

   Set the archive gate in the same edit:
   `NODE27_TIMESERIES_RETENTION_ARCHIVE_GATE=disabled`. Since #1370 that is
   the only value the runner accepts — unset, empty, `enabled` or anything
   else refuses with `RETENTION_CONFIG_INVALID`, exit 2, and no receipt, on
   every tick. Setting it acknowledges that chunks are deleted with no
   archive backstop and no restore lane; read the danger block in the
   `.example` first.

3. Register the two units and enable the timer (per the 2026-08-14 operator
   decision; do this AFTER the §8.4 manual dry-run + bounded enforce
   receipts):

   ```
   systemctl --user daemon-reload
   systemctl --user enable --now nhms-node27-timeseries-retention.timer
   systemctl --user list-timers | grep nhms-node27-timeseries-retention
   ```

   The service and timer files are installed under
   `~/.config/systemd/user/` from the checked-in
   `infra/systemd/nhms-node27-timeseries-retention.{service,timer}`. The
   cadence stays `OnCalendar=*-*-* 05:15:00 UTC` — do not retune it here.

   **The timer lane obeys ONLY the env file's
   `NODE27_TIMESERIES_RETENTION_ENFORCE` value**: every tick starts a fresh
   process that sources that file, so CLI flags and shell-prefixed env vars
   typed in any manual session have zero effect on timer ticks. Once
   `NODE27_TIMESERIES_RETENTION_ENFORCE=1` is resident in the env file, every
   scheduled firing enforces — there is no per-session way to hold it back
   short of editing that file or disabling the timer.

4. Verify the per-tick receipt rotation once a SECOND tick has run — wait for
   the next 05:15 UTC firing, or force one with
   `systemctl --user start nhms-node27-timeseries-retention.service`
   (**that forced start is a real enforcing tick: with `ENFORCE=1` resident in
   the env file it irreversibly drops up to
   `NODE27_TIMESERIES_RETENTION_PER_TICK_BOUND` chunks, same as a scheduled
   firing — it is not a no-op probe**). Two distinctly timestamped
   WRAPPER-generated receipts MUST coexist in the log root.

   Match only the wrapper's own filenames: it writes
   `retention-<UTC>.json` with `<UTC>` = `%Y%m%dT%H%M%SZ`, so the glob below
   pins the leading `2` of the year. The manual §8.4 receipts
   (`retention-dryrun-<UTC>.json`, `retention-enforce-<UTC>.json`) never count
   toward the two and deliberately do not match — a bare `retention-*.json`
   glob would be satisfied by those two alone, before the timer has ticked
   even once, and the check could never fail.

   ```
   ls -l ~/node27-timeseries-retention-logs/retention-2*.json
   ```

   Read the listing in three states:

   - **Two or more distinctly timestamped matches → PASS.** Rotation works:
     each wrapper tick kept its own receipt.
   - **Exactly one match → NOT a pass.** One wrapper tick has produced a
     receipt; either the second tick has not happened yet, or it happened and
     did not add a file.
   - **Zero matches → NOT a pass.** No wrapper-named receipt exists at all —
     which is also the visible signature of the fixed-path regression below,
     since a pinned path is conventionally `retention-manual.json`-style and
     does not match `retention-2*`.

   Before concluding "rotation is broken" from either non-pass state, rule out
   the two diagnosable causes, in this order:

   1. **The receipt-path variable is still set** (step 2 skipped): every tick
      atomically overwrites the one fixed file, so the per-tick audit trail is
      gone. Check with the anchored grep, which must print nothing:

      ```
      grep -n '^NODE27_TIMESERIES_RETENTION_RECEIPT_PATH=' \
        /home/nwm/NWM/infra/env/node27-timeseries-retention.env
      ```

      Any hit: comment the line out per step 2, then wait for / force another
      tick and re-check.
   2. **Fewer than two receipt-producing wrapper ticks have run yet.**
      `LAST` from `systemctl --user list-timers` names only the MOST RECENT
      firing, so it can never tell you HOW MANY ticks ran — count them
      directly. The wrapper appends exactly one
      `node27-timeseries-retention: start summary=` line per tick to the
      cumulative `retention.log`
      (`scripts/node27_timeseries_retention_once.sh:143`; the bracket pairs
      are described in §8.6 item 5), and manual wrapper invocations are
      counted the same way as timer firings:

      ```
      grep -c 'node27-timeseries-retention: start summary=' \
        ~/node27-timeseries-retention-logs/retention.log
      ```

      Not every counted tick wrote a receipt, and one class of firing never
      enters the count at all: a firing that lost the lock logs
      `previous wrapper still active, skipping tick` with no `start` line.
      From the counted brackets, subtract any `start` with no matching
      `done rc=` (still in flight, or died mid-tick) and any `done rc=2`
      bracket (the `RETENTION_CONFIG_INVALID` refusal, which publishes no
      receipt). If `retention.log` has been rotated or truncated away, fall
      back to `journalctl --user -u nhms-node27-timeseries-retention.service`,
      which carries one invocation record per run of THAT UNIT — timer
      firings plus `systemctl --user start` forced starts; a wrapper run
      launched straight from a shell is not journalled under the unit.

      With that corrected TICK count below two, the receipt count is simply
      premature, not broken: wait for the next firing or force one (with the
      enforcing-tick caveat above) before reading anything into the receipt
      count. With the tick count at two or more, `LAST` is still useful
      as a SECONDARY signal — a `LAST` newer than the newest
      `retention-2*.json` (compare against that receipt's `generated_at`)
      means the latest tick wrote nothing, which points back at rule-out 1 or
      at an `rc=2` refusal rather than at prematurity.

   Only with the rule-out 1 grep clean AND at least two receipt-producing
   wrapper ticks counted in `retention.log` does a `retention-2*.json` count
   below two indicate a real rotation defect.

#### Current bringup state (verified 2026-08-01, superseded 2026-08-14)

Historical posture, kept because the receipts committed under
`docs/runbooks/receipts/.../timeseries-retention/` that predate 2026-08-14
were produced under it:

- `nhms-node27-timeseries-retention.timer` was `disabled` and `inactive` —
  step 3's `enable --now` line was commented out in reality, not just in
  this runbook.
- Live `/home/nwm/NWM/infra/env/node27-timeseries-retention.env` set
  `NODE27_TIMESERIES_RETENTION_ENFORCE=0` (grep the key; that file is
  gitignored and its line numbers drift).

That was the #1071 Step B posture: refusal tests and dry-runs only, with no
unattended `drop_chunks`, gated on the completeness + drill receipts covering
the window.

**Superseding decision (2026-08-14, issue #1369):** the archive lane is
retired, so the gate is switched to `disabled` and the timer is enabled at
its committed 05:15 UTC cadence. The bringup order is fixed: set the env
mode with `ENFORCE=0` → manual dry-run receipt → **blast-radius inventory
(the query below) over the full backlog**, reviewed against the LIVE
`NODE27_TIMESERIES_RETENTION_WINDOW_DAYS` → `ENFORCE=1`
manual tick (≤ per-tick bound) → `enable --now` the timer → capture
`list-timers` → after the second tick confirm two distinctly timestamped
receipts coexist (step 4). That bringup ran on 2026-08-14: the timer is
enabled at 05:15 UTC daily, the whole 6-chunk backlog was ground down (5
candidates in the first enforce plus the 1 deferred remainder in the next
tick, leaving `deferred_remainder: []`), and its four live receipts are
committed under
`docs/runbooks/receipts/tier-node27-timeseries-storage/timeseries-retention/`
(`retention-dryrun-20260814T095619Z.json`,
`retention-enforce-20260814T095746Z.json`,
`retention-20260814T095802Z.json`, `retention-20260814T095832Z.json`). The
box remains the authority on the timer's present state — re-verify with
`systemctl --user list-timers` before quoting it. Rolling back can only mean
stopping FURTHER deletion — set `NODE27_TIMESERIES_RETENTION_ENFORCE=0` and/or
disable the timer (never delete the archive-gate line; since #1370 that just
makes every tick refuse — see `## Rollback`). Chunks already dropped are gone
for good.

#### Blast-radius inventory (bringup step 2, before `ENFORCE=1`)

The dry-run receipt names chunks and nothing else: `candidate_chunks[]` and
`deferred_remainder[]` are bare `<chunk_schema>.<chunk_name>` strings, so
neither the owning hypertable, the time range, nor the bytes at stake are
readable from the receipt. Resolve them with the read-only catalog query
below before any `ENFORCE=1` tick. It re-uses the runner's own selection
predicate — the same two target hypertables and the same non-strict
`range_end <= cutoff` (H7) — anchored on the `cutoff` the dry-run receipt
recorded, so its rows are exactly
`candidate_chunks[] ∪ deferred_remainder[]`. Nothing narrows the runner's
side any more: the completeness-bounds intersection that used to shrink it
went away with the archive gate (#1370).

```
set -a; source /home/nwm/NWM/infra/env/node27-timeseries-retention.env; set +a
# DRYRUN_RECEIPT = the timestamped path passed to --receipt-path in §8.4
# step 2. Do NOT reach for $NODE27_TIMESERIES_RETENTION_RECEIPT_PATH: that line
# is commented out in the deployed env file (§8.1 step 2), so the var expands
# to nothing — and if it does expand, step 2 was skipped and the path names a
# single repeatedly-overwritten file, not this dry-run's receipt.
DRYRUN_RECEIPT=/home/nwm/node27-timeseries-retention-logs/retention-dryrun-<UTC>.json
CUTOFF="$(jq -r '.cutoff // empty' "$DRYRUN_RECEIPT")"
: "${CUTOFF:?dry-run receipt carries no cutoff — re-run the dry-run first}"

docker exec nhms-db psql -U nhms -d nhms -P pager=off -c "
  WITH sized AS (
      SELECT chunk_schema, chunk_name, total_bytes
        FROM chunks_detailed_size('hydro.river_timeseries')
      UNION ALL
      SELECT chunk_schema, chunk_name, total_bytes
        FROM chunks_detailed_size('met.forcing_station_timeseries')
  ), backlog AS (
      SELECT format('%s.%s', c.chunk_schema, c.chunk_name) AS chunk,
             format('%s.%s', c.hypertable_schema, c.hypertable_name) AS hypertable,
             c.range_start, c.range_end, c.is_compressed,
             COALESCE(s.total_bytes, 0)::bigint AS total_bytes
        FROM timescaledb_information.chunks c
        LEFT JOIN sized s
          ON s.chunk_schema = c.chunk_schema
         AND s.chunk_name = c.chunk_name
       WHERE (c.hypertable_schema, c.hypertable_name) IN
             (('hydro','river_timeseries'), ('met','forcing_station_timeseries'))
         AND c.range_end <= '${CUTOFF}'::timestamptz
  )
  SELECT chunk, hypertable, range_start, range_end, is_compressed,
         total_bytes, pg_size_pretty(total_bytes) AS size
    FROM backlog
  UNION ALL
  SELECT format('TOTAL: %s chunks', count(*)), NULL::text,
         NULL::timestamptz, NULL::timestamptz, NULL::boolean,
         sum(total_bytes)::bigint, pg_size_pretty(sum(total_bytes))
    FROM backlog
   ORDER BY range_end NULLS LAST, chunk;"
```

Notes on reading it: `chunks_detailed_size(<hypertable>)` is the same public
TimescaleDB function the runner uses for `freed_bytes` (H4), so its
`total_bytes` includes a compressed chunk's compressed sibling and indexes —
the `is_compressed` column is there so a large compressed chunk is not
mistaken for a small one. The `LEFT JOIN` plus `COALESCE(...,0)` mirrors the
runner's own coercion: a chunk the size function does not report shows `0`
bytes rather than dropping out of the listing. The trailing `TOTAL:` row is
the whole backlog, not this tick's cut.

**Record in the PR evidence BEFORE `enable --now`:** the full backlog count
(`candidate_chunks[]` + `deferred_remainder[]`, which equals the `TOTAL:`
row's chunk count) and the query's estimated total bytes. Enabling the timer
does not authorize only the first cut — it authorizes grinding the ENTIRE
backlog away at up to `NODE27_TIMESERIES_RETENTION_PER_TICK_BOUND` chunks per
daily tick (5 in the shipped env), unattended, with no archive backstop and
no restore lane. The per-tick bound paces the deletion; it does not bound it.

Cross-check the cutoff itself before trusting the listing: the query inherits
the receipt's `cutoff` (= watermark − window), so a mis-set window silently
widens every row here. Confirm the receipt's `window_days` equals the LIVE
`NODE27_TIMESERIES_RETENTION_WINDOW_DAYS` in the env file — read it, never
type a remembered number (§8.1 step 2 shows the grep; node-27 ran 21 d as of
2026-08-01) — and that the receipt's `reference_time` is the display
watermark, not a wall clock. Note also that boundary-partial chunks are
listed like any other candidate: nothing defers them (§8.5).

### 8.2 Wire-format codes

The runner emits structured refusal reasons on `outcome=refused`. Codes are
byte-identical across four surfaces: code
(`scripts/node27_timeseries_retention.py` `WIRE_CODES` frozenset), this
runbook §8.2, the unit tests, and the #855 design fixture
(`openspec/changes/tier-node27-timeseries-storage/design.md`) — the
forward-walk test reads that fixture too. Any addition / rename MUST land in
all four surfaces in the same commit. Retirement is the one asymmetry:
retired codes are NOT removed from the fixture. They stay there verbatim as
frozen history and are absorbed into the reverse walk's allowlist instead.

Historical note (#1370): the thirteen archive-family codes — five
completeness-receipt codes and eight drill codes — were retired together with
the `enabled` archive-gate mode and are no longer members of `WIRE_CODES`. No
current tick can emit one. They survive verbatim in the frozen #855 design
fixture and in receipts published before 2026-08-14; read such a receipt as
history. The four codes below are the runner's own and are the complete
current set.

- `RETENTION_CONFIG_INVALID` — absolute-path / positive-int / env-parse
  failure before any DB call, including an archive-gate value that is
  anything other than `disabled` (§8.4 step 3). Emitted to stderr as a single
  JSON line
  `{status: "failed", code: "RETENTION_CONFIG_INVALID", reason: <detail>}`;
  the runner exits with code 2 and NEVER publishes a file receipt (the
  receipt path itself may be part of what failed to parse). Because no
  receipt exists, there is no `archive_gate` record of such a tick — a unit
  that refuses this way every tick leaves evidence only in the journal and in
  `retention.log`'s `done rc=2` brackets (§8.6 item 5).
- `RETENTION_CONCURRENT_INVOCATION` — non-blocking `fcntl.flock` on
  `/tmp/nhms-node27-timeseries-retention.lock` is already held. Receipt
  published, exit code 1.
- `RETENTION_DROP_FAILED` — per-chunk `drop_chunks` raised. Suffix
  `:<hypertable_schema>.<chunk_name>: [lock-contention(<pgcode>): ]<error>`.
  The prefix through the chunk name is byte-unchanged, and since #1664 a
  **lock-contention classification segment** may sit between that prefix and
  the error text (see §8.2.2 — it is present only for SQLSTATE `55P03` /
  `40P01`, absent for every other failure). `<error>` is credential-redacted
  driver text: DSN / password material and libpq `user "<name>"` /
  `role "<name>"` echoes are replaced by `***`, while the host/port echo is
  deliberately PRESERVED (diagnosability trade-off, #1213). Redaction is
  total and never raises — the redaction helper imports `psycopg2` at
  module scope, so in a driver-less window (venv rebuild, broken
  `psycopg2` wheel) that import itself fails and the tail degrades to the
  literal `<error text withheld: redaction unavailable (<Type>)>`, where
  `<Type>` is the ORIGINAL exception's class name (#1216). Whole tick
  refuses (H5 fail-closed); subsequent chunks NOT attempted.
- `RETENTION_UNCAUGHT_ERROR` — catch-all top-level exception. Receipt
  carries `refusal_reason = "RETENTION_UNCAUGHT_ERROR:<ClassName>: <str(exc)>"`.
  Wire code and `<ClassName>` are byte-unchanged; `<str(exc)>` crosses the
  same redaction chokepoint as above (this is the path a psycopg2
  DSN-parse failure takes, and that exception echoes the whole conninfo,
  password included), so the tail is either credential-redacted driver
  text or, in the same driver-less window, the literal
  `<error text withheld: redaction unavailable (<Type>)>`.

There is no refusal-code priority chain any more. The four codes are raised by
disjoint phases — config parse, lock acquisition, per-chunk drop, top-level
catch-all — so at most one is reachable per tick, and the thirteen-code
ordering the archive gates needed went away with them (#1370). The three
receipt-publishing codes each record the `archive_gate` mode in force at the
moment of refusal (§8.5); `RETENTION_CONFIG_INVALID` cannot, because it
precedes the receipt.

#### 8.2.1 Non-code stderr diagnostics

Not every stderr line is a wire code. The runner also emits **warning**
lines that never reach the receipt and are not members of the `WIRE_CODES`
frozenset:

- `{"chunk": "<chunk_schema>.<chunk_name>", "error": "<redacted text>",
  "warning": "freed_bytes measurement failed; recording 0"}` — the
  pre-drop size measurement for that ONE chunk RAISED (lock wait hitting the
  60 s statement timeout, catalog error, connection failure, an uncoercible
  `total_bytes` value). The runner records `freed_bytes: 0` for that chunk,
  keeps measuring the remaining chunks on fresh connections, and **still
  drops** — the tick is not refused and the exit code is unaffected. Grep
  literal: `freed_bytes measurement failed`. The `error` text is
  credential-redacted (DSN password and libpq role name are scrubbed)
  because the wrapper captures stderr into `retention.log`. Post-#1216 it
  crosses the SAME total chokepoint as the two refusal reasons in §8.2, so
  in a driver-less window the `error` value is not driver text at all but
  the literal `<error text withheld: redaction unavailable (<Type>)>` —
  the warning line, and the best-effort `0` it explains, are emitted
  either way.
- NOT every best-effort `0` produces a line here. A measurement that
  returns NO ROW, or a NULL `total_bytes`, is coerced to `0` **silently** —
  no warning is emitted at all (design D2 coercion path). §8.6 item 5
  depends on this asymmetry: an absent grep hit does NOT prove the `0` was
  measured.

#### 8.2.2 Lock-contention classification + the drop-phase lock budget (#1664)

Two changes landed together for issue #1664; read them as one mechanism.

**(a) `NODE27_TIMESERIES_RETENTION_LOCK_TIMEOUT_MS`** — the drop session now
executes `SET lock_timeout = <value>` alongside its existing
`SET statement_timeout = 300000`, both before `drop_chunks`. Default
**240000** ms; documented (commented, i.e. the default applies) in
`infra/env/node27-timeseries-retention.example`. The value must be an integer
**strictly greater than 0 and strictly less than 300000**
(`_DROP_TIMEOUT_MS`); `0`, a negative number, `300000` itself, anything above
it, or a non-integer aborts the tick with `RETENTION_CONFIG_INVALID`, exit 2,
no receipt, **no DB connection**. An empty assignment means "unset" and takes
the default. Enumeration and per-chunk measurement are untouched — they keep
the 60 s `_QUERY_TIMEOUT_MS` and get no lock budget at all.

Why 240 s and not the 2–5 s that "just fail fast" intuition suggests. A
successful 08-19 tick spent **~182 s** of wall clock that its 0.638 GB of
delete work does not explain (08-17 / 08-20 freed ~11 GB in 23 s / 1 s). Read
that number carefully: it is the wrapper's `elapsed_sec`, which brackets the
whole Python invocation — watermark, enumeration, and the per-chunk
measurement all sit inside it and none of them is lock-bounded — so it
**cannot** be attributed to the drop session, let alone to a single lock
acquisition. The choice therefore rests on the other two grounds: the costs are
asymmetric (this is the only lane with delete authority; nothing waits on its
wall clock — `TimeoutStartSec=0`, no `timeout(1)` in the wrapper, next tick
24 h later), and the 60 s gap between 240 s and 300 s keeps the two walls
distinguishable. The per-chunk drop timing (§8.6 item 6) exists to replace that
inference with a measurement — tune from it, not from `elapsed_sec`.

**(b) The classification segment.** A drop failure whose driver SQLSTATE is
`55P03` (lock not available) or `40P01` (deadlock detected) renders as:

```
RETENTION_DROP_FAILED:hydro._hyper_3_32_chunk: lock-contention(55P03): canceling statement due to lock timeout
RETENTION_DROP_FAILED:met._hyper_1_70_chunk: lock-contention(40P01): deadlock detected...
```

Any other failure — including the generic statement timeout `57014` — keeps
the pre-#1664 shape with **no** segment at all. Classification is
default-deny on the SQLSTATE alone (no message-text matching), so an
exception that carries no code is never classified. Operator greps:
`lock-contention(` for "blocked by a concurrent writer",
`RETENTION_DROP_FAILED:` (unchanged) for every drop failure.

**What this bound buys, and what it does NOT — read before tuning it.**
It buys a *bounded* wait and *attribution certainty*: contention now signs its
own name instead of hiding inside a `57014` that is indistinguishable from
"the delete itself was slow". It does **not** buy fewer failures. Three hard
edges:

1. **It does not eliminate `40P01`.** The server's deadlock detector aborts
   one side of a cycle as soon as it sees one, regardless of any
   `lock_timeout`. The 2026-08-18 failure shape (retention's `drop_chunks` on
   `met.forcing_station_timeseries` vs an autopipe
   `INSERT INTO met.forcing_version`) can still occur exactly as before.
2. **It is per-acquisition, not cumulative.** One `drop_chunks` call takes
   several locks in sequence; each may wait < 240 s and the statement can
   still exhaust its 300 s budget and come back as `57014`. So this raises
   attribution certainty substantially — it does not guarantee it.
3. **It reclaims nothing extra.** No chunk that could not be dropped before
   becomes droppable. Only the *shape* of the failure changes (earlier,
   signed), not its *frequency*.

Consequence, stated plainly: **`exit 1` will not go to zero.** The steady
state this lane is designed for is "bounded failure, visible failure, next-day
idempotent self-heal" — re-entering a drop has no side effect, so the next
13:15 CST tick simply retries the same chunk. Any statement that adding
`lock_timeout` stops the failures is wrong.

**Unverified (registered honestly).** The 2026-08-21 failure waited on a
TimescaleDB catalog *tuple* lock (server log: `while locking tuple (0,18) in
relation "dimension_slice"`). Whether PostgreSQL's `lock_timeout` covers that
class of wait is **not** established by offline evidence in this change; only
the next real contention event can answer it. If a future refusal shows
`57014` with a `dimension_slice` CONTEXT again despite the bound, that is the
answer — record it here rather than lowering the budget reflexively.

### 8.3 Metadata-table exemption + row-count invariant

The runner targets EXACTLY two hypertables (spec §Window and mechanism):

- `hydro.river_timeseries`
- `met.forcing_station_timeseries`

Metadata / coverage tables are NEVER retention targets:

- `hydro.hydro_run`
- `hydro.run_display_coverage`
- `met.forcing_version`
- `hydro.state_snapshot` (or wherever the state snapshot table currently
  lives)
- QC / lineage tables

Two guardrails enforce this:

1. **Structural**: `drop_chunks` only accepts hypertables; metadata
   tables are regular Postgres tables and cannot be dropped by
   `drop_chunks`. The runner's SQL literal restricts the tuple filter to
   the two D3 hypertables.
2. **Row-count invariant** (§6.1 test row 4): after every enforce tick,
   the row counts of the metadata / coverage tables MUST be unchanged.
   §6.3 embeds a pre/post row-count check in the live receipt review.

### 8.4 How to run

```
# 1. Prime env (once per node-27, then owned by operators).
# FIRST-TIME INSTALL ONLY — the `cp` is guarded, exactly as in §8.1 step 2.
# On node-27 the env file ALREADY EXISTS: edit it in place, never re-copy the
# example over it. The example ships
# NODE27_TIMESERIES_RETENTION_WINDOW_DAYS=14 while the box runs 21, so an
# overwrite silently reverts the live window 21 -> 14 and turns 7 more days of
# data into deletion candidates — irreversibly, and nothing is left that could
# refuse the drop. Re-grep the window after ANY edit (§8.1 step 2) and treat a
# surprise 14 as a stop-the-work event.
test -e /home/nwm/NWM/infra/env/node27-timeseries-retention.env \
  || cp /home/nwm/NWM/infra/env/node27-timeseries-retention.example \
        /home/nwm/NWM/infra/env/node27-timeseries-retention.env
chmod 0600 /home/nwm/NWM/infra/env/node27-timeseries-retention.env
# Fill DATABASE_URL (writer role) and (optionally) the lock path override.
# The SAME edit must carry NODE27_TIMESERIES_RETENTION_ARCHIVE_GATE=disabled —
# see step 3; without it every run refuses.
# COMMENT OUT NODE27_TIMESERIES_RETENTION_RECEIPT_PATH — on node-27 that line
# is currently SET (the example shipped it uncommented until 2026-08-14), so it
# has to be actively commented, not merely "left" alone. Verify with
#   grep -n '^NODE27_TIMESERIES_RETENTION_RECEIPT_PATH=' <env file>
# expecting ZERO matches (keep the ^ and = anchors — an unanchored grep matches
# the commented line too and proves nothing). With it commented the wrapper
# writes a per-tick timestamped receipt and the audit trail of irreversible
# deletions survives; a fixed path is overwritten every tick.

# 2. First run MUST be dry-run — inspect candidate_chunks + deferred_remainder.
# The runner REQUIRES a receipt path; with the env var commented out (step
# 1) a manual run must pass --receipt-path explicitly, or it aborts with
# RETENTION_CONFIG_INVALID, exit 2, and no receipt. Use a timestamped filename
# so manual runs never clobber each other or a timer tick's receipt.
# The NODE27_TIMESERIES_RETENTION_ENFORCE=0 prefix is NOT decoration: the
# --dry-run flag does NOT override the env. Once ENFORCE=1 is resident in the
# deployed env file — the steady state after the timer is enabled (§8.1 step 3)
# — an unprefixed run ENFORCES and irreversibly drops up to
# NODE27_TIMESERIES_RETENTION_PER_TICK_BOUND chunks. The inline assignment
# comes AFTER the `source`, so it wins.
set -a; source /home/nwm/NWM/infra/env/node27-timeseries-retention.env; set +a
DRYRUN_RECEIPT="$HOME/node27-timeseries-retention-logs/retention-dryrun-$(date -u +%Y%m%dT%H%M%SZ).json"
NODE27_TIMESERIES_RETENTION_ENFORCE=0 \
  uv run python scripts/node27_timeseries_retention.py --dry-run \
    --receipt-path "$DRYRUN_RECEIPT"
jq . "$DRYRUN_RECEIPT"

# 3. When ready to enforce, flip the env flag (or pass --enforce).
# Enforce PRECONDITIONS — one branch only since #1370; the archive gate is the
# first of them.
#  - NODE27_TIMESERIES_RETENTION_ARCHIVE_GATE=disabled MUST be set in the env
#    file (or passed as --archive-gate disabled; the CLI wins). It is the ONLY
#    accepted value: unset, an empty assignment, `enabled`, or anything else
#    is RETENTION_CONFIG_INVALID, exit 2, no receipt. Setting it is an
#    acknowledgement, not a convenience — read the next two bullets before
#    typing it.
#  - Authorization: docs/adr/0002-node27-timeseries-hot-cold-tiering.md
#    Revision 2026-08-11 amends this ADR's core invariant — "no deletion
#    without archive receipt" no longer holds, because the archive lane is
#    permanently retired and the two receipts it produced can never be
#    produced again. Verbatim from that revision: "The change is deliberate
#    and auditable, not a silent bypass: the retention runner keeps its
#    fail-closed default and only skips the completeness/drill gates in an
#    explicit gate-disabled mode whose receipt records the mode and cites
#    this revision." (#1370 went one step further than the quoted text: the
#    `enabled` mode was removed outright, so the "fail-closed default" it
#    speaks of is itself history — the acknowledgement is now mandatory
#    rather than opt-in.)
#  - There is NO archive backstop: the drop is irreversible and no restore
#    lane exists. Review the dry-run's candidate_chunks against the LIVE
#    NODE27_TIMESERIES_RETENTION_WINDOW_DAYS before enforcing, and keep the
#    first enforce inside the per-tick bound. Resolve each candidate's owning
#    hypertable, time range and bytes with the §8.1 blast-radius inventory
#    query FIRST — candidate_chunks[] is bare chunk names and carries none of
#    those three.
#  - Consequences to expect in the receipt: archive_gate.mode="disabled"
#    with the pinned adr_reference, salvage_backed_windows=[], and
#    boundary-partial chunks appearing as candidates rather than deferred
#    (§8.5).
# Either export NODE27_TIMESERIES_RETENTION_ENFORCE=1 in the env file or
# pass --enforce on the CLI. Same receipt-path rule as step 2: pass an explicit
# timestamped --receipt-path, because that env var stays commented out.
ENFORCE_RECEIPT="$HOME/node27-timeseries-retention-logs/retention-enforce-$(date -u +%Y%m%dT%H%M%SZ).json"
uv run python scripts/node27_timeseries_retention.py --enforce \
  --receipt-path "$ENFORCE_RECEIPT"
```

Exit codes: `0` = dry-run / enforced (both are "success" outcomes; the
receipt carries the outcome). `1` = refused (per-chunk drop failure,
concurrent invocation, uncaught error — see §8.6). `2` = config refusal
(missing / non-absolute / non-positive env, or an archive-gate value other
than `disabled`; no receipt written).

### 8.5 Reading the receipt

Receipts match `schemas/timeseries_retention_receipt.schema.json`
(schema `oneOf` — exactly one of `dry-run` / `refused` / `enforced`).

**Read `archive_gate` first — it records the authorization the tick ran
under.** Since schema `1.1` (#1369) every receipt, on all three outcome
branches, carries it, and since #1370 a live tick can only ever write one
shape:

- `archive_gate.mode = "disabled"` plus `adr_reference =
  "docs/adr/0002-node27-timeseries-hot-cold-tiering.md Revision 2026-08-11"`
  — the archive gates are skipped under that authorization. The reference
  string is a schema `const`, so a receipt cannot cite a vaguer source.

Exactly one historical shape exists on disk and is never rewritten: receipts
declaring `schema_version = "1.0"` with no `archive_gate` field at all
(pre-#1369, produced under the hard gate). Node-27 went straight from that
`1.0` shape to `1.1` with `mode = "disabled"`, so no receipt with
`archive_gate.mode = "enabled"` was ever produced — the schema enum still
admits that value, but nothing on disk carries it and the runner cannot
write it, because `NODE27_TIMESERIES_RETENTION_ARCHIVE_GATE` must read
`disabled` and any other value refuses with `RETENTION_CONFIG_INVALID`
before a receipt exists.

- `outcome=dry-run`: `mode=dry-run`; `candidate_chunks[]` lists chunks
  that WOULD be dropped up to the per-tick bound; `deferred_remainder[]`
  lists the chunks beyond that bound — and nothing else. **A boundary-partial
  chunk — one whose physical range starts before the drop window — is
  NOT deferred**: with no completeness receipt there are no coverage bounds
  and no "partially covered" notion, so such chunks sit in `candidate_chunks[]`
  like any other and will be dropped. That is the deliberate, documented
  widening of the delete surface that comes with
  `NODE27_TIMESERIES_RETENTION_ARCHIVE_GATE=disabled`. Dry-run vs enforce is
  decided SOLELY by `--enforce` /
  `NODE27_TIMESERIES_RETENTION_ENFORCE`; the `--dry-run` CLI flag controls
  nothing — it is never read, so it cannot hold back an env that says
  enforce (§8.4 step 2). With no archive gates left to evaluate, a dry-run
  ends either as `dry-run` or as one of the runner's own refusals.
- `outcome=refused`: `mode=enforce`; `refusal_reason` is one of the four
  codes in §8.2. Nothing was dropped this tick. A `refused` receipt can be
  emitted by a `--dry-run` invocation too — the mode field always reads
  `enforce` because the schema pins that pairing. The receipt carries
  `archive_gate` as well, so the audit trail records the mode in force at the
  moment of refusal.
- `outcome=enforced`: `mode=enforce`; `dropped_chunks[]` records each
  dropped chunk with its pre-drop `freed_bytes` (H4 — measured BEFORE
  `drop_chunks`); `deferred_remainder[]` records the beyond-bound
  chunks; `salvage_backed_windows[]` is always `[]` — see
  [§8.7](#87-salvage-backed-windows) for what that empty list means.

### 8.6 Recovery (post-fault operator playbook)

1. **Stuck lock (`RETENTION_CONCURRENT_INVOCATION`).** Confirm no live
   retention run is active (`ps -ef | grep node27_timeseries_retention`),
   then remove the lock file:

   ```
   rm -f /tmp/nhms-node27-timeseries-retention.lock
   ```

   The lock path is byte-identical with the runner's
   `_default_lock_path()` and `infra/env/node27-timeseries-retention.example`.
2. **Per-chunk drop failure (`RETENTION_DROP_FAILED`).** The whole tick
   refuses fail-closed (H5) — no schema `partial` outcome exists.
   **Chunks that dropped successfully before the failure are NOT
   enumerated in the receipt** (schema `oneOf` forbids `dropped_chunks`
   on refused); those chunks are already gone. To reconstruct what
   actually happened, cross-reference the wrapper's `retention.log`
   (per-chunk drop timings printed to stderr — grep literal
   `per-chunk drop timing`, item 6 below) with the current
   `timescaledb_information.chunks` state before re-running enforce. If the
   refusal came from a manual direct-`python` run there is no `retention.log`
   entry to cross-reference (item 5's scope note) — use that terminal's stderr
   and the catalog state instead.
   Inspect the offending chunk (the refusal_reason suffix names it
   `<hypertable_schema>.<chunk_name>`). What follows the chunk name is
   EITHER the redacted cause directly, OR — since #1664, and only for
   SQLSTATE `55P03` / `40P01` — a `lock-contention(<pgcode>):` classification
   segment (with its trailing space) first, and the redacted cause after it
   (§8.2.2;
   that segment is a fixed ASCII literal generated locally and is not driver
   text). The cause text after that point is redacted (§8.2), so read it as
   an intent-preserving summary,
   not as verbatim driver output; and when it is the withheld literal
   `<error text withheld: redaction unavailable (<Type>)>` the cause is
   absent ENTIRELY — classify from `<Type>` plus the DB side (item 4
   applies first in that case). Common causes: statement timeout
   (5 min per chunk — `_DROP_TIMEOUT_MS = 300_000` in
   `scripts/node27_timeseries_retention.py`, set as `statement_timeout`
   around each `drop_chunks`; there is no PROCESS-level wall on this lane
   — `node27_timeseries_retention_once.sh` wraps nothing in `timeout` and
   the systemd unit sets `TimeoutStartSec=0`, so a tick hung outside a
   statement is never killed for you. The only walls are statement-level:
   this 300 s per `drop_chunks`, plus the 60 s `_QUERY_TIMEOUT_MS` on
   catalog enumeration and per-chunk size measurement — see §8.2.1), active
   writer holding an incompatible lock, or a
   TimescaleDB catalog inconsistency. Re-run enforce after the operator
   has confirmed the DB is healthy. There is no automated retry loop —
   drops on healthy chunks should NOT proceed mid-failure without
   operator inspection.
3. **Config refusal (`RETENTION_CONFIG_INVALID`).** No receipt was
   written. Fix the env file per §8.4 and retry.
4. **Uncaught error (`RETENTION_UNCAUGHT_ERROR`).** The receipt carries
   the exception class + redacted message (§8.2). File a bug against #855
   (or the downstream owner if the class is from a shared helper). If the
   message is instead the literal
   `<error text withheld: redaction unavailable (<Type>)>`, the receipt
   carries the exception class ONLY and no message at all; that
   fingerprint points at a node-27 LOCAL environment failure (the
   redaction helper's `psycopg2` import failed), not at a runner defect.
   Check driver health FIRST:

   ```
   /home/nwm/NWM/.venv/bin/python -c "import psycopg2"
   ```

   If that import fails, rebuild the venv (`uv sync --all-extras --dev`)
   and re-run enforce; only file a bug against #855 once a healthy driver
   still reproduces the withheld tail.
5. **`freed_bytes: 0` in an `enforced` receipt.** A `0` is ambiguous in the
   receipt alone: the chunk may have been genuinely empty, or its
   measurement may have failed. Disambiguate from the wrapper log:

   ```
   grep 'freed_bytes measurement failed' /path/to/retention.log
   ```

   **Scope first — `retention.log` brackets exist ONLY for wrapper-driven
   ticks.** That is the timer lane plus any manual
   `scripts/node27_timeseries_retention_once.sh` invocation. A direct
   `uv run python scripts/node27_timeseries_retention.py` run (§8.4 steps 2-3)
   writes NO `retention.log` entry at all — not a start bracket, not a done
   bracket, not the measurement warning. Its only diagnostics are the invoking
   terminal's stderr and the receipt at the explicit `--receipt-path`. So if
   the receipt under investigation came from a manual direct-`python` run, the
   grep above has nothing to find and its silence carries zero information:
   read the terminal output you still have — that is the whole diagnostic.
   Do NOT re-run through the wrapper to manufacture a bracket. A wrapper
   invocation is a live enforcing tick: with
   `NODE27_TIMESERIES_RETENTION_ENFORCE=1` resident in the env file it
   irreversibly drops up to `NODE27_TIMESERIES_RETENTION_PER_TICK_BOUND`
   chunks, and a shell-prefixed `NODE27_TIMESERIES_RETENTION_ENFORCE=0` cannot
   hold it back because the wrapper re-sources the env file with `set -a`
   after the prefix applies (`scripts/node27_timeseries_retention_once.sh:52-58`),
   so the file's value wins. It would also mint a NEW receipt rather than
   diagnose the old one. The whole bracket procedure below applies to wrapper
   receipts only.

   Scope the match to THIS tick before reading anything into it.
   `retention.log` is cumulative — the wrapper appends every tick to the same
   file (`>>`) — so a bare `grep` also returns warnings from earlier runs.
   Each tick is bracketed in the log by the wrapper's own
   `node27-timeseries-retention: start summary=<receipt path>` and
   `node27-timeseries-retention: done rc=<rc> ... summary=<receipt path>`
   lines (`scripts/node27_timeseries_retention_once.sh:143,151`), each
   prefixed with a UTC ISO-8601 timestamp from the wrapper's `ts()`
   (`scripts/node27_timeseries_retention_once.sh:23`). Do NOT rely on the
   receipt path in those lines as the tick key. Under the required
   configuration `NODE27_TIMESERIES_RETENTION_RECEIPT_PATH` is commented out
   in the deployed env file (§8.1 step 2; the current
   `infra/env/node27-timeseries-retention.example` also ships it commented),
   so the wrapper substitutes a per-tick timestamped path and the paths do
   differ — but if the line is still set (it was uncommented in env files
   copied from the pre-2026-08-14 example) or anyone has pinned a fixed path
   against that guidance, every tick prints the same string and the path ALONE
   discriminates nothing. Correlate on
   time instead: pick the bracket whose `start summary=` timestamp and
   matching `done rc=` timestamp CONTAIN the receipt's `generated_at`
   (schema-required, `format: date-time`), and read only the lines between
   those two. Two kinds of bracket must be skipped because that tick wrote
   no receipt at all: a `start` with no matching `done rc=` (a tick still in
   flight, or a wrapper that died mid-tick), and a `done rc=2` tick — the
   config refusal of item 3, where `RETENTION_CONFIG_INVALID` publishes no
   receipt at all, so the path that bracket announced names either nothing
   or (under a pinned path) some earlier tick's file. Neither is the bracket
   to read; do NOT fall back to "the last
   bracket in the file". Then require the hit's `chunk` field to name a
   chunk that appears in THIS receipt's `dropped_chunks[]`. A hit outside
   that bracket belongs to an earlier tick and says nothing about this
   receipt's `0`.

   That second criterion does not close the refuse-then-retry window: a
   prior tick that warned about chunk X and then refused with
   `RETENTION_DROP_FAILED` (item 2) leaves a stale warning naming a chunk
   that THIS tick may genuinely measure as 0 and drop, so X can sit in this
   receipt's `dropped_chunks[]` while the only warning about it belongs to
   the earlier tick. Within THAT window the misread direction is
   conservative — it costs one extra reconciliation pass, never the reverse:
   a failed measurement is never read as a real `0`.

   An in-bracket hit names the chunk and the redacted cause (§8.2.1; on
   the withheld path the cause is withheld entirely and only the
   exception class survives) — the
   receipt's `freed_bytes` for that chunk is a best-effort 0, not a
   measurement. The chunk WAS dropped; only the reclaim accounting is
   degraded. Common cause of a hit: concurrent `compress_chunk` /
   `decompress_chunk` / manual replay holding an incompatible lock on the
   same hypertable until the 60 s statement timeout fires.

   No in-bracket hit does NOT prove the 0 was measured — for a wrapper tick
   (the only kind that has a bracket at all; see the scope note above) it
   leaves two possibilities:
   (a) a real measurement of a small or empty chunk, or (b) the
   silent-coercion path, where `chunks_detailed_size` returned no row or a
   NULL `total_bytes` and the runner recorded 0 without emitting any
   warning (design D2, §8.2.1). Narrowing (b): a chunk that fully vanished
   mid-tick normally also fails the drop phase and refuses the whole tick
   (`RETENTION_DROP_FAILED`, H5 fail-closed), so a silent 0 sitting inside
   an `enforced` receipt points at the NULL / no-row coercion, not at a
   disappeared chunk.

   No action is required unless the receipt's reclaim total is being
   reconciled against a `pg_database_size` delta.
6. **Per-chunk drop timings (#1664).** Every drop attempt — success AND
   failure — emits one sorted-key JSON line on stderr, which the wrapper
   captures into `retention.log`:

   ```
   grep 'per-chunk drop timing' ~/node27-timeseries-retention-logs/retention.log
   ```

   ```json
   {"chunk": "_timescaledb_internal._hyper_3_32_chunk", "diagnostic": "per-chunk drop timing", "elapsed_ms": 4187.226, "outcome": "dropped"}
   ```

   `chunk` is the chunk-schema-qualified name (the same key used in the
   receipt's `dropped_chunks[].name`, NOT the hypertable-qualified name the
   refusal reason uses). `outcome` is `dropped` or `failed`. The line carries
   **no error text** by design — the cause travels only through the redacted
   `refusal_reason` — so it has no credential surface and does not cross the
   redaction chokepoint. The same bracket-scoping rules as item 5 apply
   (`retention.log` is cumulative; direct-`python` runs write nothing to it).

   This is the measurement `NODE27_TIMESERIES_RETENTION_LOCK_TIMEOUT_MS`
   should be tuned from: an `elapsed_ms` close to the lock budget on a
   `lock-contention(55P03)` refusal means the wait was cut off by the bound;
   an `elapsed_ms` close to 300000 on a `57014` means the statement wall
   fired. Before #1664 no such line existed at all, although this section
   already told you to read them.
7. **Lock-blocked refusal (`lock-contention(55P03)` / `(40P01)`) — when to do
   nothing, and when to escalate (#1664).**

   ```
   grep 'lock-contention(' ~/node27-timeseries-retention-logs/retention.log
   ```

   **A single lock-blocked refused tick is ACCEPTABLE and needs no human
   intervention.** Nothing was half-done: the drop either happened or did not,
   the tick refused fail-closed (H5) before attempting later chunks, and the
   next 13:15 CST tick re-selects the same chunk and retries. Re-entry is
   idempotent; do NOT force a manual enforce run to "catch up" (§8.4's
   warnings apply — a wrapper invocation is a live enforcing tick).

   **Escalate when the pattern, not the event, is wrong:** three or more
   CONSECUTIVE days of lock-blocked refusals, or four or more lock-blocked
   ticks within one week. That means the retention lane is no longer making
   progress and the drop backlog is growing — check the candidate backlog and
   `pg_database_size` before deciding.

   What the forensics (#1664 Phase 0, node-27 receipts + PG server log)
   established about the counterparty, so it is not re-derived every time:

   - The opponent is the **autopipe ingest** timer
     (`~/.config/systemd/user/nhms-node27-autopipe.timer`,
     `OnUnitActiveSec=10min`, i.e. resident). There is **no schedule to move
     retention to** in order to avoid it.
   - The conflict surface is the **foreign keys**
     (`db/migrations/000005_met.sql:100,102` — `met.forcing_station_timeseries`
     references `met.forcing_version` and `met.met_station`) plus the
     **TimescaleDB catalog** (`dimension_slice`). `drop_chunks` dropping a
     chunk is a `DROP TABLE` and must take an `AccessExclusiveLock` on the
     FK-referenced plain tables.
   - The "compression window overruns into retention" hypothesis is
     **refuted**, by a natural control: 2026-08-19 had compression running
     04:25:00–05:28:53, fully overlapping the retention tick, and retention
     **succeeded**; the two failing days (08-18, 08-21) had no compression
     running at all. Do not re-open that hypothesis without new evidence, and
     do not add `Conflicts=` between the two lanes.
8. **Failure alerting (`OnFailure=`, #1664).** `nhms-node27-timeseries-retention.service`
   declares `OnFailure=nhms-node27-unit-failure-alert@%n.service`, so any
   non-zero exit activates the template unit
   `infra/systemd/nhms-node27-unit-failure-alert@.service`, which runs
   `scripts/node27_unit_failure_alert_once.sh` with the failed unit name.
   `%n` expands to the FULL unit name, so the live instance is
   `nhms-node27-unit-failure-alert@nhms-node27-timeseries-retention.service.service`
   — verify the INSTANCE, never the bare template.

   The handler is a dumb shell script: 30 journal lines of context, one
   message through the **frontier alert lane's existing channel**
   (`$NHMS_FRONTIER_SENDMAIL -t -i`, `NHMS_ALERT_EMAIL_TO` /
   `NHMS_ALERT_EMAIL_FROM` from `infra/env/node27-frontier-alert.env`). No
   second mail channel, no state file, no deduplication — a scheduled unit
   fails at most once per tick. Soft failures — `NHMS_ALERT_EMAIL_TO` or
   `NHMS_ALERT_EMAIL_FROM` unset, either of them carrying a CR/LF (rejected,
   never stripped — header injection), `NHMS_FRONTIER_SENDMAIL` unset or not
   executable — log a `reason=` token and **exit 0** so the alerter cannot pile
   a second failed unit on top of the one it is reporting; a transport that is
   present and rejects the message exits non-zero on purpose, because a
   silently broken alert lane is worse than a visible one. Neither
   `NHMS_ALERT_EMAIL_FROM` nor `NHMS_FRONTIER_SENDMAIL` has a default: the
   authenticated-SMTP shim refuses a From that is not the authenticated
   account, and node-27's `/usr/sbin/sendmail` accepts with exit 0 and then
   asynchronously bounces, so either default would manufacture a second failed
   unit or a "SENT" that never arrived.

   **KNOWN LIMITATION — the mail body does NOT carry `refusal_reason`.** The
   journal context the handler quotes is systemd **lifecycle lines only**
   (`Starting…`, `Main process exited, code=exited, status=1/FAILURE`,
   `Failed with result 'exit-code'`). The runner's own stderr never reaches
   the journal, for two independent reasons, both pre-existing:
   `scripts/node27_timeseries_retention_once.sh` redirects the runner's stdout
   **and** stderr into `retention.log`, and the retention unit writes
   `StandardOutput=`/`StandardError=append:` files that systemd does not
   duplicate into the journal. So the alert tells you **that** the tick failed,
   never **why**. Read the reason out of `retention.log`:

   ```
   grep 'RETENTION_' ~/node27-timeseries-retention-logs/retention.log | tail -20
   ```

   Then follow items 1-7 above from whichever wire code that prints.

   **Deployment is MANUAL** — node-27's units are user-scope
   (`~/.config/systemd/user/`), so `git pull --ff-only` updates only the
   Python behind `ExecStart`; it installs neither the new template nor the
   `OnFailure=` line into the live unit:

   ```
   install -m 0644 ~/NWM/infra/systemd/nhms-node27-unit-failure-alert@.service \
     ~/.config/systemd/user/
   install -m 0644 ~/NWM/infra/systemd/nhms-node27-timeseries-retention.service \
     ~/.config/systemd/user/
   systemctl --user daemon-reload
   systemctl --user show nhms-node27-timeseries-retention.service -p OnFailure
   systemctl --user start nhms-node27-unit-failure-alert@smoke.service   # channel smoke, touches no DB
   ```

   The `show` output must be non-empty and name the instance. Smoke-testing
   the ALERT unit is safe; **never** `systemctl --user start` the retention
   unit to test this wiring — that is a live enforcing tick and irreversibly
   drops production chunks.

### 8.7 Salvage-backed windows

`salvage_backed_windows[]` in an `enforced` receipt is **always `[]`**, and
that empty list is a positive statement rather than missing information: it
says *no archive evidence backed this deletion at all*. There is no recovery
lane for anything the tick dropped.

The field predates #1370. It was written when the retention gate could still
consult an archive-completeness receipt and record the db-export windows that
had been vouched for; with the archive lane retired the runner has no such
input and emits the empty array unconditionally. It is kept in the receipt
schema (unchanged since `1.1`) so current ticks, the historical receipts under
`docs/runbooks/receipts/tier-node27-timeseries-storage/timeseries-retention/`,
and the schema itself all keep the same shape.

Do not read `[]` as "everything dropped was archive-covered". In a historical
`enabled`-mode receipt an empty list meant "no db-export subject overlapped
this drop window"; in every current receipt it means nothing was covered.

## Rollback (unit-level, not data-level)

Two lanes remain: terminal-chunk compression (§4) and DB retention (§8).
Rollback is unit-level — disable the timers. Every receipt already on disk
stays as historical evidence.

```
systemctl --user disable --now nhms-node27-timeseries-compression.timer
systemctl --user disable --now nhms-node27-timeseries-retention.timer
```

Notes:

- **Retention rollback means "stop deleting more" and nothing else.** With
  the archive gate `disabled` (§8.4 — the only mode the runner accepts)
  chunks are dropped with no archive backstop and no restore lane, so no
  rollback recovers what a tick already dropped. The two levers are
  `NODE27_TIMESERIES_RETENTION_ENFORCE=0` in the deployed env file and
  disabling the timer above; use both when the box must stop immediately,
  since the timer lane obeys only the env file (§8.1 step 3).
- **Deleting the `NODE27_TIMESERIES_RETENTION_ARCHIVE_GATE=disabled` line is
  NOT a rollback.** Since #1370 an absent or non-`disabled` value is
  `RETENTION_CONFIG_INVALID`: the unit exits 2 every tick and publishes no
  receipt. That is a permanently failing unit, not a safe state.
- Compression rollback is the decompress procedure in §4.3 — reversible and
  data-preserving, unlike retention.
- The retired archive lane has no rollback surface left: its runners,
  wrappers, units and env templates were deleted in #1370. Its receipts under
  `docs/runbooks/receipts/tier-node27-timeseries-storage/**` are the
  historical evidence chain — do not delete them, and do not expect any
  procedure in this runbook to regenerate them.
