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
#SBATCH --output={{workspace_dir}}/{{run_id}}/logs/%A_%a.out
#SBATCH --error={{workspace_dir}}/{{run_id}}/logs/%A_%a.err

set -euo pipefail

{% for export_line in export_lines | default([]) %}
{{ export_line }}
{% endfor %}
{% for export_line in slurm_env_exports | default([]) %}
{{ export_line }}
{% endfor %}

{% if target_python_runtime | default('') %}
cd "$NHMS_TARGET_PYTHON_SOURCE_ROOT"
{% endif %}
mkdir -p "$WORKSPACE_ROOT/$NHMS_RUN_ID/logs"
{% if target_python_runtime | default('') %}
"$NHMS_TARGET_PYTHON_RUNTIME" -m workers.forcing_producer.cli produce \
  --manifest-index "$NHMS_MANIFEST_INDEX" --task-id "${SLURM_ARRAY_TASK_ID:-0}"
{% else %}
nhms-forcing produce \
  --manifest-index "$NHMS_MANIFEST_INDEX" --task-id "${SLURM_ARRAY_TASK_ID:-0}"
{% endif %}
