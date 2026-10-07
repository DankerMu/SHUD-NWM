## ADDED Requirements

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
`add_basin` with a pair or a cutover time or without an added model, and `recalibration` or `cold_start`
with an added model or without a pair or a cutover time.

#### Scenario: A pair given to an add

- **WHEN** an `add_basin` succession is given a `--pair`
- **THEN** the run is refused and no file is written

#### Scenario: An added model given to a replacement

- **WHEN** a `recalibration` or `cold_start` succession is given an `--add`
- **THEN** the run is refused and no file is written

## MODIFIED Requirements

### Requirement: Compute-side succession steps run in a fixed, receipt-gated order

The node-22 model succession tool SHALL run the steps of the succession kind in a fixed order: copyback,
preflight, begin, clone, publish, refresh and finish for `recalibration`; copyback, preflight, begin, publish,
refresh and finish for `cold_start` and for `add_basin`. It SHALL write one receipt per completed step, and
SHALL refuse a step whose preceding step in that order has no successful receipt.

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

#### Scenario: Resume after a failure

- **WHEN** the same command is run again after the cause of a failed step was removed
- **THEN** the steps that already have a receipt are skipped and the remaining steps run
