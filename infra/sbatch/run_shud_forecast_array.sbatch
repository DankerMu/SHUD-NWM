#!/usr/bin/env bash
#SBATCH --job-name=nhms_{{stage_name}}
#SBATCH --partition={{partition}}
{% if exclude_nodes | default('') %}#SBATCH --exclude={{exclude_nodes}}
{% endif %}
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

{% for export_line in export_lines | default([]) %}
{{ export_line }}
{% endfor %}
{% for export_line in slurm_env_exports | default([]) %}
{{ export_line }}
{% endfor %}

mkdir -p "$WORKSPACE_ROOT/$NHMS_RUN_ID/logs"
VALIDATION_EXPECTED_OUTCOME="$(python - <<'PY'
import os

from packages.common.manifest_index import load_manifest_entry, resolve_task_id

task_id = resolve_task_id(None)
entry = load_manifest_entry(os.environ["NHMS_MANIFEST_INDEX"], task_id)
print(entry.get("expected_outcome", ""))
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
