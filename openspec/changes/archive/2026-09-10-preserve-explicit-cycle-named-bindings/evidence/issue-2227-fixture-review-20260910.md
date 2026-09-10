# Issue #2227 OpenSpec fixture review — 2026-09-10

- Change: `preserve-explicit-cycle-named-bindings`
- Fixture level: `expanded`
- Repair intensity / effective review tier: `high`
- Reviewer type: `reviewer` (one read-only reviewer, same reviewer across repair iterations)
- Final verdict: `pass`

## Review trajectory

1. `revise`: named identity was not explicitly predicate-placeholder-name to canonical value; repeated pyformat names were conflated with positional arity; synthetic invalid and positional captures had no reachable test seam.
2. `revise`: the requirement's positional success scenario still incorrectly assigned synthetic `%s` production to the named-only shipping owner.
3. `pass`: no missing axes and no required additions after defining one pure captured-query validator seam, unique referenced-key equality for named mappings, predicate-key value validation, repeated-name semantics, and positional-only count/ordinal rules.

## Fixture boundary

The shipping `forecast_store` owner remains named-only and unchanged. Synthetic positional and invalid forms target the internal pure validator; native mappings/sequences then flow through `execute_explain`/`make_sql_probe`. The receipt schema, readonly/timeout gates, performance thresholds, and node-27 live-evidence requirement remain unchanged.
