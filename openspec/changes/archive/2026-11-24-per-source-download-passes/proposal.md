# Per-source download passes

Issue: #3237 (part of #3217, follow-up of #3225 and #3238).

## Why

The node-27 download job runs one pass for both sources: one timer, one lock,
one 8 h delay, the sources one after another. Two costs follow, and neither is
forced by the upstream:

- **GFS waits for the delay, not for publication.** Measured from the public
  bucket listings for the 34 cycles 2026-11-08T00 .. 2026-11-24T12, every GFS
  file the job needs is on the first backend of the adapter chain by T+5.22 h
  at the latest (T+4.62 h or earlier on the other 33 cycles). IFS is published
  as one batch at T+7.57 h on every cycle. So 8 h holds GFS back by about 3 h
  and IFS by at most 0.43 h. The table is on the issue.
- **A slow source delays the other.** On 2026-11-18T00 GFS was complete on the
  upstream at T+4.57 h; node-27 started it at T+8.1 h and finished at
  T+12.45 h, and IFS, published at T+7.57 h, could only start at T+12.46 h.
  Both sources missed the scheduler's 10 h lag for that cycle.

The same structure produces a limit recorded by #3238: a retry of an old cycle
runs every selected source, and one pass can hold the lock for about 12 h.

## What changes

1. **One pass per source.** The repository ships a template unit and timer,
   `nhms-node27-download@.service` / `nhms-node27-download@.timer`, deployed as
   the instances `GFS` and `IFS`. The instance name is handed to the wrapper,
   which passes it to the runner as `--source`. The two old unit files are
   removed from the repository.
2. **The lock is per source.** The runner derives one lock file per source
   from the configured lock path and takes the lock of every source the pass
   selected. Two passes for different sources do not block each other; two
   passes that share a source do, including a manual pass without `--source`
   against either instance.
3. **A delay per source.** `NODE27_DOWNLOAD_CYCLE_DELAY_HOURS_GFS` and
   `NODE27_DOWNLOAD_CYCLE_DELAY_HOURS_IFS` override the shared
   `NODE27_DOWNLOAD_CYCLE_DELAY_HOURS` for that source. A pass with several
   sources uses the largest delay among them, so its one cycle is allowed for
   all of them. The template sets GFS to 6 h and leaves the shared key at 8 h.
4. **Per-source log and summary files** when the wrapper is given a source,
   derived from the configured paths (`download-gfs.log`,
   `last-summary-ifs.json`). Without a source the wrapper behaves as today.
5. The wrapper clears `NHMS_NODE27_DOWNLOAD_SOURCES`, the key the runner
   reads, in place of `NODE27_DOWNLOAD_SOURCES`, which nothing reads.

## Decisions

- **Option 3 of the issue (independent runs, each with its own delay).** A
  per-source delay inside one unit would not fix the blocking: a oneshot unit
  cannot be started again while it runs, so a GFS download that takes four
  hours would still keep IFS from starting. Running the sources in parallel
  inside one invocation has the same flaw.
- **GFS 6 h.** The latest publication in 17 days is T+5.22 h; 6 h leaves
  0.78 h over it. A pass that still starts before publication is not an
  error: the adapter polls or fails, and the next tick retries the cycle.
  5.5 h would gain half an hour that nothing downstream uses today.
- **IFS keeps 8 h.** Publication at T+7.57 h leaves no room worth a key of its
  own in the template; the per-source key exists for symmetry of the rule, not
  because a value is proposed.
- **The lock is keyed in the runner, not in the wrapper.** A wrapper-side path
  suffix would leave a manual pass without `--source` free to run next to both
  instances on the same cycle directory.
- **A multi-source pass keeps one cycle.** Selecting a cycle per source inside
  one pass would change the summary shape for no user: the shipped units run
  one source each.
- **No scheduler change.** The scheduler's 10 h lag is a separate decision
  (issue boundary). Under that lag the gain is slack: a slow GFS download that
  starts two hours earlier, and an IFS download that no longer waits for it,
  are more likely to be complete when the scheduler looks.

## Checked, not assumed

- From node-27 the first backend of the GFS chain is reachable (HEAD of one
  needed `.idx`: s3, gcs, azure and NOMADS answer 200; `ftpprd` times out).
- The two adapter commands do not share local state: temporary files are
  unique (`tempfile.NamedTemporaryFile`), the persisted GFS breaker state is
  `state/source_circuit/gfs_<backend>.json`, raw files live under
  `raw/<source>/`. Their database writes are keyed by source
  (`met.data_source` on `source_id`, `met.forecast_cycle` on `source_id` and
  cycle time; confirmed by the fixture review).
- No program reads `last-summary.json`; only the wrapper and the env template
  name it. `scripts/node27_resource_governance.py` lists the two unit names
  in `DEFAULT_SERVICES`.

## Known limits

- With per-source instances the timer no longer updates `last-summary.json`;
  the files to read are `last-summary-gfs.json` and `last-summary-ifs.json`.
- The two downloads can now overlap and share the host's bandwidth and CPU.
  In the normal case they do not overlap (GFS finishes about 20 minutes after
  T+6 h, IFS starts at T+8 h).
- A manual pass without `--source` uses the largest delay of its sources, so
  it selects the cycle the slower source is allowed, not the newest GFS cycle.
- The numbers cover 17 days and one season. A later upstream schedule change
  shows up as GFS passes that fail or poll at T+6 h; the key is the knob.
- Why some GFS downloads take hours on node-27 is not explained here (adapter
  behaviour, out of scope).
- `packages/common/node27_pgdata_host.py` still lists the old unit names in
  an inventory of the retired production migration; it is not edited.

## Impact

- Code: `scripts/node27_download_cycles.py`, `scripts/node27_download_once.sh`,
  `scripts/node27_resource_governance.py` (service list).
- Units and env: `infra/systemd/nhms-node27-download@.service`,
  `infra/systemd/nhms-node27-download@.timer` (new);
  `infra/systemd/nhms-node27-download.service`, `.timer` (removed);
  `infra/env/node27-download.example`, `infra/env/README.md`.
- Tests: `tests/test_node27_download_cycles.py`,
  `tests/test_node27_timeseries_retention.py` (one unit name in a pinned set),
  and any test that pins the governance service list.
- Docs: `docs/runbooks/production-ops/service-bringup.md`, `overview.md`,
  `docs/runbooks/tier-node27-timeseries-storage.md` (unit names).
- Spec: `node27-download-orchestration`, one added requirement.
- Deployment on node-27 after merge: stop the old timer, wait for the old
  service to be inactive, pull, install the template units, add the GFS key to
  the untracked env file, enable the two instance timers. Until that is done
  the installed old unit keeps working: it calls the wrapper without a source.
