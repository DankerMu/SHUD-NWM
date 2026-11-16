# Tasks: retire-dead-config-knobs-and-aliases (#3146)

## Risk packs

- [x] Config — dead knobs, the template name mismatch and legacy aliases. → tasks 1.3–1.6, 2.x.
- [x] Concurrency — the scheduler lease and journal lock guard selection. → tasks 0.1, 1.1, 2.2, 3.1.
- [x] Release / rollout ordering — node-22 env first, then code, then node-27 env. → §0, §3.
- [x] Legacy compatibility — alias removal, with fail-closed on legacy-only configs. → tasks 1.1, 1.2, 2.2.
- [x] Documentation — the README drift table, retired-name marks and the spec delta. → task 1.7.
- [ ] Not selected — Schema, Auth, File IO, Resource limits, Public API.

## 0. Pre-code live migration (orchestrator, node-22)

- [x] 0.1 Before the code changes, at a node-22 scheduler pass boundary:
  1. stop `nhms-compute-scheduler.timer`, and wait for `nhms-compute-scheduler.service` to go inactive;
  2. back `compute.scheduler-dbfree.env` and `compute.replay.env` up to `~/NWM-presync-backup-<UTC date>/infra-env/`,
     mode preserved;
  3. replace the `NHMS_SCHEDULER_FILE_LOCK_GUARD_MODE=…` line with `NHMS_SCHEDULER_LEASE_GUARD_MODE=atomic`;
  4. diff by key name;
  5. restore the timer, with a trap that restores it on any failure.

  Observe the guard modes before and after:
  - `env -i bash -c 'set -a; . <file>; exec /scratch/frd_muziyao/NWM/.venv/bin/python -I -c "<print both
    _file_lock_guard_mode()>"'`, run from the checkout, must give lease=`atomic` and journal=`flock` both times;
  - for the next real pass, list the guard key names (names only) in `/proc/<MainPID>/environ`;
  - that pass's summary must show `failed=0`.

  Do not use `uv run` on node-22. Note in the receipt that `compute.replay.env` has no consumer, and that the other
  four dbfree-env units do not read the guards.

  **Receipt (2026-11-16):**
  - Before: both files resolved lease=`atomic` and journal=`flock`, with the legacy key and the JOURNAL key present.
  - 11:16:03Z: the timer was stopped. At 11:20:13Z the service reached `inactive`, which was the pass boundary.
  - Backups: `~/NWM-presync-backup-20261116/infra-env/*.bak-issue3146-20261116T111603Z`, mode 600.
  - The key-name diff shows exactly one line per file changed from `NHMS_SCHEDULER_FILE_LOCK_GUARD_MODE` to
    `NHMS_SCHEDULER_LEASE_GUARD_MODE`, at dbfree line 106 and replay line 99. Line counts are unchanged (143 and 128)
    and the mode is still 600.
  - After: both files resolve lease=`atomic` and journal=`flock`. The timer was restored at 11:20:15Z.
  - The next pass's `/proc/<MainPID>/environ` carried only `NHMS_SCHEDULER_JOURNAL_LOCK_GUARD_MODE` and
    `NHMS_SCHEDULER_LEASE_GUARD_MODE`.
  - Seven post-migration passes, 11:20Z to 12:02Z, all had systemd `Result=success`, evidence `status=planned`,
    `counts.failed_count=0`, `partial=0`, `blocked=0` and `lock.acquired=true` with no reason.

## 1. Implementation (implementer)

- [x] 1.1 Lease and journal readers:
  - Remove the legacy fallback from both `_file_lock_guard_mode()` functions.
  - If `NHMS_SCHEDULER_FILE_LOCK_GUARD_MODE` is set and the reader's primary key is unset, raise. The error names the
    primary key, uses the module's existing error type (`UnsafeSchedulerLockError` / `SafeFilesystemError`) and gives
    a stable reason.
  - If the primary key is set, ignore the legacy key.
  - Keep the journal's refusal of unsafe values.
