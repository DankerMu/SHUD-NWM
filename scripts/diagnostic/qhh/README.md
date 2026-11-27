# QHH Diagnostic Manifest

DIAGNOSTIC-ONLY: this directory is the manifest for root-level QHH diagnostic and reproduction scripts.
The scripts remain at their current root paths for compatibility. They are diagnostic-only assets, not production scheduler or orchestrator entrypoints.

## Run Boundary (authoritative)

The QHH diagnostic chain may only run from a **detached diagnostic worktree** with its own virtualenv.
The canonical active node-22 checkout (`/scratch/frd_muziyao/NWM`) must **fail closed**: its shared
`.venv` is owned by the online services, and bare `uv run` / `uv sync` must never touch it. All four
entrypoints (`scripts/run_qhh_continuous.py`, `scripts/run_qhh_cycle.sh`,
`scripts/run_qhh_cycle.sbatch`, `scripts/run_qhh_backend_smoke.sh`) refuse to start when their
checkout resolves to the canonical active root, and the Python runner additionally requires the
explicit detached root below. The backend-smoke shell (`run_qhh_backend_smoke.sh`) runs every direct
Python snippet/script/module through the detached root's exact `.venv/bin/python` interpreter and
keeps its `uv run nhms-*` / dynamic forcing CLIs reachable only after the detached + exact-interpreter
guard.

Copy-paste recipe (must not use bare `uv run`):

```bash
export QHH_DIAGNOSTIC_CHECKOUT=/path/to/detached/qhh-diagnostic-worktree  # never /scratch/frd_muziyao/NWM
QHH_DIAGNOSTIC_CHECKOUT="$(cd "$QHH_DIAGNOSTIC_CHECKOUT" && pwd -P)"
[[ "$QHH_DIAGNOSTIC_CHECKOUT" != "/scratch/frd_muziyao/NWM" ]] || { echo "QHH_DIAGNOSTIC_CHECKOUT must be a detached worktree, not the active root" >&2; exit 2; }
[[ -x "$QHH_DIAGNOSTIC_CHECKOUT/.venv/bin/python" ]] || { echo "missing $QHH_DIAGNOSTIC_CHECKOUT/.venv/bin/python (detached worktree has no virtualenv)" >&2; exit 2; }
"$QHH_DIAGNOSTIC_CHECKOUT/.venv/bin/python" "$QHH_DIAGNOSTIC_CHECKOUT/scripts/run_qhh_continuous.py" --once --executor slurm
```

The chain runs through the detached root's exact `.venv/bin/python` interpreter; if that interpreter
does not exist the command fails closed before any QHH chain/`uv` action can run.

## Diagnostic Entrypoints

| path | role | production status |
|---|---|---|
| `scripts/run_qhh_continuous.py` | QHH continuous diagnostic runner; dispatches local or Slurm single-cycle runs for bring-up and reproduction. | Diagnostic-only; do not wire into production scheduler/orchestrator code. |
| `scripts/run_qhh_cycle.sh` | QHH single-cycle full-chain diagnostic runner. | Diagnostic-only; do not use as the production multi-basin runner. |
| `scripts/run_qhh_cycle.sbatch` | QHH diagnostic Slurm wrapper for `scripts/run_qhh_cycle.sh`. | Diagnostic-only; not a gateway-owned production template under `infra/sbatch`. |
| `scripts/run_qhh_backend_smoke.sh` | QHH GFS backend-smoke reproduction entrypoint. Runs direct Python through the detached root's exact `.venv/bin/python`; `uv run nhms-*` / dynamic forcing CLIs only after the detached + exact-interpreter guard. | Diagnostic-only; evidence from this script is not final production readiness. |
| `scripts/create_qhh_shud_manifest.py` | Standalone QHH SHUD manifest builder used by the diagnostic chain. | Diagnostic-only; production manifests are assembled by orchestrator chain/runtime-manifest code. |

## Direct Helper Dependencies

These helpers are direct dependencies of the diagnostic entrypoints above and remain at their root paths:

