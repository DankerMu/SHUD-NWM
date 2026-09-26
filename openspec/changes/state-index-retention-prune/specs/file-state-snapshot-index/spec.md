## ADDED Requirements

### Requirement: State snapshot index retention pruning SHALL preserve every reader decision it can affect

The state-index repair entrypoint SHALL provide a `prune-retention` operation. It SHALL use the same two-lane topology, defaults, locks, archive-before-CAS, production publisher, read-back, receipt, and exit-code contract as the other repair operations.

It SHALL accept `retention_days` (default 21) and a scheduler `cycle_lag_hours`. It SHALL refuse with a zero-index-write refusal when `cycle_lag_hours` is not provided. It SHALL also refuse with a zero-index-write refusal when `retention_days × 24` does not exceed the scheduler's maximum lookback hours plus `cycle_lag_hours` plus 48.

Each lane SHALL be planned from its own validated entries. Entries SHALL be grouped by the validated identity key's `(model_id, normalized source_id)` prefix. Generations SHALL be the reader-normalized `model_package_checksum` (absent = empty string), ordered by `(valid_time, state_id)`. Within a group, an entry SHALL be retained when any of these holds:

- its `valid_time` is at or after the group's newest `valid_time` minus `retention_days`;
- it is its generation's earliest usable entry, latest usable entry, or latest usable entry before that window start;
- it is the first usable entry of a maximal run of consecutive usable entries sharing one generation;
- it carries `cloned_from_model_id`;
- its `state_id` is named by a retained entry's `cloned_from_state_id`.

All other entries SHALL be removed from the index only, and referenced state objects SHALL NOT be deleted. Retained entries SHALL be republished as their original raw mappings in their original relative order. The reference lane SHALL be written before the destination lane. A lane with nothing to remove SHALL be left untouched and recorded as such.

For every group and every cutoff instant, pruning SHALL leave these unchanged:

- `usable_state_history_evidence.history_exists`;
- `generation_scoped_history_signal.history_exists_any_generation`, `history_exists_current_generation`, and the latest any-generation checkpoint's package checksum;
- `clone_lineage_signal` `has_lineage`, `predecessor_model_id`, and `cutover_valid_time`.

The scheduler's generation transition decision SHALL obey these rules:

- At every cutoff, it SHALL never move from a block to an admit, and never between warm-admit and cold-admit.
- At cutoffs at or after the window start, it SHALL be identical.
- Before the window start, the only permitted changes SHALL be from an admit to a block or from one block reason to another block reason.

For cutoffs at or after the window start, every generation-scoped signal field and every strict warm-start answer SHALL also be unchanged. The only exception is the evidence-only history entry counts and the nested index evidence block (checksum, counts, capacity), which no decision reads.

The preview and receipt SHALL report per lane:

- entry, JSON-node, and byte counts before and after;
- the removed count;
- a per-group removal summary;
- a digest of the removed `state_id` list.

The enforce receipt SHALL stay within the receipt size limit.

#### Scenario: History, generation, and lineage answers survive a prune

- **WHEN** an index is pruned whose groups include interleaved and rolled-back package generations, an empty-checksum generation, clone-provenance rows, a dormant group, runs of unusable rows straddling the window start, and mixed source-id spelling
- **THEN** at every cutoff one second before, at, and one second after each distinct `valid_time`, the history-existence, latest-generation-checksum, and lineage answers are identical before and after, the transition decision is identical, and in-window cutoffs return identical signal and strict warm-start answers

#### Scenario: A pruned old checkpoint fails closed instead of admitting

- **WHEN** a manual retry asks for the exact warm-start checkpoint of a cycle older than the retention window whose entry was pruned
- **THEN** strict warm start reports `state_snapshot_index_exact_checkpoint_missing` while history existence stays true, and the transition decision is a block, never a warm or cold admit

#### Scenario: Window boundary and floor

- **WHEN** one entry sits exactly at the group's newest `valid_time` minus `retention_days` and another non-anchor entry sits one second earlier
- **THEN** the first is retained and the second is removed
- **WHEN** a request's window does not exceed the maximum lookback plus cycle lag plus 48 hours, or omits the cycle lag
- **THEN** it is refused before any lock, archive, or index write

#### Scenario: Dry-run is mutation-free and enforce is archive-first

- **WHEN** an operator previews and then enforces `prune-retention`
- **THEN** the preview changes no filesystem bytes and lists every removed `state_id`, and enforce archives both exact pre-images before the first CAS, prunes reference before destination, and reads back exactly the planned raw entries

#### Scenario: Destination failure after reference prune is partial

- **WHEN** the reference prune commits and the destination CAS then fails
- **THEN** the entrypoint reports a partial incomplete outcome with the non-refusal exit code, the reference lane is pruned, and the destination lane is byte-identical

### Requirement: State snapshot index evidence SHALL expose capacity against its hard limits

Every state-index evidence block and every publish result SHALL carry a `capacity` object. The object SHALL contain the entry count, the JSON-node count, and the byte size, together with the corresponding hard limits. It SHALL also contain the highest utilization ratio, the `0.70` warning threshold, and a boolean `warning`. A logger warning SHALL be emitted when `warning` is true. Capacity reporting SHALL NOT make any reader or publisher fail earlier than the existing hard limits.

#### Scenario: Capacity warning at 70 percent

- **WHEN** an index's highest utilization ratio across entries, JSON nodes, and bytes is at or above 0.70
- **THEN** the evidence reports `warning: true` with the ratio, a warning is logged, and reads and publishes still succeed; below 0.70 `warning` is false
