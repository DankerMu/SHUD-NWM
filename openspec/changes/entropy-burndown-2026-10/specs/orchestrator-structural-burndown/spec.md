# Spec Delta

## ADDED Requirements

### Requirement: The journal cluster map SHALL be a checked-in artifact before any extraction
`tests/fixtures/file_journal_cluster_map.json` SHALL assign every top-level definition of `services/orchestrator/file_orchestration_journal.py` to exactly one of `facade`, `context`, `read_attribution`, `primitives`, `replay`, `candidates`, `paths`, `retry_helpers`, and SHALL list, for every name that any test monkeypatches on the facade (found by searching `tests/` for `file_orchestration_journal.<name>` patch targets and for `setattr(<alias>, "<name>"`), its cluster and the clusters of all its call sites. A test SHALL fail when a top-level definition is unassigned, assigned twice, or when a patched name is assigned to a cluster other than `facade` while one of its call sites is assigned elsewhere without the late-lookup rule below.

#### Scenario: Map covers the file
- **WHEN** the map test runs at the commit that introduces the map
- **THEN** every top-level `def`/`class`/assignment name in the facade is assigned exactly once and every patched name is listed with its call-site clusters

### Requirement: Journal responsibility clusters SHALL be extracted behind a stable facade
`services/orchestrator/file_orchestration_journal.py` SHALL be reduced by moving, in the order `context` (`file_journal_context.py`), `read_attribution` (`file_journal_read_attribution.py`), `primitives` (`file_journal_primitives.py`), `replay` (`file_journal_replay.py`), `candidates` (`file_journal_candidates.py`), `paths` (`file_journal_paths.py`), `retry_helpers` (`file_journal_retry_helpers.py`) the definitions the map assigns to that cluster. After each step the facade SHALL re-export (listed in `__all__`, `# noqa: F401` where needed) every moved name that any non-test module **imports** and every moved name a test **imports**; a name whose only test references are monkeypatches SHALL NOT be re-exported (base requirement "Relocated monkeypatch seams keep their negative oracles live"). Every existing test file that references the journal module SHALL pass with no edits other than patch-target repointing listed for that step.

Patched seams follow two rules:
- A patched name moves only together with **all** its call sites; the tests' patch targets move to the new owner in the same PR and the facade does not re-export it.
- A patched name with a call site that stays in the facade (the `FileOrchestrationJournalRepository` and `FileJournalRetryService` classes stay; today this covers all nineteen patched names recorded in the cluster map's `patched` table, including `_utcnow`, `read_bytes_limited_no_follow`, `list_directory_no_follow_limited`, `_stat_signature`, `_iter_regular_json_files`, `_iter_jsonl_files`, `_released_candidate_cycle_scope`, `_cycle_scope_from_job_id`, `_next_file_manual_retry_job_id_for_run`) stays in the facade; a moved function that calls it SHALL resolve it at call time as an attribute of the facade module (late import inside the function body), never by binding the name at import time, so a patch on the facade still reaches every call.

#### Scenario: Extraction step keeps importers and patches working
- **WHEN** a cluster is moved
- **THEN** every test under `tests/` that imports or patches `services.orchestrator.file_orchestration_journal` passes, with test edits limited to the patch-target strings of names the map marks as moving with all their call sites

#### Scenario: Facade-resident seam still intercepts a moved caller
- **WHEN** a test patches `file_orchestration_journal._stat_signature` with a counting stub and exercises a function that moved to `file_journal_paths.py`
- **THEN** the stub's call count is greater than zero

#### Scenario: Paths-cluster walkers reach facade-resident IO names
- **WHEN** a test patches `file_orchestration_journal.stat_no_follow` and `file_orchestration_journal.list_directory_no_follow_limited` with counting stubs and iterates the moved `_iter_discovered_files` or `_iter_retention_surface_files` in `file_journal_paths.py`
- **THEN** each stub's call count is greater than zero

#### Scenario: Candidates-cluster callers of facade-resident patched names
- **WHEN** a test patches `file_orchestration_journal.read_bytes_limited_no_follow` or `file_orchestration_journal.accepted_submit_candidate_immutable_evidence` with a counting stub and calls the moved `_run_manifest_model_package_identity` or `_candidate_row_self_bound_identity` in `file_journal_candidates.py`
- **THEN** the stub's call count is greater than zero

#### Scenario: Retry-helper callers of the facade clock
- **WHEN** a test patches `file_orchestration_journal._utcnow` with a counting stub and calls the moved `_journal_record_for_write` or `_private_runtime_root_recovery_record` in `file_journal_retry_helpers.py`
- **THEN** the stub's call count is greater than zero and the record's `created_at` is the stub's value

#### Scenario: Annotation-only back references
- **WHEN** a moved module needs a facade-resident name (e.g. `FileOrchestrationJournalRepository`, `FileOrchestrationJournalError`) only in annotations
- **THEN** it imports it under `TYPE_CHECKING` and no import cycle is introduced

### Requirement: Journal contract SHALL remove dead re-exports
After the last extraction, the facade's `__all__` SHALL contain only names with a remaining importer, where "importer" means an AST-resolved `from … import` or attribute access through any alias of the facade module, or a dotted-string patch target, found by scanning `apps/ packages/ services/ workers/ scripts/ tests/`. A test SHALL fail if `__all__` lists a name no module uses by that definition, or if a module other than the facade and its `file_journal_*.py` siblings imports or reads through an alias a facade name missing from `__all__` (monkeypatch-only targets are never listed), and the facade's remaining top-level definitions SHALL equal the map's `facade` cluster.

#### Scenario: Unused re-export detected
- **WHEN** `__all__` lists `_foo` and no file imports, accesses or patches `file_orchestration_journal._foo`
- **THEN** the re-export hygiene test fails naming `_foo`

#### Scenario: Unlisted external read detected
- **WHEN** a non-sibling module imports `file_orchestration_journal._bar` and `__all__` does not list `_bar`
- **THEN** the re-export hygiene test fails naming `_bar`
