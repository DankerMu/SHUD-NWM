## MODIFIED Requirements

### Requirement: Entropy audit enforcement and its corpus split without report drift

The repository SHALL split `scripts/governance/audit_repo_entropy.py` and
its former single test module (now the `tests/test_entropy_audit_*.py` partitions of
`ENTROPY_AUDIT_TESTS`) below 1,000 lines with shared definitions in
support modules. The audit's check identifiers, module heatmap keys, public function
set, exit codes, and the stable subset of `metadata` SHALL be unchanged. The stable
subset is exactly `schema_version`, `mode`, `check_family_count`,
`executed_check_families`, `skipped_path_families`, `max_scanned_text_file_bytes`,
`max_artifact_fingerprint_bytes` and `baseline_path`; the remaining `metadata` keys
are excluded because they embed wall-clock time, the absolute repository root, the
resolved comparison base ref, or counts of the audit's own tracked lines, all of
which drift between any two invocations. The split SHALL also land the two deferred #1809
items: five expected-command literals migrated to the `tests/test_gateway_reconcile_*.py`
glob and the two frozen pre-#1809 provenance lines removed. Verification-command literals
in `_ScopedAgentContextConfig`, the scoped `AGENTS.md` files,
`docs/governance/STRUCTURAL_FILE_DISPOSITION_INVENTORY.md` and the triage document SHALL
name the new partitions.

#### Scenario: the audit report is unchanged by its own split

- **WHEN** `--format json` runs immediately before and after the enforcement split,
  with no other repository change between the two runs
- **THEN** the eight stable `metadata` keys are key-for-key equal, the check-id set,
  the `module_heatmap` and `high_spread_patterns` key sets, and the public function
  set are equal, and the exit code is unchanged
- **AND** the full `findings` diff is empty, or every residual entry is attributed to
  a path this split created or removed and is enumerated in the pull request body;
  an unattributable residual is behavior drift and the split is reverted.

#### Scenario: the corpus collects identically and the deferred items land

- **WHEN** the entropy audit suites are collected after partitioning
- **THEN** the sorted suffix set equals the pre-split baseline of 410
- **AND** no frozen pre-#1809 guard literal remains, and the five expected
  commands name the gateway-reconcile glob.

### Requirement: The journal cluster map SHALL be a checked-in artifact before any extraction
`tests/fixtures/file_journal_cluster_map.json` SHALL assign every top-level definition of `services/orchestrator/file_orchestration_journal.py` and of every `services/orchestrator/file_journal_*.py` sibling to exactly one registered cluster. The registered clusters are `facade`, the seven extraction clusters `context`, `read_attribution`, `primitives`, `replay`, `candidates`, `paths`, `retry_helpers`, and the repository-mixin clusters `retention`, `events`, `release`, `reclaim`, `operator`, `cohort`, `submit`, `reconcile_inventory`; a later mixin module SHALL be registered as a further cluster before it can exist on disk. The map SHALL list, for every name that any test monkeypatches on the facade (found by searching `tests/` for `file_orchestration_journal.<name>` patch targets and for `setattr(<alias>, "<name>"`), its cluster and the clusters of all its call sites. A test SHALL fail when a top-level definition is unassigned, assigned twice, or when a patched name is assigned to a cluster other than `facade` while one of its call sites is assigned elsewhere without the late-lookup rule below.

#### Scenario: Map covers the file
- **WHEN** the map test runs at the commit that introduces the map
- **THEN** every top-level `def`/`class`/assignment name in the facade is assigned exactly once and every patched name is listed with its call-site clusters

### Requirement: Journal responsibility clusters SHALL be extracted behind a stable facade
`services/orchestrator/file_orchestration_journal.py` SHALL be reduced by moving, in the order `context` (`file_journal_context.py`), `read_attribution` (`file_journal_read_attribution.py`), `primitives` (`file_journal_primitives.py`), `replay` (`file_journal_replay.py`), `candidates` (`file_journal_candidates.py`), `paths` (`file_journal_paths.py`), `retry_helpers` (`file_journal_retry_helpers.py`), and then the repository-mixin clusters `retention`, `events`, `release`, `reclaim`, `operator`, `cohort`, `submit`, `reconcile_inventory` (each in `file_journal_<cluster>.py`, as one mixin class that `FileOrchestrationJournalRepository` composes as a base), the definitions the map assigns to that cluster. After each step the facade SHALL re-export (listed in `__all__`, `# noqa: F401` where needed) every moved name that any non-test module **imports** and every moved name a test **imports**; a name whose only test references are monkeypatches SHALL NOT be re-exported (base requirement "Relocated monkeypatch seams keep their negative oracles live"). Every existing test file that references the journal module SHALL pass with no edits other than patch-target repointing listed for that step.

Patched seams follow three rules:
- A patched name moves only together with **all** its call sites; the tests' patch targets move to the new owner in the same PR and the facade does not re-export it.
- A patched name that the cluster map's `patched` table assigns to `facade` stays in the facade wherever its call sites live (the `FileOrchestrationJournalRepository` and `FileJournalRetryService` classes stay; today every name in the table is facade-resident, including `_utcnow`, `read_bytes_limited_no_follow`, `list_directory_no_follow_limited`, `unlink_no_follow`, `stat_no_follow`, `_stat_signature`, `_iter_regular_json_files`, `_iter_jsonl_files`, `_released_candidate_cycle_scope`, `_cycle_scope_from_job_id`; `_next_file_manual_retry_job_id_for_run` left the table when #3157 deleted the manual-retry pending-row allocator). A moved function that calls it SHALL resolve it at call time as an attribute of the facade module (late import inside the function body), never by binding the name at import time, so a patch on the facade still reaches every call.
- A sibling module SHALL NOT read the origin of a patched name that the facade binds by import: neither by importing the origin name from any module under any alias, nor by reading it as an attribute of anything other than the facade module. A test SHALL fail naming the sibling and the name when it does.

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
- **WHEN** a test patches `file_orchestration_journal._utcnow` with a counting stub and calls the moved `_journal_record_for_write` in `file_journal_retry_helpers.py` (`_private_runtime_root_recovery_record` was deleted with its write-only lane by #3183)
- **THEN** the stub's call count is greater than zero and the record's `created_at` is the stub's value

#### Scenario: Annotation-only back references
- **WHEN** a moved module needs a facade-resident name (e.g. `FileOrchestrationJournalRepository`, `FileOrchestrationJournalError`) only in annotations
- **THEN** it imports it under `TYPE_CHECKING` and no import cycle is introduced

#### Scenario: Repository-mixin callers reach the facade seam
- **WHEN** a test patches a facade-resident patched name on `file_orchestration_journal` with a stub that records its caller's module and drives a method of a repository mixin whose cluster the map lists among that name's callers
- **THEN** the stub is called from that mixin's module
- **AND** a test fails when the set of (patched name, mixin cluster) pairs that have a seam test differs from the pairs in the map

#### Scenario: Origin bypass in a sibling is rejected
- **WHEN** one call site in a sibling reads `utc_now` imported from `packages.common.contracts.time_normalization`, or `safe_fs.stat_no_follow` through a module alias, while the other sites of that sibling still read the facade attribute
- **THEN** the late-lookup lint fails and names the sibling and the facade name
