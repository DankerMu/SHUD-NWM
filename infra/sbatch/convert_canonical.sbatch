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

export WORKSPACE_ROOT="{{workspace_dir}}"
export NHMS_RUN_ID="{{run_id}}"
export NHMS_MODEL_ID="{{model_id}}"
export NHMS_SOURCE_ID="{{source_id | default('GFS')}}"
export NHMS_CYCLE_ID="{{cycle_id | default('')}}"
export NHMS_CYCLE_TIME="{{cycle_time}}"
export NHMS_JOB_TYPE="{{job_type}}"
export NHMS_MAX_CONCURRENT={{max_concurrent}}
export SHUD_THREADS={{shud_threads}}
export OMP_NUM_THREADS={{shud_threads}}

mkdir -p "$WORKSPACE_ROOT/$NHMS_RUN_ID/logs"
nhms-canonical convert --source-id "${NHMS_SOURCE_ID:-GFS}" --cycle-time "$NHMS_CYCLE_TIME"
