# 附录 D. Slurm sbatch 模板

版本：v0.1  
日期：2026-04-30

## 1. Forecast run 模板

```bash
#!/usr/bin/env bash
#SBATCH --job-name=nhms_shud_fcst
#SBATCH --partition=compute
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=32
#SBATCH --mem=128G
#SBATCH --time=06:00:00
#SBATCH --output=/work/nhms/slurm_logs/%x_%A_%a.out
#SBATCH --error=/work/nhms/slurm_logs/%x_%A_%a.err

set -euo pipefail

MANIFEST_INDEX=${1:?"manifest index file required"}
TASK_ID=${SLURM_ARRAY_TASK_ID}
MANIFEST_URI=$(sed -n "$((TASK_ID + 1))p" "$MANIFEST_INDEX")

module purge
module load shud/2.0
module load r/4.3
module load gdal

export OMP_NUM_THREADS=${SLURM_CPUS_PER_TASK}

nhms-fetch-manifest "$MANIFEST_URI" --output run_manifest.json
nhms-shud-runtime execute --manifest run_manifest.json
nhms-upload-run-status --manifest run_manifest.json --status succeeded
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
#SBATCH --output=/work/nhms/slurm_logs/%x_%A_%a.out
#SBATCH --error=/work/nhms/slurm_logs/%x_%A_%a.err

set -euo pipefail

MANIFEST_INDEX=${1:?"manifest index file required"}
TASK_ID=${SLURM_ARRAY_TASK_ID}
MANIFEST_URI=$(sed -n "$((TASK_ID + 1))p" "$MANIFEST_INDEX")

nhms-fetch-manifest "$MANIFEST_URI" --output run_manifest.json
nhms-parse shud-output --manifest run_manifest.json
```

## 3. 提交依赖示例

```bash
jid_download=$(sbatch download_source.sbatch manifests/download_gfs_2026043000.json | awk '{print $4}')
jid_convert=$(sbatch --dependency=afterok:$jid_download convert_canonical.sbatch manifests/convert_gfs_2026043000.json | awk '{print $4}')
jid_forcing=$(sbatch --dependency=afterok:$jid_convert --array=0-29%10 forcing.sbatch manifests/forcing_index.txt | awk '{print $4}')
jid_run=$(sbatch --dependency=afterok:$jid_forcing --array=0-29%6 run_shud_forecast.sbatch manifests/run_index.txt | awk '{print $4}')
jid_parse=$(sbatch --dependency=afterok:$jid_run --array=0-29%10 parse_output.sbatch manifests/run_index.txt | awk '{print $4}')
```
