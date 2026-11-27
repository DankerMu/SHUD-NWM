# Appendix: requirement titles that carry the codename (18, `f5c3b487e`)

`git grep -n -i -E '^### Requirement:.*node[- ]?22' -- openspec/specs` returns these 18. Scenario
headings are not listed: they are edited inside the MODIFIED block of their requirement.

Rule (`design.md` §3): a title about the retired host or its database is a retired-host statement
and is kept, because its own words (historical, archived, retirement, rollback) already say which
machine it means. Every other title uses the codename for the role or as an adjective for something
the role owns, and is renamed (RENAMED delta) with the term of `design.md` §1 that fits; a real name
it paraphrases can be given in the requirement body in code font.

| Capability | Title today | Class | Group |
|---|---|---|---|
| `compute-scheduler-operationalization` | Node-22 end-to-end automation evidence | role: renamed | 10.3 |
| `compute-scheduler-operationalization` | Node-22 exact-cycle wrapper serializes with the scheduler timer | paraphrases `scripts/ops/node22-run-cycle-once.sh`: renamed | 10.3 |
| `forcing-copyback-backfill` | Node-22 operator documentation describes execution and recovery | role: renamed | 10.1 |
| `forcing-domain-handoff` | Archived/stopped compatibility-only node-22 rollback mirror is explicit-DSN, allow-flagged, and sunset/removal-bound | retired host: kept | |
| `node22-db-retirement-cutover` | Node-22 historical PostgreSQL is stopped only after DB-free proof | retired host: kept | |
| `node22-db-retirement-cutover` | Guardrails prevent reintroducing active node-22 DB assumptions | retired host: kept | |
| `node22-dbfree-runtime` | Node-22 scheduler has an explicit DB-free runtime mode | role: renamed | 10.2 |
| `node22-unit-failure-alert` | A generic node-22 unit-failure alert template SHALL exist with absolute paths | paraphrases `nhms-node22-unit-failure-alert@.service`: renamed | 10.2 |
| `node22-unit-failure-alert` | Tracked node-22 oneshot units SHALL be wired to the alert | role: renamed | 10.2 |
| `node22-unit-failure-alert` | node-22 SHALL reach the SMTPS shim only through the exact-interpreter wrapper | role: renamed | 10.2 |
| `node22-unit-tracking` | Live node-22 units SHALL be tracked byte-exactly with a manifest and without secret values | role: renamed | 9.2 |
| `production-ops-readiness` | The node-22 refresh timer health installer restores exactly once on an enable failure | paraphrases `scripts/install_node22_refresh_timer_health.sh`: renamed | 10.2 |
| `production-topology-contract` | Node-22 scheduler consumes node-27 NFS raw manifests | role: renamed | 10.2 |
| `production-topology-contract` | Historical node-22 PostgreSQL retirement remains separately gated | retired host: kept | |
| `python-environment-truth` | Active node-22 entrypoints preserve the deferred environment | role: renamed | 10.1 |
| `real-shud-slurm-execution` | Node-22 Slurm submission path | role: renamed | 10.1 |
| `slurm-gateway-node22-deployment` | A standalone Slurm gateway service is deployed on node-22 | role: renamed | 10.2 |
| `slurm-gateway-node22-deployment` | Node-22 Slurm mutation defense in depth | role: renamed | 10.2 |

14 renamed, 4 kept.
