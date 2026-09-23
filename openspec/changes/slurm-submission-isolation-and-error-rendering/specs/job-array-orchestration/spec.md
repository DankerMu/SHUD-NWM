# Job array orchestration — submission-scoped live inputs

## MODIFIED Requirements

### Requirement: Manifest index file

The orchestrator SHALL generate a manifest index file for each array stage. The file maps each array task_id to its basin-specific parameters.

A lane whose live runtime inputs are keyed by a configured run identity — the production-closure validation lane's live submit — SHALL make those inputs submission-scoped, so that two submissions made with the same configured identity never write, overwrite or delete the same file. This SHALL cover the index file and every runtime manifest that lane's index names, and SHALL hold without depending on a lock, a scheduler query, or an operator flag: the submission's identity SHALL be claimed by an exclusive create before any file that identity names is written. A cleanup that runs after a failed submission SHALL remove only the paths that submission itself wrote, never a path derived from the configured identity alone. The gateway's own timestamped, exclusively created stage index is the existing precedent for this rule, not a surface this requirement newly constrains.

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

#### Scenario: Two concurrent live validation submissions with the same configured run_id stay disjoint

- **WHEN** the production-closure validation lane submits twice for the same configured `run_id` against the same workspace, including with the overwrite flag set
- **THEN** each submission SHALL own a distinct index path, distinct task run identities and therefore distinct runtime manifest paths and array log directory
- **THEN** neither submission SHALL overwrite a file the other wrote, and the runtime manifest each submitted task resolves SHALL be the one its own submission wrote

#### Scenario: A failed submission's cleanup leaves the other submission's inputs intact

- **WHEN** one live validation submission has already submitted successfully and a second submission for the same configured `run_id` fails at `sbatch`
- **THEN** the second submission's cleanup SHALL remove only the paths it wrote
- **THEN** the first submission's index and runtime manifests SHALL remain byte-identical and readable

#### Scenario: The dry-run lane keeps its stable evidence layout

- **WHEN** the validation lane runs without live submission (dry run or fake Slurm)
- **THEN** its evidence paths and runtime identities SHALL be unchanged, because nothing is shared and there is nothing to isolate
