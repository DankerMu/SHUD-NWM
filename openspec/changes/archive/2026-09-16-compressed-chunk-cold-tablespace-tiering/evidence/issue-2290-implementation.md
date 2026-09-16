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

## Phase2 regression checkpoint

At implementation checkpoint `725f838360f88442d0875d121d3dac552ab4eff7`, the
owned node27 environment observed 35 focused passes and one pinned engine pass
(one deselected). The selected regression then reported 9 failed, 4682 passed,
5 skipped; default full pytest did not run because the driver stopped there.
Raw output is retained in `.workplans/issue-2290/node27/targeted-red.log`.

Eight failures exposed duplicated exact-owner CI rules or stale target-set
expectations. The correction must consolidate admission coverage into existing
rules, not whitelist duplicate ownership, and preserve every previous target
through independent removal proofs. This is machine-discovered Phase2 repair,
not a comprehensive review round.

The remaining census case already produced NO-GO with zero eligible groups:
the updated fake follows production compressed-candidate filtering, while the
old test required an obsolete blocker phrase. Replace the wording-only oracle
with the observable uncompressed-only/positive-required-count boundary, without
relaxing production eligibility or asserting a different message. Compression
eligibility is unchanged, as required by #2290; message wording is not its
acceptance contract. Corrected-head results belong in the final PR evidence,
not a retrospective claim that this checkpoint passed.

## Round1 independent review

Four seats reviewed `e1d786fff123e8616d5aadb68e0bd95bc886881d`.
Independent verification retained two P1 repair inputs: new cold admission errors
escaping the performance/post-target CLI refusal adapters (CONFIRMED, contract),
and qualified enum display-name comparison rejecting a valid hydro-visible
session (PLAUSIBLE, compatibility). The ledger records round1 not-clean, gate none.
Mutation/false-GO safety was not the reported failure.

The correction boundary is stable readiness-adapter translation of known
`ColdRuntimeError` while retaining programming-error propagation, and same-row
catalog namespace/type-name predicates for enum admission. Existing
`ColumnDescriptor`, `format_type` rendering, digest material, wire payload and
origin parity remain unchanged. The compatibility verifier explicitly corrected
its conflicting proof clause: test physical identity and bound admission under
each uniform search path, not identical digests across different search paths.
The latter is a separate pre-existing evidence-presentation concern.

Raw reports, verdicts, clarification and invariant surface inventory are retained
under `.workplans/issue-2290/review/`. Static review verdicts are not executed red
proof; new CLI/engine assertions must be run against the pre-fix implementation
before final corrected-head validation is accepted.

Tests-only checkpoint `294cb7402f1f2d4db701ff4787afb6e130c18370` kept the
e1d786f production source and ran after its full baseline exited. New enum/CLI
cases returned 9 failed / 99 deselected (1.53s); the pinned engine returned
1 failed / 1 deselected (4.30s). The engine reached the hydro-visible derivation
inside `_assert_physical_parent_admission` and refused the valid narrow parent.
The CLI cases exposed raw domain errors, and incorrect/missing catalog enum
metadata was accepted by the old predicate. These are behavioral red results,
not import/collection failures. Raw logs: `node27/review-red-focused.log` and
`node27/review-red-isolated.log` under the issue workplan.

The pre-repair e1d786f full run separately passed 19690 tests, skipped 330 and
reported one optional ecCodes-loading warning (3359.63s). It is a baseline, not
proof of the later source repair. The pre-existing mixed-session
digest/payload/parity presentation issue is tracked separately as
[#2293](https://github.com/DankerMu/SHUD-NWM/issues/2293), directly under #1895/#1891;
this PR does not perform a partial canonicalization or claim live reproduction.

The pre-round2 invariant audit additionally strengthened programming-error proof:
the prior post-target test replaced the entire adapter and therefore bypassed its
new catch. It now reaches the real adapter with an inner programming error;
the equivalent performance CLI case reaches the real derivation boundary.
Both require propagation, owned-connection cleanup and no artifacts. This is
test-only preservation evidence, not another product change or claimed red case.
