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

#### Scenario: CI and local evidence split
WHEN screenshot binaries are too volatile for git
THEN the repository documents local evidence paths and commands without requiring volatile screenshots to be committed
