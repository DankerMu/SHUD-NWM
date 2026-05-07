#!/bin/bash
#SBATCH --job-name=nhms_shud_fcst
#SBATCH --output=logs/nhms_shud_fcst_%j.out
#SBATCH --error=logs/nhms_shud_fcst_%j.err
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=${SHUD_THREADS:-8}
#SBATCH --time=${SHUD_WALLTIME:-03:00:00}

set -euo pipefail

MANIFEST="${1:-${RUN_MANIFEST:-run_manifest.json}}"
nhms-shud-runtime execute --manifest "${MANIFEST}"
