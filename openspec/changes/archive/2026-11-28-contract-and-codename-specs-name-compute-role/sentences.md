# The sentences of slice 10.2

51 rows: 8 requirement titles and 43 sentences, generated from the substitution table that also
generates the deltas. "/" marks a line break in the spec. Term: `role` the compute role, `control`
the compute control host, `hosts` the compute hosts, `retired` a statement about the retired node-22
host, `none` the sentence no longer needs a subject, `title` a renamed requirement title.


## `node22-db-retirement-cutover`

| # | Term | Before | After |
|---|---|---|---|
| 1 | retired | The system SHALL stop node-22 historical do-not-connect PostgreSQL `:55433` only / after the DB-free scheduler path is live-proven and archived rollback evidence / exists. | The system SHALL stop the historical do-not-connect PostgreSQL `:55433` (of / the retired node-22 host; stopped on 2026-06-29, #837) only after the DB-free / scheduler path is live-proven and archived rollback evidence exists. |
| 2 | retired | - **WHEN** an operator attempts node-22 DB retirement | - **WHEN** an operator attempts to retire the database of the retired node-22 host |
| 3 | retired | - **WHEN** node-22 historical do-not-connect `:55433` is still listening before / retirement and before archived/stopped rollback-only state is reached | - **WHEN** the historical do-not-connect `:55433` of the retired node-22 host is / still listening before retirement and before archived/stopped rollback-only / state is reached |
| 4 | retired | - **WHEN** node-22 historical do-not-connect `:55433` is stopped into / archived/stopped rollback-only state | - **WHEN** the historical do-not-connect `:55433` of the retired node-22 host is / stopped into archived/stopped rollback-only state |
| 5 | retired | - **WHEN** node-22 scheduler is switched to DB-free mode | - **WHEN** the scheduler is switched to DB-free mode (done on / the retired node-22 host, 2026-06-28) |
| 6 | retired | - **WHEN** node-22 historical PostgreSQL has been stopped | - **WHEN** the historical PostgreSQL of the retired node-22 host has been stopped |
| 7 | retired | scheduler documentation or runtime templates from depending on node-22 / historical PostgreSQL after retirement. | scheduler documentation or runtime templates from depending on the historical / PostgreSQL of the retired node-22 host after retirement. |
| 8 | role | - **AND** node-22 scheduler examples do not include active `DATABASE_URL`. | - **AND** scheduler examples of the compute role do not include active `DATABASE_URL`. |

## `node22-dbfree-runtime`

| # | Term | Before | After |
|---|---|---|---|
| 9 | title | Node-22 scheduler has an explicit DB-free runtime mode | The compute role's scheduler has an explicit DB-free runtime mode |
| 10 | role | The system SHALL run the node-22 scheduler only in the DB-free runtime mode: | The system SHALL run the compute role's scheduler only in the DB-free runtime mode: |
| 11 | role | - **WHEN** the node-22 scheduler starts with | - **WHEN** the compute role's scheduler starts with |
| 12 | role | - **WHEN** the node-22 scheduler starts | - **WHEN** the compute role's scheduler starts |
| 13 | role | - **WHEN** two node-22 scheduler passes start against the same file lock root | - **WHEN** two scheduler passes of the compute role start against the same file lock root |
| 14 | none | depend on node-22 PostgreSQL. | depend on PostgreSQL. |

## `node22-env-hygiene`

| # | Term | Before | After |
|---|---|---|---|
| 15 | role | Backup copies of node-22 env files have a declared home | Backup copies of the compute role's env files have a declared home |
| 16 | retired | with one row per key the live node-22 file lacked at classification time | with one row per key the live file on the retired node-22 host lacked at classification time |

## `node22-unit-failure-alert`

| # | Term | Before | After |
|---|---|---|---|
| 17 | title | A generic node-22 unit-failure alert template SHALL exist with absolute paths | A generic unit-failure alert template of the compute control host SHALL exist with absolute paths |
| 18 | title | Tracked node-22 oneshot units SHALL be wired to the alert | Tracked oneshot units of the compute role SHALL be wired to the alert |
| 19 | title | node-22 SHALL reach the SMTPS shim only through the exact-interpreter wrapper | The compute role SHALL reach the SMTPS shim only through the exact-interpreter wrapper |
| 20 | role | A failed node-22 oneshot unit becomes a mail alert through a generic `OnFailure=` template with node-22 absolute paths, | A failed oneshot unit of the compute role becomes a mail alert through a generic `OnFailure=` template with the compute hosts' absolute paths, |
| 21 | role | No node-22 unit or template SHALL contain the literal `%h/NWM`. | No unit or template of the compute role SHALL contain the literal `%h/NWM`. |

## `production-ops-readiness`

| # | Term | Before | After |
|---|---|---|---|
| 22 | title | The node-22 refresh timer health installer restores exactly once on an enable failure | The refresh timer health installer restores exactly once on an enable failure |
| 23 | retired | production deployment as node-27-centric after live verification, not as the / historical node-22 writer topology. | production deployment as node-27-centric after live verification, not as the / historical writer topology of the retired node-22 host. |
| 24 | retired | writer commands of the historical node-22 writer topology as current / operational actions. | writer commands of the historical writer topology of the retired node-22 host / as current operational actions. |
| 25 | retired | - **AND** any retained historical node-22 writer material SHALL be explicitly / labeled historical or do-not-connect, not an action path. | - **AND** any retained material of that historical writer topology SHALL be / explicitly labeled historical or do-not-connect, not an action path. |

## `production-topology-contract`

| # | Term | Before | After |
|---|---|---|---|
| 26 | title | Node-22 scheduler consumes node-27 NFS raw manifests | The compute role's scheduler consumes node-27 NFS raw manifests |
| 27 | role | - **WHEN** an active script, runbook, or governance entry describes node-22 as / the current NHMS active DB writer or asks operators to use node-22 local / PostgreSQL as current production state | - **WHEN** an active script, runbook, or governance entry describes / the compute role, by its codename, as the current NHMS active DB writer or / asks operators to use a local PostgreSQL named with the codename as current / production state |
| 28 | role | The system SHALL keep node-22 as the production scheduler/control point after / node-27 owns GFS/IFS source downloads, and SHALL make shared NFS raw manifests / the source acquisition handoff between the two nodes. | The system SHALL keep the compute role as the production scheduler/control point / after node-27 owns GFS/IFS source downloads, and SHALL make shared NFS raw / manifests the source acquisition handoff between node-27 and the compute role. |
| 29 | role | - **WHEN** node-22 scheduler evaluates a production GFS or IFS source cycle | - **WHEN** the compute role's scheduler evaluates a production GFS or IFS source / cycle |
| 30 | role | - **WHEN** node-22 scheduler finds node-27 NFS raw ready for a cycle whose / canonical rows are absent | - **WHEN** the compute role's scheduler finds node-27 NFS raw ready for a cycle / whose canonical rows are absent |
| 31 | none | - **AND** it does not submit node-22 `download_source_cycle` for that cycle. | - **AND** it does not submit a `download_source_cycle` of its own for that cycle. |
| 32 | role | - **WHEN** node-22 scheduler is configured to stage node-27 NFS raw inputs | - **WHEN** the compute role's scheduler is configured to stage node-27 NFS raw / inputs |
| 33 | role | - **AND** the scheduler does not fall back to node-22 download. | - **AND** the scheduler does not fall back to a download by the compute role. |
| 34 | retired | The system SHALL treat node-22 local PostgreSQL `:55433` as historical, / do-not-connect, archived/stopped rollback-only state after #837; this node-27 / download / NFS handoff change SHALL NOT reintroduce it as an active DB / dependency. | The system SHALL treat the local PostgreSQL `:55433` of the retired node-22 host / as historical, do-not-connect, archived/stopped rollback-only state after #837; / this node-27 download / NFS handoff change SHALL NOT reintroduce it as an active / DB dependency. |
| 35 | retired | - **WHEN** an operator stops node-22 historical PostgreSQL | - **WHEN** an operator stops the historical PostgreSQL of the retired node-22 host |
| 36 | retired | - **AND** post-stop evidence shows no node-22 `:55433` listener | - **AND** post-stop evidence shows no `:55433` listener on the retired node-22 host |
| 37 | role | node-27 download, node-22 NFS-gated scheduling, downstream compute, | node-27 download, NFS-gated scheduling by the compute role, downstream compute, |
| 38 | retired | reintroduce node-22 `:55433` or `10.0.2.100:55433` as active business DB state | reintroduce `:55433` of the retired node-22 host or `10.0.2.100:55433` as active / business DB state |
| 39 | role | - **AND** current download ownership examples do not instruct node-22 to perform / production GFS/IFS source downloads when the NFS raw-manifest gate is enabled. | - **AND** current download ownership examples do not instruct the compute role / to perform production GFS/IFS source downloads when the NFS raw-manifest gate / is enabled. |
| 40 | retired | - **AND** historical node-22 DB or node-22 writer material is clearly marked as / historical or out of current NHMS production topology | - **AND** historical material about the database or the writer role of / the retired node-22 host is clearly marked as historical or out of current / NHMS production topology |
| 41 | role | runbook restricts node-22 as the compute role (no connection to any active / NHMS database, no writer role, exact-interpreter execution, changes only at / a scheduler pass boundary) | runbook restricts the compute role, under a role term or under its codename / (no connection to any active NHMS database, no writer role, exact-interpreter / execution, changes only at a scheduler pass boundary) |
| 42 | retired | mention node-22 local PostgreSQL, port `:55433`, or an archived node-22 / rollback DB mirror / - **THEN** node-22 local PostgreSQL MUST be marked historical, do-not-connect | mention the local PostgreSQL of the retired node-22 host, port `:55433`, or / an archived rollback DB mirror of that host / - **THEN** that PostgreSQL MUST be marked historical, do-not-connect |

## `slurm-gateway-node22-deployment`

| # | Term | Before | After |
|---|---|---|---|
| 43 | title | A standalone Slurm gateway service is deployed on node-22 | A standalone Slurm gateway service is deployed on the compute control host |
| 44 | title | Node-22 Slurm mutation defense in depth | Slurm mutation defense in depth of the compute role |
| 45 | control | Define the standalone Slurm gateway deployed on node-22: | Define the standalone Slurm gateway deployed on the compute control host: |
| 46 | control | A standalone HTTP Slurm gateway (app + systemd unit + listen URL) SHALL be deployed on node-22, / because the generic chain submits only via the gateway and no such service is proven there today / (`NHMS_SERVICE_ROLE=slurm_gateway` is currently reserved/fail-fast). Its submission contract is | A standalone HTTP Slurm gateway (app + systemd unit + listen URL) SHALL be deployed on / the compute control host, because the generic chain submits only via the gateway. When this was / written, no such service was proven on the retired node-22 host, and / `NHMS_SERVICE_ROLE=slurm_gateway` was reserved/fail-fast (recorded no later than 2026-07-25). Its / submission contract is |
| 47 | hosts | runtime breakage in the node-22 maintained active Python environment must not | runtime breakage in the maintained active Python environment of the compute hosts must not |
| 48 | role | - **WHEN** the node-22 gateway and DB-free scheduler are deployed | - **WHEN** the gateway and the DB-free scheduler of the compute role are deployed |
| 49 | control | - **WHEN** the node-22 deployment receipt is captured at the configured live gateway port | - **WHEN** the deployment receipt is captured on the compute control host at the configured live gateway port |
| 50 | control | - **AND** a probe from another host cannot connect to node-22 at the configured gateway port | - **AND** a probe from another host cannot connect to the compute control host at the configured gateway port |
| 51 | control | - **WHEN** local node-22 probes send an invalid submission body | - **WHEN** probes local to the compute control host send an invalid submission body |
