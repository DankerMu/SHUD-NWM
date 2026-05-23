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

mkdir -p "$WORKSPACE_ROOT/$NHMS_RUN_ID/logs"
nhms-shud-runtime run --run-type analysis --run-id "$NHMS_RUN_ID"
