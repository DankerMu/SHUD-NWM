## ADDED Requirements

### Requirement: Concurrent multi-candidate submission with durable reservation
The scheduler SHALL submit independent candidates (different basin/source/cycle) concurrently with
a durable two-phase reservation, extending m20's sequential cohort execution. (m20
`registered-basin-cycle-discovery` and `slurm-array-runner-integration` define discovery and
within-cycle array fan-out; this adds cross-candidate submit-and-return concurrency.)

#### Scenario: Reservation before lock release prevents double-submit
- **WHEN** a pass selects candidates and submits concurrently
- **THEN** inside the lock it writes a durable reservation / `pipeline_job` / idempotency key per
  candidate, atomically binds `slurm_job_id` on submit, and the reservation is queryable via
  `candidate_state` before the lock is released
- **AND** an overlapping pass finds the reservation and does not re-submit that candidate, even in
  the window before the job appears in `squeue`/`sacct`.

#### Scenario: Concurrency is evidenced, not assumed
- **WHEN** ≥2 independent candidates exist and the gateway has capacity
- **THEN** a receipt shows their submits overlapping (or not waiting for one candidate's terminal
  state before submitting the next)
- **AND** concurrency within the configured bound is preserved with no duplicate submission.

#### Scenario: Submit-crash window does not double-run
- **WHEN** the scheduler crashes after `sbatch` accepts a job but before the durable `slurm_job_id`
  bind, or an HTTP submit times out with unknown result
- **THEN** at most one `pipeline_job` per idempotency key / candidate unique index may enter
  submitted/running, and recovery reconciles the unknown submit via Slurm job name/comment/
  idempotency metadata or a durable submit receipt
- **AND** the candidate is never blindly re-submitted, so no orphan double-run occurs.

### Requirement: Multi-basin live proof with identity isolation through retry
The system SHALL prove ≥2 registered runnable basins/models run live in one daemon pass with strict
identity isolation that survives Slurm array retry/reindex.

#### Scenario: Two basins live in one pass
- **WHEN** ≥2 active runnable models are bootstrapped and a fresh cycle is available
- **THEN** a single live daemon pass drives both basins through the chain to published products
- **AND** evidence reports each basin's run identity, stage statuses, and DB counts.

#### Scenario: Identity survives array retry/reindex
- **WHEN** two basins run array stages in one pass and a failed task is retried (reindexed)
- **THEN** `task_id`/`array_task_id`/`original_task_id`/`run_id`/`model_id`/`basin_version_id`/
  `river_network_version_id`/`segment_ids` stay consistent end to end, mapping the reindexed task
  back to its original basin/segment
- **AND** same-name segments in different river networks are not merged in parsed rows or published
  products.

### Requirement: Per-basin partial-success isolation
A failure in one basin SHALL NOT corrupt sibling basins in the same pass.

#### Scenario: One basin fails, the other publishes (per-stage matrix)
- **WHEN** basin A fails while basin B succeeds, exercised as five named cases — A fails at
  forcing, at forecast, at parse, at frequency, and at publish (each a distinct isolation surface:
  pre-Slurm, array task, parse rows, frequency aggregation, publish manifest)
- **THEN** in every case A records a typed failure, B proceeds to terminal/publish, the cycle
  aggregate status reflects partial success, and B's downstream manifests/products/DB rows exclude A
- **AND** no A artifact or row is attributed to B in any of the five cases.
