# Tasks — journal-event-log-rotation (#1165)

Fixture level: expanded. Risk packs: contract, test-integrity (selected —
journal is the DB-free source of truth and the prior two issues both
shipped P1s in exactly this file's semantics; performance pack not
selected as a pack, but the read-amplification consequence is a
DECIDED requirement here: segments are bounded, see 1.7). Seams under
test: segment enumeration helper + canonical segment-name parser, append
rollover boundary, cross-segment replay order AND sequence/event-id
uniqueness, enumeration-reader tolerance, cycle-rows cache fingerprint
across segments, reconcile per-row quarantine, error `field` plumbing.

## 1. Implementation

- [x] 1.1 Canonical segment model in `file_orchestration_journal.py`:
      (a) a segment-name parser that maps `<cycle>.jsonl` → (cycle, 0)
      and `<cycle>.<n>.jsonl` (n ≥ 1, consecutive integers, no gaps) →
      (cycle, n), rejecting anything else exactly as today; (b) a
      segment-enumeration helper returning the ordered existing segment
      paths of a cycle (exact-path probing, stop at first gap — no
      directory globbing); (c) an append-target helper returning the
      last segment, or the next fresh segment when the pending write
      would exceed `max_bytes`. Safe-segment/containment discipline
      unchanged.
- [x] 1.2 Route the REAL readers/writers through the helper (the
      verified direct-construction inventory, not just `_journal_path`
      callers): `_cycle_rows` (`:3411`/`:3419`),
      `_cycle_rows_by_model_unlocked` (`:3482`/`:3488`),
      `_cycle_rows_source_fingerprint` (`:3640`/`:3646` — fingerprint
      covers ALL segments so rollover invalidates the cycle rows
      cache), `_next_sequence_unlocked` (`:5798`),
      `_next_accepted_submit_event_id_unlocked` (`:5891`),
      `_journal_path` callers `:772` / `:812`-area / `:5030`
      (watched-paths/disappearance stat set covers all segments),
      `_append_journal_record_unlocked` (`:5901`),
      `_append_journal_records_unlocked` (`:5929`), and the cycle
      source discovery stem-match (`:3335-3346` — segments belong to
      their base cycle, not skipped). After the sweep, no direct
      `f"{cycle_segment}.jsonl"` construction remains outside the
      helper/parser.
- [x] 1.3 Enumeration-reader tolerance (the fail-closed trap):
      `_journal_identity_from_path` (`:9097`) and
      `_iter_migration_journal_paths` (`:4928`) use the canonical
      parser so continuation segments resolve to their base cycle
      instead of raising `file_journal_invalid_cycle_time`; verify the
      hit sites `_iter_rollback_scope_pipeline_job_records` (`:1018`),
      `_iter_pipeline_job_records` (`:4050`) behind
      `query_pipeline_jobs_by_cycle/_by_run/_by_slurm_id`
      (`:735/:743/:781/:838/:850`), and
      `_backfill_reconcile_inventory_unlocked` (`:4875`). Unparseable
      names keep today's behavior byte-identically. Dependency note
      (do NOT add offsets here): `_iter_pipeline_job_records`
      (`:4043-4056`) and rollback-scope (`:1013-1035`) merge per-file
      `_read_jsonl` results via `_replay_order_key` and rely only on
      cross-segment sequence uniqueness (1.5) — no cumulative offset
      awareness in these walkers.
- [x] 1.3b Backfill segment-order arbitration (N1): `_backfill_
      reconcile_inventory_unlocked` (`:4866-4890`) currently builds a
      fresh `_CycleRows()` per path and its sync (`:5087-5104`) is
      last-write-wins with no replay arbitration, while
      `sorted(paths)` orders `<cycle>.1.jsonl` BEFORE `<cycle>.jsonl`.
      Group segment paths by (source, cycle) via the canonical parser,
      replay each cycle's segments IN SEGMENT ORDER through one
      `_CycleRows`, then sync once; any walker ordering derives from
      parser (source, cycle, segment_index), never bare path sort.
- [x] 1.4 Rollover semantics: single append rolls to a fresh segment
      when `existing + line` would exceed the limit; batch append rolls
      when `existing + batch` would; an oversized single record/batch
      alone still raises `file_journal_byte_limit_exceeded` and writes
      nothing (no empty segment file left behind). Per-segment
      `_require_within_byte_limit` on read and write; atomic write and
      `_apply_record_to_cycle_rows_cache` unchanged per record.
