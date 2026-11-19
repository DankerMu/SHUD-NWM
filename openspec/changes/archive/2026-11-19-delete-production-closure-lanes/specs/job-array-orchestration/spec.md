## REMOVED Requirements

### Requirement: Manifest index file

**Reason**: Replaced by "Array stage manifest index file": the production-closure Slurm validation (`validate-slurm`) live-submit paragraph and its three scenarios of this requirement are cut (#3221), and OpenSpec refuses a MODIFIED block that drops a scenario, so the surviving text moves to a new header.
**Migration**: See the ADDED requirement "Array stage manifest index file"; apart from those cuts its body and scenarios are unchanged.

## ADDED Requirements

### Requirement: Array stage manifest index file

The orchestrator SHALL generate a manifest index file for each array stage. The file maps each array task_id to its basin-specific parameters.

#### Scenario: Manifest index file is generated before array submission

- **WHEN** the orchestrator prepares an array stage for N basins
- **THEN** it MUST write a JSON file at `workspace/{cycle_id}/manifests/{stage_name}_index.json`
- **THEN** the file MUST contain a JSON array of N objects, each with at minimum: `task_id`, `model_id`, `basin_version_id`, `run_id`, `workspace_dir`
- **THEN** the `task_id` field MUST equal the array index (0 to N-1)

#### Scenario: Manifest index includes all registered basins for the cycle

- **WHEN** 10 basins are registered for the forecast cycle
- **THEN** the manifest index MUST contain exactly 10 entries
- **THEN** each entry MUST reference a valid `model_id` and `basin_version_id` from the basin registry

#### Scenario: Manifest index is immutable after submission

- **WHEN** an array job has been submitted with a manifest index file
- **THEN** the orchestrator MUST NOT modify the manifest index file while the array job is running
- **THEN** any re-submission (e.g., retry) MUST generate a new manifest index file with a versioned filename
