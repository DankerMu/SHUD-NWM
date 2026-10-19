## ADDED Requirements

### Requirement: Journal facade and sibling edits SHALL select the test files that patch facade globals
A changed path that is `services/orchestrator/file_orchestration_journal.py` or matches `services/orchestrator/file_journal_*.py` SHALL select every file in `FILE_ORCHESTRATION_JOURNAL_IMPORTER_TESTS`, and that set SHALL contain the test files that import the journal facade inside a function body and patch a facade global through it (`tests/test_gateway_reconcile_{file_cohort_comment,identity_release,claimant_exclusivity,inventory,round10,writer_launch,writer_quiescence,writer_receipts,file_submit_barrier}.py` and `tests/test_safe_fs_lock.py`). A test SHALL pin this for every `file_journal_*.py` found on disk, with a non-empty parameter set.

#### Scenario: A sibling edit selects the facade patchers
- **WHEN** `services/orchestrator/file_journal_paths.py` is the only changed path
- **THEN** the selection contains `tests/test_gateway_reconcile_file_cohort_comment.py` and `tests/test_gateway_reconcile_identity_release.py`

#### Scenario: Removing the sibling glob rule turns the pin red
- **WHEN** the `services/orchestrator/file_journal_*.py` rule is removed from the selector rules
- **THEN** every `file_journal_*.py` sibling parameter of the pin fails (the facade parameter keeps its own stop rule)
