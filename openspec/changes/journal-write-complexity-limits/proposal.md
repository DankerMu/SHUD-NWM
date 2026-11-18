# Proposal — journal-write-complexity-limits (#3197)

Closes #3197.

```text
Issue type: bugfix
Fixture level: expanded
Upstream suggested level: absent (override: shared journal write path, persisted state)
Blast radius: a write that used to append is refused; or a write still appends a record that a reader then refuses
Selected risk packs: File IO / path safety, Error handling / rollback / partial outputs, Legacy compatibility, Resource limits
Evidence floor: E1-E6 below
```

## Why

The file journal enforces its JSON complexity limits only on read: `self.max_json_depth`/`self.max_json_nodes` in
`_decode_mapping` (`services/orchestrator/file_journal_replay.py:251`, `:277-300`). A write whose record is over the limit
succeeds, and the next read of that cycle blocks (`file_journal_read_blocked`). At depth 61 the line is appended and then
`_materialize_latest_unlocked` raises, so the caller sees an error although the record is already durable.

**Deviation from the issue's fix direction.** The issue says to check the outgoing record. For depth, that alone does not
close the symptom. At depth 60 the jsonl line reads back, but `latest/<source>/<cycle>/<model>.json` is refused, because
the latest view nests some record types one level deeper. So the depth check leaves headroom for each record type's
latest-view offset.

## What

1. **Verify the depth offsets (task 1.1).**

   Expected extra depth over the record, from the code:

   | File | Record type | Expected offset | Why |
   |---|---|---|---|
   | jsonl line | all | 0 | the line is the record |
   | direct row (`:10939`) | all | 0 | the file is the record |
   | latest view (`:11598-11621`) | `pipeline_job`, `pipeline_event` | +1 | payload moves from depth 2 to a list element at depth 3 |
   | latest view | `hydro_run`, `forecast_cycle`, `forcing_version`, `model_context` | 0 | single-row slot |

   - Why the offsets are constant: `_latest_mapping` and `_upsert_by_key` (`file_journal_replay.py:98-119`) replace whole
     rows, and `_apply_latest_view` (`:7541`) re-embeds without adding levels.
   - How to measure: per record type, with a small `max_json_depth`, read back through a **fresh** repository instance,
     so that neither the writer's `_apply_record_to_cycle_rows_cache` nor the prevalidated-bytes cache hides a block.
   - Record the measured table. If any depth offset is not constant per record type, stop and report.
2. **Depth check.** `_validate_outgoing_record` is "the one write-side validator every journal lane runs before its first
   byte" (`:11162-11170`). It additionally runs `_validate_json_complexity` on the built record:
   - `max_depth = self.max_json_depth - _LATEST_VIEW_DEPTH_HEADROOM[record_type]`;
   - the limits are the instance values, not the module constants;
   - `_LATEST_VIEW_DEPTH_HEADROOM` is one named per-record-type mapping, with a comment citing the measurement.
3. **Node check.** The node count is checked against the record itself only: `max_nodes = self.max_json_nodes`. That
   bounds the jsonl line and the direct row, which are the record.
   - The latest view's node count also includes its own wrapper fields and the cycle's other rows; for example, a
     `pipeline_event` view always carries its job row, and a forecast_cycle event lands in every model's view.
   - That count depends on cycle state, so it stays a read-side limit (Non-goals).
4. **Closed lane list.** Every lane that writes record content passes through the validator before its first byte. This
   was verified by the fixture review, and the implementer re-confirms it:
   - validator call sites (11): `:4444, :4549, :4783, :5010, :5230, :6553, :10701, :10793, :10822, :10990, :11147`;
   - single appends (3): `:10738, :10893, :11154`;
   - batch appends (7): `:4452, :4557, :4791, :5018, :5238, :6561, :10836`. Each validates every record in its loop
     before it appends, so a batch with one over-limit record writes zero bytes. Operator bind validates (`:10793`,
     `:10822`) before the anchor sync and the append.
   - derived files written after the append: the direct row (`:10939`, which is the record) and the latest view
     (`:11622`, whose depth the headroom covers);
   - out of scope, fixed-shape scalar files: anchors (`:10057`, `:10120`), rollback receipts, fences, markers
     (`:8594-8941`), the empty fence sidecar (`:8728`);
   - deliberately unchecked, with the reason recorded in a comment:
     - archive restore (`services/orchestrator/scheduler_journal_restore.py:188-196`) rewrites archived bytes verbatim;
     - `_restore_derived_master_direct_unlocked` (`:10944`) rebuilds a direct row from an existing journal record and
       appends nothing. Checking it with headroom would refuse records the direct reader accepts.
       - It reaches `_validate_outgoing_record` at `:10990`, so the complexity check is **not** added inside that
         validator unconditionally. The validator gets an explicit opt-out (for example `check_complexity=False`),
         passed only at `:10990`; or the check becomes a separate call at the append lanes.
       - E3 covers it: a canonical record whose depth falls in (limit − headroom, limit] still rebuilds its direct row.
