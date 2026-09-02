# Tasks

Fixture level: expanded · repair intensity: high · issues: #1567 #1658 #1761 #1760
(one PR, serial implementation, order #1567 → #1658 → #1761 → #1760).
Line cites are against `services/orchestrator/file_orchestration_journal.py` at
`origin/master` `4f0ff53f` (14,713 lines); symbol names are authoritative.

## 0. Evidence Floor

Oracle is local + CI pytest (all four issues are `db-free` / `local-only`; no
node-27 or node-22 receipt applies).

- [ ] `uv run pytest tests/test_file_orchestration_journal.py tests/test_file_orchestration_journal_read_cache.py -q` green on the default (case-insensitive) macOS volume
- [ ] The same two files green with `--basetemp` on a case-sensitive APFS volume (issue #1761 `Verification:` block B — `hdiutil create ... 'Case-sensitive APFS'`); the two filesystem-branching pins must run their *other* branch here
- [ ] `uv run pytest tests/test_orchestration_chain.py tests/test_warm_start_chaining.py tests/test_production_scheduler.py tests/test_file_orchestration_migration.py -q` green (proves no existing writer — chain, warm-start, scheduler, historical import — trips the #1760 gate and no chain path regresses on the cache changes)
- [ ] `uv run ruff check .` clean
- [ ] Red proofs, batched, against pre-change source (implementer contract): the #1567 warm-tamper test, the #1658 survival test, the #1761 double-read tests, and the #1760 rejection tests each shown red before / green after; `git stash list` holds no `red-proof` entry afterwards
- [ ] `openspec validate journal-cache-fingerprint-and-identity-followups --strict --no-interactive`

## 1. #1567 — containment-aware cycle-rows fingerprint (D1, D1b)

Seams under test: public `list_stage_statuses` (read lane the issue names) and
the `_cycle_rows` cache through a shared `FileOrchestrationJournalRepository`
instance; owner path through `_locked_cycle_write`.

- [ ] One containment-aware signature helper; every stat in
      `_cycle_rows_source_fingerprint` / `_cycle_segment_signatures` (segment
      slots, event slots, latest scandir directory, by-cycle partition, flat
      root) routes through it. Absence → `None`; containment fault → a
      dedicated non-`None` marker.
- [ ] A fingerprint carrying a marker is neither a hit nor stored (assert via
      the cache dict after the read).
- [ ] Owner hit (`in_write_window`) runs the directory probe; a marker forces a
      recompute. No source-file fingerprint is computed on an untouched tree
      (`test_cycle_write_window_owner_keeps_fingerprint_free_fast_path` stays
      green as written).
- [ ] Update the code comment at `:5716-5724` in `_cycle_rows` (it names #1567 as open scope).
- [ ] Tests (input → expected):
      - warm instance reads legal `[]` for cycle C (cache populated); replace
        `journal/<src>` with a symlink to an empty decoy directory; the same
        instance's `list_stage_statuses` → blocked row `file_journal_unreadable`,
        not `[]`
      - same tree: a fresh instance → identical result to the warm one
      - untouched empty directory: second read is a cache hit (no `_read_jsonl`
        call) and still `[]`
      - tamper placed under `latest/<src>` (the scandir parent) instead of
        `journal/<src>` → the same fail-loud, proving the helper covers the
        sibling stat, not only the segment slots
      - owner window: read inside the window (hit), swap the parent for a
        symlink, read again inside the window → `file_journal_unreadable`
      - write face: frame-2 transition under a warm cache still raises (existing
        PR #1566 pin re-run, unchanged)

## 2. #1658 — scoped window-exit clear (D2)

Seam under test: `_locked_cycle_write` + `_cycle_rows_cache` on one shared instance.

- [ ] Exit `finally` (`_locked_cycle_write` `:10089`) evicts only
      `key[0] == source_id and key[1] == cycle_segment` (normalized source id),
      base key included; the entry clear (`:10072`, the first statement under
      `with self._cache_lock:` at the top of `_locked_cycle_write`) is unchanged
      (diff shows no edit to that statement).
- [ ] Tests:
      - open X's window; inside it populate Y's entry (other cycle, same or other
        source); exit → Y's entry still present and Y's next read makes zero
        `_read_jsonl` / `_read_optional_json` calls; X's prefix (incl. base key)
        is gone
      - existing `test_cycle_write_window_*` and
        `test_non_owner_read_correct_even_with_cache_clear_disabled` stay green
        unmodified

## 3. #1761 — identity dedup on the two remaining sites (D3)

Seams under test: `_cycle_read_source_segments(..., root=)` module function
and the public `query_pipeline_jobs_by_cycle` read with touched-path instrumentation.

- [ ] `_merge_cycle_source_discovery(..., root=)` dedupes by
      `_names_same_directory`; both callers pass `self.root`; `root=None` keeps
      string dedup.
- [ ] Overrides branch dedupes by identity; source-mismatch validation and
      `file_journal_missing_identity` on empty are preserved.
- [ ] Re-pin the `[("IFS", ("IFS", "ifs"))]` assertion
      (`tests/test_file_orchestration_journal.py:5298`) with a
      `_filesystem_is_case_sensitive` (test file `:14914`) branch/skip, the shape
      of the pin at test file `:14966` (`:14920` is the filesystem-agnostic
      shape); do not delete the assertion.
- [ ] Tests (each with a case-insensitive branch and a case-sensitive branch/skip):
      - cross-surface discovery `latest/IFS` + `journal/ifs`, public read →
        no `(st_dev, st_ino)` opened under two spellings
      - case-sensitive: `journal/gfs` and `journal/GFS` both real → both read,
        rows from both returned
      - overrides `("IFS", "ifs")` → one segment on case-insensitive, two on
        case-sensitive; overrides naming another source →
        `file_journal_source_mismatch`; overrides that dedupe to nothing →
        `file_journal_missing_identity`
      - a symlink alias `journal/ifs -> elsewhere` is kept as a distinct segment
        (not collapsed) and the read then fails closed via containment

## 4. #1760 — job_id scope gate at the write boundary (D4)

Seam under test: the public pipeline-job write entrypoints (the same ones the
chain uses), never the private `_write_pipeline_job_direct_unlocked` in isolation.
Gate location (design D4): `_validate_outgoing_record` (`:9419`) for
`record_type == "pipeline_job"` — the one validator all eight write call sites
run before their first byte (`:3724`, `:3829`, `:4064`, `:4255`, `:5131`,
`:9023`, `:9203`, `:9306`).

Lanes each rejection assertion must traverse (one test per lane class):
- single-row lane via a **containment-enabled** public writer —
  `reserve_pipeline_job` (`:2619`, `_committed_projection_containment=True` at
  `:2704`) or `reclaim_pipeline_job_reservation` (`:2707`, flag at `:2906`) —
  through `_write_pipeline_job_unlocked` (append at `:9060`; master section
  `_write_current_master_unlocked` append at `:9106`). Assert additionally that
  no `committed_projection_fault` event was emitted, proving the gate fired
  ahead of `_project_committed_pipeline_job_write` (`:8897`) and was not
  swallowed by its `except Exception`. `upsert_pipeline_job` and
  `append_historical_pipeline_job` are NOT valid vehicles for this assertion
  (they never enter the projection wrapper, so the no-event check would be a
  tautology).
- historical-import lane: `import_historical_scheduler_state`
  (`services/orchestrator/file_orchestration_migration.py:1240`) →
  `append_historical_pipeline_job` (`:2603`) with one divergent job row in the
  `jobs` list — assert the import raises `file_journal_job_id_scope_mismatch`,
  rows before it are imported, the divergent row has no journal record and no
  direct file, and a re-run after correcting the row is idempotent for the
  already-imported rows (test lives in `tests/test_file_orchestration_migration.py`)

Test-input precision for every rejection test (all lanes): the divergent row
must keep `cycle_id` / `run_id` / `idempotency_key` consistent with its own
`source_id` / `cycle_time` so that only `job_id` diverges — otherwise a
pre-existing identity error (`file_journal_run_mismatch`) fires first and the
test asserts the wrong token.
- a batch lane via a public writer that appends a record batch (any of the
  lanes appending at `:3732`, `:3837`, `:4072`, `:4263`, `:5139`) — assert the
  sibling records of the batch are not appended either
- the repair lane `_restore_derived_master_direct_unlocked` (`:9157`) with a
  divergent canonical row — assert no direct file restored and the
  reconcile-inventory anchor still present (kept per its docstring)

- [ ] One gate definition inside `_validate_outgoing_record`, no per-lane copy;
      new code `file_journal_job_id_scope_mismatch`, `field="job_id"`,
      evidence `{"expected": "<source>/<cycle>", "actual": "<source>/<cycle>"}`
      bounded like sibling errors. The gate sits beside — not inside —
      the `_apply_journal_record` call, so the read-side replay is untouched.
- [ ] `_cycle_scope_from_job_id` → `None` passes (fall-open unchanged).
- [ ] Read-side `_validate_pipeline_job_identity` (`:14028`) and
      `_apply_journal_record` (`:6119`) untouched.
- [ ] Tests:
      - single-row lane: `job_id` embeds cycle ≠ row `cycle_time` → rejected
        with the code and evidence; afterwards no journal record for that row
        exists in any segment, no `pipeline-jobs/**/<job_id>.json` exists, no
        `committed_projection_fault` event exists
      - single-row lane: `job_id` embeds source ≠ row `source_id` → same
        (independent test)
      - batch lane: one divergent row in the batch → rejected; none of the
        batch's records appended, no direct file
      - repair lane: divergent canonical row → rejected; no direct file
        restored; anchor still present
      - historical-import lane: one divergent row in `jobs` → import raises at
        that row; earlier rows present; no record/direct file for the divergent
        row; corrected re-run idempotent
      - `job_id` matching neither regex → accepted, row readable
      - full journal + chain + warm-start + scheduler suites green (no writer
        trips the gate)

## 5. Spec + docs

- [ ] Spec delta under `pipeline-job-persistence`: MODIFIED probe requirement
      (drop the #1567 carve-out), MODIFIED fast-path requirement (owner probe,
      scoped exit wipe), MODIFIED cycle-scoped lookup requirement (#1760
      residual → enforced), ADDED identity-dedup requirement.
- [ ] `openspec validate` strict green.

## Risk packs

- Public API / CLI / script entry: not selected — no public signature changes; the new error token is carried by the existing exception type on an existing lane.
- Config / project setup: not selected — no config surface.
- File IO / path safety / overwrite: **selected** — symlinked-parent detection in the cache fingerprint and the owner path (#1567); zero-bytes-written on a rejected write (#1760). Closure items: symlink ancestor swap, genuine absence preserved, no partial output.
- Schema / columns / units / field names: not selected — no payload shape change.
- Auth / permissions / secrets: not selected.
- Concurrency / shared state / ordering: **selected** — cache entry/exit wipes, owner marker semantics, shared-instance survival test (#1658, #1567 D1b). Single lock order preserved; no cache-mutex → write-mutex nesting.
- Resource limits / large input / discovery: **selected** — each per-source directory enumerated once per cycle read on case-insensitive volumes; dual-filesystem evidence (#1761).
- Legacy compatibility / examples: **selected** — historical divergent rows (0/4309 measured) would now fail `_restore_derived_master_direct_unlocked` (anchor kept) and abort `import_historical_scheduler_state` at that row, the same abort-at-row shape every existing identity error already has on that lane (no new prefilter; design D4); case-sensitive production volumes read both real directories unchanged; unparseable job ids stay accepted.
- Error handling / rollback / partial outputs: **selected** — `file_journal_unreadable` parity between cold/warm/owner; `file_journal_job_id_scope_mismatch` before the first byte; `file_journal_source_mismatch` / `file_journal_missing_identity` preserved.
- Release / packaging / dependency compatibility: not selected.
- Documentation / migration notes: not selected — the spec delta is the documentation; no runbook changes.
- Domain packs (Geospatial, Hydro-met time series, SHUD numerical, PostGIS/Timescale, Slurm lifecycle, External providers, Run manifest/QC, Published artifacts/display): not selected — the file journal's cache/identity discipline touches none of them; no scheduling, DB, or display behavior changes.

## Non-goals

See `proposal.md` — other `_stat_signature` callers, `_direct_jobs_cycle_cache`
and the #1734 memo, the read-side job_id decomposition, historical row
backfill, #1757/#1758, any DB or node-27/node-22 receipt.
