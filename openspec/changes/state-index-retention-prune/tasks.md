# Tasks — state-index-retention-prune (#2548, #2614)

## Risk packs

- Public API / CLI / script entry — selected: new repair operation + `--retention-days` → CLI tests (2.x).
- Config / project setup — not selected: no env/config key added (CLI flag only).
- File IO / path safety / overwrite — selected: rewrites both production index lanes → archive-first/read-back tests reuse production publisher (2.3, 2.4).
- Schema / columns / units / field names — not selected: index payload format unchanged; capacity fields are additive evidence (3.1 asserts no reader change).
- Auth / permissions / secrets — not selected: runs as provider owner under existing gates.
- Concurrency / shared state / ordering — selected: shared lock with upsert, reference→destination order → 2.5.
- Resource limits / large input / discovery — selected: the whole point; receipt size bound + capacity evidence → 2.6, 3.1.
- Legacy compatibility / examples — selected: raw entry shapes (16/19/20 keys) and order preserved → 2.2.
- Error handling / rollback / partial outputs — selected: partial outcome after reference commit → 2.4.
- Release / packaging / dependency — not selected.
- Documentation / migration notes — selected: runbook §8.12 → 4.1.

## 1. Retention planner (module: `packages/common/state_manager.py`)

- [x] 1.1 Add `prune-retention` to `STATE_INDEX_REPAIR_OPERATIONS`; `repair_state_snapshot_index(..., retention_days=21, cycle_lag_hours=None)`; refuse non-int/unset lag (`repair_cycle_lag_unset`) and `retention_days*24 <= MAX_LOOKBACK_HOURS + cycle_lag_hours + 48` (`repair_retention_window_too_short`, import the constant from its owner — no restated literal) before any lock; selectors/lane/missing-lane flags are not applicable to this operation (refuse).
- [x] 1.2 Planner branch: per lane, group by validated key `(key[0], key[1])`, generation = `str(entry.get("model_package_checksum") or "")`, order `(valid_time, state_id)`; apply K1–K5 (design.md); produce retained raw entries in original order plus the retention preview block; `skip` with `nothing_to_prune` when nothing is removed.
- [x] 1.3 Enforce reuses the existing locked re-read, archive, reference→destination CAS, read-back, and partial/uncertain classification unchanged.
- [x] 1.4 Capacity evidence on `state_index_evidence()` (stored on `_StateIndexSnapshot.capacity`, NOT on the nested `_StateIndexSnapshot.evidence` block every reader embeds), `validated_entries_for_renewal` evidence, the `publish_state_snapshot_index` result, and each prune lane's `retention.capacity_before/after`; one `logger.warning` per load at ≥0.70.

## 2. Tests (new `tests/test_state_index_retention.py`; lock parents via `tests.provider_mode_helpers`)

- [x] 2.1 Governing-invariant property test over cutoffs {vt−1s, vt, vt+1s} for every distinct valid_time: all-c fields identical (history_exists, any/current-generation existence for every checksum, latest_any checksum, lineage); c ≥ W_g full generation signal + strict warm start identical. Fixture: interleaved generations A,B,A; `""` generation; clone rows; dormant group; unusable runs straddling W_g; mixed `gfs`/`GFS`.
- [x] 2.1b Decision test: `evaluate_transition_decision` (services/orchestrator/scheduler_generation.py) fed before/after signals at every cutoff of 2.1 obeys the one-way rule — never block→admit, never warm↔cold; identical at c ≥ W_g (incl. a stale-declaration `BLOCK_DECLARATION_STALE` vs `COLD_DECLARED_CUTOVER` case); at c < W_g only admit→block (e.g. `WARM_CONTINUE`→`BLOCK_PREDECESSOR_PENDING`); strict warm start for a pruned old exact checkpoint → `state_snapshot_index_exact_checkpoint_missing`. In-window payload comparison excludes only `history_entry_count*` and the nested `state_snapshot_index` evidence block.
- [x] 2.2 Boundary: exactly-at-window kept, 1 s older non-anchor removed; each of K2 (earliest / latest / latest-before-window per generation, incl. the `""` generation), K3 (run starts), K4, K5 keeps an out-of-window entry; unusable non-anchor rows removed and never create a run start (K3 over the usable subsequence); raw mappings and order preserved for 14/17/18 and 16/19/20-key shapes; a second prune after enforce (anchor and invariant fixtures) removes nothing in both lanes.
- [x] 2.3 Dry-run changes no bytes (snapshot of both roots' tree incl. no lock/archive/receipt files) and lists removed ids; enforce archives both preimages before first CAS and read-back equals plan.
- [x] 2.4 Destination CAS failure after reference commit → partial/incomplete (CLI exit 3), reference pruned, destination byte-identical; window too short (e.g. 16 days with lag 16) and missing lag → refusal exit 2, zero writes; nothing-to-prune → both lanes untouched.
- [x] 2.5 Upsert landing between dry-run and enforce survives enforce (enforce re-plans under the shared lock).
- [x] 2.6 Enforce receipt for a ≥5k-removal index stays under `MAX_RECEIPT_BYTES`; per-lane group summaries have a byte budget with a `group_summaries_truncated` count, and both lanes over budget still write the receipt (exit 0).
- [x] 2.7 CLI `main(["prune-retention", ...])` dry-run/enforce round trip including `--retention-days`.

## 3. Capacity evidence

- [x] 3.1 Warning true at ≥0.70 / false below for each of entries, nodes, bytes; reads and publishes still succeed at 0.95; reader answers' nested `state_snapshot_index` carries no `capacity`; renewal evidence does.

## 4. Docs and #2614

- [x] 4.1 Runbook `docs/runbooks/production-ops/known-issues-state-index.md` §8.12: dry-run → review → enforce procedure (source `compute.scheduler-dbfree.env` for lag), receipt fields, cadence (rerun when capacity `warning` fires), known limitations (backdated clone cutover older than source window → `missing_qualified_source`; `GET /state-snapshots/{id}` 404 for pruned ids; restore from archive).
- [x] 4.2 #2614: `_seed_index` uses `make_directory_with_explicit_mode`; `provider_atomic.py` gates untouched.

## Evidence Floor

- node-27: `(umask 0002; TMPDIR=/home/nwm/tmp uv run pytest -q tests/test_state_index_upsert_cost.py tests/test_state_index_retention.py)` all pass; same with `umask 0022`.
- node-27: `uv run pytest -q tests/test_state_manager.py tests/test_scheduler_state_index_repair.py` (existing repair suites) pass.
- local: `uv run ruff check .`; `openspec validate state-index-retention-prune --strict --no-interactive`.
- Production-copy CLI dry-run (2026-09-25 node-22 scratch + ghdc copies) numbers in PR body; node-22 real dry-run after merge; `--enforce` on production only after the operator approves the reviewed dry-run.
