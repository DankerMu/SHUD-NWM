## Design

Change surface: `repair_state_snapshot_index` (new operation `prune-retention`), `_plan_state_index_repair` (new planner branch), `scripts/scheduler_state_index_repair.py` (CLI), `_StateIndexSnapshot.evidence` / `publish_state_snapshot_index` result (capacity fields).

Must preserve:
- Every existing repair operation, flag, receipt field, exit code (0/2/3) and lock order.
- Hot-path publishers (`upsert_state_snapshot`, `set_usable_flag`, `merge_state_snapshot_index_copyback`, `publish_state_snapshot_index`) and every reader's validation defaults. Capacity fields are additive evidence and never fail earlier than the existing caps.
- Surviving entries republished as their raw mappings in their original order, never re-normalized (production mixes 20/19/16-key entry shapes).

Must add:
- `prune-retention` over both lanes, each planned independently from its own validated entries (the lanes are not whole-file mirrors); reference pruned before destination. A lane with nothing to remove is `skip`/untouched with reason `nothing_to_prune`; a no-op enforce touches neither lane.
- Grouping: validated identity-key prefix `(key[0], key[1])` (normalized source id — production carries both `gfs` and `IFS`), never raw strings. Generation key: exactly the reader's normalization `str(entry.get("model_package_checksum") or "")`, so checksum-less entries form the real generation `""`. Ordering: `(valid_time, state_id)`, the readers' own sort key. `created_at` is never used (null on every production entry).
- Window: `W_g = newest valid_time in group g − retention_days`.
- Retention predicate — an entry of group g is kept iff at least one holds:
  - K1 `valid_time ≥ W_g` (in-window);
  - K2 for its generation: it is the earliest usable, the latest usable, or the latest usable with `valid_time < W_g`;
  - K3 it is the first usable entry of a maximal run of consecutive usable entries (ordered as above) sharing one generation;
  - K4 it carries `cloned_from_model_id`;
  - K5 its `state_id` is the `cloned_from_state_id` of a kept entry (any group).
- Window floor (checked precondition, zero-write refusal `repair_retention_window_too_short`): `retention_days × 24 > MAX_LOOKBACK_HOURS (336, services/orchestrator/scheduler.py) + cycle_lag_hours + 48`, the 48 h being one `required_lead_hours` step (≤24 with allowed cycles 0,12) plus the self-heal producer probe (≤24). `cycle_lag_hours` comes from `--cycle-lag-hours` or `NHMS_SCHEDULER_CYCLE_LAG_HOURS`; missing → refusal `repair_cycle_lag_unset`. Default `retention_days = 21` (504 h > 336+16+48 = 400 h on node-22, where lanes run lookback 32 h / replay 288 h, lag 16 h). Deriving from `MAX_LOOKBACK_HOURS` rather than one env file covers every scheduler lane, including the 288 h replay lane.
- Preview/receipt per lane: `retention` block with `retention_days`, `cycle_lag_hours`, entry / JSON-node / byte counts before and after, removed count, per-group removal summary (`model_id`, `source_id`, removed count, removed valid_time min/max, kept count), digest of the sorted removed `state_id` list. Dry-run stdout additionally lists every removed `state_id`; the enforce receipt carries only the summary + digest and stays under `MAX_RECEIPT_BYTES`.
- Capacity evidence: `capacity = {entry_count, max_entries, json_nodes, max_json_nodes, index_bytes, max_bytes, utilization_ratio, warning_threshold: 0.70, warning}`; `logger.warning` once per load when `warning` is true.