5. **Historical import is zero-byte.** `import_historical_scheduler_state` (`file_orchestration_migration.py:1356-1401`)
   appends row by row. A mid-run refusal would leave a partial import. On a rerun, an event without an `event_id`
   (`:6893-6912`) would get a new id and be duplicated.
   - Fix: following the #2674 pattern (`:1331-1342`), pre-scan every built record with the same complexity predicate
     before the first append. One shared helper serves both paths, so the predicate is not duplicated.
   - **Where:** at the same point as the #2674 gate, before `_verified_or_created_journal_root` (`:1347`). A refusal
     then does not even create the journal root.
   - **What is scanned:** what the writer would actually build: after strip and redact, and for events after
     `_public_pipeline_event_payload`. `public_evidence.py:58-61` collapses nested values under sensitive keys to
     `"[redacted]"`, so scanning raw rows would refuse rows the writer accepts. Rows the import skips are not scanned.
   - An over-limit history then refuses the import with zero bytes.
6. **Error codes.** Refusals raise `FileOrchestrationJournalError` with the existing codes
   `file_journal_json_depth_exceeded`/`file_journal_json_node_limit_exceeded`, and `field` names the record type.
   - No new `_on_write` codes are needed. Production consumers of these codes:
     - `ROW_CONTENT_SKIP_REASONS` (`:402-428`), read by `_flat_direct_pipeline_job_record` (`:7905`), which is read side
       only;
     - the scheduler and the stall probe don't branch on them;
     - `file_journal_read_blocked` is an unrelated placeholder.
   - Update `docs/runbooks/node22-control-plane-manual-recovery.md:714` if it describes them as read-only failures.
7. **Newly refused inputs, classified per lane** in the PR body (as #3183 E7 did): what a caller sees now vs before.
8. **Facade hygiene.** If `file_orchestration_journal.py` gains an import from `file_journal_replay`, update
   `tests/fixtures/file_journal_cluster_map.json` and keep the first-import guard green.

## Non-goals

- Latest-view node counts, meaning the view's wrapper fields plus the cycle's other rows (which includes aggregate growth
  across many individually valid records), remain a read-side limit.
  - Residual risk, recorded, in two steps:
    1. A write whose record is within `max_nodes` succeeds, but `_atomic_write_json_unlocked` does not check what it
       writes, so it can produce a latest view over `max_nodes`.
    2. The next write for that model reads the view through `_cycle_rows` (`:7178-7196`), so that write appends and then
       raises.
  - With `max_nodes = 300_000` and a wrapper of about 16 nodes, only a cycle already near the limit can hit it.
- Changing the limits themselves, the read side, or any record format.

## Must preserve

- Every record that round-trips today still writes and reads, byte-identically.
  - **Recorded deviation:** `pipeline_job` carries headroom 1 for the latest view, so a job record whose depth is exactly
    `max_json_depth` is now refused. A master job never enters a latest view, and the same holds for a model-less job in
    a cycle with no latest view, so for these records the refusal is stricter than strictly necessary. Per-row refinement
    was rejected (KISS): at the default limit of 64 against real records of depth ≤ 10, this boundary is unreachable in
    practice.
- Write ordering and locking are unchanged; a refusal leaves the journal tree byte-identical, with zero bytes written.
- Existing journal content that is already on disk is still readable and restorable: restore lanes are not checked.

## Evidence Floor

- [x] E1 `uv run ruff check --no-cache .`; `git diff --check`; entropy hard gate `hard_gate_failing_count 0`.
- [x] E2 The task 1.1 measured depth-offset table (record type × file), with the probe script and output.
- [x] E3 Boundary tests, with a small-limit repository and a fresh-instance read-back:
  - at least `hydro_run` (headroom 0) and `pipeline_job` or `pipeline_event` (headroom 1) each have a depth pair: the
    deepest admitted record round-trips through the jsonl, direct and latest readers, and one level deeper is refused
    before any byte, with the full journal tree byte-identical (sha of every file);
  - a node-count pair on `pipeline_job` (the only type with a direct row): `max_nodes` is admitted and readable from
    the jsonl line and the direct row, and `max_nodes + 1` is refused with zero bytes. The pair decodes those two files
    directly at the instance limits, because the public readers go through the latest view first;
  - the restore lane: a canonical record at depth in (limit − headroom, limit] still rebuilds its direct row through
    `_restore_derived_master_direct_unlocked`;
  - a batch (one batch lane) with one over-limit record writes nothing;
  - a historical import whose history holds one over-limit record writes nothing, and the journal root does not exist
    afterwards;
  - a historical row that is over the limit only before redaction still imports;
  - an over-limit structure under a runtime-root key and under an ordinary key behaves the same.
- [x] E4 Mutation reds, each restored:
  - remove the check;
  - use the module constants instead of the instance limits;
  - move the check after the append, which makes the zero-byte assertion red;
  - headroom −1, which makes the one-over refusal red;
  - headroom +1, which makes the at-limit round-trip red;
  - collapse the per-type mapping to a single constant, 0 and then 1; each must turn a test red;
  - drop the historical-import pre-scan;
  - apply the check on the restore lane too, which makes the restore test red.
- [ ] E5 Local: `uv run pytest -q tests/test_file_orchestration_journal.py tests/test_file_orchestration_journal_facade.py tests/test_file_journal_cluster_map.py`
  plus the historical-import suite and the selector set for the diff. node-27 temporary worktree: the same set with the
  real-DB integration env, `leftover_it_dbs=0`.
- [ ] E6 `openspec validate journal-write-complexity-limits --strict --no-interactive`. The PR lists the What 7
  classification and the Non-goals residual.
