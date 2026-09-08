# #2137 Python/readiness child current state — 2026-09-08

## Boundary

Issue #2137 is the second child selected by the #1895 breadth retro. It delivers only the local, merge-before-access G0 Python/readiness chain: census, C1-C3/G8 owners, schemas/binders, shared private evidence/file primitives, canonical readonly validator split, executable runbook contract and selector/tests.

It is a prerequisite for #1895, not a replacement. It does not access node-27/node-22, run census/probe/install/live C1-C4, move chunks, restore timers, close #1895/#1891, or archive this shared change. Tasks 4.1-4.8 remain unchecked and belong to the post-merge #1895 maintenance window.

## Prerequisites now merged

- #1893 runner, #1894 installer, #1929 numeric runtime principal and #1970 river-click oracle are closed.
- #2123 C4 producer/validator/private publisher/binder merged through PR #2126; PR #2133 archived its change and promoted the authoritative `c4-live-display-evidence` spec.
- #2130 merged through PR #2134; PR #2135 archived the change and promoted the C4 input-classification precedence requirement.

## Current implementation state

- Issue #2137 is OPEN and implementation-ready. Its GitHub body carries the local-only PR boundary and complete Evidence Floor.
- The rebuilt branch contains census/runbook commit `0924dc7b`, readiness owner commit `a0b6ccf9`, selector closure `432762f2`, and post-master selector repair `1ba75811`; PR #2144 is open for this child.
- The old complete recovery anchor remains `preserve/issue-1895-pre-c4-replay` at `2f7e95b4`; it is not a delivery branch or review SHA.
- The branch was rebuilt from master `a285835c`; it first integrated
  `origin/master` at `49710a2d` through merge commit `2e0357ec`, then integrated
  the latest `origin/master` at `4c79cc39` through merge commit `9aab3720`. The
  only conflict was the append-like cross-PR gate-memory file. All common JSON
  records were structurally identical, and the resolution retained master's
  seven new closed-issue records plus the branch's open #2137 record.
- The eight selector routes exposed during replay use the existing C1/C2/C3 and
  storage partition tuples. Shipping exact-set checks matched 8/8 and removal
  mutants failed 8/8. The post-merge scheduler manifest importer gap was repaired
  by `1ba75811`; the pre-review shipping targeted run passed 6,527 assertions and
  the default full local unit row passed 18,058. After round-1 closure, the same
  rows passed 6,594 and 18,125 assertions respectively. After the round-2 depth
  closure, they passed 6,607 and 18,138 assertions.
- Post-master verification at
  `9aab37209ac104002a75ea03776bad9ab07ae2a5` kept the shipping selector at 72
  entries and ran 6,609 targeted assertions. Full-tree collection found 18,642
  tests, and the default full unit row passed 18,409 assertions. This current-
  state addendum supersedes stale status statements in the 2026-09-07 historical
  records without deleting their process disclosures.

## Remaining gate

The checklist ownership wording, focused fixture/alignment review, target OpenSpec
strict validation, latest-master integration, round-1 verification/fix closure
and post-fix local validation are complete. Round 2 triggered a registered
path-safety depth retro; its corrective action and local verification are now
complete. One budgeted comprehensive round, Gap Sweep and GitHub CI remain.
Tasks 4.1-4.8 remain untouched until #2137 merges;
no node-27 access is permitted before then.
