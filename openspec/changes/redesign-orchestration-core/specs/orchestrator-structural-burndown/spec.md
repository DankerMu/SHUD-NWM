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
