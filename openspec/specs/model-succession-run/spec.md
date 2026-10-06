# model-succession-run Specification

## Purpose
TBD - created by archiving change node22-model-succession-run. Update Purpose after archive.

## Requirements

### Requirement: Compute-side succession steps run in a fixed, receipt-gated order

The node-22 model succession tool SHALL run the steps of the succession kind in a fixed order: copyback,
preflight, begin, clone, publish, refresh and finish for `recalibration`; copyback, preflight, begin, publish,
refresh and finish for `cold_start`. It SHALL write one receipt per completed step, and SHALL refuse a step
whose preceding step in that order has no successful receipt.

#### Scenario: Publish before clone

- **WHEN** the publish step of a `recalibration` succession is reached and the clone step has no step receipt
  for the succession
- **THEN** the tool exits non-zero naming the missing receipt and neither registry manifest changes

#### Scenario: Cold-start publish before begin

- **WHEN** the publish step of a `cold_start` succession is reached and the begin step has no step receipt
  for the succession
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

### Requirement: A cold-start succession skips the state clone and proves the packaged initial condition

The succession tool SHALL accept the kind `cold_start`, whose steps are `copyback`, `preflight`, `begin`,
`publish`, `refresh`, `finish`. A cold-start succession MUST NOT read or write a state index. Its `preflight`
SHALL audit the packaged initial condition of every new model of the plan and write the audit receipt once
into the succession directory; its `publish` MUST refuse unless that receipt is present, unchanged since
`preflight`, and shows every new model as qualified.

#### Scenario: Cold start with qualified initial conditions

- **WHEN** a `cold_start` succession is applied for pairs whose packages differ structurally and whose new
  packages ship a qualified initial condition
- **THEN** the steps run in the cold-start order without a clone step
- **AND** both state indexes are unchanged
- **AND** the merged registry is published and `ic-audit.json` is in the succession directory

#### Scenario: A new model's initial condition is not qualified

- **WHEN** the audit finds a new model of the plan whose `ic_status` is not qualified, or finds no row for it
- **THEN** `preflight` fails before the scheduler timer is stopped
- **AND** nothing is published

#### Scenario: The audit receipt is missing or changed at publish

- **WHEN** `publish` of a `cold_start` succession finds no `ic-audit.json`, or one whose sha256 differs from
  the one recorded by `preflight`
- **THEN** the step fails and both registry manifests are unchanged

### Requirement: The succession kind must match what changed between the packages

The succession tool SHALL compare the two packages of every pair on the eight state-compatibility surfaces
and MUST refuse a run whose kind does not match the result: `cold_start` when the surfaces of a pair are
equal, `recalibration` when they are not. The refusal SHALL name the pair and the other kind, and MUST happen
before the scheduler timer is stopped. A comparison that cannot be made MUST fail the step and MUST NOT be
treated as either result.

#### Scenario: A calibration-only change sent as a cold start

- **WHEN** a `cold_start` succession names a pair whose packages are state-compatible
- **THEN** `preflight` refuses, naming the pair and `--kind recalibration`
- **AND** the timer was not stopped and nothing was published

#### Scenario: A structural change sent as a recalibration

- **WHEN** a `recalibration` succession names a pair whose packages are not state-compatible
- **THEN** `preflight` refuses, naming the pair and `--kind cold_start`
- **AND** the timer was not stopped and nothing was published

#### Scenario: The comparison cannot be made

- **WHEN** a package of a pair is missing in the compute store or lacks a file the comparison needs
- **THEN** `preflight` fails under either kind before the timer is stopped

### Requirement: A cold-start succession records that the hydrograph is not continuous

The plan, the `publish` and `finish` step receipts and the reports of a `cold_start` succession SHALL carry a
continuity record stating that no state is carried from the old models, that the new models start from their
packaged initial condition, and the cutover time the operator declared.

#### Scenario: Continuity record of a cold start

- **WHEN** a `cold_start` succession completes
- **THEN** `plan.json`, `step-publish.json` and `step-finish.json` each carry `continuity` with mode
  `cold_start`, `state_carried` false and the declared cutover time
