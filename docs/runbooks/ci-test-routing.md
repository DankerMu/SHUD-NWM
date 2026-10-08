# CI Test Routing: e2e / grib markers (node-27)

## Why

The pure-CI `unit-test` job (`.github/workflows/ci.yml`) runs the backend pytest
suite on a plain GitHub runner whose dependencies are installed from `uv.lock`
(a `--locked` install since #2573; the job puts the resulting cwd `.venv/bin`
on `PATH`). That environment has **no real PostgreSQL/Slurm/SHUD and
no ecCodes library** (so no GRIB2 decode). A handful of tests are coupled to those
environment facts and cannot pass in pure CI; they belong on the current
**node-27** oracle, whose active environment is already Python 3.11.

To keep CI honest (no false reds from environment coupling) these tests are
tagged and excluded from the pure-CI gate, then run explicitly on node-27.

## Markers

- `@pytest.mark.e2e` — end-to-end pipeline tests (network / multi-step).
- `@pytest.mark.grib` — decode GRIB2 through cfgrib. The GRIB2 payloads are encoded at
  test time with the runtime's own ecCodes (`tests/grib2_fixture_support.py`); no GRIB
  file is checked in. Each test asserts the converter did not fall back to its netcdf4
  reader and prints the ecCodes version. A test that decodes no GRIB2 does not carry
  the marker.
- `@pytest.mark.node27_docker` — dedicated disposable Docker oracle; only the
  `integration` / `timescaledb_210` / `node27_docker` triple is eligible.

All three are **opt-in** in `tests/conftest.py`:
default-skip, run only when the matching env flag is set.

| Marker | Opt-in flag |
|---|---|
| `e2e`  | `NHMS_RUN_E2E=1`  |
| `grib` | `NHMS_RUN_GRIB=1` |
| `node27_docker` | `NHMS_RUN_NODE27_DOCKER=1` for the disposable-Docker triple marker only |

## CI exclusion

The `unit-test` job (`Unit Tests (full)`) is a matrix of shards, not one pytest
process. The shard count is the length of the `matrix.shard` list in
`.github/workflows/ci.yml`. Each shard lists its own test files and runs only
those:

```
python scripts/ci/shard_tests.py --shard N --total <shard count> > shard-files.txt
test -s shard-files.txt
mapfile -t shard_files < shard-files.txt
pytest "${shard_files[@]}" -q --tb=short --durations=25 -m "not e2e and not grib and not integration"
```

So pure CI never collects e2e/grib/integration tests. The shards are disjoint
and together cover every test file pytest collects, so the full lane is green
only when **every** shard is green.

To reproduce one shard locally, run the commands below (4 is the shard count in
`ci.yml` today; `mapfile` needs bash 4 or newer, so not the stock macOS
`/bin/bash`). `--no-sync` runs the environment that is already installed and never rebuilds
it, like the node-27 lane below:

```bash
uv run --no-sync python scripts/ci/shard_tests.py --shard N --total 4 > shard-files.txt
mapfile -t shard_files < shard-files.txt
uv run --no-sync pytest "${shard_files[@]}" -q --tb=short --durations=25 -m "not e2e and not grib and not integration"
```

The generic GitHub `real-db-integration` (`SQL Migration Dry Run`) lane runs its
TimescaleDB service with:

```
pytest -vv -rs -m "integration and not timescaledb_210"
```

This is the generic SQL lane: ordinary `integration` items run, while
`timescaledb_210` stays out because its PostgreSQL 15.2 / TimescaleDB 2.10.2
oracle is node-27. A Docker socket, `/.dockerenv`, or a runnable Docker daemon
is not authorization to run that marker.

### Refreshing the shard duration table

`scripts/ci/full_test_durations.json` holds seconds per test file and only
balances the shards. Rebuild it from the junit XML of one full run with the
same marker expression:

```bash
uv run --no-sync pytest tests/ -q -m "not e2e and not grib and not integration" --junitxml=full-junit.xml
uv run --no-sync python scripts/ci/shard_tests.py durations --junit full-junit.xml
```

The second command overwrites the checked-in table (`--output` to write
elsewhere). A stale table breaks nothing: a file the table does not know gets
the median weight, and the shards stay disjoint and complete. The only drift
signal is the per-shard P95 margin line of the watcher
(`scripts/ci/full_regression_watch.py margin`), which turns from a notice into
a warning when a shard's P95 nears the job timeout.

## Retired selective-cold Docker probe

The former #1892 disposable selective-cold probe was retired with the R3 source
closure. There is no current node-27 command in this runbook to run that
withdrawn probe.

The generic `node27_docker` triple marker remains a collection boundary for a
specifically owned disposable Docker oracle: an item must carry
`integration`, `timescaledb_210`, and `node27_docker`, and
`NHMS_RUN_NODE27_DOCKER=1` unskips only that triple-marked item. It does not
unskip ordinary `integration` items or bypass their independent
`NHMS_RUN_INTEGRATION=1` and database-URL requirements.
`tests/test_node27_docker_collection_gate.py` pins that distinction.

## node-27 run convention (produce a receipt)

Run periodically on **node-27, outside production windows**. node-27's active
environment is already Python 3.11. The root rule is "Python 一律用 uv", so the
lane uses `uv run --no-sync` — which never syncs/updates the environment and
executes the already-correct active venv. The deferred node-22 checkout must
not be used for e2e/grib validation. The lane is fail-fast: after `ssh`, the
remote shell runs under `set -euo pipefail`, so a failed `git pull` or a
non-3.11 environment aborts the lane before pytest instead of continuing:

