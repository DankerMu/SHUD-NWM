## Why

The db-free file state snapshot index (`scheduler/state-index/index-last.json`) has never been pruned (#2548). On 2026-09-25 the node-22 private index held 9,950 entries / 203,581 JSON nodes / 11.33 MB; both publish-time caps (`MAX_STATE_SNAPSHOT_INDEX_JSON_NODES=300_000`, `MAX_STATE_SNAPSHOT_INDEX_BYTES=16 MiB`) bind together at about 14.6k entries, and growth is 192 entries/day, so every `state_save_qc`, `set_usable_flag`, copyback, and warm-start read fails closed around 2026-10-20. Separately (#2614), `tests/test_state_index_upsert_cost.py::_seed_index` creates the provider lock parent with a bare `mkdir`, so 8 of its 9 tests are always red on the node-27 oracle (umask 0002).

## What Changes

- Add a `prune-retention` operation to the existing two-lane state-index repair entrypoint (`repair_state_snapshot_index` + `scripts/scheduler_state_index_repair.py`). It reuses dry-run by default, `--enforce`, the reference→destination lock order, archive-before-CAS, the production publisher, read-back and receipts, and takes `--retention-days` (default 21). It refuses any window that does not exceed the deepest scheduler lookup: `MAX_LOOKBACK_HOURS` (336) + `cycle_lag_hours` + 48 h of lead/probe step-back.
- The retention predicate works per `(model_id, source_id)` identity group. It keeps every entry within `retention_days` of that group's own newest `valid_time`, and also keeps a small set of anchors: per `model_package_checksum` generation, the earliest usable entry, the latest usable entry, and the latest usable entry before the window start; the first usable entry of every maximal same-generation run; every clone-provenance entry; and every clone-source entry. Everything else is removed from the index only. State objects are never deleted.
- Add capacity evidence (entry / JSON-node / byte counts vs limits, utilization ratio, `warning` at ≥70%) to the state-index evidence block every reader already embeds, plus a logger warning.
- Fix #2614: `_seed_index` creates its directories with `tests.provider_mode_helpers.make_directory_with_explicit_mode`.
- Runbook section for the operator cadence.

## Triage

```text
Issue type: bugfix (+ test fix #2614)
Fixture level: expanded
Upstream suggested level: absent
Blast radius: a wrong prune flips warm-start / cold-start / lineage decisions for every model, or corrupts both production index lanes
Selected risk packs: Public API/CLI entry; File IO/path safety/overwrite; Concurrency/shared state/ordering; Resource limits/large input; Error handling/rollback/partial outputs; Documentation/migration notes
Evidence floor: tests/test_state_index_retention.py + tests/test_state_index_upsert_cost.py + tests/test_state_manager.py repair suites green on node-27 under umask 0002 and 0022; CLI dry-run on production-copy indexes reports the before/after counts; ruff clean
```

## Impact

- `packages/common/state_manager.py`: new repair operation + planner, capacity evidence.
- `scripts/scheduler_state_index_repair.py`: new operation choice and `--retention-days`.
- `tests/test_state_index_retention.py` (new), `tests/test_state_index_upsert_cost.py`.
- `docs/runbooks/production-ops/known-issues-state-index.md`: a new §8.12 operator procedure.
- No schema or payload format change. Hot-path publishers (`upsert_state_snapshot`, `set_usable_flag`, copyback) are unchanged.
