## ADDED Requirements

### Requirement: Default Python matches the merge gate

The repository SHALL track a Python 3.11 major/minor pin used by default `uv` commands while retaining `requires-python >=3.11` and permitting explicit supported-version runs.

#### Scenario: Default and explicit interpreter selection

- **WHEN** a developer synchronizes a clean checkout and runs `uv run python -V`
- **THEN** the command reports Python 3.11.x
- **AND** `uv run --python 3.14 python -V` can explicitly select Python 3.14

#### Scenario: Newer standard-library API is rejected by default

- **WHEN** default `uv run python` invokes `Path.rglob(..., recurse_symlinks=True)`
- **THEN** Python 3.11 raises `TypeError` instead of allowing a 3.13-only API to escape local verification

### Requirement: Hook configuration follows the active Git worktree

The large-file guard SHALL resolve `.large-file-guard.json` from the Git top level governing the tool-call `cwd`, use the same worktree for Git inspection, and identify the effective config path when blocking.

#### Scenario: Worktree-local exclusion overrides the main checkout context

- **WHEN** `CLAUDE_PROJECT_DIR` names the main checkout, the tool-call `cwd` names a linked worktree, and only the worktree config excludes a staged oversized file
- **THEN** the hook accepts the commit attempt using the worktree config

#### Scenario: Nested worktree cwd resolves to its Git top level

- **WHEN** `CLAUDE_PROJECT_DIR` names the main checkout, the tool-call `cwd` is a nested directory in a linked worktree, and `.large-file-guard.json` exists only at that worktree's Git top level
- **THEN** the hook uses `{worktree-top-level}/.large-file-guard.json` rather than `{cwd}/.large-file-guard.json` for both exclusion passes and block diagnostics

#### Scenario: Worktree block names its config

- **WHEN** the active worktree config does not exclude a staged oversized file
- **THEN** the hook exits 2 and its diagnostic names the exact worktree config path it read

#### Scenario: Existing merge filtering remains compatible

- **WHEN** a merge conclusion contains an oversized file inherited unchanged from another parent
- **THEN** the hook does not attribute that file to the merge conclusion
- **AND** it still blocks newly authored oversized content

### Requirement: Replay reason ownership uses stable identifiers

The replay tool SHALL maintain complete ownership metadata for every `_PRE_COMMIT_INDEX_REASONS` member using one or more source function names rather than source line numbers, preserving all audited raise-point owners represented by the replaced index without changing the allowlist or classification behavior.

#### Scenario: Every refusal reason has a valid owner

- **WHEN** focused tests inspect `_PRE_COMMIT_INDEX_REASONS` and its ownership metadata
- **THEN** their key sets are equal
- **AND** known multi-owner reasons retain every function represented by the replaced audit index
- **AND** every named owner function in `packages/common/state_manager.py` contains the corresponding reason literal

#### Scenario: Unrelated source insertion does not stale the audit index

- **WHEN** unrelated lines are inserted before a mapped raise function
- **THEN** the function-name ownership remains valid without updating numeric citations

#### Scenario: Replay behavior is preserved

- **WHEN** the existing replay focused suite exercises pre-commit refusals and commit-uncertain outcomes
- **THEN** allowlist membership, exit codes, receipt behavior, and classification results remain unchanged