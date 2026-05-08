#!/bin/bash
#SBATCH --job-name={stage_name}_{run_id}
#SBATCH --cpus-per-task=2
#SBATCH --mem=4G
#SBATCH --time=02:30:00
#SBATCH --output=logs/{run_id}/{stage_name}.out
#SBATCH --error=logs/{run_id}/{stage_name}.err

set -euo pipefail

export WORKSPACE_ROOT="{workspace_dir}"
export OBJECT_STORE_ROOT="{object_store_root}"
export OBJECT_STORE_PREFIX="{object_store_prefix}"

mkdir -p "$WORKSPACE_ROOT/logs/{run_id}"
nhms-era5 download --date "{analysis_date}" --area "{era5_area}"
