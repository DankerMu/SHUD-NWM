# Tasks

Fixture level: `standard`. Risk packs: production download scheduling (a
wrong lock lets two passes write one cycle directory; a wrong delay starts a
source before the upstream has it), deployment contract of a systemd unit
rename, operator contract of the wrapper.

Must preserve: with no wrapper argument and no per-source delay key the pass
selects the same cycle, for the same sources, with the same summary path, log
path and summary keys as before; two such passes still exclude each other; an
explicit `--cycle-time` / `NODE27_DOWNLOAD_CYCLE_TIME` is used as given; the
selection rule of #3238 (readiness, latest allowed cycle first, then the
oldest not-ready cycle) and its summary detail; the lock preflight on
`NODE27_DOWNLOAD_LOCK_PATH` and the `lock_blocked` summary with blocker
`NODE27_DOWNLOAD_LOCK_HELD`; the wrapper's env-file checks and every existing
`bootstrap_blocked` reason; the shared `NODE27_DOWNLOAD_CYCLE_DELAY_HOURS`
key, its default of 8 and its 0..72 range; the adapters are not edited.

## 1. Runner (`scripts/node27_download_cycles.py`)

- [ ] 1.1 Delay per source. For a source the delay is the value of
  `NODE27_DOWNLOAD_CYCLE_DELAY_HOURS_<SOURCE>` when that key is set and not
  empty, else the shared key, else the default. A per-source value is
  validated like the shared one (numeric, 0..72) and the error names the key
  that held it. The delay of a pass is the largest delay among its selected
  sources. Keys of sources the pass did not select are not read.
- [ ] 1.2 `_latest_allowed_cycle` takes the selected sources (keyword,
  optional; when absent it resolves them the way selection does) and
  `_select_automatic_cycle` resolves its sources first and then passes them.
  No other caller changes behaviour when no per-source key is set.
- [ ] 1.3 Lock per source. The lock file of a source is the configured lock
  path with `.` and the lower-case source name appended
  (`/tmp/node27-download.lock.gfs`). A pass takes, without blocking, the lock
  of every selected source; when any of them is held it releases the ones it
  took and ends as today's `lock_blocked` (same status, return code, blocker
  code and field). The configured path itself is no longer locked; its
  preflight is unchanged. `download_lock` stays the one place that calls
  `flock`.

## 2. Wrapper (`scripts/node27_download_once.sh`)

- [ ] 2.1 One optional positional argument: the source. `GFS` and `IFS` are
  accepted in any letter case. Any other value, or more than one argument,
  ends with `bootstrap_blocked` and a new reason before the env file is
  sourced and before the runner is started.
- [ ] 2.2 With a source: the runner gets `--source <SOURCE>` (upper case); the
  log file and the summary file are the paths the wrapper would use without a
  source, with `-<source>` (lower case) inserted before the last extension of
  the file name, or appended when the file name has none
  (`download.log` -> `download-gfs.log`, `last-summary.json` ->
  `last-summary-gfs.json`). The start/done lines name the source.
- [ ] 2.3 Without a source nothing changes: no `--source`, the configured
  paths.
- [ ] 2.4 The strict-env block clears `NHMS_NODE27_DOWNLOAD_SOURCES` in place
  of `NODE27_DOWNLOAD_SOURCES`. The header comment describes the argument.

## 3. Units, env template, service list

- [ ] 3.1 `infra/systemd/nhms-node27-download@.service`: the old service with
  `ExecStart=/bin/bash /home/nwm/NWM/scripts/node27_download_once.sh %i` and a
  description that names the instance; the same `systemd.log` / `systemd.err`
  lanes and `TimeoutStartSec=0`.
  `infra/systemd/nhms-node27-download@.timer`: the old timer with
  `Unit=nhms-node27-download@%i.service`. Delete
  `infra/systemd/nhms-node27-download.service` and `.timer`.
- [ ] 3.2 `infra/env/node27-download.example`: keep
  `NODE27_DOWNLOAD_CYCLE_DELAY_HOURS=8`; add
  `NODE27_DOWNLOAD_CYCLE_DELAY_HOURS_GFS=6` with a comment that states the
  rule (per-source key, else shared key; a pass with several sources uses the
  largest), the measured basis in one line (GFS on the first backend by
  T+5.22 h at the latest over 34 cycles in November 2026, IFS at T+7.57 h),
  and that the lock, log and summary files are per source when the wrapper is
  given one.
