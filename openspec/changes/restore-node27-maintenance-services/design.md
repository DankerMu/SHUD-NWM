## Context

Three node-27 maintenance units failed on 2026-09-18 (proposal "Why"). The
operator decisions taken for this change (2026-09-18, recorded answers):

1. #2360: split the canonical lane into a system unit run as `frd_muziyao`; the
   operator runs one prepared sudo script. (Whole-unit identity switch rejected:
   the precip PNG cache root `/home/nwm/.cache/nhms/mvt` is `nwm:nwm 775`, so
   uid 1103 cannot delete there — the fail-closed would move lanes, not end.)
2. Accept the immediate `Persistent=true` catch-up of the refused 2026-09-18
   retention tick when the 06:36Z timer is installed (done: stage A).
3. #2425: newest-first within a hypertable + bound 2, deployed by rebinding the
   compression unit to the repo unit on `/home/nwm/NWM`.

## Goals / Non-Goals

Goals: the three units end `Result=success` on their production ticks; the
compression lane stops doing work retention deletes; canonical pruning resumes
without relaxing the lock contract; every raw-retention failure alerts.

Non-goals (each with its reason):

- Relaxing `.nhms-copyback-batch.lock` to group-shared — node-22's pre-#1831
  code refuses non-`0600` locks (#2360 boundary).
- A retention-horizon filter on the compression side (#2425 option b) —
  newest-first already keeps the selected set away from the retention edge
  whenever a hypertable has ≥ bound eligible chunks younger than the retention
  cutoff; the residual edge is recorded in Risks, not built for.
- Changing the retention window (21 d) or the retention runner — retention is
  correct (#2425 boundary).
- Governance collector coverage of system units —
  `scripts/node27_resource_governance.py` audits the `nwm` user manager by
  design; the system unit's failures reach the operator through its own
  `OnFailure=` (D5).

## Decisions

### D1 — newest-first within a hypertable, table order unchanged

`_CHUNK_QUERY` keeps `ORDER BY hypertable_schema, hypertable_name, range_end ASC`
(receipt determinism). `_classify` groups eligible chunks by
`(hypertable_schema, hypertable_name)` in query order and stable-sorts each
group by `range_end` descending (a sort, not a reversal: the input order of an
injected fetch is not guaranteed), then takes the first `per_tick_bound`.
Deferred keeps the same reordered sequence; skipped (inside lag) keeps query
order.

Why not a global `range_end DESC`: `hydro.river_timeseries_legacy` has
558 GB / 108 GB 7-day chunks that would then outrank the day chunks and burn
the 3600 s statement budget every tick.

Why this fixes #2425: compression cutoff is `W − lag` (2 d), retention cutoff
`W − 21 d` on the same watermark W (confirmed by the 2026-09-17 receipts, both
`2026-09-16T00:00:00Z`). Newest-first selects the youngest eligible chunks —
~19 days of life left — instead of the oldest, which are retention's next
targets.

### D2 — bound 2 from the narrow geometry

Inputs (node-27, 2026-09-17/18): river day chunks 19–21 GB; 46.84 GB compressed
in 2593 s (timer start 04:25:32Z → receipt 05:08:45Z) ≈ 55 s/GB including
per-chunk overhead; arrival 1 river chunk/day + ~1 forcing chunk/week.

- Wall: `2 × 21 GB × 55 s/GB ≈ 2310 s ≤ 3900 s`, leaving ~1590 s for growth
  and the non-compress residual; `4 × 20 GB` (≈ 4400 s) does not fit — the
  2026-09-18 rc=124.
- Throughput: river takes both slots while it has 2 eligible chunks (table
  order: `hydro.river_timeseries` → `_legacy` → `met.*`, so forcing gets a slot
  only when neither hydro table has one). River arrives 1/day; compression
  takes 2/day from the young end and retention 1/day from the old end, so the
  river backlog shrinks by ~2 chunks/day net. Measured after the Stage A
  bound-2 run (W = 2026-09-17T12:00Z): 14 eligible uncompressed river chunks,
  so the backlog drains around 2026-09-25. That run compressed 41.13 GB in
  2241 s (54.5 s/GB), confirming the rate above.
- Retention overlap: a ≤ 2310 s tick from 04:25Z ends by ~05:04Z, far before
  the 06:36Z retention timer; even the full 3941 s systemd wall (05:30:41Z)
  clears it.

The template, the pin test, the runbook derivation, the live env and the
in-code default for direct invocations
(`DEFAULT_COMPRESSION_PER_TICK_BOUND`, `packages/common/node27_timeseries_compression_budget.py:22`)
all carry 2 (hypertable-compression requirement "per-tick bound … consistent").

### D3 — `NODE27_RAW_RETENTION_LANES`

Env-only (like `NODE27_RAW_RETENTION_ENABLED` / `_PLAN_ONLY`, no CLI flag).
Comma-separated, whitespace-trimmed, exact lowercase names from
`{raw, canonical, precip-cache}`. Unset → all three (byte-identical to today).
Set but empty after trimming, or containing an unknown name → preflight blocker
`{"field": "lanes", ...}` → no lane is touched. An unselected lane contributes
exactly one `skipped[]` entry `{"key": <lane>, "reason": "lane_not_selected"}`,
its root is never probed or listed, and no lock is taken for it. The summary
records the selected lanes (sorted) so each unit's summary says which unit
wrote it. Both units still use one cutoff rule (same watermark anchor, same
`retention_days`), so a cycle's canonical mirror and its PNGs age out on the
same tick date even though two processes remove them.

Data shape: `RawRetentionConfig` gains `lanes: frozenset[str]` with a default
of all three lanes (it is constructed by keyword elsewhere, e.g.
`tests/test_node27_mvt_cache_retention.py`). `lanes` goes into the run payload
base (so disabled and plan-only summaries carry it too) but not into the
preflight-blocked payload (no config exists there). `SCHEMA_VERSION` stays
`nhms.node27_raw_retention.production.v5`: the field is additive, the summary
has no JSON schema, and every documented consumer is a `jq` projection that
ignores unknown keys.

### D4 — canonical system unit

`infra/systemd/system/nhms-node27-canonical-retention.service`:
`Type=oneshot`, `User=frd_muziyao`, `WorkingDirectory=/home/nwm/NWM`,
`ExecStart=/home/nwm/NWM/scripts/node27_raw_retention_once.sh`,
`Environment=NODE27_RAW_RETENTION_ENV_FILE=/etc/nhms/node27-canonical-retention.env`
and `NODE27_RAW_RETENTION_BOOTSTRAP_LOG=/var/log/nhms-node27-canonical-retention/bootstrap.log`,
`LogsDirectory=nhms-node27-canonical-retention` (mode 0755, so `nwm` can read
summaries), `RuntimeDirectory=nhms-node27-canonical-retention` for the flock
path, `StandardOutput=journal`, `StandardError=journal`, `TimeoutStartSec=0`,
`OnFailure=nhms-node27-system-unit-failure-alert@%n.service`. Timer
`OnCalendar=*-*-* 03:35:00 UTC`, `Persistent=true`.

The env file is generated by the install script from the `nwm` env
(`infra/env/node27-raw-retention.env`): lines setting `NODE27_RAW_RETENTION_LANES`,
`_LOG_ROOT`, `_LOCK_PATH`, `_LOG_FILE`, `_SUMMARY_PATH` or `_BOOTSTRAP_LOG` are
filtered out, then `LANES=canonical`, `LOG_ROOT=/var/log/nhms-node27-canonical-retention`
and `LOCK_PATH=/run/nhms-node27-canonical-retention/raw-retention.lock` are
appended, so no key is duplicated and no path points into `/home/nwm`. Written
with umask 077 and `chown frd_muziyao` (the wrapper requires mode 600, no
symlink). Values are never printed. It is a snapshot: after any change to the
`nwm` env the operator re-runs the install script (runbook), and the Stage B
receipt checks both summaries carry equal `cutoff`, `retention_days` and
`sources`.

Access facts (node-27 2026-09-18): uid 1103 groups `nfsdata,nwmuser`;
`/home/nwm`, `/home/nwm/NWM` 755, `.venv` 775, the uv interpreter tree 775 —
readable (the install script still proves it with an import probe, D6);
object-store root `frd_muziyao` 775 (the guard compares the root owner);
`canonical/<S>` `frd_muziyao:nwmuser 2775`; the lock owner is 1103.

### D5 — alerting

- `nwm` user unit `nhms-node27-raw-retention.service` gains
  `OnFailure=nhms-node27-unit-failure-alert@%n.service` (same as the timeseries
  retention unit), restoring the `counts.failed == 0` criterion as a live signal.
- System template `nhms-node27-system-unit-failure-alert@.service`: `User=nwm`,
  `SupplementaryGroups=systemd-journal`,
  `EnvironmentFile=-/home/nwm/NWM/infra/env/node27-frontier-alert.env`,
  `Environment=NHMS_UNIT_FAILURE_JOURNAL_SCOPE=system`, same handler.
- Handler: `NHMS_UNIT_FAILURE_JOURNAL_SCOPE=system` → `journalctl -u`; any other
  value or unset → `journalctl --user -u` (today's behaviour).

The alert channel is live: 2026-09-18 13:15:01 the user template sent
`SMTP-ACCEPTED` for the refused retention tick.

### D6 — install script (operator, sudo, once)

`scripts/node27_canonical_retention_install.sh`, idempotent, fail-closed
preconditions: running as root; `frd_muziyao` resolves to uid 1103; the
object-store root and the lock file are owned by 1103, the lock file exists and
is `0600` (never created); the three repo unit files exist; source env exists
mode 600; an import probe as the unit user succeeds
(`runuser -u frd_muziyao -- env PYTHONPATH=/home/nwm/NWM /home/nwm/NWM/.venv/bin/python -c "import psycopg2, scripts.node27_raw_retention"`
from `/home/nwm/NWM`). Actions: `/etc/nhms` 0755 root; env file as D4;
`install -m 0644` the two system units + system alert template into
`/etc/systemd/system/`; `daemon-reload`; `enable --now` the timer (the timer
file carries `[Install] WantedBy=timers.target`); one synchronous
`systemctl start` of the service; print `systemctl show -p Result,ExecMainStatus`
and the newest summary's counts (no env values). Rollback printed in the
runbook: `systemctl disable --now` the timer, remove the three units and the env
file, `daemon-reload`.

### D7 — compression unit rebind (#2285 item 1)

#1895 is closed; its fence tree `NWM-maintenance-reviewed-95481481` at `95481481`
never receives fixes. The rebind installs the repo unit (`WorkingDirectory` and
`ExecStart*` under `/home/nwm/NWM`, env
`/home/nwm/NWM/infra/env/node27-timeseries-compression.env`). The live env
content is carried over from the fence env (bound already 2 since stage A),
except its `NODE27_TIMESERIES_COMPRESSION_REPO_ROOT`, which names the fence
tree and would keep the runner on fence code (the budget preflight resolves the
runner root from the env file first, and `--check` does not exercise that path):
back up the existing `infra/env` file (`cp -p`), `install -m 0600` the fence
env, rewrite `REPO_ROOT` to `/home/nwm/NWM`, assert with `grep -c` that
`REPO_ROOT=/home/nwm/NWM`, `RECEIPT_PATH` and `LOCK_PATH` each appear once with
the expected `/home/nwm/NWM/.nhms-issue1069-live/` paths, run the budget
preflight `--check`, then install the unit and `daemon-reload`. Proof that the
rebind took: the next receipt's `head_sha` equals `git -C /home/nwm/NWM
rev-parse HEAD`, and `systemctl --user show -p DropInPaths` is empty. The fence SOURCE, its `.venv` and STATE
stay as rollback inputs (runbook "do not delete these paths" is kept, reworded
from "active" to "rollback").

### D8 — deployment order on node-27

Stage A (done 2026-09-18 13:57Z, before this PR): fence env bound 4→2 (backup
`…env.bak-bound4-20260918`, preflight rc 0); install repo `raw-retention.service`
and `timeseries-retention.timer`; `daemon-reload`; `reset-failed` compression +
retention; timer restart → catch-up tick `Result=success` (dropped
`_hyper_9_126`, `_hyper_3_62`, `_hyper_1_61`); manual compression start under
bound 2 13:58:50Z → `Result=success` 14:36:11Z, receipt `outcome=clean`,
`per_tick_bound=2`, selected `_hyper_9_153`/`_hyper_9_154` committed
(41.13 GB → 11.01 GB).

Stage B (after merge): `git pull --ff-only` in `/home/nwm/NWM`; D7 rebind; add
`NODE27_RAW_RETENTION_LANES=raw,precip-cache` to the `nwm` env; install the
repo `raw-retention.service` (OnFailure) + `daemon-reload`; operator runs D6;
start the `nwm` raw-retention unit once (a normal production tick: raw and PNG
cache prune) and the compression unit once under the repo code; start the
system alert template once by hand
(`nhms-node27-system-unit-failure-alert@nhms-node27-canonical-retention.service.service`)
and record its `SENT` / `SMTP-ACCEPTED` lines from the system journal; record
which database and hypertables `yd-node27-timeseries-retention` touches
(#2425 note); collect receipts.

## Risks / Trade-offs

- **Legacy giant chunks (#1988)**: once `hydro.river_timeseries` has fewer than
  `bound` eligible uncompressed chunks, the free slot goes to
  `hydro.river_timeseries_legacy`, whose uncompressed 558 GB chunk exceeds the
  wall with anything else in the tick. This trajectory exists identically under
  oldest-first. Dates (#2425 catalog): the 558 GB legacy chunk (range_end
  2026-09-17) is compression-eligible from ~09-19 and dropped by retention on
  ~10-08; the river backlog keeps both slots until ~09-25 (14 eligible on
  09-18). Between ~09-25 and ~10-08 the second slot can select it and the tick ends rc=124. The open
  #1988 (DROP the legacy table) removes it; the runbook records the dated
  check and the stop-gap (bound 1 until that chunk is dropped: with one river
  arrival per day, bound 1 never reaches the legacy table).
- **Transitional edge**: while a river backlog drains, the last remaining
  backlog chunk can be the second slot on the day retention drops it (one chunk,
  once). Accepted over coupling compression to the retention window.
- **Two processes prune one cycle** (D3): canonical and its PNGs leave on the
  same cutoff date but not atomically; if one unit fails, the other lane still
  prunes. That partial state is already allowed today — the living requirement
  "A lane's traversal failure retires only that lane or source" isolates lanes
  inside one process the same way.
- **#2425 acceptance 1/3 need two consecutive daily ticks**: observed after
  merge and posted to #2425 before it is closed.
