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
{% for export_line in slurm_env_exports | default([]) %}
{{ export_line }}
{% endfor %}
NHMS_CYCLE_TIME="${NHMS_CYCLE_TIME:-${NHMS_CYCLE_ID:-}}"
export NHMS_CYCLE_TIME

mkdir -p "$WORKSPACE_ROOT/$NHMS_RUN_ID/logs"
nhms-canonical convert --source-id "${NHMS_SOURCE_ID:-GFS}" --cycle-time "$NHMS_CYCLE_TIME"
