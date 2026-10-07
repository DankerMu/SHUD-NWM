# model-succession-run Specification

## Purpose

The node-22 command that carries a model succession through its compute-side steps after the node-27
provision step: copying the new packages to the compute store, stopping the scheduler timer, carrying or
deliberately not carrying state, publishing the merged scheduler registry, refreshing the providers and
starting the timer again, each step gated on the receipt of the one before it.

## Requirements

### Requirement: Compute-side succession steps run in a fixed, receipt-gated order

The node-22 model succession tool SHALL run the steps of the succession kind in a fixed order: copyback,
preflight, begin, clone, publish, refresh and finish for `recalibration`; copyback, preflight, begin, publish,
refresh and finish for `cold_start` and for `add_basin`; preflight, begin, publish, refresh and finish for
`remove_basin`. It SHALL write one receipt per completed step, and SHALL refuse a step whose preceding step
in that order has no successful receipt.

#### Scenario: Publish before clone

- **WHEN** the publish step of a `recalibration` succession is reached and the clone step has no step receipt
  for the succession
- **THEN** the tool exits non-zero naming the missing receipt and neither registry manifest changes

#### Scenario: Cold-start publish before begin

- **WHEN** the publish step of a `cold_start` succession is reached and the begin step has no step receipt
  for the succession
- **THEN** the tool exits non-zero naming the missing receipt and neither registry manifest changes

#### Scenario: Add-basin publish before begin

- **WHEN** the publish step of an `add_basin` succession is reached and the begin step has no step receipt
  for the succession
- **THEN** the tool exits non-zero naming the missing receipt and neither registry manifest changes

#### Scenario: Remove-basin publish before begin

- **WHEN** the publish step of a `remove_basin` succession is reached and the begin step has no step receipt
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

### Requirement: A succession can add the models of a new basin

The succession tool SHALL accept the kind `add_basin`, which names new models without predecessors and runs
`copyback`, `preflight`, `begin`, `publish`, `refresh` and `finish`. Its `preflight` SHALL audit the packaged
initial condition of every added model and MUST fail before the scheduler timer is stopped unless all are
qualified. Its `publish` SHALL add the models to the merged registry and MUST leave every existing row
unchanged.

#### Scenario: Two sources of a new basin are added

- **WHEN** an `add_basin` succession is applied for the two provisioned models of a new basin whose packages
  ship a qualified initial condition
- **THEN** the six steps run in order, the registry holds two more rows, and every earlier row is unchanged
- **AND** no state index is changed

#### Scenario: An added model is already scheduled

- **WHEN** an `add_basin` succession names a model that is already in the canonical manifest
- **THEN** the run is refused before the timer is touched and nothing is published

#### Scenario: An added model's initial condition is not qualified

- **WHEN** the audit finds an added model whose `ic_status` is not qualified
- **THEN** `preflight` fails before the scheduler timer is stopped and nothing is published

#### Scenario: Continuity record of an added basin

- **WHEN** an `add_basin` succession completes
- **THEN** `plan.json`, `step-publish.json` and `step-finish.json` each carry `continuity` with mode
  `new_basin` and `state_carried` false

### Requirement: The arguments of a succession must fit its kind

The succession tool SHALL refuse, before writing anything, a plan whose arguments do not fit its kind:
`add_basin` with a pair or a cutover time or without an added model; `recalibration` or `cold_start`
with an added model or without a pair or a cutover time; `remove_basin` with a pair, an added model, a
cutover time, a provision succession id or a new-rows registry, or without a removed model; any other kind
with a removed model; and any plan that names a model id twice.

#### Scenario: A pair given to an add

- **WHEN** an `add_basin` succession is given a `--pair`
- **THEN** the run is refused and no file is written

#### Scenario: An added model given to a replacement

- **WHEN** a `recalibration` or `cold_start` succession is given an `--add`
- **THEN** the run is refused and no file is written

#### Scenario: Arguments of another kind given to a removal

- **WHEN** a `remove_basin` succession is given `--pair`, `--add`, `--cutover-time`,
  `--provision-succession-id` or `--new-rows-registry`, or no `--remove`
- **THEN** the run is refused and no file is written

#### Scenario: A removed model given to another kind

- **WHEN** a `recalibration`, `cold_start` or `add_basin` succession is given a `--remove`
- **THEN** the run is refused and no file is written

#### Scenario: A model id named twice

- **WHEN** the same model id is named twice with `--remove`
- **THEN** the run is refused and no file is written

### Requirement: A succession can remove the models of a basin

The succession tool SHALL accept the kind `remove_basin`, whose models are named with `--remove` and whose
steps are `preflight`, `begin`, `publish`, `refresh`, `finish`. It MUST NOT require a provision receipt, copy
a package or read a state index. Its `preflight` SHALL run the publish dry-run for the removal, and the
removal MUST be refused before the scheduler timer is stopped when it would leave a basin with only some of
its sources or names a model that is not in the canonical manifest.

#### Scenario: Both sources of a basin removed

- **WHEN** a `remove_basin` succession naming every model of one basin is applied
- **THEN** the five steps run in order and both manifests lose exactly those rows
- **AND** every other row and both state indexes are unchanged
- **AND** `plan.json`, `step-publish.json` and `step-finish.json` carry `continuity` with mode `basin_removed`

#### Scenario: Only one source removed

- **WHEN** a `remove_basin` succession names one of the two models of a basin
- **THEN** `preflight` fails with the publish dry-run's refusal and the timer was not stopped

#### Scenario: A removed model is not in the manifest

- **WHEN** a `--remove` id is not in the canonical manifest and no step of the succession has run
- **THEN** the tool refuses naming the id, does not report a publish in effect, and nothing is written