```bash
ssh -p 32099 nwm@210.77.77.27
set -euo pipefail
cd /home/nwm/NWM
export PATH=$HOME/.local/bin:$PATH
git pull --ff-only
export NHMS_GRIB_ENV_ROOT=/home/nwm/nhms-grib
export LD_LIBRARY_PATH=$NHMS_GRIB_ENV_ROOT/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}
export ECCODES_DIR=$NHMS_GRIB_ENV_ROOT
export ECCODES_DEFINITION_PATH=$NHMS_GRIB_ENV_ROOT/share/eccodes/definitions
uv run --no-sync python -c "import sys; assert sys.version_info[:2] == (3, 11), sys.version"
NHMS_RUN_E2E=1 NHMS_RUN_GRIB=1 uv run --no-sync pytest \
  -m "e2e or grib" -v -rA | tee artifacts/ci-routing/e2e-grib-$(date +%F).log
```

Keep the log as the receipt (gitignored `artifacts/` is fine for evidence).
`-rA` puts the captured output of passed tests into the log: every grib test
prints `ecCodes <version>`, so the receipt names the version that decoded.

### GRIB runtime (#2594)

The four `export` lines wire the ecCodes C library into the lane; they mirror
`scripts/run_qhh_cycle.sbatch`, which does the same for production cycles. The
Python bindings in the active venv find `libeccodes.so` through
`LD_LIBRARY_PATH`, and `ECCODES_DEFINITION_PATH` must come from the same build
as the library. Without them every grib test used to fail with its own
`Cannot find the ecCodes library` RuntimeError.

Runtime source: `/home/nwm/nhms-grib` on node-27 ships conda build
`eccodes-2.47.0-ha1d8304_0`, the same conda build as the production compute
GRIB env (checked 2026-09-30). If a later check finds the two builds differ,
align them before trusting a grib receipt. The grib fixtures need no alignment
of their own: they are encoded at test time with the same ecCodes that decodes
them (samples resolve without `ECCODES_SAMPLES_PATH`, probed on 2.47.0), so a
receipt proves decode on the version it prints.

`tests/conftest.py` preflights this: with `NHMS_RUN_GRIB=1` and at least one
`grib` item left after `-m`/`-k` deselection, the session loads ecCodes once
before any test runs. If that fails, pytest stops with a single usage error
(exit code 4) that names the missing ecCodes runtime and points here. It never
skips: an opted-in lane that skips would look green.

### real_disk cycle receipt (#2595)

`tests/test_object_store_forcing_real_disk.py` (markers `e2e` + `real_disk`)
also needs `NHMS_RUN_REAL_DISK=1`, `OBJECT_STORE_ROOT` and a read-only
`DATABASE_URL` (`nhms_display_ro`). It picks the newest cycle that all four
station/source combinations have in the store and prints it once per module
(`real_disk cycle: ...`). pytest captures that print, so `-q` and `-v` drop it
from the log. Run this suite with `-rA` (or `-s`) so the chosen cycle lands in
the receipt:

```bash
NHMS_RUN_E2E=1 NHMS_RUN_REAL_DISK=1 uv run --no-sync pytest -rA \
  tests/test_object_store_forcing_real_disk.py | tee artifacts/ci-routing/real-disk-$(date +%F).log
```

With `-rA` the line appears in the `PASSES` section, under the captured stdout
of the first test's setup. The receipt must read 4 passed, 0 skipped, plus that
line.

## node-27 detached-worktree lane (#2615)

Use this to test a commit on node-27 without moving the active checkout
(`/home/nwm/NWM`, which serves the display API): a detached worktree at the
commit, run with the active checkout's venv.

```bash
ssh -p 32099 nwm@210.77.77.27
set -euo pipefail
cd $HOME/NWM
git fetch origin
WT=$(realpath -m "$HOME/wt-<sha>")
git worktree add --detach "$WT" <sha>
cd "$WT"
export PATH=$HOME/NWM/.venv/bin:$PATH
export PYTHONPATH="$WT"
mkdir -p /home/nwm/tmp
export TMPDIR=/home/nwm/tmp
python -P -c "import packages; assert packages.__file__.startswith('$WT/'), packages.__file__"
python -m pytest -q <files> | tee $HOME/wt-<sha>-pytest.log
```

For grib files, also export the four GRIB runtime lines from the lane above
before pytest.

Why each line is there:

- `WT=$(realpath -m ...)` canonicalises the path. node-27's `HOME` is
  `/home/nwm/` (trailing slash), so a bare `$HOME/wt-<sha>` becomes
  `/home/nwm//wt-<sha>`; Python normalises the double slash out of
  `packages.__file__`, and the `startswith('$WT/')` assertion below would then
  fail even though the right code is loaded.
- `PATH` puts the active venv's `bin` first, so `python` is the venv
  interpreter and console scripts the tests call by name (for example
  `check-jsonschema`) resolve. Without it those tests fail as false reds.
- `PYTHONPATH=$WT` must stay. The active venv's editable install maps
  `packages` (and the other top-level packages) to the active checkout
  `/home/nwm/NWM`. Without `PYTHONPATH`, any subprocess the tests start by path
  would silently import the active checkout's code instead of the worktree's.
- `TMPDIR=/home/nwm/tmp` keeps pytest's temporary directories off the root
  volume (#1765).
- The `python -P -c` assertion proves the worktree's code is the code under
  test before pytest starts. `-P` is required: without it `-c` puts the current
  directory first on `sys.path`, so the check would pass even when `PYTHONPATH`
  is wrong.

Recorded deviation: this recipe calls the venv's `python` directly (found
through `PATH`) instead of the lane's `uv run --no-sync`. In a worktree
without its own venv, uv would target a different environment than the active
node-27 one; this recipe never creates or syncs an environment.

Clean up with `git worktree remove $WT` from the active checkout when done.
