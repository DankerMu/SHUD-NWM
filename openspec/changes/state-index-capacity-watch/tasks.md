# Tasks: state-index-capacity-watch (#2653)

## Risk packs

- Config / project setup: **selected**. The unit env wiring and a lag that is missing fail closed. Covered by 1.2 and 2.2.
- Public API / CLI / script entry: **selected**. New wrapper exit codes. Covered by 1.2.
- File IO / path safety / overwrite: **selected**. Receipt root safety and rotation, and the guarantee that nothing is written to the index, archive or repair roots. Covered by 1.2 and 1.3.
- Release / packaging: **selected**. The unit files must pass the node-22 interpreter invariant. Covered by 2.2.
- Documentation / migration notes: **selected**. Runbook §8.12. Covered by 3.1.
- Error handling: **selected (light)**. CLI refusal and incomplete results map to non-zero exits. Covered by 1.2.
- Concurrency / shared state: **selected (light)**. An unlocked read can race a writer's atomic replace; this is handled by a bounded `provider_preimage_changed` retry. Covered by 1.2.
- Auth / permissions / secrets: not selected.
- Resource limits / large input: not selected. The input is the index the CLI already bounds.
- Schema / columns / units / field names: not selected. The receipt is a new, private artifact.
- Legacy compatibility: not selected.

## 1. Wrapper

- [ ] 1.1 Add `scripts/node22_state_index_capacity_watch.py` as specified in design.md. It has no enforce path.
- [ ] 1.2 Tests. Cover each case, with the repair call faked where needed:
  - healthy → exit 0, receipt written;
  - `capacity.warning=true` on either lane → exit 1, receipt written;
  - `checksum_valid=false` → exit 1;
  - `entry_count_before == 0` → exit 1;
  - a missing lane → exit 1;
  - `RepairCliError` (for example `repair_cycle_lag_unset`, driven through the real repair function with valid roots and prefix set and only the lag env unset) → exit 2;
  - `RepairIncompleteError` → exit 3;
  - `capacity_after.warning=true` → exit 1 with `capacity_warning_unprunable`;
  - `provider_preimage_changed` once, then success → exit 0 with `attempts=2`; persistent → exit 2 after 3 attempts;
  - receipt root unset, missing, not owned, or group/other-accessible → exit 2;
  - receipt write failure → exit 2, with the verdict JSON still printed on stdout;
  - an unexpected exception → exit 4 with a `refused` receipt;
  - a subprocess smoke test: `sys.executable -m scripts.node22_state_index_capacity_watch --help` from the repo root exits 0, which proves the import chain works.
- [ ] 1.3 Tests for the dry-run-only guarantee:
  - the repair function is always called with `enforce=False`, and the parser has no `--enforce`;
  - an end-to-end run against real temporary reference and destination index fixtures leaves both index files byte-identical;
  - no file is created under the archive or repair-receipt roots, even when those env vars are set;
  - the receipt is bounded: no `removed_state_ids` and no `groups`, and at most 30 timestamped receipts are kept.

## 2. Units

- [ ] 2.1 Add `infra/systemd/nhms-scheduler-state-index-capacity.{service,timer}` as specified in design.md.
- [ ] 2.2 Tests:
  - an exact-field pin for both files, including the `-m` ExecStart form;
  - both files are added to the node-22 governed bare-`uv` scan and the substituted-python scan.

## 3. Docs and decision

- [ ] 3.1 Runbook §8.12:
  - replace "定时器是后续工作，当前靠人工节奏" with the timer model;
  - add install and enable steps:
    - `install -d -m 0700` the receipt root;
    - `install -m 0644` the units into `~/.config/systemd/user/`;
    - `daemon-reload`;
    - `enable --now` the timer;
  - explain how to read a failed unit (`systemctl --user status`, `journalctl --user -u`, the receipt `latest.json`);
  - state that an alert means running the existing dry-run → review → enforce flow, except `capacity_warning_unprunable`, which escalates to #2541;
  - record the stage 2 decision with its rationale.
- [ ] 3.2 Post the stage 2 decision on #2653, as part of the PR (orchestrator).

## 4. Deploy (orchestrator, operator-confirmed)

- [ ] 4.1 After merge, with operator confirmation, because pulling node-22 also deploys every other undeployed master change:
  - `git pull --ff-only` on node-22;
  - re-`grep` the live `compute.scheduler-dbfree.env` for the four keys;
  - create the receipt root with `install -d -m 0700`;
  - install and enable the units;
  - run the service once;
  - capture the unit result and receipt (both lanes' capacity and `removed_state_ids_sha256`) on the issue.

## Evidence Floor

- **Local:**
  - behavior tests fail against the base (the wrapper does not exist there) and pass afterwards;
  - `uv run pytest -q` on the new test file, `tests/test_node22_entrypoint_invariant.py`, `tests/test_node22_entrypoint_invariant_python_scan.py`, the state-index repair / prune suites, and `tests/test_select_ci_tests.py`;
  - `uv run ruff check .`;
  - `openspec validate state-index-capacity-watch --strict --no-interactive`.
- **node-27** (`TMPDIR=/home/nwm/tmp`): the same suites.
- **node-22:** the 4.1 live unit receipt.
