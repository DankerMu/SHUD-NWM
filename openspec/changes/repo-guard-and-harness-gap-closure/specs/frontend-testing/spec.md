## ADDED Requirements

### Requirement: The mocked-regression Playwright lane SHALL be an automatic CI gate on frontend changes

The deterministic mocked-regression lane SHALL execute automatically in CI on every pull request whose changed files match the workflow's `frontend` path filter,
and SHALL fail the run when any of its tests fails. The lane SHALL NOT be configured with test retries:
this project has no recorded flakiness for it, and a retry would convert a real failure into a green run.

This requirement governs only the mocked lane. The `live-display` and `live-river-click` lanes bind to a real
runtime and keep node-27 as their oracle; the M15 visual-conformance lane stays manually dispatched under the
existing governance decision. A change that is not matched by the `frontend` filter SHALL NOT pay the lane's cost. A change that IS matched pays
it even when it touches no frontend source — the filter also covers the OpenAPI contract directory — and that cost
is accepted rather than avoided by a second, narrower filter, because a second filter would be a second path truth
to keep correct.

#### Scenario: A frontend pull request executes the lane

- **GIVEN** a pull request whose changed files match the CI `frontend` path filter
- **WHEN** the frontend CI job runs
- **THEN** the mocked-regression lane executes its specs and the run's log records a non-zero passed count,
  rather than skipping the lane or collecting it without executing assertions

#### Scenario: A broken mocked assertion turns the gate red

- **GIVEN** a branch in which one assertion in a mocked-regression spec has been inverted so the spec must fail
- **WHEN** the frontend CI job runs on that branch
- **THEN** the job fails, so the gate is proven to bite rather than exiting zero

#### Scenario: A backend-only or docs-only pull request does not run the lane

- **GIVEN** a pull request whose changed files match neither the `frontend` path filter nor any of its members
- **WHEN** CI runs
- **THEN** the frontend job is skipped and the lane costs nothing
