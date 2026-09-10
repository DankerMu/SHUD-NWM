## Why

PR #2220 changed the shipping forecast-series query from positional psycopg placeholders to a named `%(issue_time)s` mapping while preserving the explicit-cycle equality. The node-27 #1895 G7 recorder still recognizes only `h.cycle_time = %s`, so current master deterministically fails all 23 performance-closure tests before any live sample can run.

## What Changes

- Preserve the shipping query's actual named parameter mapping from capture through live `EXPLAIN` execution.
- Validate explicit-cycle equality, placeholder shape, and run/model/segment/cycle bindings for both named and retained positional forms without accepting the `selected_cycles` branch; named bindings are resolved by the placeholder names referenced by each predicate, never by scanning mapping keys or values for membership.
- Add one internal pure validator seam for captured SQL plus parameters so malformed synthetic captures can be tested without changing the named-only shipping forecast owner.
- Canonicalize mapping and sequence parameter containers deterministically for the frozen query digest.
- Add fail-closed regression coverage for malformed, mixed, missing, stale, and wrongly bound parameter shapes.
- Keep the receipt schema, forecast-store SQL, performance thresholds, readonly/timeout controls, and node-27 rollout procedure unchanged.

## Capabilities

### New Capabilities

- `explicit-cycle-query-binding`: Defines the recorder-to-EXPLAIN parameter-binding and digest contract for the four #1895 product-curve lanes.

### Modified Capabilities

None.

## Impact

Affected runtime boundaries are `packages/common/node27_issue1895_query.py` and `packages/common/node27_issue1895_performance_live.py`, with their performance, live CLI, publication, selector, and runbook-contract tests. No database schema, public forecast API, production configuration, or evidence JSON shape changes.
