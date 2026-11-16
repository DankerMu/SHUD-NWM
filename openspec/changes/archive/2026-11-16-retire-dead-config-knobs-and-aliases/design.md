# Design: retire-dead-config-knobs-and-aliases (#3146)

Change surface:

| area | files |
|---|---|
| scheduler guard readers | `services/orchestrator/scheduler_lease.py`, `services/orchestrator/file_orchestration_journal.py` |
| retired aliases | `scripts/node27_autopipe_cron.sh`, `scripts/basin_retirement/envfile.py`, `scripts/backfill_pipeline_job_provenance.py`, `services/orchestrator/chain_repository_state.py` |
| alias-isolated tests | `tests/node27_retire_basin_helpers.py`, `tests/test_node27_autopipeline_preflight.py`, `tests/test_node27_retire_basin_exclude_and_wait.py` |
| display deployment surface | `infra/compose.display.yml`, `infra/env/display.example`, `infra/README.two-node-docker.md`, `scripts/validate_two_node_docker_runtime.py`, possibly `services/production_closure/ops_validation.py` |
| governance template | `infra/env/node27-resource-governance.example` |
| docs | `infra/env/README.md` (drift table), `docs/runbooks/tier-node27-timeseries-storage.md`, `docs/adr/0002-*.md` (retired-name marks) |
| tests | `tests/test_node22_scheduler_env_drift_table.py`, `tests/test_production_scheduler.py` (legacy-key cases), `tests/fixtures/file_journal_cluster_map.json`, `tests/test_env_templates.py`, the validator and ops-validation suites |
| live env, outside the repo | node-22 `compute.scheduler-dbfree.env` and `compute.replay.env`; node-27 `display.env` and `node27-resource-governance.env` |

Must preserve:
- **Effective node-22 guards.** The lease guard stays `atomic` and the journal guard stays `flock`, before and after
  both the env migration and the code change.
- **Primary-key precedence.** When a primary key is set, it governs. A legacy key alongside it is ignored, as today.
- **Fail-closed on a legacy-only config.** A legacy key with no primary key raises an error naming the primary key;
  it is neither silently ignored nor silently defaulted. The journal reader keeps its existing refusal of
  `atomic`/`none`/`off`/`disabled`.
- **Consumed values.** Every value a running service actually reads, on the deployed checkouts as well as on master:
  - node-27 `/home/nwm/NWM` at `2710f5a79`;
  - node-22 `/scratch/frd_muziyao/NWM` at `b2fc70547`.
- **The dev MinIO seed path.** `.env.example`, `docker-compose.dev.yml` and `seed_demo.py` keep the S3 keys they read.
- **The node22-env-hygiene drift-table contract.** The README table, the template key existence and the
  loader-effective defaults stay in force, amended only for `NHMS_SCHEDULER_LEASE_GUARD_MODE` being set live.
- **Test isolation from live locks.** No test may flock the node-27 live `/tmp/autopipe.cron.lock`; tests isolate it
  through `AUTOPIPE_LOCK_PATH`.
- **The ops-evidence checklist.** `ops_validation.SERVICE_CONFIGS` and `docs/runbooks/production-service-config.md`
  are kept.
- Passwords and secret values are never printed or changed.

Must add/change: see `proposal.md` "What". Rollout order: node-22 env → code → node-27 env.

Governing invariant: after every rollout step, each node-22 scheduler pass resolves lease=`atomic` and journal=`flock`.
No running service loses a value it reads. No env name stays documented or templated as live without a reader.

Sibling surfaces:
- the other readers of these names, covering settings classes, `${VAR}` in compose and shell, systemd `Environment=`
  and drop-ins, and crontab;
- `.bak-*` env backups on node-22, which carry the legacy key: the fail-closed refusal covers a restore;
- `docs/runbooks/node22-control-plane-manual-recovery.md`;
- the receipts and archives that mention the old names (historical, unchanged).

Seams under test:
- the lease and journal reader matrix: primary set, legacy-only, both set, neither set;
- the drift-table test;
- the env-template tests;
- the two-node validator for the remaining keys;
- the autopipe and basin-retirement env readers;
- the backfill object-store root.

Required evidence: `tasks.md` §2–§3.

Review focus:
- the lease semantics: `atomic` is preserved and fail-closed is correct;
- the rollout order;
- the reader proof for each removed key, on deployed checkouts as well as master;
- that the dev-seed path is untouched;
- the spec delta.
