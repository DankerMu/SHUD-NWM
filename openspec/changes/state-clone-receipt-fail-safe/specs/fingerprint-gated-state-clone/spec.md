## ADDED Requirements

### Requirement: File-index clone invocations preserve durable abort evidence and primary errors

The node-22 file-index clone CLI SHALL require a persistent receipt path for every `recalibration` invocation, including dry-run. `baseline_cutover` SHALL retain its existing optional receipt flag.

When an `--apply` invocation has already persisted one or more clone rows and then aborts, the CLI SHALL attempt to write a receipt that enumerates every completed persisted clone and identifies the failed pair or basin/source location and failure reason before propagating the original failure. A receipt-write failure during this abort handling SHALL be exposed to the operator but SHALL NOT replace the original clone, validation, or mirror-write failure. A receipt-write failure on an otherwise successful invocation SHALL still fail normally, and an existing receipt SHALL never be overwritten.

#### Scenario: Baseline abort after a persisted clone produces evidence and stays failed

- **WHEN** `baseline_cutover --apply` persists at least one warm clone row and a later basin/source fails validation or clone admission
- **THEN** the requested receipt records every completed persisted clone with its `state_id`, marks the invocation aborted, and identifies the failed basin/source and original reason
- **THEN** the original exception still propagates and the process does not report success.

#### Scenario: Receipt failure cannot mask a recalibration refusal

- **WHEN** a recalibration apply has persisted an earlier pair, a later pair is refused, and the requested `O_EXCL` receipt path already exists
- **THEN** the original refusal exception propagates rather than `FileExistsError`
- **THEN** the operator-visible exception also reports that receipt persistence failed and names the receipt error, while the existing file remains unchanged.

#### Scenario: Receipt failure cannot mask mirror divergence

- **WHEN** a recalibration apply writes the canonical row, the mirror write fails, and receipt persistence also fails
- **THEN** the original mirror-divergence `CutoverCloneError` propagates rather than the receipt error
- **THEN** the operator-visible exception reports both the mirror failure and receipt persistence failure.

#### Scenario: Clean receipt failure remains a failure

- **WHEN** either mode otherwise completes but its requested receipt cannot be created
- **THEN** the receipt-write exception propagates and is not swallowed
- **THEN** an existing receipt is not overwritten.

#### Scenario: Recalibration always declares a receipt destination

- **WHEN** either a dry-run or `--apply` recalibration invocation omits `--receipt`
- **THEN** per-mode flag enforcement rejects it with the same parser-error shape as the other required recalibration flags
- **THEN** a successful invocation with a unique receipt path persists JSON equal to its returned receipt payload.

#### Scenario: Baseline compatibility and no-write failures remain unchanged

- **WHEN** an existing baseline-cutover invocation omits `--receipt`, or either mode fails before any clone row is persisted
- **THEN** baseline flag parsing remains valid and no new empty abort receipt is required
- **THEN** successful baseline receipt fields and dry-run state-index behavior retain their prior meanings.
