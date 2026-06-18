## ADDED Requirements

### Requirement: Array templates invoke supported worker commands
Every real Slurm array template SHALL invoke only CLI commands and arguments accepted by the installed Python entry points.

#### Scenario: Forcing array template command is parser-compatible
- **WHEN** the `produce_forcing_array` template is rendered with a valid manifest index and task id
- **THEN** the rendered `nhms-forcing` command MUST be accepted by the forcing CLI parser without unknown-option errors

#### Scenario: Runtime array template command is parser-compatible
- **WHEN** the `run_shud_forecast_array` template is rendered with a valid manifest index and task id
- **THEN** the rendered `nhms-shud-runtime` command MUST be accepted by the runtime CLI parser without unknown-option errors

#### Scenario: Parser array template command is parser-compatible
- **WHEN** the `parse_output_array` template is rendered with a valid manifest index and task id
- **THEN** the rendered `nhms-parse` command MUST be accepted by the output parser CLI parser without unknown-option errors

### Requirement: Manifest index entries drive per-task execution
Array workers SHALL derive the task-specific model, run, source, cycle, and workspace fields from the manifest index entry selected by `SLURM_ARRAY_TASK_ID` or an explicit `--task-id`.

#### Scenario: Manifest entries expose required fields
- **WHEN** an array worker validates a manifest index entry
- **THEN** the entry MUST include `task_id`, `model_id`, `basin_version_id`, `river_network_version_id`, `run_id`, `source_id`, `cycle_time`, `workspace_dir`, and stage-specific input/output fields before work begins

#### Scenario: Explicit task id overrides Slurm environment
- **WHEN** both `SLURM_ARRAY_TASK_ID` and `--task-id` are provided
- **THEN** the documented precedence rule MUST select one task id deterministically and the worker MUST report that choice in validation output or logs

#### Scenario: Array task ids are zero-based
- **WHEN** the manifest contains entries indexed from zero
- **THEN** task id `0` MUST select the first entry and task id `1` MUST select the second entry

#### Scenario: Different array tasks execute different runs
- **WHEN** two manifest index entries have different `run_id` and `model_id` values
- **THEN** task 0 and task 1 MUST invoke downstream worker logic with their own entry values, not a shared run or model

#### Scenario: Missing required manifest field is rejected before work begins
- **WHEN** the selected manifest entry lacks a required field
- **THEN** the worker MUST fail with a structured validation error and MUST NOT write partial output

#### Scenario: Missing task entry is rejected before work begins
- **WHEN** an array worker receives a task id outside the manifest index range
- **THEN** the worker MUST fail with a structured validation error and MUST NOT write partial output

### Requirement: Publish stage has an executable entrypoint
The publish stage SHALL call an implemented command or service method that can publish products for a cycle or explicitly mark publication as unsupported.

#### Scenario: Publish template command exists
- **WHEN** the `publish_tiles` template is rendered
- **THEN** the command it invokes MUST exist in the installed entry points or the stage MUST be disabled with a documented terminal status

### Requirement: Real-template smoke tests cover mock blind spots
The test suite SHALL include smoke tests that render real Slurm templates and validate worker CLI compatibility without depending on the mock Slurm backend executing scripts.

#### Scenario: Mock backend does not hide CLI drift
- **WHEN** worker CLI arguments change
- **THEN** a template/CLI smoke test MUST fail if any real template still calls the old argument contract
