#!/usr/bin/env bash
#SBATCH --job-name=nhms_{{stage_name}}
#SBATCH --partition={{partition}}
{% if exclude_nodes | default('') %}#SBATCH --exclude={{exclude_nodes}}
{% endif %}
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
SOURCE_ID="${NHMS_SOURCE_ID:-GFS}"
if [ "${SOURCE_ID^^}" = "IFS" ]; then
    nhms-ifs download --cycle-time "$NHMS_CYCLE_TIME"
else
    nhms-gfs download --cycle-time "$NHMS_CYCLE_TIME"
fi
