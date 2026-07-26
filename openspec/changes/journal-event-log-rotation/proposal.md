# Per-cycle journal event-log rotation + reconcile quarantine (#1165)

## Why

Live outage (node-22, 2026-07-27): the #1160 retry spin wrote ~116 retries
of event lines into `journal/IFS/2026072000.jsonl`, now 16,776,489 bytes —
727 bytes under `MAX_FILE_JOURNAL_JSON_BYTES = 16 MiB`
(`services/orchestrator/file_orchestration_journal.py:119`). Appending ANY
event line crosses the limit and raises
`file_journal_byte_limit_exceeded` (`:3939-3944`; append checks at
`:5920`/`:5926`). Restart reconcile must resolve the reserved-unbound row
`..._forecast_retry_116` of exactly that cycle on EVERY pass —
`query_reserved_unbound_jobs()` is global (`:867-876`), the reconcile
loop's only journal-aware handler is `except ReconcileQueryUnavailable`
(`services/orchestrator/reconcile.py:1408`; the loop itself starts at
`:1340`), so the journal error escapes the per-row journal writes and
aborts the whole reconcile
(`services/orchestrator/scheduler_runtime.py:1531-1534`), pinning the pass
to `restart_reconcile_unknown` (`:1296`). Result: the #1163 repair
authorization works (18/18 candidates `authorized` in plan evidence) but
no pass can reach submission — node-22 is parked. Aggravator: the error's
`field` (which file) is discarded because
`FileOrchestrationJournalError.__init__` passes only `reason` to
`RuntimeError` (`file_orchestration_journal.py:283-288`) and
`_restart_reconcile_error_message` stringifies the error
(`scheduler_runtime.py:1635-1638`) — operators see five words and no path.

## What Changes

- **Segment rotation for per-cycle event logs (the unblock)**: introduce
  continuation segments `<cycle>.<n>.jsonl` (n ≥ 1, consecutive) next to
  the base `<cycle>.jsonl`. One segment-enumeration helper plus one
  canonical continuation-segment name parser are the single source of
  truth for BOTH readers and writers. The load-bearing consumers that
  must route through them (fixture-review-verified inventory — these are
  the REAL direct `f"{cycle_segment}.jsonl"` constructions, not the
  `_journal_path` wrappers): the primary replay reader `_cycle_rows`
  (def `:3359`, journal read `:3411`, pipeline-events `:3419`),
  `_cycle_rows_by_model_unlocked` (def `:3439`, reads `:3482`/`:3488`),
  the cache fingerprint `_cycle_rows_source_fingerprint` (def `:3601`,
  stat signatures `:3640`/`:3646`), the true sequence floor
  `_next_sequence_unlocked` (def `:5791`, read `:5798`), the accepted-
  submit event-id scan `_next_accepted_submit_event_id_unlocked`
  (`:5874-5900`, read `:5891`), the base-path primitive `_journal_path`
  (`:6183`) and its callers (`:772`, `:812`-area, `:5030`
  watched-paths/disappearance stat set, `_append_journal_record_unlocked`
  `:5901`, `_append_journal_records_unlocked` `:5929`), and the cycle
  source discovery stem-match at `:3335-3346`. Appends target the LAST
  segment; when existing content plus the new line (or whole batch)
  would exceed `max_bytes`, the write rolls over to a fresh next
  segment. Per-segment byte-limit enforcement is unchanged; a single
  record (or single batch) larger than the limit by itself still fails
  exactly as today, writing nothing (no empty segment left behind).
  Existing base files are never rewritten — rotation only changes where
  NEW lines land. Segments per cycle are BOUNDED at **3 total (base +
  2 continuations)**: 48 MiB per-cycle capacity stays under the 64 MiB
  read-cache budget with headroom for other cycles (see Known risks);
  the #1163 retry cap is the primary guard against unbounded growth and
  the incident cycle needs exactly one continuation to resolve.
  Exceeding the bound fails closed with the SAME error class but a
  DISTINCT reason `file_journal_segment_limit_exceeded` (naming the
  cycle file), so segment exhaustion is distinguishable from an
  oversized single record in evidence and in quarantine outcomes.
  Orphan/gapped segments (`<cycle>.5.jsonl` without predecessors) are
  a fail-closed integrity error (distinct reason, e.g.
  `file_journal_segment_gap`) under ONE unified rule for BOTH the
  cycle-level enumeration and the recursive walkers — never "ignored
  by one reader, read by another"; non-numeric suffixes
  (`<cycle>.x.jsonl`) keep today's behavior byte-identically.