- [ ] 3.3 `scripts/node27_resource_governance.py` `DEFAULT_SERVICES`: the two
  download names are replaced by the four instance names
  (`nhms-node27-download@GFS.service`, `@GFS.timer`, `@IFS.service`,
  `@IFS.timer`). Read `collect_systemd` first; if it cannot take an instance
  name as it is, stop and report.

## 4. Tests

In `tests/test_node27_download_cycles.py` unless a file is named. Each test
sets or deletes every environment variable it depends on;
`_clear_selection_overrides` also deletes the two per-source delay keys.

- [ ] 4.0 Existing helper: the lambda in `_pin_latest_allowed_cycle` takes the
  new keyword (`sources=None`). Without it the five automatic-pass tests that
  use the helper fail with a `TypeError` once 1.2 is in. Besides the
  helper `_clear_selection_overrides`, 4.5 and 4.6, this is the only edit to
  existing test code; report it.

- [ ] 4.1 Delay resolution, asserted through `_select_automatic_cycle` (the
  latest allowed cycle in its detail, or the `ValueError` it raises) so that
  the tests run on the unmodified tree: the per-source key wins over the shared key; the
  shared key is used when the per-source key is absent or empty; the default
  is 8 with neither; a non-numeric and an out-of-range per-source value raise
  and the message contains the per-source key name; an invalid key of a
  source that is not selected does not raise; a pass with GFS and IFS, GFS
  key 6, shared key 8 gets 8.
- [ ] 4.2 Selection across passes on one store (GFS key 6, shared 8, clock
  six hours and one minute after cycle C3; C1 and C2 downloaded for both
  sources except one IFS file of C1; nothing of C3): the selection for GFS
  alone is C3; for IFS alone it is C1; for both it is C1 and the detail's
  latest allowed cycle is C2.
- [ ] 4.3 Locks with real `flock`, through `main` with the source command
  stubbed. The test holds the derived GFS lock file
  (`<configured path>.gfs`), not the configured path: (a) a GFS pass ends as
  `lock_blocked` and the stub is not called; (b) a GFS+IFS pass ends as
  `lock_blocked` and the stub is not called, once with the GFS lock file held
  and once with the IFS lock file held; (c) an IFS pass calls the stub;
  (d) a GFS+IFS pass run while the IFS lock file is held ends as
  `lock_blocked`, and afterwards the GFS lock file can be locked by the test
  (the pass released it); both assertions are made. The two existing
  tests that replace `download_lock` with a fake that refuses every path stay
  green unedited as long as `download_lock` keeps taking one path.
- [ ] 4.4 The wrapper, executed with `bash` against a temporary repository
  root (`NODE27_DOWNLOAD_REPO`) whose `.venv/bin/python` is a stub that
  records its arguments and `NHMS_NODE27_DOWNLOAD_SOURCES`, and a 0600 env
  file. The wrapper is started with an explicit minimal environment (`PATH`
  plus what the case sets), never the inherited one:
  `NODE27_DOWNLOAD_REPO`, `NODE27_DOWNLOAD_ENV_FILE` and
  `NODE27_DOWNLOAD_BOOTSTRAP_LOG` always point into the temporary directory
  (the bootstrap log defaults to a production file on node-27, where these
  tests also run); `NODE27_DOWNLOAD_ALLOW_AMBIENT_ENV` and
  `NODE27_DOWNLOAD_LOG_FILE` are absent unless a case sets them. Cases: (a) argument `GFS` -> `--source GFS`, summary `last-summary-gfs.json`,
  log `download-gfs.log`; (b) argument `ifs` -> `--source IFS`, `-ifs` files;
  (c) no argument -> no `--source`, the configured paths; (d) argument `ERA5`
  and (e) two arguments -> return code 2, the stub did not run; (f) with an
  ambient `NHMS_NODE27_DOWNLOAD_SOURCES` and an env file that does not set
  it, the stub sees it unset; (g) a configured summary path without an
  extension gets the suffix appended; (h) an explicit
  `NODE27_DOWNLOAD_LOG_FILE` in the env file gets the same `-gfs` insertion.
