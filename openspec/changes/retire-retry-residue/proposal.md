# Proposal — retire-retry-residue (#3183)

Closes #3183.

```text
Issue type: refactor (retire residue) + test
Fixture level: expanded
Upstream suggested level: absent (override: deletes a persisted-file writer in the shared journal write path)
Blast radius: a pipeline-event write that crashes or changes its journal record; a test-vocabulary or guard regression
Selected risk packs: File IO / path safety, Concurrency / shared state, Error handling, Legacy compatibility, Resource limits, Documentation
Evidence floor: E1-E6 below
```

## Why

#3157 (PRs #3181/#3182) retired the manual-retry submit lanes. Its reviews found five leftovers, which are listed in the
issue against `origin/master@172394407`.

## What

1. **Delete the write-only private runtime-root recovery lane.** Decision: delete, don't keep as forensics.
   - Its only reader, `_pipeline_event_private_runtime_root_candidates`, fed the retired runtime-root resolution and was
     deleted in #3182.
   - No runbook, script or tool reads `private/runtime-root-recovery/`: `git grep` over `docs/`, `scripts/`, `services/`,
     `workers/` and `packages/` finds no reader.

   Changes:
   - In `services/orchestrator/file_orchestration_journal.py`, delete:
     - the `private_recovery_payload` computation and branch (`:11147`, `:11166-11172`);
     - `_write_pipeline_event_private_recovery_unlocked` (`:11181-11205`);
     - the imports of `_private_runtime_root_recovery_path`/`_record` (`:259-260`).
   - In `services/orchestrator/file_journal_retry_helpers.py`, delete:
     - `_private_runtime_root_recovery_record`, `_runtime_root_recovery_candidate_records`,
       `_runtime_root_recovery_candidate_value` and `_private_runtime_root_recovery_path` (`:176-262`);
     - `FILE_ORCHESTRATION_PRIVATE_RECOVERY_SCHEMA_VERSION` and `_PRIVATE_RUNTIME_ROOT_RECOVERY_RECORD_TYPE` (`:62-63`);
     - `_RUNTIME_ROOT_EVENT_CANDIDATE_PATHS` (`file_orchestration_journal.py:525`), unconditionally (it has no other
       user).
   - The lane's tail in `services/orchestrator/retry.py`, which has no production user once the lane is gone:
     - `_RUNTIME_ROOT_FIELDS`, `_DB_FREE_REQUIRED_FIELD`, `_DB_FREE_SELECTOR_FIELDS` and `_DB_FREE_RUNTIME_FIELDS`
       (`:79-96`);
     - `_mapping_at` and `_has_runtime_root_field` (`:436-446`);
     - their import in `file_journal_retry_helpers.py:53-56`.
     `tests/test_retry_cancel_consistency.py:22-23,477-533` used the two functions only as assertion helpers, so it gets
     test-local equivalents with the same field list. The docstring at `:477-478` no longer calls them retry.py readers.
     Its assertions are unchanged.
   - The public redaction of runtime-root fields (`_public_evidence` and friends) is unchanged. It does not use the
     deleted names; confirm with `git grep -w`.
   - Tests:
     - `tests/test_file_orchestration_journal.py:2663` keeps its public-redaction assertions. Its private-file half
       (`:2709`) becomes "no `private/runtime-root-recovery` file is written";
     - `:2722` (private record omits secrets) is deleted, because its subject no longer exists;
     - the test at `tests/test_file_journal_late_lookup_seams.py:247` exists only for the deleted function, so it is
       deleted outright. The separate `_journal_record_for_write` facade-clock seam test above it stays;
     - `tests/fixtures/file_journal_cluster_map.json` (`:157`, `:291`, `:294`, `:326-335`, `:445`) is updated so that
       `tests/test_file_journal_cluster_map.py` still matches the module's definitions;
     - the `tests/test_production_scheduler.py:57751` docstring names a `SafeFilesystemError` source that still exists
       (`_append_journal_record_unlocked` / `_materialize_latest_unlocked`). The test injects at
       `_append_validated_record_unlocked` and is unchanged otherwise.
   - Pre-existing on-disk records are left in place, with no migration and no deletion. A future rewind modelled on
     `docs/runbooks/receipts/2026-09-28-issue2675-forcing-bind-rehearsal.md:52` finds fewer sidecars to move, which is
     harmless.
   - **Failure paths change, deliberately.** The private write ran after `_validate_outgoing_record` and before
     `_append_journal_record_unlocked` (`:11166-11172`). It could raise at:
     - `_required_safe_identity(entity_id)`;
     - `_safe_segment(event_id)`, which a caller-supplied `event_id` reaches through `append_historical_pipeline_event`;
     - `_validate_json_complexity` on the private record;
     - `_require_within_byte_limit` and `_atomic_write_bytes_unlocked` (`SafeFilesystemError`/`OSError`).

     Inputs that failed only there now append normally. Each of these input classes is either already rejected
     earlier by `_validate_outgoing_record`/`_journal_record_for_write` (so its behaviour is unchanged), or it is listed
     in the PR as now accepted. The implementer checks each one and records which.
