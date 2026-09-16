# Proposed retirement of the G7-only recorder contract

This capability's canonical purpose and requirements describe the #1895/G7
readiness recorder, not the shipping forecast query owner. Its actual callers
are the retiring `node27_issue1895_*` performance/lanes chain. A test import does
not make that recorder a surviving production capability. The following removes
only the recorder's contract with its source/test/selector cutover; the shipping
forecast owner, its named psycopg binding, API behavior and independent tests
remain unchanged. No canonical deletion is performed by this proposal itself.

## REMOVED Requirements

### Requirement: Native explicit-cycle bindings remain executable and identity-bound

**Reason**: The requirement explicitly governs the withdrawn #1895 G7 recorder,
including its synthetic positional-capture compatibility. No independent
production caller of that recorder is identified; preserving it solely for its
old test would retain dead cold-acceptance code.

**Migration**: After the R1.4 retained manual-workload transfer, delete the
`node27_issue1895_query` recorder/capture validator and its retired callers,
`tests/test_issue2227_explicit_cycle_named_binding.py` and the corresponding
`ISSUE2227_*` selector edges. Do not move the whole recorder to a new owner or
alter the actual shipping forecast query implementation. Surviving PGDATA
workload instructions retain needed capture, canonical identity validation,
native EXPLAIN binding and deterministic query identity under their own owner
with behavioral proof before deletion. Shipping SQL alone does not replace
that evidence producer; the synthetic positional/four-lane G7 protocol retires.

### Requirement: Query digests are deterministic across supported containers

**Reason**: This digest contract is for the retiring G7 recorder's captured-query
and frozen-lane evidence. It is not a mandate to preserve an otherwise unused
capture/digest helper after that acceptance chain is removed.

**Migration**: Transfer any deterministic query-identity behavior consumed by
the retained R1.4 workload, with semantic-change and mapping-order proof, before
removing the old helper/export, lane consumers, tests and selector entries.
Keep unrelated query/identity hashing and shipping forecast parameters untouched;
no compatibility export from `node27_issue1895_query` remains.

### Requirement: Existing G7 evidence and safety controls remain intact

**Reason**: The four-lane G7 rollout, its private performance receipt and its
live cold-rollout acceptance are explicitly withdrawn, not executed. Historical
repair evidence and the completed #2227 delivery remain historical records.

**Migration**: Remove the old G7-only source, schema/test/selector dependencies
and current references in the same cutover. Independent SQL/API/browser
performance, readonly and deployment obligations keep their actual owners and
proof strength; neither a historical G7 result nor local retirement checks are
new production PASS evidence.