Governing invariant (for every group, every generation checksum present, every cutoff c):
- All c (proof: K2-earliest keeps the minimum-vt usable entry per generation; K3 keeps the start of the run owning the latest usable entry ≤ c, and every later run starts after c; K4 keeps every clone row): `usable_state_history_evidence.history_exists` (strict `<`); `generation_scoped_history_signal.history_exists_any_generation` / `history_exists_current_generation` (`≤`); `latest_any_generation_checkpoint.model_package_checksum` (read at scheduler_generation.py:1174-1177 for `BLOCK_DECLARATION_STALE` vs `COLD_DECLARED_CUTOVER`); `clone_lineage_signal` `has_lineage` / `predecessor_model_id` / `cutover_valid_time`. Decision rule for `evaluate_transition_decision`: at every cutoff it never moves block → admit and never warm ↔ cold; at c ≥ W_g it is identical; at c < W_g the only permitted moves are admit → block and block → another block reason (e.g. `BLOCK_WRONG_GENERATION` → `BLOCK_PREDECESSOR_PENDING`).
- c ≥ W_g (proof: the latest usable ≤ c is in-window or is its generation's latest usable before W_g, K2): every other `generation_scoped_history_signal` field, `strict_warm_start_evidence`, and `usable_state_history_evidence.latest_usable_state` are identical — EXCEPT the evidence-only count/index fields, which legitimately change and are read by no decision (grep: consumers only inside state_manager.py): `history_entry_count` (`usable_state_history_evidence`), `history_entry_count_any` / `history_entry_count_current`, and the nested `state_snapshot_index` evidence block (checksum, entry_count, index_bytes, capacity).
- c < W_g, accepted changes (reachable only via an unbounded manual retry of a cycle older than the window): a pruned exact checkpoint reads `state_snapshot_index_exact_checkpoint_missing`; `has_exact_predecessor` becomes false; a pruned wrong-generation entry at the expected key turns `BLOCK_WRONG_GENERATION` into `BLOCK_PREDECESSOR_PENDING` (may emit §8.6 predecessor work). The candidate still blocks; it never warm- or cold-admits.

Sibling surfaces:
- Successor-checkpoint continuity (`scheduler_core.py:873-921` → `scheduler_candidates.py:435/490/3004`, `scheduler_discovery.py:402`): strict lookups at successor times, bounded by the discovery lookback, so the window floor keeps every such lookup in-window.
- `merge_state_snapshot_index_copyback`: union-by-key, but every production caller scopes it with `authoritative_run_ids`. A replay of an old run can resurrect only that run's rows, which is why both lanes are pruned reference-first.
- `scripts/scheduler_refresh/runner.py:264` (`validated_entries_for_renewal`) republishes the full list under the CAS preimage, so a concurrent prune makes it fail CAS rather than resurrect entries.
- `apps/api/routes/state_snapshots.py` `GET /state-snapshots/{state_id}` returns 404 for pruned ids. This is a known limitation, and the archived preimage keeps them.
- New backdated clones (`scripts/node22_clone_direct_grid_cutover_states.py --cutover-time`, `state_clone.py:545-551`) need M0's exact entry at the cutover. A cutover older than M0's window and not an anchor is refused with `missing_qualified_source` (fail-closed). This is a runbook limitation: choose a cutover inside the source window or at an anchor, or restore from the archive. `get_latest_state_before` may change its refusal label, which is refusal-to-refusal.
- Checkpoint-chain holes lying entirely before W_g lose their predecessor. They are beyond the discovery lookback (floor), so §8.6 could not reach them anyway. A hole spanning W_g keeps its predecessor via K2 (latest usable before W_g).
- `upsert_state_snapshot` / `set_usable_flag` share `.index-last.json.lock` with the repair enforce path, so they serialize.

Seams under test: `repair_state_snapshot_index(operation="prune-retention", …)`, the CLI `main([...])`, the public `FileStateSnapshotIndexRepository` reader methods before/after the prune, `evaluate_transition_decision` fed by the before/after signals, and `state_index_evidence()` / `publish_state_snapshot_index` capacity fields.

Required evidence:
- Property test. Build a multi-group index with interleaved generations (A, B, A rollback), a `""` generation, clone rows, a dormant group, runs of unusable rows straddling W_g, and mixed `gfs`/`GFS` spelling. Prune it. Then for cutoffs {vt − 1 s, vt, vt + 1 s} of every distinct valid_time, the all-c fields above are identical, and for c ≥ W_g the full signal/strict-evidence payloads (minus index checksum/count evidence) are identical.
- Decision test: `evaluate_transition_decision` before and after the prune is identical for the all-c cases. For the out-of-window accepted cases the decision is a block (never admit).
- Boundary: an entry exactly at W_g is kept; a non-anchor entry one second earlier is removed; each of K2–K5 keeps an out-of-window entry.
- Window refusal: `retention_days × 24 ≤ 336 + lag + 48` → refusal, zero writes; a missing lag → refusal.
- Dry-run → zero filesystem changes. Enforce → archive before CAS, reference before destination, read-back equals the planned raw entries.
- A destination CAS failure after the reference commit → partial (exit 3); reference pruned, destination unchanged.
- An upsert landing between dry-run and enforce is not lost.
- Capacity warning at ≥70% true, below false; no reader or publisher fails earlier.
- The production-copy CLI dry-run (node-22 scratch + ghdc copies of 2026-09-25) numbers are in the PR.

Non-goals: deleting state objects; pruning inside publishers; a systemd timer (follow-up); sharding; batch upsert (#2541 option b).

Review focus: (1) K1–K5 completeness vs the governing invariant and decision families; (2) identity-key grouping, generation normalization, sort key; (3) lock/CAS/partial parity with `remove-entry`; (4) raw-entry order preservation; (5) window-floor refusal and receipt size bound.
