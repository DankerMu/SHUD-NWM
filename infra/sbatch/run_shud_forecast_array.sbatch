#!/usr/bin/env bash
#SBATCH --job-name=nhms_{{stage_name}}
#SBATCH --partition={{partition}}
{% if account | default('') %}#SBATCH --account={{account}}
{% endif %}
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
export NHMS_RUN_ID="{{run_id}}"
export NHMS_MODEL_ID="{{model_id}}"
export NHMS_CYCLE_ID="{{cycle_id | default('')}}"
export NHMS_JOB_TYPE="{{job_type}}"
export NHMS_MANIFEST_INDEX="{{manifest_index_path}}"
export NHMS_MAX_CONCURRENT={{max_concurrent}}
export SHUD_THREADS={{shud_threads}}
export OMP_NUM_THREADS={{shud_threads}}
{% for export_line in slurm_env_exports | default([]) %}
{{ export_line }}
{% endfor %}

mkdir -p "$WORKSPACE_ROOT/$NHMS_RUN_ID/logs"
VALIDATION_EXPECTED_OUTCOME="$(python - <<'PY'
import json
import os

task_id = int(os.environ.get("SLURM_ARRAY_TASK_ID", "0"))
with open(os.environ["NHMS_MANIFEST_INDEX"], encoding="utf-8") as handle:
    tasks = json.load(handle)
for task in tasks:
    if int(task["task_id"]) == task_id:
        print(task.get("expected_outcome", ""))
        break
PY
)"
if [[ "$VALIDATION_EXPECTED_OUTCOME" == "controlled_failure" ]]; then
  echo "{{controlled_failure_log_marker | default('NHMS_PRODUCTION_SLURM_CONTROLLED_FAILURE_EXPECTED')}}"
  python - <<'PY'
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from workers.output_parser.parser import (
    HydroRunContext,
    OutputParsingError,
    RiverSegmentOrder,
    parse_rivqdown_file,
)

with TemporaryDirectory() as tmpdir:
    malformed = Path(tmpdir) / "controlled_failure.rivqdown"
    malformed.write_text("time,seg_a\n2026-05-16T00:00:00Z,NaN\n", encoding="utf-8")
    try:
        parse_rivqdown_file(
            malformed,
            HydroRunContext(
                run_id="production_closure_controlled_failure",
                model_id="validation",
                basin_version_id="basin_v1",
                river_network_version_id="rnv_v1",
                source_id="GFS",
                cycle_id="production_closure_validation",
                cycle_time=datetime(2026, 5, 16, tzinfo=UTC),
                start_time=datetime(2026, 5, 16, tzinfo=UTC),
            ),
            (RiverSegmentOrder("seg_a", "rnv_v1", 0),),
        )
    except OutputParsingError as error:
        print(f"{error.error_code}: {error.message}")
        raise SystemExit(2 if error.error_code == "NON_FINITE_FLOW" else 1) from error
raise SystemExit(1)
PY
fi
nhms-shud-runtime execute --manifest-index "$NHMS_MANIFEST_INDEX" --task-id "${SLURM_ARRAY_TASK_ID:-0}"
