#!/usr/bin/env bash
#SBATCH --job-name=nhms_{{stage_name}}
#SBATCH --partition={{partition}}
#SBATCH --nodes={{nodes}}
#SBATCH --ntasks={{ntasks}}
#SBATCH --cpus-per-task={{cpus_per_task}}
#SBATCH --mem={{memory_gb}}G
#SBATCH --time={{walltime}}
#SBATCH --output={{workspace_dir}}/{{run_id}}/logs/%A.out
#SBATCH --error={{workspace_dir}}/{{run_id}}/logs/%A.err

set -euo pipefail

{% for export_line in export_lines | default([]) %}
{{ export_line }}
{% endfor %}

# Minimal self-contained smoke job: no DB / SHUD / forcing dependency.
# Logs land under the workspace root (never the system disk).
mkdir -p "$WORKSPACE_ROOT/$NHMS_RUN_ID/logs"

echo "smoke job_type=$NHMS_JOB_TYPE run_id=$NHMS_RUN_ID model_id=$NHMS_MODEL_ID"
echo "smoke sleeping ${SMOKE_SLEEP_SECONDS:-5}s"
sleep "${SMOKE_SLEEP_SECONDS:-5}"
echo "smoke done run_id=$NHMS_RUN_ID"
