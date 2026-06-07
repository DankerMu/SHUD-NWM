# CI Test Routing: e2e / grib markers (node-22)

## Why

The pure-CI `unit-test` job (`.github/workflows/ci.yml`) runs the backend pytest
suite on a plain GitHub runner installed via `pip install -e ".[dev]"`. That
environment has **no real PostgreSQL/Slurm/SHUD, no cwd `.venv`, and no
eccodes-version-matched GRIB fixtures**. A handful of tests are coupled to those
environment facts and cannot pass in pure CI; they belong on the **node-22**
(`compute_control`) oracle.

To keep CI honest (no false reds from environment coupling) these tests are
tagged and excluded from the pure-CI gate, then run explicitly on node-22.

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

## node-22 run convention (produce a receipt)

Run periodically on **node-22, outside production windows**:

```bash
ssh -p 32099 frd_muziyao@210.77.77.22
cd /scratch/frd_muziyao/NWM
git pull --ff-only
NHMS_RUN_E2E=1 NHMS_RUN_GRIB=1 uv run pytest -m "e2e or grib" -v | tee artifacts/ci-routing/e2e-grib-$(date +%F).log
```

Keep the log as the receipt (gitignored `artifacts/` is fine for evidence).