- **Existing directory-enumeration readers must tolerate segments
  (fail-closed trap)**: three recursive `.jsonl` enumerators exist today
  and would otherwise fail-close or silently skip on segment names —
  `_iter_jsonl_files` (`:9134`, suffix filter `:9289`) feeding
  `_journal_identity_from_path` (`:9097`) whose
  `_parse_cycle_segment(Path(parts[2]).stem)` raises
  `file_journal_invalid_cycle_time` on `"2026072000.1"` (hit sites:
  `_iter_rollback_scope_pipeline_job_records` `:1018`,
  `_iter_pipeline_job_records` `:4050` behind
  `query_pipeline_jobs_by_cycle/_by_run/_by_slurm_id` and `:735/:743/
  :781/:838/:850`), `_iter_migration_journal_paths` (`:4928`, hit site
  `_backfill_reconcile_inventory_unlocked` `:4875`), and the stem-match
  at `:3335-3346` which would silently SKIP segments. All three learn
  the canonical segment parser: a continuation segment belongs to its
  base cycle; unparseable names keep today's behavior. **Backfill
  ordering trap (decided)**: `_iter_migration_journal_paths` returns
  `sorted(paths)`, and lexicographic order puts `<cycle>.1.jsonl`
  BEFORE `<cycle>.jsonl` (`'1'` < `'j'`); `_backfill_reconcile_
  inventory_unlocked` (`:4866-4890`) builds a fresh `_CycleRows()` PER
  PATH and its sync (`:5087-5104`) is last-write-wins with NO
  `_replay_order_key` arbitration — so a stale base segment would
  overwrite continuation-segment terminal states (resurrecting
  `reserved` anchors or deleting live ones). Fix: backfill groups
  segment paths by (source, cycle) via the canonical parser and replays
  each cycle's segments IN SEGMENT ORDER through one `_CycleRows`
  before syncing; walker ordering uses parser-derived
  (source, cycle, segment_index), never bare path sort.
- **Cross-segment replay order and id uniqueness stay monotonic
  (decided scheme)**: `_REPLAY_ORDER_FIELD` is assigned from per-file
  line numbers today (`:3933`); with segments it becomes a FIXED-STRIDE
  offset — `segment_index * MAX_FILE_JOURNAL_RECORDS + line_number` —
  strictly monotonic across segments and bounded by
  `segments_bound * MAX_FILE_JOURNAL_RECORDS`. The latest-view sentinel
  `_LATEST_REPLAY_ORDER_SENTINEL` (`:151`, today
  `MAX_FILE_JOURNAL_RECORDS + 1`) is raised in lockstep to
  `segments_bound * MAX_FILE_JOURNAL_RECORDS + 1`, preserving the
  invariant that the latest view wins same-`sequence` ties in
  `_replay_order_key` (`:8364`) — a naive cumulative offset would let
  segment ≥ 2 journal lines overrun the sentinel and silently invert
  latest-view precedence (`_apply_latest_view` call sites `:3711`,
  `:3715`, `:3724`, `:3728`, `:3744`, `:3748`). Cross-segment
  `sequence`/`event_id` UNIQUENESS stays load-bearing and depends on
  the floor scans (`:5798`, `:5891`) reading ALL segments; the bare
  per-file `_read_jsonl` readers behind `_iter_pipeline_job_records`
  (`:4043-4056`) and rollback-scope iteration (`:1013-1035`) merge by
  `_replay_order_key` and therefore rely ONLY on sequence uniqueness —
  they need no offset awareness (recorded so the implementer does not
  add one).
