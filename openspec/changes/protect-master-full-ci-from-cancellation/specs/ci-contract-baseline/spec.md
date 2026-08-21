## ADDED Requirements

### Requirement: CI concurrency MUST preserve every non-PR workflow run

The CI workflow MUST group pull-request runs by pull-request identity and cancel superseded runs for that same pull request, while every `push` and `workflow_dispatch` run MUST use its own workflow-run identity and MUST NOT cancel or replace another non-PR run that is running or pending.

#### Scenario: Superseded pull-request run is cancelled

- **WHEN** a new CI run starts for the same pull request
- **THEN** both runs resolve to the same PR-scoped concurrency group and `cancel-in-progress` is true for that event

#### Scenario: Master pushes cannot cancel or replace each other

- **WHEN** two `push` events on `master` create separate CI workflow runs
- **THEN** their groups differ by `github.run_id`, `cancel-in-progress` is false, and neither run is removed by this workflow's concurrency policy

#### Scenario: Manual full CI is isolated from master pushes

- **WHEN** a `workflow_dispatch` run overlaps a `push` run
- **THEN** each uses its own `github.run_id` group and neither can cancel or replace the other through this concurrency policy

### Requirement: CI workflow policy changes MUST execute their contract suite

A pull request that changes `.github/workflows/ci.yml` MUST open the backend targeted-test gate and the selector MUST choose `tests/test_select_ci_tests.py`, so concurrency, paths-filter, and workflow-consumer contracts execute assertions on the same pull request that changes them. The paths-filter PR files result MUST be the single changed-file authority consumed by the selector; the selector MUST NOT recompute a merge-base diff that can diverge after master changes while the pull request is open.

#### Scenario: Workflow-only change reaches assertion-level tests

- **WHEN** the changed-file set contains `.github/workflows/ci.yml`
- **THEN** the backend paths-filter matches and targeted selection contains `tests/test_select_ci_tests.py` rather than collapsing to zero-assertion collect-only smoke

#### Scenario: Removing either routing leg fails a guard

- **WHEN** the workflow path is removed from either the backend filter or the selector rule
- **THEN** the selector contract suite fails and names the missing self-routing leg

#### Scenario: Selector consumes the same pull-request changed-file set as paths-filter

- **WHEN** master changes while a pull request remains open, including an identical change to a file also touched by the pull request
- **THEN** the targeted selector consumes the paths-filter `all_files` output for that pull request and cannot silently drop the file through a separate merge-base diff