- [x] 1.5 Cross-segment ordering and uniqueness (decided scheme, N2):
      `_REPLAY_ORDER_FIELD` = `segment_index * MAX_FILE_JOURNAL_RECORDS
      + line_number` (fixed stride, strictly monotonic, segment 0
      byte-identical to today); raise
      `_LATEST_REPLAY_ORDER_SENTINEL` (`:151`) in lockstep to
      `segments_bound * MAX_FILE_JOURNAL_RECORDS + 1` so the latest
      view STILL wins same-`sequence` ties in `_replay_order_key`
      (`:8364`) — a naive cumulative offset overruns today's sentinel
      (100_001) from segment 2 onward and silently inverts latest-view
      precedence (`_apply_latest_view` sites `:3711-:3748`).
      `_next_sequence_unlocked` / `_next_accepted_submit_event_id_
      unlocked` floors computed over ALL segments so `sequence`/
      `event_id` never reuse (reuse is silent state corruption).
      Single-segment cycles byte-identical.
- [x] 1.6 Reconcile per-row quarantine (`reconcile.py` loop from
      `:1340`): wrap each row's WHOLE body — all journal write points
      (~`:1372, :1388, :1419, :1447, :1468, :1484, :1509, :1553`),
      including `:1419` inside the existing `ReconcileQueryUnavailable`
      handler (`:1408`) — in a per-row `FileOrchestrationJournalError`
      catch that records ONE quarantined outcome `{row, reason, field}`
      (no duplicate append when the row already recorded an outcome)
      and continues. Extend `ReservationReconcileOutcome`
      (`:1274-1293`) with optional reason/field carriers; forward them
      in the hand-written projection `scheduler_runtime.py:1513-1528`.
      `ReconcileQueryUnavailable` semantics byte-identical.
- [x] 1.7 Segment bound (decided, N3/N5): module constant = **3 total
      segments per cycle** (base + 2 continuations; 48 MiB worst case,
      under the 64 MiB read-cache budget `:128` with headroom — N=4
      would evict the whole cache per replay). Exceeding it fails
      closed with the SAME error class but DISTINCT reason
      `file_journal_segment_limit_exceeded` (naming the cycle file),
      so segment exhaustion is distinguishable from an oversized
      record in evidence and quarantine outcomes. Orphan/gapped
      segments WITHIN the probe window are a fail-closed integrity
      error (`file_journal_segment_gap`) with the same answer from
      cycle-level enumeration and recursive walkers (N4); BEYOND the
      window (index ≥ 4, writer-unreachable) walkers/backfill still
      fail closed while the exact-path cycle reader is blind — the
      adjudicated bounded-window asymmetry, pinned both directions
      (round-2 ruling); non-numeric suffixes keep today's behavior.
- [x] 1.8 Error observability: include the error's `field` (redacted
      via the existing `_restart_reconcile_error_token` discipline) in
      `_restart_reconcile_error_message` (`scheduler_runtime.py:
      1635-1638`).
- [x] 1.9 Docs: update `docs/runbooks/qhh-22-business-bringup.md:222,
      228` for the segmented layout (base + bounded continuation
      segments, same append-only audit semantics).

## 2. Tests (requirement-driven; red-before where marked)

- [x] 2.1 (red) Append rollover: fill a cycle log to just under
      `max_bytes` (inject a small `max_bytes` via the constructor param
      `:435`, existing `max_bytes=32` test precedent — production
      default untouched), append one more event → continuation segment
      created, both under limit, replay yields identical rows/order to
      a single-file oracle. Red today: raises
      `file_journal_byte_limit_exceeded`.
- [x] 2.2 (red) End-to-end incident geometry: journal state with a
      reserved-unbound forecast row whose cycle log is at capacity →
      scheduler pass restart reconcile resolves the row (post-rotation)
      instead of `restart_reconcile_unknown`; pass proceeds to
      candidate processing.
- [x] 2.3 Single-segment byte-identity: cycles that never overflow
      produce byte-identical reads, `_REPLAY_ORDER_FIELD` values, and
      event ids vs master.
- [x] 2.4 Oversized single record/batch still fails closed with the
      same error, writes nothing, and leaves NO empty segment file.
- [x] 2.5 Batch rollover: batch that fits a fresh segment but not the
      current one lands entirely in the next segment; cache/replay
      consistent.
