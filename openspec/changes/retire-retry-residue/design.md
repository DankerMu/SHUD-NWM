## Design — retire-retry-residue (#3183)

Change surface:
- the `pipeline_event` write path in `file_orchestration_journal.py`;
- `file_journal_retry_helpers.py` and the runtime-root tail of `services/orchestrator/retry.py`;
- `apps/api/routes/pipeline.py`;
- `.large-file-guard.json`;
- ADR 0009;
- tests: journal, late-lookup seams, the cluster map fixture, status-vocab parity, `tests/test_retry_cancel_consistency.py` (test-local helpers), the `tests/test_production_scheduler.py` docstring, and the new upsert pin.

Must preserve:
- for every input the old path accepted, `pipeline_event` journal records and public projections are byte-identical;
- locking and ordering of event writes;
- public redaction of runtime-root fields;
- parity of every remaining status-vocabulary site.

Governing invariant: for every input the old path accepted, removing a write that no code reads changes no journal record, no public projection and no reader. Only the private side file stops appearing. Inputs rejected only by the deleted write are classified in E7.

Sibling surfaces:
- other `private/` writers in the journal: untouched;
- the whole-tree byte and inventory scans (`file_journal_full_tree_budget_contract`, retention): they count files but do
  not parse this subtree, so old files stay inert;
- the late-lookup seam tests: they keep the `_journal_record_for_write` clock seam;
- the cluster-map fixture: it must equal the module's definitions;
- `orchestrator-structural-burndown` "Retry-helper callers of the facade clock": updated by a MODIFIED delta.

Seats:
- correctness: deletion completeness, no shared helper removed, byte-identity;
- test evidence + spec compliance (upsert pin vs `pipeline-job-persistence`, ADR census);
- invariant: the journal write path and the inert old records.

Review focus:
1. Nothing still calls the deleted helpers, and no helper with another user was deleted.
2. The E2 byte-identity is real.
3. The upsert pin discriminates the keep-merge on `error_message` specifically.
4. The ADR count comes from the guard, not from arithmetic.
