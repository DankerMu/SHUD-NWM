## ADDED Requirements

### Requirement: Visual regression governance
The project SHALL define how visual evidence is reviewed and when regressions block implementation.

#### Scenario: New visual PR
WHEN a PR changes shared visual components or map-first pages
THEN the PR updates or references current screenshot evidence and explains acceptable deltas

#### Scenario: Failure criteria
WHEN text overflows, panels overlap, or controls lose accessible names
THEN visual conformance fails until corrected or explicitly scoped out

#### Scenario: Review checklist
WHEN M15 closes
THEN progress or governance documentation lists required routes, extended routes, viewports, state labels, evidence path, SHA metadata, acceptable deltas, and blocking visual regressions

#### Scenario: Evidence SHA identity
WHEN visual evidence is generated for CI or PR review
THEN every manifest entry uses the checked-out `git rev-parse HEAD` commit SHA, PR CI checks out the pull request head SHA, and evidence capture rejects placeholder or environment SHA values that differ from `HEAD`

#### Scenario: Evidence artifact closure
WHEN visual evidence is generated under `.codex/evidence/issue-176`
THEN the run cleans or rejects stale screenshots so uploaded PNG artifacts are exactly the manifest-listed screenshots

#### Scenario: CI and local evidence split
WHEN screenshot binaries are too volatile for git
THEN the repository documents local evidence paths and commands without requiring volatile screenshots to be committed
