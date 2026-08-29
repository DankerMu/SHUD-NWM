# 附录 D. Slurm sbatch 模板

版本：v0.2  
日期：2026-05-06

生产提交由 Slurm gateway 按 `job_type` 渲染 `infra/sbatch` 下的模板；
权威映射以 `services/slurm_gateway/config.py` 的
`DEFAULT_JOB_TYPE_TEMPLATES` 为准。

四个生产 array 模板（`produce_forcing_array`、`run_shud_forecast_array`、
`parse_output_array`、`save_state_snapshot_array`）把 `%A_%a` stdout/stderr
写到 cohort-neutral 目录 `{{workspace_dir}}/{{cycle_id}}/array_logs/<index-stem>/`。
该目录由 immutable manifest-index 文件名派生，gateway 在调用 `sbatch` 前安全创建；
路径不含任何 member `run_id`。历史 `workspace/<run_id>/logs/%A_%a` 布局只作为只读回退。
非 array 模板与任务产物路径不变。

## 1. Forecast array 模板

```bash
#!/usr/bin/env bash
#SBATCH --job-name=nhms_shud_fcst
#SBATCH --partition=compute
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=32
#SBATCH --mem=128G
#SBATCH --time=06:00:00
#SBATCH --output={{workspace_dir}}/{{cycle_id}}/array_logs/<index-stem>/%A_%a.out
#SBATCH --error={{workspace_dir}}/{{cycle_id}}/array_logs/<index-stem>/%A_%a.err

set -euo pipefail

MANIFEST_INDEX=${NHMS_MANIFEST_INDEX:?"manifest index file required"}
TASK_ID=${SLURM_ARRAY_TASK_ID:-0}

module purge
module load shud/2.0
module load r/4.3
module load gdal

export OMP_NUM_THREADS=${SLURM_CPUS_PER_TASK}

nhms-shud-runtime execute --manifest-index "$MANIFEST_INDEX" --task-id "$TASK_ID"
```

## 2. Parser 模板

```bash
#!/usr/bin/env bash
#SBATCH --job-name=nhms_parse
#SBATCH --partition=compute
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=8
#SBATCH --mem=32G
#SBATCH --time=02:00:00
#SBATCH --output={{workspace_dir}}/{{cycle_id}}/array_logs/<index-stem>/%A_%a.out
#SBATCH --error={{workspace_dir}}/{{cycle_id}}/array_logs/<index-stem>/%A_%a.err

set -euo pipefail

MANIFEST_INDEX=${NHMS_MANIFEST_INDEX:?"manifest index file required"}
TASK_ID=${SLURM_ARRAY_TASK_ID:-0}

nhms-parse shud-output --manifest-index "$MANIFEST_INDEX" --task-id "$TASK_ID"
```

## 3. 提交依赖示例

```text
convert_canonical             -> infra/sbatch/convert_canonical.sbatch
produce_forcing_array         -> infra/sbatch/produce_forcing_array.sbatch
run_shud_forecast_array       -> infra/sbatch/run_shud_forecast_array.sbatch
parse_output_array            -> infra/sbatch/parse_output_array.sbatch
save_state_snapshot_array     -> infra/sbatch/save_state_snapshot_array.sbatch
```
