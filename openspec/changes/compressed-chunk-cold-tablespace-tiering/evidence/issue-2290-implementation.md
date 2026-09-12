# #2290 physical-parent admission implementation evidence

## Boundary

Parent #1895 / #1891; dependency #2224 merged via PR #2248. This preparation
changes physical-parent admission, not the reviewed-count successor #2291 or any
production operation. The shared change stays active and production4.1-4.8 are
unexecuted. Final verification and independent review are required before merge.

## Tests-first observation

The tests-only commit `ecfc972721e349c391e72b2f3d4bee99ef41afb5` was fetched via
GitHub into a session-owned node27 checkout. Only the new assertion suite was
committed at that checkpoint; production catalog source was still pre-change.
The existing public inventory derivation seam executed successfully, then the
new behavior assertions failed rather than collection/import setup failing:

```text
uv run --no-sync pytest -q tests/test_issue2290_cold_parent_admission.py \
  -k 'wide_with_surrogate_keys_refuses or same_columns_changed_parent_changes_digest'
3 failed, 32 deselected in 0.19s
```

The wide-with-surrogate projection was incorrectly accepted. Changing only
`parent_oid` or only `hypertable_id` left the old columns-only digest unchanged
(`04ae13a9e475036e318bea08a49391124d0fde596ab0560cfd6802897642df06`). These three
observed failures establish the admission and physical-identity digest gap.

## Implemented contract

Parent IDs and descriptors come from one joined catalog observation; both IDs are
mandatory positive integers and consistent across rows. River narrow shape
excludes the legacy text projection. Expected inventories constrain candidate and
origin membership and pass through all runtime, census, post-target and
intersecting-group consumers. Identity joins the opaque inventory digest while
closed wire keys, origin checksum/data query, timeouts and existing movement
semantics remain unchanged. Forcing keeps its existing shape.

The real disposable fixture models wide->legacy rename, canonical one-day narrow
creation, compressed legacy exclusion, same-shape parent replacement/digest drift,
stale reload refusal, restoration and absence of legacy. The existing origin
result/plan, shipping-role, recompression and lifecycle oracle remains the
acceptance owner. Direct CI routes and removal cases accompany all changed
acceptance owners; no name-only/default-identity path is retained.

A performance-live binding caller was discovered in the required intersecting-
group observation chain and migrated with its fake. This is the fixture's existing
consumer surface, not a new display/API feature. No migration/grant/lifecycle or
count-contract change was included. Local Ruff passed after formatting; node27
final-head green/engine/regression evidence and cross-review results are published
in the PR evidence bundle when executed, not inferred from this red record.
