## ADDED Requirements

### Requirement: Guard identity follows the operation worktree

The large-file guard SHALL resolve the Git top level governing the tool-call `cwd` and SHALL use that root for configuration, Git inspection, direct filesystem reads, merge metadata, and diagnostics.

#### Scenario: Nested linked-worktree cwd uses worktree policy

- **GIVEN** `CLAUDE_PROJECT_DIR` names the main checkout and tool-call `cwd` names a nested directory in a linked worktree
- **WHEN** only the worktree config excludes a staged oversized file
- **THEN** the hook accepts the operation using `{worktree-top-level}/.large-file-guard.json`

#### Scenario: Worktree block names the effective config

- **WHEN** the worktree config does not exclude an oversized file
- **THEN** the hook exits 2
- **AND** the diagnostic names the exact worktree config path

#### Scenario: Missing or non-Git cwd uses the bounded fallback

- **WHEN** tool-call `cwd` is absent or is not inside a Git repository
- **THEN** the hook uses the Git top level associated with `CLAUDE_PROJECT_DIR`
- **AND** config, files, MERGE_HEAD, and diagnostics remain bound to that fallback root

### Requirement: Git path identity preserves legal bytes

Path-valued Git output SHALL retain every legal filesystem byte and SHALL remove only the one LF delimiter emitted by the Git command protocol.

#### Scenario: Whitespace and control-byte suffixes remain identity data

- **WHEN** a worktree path ends in a space, carriage return, or line feed byte
- **THEN** exclusions, oversized-file reads, and diagnostics use that exact path
- **AND** no generic whitespace or universal-newline normalization changes it

### Requirement: Existing file and merge behavior remains compatible

The worktree-local repair SHALL preserve direct tracked-file inspection and merge-parent attribution.

#### Scenario: Linked worktree commit-a sees direct modifications

- **WHEN** `commit -a` includes an unstaged oversized modification to a tracked file
- **THEN** the hook reads it from the resolved linked worktree and blocks it

#### Scenario: Merge conclusion ignores inherited oversized content

- **WHEN** a merge conclusion inherits an oversized file unchanged from another parent
- **THEN** the hook does not attribute that file to the merge conclusion
- **AND** it still blocks newly authored oversized content

#### Scenario: Fallback merge metadata uses the fallback root

- **WHEN** absent or non-Git `cwd` activates the fallback during a merge conclusion
- **THEN** MERGE_HEAD is resolved from the same fallback Git root
- **AND** inherited/newly-authored attribution remains correct
