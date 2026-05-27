# Reviewer Packages

Select the smallest reviewer set that covers the artifact risk. Add reviewers
for risk, not for ceremony.

## PR Review

Low or medium implementation reviews use four reviewers:

| Reviewer | Scope |
| --- | --- |
| Correctness | Behavior, edge cases, regression risk, error handling |
| Integration | Callers, contracts, module boundaries, deployment/runtime fit |
| Security/Performance | Auth, input handling, data exposure, path safety, complexity, resource use |
| Test & Evidence Coverage | Tests, verification commands, fixtures, observability, proof quality |

High-risk implementation reviews add:

| Reviewer | Scope |
| --- | --- |
| Spec Compliance | Checks implementation against OpenSpec/design/issue acceptance criteria |
| Invariant/State-Machine/Compatibility | Checks end-to-end identity, state transitions, sibling surfaces, backward compatibility |

## OpenSpec Review

Stage-change and OpenSpec reviews use three reviewers:

| Reviewer | Scope |
| --- | --- |
| Design Consistency | Change files vs design docs: names, IDs, endpoints, schema fields, enum values |
| Spec Completeness | Requirements/scenarios: testability, edge cases, cross-spec consistency, missing capabilities |
| Tasks Executability | Task granularity, dependency order, coverage of specs, verification clarity, small-PR fit |

## Hybrid Review

When reviewing a PR against OpenSpec, combine:

- Spec Compliance
- Correctness
- Integration
- Security/Performance
- Test & Evidence Coverage
- Invariant/State-Machine/Compatibility for high-risk surfaces

## Prompt Requirements

Every reviewer prompt should include:

- artifact scope and exact files or refs
- selected risk level and why
- specific checklist for that reviewer only
- instruction to report only actionable findings
- output format with severity, failure class, evidence, scenario, fix direction, and blocking status
- delegation boundary forbidding nested AI/codeagent/subagent calls
