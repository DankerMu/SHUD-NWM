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

So pure CI never collects e2e/grib/integration tests. `real-db-integration`
still runs `-m integration` against its TimescaleDB service.

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
