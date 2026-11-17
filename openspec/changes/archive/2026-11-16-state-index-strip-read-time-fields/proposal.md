# state-index-strip-read-time-fields (#3123)

## Why
Reading a state snapshot index injects two in-memory evidence fields into every entry: `index_generated_at` (a
string) and `object_evidence` (null on unverified reads). `upsert_state_snapshot` and `set_usable_flag` load entries
through `_load_entries_for_update`, which does not strip them, and then publish them back through `_publish_entries` →
`publish_state_snapshot_index`. The private index therefore persists two extra JSON nodes per entry.

Observed on node-22 on 2026-11-11, with 12154 entries per lane:
- the private lane had 252205 json_nodes;
- the shared lane had 227897;
- the difference is 24308, exactly 2 × 12154.

That is about 10% of capacity wasted against the 300000-node hard limit, so fail-closed (state save QC,
`set_usable_flag`, warm-start reads) arrives earlier. `prune-retention` strips the fields, but the next
`set_usable_flag` writes all of them back.

Copyback (`state_manager.py` ~:2633), `remove-entry` and `prune-retention` already strip both fields. The living spec
states the rule for prune ("republished as their raw mappings, excluding only the read-time injected
`index_generated_at` and `object_evidence` fields, as every repair operation does").

## What
1. Strip the two read-time fields in the **single writer**, `publish_state_snapshot_index`, before the payload is
   built (the checksum at ~:2150 is computed afterwards, so it stays self-consistent). Every publish path then stops
   persisting them:
   - snapshot upsert and `set_usable_flag` (`_publish_entries`);
   - the scheduler file-provider refresh renewal (`validated_entries_for_renewal` ~:1987, republished by
     `scripts/scheduler_refresh/runner.py:555`), which also returns unstripped entries today;
   - copyback, repair, remove-entry and prune.
   **The read-side strips (`_copyback_raw_entries` ~:2633 and the repair read-back ~:3178) are still required.** While
   legacy files that carry the fields exist, they keep the copyback equality checks (~:2451, ~:2488) and the repair
   read-back comparison (~:3064) from reporting false conflicts. Do not remove them.
2. **Env templates** (from the #3122 review, recorded on the issue): add `NHMS_SLURM_SCHEDULER_STATE_INDEX` (the private
   lane) to `infra/env/compute.scheduler-dbfree.env.example`, and to any other template that carries
   `NHMS_SCHEDULER_STATE_INDEX` for a Slurm `state_save_qc` lane. Use the node-22 production value
   `/scratch/frd_muziyao/nhms-prod/object-store/scheduler/state-index/index-last.json`, already present in the live
   `compute.scheduler-dbfree.env:98` and `compute.replay.env:91`. When the key is missing, `chain_manifests.py:339-340`
   falls back to the shared index, so an env rebuilt from the template would make `state_save_qc` rewrite the shared
   lane. Keep the env drift-table and template tests green, and add the key to the `infra/env/README.md`
   state-index key list (~:334-340).
3. **Runbook**: in `docs/runbooks/production-ops/known-issues-state-index.md`, rewrite the write-back description
   (~:525-528) and the "reference lane 的回弹与余量" section (~:885-892) as historical, i.e. true before #3123. State the
   post-fix per-entry cost (the copyback shape, about 18.5 nodes/entry) as current.

Reading is unchanged: validation still re-injects both fields in memory on every read, so in-memory consumers such as
the warm-start evidence `object_evidence` (~:1446) see the same values.

An index that already carries the fields still validates, because reads accept and overwrite them. It is cleaned on
its next publish. There is no migration.

## Non-goals
- Removing the read-side strips (still required for legacy files; see What 1).
- Reported, not fixed: node-22 live `compute.replay.env` sets `NHMS_SLURM_SCHEDULER_STATE_INDEX` twice (lines 91 and 124,
  same value). It is a live config file, so it is left to the operator.
- Changing the capacity limits.

## Triage
- Issue type: bug (persisted-format bloat).
- Fixture level: **standard**, a one-function change to the shared state-index writer used by node-22 production.
- Risk packs: Legacy compatibility (existing indexes with the fields), Persistence format.
- Blast radius: every state-index publish. The failure to rule out is a payload checksum or validation regression
  that refuses a publish.

## Evidence Floor
- [x] E1 `uv run ruff check --no-cache .` — `All checks passed!`.
- [x] E2 New tests. Each one is red on the old code (record the red run). **Legacy fixtures are seeded as raw
  bytes** (a valid checksum written directly to disk, with an assertion that the fields are on disk), never through
  the publisher, which would strip them after the fix.
  - Upsert: seed at least one existing entry, upsert a new one, and assert the **existing** entries are persisted
    without either field. On the old code only the reloaded existing entries carry the fields, so an empty-index test
    would be vacuous.
  - `set_usable_flag`: the same assertion over all entries.
  - Writer-level: `publish_state_snapshot_index([entry with both fields])` writes a file without them, and reading it
    back passes checksum validation with both fields injected in memory.
  - Legacy: a raw-bytes index carrying the fields still reads, and is clean after one publish.
  - With entries that carry all normalized keys, json_nodes after `set_usable_flag` equals the same entries published
    by copyback.
  - Not new tests, so the red-on-old rule does not apply: re-seed the existing legacy-strip tests as raw bytes so
    they keep testing the read-side strips:
    `tests/test_run_tree_copyback.py:330-355` and `tests/test_state_index_retention.py:105-121,150-162` (SHAPES
    16/19/20). Run them, the selector set for the diff, and the state-index suites
  (`tests/test_state_*.py` touching the index, `tests/test_file_state_snapshot_index*.py` if present), all green
  locally. — red-on-old for all 7 new tests (json_nodes `(56, 50) == (50, 50)`). Copyback read-side-strip guard mutation: red (`['legacy-state'] == []`) with the strip removed, green when restored. Local: 6592 passed / 4 skipped (selection + named suites), then 1218 + 274 passed after the fix pass with the new file tracked.
- [x] E3 The same set passes in a node-27 temporary worktree. — node-27 under umask 0002: 46 files at 880b79bea gave 5856 passed and 1 failed (selector support-module closure; fixed in 3f6f155ec). Rerun of the 5 affected files at 3f6f155ec: 1293 passed, 0 leftover DBs.
- [x] E4 node-22, read-only, after merge and pull (receipt in the archive PR). After the first `state_save_qc`
  upsert under the new code, count the private index entries that carry either key, using
  `/scratch/frd_muziyao/NWM/.venv/bin/python -I -c` (no `uv run`, no `uv sync`). The result must be 0.
  — receipt: node-22 at 5988ef56b; the private index republished 2026-11-17T04:27:53Z (daily file-provider refresh
  after the scheduler pass) holds 9672 entries, 0 carrying either key.
- [x] E5 `openspec validate state-index-strip-read-time-fields --strict --no-interactive`. — valid.
