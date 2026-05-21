#!/usr/bin/env bash
#SBATCH --job-name=qhh_cycle
#SBATCH --partition=CPU
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=8
#SBATCH --mem=128G
#SBATCH --time=08:00:00

set -euo pipefail

ROOT_DIR="${QHH_REPO_ROOT:-/scratch/frd_muziyao/NWM}"
cd "$ROOT_DIR"

mkdir -p "${QHH_RUN_ROOT:-$ROOT_DIR/.nhms-runs/qhh-continuous}/slurm-logs"

export OMP_NUM_THREADS="${SLURM_CPUS_PER_TASK:-8}"
export SHUD_THREADS="${SLURM_CPUS_PER_TASK:-8}"
export QHH_AUTO_START_PG=0
if [[ -f "$ROOT_DIR/.conda-postgres-runtime/lib/libstdc++.so.6" ]]; then
  export LD_PRELOAD="$ROOT_DIR/.conda-postgres-runtime/lib/libstdc++.so.6${LD_PRELOAD:+:$LD_PRELOAD}"
fi
ECCODES_RUNTIME="${QHH_ECCODES_RUNTIME:-${QHH_RUN_ROOT:-$ROOT_DIR/.nhms-runs/qhh-continuous}/eccodes-runtime}"
if [[ -d "$ECCODES_RUNTIME/lib" ]]; then
  export LD_LIBRARY_PATH="$ECCODES_RUNTIME/lib:$ROOT_DIR/.conda-postgres-runtime/lib:${LD_LIBRARY_PATH:-}"
  export ECCODES_DIR="$ECCODES_RUNTIME"
fi
if [[ -d "$ECCODES_RUNTIME/share/eccodes/definitions" ]]; then
  export ECCODES_DEFINITION_PATH="$ECCODES_RUNTIME/share/eccodes/definitions"
fi

exec "$ROOT_DIR/scripts/run_qhh_cycle.sh" "$QHH_SOURCE_ID" "$QHH_CYCLE_TIME"
