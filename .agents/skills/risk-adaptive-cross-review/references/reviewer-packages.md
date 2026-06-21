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

## Reviewer Checklists

Canonical per-reviewer scope. Workflows that run these reviewers (e.g.
`subagent-workflow` Phase 4) inline the relevant checklist into each subagent
brief instead of restating it. Apply only the checklist for the reviewer's role
plus any triggered cross-cutting lens below. Findings follow
[finding-contract.md](finding-contract.md).

### Correctness

- Logic correctness within each changed file.
- The governing invariant cannot be violated by valid boundary, stale, mismatched, or unauthorized inputs.
- Function signatures match the target API; type safety holds at call sites.
- Edge cases: null/empty inputs, boundary values, off-by-one at first/last/single-item.
- Control flow: error branches, early returns, loop bounds.
- Correctness against the selected risk packs.
- If a bug reveals a reusable unsafe pattern, name all sibling call sites that must satisfy the same invariant.

### Integration

- Return-value contracts match downstream expectations; callers/consumers migrated to any changed signature, enum, default, pagination, persistence shape, or async timing.
- Apply the **removed-behavior audit** lens for every deleted/rewritten line.
- Trace the source-of-truth identity/contract through producers, validators, storage/cache/query, routes/entrypoints, consumers, and failure paths.
- Shared variables flow correctly from setup/config to consumers; execution order satisfies prerequisites.
- Unchanged consumers of changed outputs still work; compatibility axes preserved.
- Producer/consumer, receipt/summary, schema, and evidence flows bind to one identity and cannot mix from sibling paths or stale state.
- Apply the **wrapper/proxy faithfulness** lens when the change touches a wrapper/adapter/cache/proxy.

### Security/Performance

- Path safety: traversal, symlinks, overwrite behavior.
- Adversarial/boundary inputs cannot bypass the selected risk-pack controls.
- Resource management: file handles, connections, device/open-close pairing.
- Unbounded operations: loops without guards, large allocations, memory safety.
- Data integrity: no silent numerical or semantic changes.
- Info leakage: tokens, keys, credentials in logs or output.
- Performance regressions introduced by the diff: repeated I/O, N+1, serialization, blocking hot path, avoidable copy. Name the cheaper option.
- For path/evidence/resource findings, check analogous sibling modules and shared helpers before concluding the pack is closed.

### Test & Evidence Coverage

- Map every selected task and required evidence to an explicit test, command, fixture, or justified non-goal.
- Positive and failure/boundary regression evidence where applicable.
- Tests exercise real integration surfaces when the fixture requires real DB/state/orchestrator/API behavior; fake-only coverage is insufficient unless the fixture says so.
- Unchanged downstream consumer compatibility tests named by the fixture.
- Local verification covers touched modules and omits no selected risk pack.
- Flag stale, overbroad, or misleading evidence claims.

### Spec Compliance

- Cross-check every task/requirement against the diff; mark DONE or MISSING.
- Flag scope creep not covered by the issue or fixture; verify acceptance criteria.
- Check test coverage against the task list and that selected risk packs are addressed.
- If a risk pack has repeated or analogous surfaces, verify the evidence covers the invariant across them, not one example.

### Invariant/State-Machine/Compatibility

- Trace the governing invariant across producers, validators, storage/cache/query, entrypoints, consumers, failure paths, stale-state boundaries, and evidence surfaces.
- For state machines: transition ordering, terminal/active/manual states, retry limits, cancellation gaps, stale DB/cache boundaries, no duplicate or lost transition.
- Identity cannot collapse across siblings, stale rows, aggregate rows, or partial-success manifests.
- Backward compatibility for unchanged consumers and older persisted state shapes.
- A reusable unsafe state/helper pattern means the full sibling surface must be audited.

## Cross-Cutting Review Lenses

Change-triggered lenses, not separate reviewers. Each named reviewer also applies
the lenses relevant to its scope whenever the diff trips a trigger. A lens finding
still obeys the finding contract: severity, failure class, violated invariant,
concrete scenario, sibling surfaces, blocking status.

### Removed-behavior audit

Owners: Correctness, Integration. Mandatory at medium and high risk.

For every line the diff deletes or rewrites, name the invariant, guard, validation,
error path, or test it guaranteed, then locate where the new code re-establishes it.
If it is not re-established, that is a finding: state the old guarantee, the
now-missing path, and the observable failure.

Triggers: removed or narrowed validation/guard; dropped error handling, rollback,
cleanup, or cancellation; deleted migration/backfill safety step; removed
permission/ownership/tenant/CSRF/OAuth/auth check; deleted retry/idempotency/ordering
that callers still rely on; removed test that documented real production behavior with
no equivalent coverage.

### Wrapper/proxy faithfulness

Owners: Integration, Security/Performance. Mandatory when the diff adds or changes a
wrapper, adapter, cache, proxy, decorator, facade, client, or repository.

- Every method forwards to the intended wrapped instance, not back through a global
  registry, singleton, session, or the wrapper itself (recursion).
- Cache keys include every value that changes the result: tenant, auth scope, locale,
  flags, pagination cursor, headers, method, body, version. Name the missing dimension
  and the wrong cached result it returns.
- Invalidation, TTL, error caching, partial failure, and concurrent-fill behavior hold.
- Return values, thrown errors, cancellation, streaming/backpressure, ordering, and
  side effects are preserved; all methods callers use are forwarded.

Failure classes: wrapper forwarding, stale/cross-tenant cache, dropped method,
swallowed error.

### Altitude / ownership

Owners: Correctness, Integration, Spec Compliance. Apply on any change.

Check that each change lives at the right depth. A finding names the rightful owner
and why the current layer is too shallow or too deep:

- A special case in a route/UI/CLI that the domain/service/schema owner should enforce.
- Validation duplicated at the edge while the central invariant stays weak.
- A caller-specific workaround for a callee contract bug.
- Business logic pushed into serialization, rendering, migration glue, or tests.
- A patch that suppresses an error instead of fixing the source of the invalid state.

## Prompt Requirements

Every reviewer prompt should include:

- artifact scope and exact files or refs
- selected risk level and why
- specific checklist for that reviewer only
- any cross-cutting lens checklist the diff triggers (removed-behavior, wrapper/proxy, altitude)
- instruction to report only actionable findings
- output format with severity, failure class, evidence, scenario, fix direction, and blocking status
- delegation boundary forbidding nested AI/codeagent/subagent calls