2. **`apps/api/routes/pipeline.py:65` `_PENDING_JOB_STATUSES`.** Decision: delete it.
   - It has no production reader. `tests/test_status_vocab_parity.py:329` and `:597` pin it only as a parity row. Both
     rows go, the docstring at `:591` ("five route buckets") becomes four, and the now-unused `PIPELINE_JOB_PENDING`
     import at `pipeline.py:41` goes too.
   - `packages/common/status_vocab.py:92` `PIPELINE_JOB_PENDING` is **kept** as a vocabulary member. It is not a dead
     constant:
     - `scan_status_literals` reads the vocabulary to name literal status sets, and
       `tests/test_status_literal_scan.py:251` relies on it to recognise `("pending", "queued")`;
     - its vocabulary pin (`tests/test_status_vocab_parity.py:268`) stays.
3. **Stale large-file guard exemptions.** Remove `services/orchestrator/retry.py` (446 lines) and `tests/test_retry.py`
   (763 lines) from `.large-file-guard.json` (`:92`, `:56`).
4. **ADR 0009 census.** Refresh `docs/adr/0009-path-canonicalization-dereference-doctrine.md`:
   - drop the three deleted `retry.py` table rows (`:125-127`);
   - reword the `:115` prose that names `retry.py::_local_runtime_root_safety`;
   - restate the count. N comes from the census guard `tests/test_path_canonicalization_family_guard.py`, which pins no
     count; the cardinality appears only in a message at `:589`. Print it with
     `uv run python -c "from tests.test_path_canonicalization_family_guard import _authority_set; print(len(_authority_set()))"`.
     The expected N is 16, recorded with its output;
   - the snapshot line names the pre-change base SHA, not the commit's own;
   - the split at `:145-146` ("15 个走 admit … 4 个立场 A") becomes 15 admit / 1 stance A;
   - the stance-A example at `:24` becomes `scheduler_config/db_free.py::_db_free_loop_filtered_realpath`, the only
     function still carrying `ADR 0009 loop-filtered`, and "等" goes;
   - "19 行" at `:211` and `:311` is history and stays.
5. **Upsert placeholder pin.** Add a test for spec `pipeline-job-persistence` "Display redaction placeholders SHALL
   NOT reach durable journal state through any write path" (`:748-750`), on the `error_message` keep-merge of
   `upsert_pipeline_job`.
   - The discriminating case: a real whole-URI `error_message` is persisted, then the public row (where `_pipeline_job_row`
     leaves `[object-uri]`/`[uri]` intact) is round-tripped through `upsert_pipeline_job`. Parametrize over both
     placeholders.
   - Assert on the durable surfaces, `_durable_pipeline_job_payloads(...)[-1]` and `_direct_row_payload`, not on the
     public row, which masks the URI either way. The durable `error_message` must equal the real value.
   - Two companion cases document the rest of the rule but are not discriminating, because the write-boundary strip in
     `_journal_record_for_write` covers them too:
     - with nothing persisted, the durable value is `None`;
     - an embedded placeholder survives verbatim.
   - E4 mutation: at `file_orchestration_journal.py:3000-3003` set `row["error_message"] = incoming["error_message"]`,
     leaving the other keys resolved. Expected red: the durable `error_message` is `None` instead of the real value.

