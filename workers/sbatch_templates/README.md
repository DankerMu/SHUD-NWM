# Legacy Slurm Templates

`workers/sbatch_templates/` contains legacy single-run Slurm examples. These files
are retained for reference and compatibility checks, but they are **not** used by
M3+ array orchestration or the production real Slurm gateway path.

The canonical production template directory for real Slurm is `infra/sbatch/`.
`SlurmGatewaySettings.template_dir` defaults to `infra/sbatch`, and
`services/slurm_gateway/config.py` owns the authoritative
`DEFAULT_JOB_TYPE_TEMPLATES` mapping.

## Production Template Ownership

Real Slurm submissions must use gateway-owned templates from `infra/sbatch/`.
The gateway renders those templates with request manifest fields merged with the
resolved resource profile. M3+ array orchestration submits `job_type`, `cycle_id`,
`stage_name`, `tasks`, and a nested manifest to the gateway; it does not execute
templates from this directory.

## Job Type Mapping

The production mapping is:

| job_type | production template |
| --- | --- |
| `download_source_cycle` | `infra/sbatch/download_source_cycle.sbatch` |
| `convert_canonical` | `infra/sbatch/convert_canonical.sbatch` |
| `produce_forcing_array` | `infra/sbatch/produce_forcing_array.sbatch` |
| `run_shud_forecast_array` | `infra/sbatch/run_shud_forecast_array.sbatch` |
| `parse_output_array` | `infra/sbatch/parse_output_array.sbatch` |
| `compute_frequency_array` | `infra/sbatch/compute_frequency_array.sbatch` |
| `publish_tiles` | `infra/sbatch/publish_tiles.sbatch` |
| `hindcast` | `infra/sbatch/hindcast.sbatch` |

Unsupported legacy `job_type` values are rejected by the real gateway before
Slurm submission.
