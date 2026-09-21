## ADDED Requirements

### Requirement: Guard exclusion burndown removes ten oversized surfaces without replacement

The repository SHALL split the ten guard-excluded oversized surfaces named by this
change so that every resulting file is below the 1,000-line limit, and SHALL remove
exactly those ten entries from `.large-file-guard.json` in the same commit as each
split. No replacement exclusion SHALL be added for any file a split produces. Where a
split forces an edit to a file that already exceeded the limit before this change and
was never excluded, the change MAY add one exclusion for that pre-existing surface,
and SHALL record the deviation and file a separate split issue for it. Both `maxLines`
and every unrelated exclusion SHALL remain byte-identical.

#### Scenario: the ten exclusions disappear and nothing takes their place

- **WHEN** `.large-file-guard.json` is compared against its pre-change content
- **THEN** exactly the ten named entries are absent, the only added entry is a
  recorded pre-existing oversize surface that a split had to edit, and `maxLines`
  plus every remaining exclusion is byte-identical
- **AND** every file produced by the ten splits is below 1,000 lines.

#### Scenario: a split lands without its exclusion removal

- **WHEN** a split commit leaves its source path in the exclusion list
- **THEN** the change is incomplete, because the guard signal for that surface
  stays closed and the burndown direction is not restored.

### Requirement: Relocated monkeypatch seams keep their negative oracles live

A relocated patch seam SHALL keep its negative oracle live: when a symbol that tests
patch through `monkeypatch.setattr(<module>, <name>, ...)` moves to a new owner
module, the change SHALL move every call site of that symbol with it, SHALL repoint the patch target to the new owner in the same commit, and
SHALL NOT re-export that symbol from the original module. Symbols whose call sites
remain in the original module SHALL keep their existing patch target unchanged.

#### Scenario: a negative case still reaches the real call site

- **WHEN** a test replaces a relocated seam with a forbidden or failing stub
- **THEN** the stub is installed on the module that actually performs the call,
  and deliberately breaking the real call site turns that test red
- **AND** the original module does not expose the relocated name, so a patch aimed
  at the stale target raises instead of passing vacuously.

### Requirement: Copyback replay and retention mutex corpora partition without drift

The repository SHALL partition the scheduler state-index copyback replay suite and
the retention copyback-mutex corpus into collectible modules below 1,000 lines, with
shared definitions confined to non-collectible helpers and no collectible
compatibility shim. The retention copyback-mutex lane SHALL move to its own owner
module while `services/orchestrator/retention.py` retains the planner. Copyback
mutex semantics, budget semantics, and deletion selection SHALL be unchanged.

#### Scenario: collection identity survives partitioning

- **WHEN** the replay and mutex corpora are collected after partitioning
- **THEN** the sorted `::test_name[param-id]` suffix sets equal the pre-split
  baselines of 32 and 25 exactly, with no additions, losses, duplicates or skips
- **AND** assertions, decorators, fixtures and parameter IDs are unchanged.

#### Scenario: targeted selection follows the retention owner split

- **WHEN** targeted selection runs for `services/orchestrator/retention.py` or for
  the extracted copyback-mutex owner
- **THEN** every retention partition, including the newly split files, is selected
- **AND** the selector contract tests pass.

#### Scenario: an orphaned partition cannot hide behind same-name derivation

- **WHEN** a corpus is partitioned into suites that have no same-named production
  source, so same-name derivation no longer routes them and a vanished rule target
  only emits a warning
- **THEN** the partition is routed by an explicit `PathTestRule` that enumerates every
  new suite, and a tracked-tree guard asserts the corpus is exactly the intended
  number of collectible suites plus non-collectible helpers
- **AND** adding a surplus partition, leaving a compatibility shim, or turning a
  helper into a suite turns that guard red.

### Requirement: Scheduler registry refresh surfaces split behind stable CLI facades

The repository SHALL split the scheduler file-provider refresh script, the registry
publish script, and their two pytest corpora below 1,000 lines. Both historical script paths
SHALL remain executable facades: `python -m` entrypoints, argparse surface, help
output, environment variables, flags, exit codes, `schema_version` and audit field
semantics SHALL be equivalent to the pre-split owners.

#### Scenario: the CLI contract is byte-stable across the split

- **WHEN** `python -m scripts.scheduler_file_provider_refresh --help` and
  `python -m scripts.publish_scheduler_file_registry --help` run after the split
- **THEN** both exit zero and emit stdout byte-identical to the pre-split baseline.

#### Scenario: both refresh corpora collect identically

- **WHEN** the refresh and publish suites are collected after partitioning
- **THEN** the sorted suffix sets equal the pre-split baselines of 315 and 59
- **AND** every new partition is named by an explicit `PathTestRule` rather than
  inferred from a same-name source pair.

### Requirement: Entropy audit enforcement and its corpus split without report drift

The repository SHALL split `scripts/governance/audit_repo_entropy.py` and
`tests/test_entropy_audit_script.py` below 1,000 lines with shared definitions in
support modules. The audit's check identifiers, module heatmap keys, public function
set, exit codes, and the stable subset of `metadata` SHALL be unchanged. The stable
subset is exactly `schema_version`, `mode`, `check_family_count`,
`executed_check_families`, `skipped_path_families`, `max_scanned_text_file_bytes`,
`max_artifact_fingerprint_bytes` and `baseline_path`; the remaining `metadata` keys
are excluded because they embed wall-clock time, the absolute repository root, the
resolved comparison base ref, or counts of the audit's own tracked lines, all of
which drift between any two invocations. The split SHALL also land the two deferred #1809
items: five expected-command literals migrated to the `tests/test_gateway_reconcile_*.py`
glob and the two frozen pre-#1809 provenance lines removed from the compatibility
inventories. Verification-command literals in `_ScopedAgentContextConfig`, the scoped
`AGENTS.md` files, the governance inventories and the triage document SHALL name the
new partitions.

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
- **AND** no frozen pre-#1809 guard literal remains in either compatibility
  inventory, and the five expected commands name the gateway-reconcile glob.

### Requirement: Production ops runbook becomes an index over sub-runbooks

`docs/runbooks/current-production-ops.md` SHALL be split into
`docs/runbooks/production-ops/` sub-runbooks each below 1,000 lines, with the
historical path retained as an index landing page below 1,000 lines. Commands, jq
expressions and reproduction steps SHALL be preserved verbatim, and every existing
inbound reference and anchor SHALL still resolve.

#### Scenario: inbound links and anchors survive the split

- **WHEN** the references to `current-production-ops.md` are resolved after the split
- **THEN** every referencing file still resolves, the anchor
  `#311-pipeline-job-provenance-sidecar-and-recovery-2420` still resolves, and
  markdown lint passes
- **AND** the targeted selector routes the document's new paths.
