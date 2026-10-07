## MODIFIED Requirements

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

## ADDED Requirements

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
