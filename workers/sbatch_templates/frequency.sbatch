#!/bin/bash
#SBATCH --job-name=frequency_{run_id}
#SBATCH --cpus-per-task=1
#SBATCH --mem=2G
#SBATCH --time=00:20:00
#SBATCH --dependency=afterok:{parse_job_id}
#SBATCH --output=logs/{run_id}/frequency.out
#SBATCH --error=logs/{run_id}/frequency.err

set -euo pipefail

export WORKSPACE_ROOT="{workspace_dir}"
export OBJECT_STORE_ROOT="{object_store_root}"
export OBJECT_STORE_PREFIX="{object_store_prefix}"

mkdir -p "$WORKSPACE_ROOT/logs/{run_id}"
nhms-flood compute-return-period --run-id "{run_id}"
