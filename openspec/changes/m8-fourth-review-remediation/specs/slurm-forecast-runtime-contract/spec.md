## ADDED Requirements

### Requirement: Forecast array tasks have executable runtime manifests

Every forecast array task SHALL resolve to a complete SHUD runtime manifest before the Slurm job is submitted.

#### Scenario: Manifest index entry identifies each runtime manifest

WHEN the orchestrator submits the `forecast` array stage for a cycle
THEN each active task in the manifest index MUST include a `run_id`
AND each active task MUST either include a `manifest_path` field or follow the documented fixed path `WORKSPACE_ROOT/runs/{run_id}/input/manifest.json`
AND the selected path resolution rule MUST match `nhms-shud-runtime execute --manifest-index --task-id`.

#### Scenario: Runtime manifest exists for every active basin

WHEN the runtime CLI resolves a forecast task to a manifest path
THEN that manifest file MUST exist before Slurm submission
AND the runtime manifest MUST follow the existing run manifest schema, including `run_id`, `run_type`, `scenario_id`, nested `model.model_id`, nested `model.basin_version_id`, nested `model.river_network_version_id`, `start_time`, `end_time`, and output/input URI fields
AND workspace/object-store root and prefix values MUST be supplied through the manifest-index entry, sbatch environment, or documented runtime configuration without corrupting the runtime manifest schema.

#### Scenario: Hydro run state exists before execution

WHEN a forecast array task starts
THEN the corresponding `hydro.hydro_run` record MUST exist or be created idempotently by the runtime
AND duplicate task retries MUST NOT create conflicting run records
AND the run status transitions MUST remain legal for the production enum.

#### Scenario: Missing runtime manifest fails before false success

WHEN a forecast array task cannot resolve a complete runtime manifest
THEN the worker MUST fail with a stable error code
AND the pipeline job MUST be marked failed
AND the cycle MUST NOT advance to a successful forecast or publish state.

#### Scenario: Invalid runtime manifest fails before false success

WHEN the resolved runtime manifest is unreadable, invalid JSON, or missing required schema fields
THEN the worker MUST fail with a stable error code
AND the orchestrator MUST preserve a failed/partial cycle state instead of advancing to publish.

### Requirement: Manifest index and worker CLI stay compatible

The manifest index schema and worker CLI resolution rules SHALL be tested as a single contract.

#### Scenario: Array worker contract test covers real CLI resolution

WHEN tests build a forecast array manifest index
THEN they MUST invoke the same `nhms-shud-runtime execute --manifest-index --task-id` path used by `infra/sbatch/run_shud_forecast_array.sbatch`
AND they MUST assert that the resolved runtime manifest content is complete.
