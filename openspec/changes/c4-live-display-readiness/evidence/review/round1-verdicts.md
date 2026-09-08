# PR #2126 round 1 independent verdicts

Reviewed head SHA: `775dfe7c9fe6652826fd2bb9e47ae24084a7a76f`.

| ID | Class | Severity | Verdict | Disposition | Verifier |
| --- | --- | --- | --- | --- | --- |
| C4-R1-1 | test-evidence | P1 | CONFIRMED | FIX_NOW | a700f5d782b1d03cd |
| C4-R1-2 | test-evidence | P1 | CONFIRMED | FIX_NOW | a700f5d782b1d03cd |
| C4-R1-3 | contract | P2 | CONFIRMED | FIX_NOW (rides coverage fix) | a51698e34bcd6beef |
| C4-R1-4 | authorization | P2 | CONFIRMED | FIX_NOW (rides coverage fix; fetch-path evidence required) | aa2e6ab1c0bfeec68 |

## C4-R1-1

Checked task 3.4 requires input/bracket/POSIX-facts rejection but C4 binder tests do not execute those negatives. Core `c4-receipt-binder-core.mjs:172-180,354-355,361-362,378-382` implements gates; hooks at 384/406/423 permit identity-change proof. River binder exercises a different implementation. Minimum: each missing argument; malformed/reordered bracket; out-of-window receipt/mtime; wrong mode/nlink; one hook-driven identity change. Add public acceptC4Receipt seam tests and selective isolated guard-removal proof. No arbitrary combination explosion; unchecking/deleting tasks does not resolve required coverage. Executable CLI is not necessary for this specific core candidate.

## C4-R1-2

Checked task 3.3 runtime failure is not asserted through runC4DisplayLane. homeResponses always readonly; nearest request-failure and body-limit cases target different branches. Minimum two lane tests: contradictory/non-readonly complete 200, and completionError, both RUNTIME_CONFIG_INVALID. Fake page already supports these inputs. RBAC/store helper tests are not lane wiring proof.

## C4-R1-3

Missing pins deliberately FAIL CONFIG_INVALID in config.ts:76-79,88-91,122-129, owner, frozen source 0ed538ba, G7 2f7e95b4, config tests and README. Schema prohibits CONFIG_INVALID under BLOCKED. Only new spec/design generalized all missing input to BLOCKED; issue AC says missing inputs cannot PASS and is already correct. Correct spec/design to explicit per-key matrix; do not change code/classification/tests. Downgrade P1→P2 because both statuses reject success, no bypass.

## C4-R1-4

Raw contradictory readonly role/false flag lies in API boolean input domain; existing driftedDisplayRuntimeConfig test explicitly asserts normalizer coercion. New allowDisplayReadonly consumes coerced true, contradicting this PR's raw dual-field conflict-deny contract and C4 lane decision. Production RuntimeConfig.public_dict emits consistent pairs, so this is not a claimed current live exploit. Stop forcing readonly flag true while retaining readonly mutation/queue safe defaults. Add real fetchRuntimeConfig with mocked GET then viewer gate denial, not setState-only proof. Inspect sibling store/UI consumers for expected fail-closed behavior.

## Round outcome and fix boundary

Four candidates, all confirmed, no deferrals or refutations. Round 1 NOT CLEAN; two coverage P1s buy one fix pass, P2s ride it. No new comprehensive round or counter reset until fixes/verification complete. Post-fix review uses pinned core plus at most one rotating seat (maximum three), one full-scope reviewer.

All verifiers read-only and did not run tests/CI/live. CI check status was successful but exact Unit Tests assertions remain to be checked by orchestrator. Local green evidence does not erase these coverage gaps.
