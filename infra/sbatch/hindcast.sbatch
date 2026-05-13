#!/usr/bin/env bash
#SBATCH --job-name=nhms_{{stage_name}}
#SBATCH --partition={{partition}}
#SBATCH --nodes={{nodes}}
#SBATCH --ntasks={{ntasks}}
#SBATCH --cpus-per-task={{cpus_per_task}}
#SBATCH --mem={{memory_gb}}G
#SBATCH --time={{walltime}}
#SBATCH --output={{workspace_dir}}/{{run_id}}/logs/%A_%a.out
#SBATCH --error={{workspace_dir}}/{{run_id}}/logs/%A_%a.err

set -euo pipefail

export WORKSPACE_ROOT="{{workspace_dir}}"
export OBJECT_STORE_ROOT="{{object_store_root | default(workspace_dir)}}"
export OBJECT_STORE_PREFIX="{{object_store_prefix | default('')}}"
export NHMS_MANIFEST_INDEX="{{manifest_index_path}}"

TASK_JSON="$(python - <<'PY'
import json
import os

task_id = int(os.environ.get("SLURM_ARRAY_TASK_ID", "0"))
with open(os.environ["NHMS_MANIFEST_INDEX"], encoding="utf-8") as handle:
    tasks = json.load(handle)
print(json.dumps(tasks[task_id]))
PY
)"
export TASK_JSON

MODEL_ID="$(python -c 'import json, os; print(json.loads(os.environ["TASK_JSON"])["model_id"])')"
SOURCE_ID="$(python -c 'import json, os; print(json.loads(os.environ["TASK_JSON"])["source_id"])')"
YEAR="$(python -c 'import json, os; print(json.loads(os.environ["TASK_JSON"])["year"])')"

nhms-flood hindcast-year --model-id "$MODEL_ID" --source-id "$SOURCE_ID" --year "$YEAR"
