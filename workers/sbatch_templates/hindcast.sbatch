#!/bin/bash
#SBATCH --job-name=hindcast_{model_id}_{year}
#SBATCH --cpus-per-task=4
#SBATCH --mem=8G
#SBATCH --time=02:00:00
#SBATCH --array=0-{array_max}
#SBATCH --output=logs/hindcast/{model_id}_%A_%a.out
#SBATCH --error=logs/hindcast/{model_id}_%A_%a.err

set -euo pipefail

export WORKSPACE_ROOT="{workspace_root}"
mkdir -p "$WORKSPACE_ROOT/logs/hindcast"

nhms-flood hindcast-year --model-id "{model_id}" --source-id "{source_id}" --year "{year}"
