## ADDED Requirements

### Requirement: Node-27 ingest has an explicit data-plane role

The node-27 ingest worker SHALL run under an explicit data-plane ingest contract
that is separate from the display API's `display_readonly` contract.

#### Scenario: Ingest preflight validates writer dependencies

- **WHEN** the node-27 ingest wrapper starts a bounded autopipeline pass
- **THEN** it validates writer `DATABASE_URL`, `OBJECT_STORE_ROOT`,
  `BASINS_ROOT`, and work/log roots before processing runs
- **AND** missing or unsafe values fail before any partial seed, import,
  activate, geometry backfill, register, forcing handoff/mirror, parse,
  coverage, or publish-status step starts
- **AND** error output redacts secrets

#### Scenario: Display runtime remains read-only

- **WHEN** node-27 display API starts with `NHMS_SERVICE_ROLE=display_readonly`
- **THEN** it keeps control mutations disabled, Slurm routes disabled, and no
  data-plane writer preflight
- **AND** display API health/runtime evidence does not imply that ingest writer
  dependencies are valid

### Requirement: Node-27 ingest remains bounded and failure-isolated

The node-27 ingest pass SHALL stay idempotent, bounded, and failure-isolated
across runs and basins.

#### Scenario: One run failure does not abort unrelated ingest

- **WHEN** a discovered run fails register, forcing handoff, parse, coverage, or
  publish-status refresh
- **THEN** the JSON summary records that run's stage and stable reason
- **AND** unrelated runnable runs in the same pass continue
- **AND** already-ingested runs are skipped unless a force/retry option is used

#### Scenario: Basin seed failure is isolated and visible

- **WHEN** basin registry seed, import, model activation, or geometry backfill
  fails before per-run ingest
- **THEN** the JSON summary records the basin, stage, and stable reason
- **AND** runs for that unseeded basin are not partially processed
- **AND** unrelated seeded basins and runs may continue in the same bounded pass

#### Scenario: Ingest evidence distinguishes data-plane and display-plane health

- **WHEN** an operator reviews node-27 ingest logs or receipts
- **THEN** the evidence identifies the data-plane ingest role, `seed registry ->
  register -> object-store forcing handoff or explicit mirror -> parse ->
  refresh coverage -> publish status` stage shape, object-store root, DB
  host/port without secrets, discovered/processed/skipped counts, and final
  return code
- **AND** display API `/health` is treated as a separate consumer health check
