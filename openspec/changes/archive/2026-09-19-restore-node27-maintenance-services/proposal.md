## Why

On 2026-09-18 three node-27 maintenance user units sat in `failed`
(`systemctl --user list-units --failed`, read-only census 13:5xZ):

| unit | exit | measured cause |
|---|---|---|
| `nhms-node27-raw-retention.service` | 1 | summary `raw-retention-20260918T033532Z.json`: `copyback_lock_failures.lock_unsafe = 10`, every aged canonical cycle `CopybackLockError` EACCES on `.nhms-copyback-batch.lock` (`-rw------- frd_muziyao`). The unit runs as `nwm` (1005); the lock contract needs the copyback root owner (1103). This is #2360's accepted fail-closed state. |
| `nhms-node27-timeseries-compression.service` | 124 | wrapper wall (3900 s). Narrow `hydro.river_timeseries` day chunks are now 19–21 GB; the 2026-09-17 tick compressed 46.84 GB in 2593 s (~55 s/GB). `bound=4` selected 150–153 (~78 GB): 150/151/152 committed, 153 was TERMed and rolled back. |
| `nhms-node27-timeseries-retention.service` | 1 | `RETENTION_CONCURRENT_INVOCATION` at 05:15:00Z — compression was still running (04:25Z + wall). The repo timer is 06:36Z (`59a26eca`), the installed one still 05:15Z: #2285 drift item 3. |

Behind the compression failure sits #2425: both compression and retention walk
`range_end ASC`, and retention's eligible set (`range_end <= W − 21 d`) is always
a prefix of compression's (`range_end < W − 2 d`) on the same display watermark
W, so compression's first picks are exactly the chunks retention drops within a
day (2026-09-16: 4 of 4 dropped same day; 2026-09-17: 3 of 4 dropped 50 min
later, the 4th on the next tick).

## What Changes

- **Compression selection (#2425)**: within each hypertable, walk eligible chunks
  **newest-first** (`range_end` descending); the hypertable order stays
  `(schema, name)` so a free slot still never jumps to a later table ahead of
  the current one's eligible chunks. Selected and deferred lists keep a
  deterministic order.
- **Compression per-tick bound 4 → 2** in the template, pin test, runbook
  derivation and live env, re-derived from the narrow-table geometry (1 river
  day-chunk/day, ~20 GB each, ~55 s/GB).
- **Raw-retention lane selection (#2360)**: new env gate
  `NODE27_RAW_RETENTION_LANES` (subset of `raw,canonical,precip-cache`; unset =
  all three, byte-identical behaviour). An unselected lane is recorded as a
  `lane_not_selected` skip and never enumerated; an unknown or empty value is a
  preflight blocker (no deletion).
- **Canonical lane runs as the copyback root owner**: new system units
  `nhms-node27-canonical-retention.{service,timer}` with `User=frd_muziyao`,
  `LANES=canonical`; the `nwm` user unit runs `raw,precip-cache`. A root install
  script (`scripts/node27_canonical_retention_install.sh`) is run once by the
  operator with sudo. The lock file mode/owner is unchanged.
- **Failure alerting on both raw-retention units**: the `nwm` user unit gains
  `OnFailure=nhms-node27-unit-failure-alert@%n.service`; a system-scope template
  `nhms-node27-system-unit-failure-alert@.service` (runs the same handler as
  `nwm` with `systemd-journal` read access) serves the system unit; the handler
  gains a journal-scope switch.
- **#2285 unit drift**: install the repo `raw-retention.service` and
  `timeseries-retention.timer` (done 2026-09-18 stage A), and rebind the
  compression unit from the retired #1895 fence tree
  `/home/nwm/NWM-maintenance-reviewed-95481481` to the repo unit on
  `/home/nwm/NWM` (fence SOURCE retained as rollback, not deleted).

## Capabilities

### New Capabilities

(none)

### Modified Capabilities

- `hypertable-compression`: newest-first selection within a hypertable; bound
  target 2.
- `node27-raw-retention`: lane selection gate; canonical lane in a system unit
  as the copyback root owner; both units alert on failure.

## Impact

- Code: `scripts/node27_timeseries_compression.py` (`_classify`),
  `scripts/node27_raw_retention.py` (config + `collect_targets` + summary),
  `scripts/node27_unit_failure_alert_once.sh` (journal scope).
- New: `scripts/node27_canonical_retention_install.sh`,
  `infra/systemd/system/nhms-node27-canonical-retention.{service,timer}`,
  `infra/systemd/system/nhms-node27-system-unit-failure-alert@.service`.
- Changed: `infra/systemd/nhms-node27-raw-retention.service` (`OnFailure=`),
  `infra/env/node27-timeseries-compression.example` (bound 2),
  `infra/env/node27-raw-retention.example` (lanes).
- Docs: `docs/runbooks/tier-node27-timeseries-storage.md` (per-tick capacity,
  live compression unit), `docs/runbooks/current-production-ops.md` (canonical
  lock identity), receipt under `docs/runbooks/receipts/`.
- node-27 live: env/unit writes listed in design D8; one sudo step by the
  operator.
