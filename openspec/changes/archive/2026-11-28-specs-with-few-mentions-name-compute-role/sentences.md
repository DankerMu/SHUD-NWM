# Sentences of slice 10.1

Generated from the substitution table of the slice (`design.md` D1). Each old fragment occurs
exactly once in its main spec; the line is the line in the main spec before the change. "Where"
is `Purpose` for a Purpose line (edited in the main spec at archive, D5) and `R` for a requirement
(MODIFIED block).

| Capability | Line | Where | Before | After | Term |
|---|---|---|---|---|---|
| `adapter-cycle-hours` | 78 | R | follows the runbook on node-22 or node-27 | follows the runbook on the compute control host or node-27 | control host |
| `atomic-cutover-transaction` | 4 | Purpose | through the node-22 clone CLI (`scripts/node22_clone_direct_grid_cutover_states.py`) | through the clone CLI of the compute role (`scripts/node22_clone_direct_grid_cutover_states.py`) | role |
| `atomic-cutover-transaction` | 15 | R | outside activation by the node-22 clone CLI `scripts/node22_clone_direct_grid_cutover_states.py`, whose | outside activation by the clone CLI of the compute role, `scripts/node22_clone_direct_grid_cutover_states.py`, whose | role |
| `basin-catalog-manifest-audit` | 9 | R | compares them with the node-22 scheduler manifest's `models[].basin_id` set | compares them with the `models[].basin_id` set of the compute role's scheduler manifest | role |
| `direct-grid-readiness-evidence` | 59 | R | direct-grid evidence package on node-22 and verify | direct-grid evidence package (staged on the retired node-22 host, 2026-07-07) and verify | retired host |
| `direct-grid-readiness-evidence` | 68 | R | `shud` binary path used on node-22 (production binary, no rebuild) | `shud` binary path used on the retired node-22 host (production binary, no rebuild) | retired host |
| `direct-grid-readiness-evidence` | 69 | R | records the node-22 Slurm/SHUD runtime host, | records the Slurm/SHUD runtime host (the retired node-22 host), | retired host |
| `doc-status-alignment` | 62 | R | the compute plane's authority is the node-22 file-registry manifest | the compute plane's authority is the compute role's file-registry manifest | role |
| `doc-status-alignment` | 71 | R | the `dg_*` rows that run on node-22 are not | the `dg_*` rows that the compute role runs are not | role |
| `file-model-readiness` | 10 | R | SHALL support node-22 scheduler model discovery from a versioned | SHALL support the compute role's scheduler model discovery from a versioned | role |
| `file-model-readiness` | 87 | R | **WHEN** node-22 DB-free scheduler evaluates | **WHEN** the compute role's DB-free scheduler evaluates | role |
| `file-orchestration-journal` | 54 | R | **WHEN** node-22 scheduler reads orchestration state | **WHEN** the compute role's scheduler reads orchestration state | role |
| `file-orchestration-journal` | 121 | R | do-not-connect node-22 PostgreSQL `:55433` for the archived/stopped rollback | do-not-connect PostgreSQL `:55433` of the retired node-22 host for the archived/stopped rollback | retired host |
| `file-state-snapshot-index` | 4 | Purpose | state-snapshot index on node-22: | state-snapshot index of the compute role: | role |
| `file-state-snapshot-index` | 872 | R | node-22 SHALL run a daily user-level oneshot unit that performs the `prune-retention` dry-run of both | The compute control host SHALL run a daily user-level oneshot unit that performs the `prune-retention` dry-run of both | control host |
| `file-state-snapshot-index` | 976 | R | node-22 SHALL run a daily user-level oneshot unit that performs a `prune-retention` dry-run. | The compute control host SHALL run a daily user-level oneshot unit that performs a `prune-retention` dry-run. | control host |
| `filesystem-permission-determinism` | 10 | Purpose | so the same code produced `0o755` on node-22 and CI | so the same code produced `0o755` on the retired node-22 host (recorded 2026-08-20) and CI | retired host |
| `fingerprint-gated-state-clone` | 4 | Purpose | Its production caller is the node-22 clone CLI. | Its production caller is the clone CLI of the compute role. | role |
| `fingerprint-gated-state-clone` | 294 | R | The node-22 file-index clone CLI SHALL require | The file-index clone CLI of the compute role SHALL require | role |
| `forcing-copyback-backfill` | 68 | R | MUST document the node-22 backfill command | MUST document the backfill command of the compute role | role |
| `hydro-result-ingest-and-publish` | 45 | R | the same run produced by node 22. | the same run produced by the compute role. | role |
| `mapping-builder-cli` | 34 | R | or node-22 `/volume/nwm/Basins/...`) | or `/volume/nwm/Basins/...` on the compute hosts) | hosts |
| `model-succession-run` | 5 | Purpose | The node-22 command that carries a model succession | The command of the compute role that carries a model succession | role |
| `model-succession-run` | 14 | R | The node-22 model succession tool SHALL run | The model succession tool of the compute role SHALL run | role |
| `monitoring-migration` | 83 | R | Failed-run recovery is the node-22 manual-repair marker lane. | Failed-run recovery is the manual-repair marker lane of the compute role. | role |
| `multibasin-product-discovery` | 21 | R | 并由 22 节点产出 published run 后 | 并由计算角色产出 published run 后 | role |
| `node27-raw-retention` | 104 | R | shared by node-22 and node-27) | shared by the compute hosts and node-27) | hosts |
| `node27-window-admission` | 102 | R | SHALL NOT execute node-22 operations, retention, | SHALL NOT execute operations of the compute role, retention, | role |
| `ops-mvp-pipeline-control` | 11 | R | failed-run recovery is the node-22 manual-repair marker lane (#3179) | failed-run recovery is the manual-repair marker lane of the compute role (#3179) | role |
| `ops-mvp-pipeline-control` | 45 | R | recovery runs through the node-22 manual-repair marker lane (`scripts/ | recovery runs through the manual-repair marker lane of the compute role (`scripts/ | role |
| `orchestrator-structural-burndown` | 698 | R | SHALL partition the node-22 refresh-timer health suite into collectible | SHALL partition the refresh-timer health suite of the compute role into collectible | role |
| `pipeline-monitoring-frontend` | 153 | R | failed-run recovery is the node-22 manual-repair marker lane (`scripts/ | failed-run recovery is the manual-repair marker lane of the compute role (`scripts/ | role |
| `precipitation-raster-overlay` | 96 | R | the same NFS tree node-22 writes under | the same NFS tree the compute role writes under | role |
| `published-pipeline-job-provenance` | 10 | R | The node-22 DB-free scheduler SHALL publish | The DB-free scheduler of the compute role SHALL publish | role |
| `python-environment-truth` | 5 | Purpose | node-22 environment safety, | environment safety of the compute hosts, | hosts |
| `real-shud-slurm-execution` | 23 | R | is visible from the node-22 execution context, | is visible from the execution context of the compute role, | role |
| `real-shud-slurm-execution` | 42 | R | SHALL use a working node-22 Slurm gateway or host submission path | SHALL use a working Slurm gateway or host submission path of the compute role | role |
| `real-slurm-gateway-contract` | 226 | R | (the production node-22 case) | (the case of the retired node-22 host, UTC+8; recorded 2026-08-18) | retired host |
| `retry-execution-contract` | 27 | R | When the node-22 manual-retry script previews | When the manual-retry script of the compute role previews | role |
| `runtime-evidence-and-operations` | 545 | R | deployed to a quiescent node-22 runtime | deployed to a quiescent runtime on the retired node-22 host | retired host |
| `runtime-evidence-and-operations` | 547 | R | DB connection from node-22 or forced stage marker | DB connection from the retired node-22 host or forced stage marker | retired host |
| `scheduler-evidence-retention` | 4 | Purpose | artefacts on node-22 via a systemd user timer | artefacts of the compute role via a systemd user timer | role |
| `scheduler-evidence-retention` | 10 | R | `~/.config/systemd/user/` on node-22 and SHALL be enabled by the node-22 scheduler deployment procedure | `~/.config/systemd/user/` on the compute control host and SHALL be enabled by the scheduler deployment procedure of the compute role | control host, role |
| `scheduler-evidence-retention` | 90 | R | the very first fire on a fresh node-22 succeeds | the very first fire on a fresh compute control host succeeds | control host |
| `scheduler-pass-timing-instrumentation` | 4 | Purpose | Every production scheduler pass on node-22 emits | Every production scheduler pass of the compute role emits | role |
| `scheduler-pass-timing-instrumentation` | 104 | R | (the canonical node-22 workload) | (the canonical workload on the retired node-22 host, recorded no later than 2026-07-25) | retired host |
| `z-policy-verdict` | 14 | R | and/or the node-22 forensic command) | and/or the forensic command on the retired node-22 host) | retired host |

## Requirement titles (RENAMED)

| Capability | Before | After |
|---|---|---|
| `forcing-copyback-backfill` | Node-22 operator documentation describes execution and recovery | Operator documentation of the compute role describes execution and recovery |
| `python-environment-truth` | Active node-22 entrypoints preserve the deferred environment | Active entrypoints of the compute role preserve the deferred environment |
| `real-shud-slurm-execution` | Node-22 Slurm submission path | Slurm submission path of the compute role |
