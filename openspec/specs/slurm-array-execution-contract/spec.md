# slurm-array-execution-contract Specification

## Purpose
TBD - created by archiving change m6-system-hardening-alignment. Update Purpose after archive.
## Requirements
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

The test suite SHALL include smoke tests that render real Slurm
templates and validate worker CLI compatibility without depending on
the mock Slurm backend executing scripts. The smoke tests SHALL judge
the rendered command text after folding line continuations — any
horizontal whitespace before the backslash, the backslash at
end-of-line, the newline, and the next line's leading spaces or tabs,
folded together to a single space — and nothing else. That
fold matches bash for continuations at token boundaries in unquoted
command text, which is the only place the templates use them; it is
deliberately NOT a full bash-continuation emulator (bash splices with
nothing and preserves quoted leading whitespace, and performs no
continuation inside single quotes or quoted heredocs — the fold is
applied only to command-form judgments, never to heredoc or export
assertions). It grants no other tolerance, so a template may lay its
command out across continued lines without disarming the guard, while
any change to the command's tokens still fails, and a continuation
followed by a blank line stays two separate commands exactly as bash
executes it. Command extraction
SHALL recover the full invocation including the executable name
(`c5496c07` reformatted three templates' commands as continued lines
and the last-line extraction silently lost the executable, leaving the
parser-compatibility guard non-functional for those templates while
its tests were red for an unrelated-looking verbatim mismatch). For
every template that branches on `target_python_runtime`, the smoke
tests SHALL assert both branches at argument level — the full
console-entrypoint command when the runtime is unset, and when it is
set the full `"$NHMS_TARGET_PYTHON_RUNTIME" -m <module> <command>
--manifest-index …` invocation, with the per-template module as the
templates render it (`workers.forcing_producer.cli` /
`workers.shud_runtime.cli` / `packages.common.state_cli` — the state
template's module is not under `workers.`). Prefix-level presence
checks alone do not satisfy this: the pre-existing gateway tests
already assert prefixes for both branches, and the residual this
closes is the unasserted subcommand and arguments.

#### Scenario: Mock backend does not hide CLI drift

- **WHEN** worker CLI arguments change
- **THEN** a template/CLI smoke test MUST fail if any real template
  still calls the old argument contract

#### Scenario: Line-continued commands are judged whole

- **WHEN** a template lays its worker command out with backslash
  continuations at token boundaries in unquoted command text
- **THEN** the smoke test MUST extract the same argv as the canonical
  single-line expected command — executable name included — and MUST
  still execute the worker CLI parser against it

#### Scenario: Continuation tolerance does not weaken discrimination

- **WHEN** a rendered command carries a wrong executable token or a
  wrong option name, in continued or single-line layout
- **THEN** the smoke test MUST fail — the normalization folds only
  backslash-newline sequences and grants no other tolerance

#### Scenario: Both target_python_runtime branches are covered at argument level

- **WHEN** a template branches its worker command on
  `target_python_runtime`
- **THEN** the smoke tests MUST assert each branch's full rendered
  command including subcommand and arguments — prefix-level presence
  of the runtime or entrypoint alone does not satisfy this