- [x] 2.6 Cross-segment uniqueness and ordering: after rollover,
      `_next_sequence_unlocked` and `_next_accepted_submit_event_id_
      unlocked` are strictly increasing (floors read all segments);
      last-writer-wins replay across segments matches the
      single-file oracle.
- [x] 2.7 Cache invalidation across segments: after rollover, a new
      append to the continuation segment invalidates the cycle rows
      cache (`_cycle_rows_source_fingerprint` observes all segments) —
      no stale rows from the frozen base segment.
- [x] 2.8 Enumeration tolerance (AC-level, red without 1.3): with a
      continuation segment present, `query_pipeline_jobs_by_cycle/
      _by_run/_by_slurm_id`, rollback-scope iteration, and
      reconcile-inventory backfill neither raise
      `file_journal_invalid_cycle_time` nor skip segment records; the
      cycle source discovery (`:3335-3346`) sees segment content.
- [x] 2.9 (red) Reconcile quarantine: two reserved-unbound rows, first
      one's journal write forced to raise `FileOrchestrationJournal
      Error` → first row quarantined with reason+field in evidence
      (single outcome, no duplicate), second row resolved, pass status
      not `restart_reconcile_unknown`.
- [x] 2.10 (red) Error message includes redacted `field`; no raw
      absolute path leaks (today `str(error)` is the bare reason —
      `FileOrchestrationJournalError.__init__` `:284`).
- [x] 2.11 Segment bound: cycle at the cap (3) fails the next rollover
      closed with reason `file_journal_segment_limit_exceeded`; bound
      value and its read-cache rationale pinned.
- [x] 2.12 Containment + bounded-window foreign-file rule: segment
      paths honor no-follow/root confinement; non-numeric suffixes
      (`<cycle>.x.jsonl`) keep today's identity-parsing behavior
      byte-identically; IN-WINDOW gapped segments (e.g. `<cycle>.2`
      without `.1`) fail closed with `file_journal_segment_gap` from
      BOTH the cycle-level enumeration and the recursive walkers;
      OUT-OF-WINDOW orphans (`<cycle>.5.jsonl` without predecessors,
      writer-unreachable) fail closed in walkers/backfill while the
      exact-path cycle reader returns base-only rows silently — the
      adjudicated asymmetry, pinned from BOTH reader sides (`.5` and
      `(0,1,2,4)` geometries) so silent flips in either direction go
      red.
- [x] 2.13 (red without 1.3b) Backfill ordering: base + continuation
      segments where the continuation terminates a job that the base
      leaves `reserved` → `_backfill_reconcile_inventory_unlocked`
      neither resurrects the reserved anchor nor deletes a live one;
      inventory reflects segment-order replay (kill the lexicographic
      `sorted()` geometry: `<cycle>.1.jsonl` before `<cycle>.jsonl`).
- [x] 2.14 Latest-view sentinel invariance: same-`sequence` tie between
      a latest-view row and a journal record in segment ≥ 2 still
      resolves to the latest view (sentinel raised in lockstep with
      the bound); explicit regression for the naive-cumulative-offset
      inversion. (2.3's single-segment byte-identity can never catch
      this.)

## 3. Verification

- [x] 3.1 `uv run pytest -q tests/test_file_orchestration_journal.py
      tests/test_file_orchestration_journal_read_cache.py
      tests/test_production_scheduler.py tests/test_orchestration_chain.py`
- [x] 3.2 `uv run ruff check .`
- [x] 3.3 `openspec validate journal-event-log-rotation --strict
      --no-interactive`

## 4. Spec delta

- [x] 4.1 `specs/pipeline-job-persistence/spec.md` ADDED requirement +
      scenarios (authored with fixture; includes enumeration-tolerance
      and bounded-segments scenarios).

## 5. Post-merge ops (node-22, NOT merge-gating)

- [ ] 5.1 node-22 pull → rerun gfs repair `--plan` then `--submit`
      (expect restart reconcile to resolve `retry_116` by rolling the
      IFS journal to a continuation segment, repair submission to reach
      the Slurm gateway, forcing rebuild in squeue) → repeat for IFS →
      verify forecast completes → `systemctl --user start
      nhms-compute-scheduler.timer`; capture one
      `runs/<run_id>/logs/task_outcome.json` vs journal if any task
      fails (carried from #1160 §5.1).