- **Restart-reconcile per-row quarantine (defense in depth, #1154
  shape)**: the reserved-unbound resolution loop wraps each row's WHOLE
  body (all ~8 journal write points, `reconcile.py` ~`:1372-:1553`,
  including the write at `:1419` INSIDE the existing
  `ReconcileQueryUnavailable` handler) in a per-row
  `FileOrchestrationJournalError` catch → record a quarantined outcome
  (reason + offending file `field`) and `continue`; no duplicate
  outcome append for rows that already recorded one.
  `ReconcileQueryUnavailable` semantics unchanged. Evidence shape:
  `ReservationReconcileOutcome` (`reconcile.py:1274-1293`) gains
  optional reason/field carriers and the hand-written projection at
  `scheduler_runtime.py:1513-1528` forwards them.
- **Error-path observability (D3)**: plumb the error's `field` through
  the restart-reconcile error message using the existing
  `_restart_reconcile_error_token` redaction discipline
  (`scheduler_runtime.py:1635-1638`).

## Impact

- Affected specs: `pipeline-job-persistence` — ADDED requirement
  (rotation + quarantine semantics).
- Affected code: `services/orchestrator/file_orchestration_journal.py`
  (segment helper + parser, append rollover, all readers above,
  enumeration identity parsing, replay-order offsets, cache
  fingerprint), `services/orchestrator/reconcile.py` (per-row
  quarantine + outcome fields), `services/orchestrator/
  scheduler_runtime.py` (outcome projection + error message field).
- Affected docs: `docs/runbooks/qhh-22-business-bringup.md:222,228`
  documents `journal/<source>/<cycle>.jsonl` as the append-only audit
  layout — update for the segmented layout.
- Must preserve: append-only audit semantics (no history rewrite, no
  deletion); per-segment `_require_within_byte_limit` and
  `read_bytes_limited_no_follow` containment discipline (safe-segment
  naming, no-follow, root confinement for segment paths); atomic write;
  `_locked_cycle_write` (`:6104`, flock per `.locks/<source>/<cycle>`)
  serializes segment creation — lock key unchanged; `_read_bytes_cache`
  keyed per segment path stays valid; **cycle rows cache invalidation
  must observe ALL segments** — `_cycle_rows_source_fingerprint` today
  stats only the base file (`:3640`/`:3646`), and after rollover the
  base never changes again, so an unextended fingerprint would return
  stale rows forever; `:5030` watched-paths/disappearance detection
  must likewise cover all segments; replay semantics for single-segment
  cycles byte-identical (a cycle that never rolls over reads exactly as
  today, including `_REPLAY_ORDER_FIELD` values and event ids);
  clean-reservation invariant untouched; `ReconcileQueryUnavailable`
  fail-closed behavior unchanged; gateway/candidate semantics untouched.
- Non-goals: offline compaction/archive tooling (issue option B); the
  5MB scheduler-evidence fallback retention rider (routed as follow-up);
  shrinking or rewriting the existing oversized IFS file; #1118's
  generic circuit breaker; changing `MAX_FILE_JOURNAL_JSON_BYTES`.

## Known risks / disclosed residuals

- Rotation changes on-disk layout for cycles that overflow; any
  external consumer reading `<cycle>.jsonl` directly would see only the
  base segment. In-repo consumers are all routed through the helper
  (inventory above); node-27 does not read node-22's journal (NFS
  products only); `docs/runbooks/qhh-22-business-bringup.md` is updated.
  The live receipt (§5) verifies the scheduler end-to-end.
- Read amplification is real and disclosed: `_cycle_rows` replays every
  segment; `MAX_FILE_JOURNAL_RECORDS` (`:120`) is enforced PER FILE
  (`:3919-3921`), so an N-segment cycle admits up to N× records, and
  N × 16 MiB reaches `MAX_FILE_JOURNAL_READ_CACHE_BYTES = 64 MiB`
  (`:128`) at N=4 — a cycle that large would evict the entire process
  read cache on every replay (`_read_bytes_cache_store` `:3857-3866`).
  Hence the bound of 3 (48 MiB worst case, headroom retained); the
  #1163 retry cap remains the primary guard. The bound value and this
  rationale are pinned by test 2.11.
- Quarantine deliberately narrows the reconcile's fail-closed surface
  for `FileOrchestrationJournalError` only, mirroring #1154; the
  quarantined row's cycle remains unresolved (fail-closed locally) and
  visible in evidence.
