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

from packages.common.manifest_index import load_manifest_entry, resolve_task_id

task_id = resolve_task_id(None)
entry = load_manifest_entry(os.environ["NHMS_MANIFEST_INDEX"], task_id)
print(json.dumps(entry, sort_keys=True))
PY
)"
export TASK_JSON

MODEL_ID="$(python -c 'import json, os; print(json.loads(os.environ["TASK_JSON"])["model_id"])')"
SOURCE_ID="$(python -c 'import json, os; print(json.loads(os.environ["TASK_JSON"])["source_id"])')"
YEAR="$(python -c 'import json, os; print(json.loads(os.environ["TASK_JSON"])["year"])')"
RUN_ID="$(python -c 'import json, os; print(json.loads(os.environ["TASK_JSON"])["run_id"])')"
FORCING_VERSION_ID="$(python -c 'import json, os; print(json.loads(os.environ["TASK_JSON"])["forcing_version_id"])')"
FORCING_PACKAGE_URI="$(python -c 'import json, os; print(json.loads(os.environ["TASK_JSON"]).get("forcing_package_uri", ""))')"
export NHMS_RUN_ID="$RUN_ID"
export NHMS_MODEL_ID="$MODEL_ID"
export NHMS_SOURCE_ID="$SOURCE_ID"
export NHMS_YEAR="$YEAR"
export NHMS_FORCING_VERSION_ID="$FORCING_VERSION_ID"
export NHMS_FORCING_PACKAGE_URI="$FORCING_PACKAGE_URI"

nhms-flood hindcast-year --model-id "$MODEL_ID" --source-id "$SOURCE_ID" --year "$YEAR"
