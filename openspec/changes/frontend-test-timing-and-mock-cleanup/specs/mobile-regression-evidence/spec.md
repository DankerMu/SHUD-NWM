## ADDED Requirements

### Requirement: Regression tests SHALL wait on the state they assert

A regression case that changes the viewport on a loaded page SHALL, before measuring, wait on a condition that only holds once the rendered structure for the new viewport form has been committed; a style-only condition SHALL NOT be the only gate before assertions that depend on rendered structure. The display-lane unit cases for headers-only, failed-completion and stalled-body responses SHALL run on an injected clock and SHALL each assert one specific failure code and stage.

#### Scenario: Rotating the control bar on a loaded page

- **WHEN** the control-bar rotation case switches a loaded page from portrait to short landscape
- **THEN** it waits for the landscape structure of the control bar before measuring geometry

#### Scenario: Time-budget cases are deterministic

- **WHEN** the display-lane unit tests exercise headers-only, failed-completion and stalled-body responses
- **THEN** each case runs on an injected clock and asserts exactly one failure code and stage

### Requirement: Every documented test entry point SHALL select tests

A package script that documentation or a workflow names as a test entry point SHALL select at least one test and exit zero when they pass. The M15 visual conformance spec SHALL stay excluded from every project of the mocked regression lane and SHALL be runnable through its own script.

#### Scenario: The M15 visual script runs its spec

- **WHEN** `pnpm run test:e2e:m15-visual --list` is run
- **THEN** at least one test is listed and the command exits zero

#### Scenario: The mocked lane still excludes it

- **WHEN** the mocked regression lane lists its tests
- **THEN** no test from the M15 visual conformance spec is listed