- [ ] 4.5 Unit and template text (retarget
  `test_wrapper_and_env_contract_do_not_source_display_env` and the two path
  constants): the template service's `ExecStart` ends with
  `node27_download_once.sh %i`; the template timer has `OnUnitActiveSec=30min`
  and `Unit=nhms-node27-download@%i.service`; the two old unit files do not
  exist; the env template has both delay lines.
- [ ] 4.6 `tests/test_node27_timeseries_retention.py`: the pinned set of
  services with a `systemd.err` lane names `nhms-node27-download@.service` in
  place of `nhms-node27-download.service`; nothing else in that test changes.
  `tests/test_node27_resource_governance.py` compares against
  `DEFAULT_SERVICES` itself and needs no edit; `tests/test_node27_pgdata_migrate.py`
  uses the old names as fixture strings and is not edited.

## 5. Evidence floor

- [ ] 5.1 Red first, in `.workplans/3237/red-runs.log`, with the failing
  line. Fail on the unmodified tree: 4.1 "the per-source key wins" and the
  two invalid per-source values; 4.2 GFS alone; 4.3 (a), (b), (d); 4.4 (a),
  (b), (d), (e), (f), (g) with a source, (h). Pass before and after, and the
  log says so: 4.1 shared-key fallback, default, the ignored key of an
  unselected source, and GFS+IFS -> 8; 4.2 IFS alone and both; 4.3 (c); 4.4
  (c). Those are the guards of mutations M2, M3b and M9 below.
- [ ] 5.2 Mutations, each applied by hand, the named test run, the result
  recorded, the file restored and checked with `cmp`:
  M1 the per-source key is never read -> 4.1, 4.2;
  M2 a pass with several sources uses the smallest delay -> 4.1, 4.2;
  M3 every pass locks the configured path only -> 4.3 (a), (b);
  M3b every pass takes the locks of both sources -> 4.3 (c);
  M4 a pass with several sources takes only its first source's lock -> 4.3
  (b), the variant with the second source's lock file held;
  M5 a blocked pass keeps the locks it took (hold a reference to their open
  handles past the block) -> 4.3 (d); if the implementation leaves no place
  where that can be written, record M5 as not expressible and say why;
  M6 the wrapper does not pass `--source` -> 4.4 (a);
  M7 the wrapper does not derive the summary path -> 4.4 (a);
  M8 the wrapper accepts any argument -> 4.4 (d);
  M9 an invalid key of an unselected source raises -> 4.1.
- [ ] 5.3 Local: `tests/test_node27_download_cycles.py`,
  `tests/test_node27_timeseries_retention.py`,
  `tests/test_node27_pgdata_migrate.py`, the tests of
  `scripts/node27_resource_governance.py`, and every test file that names
  `nhms-node27-download` or `node27_download_once` (find with `git grep -l`);
  `bash -n scripts/node27_download_once.sh`;
  `uv run --no-sync ruff check --no-cache .`; markdownlint on the changed
  docs; the topology hard gate count unchanged;
  `openspec validate per-source-download-passes --strict --no-interactive`.
- [ ] 5.4 node-27: the same test files in a temporary detached worktree, no
  skip in `tests/test_node27_download_cycles.py`.
- [ ] 5.5 Docs (orchestrator): `docs/runbooks/production-ops/service-bringup.md`
  (unit names, the switch-over steps, the per-source files),
  `docs/runbooks/production-ops/overview.md`, `infra/env/README.md`,
  `docs/runbooks/tier-node27-timeseries-storage.md` (unit names in the writer
  lists).
- [ ] 5.6 CI: every shard of the full suite green.

## 6. After merge (evidence owed on the issue, not a task of this change)

- Deployment on node-27 at a moment when the old service is inactive: disable
  the old timer, pull, install the template units, add the GFS delay key to
  the untracked env file, enable `nhms-node27-download@GFS.timer` and
  `@IFS.timer`; receipt with the first pass of each instance.
- One week later: the completion table of #3225 again (newest raw-file mtime
  per cycle and source, T+ hours), and scheduler pass evidence for the same
  window (no cycle stuck unavailable, no partially downloaded cycle
  submitted). The issue closes on that.
