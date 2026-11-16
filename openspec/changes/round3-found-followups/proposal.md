# round3-found-followups (#3164)

## Why

Round-3 cleanup turned up issues that were reported but never filed. Owner decision (2026-11-16): handle them all in
one PR and **retire `station_set_flip`**. The owner also confirmed that the node-22 `nhms-compute-api` and
`nhms-slurm-gateway` restart, done at a pass boundary, is an ops step outside this PR.

## What (one verdict per item)

1. **z_policy verdict tests, the master regression introduced by #3150.** Two tests in
   `tests/test_mapping_builder_z_policy_verdict.py` pin `openspec/changes/direct-grid-build-enablement/...`. They are
   changed to accept the archive relocation that production already resolves (`ARCHIVE_VERDICT_GLOB`): assert that
   the resolved path equals `DEFAULT_VERDICT_PATH` or matches `ARCHIVE_VERDICT_GLOB`, and read the bytes from the
   resolved path. Production code is unchanged.
2. **Retire `station_set_flip` (decision A).** No production code registers the hook, so it never ran. The production
   readers of `active_flag` (`workers/forcing_producer/store.py::load_met_stations`, which selects the legacy set, and
   `packages/common/object_store_forcing.py`, which desensitizes the 404 details) do not depend on the hook, so
   deleting it changes no behaviour. Delete:
   - `packages/common/station_set_flip.py`;
   - its suites `tests/test_direct_grid_display_cutover_flip_atomic.py` and `..._flip_mvt_set.py`, and
     `tests/direct_grid_display_cutover_flip_helpers.py`;
   - the selector rules, tables and pins for them;
   - the flip hook's import and its UPDATE-only lock test in `tests/test_direct_grid_display_cutover_history.py`;
   - the selector rules, tables, pins and re-exports for them, and the `line_reference_baseline.json` and
     `full_test_durations.json` entries.

   Specs:
   - retire the `station-set-atomic-flip` capability;
   - REMOVE the two `display-cutover-rehearsal-receipt` requirements that drive the flip. The rehearsal was a one-time
     certification and its receipt stays archived. Its flow-curve continuity deferral stays live, bound to the pilot's
     first real cutover;
   - `ci-test-selector-routing` and `ci-contract-baseline` stop naming the flip owner;
   - `atomic-cutover-transaction` drops its flip-ordering scenario;
   - `variant-activation-cutover` records that the mount slot stays a reserved no-op.

   **The `station_flag_flip` mount point stays, as a default no-op.** Dropping it means editing
   `tests/test_variant_activation_cutover.py` (1770 lines, not excluded from the large-file guard, and the guard blocks
   any staged change to such a file). That work goes to #3165, which splits the test and then drops the
   slot.

   The cutover lifecycle, the state-clone hook and the producer guard that mirrors stay `active_flag=false`
   (`fixed-station-forcing-production`) are kept. Migration 000033 and its index are kept, because dropping them needs
   a node-27 DB write; this is recorded in the inventory. Archived evidence is unchanged.
3. **Retired archive lane (`NHMS_ARCHIVE_ROOT`).** ADR 0002 retired it on 2026-08-11, and no code reads it.
   - `timeseries-product-archive`: keep "The product-archive mover capability is permanently retired" and REMOVE the
     remaining requirements.
   - `archive-rebuild-drill`: the same shape (a retired requirement over stale ones). Keep the retirement
     requirement and REMOVE the rest.
   - `db-export-salvage`: retire the capability.
   - `timeseries-db-retention`: REMOVE "Retention enforcement is hard-gated on archive receipts". It contradicts the
     same spec's explicit-`disabled` requirement and the code.
   - `orchestrator-structural-burndown`: MODIFY the partition requirement so it covers only the refresh-timer corpus,
     because the flip corpus is deleted.
   - `runtime-storage-source-canonicalization`: replace "Durable artifacts use object-store root" (REMOVED plus ADDED,
     because the validator refuses a MODIFIED that drops scenarios) with a requirement that has no archive tier.
