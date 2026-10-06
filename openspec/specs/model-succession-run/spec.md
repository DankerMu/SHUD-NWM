# model-succession-run Specification

## Purpose
TBD - created by archiving change node22-model-succession-run. Update Purpose after archive.

## Requirements

### Requirement: Compute-side succession steps run in a fixed, receipt-gated order

The node-22 model succession tool SHALL run copyback, preflight, begin, clone, publish, refresh and finish in
that order,
SHALL write one receipt per completed step, and SHALL refuse a step whose preceding step has no successful
receipt.

#### Scenario: Publish before clone

- **WHEN** the publish step is reached and the clone step has no step receipt for the succession
- **THEN** the tool exits non-zero naming the missing receipt and neither registry manifest changes

#### Scenario: Resume after a failure

- **WHEN** the same command is run again after the cause of a failed step was removed
- **THEN** the steps that already have a receipt are skipped and the remaining steps run

### Requirement: The scheduler timer stays stopped until the succession finishes

The tool SHALL stop the scheduler timer in the begin step, SHALL NOT start it when any step fails, and SHALL
start it only in the finish step or on an explicitly confirmed abort, and only when it was active at begin.

#### Scenario: A step fails

- **WHEN** any step of an apply fails
- **THEN** the timer is left stopped, the exit status is non-zero, and a failure receipt states that the timer is stopped and how to resume or abort

#### Scenario: Scheduler started by someone else

- **WHEN** the timer or the scheduler service is found running before the clone, publish, refresh or finish step
- **THEN** that step writes nothing and the tool refuses

### Requirement: A succession dry-run changes nothing

Without `--apply` the tool SHALL change no file, write no receipt and issue only read-only unit queries.

#### Scenario: Dry-run

- **WHEN** the tool runs without `--apply`
- **THEN** no file under the receipt root, either object store or the state indexes changes, and no unit is started or stopped
