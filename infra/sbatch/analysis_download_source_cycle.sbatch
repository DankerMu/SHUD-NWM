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

mkdir -p "$WORKSPACE_ROOT/$NHMS_RUN_ID/logs"
nhms-era5 download --date "{{analysis_date | default(cycle_time[:10])}}" --area "{{era5_area | default('55,70,15,140')}}"
