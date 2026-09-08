# PR #2126 / issue #2123 — round 1 reports and deduplicated candidates

Reviewed implementation/evidence head: `775dfe7c9fe6652826fd2bb9e47ae24084a7a76f`. Base `e6b5e4ffd06c6e6b18824ec51082ff4965a1c42a`. Four seats actually ran under high round-1 cap. User reconfirmed existing 0.34.0 rules after comparing main checkout/worktree. Reports below are orchestrator summaries of returned reports, not fabricated full transcripts. No verification verdict yet; no clean claim.

## Seat: correctness — a953dc49b3de2be85

One candidate C4-R1-4: P2, new authorization exception consumes a normalized store config. `monitoring.ts:209-216` forces display_readonly=true for readonly role, so a raw contradictory false flag becomes true before RBACGate. Existing component test injects store state directly, bypassing fetchRuntimeConfig/validateRuntimeConfig. C4 browser lane checks raw response and rejects it, creating a policy discrepancy. Current API derives both fields consistently, which is important counterevidence. Suggested proof: mocked client.GET contradictory raw response → fetchRuntimeConfig → gate denies viewer. Candidate requires independent reachability/contract verdict; do not assume live exploit.

Other inspected paths reported without candidates: jobs/logs envelopes, DOM strings, seconds-granularity bracket, terminal single-publication, river wrapper, preflight mapping, 10s RBAC wait. Runtime HTML-error-then-recovery and future live drift noted but not promoted to defects.

## Seat: invariant-state — a8a829d7d2f051841

No actionable candidates. Traced lane/terminal/identity/state/receipt/binder and unchanged river-click consumer. Raw runtime flags normalized by store was noted as pre-existing; correctness seat independently argues new auth use makes it relevant (C4-R1-4, verifier decides). API origin must match actual bundle requests; wrong origin fails closed, not false PASS. Report accidentally called issue #2123 a PR; actual PR is #2126. Predicted Python CI success is not accepted as evidence.

## Seat: test-evidence+spec-compliance — a134dd1b3e62dd573

C4-R1-1 (P1 coverage): checked tasks 3.4 enumerates C4 binder input/bracket/POSIX-facts rejection but standalone c4ReceiptBinderCore.test.ts lacks missing-argument, malformed/reordered/outside bracket, mode/nlink, and hook-driven parent/receipt-change negatives. River binder tests do not execute this independent C4 core. CLI parsing also has no executable proof. Shipping implementation appears correct by inspection; defect is missing requirement-driven evidence. Proposed tests cover each actual gate and isolated guard-removal mutants; unchecking tasks is not an acceptable substitute for required coverage.

C4-R1-2 (P1 coverage): checked tasks 3.3 runtime failure cases not exercised through lane; RUNTIME_CONFIG_INVALID has no assertion, all lane runtime responses readonly. Add complete 200 contradictory/non-readonly and completion-error cases through shipping lane. Existing RBAC/store helper tests do not establish lane wiring.

C4-R1-3 (P2 spec mismatch): spec scenario says any required input missing → BLOCKED; missing basin/segment intentionally returns FAIL CONFIG_INVALID in config.ts, tests and README. This duplicates security/integration's candidate at P1; retain highest proposed severity pending verifier, not two findings.

Nonblocking notes: inert failure enum vocabulary, generic unexpected JSON message, base spec prealignment. No new candidate for those. Confirmed 97c7ec4e..775dfe7c docs-only, so supplied Phase2 implementation binding valid.

## Seat: security-perf+integration — adf2fc4ad88988540

C4-R1-3 (P1 proposed): spec `specs/c4-live-display-evidence/spec.md:7-9` and design regression row require missing any five inputs → BLOCKED; missing/blank basin/segment is FAIL CONFIG_INVALID per config.ts:88-91,122-129, owner, README, tests. Both reject success; conflict is classification contract, not unsafe acceptance. Counterevidence strongly indicates deliberate implementation classification; verifier must determine source contract and minimal correction without silent oracle weakening.

No other candidates. Publisher extraction preserves no-clobber/link-first/fsync/FD semantics; note: serialize-before-path-preflight changes error precedence only when both payload and path invalid, with no successful write and no known downstream diagnostic dependency. Not promoted to a defect. Reviewer called paired lens a double seat; it was one reviewer seat carrying two lenses.

## Shared limits

All four supplied reports state no tests/build/CI runs, read-only review. Supplied local evidence: 68 files / 853 tests PASS on 97c7ec4e, current head adds evidence only. Python selector assertion still pending CI; no node-27/live receipt. Coverage candidates are not claims of failing shipping logic. Round ledger will record outcome after independent candidate verification, without resetting this round or hiding deferred catches.
