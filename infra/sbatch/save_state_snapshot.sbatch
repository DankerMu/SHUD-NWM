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
export OBJECT_STORE_ROOT="{{object_store_root | default(workspace_dir)}}"
export OBJECT_STORE_PREFIX="{{object_store_prefix | default('')}}"
export NHMS_RUN_ID="{{run_id}}"
export NHMS_MODEL_ID="{{model_id}}"
export NHMS_SOURCE_ID="{{source_id}}"
export NHMS_CYCLE_ID="{{cycle_id}}"
export NHMS_CYCLE_TIME="{{cycle_time}}"
export NHMS_START_TIME="{{start_time}}"
export NHMS_END_TIME="{{end_time}}"
export NHMS_JOB_TYPE="{{job_type}}"
export NHMS_RUN_MANIFEST_URI="{{run_manifest_uri}}"
export NHMS_MAX_CONCURRENT={{max_concurrent}}
export SHUD_THREADS={{shud_threads}}
export OMP_NUM_THREADS={{shud_threads}}

mkdir -p "$WORKSPACE_ROOT/$NHMS_RUN_ID/logs"
nhms-state save --run-id "$NHMS_RUN_ID"