- [x] 1.2 Update the tests that pin the legacy behaviour:
  - `tests/test_production_scheduler.py` ~:24180;
  - `tests/fixtures/file_journal_cluster_map.json` ~:101;
  - `tests/test_node22_scheduler_env_drift_table.py`, including
    `test_lease_action_records_and_defers_without_setting_the_legacy_key`. That test now asserts that the lease row's
    operator action is the bare assignment `NHMS_SCHEDULER_LEASE_GUARD_MODE=atomic`, which
    `test_operator_action_is_a_bare_assignment_or_keep_unset` already accepts. Add the
  reader matrix for both readers: primary set, legacy-only (raises), both set (primary wins), neither set (default).
- [x] 1.3 Drop the `NODE27_AUTOPIPE_LOCK_PATH` alias:
  - in `scripts/node27_autopipe_cron.sh`;
  - in `scripts/basin_retirement/envfile.py`, whose process-level fallback becomes `AUTOPIPE_LOCK_PATH` (matching
    cron), never directly the default.

  Move the alias-isolated tests to `AUTOPIPE_LOCK_PATH`: `tests/node27_retire_basin_helpers.py` ~:771,
  `tests/test_node27_autopipeline_preflight.py` ~:1103 and `tests/test_node27_retire_basin_exclude_and_wait.py`
  ~:341,360. Then confirm that no test can reach `/tmp/autopipe.cron.lock`.

  Make `scripts/backfill_pipeline_job_provenance.py` and `services/orchestrator/chain_repository_state.py` ~:317 read
  only `OBJECT_STORE_ROOT`, and update their tests.
- [x] 1.4 Dead display knobs. For each of `CORS_ALLOWED_ORIGINS`, `NHMS_ARTIFACT_BACKEND`, `S3_ENDPOINT_URL` and
  `S3_BUCKET_NAME`, prove there is no reader on the display surface:
  - search case-insensitively, including settings-class field names, `${VAR}` and `os.environ[...]`;
  - search master, and the deployed node-27 checkout with read-only `git grep` (the orchestrator supplies the output);
  - record the evidence.

  Then remove them from:
  - `infra/compose.display.yml`;
  - `infra/env/display.example`;
  - `infra/README.two-node-docker.md`;
  - the `scripts/validate_two_node_docker_runtime.py` display key lists.

  Review the guard lists `COMPUTE_ONLY_PATH_ENV_KEYS` (~:331, used at ~:4418) and `DISPLAY_AUDITED_RUNTIME_ENV` key by
  key, keeping `AWS_*`. Adjust the ambient-import tests in `tests/test_two_node_docker_runtime.py` (~:1853, ~:1942).

  Keep the dev-seed path: `.env.example`, `docker-compose.dev.yml`, `seed_demo.py` and the `README.md` local dev
  table.

  **KEEP** `ops_validation.SERVICE_CONFIGS["api"]`'s `CORS_ALLOWED_ORIGINS` and
  `docs/runbooks/production-service-config.md`. They form the ops-evidence checklist read through
  `NHMS_PRODUCTION_OPS_API_CORS_ALLOWED_ORIGINS` and pinned by `tests/test_production_ops_validation.py:234`. Record
  this reason in the PR.
- [x] 1.5 Governance template: replace `NODE27_RESOURCE_GOVERNANCE_REPO_ROOT` with `NODE27_GOVERNANCE_REPO_ROOT` in
  `infra/env/node27-resource-governance.example`. Add a one-line note: the wrapper's own `NODE27_RESOURCE_GOVERNANCE_REPO` can
  be set only through the unit `Environment=`, because the env file lives under it, and it defaults to `/home/nwm/NWM`.
- [x] 1.6 Mark as retired, rather than delete, the retired cold-tier, backfill and archive env names where docs present
  them as live:
  - `docs/runbooks/tier-node27-timeseries-storage.md` (issue line refs);
  - `docs/adr/0002-node27-timeseries-hot-cold-tiering.md` ~:436-438;
  - `infra/env/node27-resource-governance.example` ~:40.

  Historical receipts are unchanged.
