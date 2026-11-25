# Tasks

Fixture level: `expanded`. Risk packs: journal cache correctness (#1567: a
shared decode served under a fingerprint it was not read under gives stale
or tampered rows; #1661: no cache may skip the fingerprint); scheduler
verdicts (the rows feed candidate state and completion scoring); aliasing
(one decoded record now backs the rows of every model of a cycle); memory of
the repository instance; concurrency (orchestration threads share one
repository).

Must preserve: for every journal state, `_cycle_rows(...)` returns what a
cold repository returns, for every model and for `model_id=None`, and raises
what it raises (class, token, field) in the same cases; every non-owner
`_cycle_rows` call computes its fingerprint; `_cycle_rows_cache` semantics
(keys, hit rule, fault rule, window rules, cap) are untouched; the
write-window owner reads the segments itself; `_read_cycle_segments`,
`_cycle_segment_paths`, `_read_jsonl`, `_apply_journal_record`,
`_cycle_rows_by_model_unlocked`, `_cycle_journal_records` and every other
caller of `_read_cycle_segments` are not edited; the order in which surfaces,
source segments and records are applied; pass evidence shape
(`journal_read_attribution` keeps its keys and still reconciles per lane);
existing tests stay green unedited, in particular
`tests/test_file_orchestration_journal_read_cache.py`,
`tests/test_cycle_rows_fingerprint_pinned_root.py`,
`tests/test_file_orchestration_journal.py`, `tests/test_production_scheduler.py`.

## 1. Shared decode (`services/orchestrator/file_orchestration_journal.py`)

- [ ] 1.1 The repository holds `_cycle_segment_records_cache`, a dict from
  `(surface, source_segment, cycle_segment)` to
  `(fingerprint, records)`, guarded by `_cache_lock`, bounded by the module
  constant `MAX_FILE_JOURNAL_CYCLE_SEGMENT_RECORDS_CACHE_ENTRIES = 8`, oldest
  entry out first. `surface` is `"journal"` or `"pipeline-events"`. A comment
  at the constant says what an entry holds and why the bound is small.
- [ ] 1.2 A method `_shared_cycle_segment_records(...)` implements the
  design's pseudocode: not shareable -> `self._read_cycle_segments(...)`
  directly, nothing consulted, nothing stored; shareable -> a hit only when
  the stored fingerprint equals the call's; otherwise read through
  `self._read_cycle_segments(...)` (looked up on the instance at call time)
  and store. A directory whose read raises leaves no entry for that
  directory and removes no other entry.
- [ ] 1.3 The miss path of `_cycle_rows` calls that method for the two
  surfaces in today's order, with `shareable = not in_write_window and not
  probe_faulted` (for a non-owner, `probe_faulted` is "the fingerprint is
  the fault marker"). No other statement of `_cycle_rows` changes.
## 2. Spec

- [ ] 2.1 `pipeline-job-persistence` gains the requirement in this change's
  delta.

## 3. Tests (new file `tests/test_cycle_segment_records_sharing.py`)

Fixture journal for these tests: one source, one cycle, five models with
latest views, journal records for each model and at least two model-less
records (a cohort master), pipeline events, by-cycle direct jobs; variants
with rollover segments (`.1`), with both spellings (`source_segments`
resolved by the repository on a case-sensitive filesystem; skipped with a
reason where the filesystem folds case), and with an empty cycle.

- [ ] 3.1 Equivalence. For each variant, read `model_id` in `[m1..m5, None]`
  and in the reverse order on one repository instance, and compare every
  result with the result of a new repository instance reading only that
  model (deep equality of all row collections). Also after a second full
  round on the same instance (all hits of the rows cache).
- [ ] 3.2 One decode per cycle and surface. Count calls of
  `repository._read_cycle_segments` (instance attribute replaced by a
  counting wrapper): five model reads plus one `None` read of one cycle give
  two calls per source segment (one per surface), not twelve per source
  segment. Count
  `repository._cycle_rows_source_fingerprint` the same way: six calls, one
  per read, as before the change.
- [ ] 3.3 A changed file is decoded again. After reading `m1` and `m2`,
  append one valid record for `m3` to the cycle's journal file by writing
  the file directly (not through the repository); the read of `m3` calls
  `_read_cycle_segments` again for both surfaces, returns rows containing
  the new record, and equals a cold repository's result. The entry's stored
  fingerprint has changed.
- [ ] 3.4 Fault. After a stored read, replace `pipeline-events/<source>` by
  a symlink to a copy (the journal surface stays real, so it is decoded
  before the events surface raises): the next read of another model raises
  what a cold repository raises on that tree (same class and token); a
  counting wrapper shows it called `_read_cycle_segments` for the journal
  surface (the stored entry was not served to a faulted read); and no entry
  of the cache carries the fault marker as its fingerprint. Restoring the
  tree with a copy lets the next read store again.
- [ ] 3.5 The owner does not share. Populate the cache, then enter
  `with repository._locked_cycle_write(...)` for the cycle on the same
  thread: two model reads inside the window each call `_read_cycle_segments`
  for both surfaces, and the cache holds the same entry objects as before
  the window (nothing taken, nothing stored, nothing cleared). After the
  window, a read of an unchanged cycle is a shared hit again.
- [ ] 3.6 A failing directory leaves no entry. For a segment gap
  (`<cycle>.jsonl` absent, `<cycle>.1.jsonl` present:
  `file_journal_segment_gap`), a slot past the cap (slots `.jsonl` through
  `.3.jsonl` all present: `file_journal_segment_limit_exceeded`) and a
  malformed JSON line (`file_journal_malformed_json`): each of three model
  reads raises the same error as a cold repository, `_read_cycle_segments`
  is called for that directory by each of them, and the cache holds no entry
  for it. With a healthy journal surface and a failing events surface, the
  events error is raised after the journal surface was decoded, the journal
  entry is stored and served to the next model, and there is no events
  entry.
- [ ] 3.7 Bound and key. Reading five cycles with
  `source_segment_override="gfs"` makes ten inserts and leaves eight
  entries: the first cycle's two are gone, the other four cycles' are
  present; journal and events of one cycle are distinct entries.
- [ ] 3.8 Aliasing guard. Build a scheduler over a real file journal the
  way `tests/test_raw_manifest_readiness_scope.py` does for its scoring test
  (`tests/test_scheduler_backfill._build_scheduler(active_repository=
  FileOrchestrationJournalRepository(root), models=[five models])`,
  `_completion_status`, and the db-free cold-start environment that test
  sets with `_set_db_free_cold_start_env`, so that scoring reaches the
  strict/successor branch and calls `candidate_state` instead of returning
  early from `has_completed_pipeline`). The journal carries, besides the
  latest views, benign pipeline-event records (a publish-succeeded event, as
  that test file uses) with a nested `details` mapping and a `sequence`
  greater than the latest views' `replay.latest_sequence`, so that rows are
  built from shared records and not only from latest-view copies; the
  verdict must be what it is without those events. Run completion scoring
  for the cycle; a spy shows `candidate_state` ran for the models and the
  shared cache is not empty. Oracle: for every entry of
  `_cycle_segment_records_cache`, its records deep-equal what a new
  repository instance decodes from the same directory
  (`FileOrchestrationJournalRepository(root)._read_cycle_segments(...)`).
  Positive control with the same oracle: writing into `details` of one of
  those events in a row returned by `_cycle_rows` makes the comparison fail,
  so the oracle is known to see such a write.
- [ ] 3.9 Threads. Four threads each read a different model of the same
  cycle on one repository, started together behind a barrier, repeated 50
  times with a fresh repository: every result equals the cold result, and
  no exception escapes. This is a smoke test: it does not prove the locking.
- [ ] 3.10 Attribution. For five model reads of one cycle, compare the
  read-attribution counters with those of the same sequence run with
  `_shared_cycle_segment_records` replaced by a stand-in that always reads:
  `cache_hit_calls` is lower, `calls` is equal, and in both runs the
  per-lane rows sum to the totals.

## 4. Evidence

- [ ] 4.1 Red first, recorded in `.workplans/3236/red-runs-b2.log`: against
  the unmodified source, 3.2 fails (twelve calls per source segment) and 3.5 / 3.7 fail on the
  missing attribute; 3.1, 3.3 (rows) and 3.6 (errors) pass, because they
  state behaviour that must not change.
- [ ] 4.2 Mutations, applied with an exact-replacement script kept under
  `.workplans/3236/`, each run against the named test, outcome and failing
  assertion recorded, source restored and compared (`cmp`):
  - M1 a hit does not compare fingerprints -> 3.3 red;
  - M2 a faulted read stores (`shareable` ignores the fault) -> 3.4 red (an
    entry under the fault marker);
  - M3 the owner shares (`shareable` ignores `in_write_window`) -> 3.5 red;
  - M4 the key omits the surface -> 3.1 red, by
    `file_journal_record_type_mismatch` when events are replayed as journal
    records;
  - M5 the key omits the cycle segment -> 3.7 red (entry count); wrong rows
    are not possible because the fingerprint names the cycle's slots, and
    the log says so;
  - M6 no eviction -> 3.7 red;
  - M7 a directory whose read raises stores an empty list -> 3.6 red;
  - M8 the helper is bypassed (always reads) -> 3.2 red;
  - M9 oracle probe, in `services/orchestrator/file_journal_replay.py` and
    restored afterwards: `_payload_or_record_payload` returns the decoded
    object itself in both branches (`record["payload"]` for an enveloped
    record, `record` otherwise) and `_with_replay_order` writes into the
    mapping it was given instead of a copy -> 3.8's comparison with a fresh
    decode red.
- [ ] 4.3 Local: the new file;
  `tests/test_file_orchestration_journal_read_cache.py`,
  `tests/test_cycle_rows_fingerprint_pinned_root.py`,
  `tests/test_file_journal_cluster_map.py`,
  `tests/test_file_journal_mixin_seams.py`,
  `tests/test_file_journal_late_lookup_seams.py`,
  `tests/test_gateway_reconcile_inventory.py`,
  `tests/test_gateway_reconcile_round10.py`,
  `tests/test_file_orchestration_journal.py`; from
  `tests/test_production_scheduler.py` the tests selected by `-k "completion
  or journal_read_attribution or read_attribution"`; `uv run --no-sync ruff
  check --no-cache .`; `openspec validate
  share-decoded-cycle-segments-across-models --strict --no-interactive`; the
  repository entropy hard gate at its baseline.
- [ ] 4.4 node-27: the same files in a temporary detached worktree, without
  the integration opt-in (no database is created or dropped). Receipt on the
  PR.

After merge, before the production checkout moves (owed on #3236, not a
merge gate): one plan-only pass of the merged commit from a separate clone
at a pass boundary, with the sampling profiler. Status, counts and the
per-source backfill audit identical to the neighbouring production passes;
wall and CPU; samples under `_read_cycle_segments` against the prediction (a
few dozen, from 1,729 after B1); `journal_read_attribution` totals before
and after (`cache_hit_calls` lower, `calls` unchanged).
