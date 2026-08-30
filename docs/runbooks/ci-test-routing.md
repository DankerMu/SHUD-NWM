# CI Test Routing: e2e / grib markers (node-27)

## Why

The pure-CI `unit-test` job (`.github/workflows/ci.yml`) runs the backend pytest
suite on a plain GitHub runner installed via `pip install -e ".[dev]"`. That
environment has **no real PostgreSQL/Slurm/SHUD, no cwd `.venv`, and no
eccodes-version-matched GRIB fixtures**. A handful of tests are coupled to those
environment facts and cannot pass in pure CI; they belong on the current
**node-27** oracle, whose active environment is already Python 3.11.

To keep CI honest (no false reds from environment coupling) these tests are
tagged and excluded from the pure-CI gate, then run explicitly on node-27.

## Markers

- `@pytest.mark.e2e` — end-to-end pipeline tests (network / multi-step).
- `@pytest.mark.grib` — require real GRIB2 decode + eccodes-version-matched fixtures.

Both are **opt-in** in `tests/conftest.py` (same pattern as `integration`):
default-skip, run only when the matching env flag is set.

| Marker | Opt-in flag |
|---|---|
| `e2e`  | `NHMS_RUN_E2E=1`  |
| `grib` | `NHMS_RUN_GRIB=1` |

## CI exclusion

The `unit-test` job runs:

```
pytest tests/ -v --tb=short -m "not e2e and not grib and not integration"
```

So pure CI never collects e2e/grib/integration tests. The generic GitHub
`real-db-integration` (`SQL Migration Dry Run`) lane runs its TimescaleDB service
with:

```
pytest -vv -rs -m "integration and not timescaledb_210"
```

This is the generic SQL lane: ordinary `integration` items run, while
`timescaledb_210` stays out because its PostgreSQL 15.2 / TimescaleDB 2.10.2
oracle is node-27. A Docker socket, `/.dockerenv`, or a runnable Docker daemon
is not authorization to run that marker.

## node-27 TimescaleDB 2.10.2 lane (produce a receipt)

Run the #1892 isolated probe only explicitly on node-27, outside a production
window. Do not target `nhms-db`, port `55432`, live PGDATA, production paths, or
a production/live DSN. The probe creates its own disposable cluster; the
integration URL only unlocks collection and this isolated probe never connects
to it. Its safe receipt command is intentionally separate from the generic SQL
lane and pins the probe node explicitly:

```bash
NHMS_RUN_INTEGRATION=1 \
NHMS_INTEGRATION_DATABASE_URL=postgresql://unused:unused@127.0.0.1:1/postgres \
uv run --no-sync pytest -vv -rs -m timescaledb_210 \
  tests/test_probe_compressed_chunk_cold_tablespace.py::test_isolated_cluster_probe_is_opt_in
```

Keep the terminal report as the receipt: it must show the explicit marker
execution and identity-bound owned cleanup. Future `timescaledb_210` tests that
use database fixtures need their own explicit database/DSN contract; they must
not silently inherit this non-routable dummy-URL command.

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
uv run --no-sync python -c "import sys; assert sys.version_info[:2] == (3, 11), sys.version"
NHMS_RUN_E2E=1 NHMS_RUN_GRIB=1 uv run --no-sync pytest \
  -m "e2e or grib" -v | tee artifacts/ci-routing/e2e-grib-$(date +%F).log
```

Keep the log as the receipt (gitignored `artifacts/` is fine for evidence).
