# The sentences of slice 10.3

66 rows: 2 requirement titles and 64 sentences, generated from the substitution table that also
generates the deltas. "/" marks a line break in the spec. Term: `role` the compute role, `control`
the compute control host, `hosts` the compute hosts, `retired` a statement about the retired node-22
host, `title` a renamed requirement title.


## `basin-retirement`

| # | Term | Before | After |
|---|---|---|---|
| 1 | role | after its models were removed from the scheduler on node-22: | after its models were removed from the compute role's scheduler: |
| 2 | role | refuse to run any step unless the node-22 succession of the same id has finished | refuse to run any step unless the compute role's succession of the same id has finished |
| 3 | role | - **WHEN** the tool is applied for a basin version whose node-22 removal has finished | - **WHEN** the tool is applied for a basin version whose removal from the compute role's scheduler has finished |

## `canonical-precip-copyback`

| # | Term | Before | After |
|---|---|---|---|
| 4 | hosts | On node-22 the copyback root is the shared NFS mount while the journal and object-store roots are local, so | On the compute hosts the copyback root is the NFS mount shared with node-27 while the journal and object-store roots are on the compute hosts' own storage, so |
| 5 | role | — on node-22's `forecast_state_save_qc` terminal contract that is | — on the compute role's `forecast_state_save_qc` terminal contract that is |
| 6 | retired | and the node-22 receipt `docs/runbooks/receipts/2026-09-06-precip-copyback-backfill.md` §6.1 observed | and the receipt `docs/runbooks/receipts/2026-09-06-precip-copyback-backfill.md` (taken on the retired node-22 host) §6.1 observed |
| 7 | hosts | the receipt lives in the scheduler journal on node-22's local disk, so | the receipt lives in the scheduler journal on storage private to the compute hosts, so |
| 8 | hosts | — node-22's frozen checkout never needs an environment built — | — the frozen checkout of the compute hosts never needs an environment built — |
| 9 | control | runnable on node-22 **from the repo root** | runnable on the compute control host **from the repo root** |
| 10 | retired | because the node-22 operation in this change's tasks invokes it without a flag | because the operation in this change's tasks (run on the retired node-22 host, 2026-09-06) invoked it without a flag |
| 11 | role | because node-22 writes as one account and node-27 reads | because the compute role writes as one account and node-27 reads |
| 12 | hosts | so the frozen node-22 checkout still needs no environment built | so the frozen checkout of the compute hosts still needs no environment built |

## `compute-scheduler-operationalization`

