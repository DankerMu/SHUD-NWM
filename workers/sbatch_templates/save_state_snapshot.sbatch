#!/bin/bash
#SBATCH --job-name={stage_name}_{run_id}
#SBATCH --cpus-per-task=1
#SBATCH --mem=2G
#SBATCH --time=00:15:00
#SBATCH --output=logs/{run_id}/{stage_name}.out
#SBATCH --error=logs/{run_id}/{stage_name}.err

set -euo pipefail

export WORKSPACE_ROOT="{workspace_dir}"
export OBJECT_STORE_ROOT="{object_store_root}"
export OBJECT_STORE_PREFIX="{object_store_prefix}"

mkdir -p "$WORKSPACE_ROOT/logs/{run_id}"
nhms-state save --run-id "{run_id}"