- [x] 1.7 Update the README drift table row for `NHMS_SCHEDULER_LEASE_GUARD_MODE`:
  - the operator action becomes the bare assignment `NHMS_SCHEDULER_LEASE_GUARD_MODE=atomic`;
  - status and effective value when unset (`flock`) are unchanged;
  - add a note under the table recording that #3146 set the key live on 2026-11-16 and removed the legacy key.

  Also fix the README prose that says the live file "lacks the seven keys" and "changes no live value". Keep all of
  this consistent with the `node22-env-hygiene` delta.

## 2. Tests / verification (each expected to pass)

- [x] 2.1 `uv run ruff check .` is clean.
- [x] 2.2 `uv run pytest -q tests/test_env_templates.py tests/test_node22_scheduler_env_drift_table.py tests/test_node22_env_backup_sweep.py`,
  plus the reader-matrix tests from 1.2.
- [ ] 2.3 The selector-chosen set for the diff, plus the validator and ops-validation suites, passes locally and in a
  node-27 temp worktree.
- [ ] 2.4 Reader proof for each removed key and alias: zero hits outside docs, archives and receipts, on master and on
  the deployed node-27 and node-22 checkouts. The output is attached to the PR. For `S3_ENDPOINT_URL` and
  `S3_BUCKET_NAME` the zero-hit rule is scoped to the display deployment surface; the dev-seed hits are expected.

## 3. Post-merge live steps (orchestrator)

- [ ] 3.1 node-22: after the checkout is ff-only pulled at a pass boundary, confirm the next pass with the 0.1 method:
  lease `atomic` and journal `flock`, and the pass summary shows `failed=0`.
- [ ] 3.2 node-27:
  - Back up `node27-resource-governance.env` and do the governance rename now. The value is the same, and the
    deployed `2710f5a79` default is equal to it.
  - **Edit `display.env` only after node-27 is redeployed with this change.** The deployed `2710f5a79` C1 validator
    still interpolates the four keys, so removing them earlier would make it report
    `DISPLAY_INTERPOLATION_ENV_MISSING`. Runtime is unaffected either way. The redeploy is an owner decision.
  - Then back up `display.env` and remove the four dead keys. Nothing reads them, so no restart is needed.
  - Back up `node27-resource-governance.env` and replace `NODE27_RESOURCE_GOVERNANCE_REPO_ROOT` with
    `NODE27_GOVERNANCE_REPO_ROOT` (same value).
  - Diff by key name.
  - Run `env -i bash -c 'set -a; . display.env'`; it must exit 0.
  - Check `/health` returns 200.
  - Check that the next governance run is not `BLOCKED rc=2` and that its summary `repo_root` is `/home/nwm/NWM`.

## Deviations

- The `LEGACY_*` guard constants are kept, because the fail-closed refusal needs them. So
  `tests/fixtures/file_journal_cluster_map.json` is unchanged; it was listed in 1.2.
- A legacy guard key set to `""` is refused as well. The test is "is the key set", not "is it non-empty".
- `envfile.py`: an empty `AUTOPIPE_LOCK_PATH=` line resolves to the default, as cron's `${AUTOPIPE_LOCK_PATH:-…}`
  does. The process environment is consulted only when the line is absent. The proposal's "never directly to the
  default" holds for the absent-line case only.
- The drift test `test_lease_action_records_and_defers…` is renamed to
  `test_lease_action_is_the_live_assignment_and_never_names_the_legacy_key`, and a note-under-table assertion is added.
- The journal reader matrix is exercised through the repository cycle lock, not module-private names; the facade
  guard requires this.
- `infra/env/README.md` ~:436 is edited: "object-store/CORS settings" becomes "object-store settings".
- Review round 1 P2: `S3_BUCKET_NAME` is also removed from the dev path, where it has no reader. The
  `legacy-dead-code-retirement` requirement is narrowed so that fail-closed applies to safety-deciding aliases only.
- The lease refusal surfaces as `lock_contended` with
  `lock.reason=retired_lock_guard_mode_key:NHMS_SCHEDULER_LEASE_GUARD_MODE`. This is the same path as the existing
  `unsafe_lock_guard_mode`, and the stall probe alerts on it as `lock_contended_persistent`. Rolling back by restoring
  a pre-#3146 `.bak` env under the new code stops the scheduler until the LEASE key is set. That is the intended
  fail-closed behaviour.
