# Production Slurm Templates

`infra/sbatch/` is the canonical production template directory for real Slurm.
`SlurmGatewaySettings.template_dir` defaults to this path.

Templates are rendered by `RealSlurmGateway` with Jinja2
`SandboxedEnvironment` and `StrictUndefined`. Template variables come from the
orchestrator manifest merged with the resolved resource profile. Request
top-level fields such as `job_type`, `cycle_id`, `stage_name`, and `tasks`
override same-named nested manifest fields before rendering.

Object store settings use lower-case manifest keys and upper-case worker
environment variables:

| manifest key | exported variable |
| --- | --- |
| `object_store_root` | `OBJECT_STORE_ROOT` |
| `object_store_prefix` | `OBJECT_STORE_PREFIX` |

Templates must export both variables so workers write durable artifacts to the
configured object store path instead of silently falling back to the workspace.

`publish_tiles.sbatch` does not render `DATABASE_URL` into script text. Slurm
runtime must provide `DATABASE_URL` through a protected execution environment so
`nhms-pipeline publish-tiles` can read it directly.

## Job Type Mapping

The authoritative mapping is `DEFAULT_JOB_TYPE_TEMPLATES` in
`services/slurm_gateway/config.py`:

| job_type | template |
| --- | --- |
| `download_source_cycle` | `download_source_cycle.sbatch` |
| `convert_canonical` | `convert_canonical.sbatch` |
| `produce_forcing_array` | `produce_forcing_array.sbatch` |
| `run_shud_forecast_array` | `run_shud_forecast_array.sbatch` |
| `parse_output_array` | `parse_output_array.sbatch` |
| `save_state_snapshot_array` | `save_state_snapshot_array.sbatch` |
| `compute_frequency_array` | `compute_frequency_array.sbatch` |
| `publish_tiles` | `publish_tiles.sbatch` |
| `analysis_download_source_cycle` | `analysis_download_source_cycle.sbatch` |
| `analysis_convert_canonical` | `analysis_convert_canonical.sbatch` |
| `analysis_produce_forcing` | `analysis_produce_forcing.sbatch` |
| `run_shud_analysis` | `run_shud_analysis.sbatch` |
| `parse_analysis_output` | `parse_analysis_output.sbatch` |
| `save_state_snapshot` | `save_state_snapshot.sbatch` |
| `hindcast` | `hindcast.sbatch` |
