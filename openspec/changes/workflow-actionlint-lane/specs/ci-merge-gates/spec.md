## ADDED Requirements

### Requirement: Workflow files SHALL be linted on pull requests that change them

The CI workflow SHALL run a pinned actionlint over every file under `.github/workflows/` in a job gated by a `workflows` path filter that matches `.github/workflows/**`. The job SHALL NOT run on pull requests that leave the workflow files untouched and SHALL NOT be a dependency of any other job.

#### Scenario: A pull request edits a workflow

- **WHEN** a pull request changes any file under `.github/workflows/`
- **THEN** the Workflow Lint job runs and fails the pull request if actionlint reports a finding

#### Scenario: The filter entry is lost

- **WHEN** the `.github/workflows/**` entry is removed from the `workflows` filter block or moved under another filter
- **THEN** a meta-test in the targeted backend suite fails
