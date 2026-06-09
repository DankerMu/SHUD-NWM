# QHH Diagnostic Manifest

DIAGNOSTIC-ONLY: this directory is the manifest for root-level QHH diagnostic and reproduction scripts.
The scripts remain at their current root paths for compatibility. They are diagnostic-only assets, not production scheduler or orchestrator entrypoints.

## Diagnostic Entrypoints

| path | role | production status |
|---|---|---|
| `scripts/run_qhh_continuous.py` | QHH continuous diagnostic runner; dispatches local or Slurm single-cycle runs for bring-up and reproduction. | Diagnostic-only; do not wire into production scheduler/orchestrator code. |
| `scripts/run_qhh_cycle.sh` | QHH single-cycle full-chain diagnostic runner. | Diagnostic-only; do not use as the production multi-basin runner. |
| `scripts/run_qhh_cycle.sbatch` | QHH diagnostic Slurm wrapper for `scripts/run_qhh_cycle.sh`. | Diagnostic-only; not a gateway-owned production template under `infra/sbatch`. |
| `scripts/run_qhh_backend_smoke.sh` | QHH GFS backend-smoke reproduction entrypoint. | Diagnostic-only; evidence from this script is not final production readiness. |
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
| `scripts/publish_qhh_display_products.py` | `scripts/run_qhh_cycle.sh`, `scripts/run_qhh_backend_smoke.sh`. | Publish QHH diagnostic display products and evidence artifacts after result parsing. |

## Out-of-Chain Helper

`scripts/seed_qhh_smoke_met_station.py` is a related QHH smoke helper, but it is not a direct dependency of the governed diagnostic entrypoints listed above. It remains a standalone diagnostic helper unless a later issue wraps, archives, or retires it with focused evidence.

## Production Replacement

Use the generic production scheduler/orchestrator path for production automation.
Dry-run planning is no-mutation evidence only; production submission must be
explicit:

```bash
uv run nhms-pipeline plan-production --dry-run --source gfs --source IFS --workspace-root "$WORKSPACE_ROOT"
uv run nhms-pipeline plan-production --submit --source gfs --source IFS --workspace-root "$WORKSPACE_ROOT"
uv run nhms-pipeline plan-production --continuous --submit --max-passes "$NHMS_SCHEDULER_MAX_PASSES"
```

Production deployment variables live in `infra/env/compute.example`.
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
uv run pytest -q tests/test_qhh_scripts_static.py
rg -n "run_qhh_continuous|run_qhh_cycle|run_qhh_backend_smoke|create_qhh_shud_manifest" services/orchestrator --glob '*.py'
```

The pytest guard recursively scans `services/orchestrator/**/*.py` and fails if production scheduler/orchestrator sources reference QHH diagnostic tokens. The `rg` command is expected to return no matches; exit code 1 from no matches is the expected pass condition.

## Change Discipline

Do not move or delete the root-level diagnostic scripts in this slice. A future move must keep compatibility wrappers and update runbooks in the same change.