4. **Stall alert reason.** The `lock_contended_persistent` alert carries the pass evidence's `lock.reason` in its
   receipt when one is present, for example `retired_lock_guard_mode_key:NHMS_SCHEDULER_LEASE_GUARD_MODE` (the key
   it names is the one to set). The lease refusal is written as a `lock_contended` pass (`scheduler_lease.py:229-237`
   feeds `scheduler_runtime.py:717`), so today an operator cannot tell it from ordinary contention. The verdict,
   anchor, exit code and streak are unchanged. Spec: ADDED requirement in `scheduler-stall-alerting`.
5. **`scheduler_lease._guarded` fd leak.** `parent_fd` is opened at `:407` before `_file_lock_guard_mode()` at `:408`,
   which can raise, so the fd leaks. Resolve the mode before opening the fd.
6. **The umask-fragile test.** `tests/test_node22_env_backup_sweep.py::test_script_static_contract` checks the
   committed git mode (`git ls-files -s` = `100755`) and that the file is executable. It no longer compares the
   checkout's `S_IMODE` to `0o755`, which fails under umask 0002.
7. **node-22 `compute.env` `DATABASE_URL`: KEEP.** The file is the superseded compose-lane instance of
   `compute.example`. No node-22 unit or container loads it. `infra/env/README.md` says so.
8. **Doc drift:**
   - `docs/spec/03_database_design.md:157-301`: the line references are refreshed or replaced with symbol anchors;
   - old spec names are fixed in `docs/runbooks/qhh-mvp-production-like-e2e-checklist.md` and
     `docs/governance/API_CONTRACT_RETIREMENT_INVENTORY.md` ~:140;
   - ADR 0002's reference to the nonexistent `runtime_storage_source.py` is corrected;
   - the ROLE_BOUNDARY route list gains `precip.py` and `basemap.py`, after verifying their role against the code.

## Rollout

After merge, both checkouts are ff-only pulled. node-22 is pulled at a scheduler pass boundary, with no `uv sync`
(#1831), and gets items 4 and 5. node-27 is pulled and the display API restarted. Item 2 deletes a module that no
production entry point imports; the mount tuple and the lifecycle are untouched. `/health` and `/api/v1/layers` are
compared before and after.

## Non-goals

- Dropping migration 000033's index.
- Dropping the `station_flag_flip` mount point (#3165: split `tests/test_variant_activation_cutover.py`,
  then drop the slot). `fixed-station-forcing-production` lines ~106 and ~113 still mention "the Change 8 flip" as the
  owner of `true`; that wording is refreshed in the same follow-up.
- Item B (#3157), which is a separate issue.
- The conditional flip scenarios in `historical-cycle-display-degradation` (:44-52) and `active-model-dynamic-resolution`
  (:20-23, :46-50). They describe what holds "when the flip runs" and are vacuous now that no flip exists. Their wording
  refresh goes to #3165, together with `fixed-station-forcing-production`.
- Any change to `fixed-station-forcing-production`'s "never escalate `active_flag`" guard.

## Triage

Issue type: tech-debt cleanup.
Fixture level: expanded. The triggers:
- legacy compatibility, through the capability retirements and the hook mount contract;
- test removal;
- spec retirement;
- the scheduler lease code path.

Upstream suggested level: absent.

Blast radius:
- Deleting a hook slot that the cutover lifecycle still requires breaks activation.
- A wrong selector edit drops coverage.
- A wrong spec REMOVED or MODIFIED silently drops a live obligation.
- The stall-text change breaks the alert handler contract.

Selected risk packs: Legacy compatibility; Concurrency (lease fd path); Documentation; Release (two-node pull).

Evidence floor:
- `uv run ruff check .`;
- the selector-chosen set, plus the touched suites, locally and in a node-27 temp worktree;
- `openspec validate --strict` for the change and for every touched spec;
- entropy hard gate;
- after merge, the master full regression shows only the #3153 failure;
- the node-22 and node-27 pull receipts.