| path | invoked by | diagnostic purpose |
|---|---|---|
| `scripts/apply_smoke_migrations.py` | `scripts/run_qhh_cycle.sh`, `scripts/run_qhh_backend_smoke.sh` when smoke migrations are enabled. | Local QHH smoke compatibility migrations for environments without TimescaleDB. |
| `scripts/reset_qhh_smoke_db.py` | `scripts/run_qhh_backend_smoke.sh` when `QHH_RESET_SMOKE_DB=1`. | Repeatable QHH smoke reset for QHH-related registry, forcing, run, timeseries, and QC rows. |
| `scripts/seed_qhh_forcing_stations.py` | `scripts/run_qhh_cycle.sh`, `scripts/run_qhh_backend_smoke.sh`. | Seed standard QHH forcing stations from `qhh.tsd.forc` for diagnostic forcing production. |
| `scripts/seed_qhh_shud_output_segments.py` | `scripts/run_qhh_cycle.sh`, `scripts/run_qhh_backend_smoke.sh`. | Seed QHH SHUD output river identities so parser rows align with `.sp.riv` output order. |
| `scripts/summarize_qhh_smoke_results.py` | `scripts/run_qhh_cycle.sh`, `scripts/run_qhh_backend_smoke.sh`. | Write QHH diagnostic result summary artifacts from run, timeseries, and QC rows. |
| `scripts/publish_qhh_display_products.py` (removed — absent from the tree since at least `af49ced22`; historical row) | None today: `scripts/run_qhh_cycle.sh` and `scripts/run_qhh_backend_smoke.sh` now call the `nhms-orchestrator publish-qdown` CLI at this step. | Publish QHH diagnostic display products and evidence artifacts after result parsing. |

## Out-of-Chain Helper

`scripts/seed_qhh_smoke_met_station.py` is a related QHH smoke helper, but it is not a direct dependency of the governed diagnostic entrypoints listed above. It remains a standalone diagnostic helper unless a later issue wraps, archives, or retires it with focused evidence.

## Backend Smoke On A Local Database

`scripts/run_qhh_backend_smoke.sh` can run the whole GFS chain (basin discovery and package, registry
import, download, canonical conversion, forcing, SHUD, output parse, result summary) against a
PostgreSQL that lives inside the detached worktree. `scripts/local_pg.sh` starts that database from a
runtime under `.conda-postgres-runtime/` and keeps its data, socket and logs under `.pgdata/`,
`.pgsocket/` and `.pglogs/` of the worktree; it listens on `127.0.0.1`, port `PGPORT`.

The helper's default port is 55432, which on node-27 is the production postmaster. `local_pg.sh url`
only prints the URL for the configured port; it does not check which server answers there. So the
block below sets another port and starts the smoke script only when `local_pg.sh start` succeeded.
Never run this lane with `PGPORT=55432` on node-27.

```bash
export QHH_DIAGNOSTIC_CHECKOUT=/path/to/detached/qhh-diagnostic-worktree  # never /scratch/frd_muziyao/NWM
QHH_DIAGNOSTIC_CHECKOUT="$(cd "$QHH_DIAGNOSTIC_CHECKOUT" && pwd -P)"
[[ "$QHH_DIAGNOSTIC_CHECKOUT" != "/scratch/frd_muziyao/NWM" ]] || { echo "QHH_DIAGNOSTIC_CHECKOUT must be a detached worktree, not the active root" >&2; exit 2; }
[[ -x "$QHH_DIAGNOSTIC_CHECKOUT/.venv/bin/python" ]] || { echo "missing $QHH_DIAGNOSTIC_CHECKOUT/.venv/bin/python (detached worktree has no virtualenv)" >&2; exit 2; }
cd "$QHH_DIAGNOSTIC_CHECKOUT"

export PGPORT=55440   # a free local port; never 55432 on node-27
./scripts/local_pg.sh start &&
  DATABASE_URL=$(./scripts/local_pg.sh url) \
  QHH_CYCLE_TIME=2026052100 \
  QHH_RESET_SMOKE_DB=1 \
  SHUD_TIMEOUT_SECONDS=1800 \
  ./scripts/run_qhh_backend_smoke.sh
```

Run every command of the block from the detached root, after its `cd`. `QHH_CYCLE_TIME` is the GFS
cycle to run (`YYYYMMDDHH`); `QHH_RESET_SMOKE_DB=1` clears the QHH smoke rows first, so the run can be
repeated. `./scripts/local_pg.sh status` and `./scripts/local_pg.sh stop` report on and stop the local
server. The script checks the boundary again in its own header: it resolves its root with `pwd -P`,
refuses `/scratch/frd_muziyao/NWM`, and exits 2 when the detached root has no `.venv/bin/python`.