## Non-goals

- No change to public redaction, the journal record format of `pipeline_event`, retention or budget code, or any other
  `private/` subtree.
- No retry, marker-lane, route or frontend change (#3179 covers the UI).

## Must preserve

- For every input the old path accepted, `pipeline_event` journal records and public projections are byte-identical. Only
  the private side file disappears. For inputs rejected only by the deleted private write, see What 1, "Failure paths
  change".
- Event-write ordering and locking are unchanged, as is every other caller of the deleted helpers' shared dependencies.
- Status-vocabulary parity for every remaining site.

## Evidence Floor

- [ ] E1 Lint and deletion checks:
  - `uv run ruff check --no-cache .` passes, `git diff --check` is clean, and the entropy hard gate reports
    `hard_gate_failing_count 0`;
  - `git grep -n -w -E "_private_runtime_root_recovery_record|_private_runtime_root_recovery_path|_write_pipeline_event_private_recovery_unlocked|_runtime_root_recovery_candidate_records|_runtime_root_recovery_candidate_value|_PRIVATE_RUNTIME_ROOT_RECOVERY_RECORD_TYPE|FILE_ORCHESTRATION_PRIVATE_RECOVERY_SCHEMA_VERSION|_RUNTIME_ROOT_EVENT_CANDIDATE_PATHS|_RUNTIME_ROOT_FIELDS|_DB_FREE_REQUIRED_FIELD|_DB_FREE_SELECTOR_FIELDS|_DB_FREE_RUNTIME_FIELDS|_PENDING_JOB_STATUSES" -- ':!openspec' ':!docs/review-loop-log.jsonl' ':!docs/adr'`
    is empty, except for test-local helpers deliberately named the same (list them);
  - `git grep -n -w -E "_mapping_at|_has_runtime_root_field" -- services` is empty;
  - `.large-file-guard.json` has lost exactly the two entries.
- [ ] E2 Byte-identity of the journal tree:
  - on master and on HEAD, with `_utcnow` pinned to a fixed instant, append the exact event from
    `tests/test_file_orchestration_journal.py:2663` (same `details`, same identities) into a fresh journal root;
  - compare the full file tree under the root. The only path difference is `private/runtime-root-recovery/**`, which
    exists only on master. Every other file (journal jsonl, `latest/`, direct rows) is byte-equal;
  - the public rows returned by the read API are equal.
- [ ] E3 Local:
  - the issue set: `uv run pytest -q tests/test_file_orchestration_journal.py tests/test_file_orchestration_journal_facade.py tests/test_file_journal_cluster_map.py`;
  - plus `tests/test_file_journal_late_lookup_seams.py`, `tests/test_status_vocab_parity.py`, the ADR census guard test,
    and the selector set for the diff.
- [ ] E4 Mutation reds, each restored:
  - the upsert merge assigns `incoming["error_message"]` directly → the new pin is red;
  - the private recovery write is re-added → the rewritten `:2663` test is red.
- [ ] E5 node-27 temporary worktree: the E3 set with the real-DB integration env, `leftover_it_dbs=0`.
- [ ] E6 `openspec validate retire-retry-residue --strict --no-interactive`.
- [ ] E7 Failure-path classification. The PR body lists each of the four deleted raise sites with a verdict, checked in
  code and, where reachable, by a test:
  - `entity_id`: still rejected earlier by `_validate_event_identity` → `_required_safe_identity`
    (`file_journal_paths.py:677`), via `_validate_outgoing_record`. Unchanged.
  - `event_id`: the write path checks only `_optional_text` (`file_journal_paths.py:676`). A historical event whose
    `event_id` fails `_safe_segment` and that carries runtime-root fields is now appended. A test pins this, and the
    PR states why it is harmless: replay keys on `event_id` and never uses it as a path segment.
  - Complexity and byte limits: the verdict comes from the limits that `_journal_record_for_write`,
    `_append_journal_record_unlocked` and `_materialize_latest_unlocked` still enforce.
  - Filesystem faults: the remaining `SafeFilesystemError` sources are `_append_journal_record_unlocked` and
    `_materialize_latest_unlocked`.
