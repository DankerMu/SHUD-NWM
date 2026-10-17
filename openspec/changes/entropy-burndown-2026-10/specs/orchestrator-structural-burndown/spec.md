# Spec Delta

## ADDED Requirements

### Requirement: Journal responsibility clusters SHALL be extracted behind a stable facade
`services/orchestrator/file_orchestration_journal.py` SHALL be reduced by moving, in order, the context-mapping helpers (`file_journal_context.py`), read attribution (`file_journal_read_attribution.py`), the identity/segment primitives that break the path-cluster cycle (`file_journal_primitives.py`), replay and JSON primitives (`file_journal_replay.py`), cycle-scope/candidate/cohort helpers (`file_journal_candidates.py`), segment/identity/path discovery (`file_journal_paths.py`) and retry helpers (`file_journal_retry_helpers.py`) into sibling modules. After each step the facade SHALL re-export every moved public and private name that any non-test or test module imports or monkeypatches, with `# noqa: F401` where needed, and every existing test file that references the journal module SHALL pass unchanged. Functions whose callers are monkeypatched by name on the facade (`_utcnow`, `read_bytes_limited_no_follow`, `list_directory_no_follow_limited`, `_stat_signature`, `_iter_regular_json_files`, `_iter_jsonl_files`, `_released_candidate_cycle_scope`, `_cycle_scope_from_job_id`, `_next_file_manual_retry_job_id_for_run`) SHALL either stay in the facade with their callers or have the tests' patch targets moved to the new module in the same PR.

#### Scenario: Extraction step keeps importers and patches working
- **WHEN** a cluster is moved and the facade re-exports it
- **THEN** every test under `tests/` that imports or patches `services.orchestrator.file_orchestration_journal` passes without edits, or the PR shows the patch-target edits for that cluster only

#### Scenario: Annotation-only back references
- **WHEN** a moved module needs `FileOrchestrationJournalRepository` only in annotations
- **THEN** it imports it under `TYPE_CHECKING` and no import cycle is introduced

### Requirement: Journal contract SHALL remove dead re-exports
After the last extraction, the facade SHALL re-export only names with a remaining importer (verified by repository search), and a test SHALL fail if the facade re-exports a name no module imports.

#### Scenario: Unused re-export detected
- **WHEN** the facade re-exports `_foo` and no file imports or patches `file_orchestration_journal._foo`
- **THEN** the re-export hygiene test fails naming `_foo`