Without `DATABASE_URL` the script records a `blocked` database gate and skips the registry import and
every stage after it; without `QHH_CYCLE_TIME` it skips download, canonical conversion, forcing and
the SHUD run. `QHH_RESET_SMOKE_DB=1` runs `scripts/reset_qhh_smoke_db.py` before the registry import.

Script defaults, each overridable through the environment:

- `QHH_RUN_ROOT=$QHH_DIAGNOSTIC_CHECKOUT/.nhms-runs/qhh-smoke` (run products and the local object root)
- `NHMS_BASINS_ROOT=data/Basins`, relative to the worktree. `data/` is not tracked, so a new worktree
  has none: set the variable to the machine's Basins source, or link it there, before the run.
- `QHH_PACKAGE_VERSION=v0.0.1-qhh-smoke-lake2`
- `QHH_GFS_FORECAST_START_HOUR=3`
- `QHH_GFS_FORECAST_END_HOUR=24`
- `QHH_MODEL_OUTPUT_INTERVAL=180`
- `QHH_SHUD_COMMAND_STYLE=shud_project`
- `QHH_USE_SMOKE_MIGRATIONS=1` (`scripts/apply_smoke_migrations.py`, for a database without TimescaleDB)
- `SHUD_EXECUTABLE=$QHH_DIAGNOSTIC_CHECKOUT/SHUD/shud`

A completed run is diagnostic and reproduction evidence for the cycle it ran. It is not a production
receipt and not proof of scheduler readiness.

## Production Replacement

Use the generic production scheduler/orchestrator path for production automation.
Dry-run planning is no-mutation evidence only; production submission must be
explicit. Before the operator-approved maintenance cutover, the active node-22
checkout's shared `.venv` must not be rebuilt, so these commands run through the
exact pre-window active interpreter
`/scratch/frd_muziyao/NWM/.venv/bin/python -m services.orchestrator.cli`
(fail-closed: the interpreter must exist or the command refuses to start; no
bare `uv run` / `uv sync` belongs on this surface):

```bash
/scratch/frd_muziyao/NWM/.venv/bin/python -m services.orchestrator.cli plan-production --dry-run --source gfs --source IFS --workspace-root "$WORKSPACE_ROOT"
/scratch/frd_muziyao/NWM/.venv/bin/python -m services.orchestrator.cli plan-production --submit --source gfs --source IFS --workspace-root "$WORKSPACE_ROOT"
/scratch/frd_muziyao/NWM/.venv/bin/python -m services.orchestrator.cli plan-production --continuous --submit --max-passes "$NHMS_SCHEDULER_MAX_PASSES"
```

Production deployment variables: see the unit-to-file table of `infra/env/README.md`.
The generic production continuous daemon is
`nhms-pipeline plan-production --continuous --submit`, which calls
`services/orchestrator/scheduler.py` `run_continuous` and submits through the
standalone Slurm gateway using `infra/sbatch`. Without `--submit`,
`plan-production` remains dry-run/no-mutation, including when `--continuous` is
present. QHH `run_qhh_*` scripts bypass that gateway-owned production path and
must remain diagnostic/reproduction assets.

## Static Guard Tests

Production isolation is enforced by:

```bash
/scratch/frd_muziyao/NWM/.venv/bin/python -m pytest -q tests/test_qhh_scripts_static.py tests/test_run_qhh_continuous.py
rg -n "run_qhh_continuous|run_qhh_cycle|run_qhh_backend_smoke|create_qhh_shud_manifest" services/orchestrator --glob '*.py'
```

The pytest guard recursively scans `services/orchestrator/**/*.py` and fails if production scheduler/orchestrator sources reference QHH diagnostic tokens. The `rg` command is expected to return no matches; exit code 1 from no matches is the expected pass condition.

Static guard tests must run through an exact interpreter — the canonical active root's
`/scratch/frd_muziyao/NWM/.venv/bin/python -m pytest`, or a detached worktree's exact
`.venv/bin/python -m pytest` — never bare `uv run` (no `uv run` may trigger the active `.venv` rebuild).

## Change Discipline

Do not move or delete the root-level diagnostic scripts in this slice. A future move must keep compatibility wrappers and update runbooks in the same change.
