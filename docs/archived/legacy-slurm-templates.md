# Archived Legacy Slurm Templates

Archived: 2026-06-09

Retired source path: `workers/sbatch_templates`

This archive preserves the legacy template names and migration notes from the removed active-tree directory. The legacy files were single-run examples, not the production Slurm gateway template set.

## Current Ownership

Production Slurm submissions use `infra/sbatch` through `services/slurm_gateway/config.py`.
`SlurmGatewaySettings.template_dir` defaults to `infra/sbatch`, and `DEFAULT_JOB_TYPE_TEMPLATES` owns the job-type-to-template mapping. Do not recreate `workers/sbatch_templates` as an active worker path.

## Legacy Template Migration

| legacy template name | active replacement | migration note |
|---|---|---|
| `convert_canonical.sbatch` | `infra/sbatch/convert_canonical.sbatch` | Forecast canonical conversion now renders gateway-owned templates with source and cycle values from the Slurm request manifest. |
| `convert_canonical_era5.sbatch` | `infra/sbatch/analysis_convert_canonical.sbatch` | ERA5 analysis conversion uses the analysis canonical job type; forecast canonical conversion uses `convert_canonical.sbatch` with manifest source identity. |
| `download_era5.sbatch` | `infra/sbatch/analysis_download_source_cycle.sbatch` | ERA5 download is handled by the analysis source-cycle job type. |
| `download_gfs.sbatch` | `infra/sbatch/download_source_cycle.sbatch` | Forecast source-cycle download is rendered through the canonical gateway template. |
| `frequency.sbatch` | `infra/sbatch/compute_frequency_array.sbatch` | Return-period computation is array-capable in production. |
| `hindcast.sbatch` | `infra/sbatch/hindcast.sbatch` | Hindcast remains an active production job type under `infra/sbatch`. |
| `parse_analysis_output.sbatch` | `infra/sbatch/parse_analysis_output.sbatch` | Analysis output parsing remains a single production job type under `infra/sbatch`. |
| `parse_output.sbatch` | `infra/sbatch/parse_output_array.sbatch` | Forecast output parsing is array-capable in production. |
| `produce_forcing.sbatch` | `infra/sbatch/produce_forcing_array.sbatch` | Forecast forcing production is array-capable in production. |
| `produce_forcing_analysis.sbatch` | `infra/sbatch/analysis_produce_forcing.sbatch` | Analysis forcing production uses the analysis job type under `infra/sbatch`. |
| `run_shud_analysis.sbatch` | `infra/sbatch/run_shud_analysis.sbatch` | Analysis SHUD execution remains a single production job type under `infra/sbatch`. |
| `run_shud_forecast.sbatch` | `infra/sbatch/run_shud_forecast_array.sbatch` | Forecast SHUD execution is array-capable in production. |
| `save_state_snapshot.sbatch` | `infra/sbatch/save_state_snapshot.sbatch`; `infra/sbatch/save_state_snapshot_array.sbatch` | Analysis state save uses the single job type; forecast state save uses the array-capable job type. |

## Compatibility Boundary

Legacy rendered `script` payloads are historical compatibility material only. Production real Slurm submissions render gateway-owned templates by `job_type`; unsupported legacy job types are rejected before Slurm submission.