| # | Term | Before | After |
|---|---|---|---|
| 13 | title | Node-22 end-to-end automation evidence | End-to-end automation evidence of the compute role |
| 14 | title | Node-22 exact-cycle wrapper serializes with the scheduler timer | The exact-cycle wrapper serializes with the scheduler timer |
| 15 | role | Specify how the node-22 compute scheduler is run in production: | Specify how the compute role's scheduler is run in production: |
| 16 | role | and tracked templates are authoritative for each node-22 unit ( | and tracked templates are authoritative for each unit of the compute role ( |
| 17 | role | The system SHALL provide automated tests or commands that prove node-22 QHH business flow from | The system SHALL provide automated tests or commands that prove the compute role's QHH business flow from |
| 18 | role | - **AND** final node-22 business automation readiness remains false | - **AND** final business automation readiness of the compute role remains false |
| 19 | role | SHALL carry a table mapping every node-22 user systemd unit ( | SHALL carry a table mapping every user systemd unit of the compute role ( |

## `forcing-domain-handoff`

| # | Term | Before | After |
|---|---|---|---|
| 20 | retired | forcing readiness without querying an active node-22 database. The handoff MUST | forcing readiness without querying the database of the retired node-22 host. The handoff MUST |
| 21 | retired | - **AND** the receipt proves readiness did not depend on implicit node-22 DB / access or `infra/env/display.env` mirror fallback | - **AND** the receipt proves readiness did not depend on implicit access to the DB of / the retired node-22 host or on `infra/env/display.env` mirror fallback |
| 22 | retired | The system SHALL keep any archived node-22 rollback forcing mirror path | The system SHALL keep any archived rollback forcing mirror path of the retired node-22 host |

## `job-retry-mechanism`

| # | Term | Before | After |
|---|---|---|---|
| 23 | role | audit evidence, and the node-22 manual-repair marker lane that lets the scheduler | audit evidence, and the manual-repair marker lane of the compute role that lets the scheduler |
| 24 | role | (shared by the node-22 manual-repair marker lane `FileJournalRetryService.record_manual_repair`) | (shared by the manual-repair marker lane of the compute role, `FileJournalRetryService.record_manual_repair`) |
| 25 | role | - **WHEN** the node-22 manual-retry operator entrypoint previews | - **WHEN** the manual-retry operator entrypoint of the compute role previews |
| 26 | role | Scheduler retry submissions for node-22 DB-free scheduler operation SHALL preserve the | Scheduler retry submissions for the compute role's DB-free scheduler operation SHALL preserve the |
| 27 | role | - **AND** node-22 production requires direct-grid forcing | - **AND** the compute role's production requires direct-grid forcing |
| 28 | control | - **WHEN** an operator provisions node-22 from | - **WHEN** an operator provisions the compute control host from |
| 29 | hosts | resolved copyback authority to the fixed node-22 topology identity | resolved copyback authority to the fixed topology identity of the compute hosts |

## `node27-download-orchestration`

| # | Term | Before | After |
|---|---|---|---|
| 30 | retired | - **AND** it rejects display_readonly-like credentials and node-22 historical / PostgreSQL endpoints such as `:55433` | - **AND** it rejects display_readonly-like credentials and historical PostgreSQL / endpoints of the retired node-22 host such as `:55433` |
| 31 | retired | - **AND** node-22 historical PostgreSQL is not read or written for source-cycle / truth | - **AND** the historical PostgreSQL of the retired node-22 host is not read or / written for source-cycle truth |
| 32 | role | node-22 scheduler will evaluate. | the compute role's scheduler will evaluate. |
| 33 | role | business cycles on node-27 and hands completed raw cycles to node-22 scheduler | business cycles on node-27 and hands completed raw cycles to the compute role's scheduler |
| 34 | role | - **THEN** node-22 scheduler observes the raw manifest on NFS before starting | - **THEN** the compute role's scheduler observes the raw manifest on NFS before starting |
| 35 | role | - **AND** public latest-product endpoints can advance without node-22 performing | - **AND** public latest-product endpoints can advance without the compute role performing |

## `pipeline-job-persistence`

| # | Term | Before | After |
|---|---|---|---|
| 36 | role | The scheduler SHALL record every stage job it submits to Slurm in the node-22 / file orchestration journal, and | The scheduler SHALL record every stage job it submits to Slurm in the / file orchestration journal of the compute role, and |
| 37 | role | - **WHEN** the DB-free node-22 Orchestrator submits a stage job | - **WHEN** the compute role's DB-free Orchestrator submits a stage job |
| 38 | role | - **AND** `ops.pipeline_job` SHALL not be populated by the node-22 journal lane | - **AND** `ops.pipeline_job` SHALL not be populated by the journal lane of the compute role |
| 39 | role | The node-22 file-journal retention entrypoint SHALL derive | The file-journal retention entrypoint of the compute role SHALL derive |

## `production-scheduler-orchestration`

| # | Term | Before | After |
|---|---|---|---|
| 40 | control | on completion or stable blocker, and remain business-runnable on node-22 in / DB-free mode under configured concurrent submission. | on completion or stable blocker, and remain business-runnable on / the compute control host in DB-free mode under configured concurrent submission. |
| 41 | role | WHEN the DB-free node-22 scheduler runs a live pass | WHEN the compute role's DB-free scheduler runs a live pass |
| 42 | role | WHEN node-22 runs the compute scheduler with a concurrent submit bound greater | WHEN the compute role runs its scheduler with a concurrent submit bound greater |
| 43 | role | WHEN node-22 runs the scheduler and no eligible candidate or array task exists | WHEN the compute role runs the scheduler and no eligible candidate or array task exists |
| 44 | hosts | the pass receipts and journal live on node-22 private storage it cannot reach, | the pass receipts and journal live on storage private to the compute hosts that it cannot reach, |
| 45 | retired | Measured at the live node-22 evidence root on 2026-09-16, the two counts are equal on every retained pass, so the boundary is presently empty in production while remaining structurally real. | Measured on 2026-09-16 at the then-live evidence root on the retired node-22 host, the two counts were equal on every retained pass, so the boundary was then empty in production while remaining structurally real. |
| 46 | retired | Measured at the live node-22 evidence root on 2026-09-16, every retained pass | Measured on 2026-09-16 at the then-live evidence root on the retired node-22 host, every retained pass |

## `scheduler-registry-refresh`

| # | Term | Before | After |
|---|---|---|---|
| 47 | role | The node-22 file-provider refresh lane republishes, database-free, | The file-provider refresh lane of the compute role republishes, database-free, |
| 48 | role | The system SHALL provide a read-only, database-free health probe for the node-22 / file-provider refresh lane that records | The system SHALL provide a read-only, database-free health probe for the / file-provider refresh lane of the compute role that records |
| 49 | role | The tracked template for the node-22 database-free scheduler environment SHALL | The tracked template for the compute role's database-free scheduler environment SHALL |
| 50 | role | - **WHEN** the node-22 database-free scheduler environment is rebuilt from its | - **WHEN** the compute role's database-free scheduler environment is rebuilt from its |
| 51 | role | The node-22 file-provider refresh installer SHALL run under `errtrace`. | The file-provider refresh installer of the compute role SHALL run under `errtrace`. |
| 52 | hosts | The service SHALL use only trusted node-22 repository/Basins/object-store/file- / provider paths and SHALL emit | The service SHALL use only trusted repository/Basins/object-store/file- / provider paths of the compute hosts and SHALL emit |
| 53 | hosts | - **THEN** the service uses absolute node-22 repo/interpreter paths, the mode- | - **THEN** the service uses absolute repo/interpreter paths of the compute hosts, the mode- |
| 54 | role | The system SHALL refresh every node-22 provider before its 168-hour consumer | The system SHALL refresh every provider of the compute role before its 168-hour consumer |

## `scheduler-stall-alerting`

| # | Term | Before | After |
|---|---|---|---|
| 55 | role | This capability governs the node-22 side of | This capability governs the compute role's side of |
| 56 | control | and exits non-zero for anything but healthy — the journal and a failed unit being the only / alerting channel that host has. | and exits non-zero for anything but healthy — the journal and a failed unit being the only / alerting signals it gives (the generic unit-failure alert of the compute control host mails a / failed unit). |
| 57 | role | It is the node-22 counterpart of `frontier-stall-alerting`, which watches node-27's / published frontier. The two differ where their hosts differ: node-27 has a database and a / mail lane, node-22 has neither, so this capability carries no persistent state and no / outbound channel. | It is the compute role's counterpart of `frontier-stall-alerting`, which watches node-27's / published frontier. The two differ where their sides differ: node-27 has a database and a / mail lane of its own; the compute role has no database and the probe sends nothing itself, so / this capability carries no persistent state and no outbound channel. |
| 58 | role | The system SHALL provide a read-only, database-free health probe for the node-22 / DB-free scheduler lane that reads | The system SHALL provide a read-only, database-free health probe for the / DB-free scheduler lane of the compute role that reads |
| 59 | control | on the probe's error output, because the failed unit is what the host's / `OnFailure=` alert lane forwards ( | on the probe's error output, because the failed unit is what / the compute control host's `OnFailure=` alert lane forwards ( |
| 60 | role | The node-22 stall probe SHALL classify | The stall probe of the compute role SHALL classify |

## `variant-activation-cutover`

| # | Term | Before | After |
|---|---|---|---|
| 61 | role | the node-22 file-provider refresh lane republishes it | the file-provider refresh lane of the compute role republishes it |
| 62 | role | SHALL publish nothing to the node-22 scheduler file plane: neither | SHALL publish nothing to the compute role's scheduler file plane: neither |
| 63 | role | does not change node-22's dispatch set. | does not change the compute role's dispatch set. |
| 64 | role | node-22's registry manifest, and so its dispatch set, changes only | The compute role's registry manifest, and so its dispatch set, changes only |
| 65 | control | - node-22 `scripts/node22_model_succession.py` runs the steps of its `--kind`: | - on the compute control host, `scripts/node22_model_succession.py` runs the steps of its `--kind`: |
| 66 | role | The node-22 scheduler and its refresh timer keep | The compute role's scheduler and its refresh timer keep |
