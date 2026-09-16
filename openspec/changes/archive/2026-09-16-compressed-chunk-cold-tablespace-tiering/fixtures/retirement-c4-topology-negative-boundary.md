# Post-merge C4 evidence topology-guard repair

Issue #1895 / epic #1891, following PR #2321 and #2322. This is a bounded
repair of the static governance reader, not a change to production topology or
the approved C4 evidence. R1.4 and later retirement work remain pending.

## Diagnosis and scope

Master CI 34787384045 at merge `7aff2031678ab49c651ffeadf793321c7bb13b9b`
failed the repository hard-gate test; 20227 other tests passed. The precise
local read-only report reproduced one false finding at
`evidence/retirement-c4-verification.json:35`. A minimized public
`build_report(..., mode="hard-gate")` experiment runs in under one second:
its coordinated negative limit is classified as an affirmative writer claim,
while the already-supported explicit negative is accepted and affirmative
controls remain findings. The writer lexeme predicate fires; the active-primary
predicate does not. The negative-boundary recognizer expects the database noun
after the node reference and misses the coordinated scope of the leading
negation. No real database or runtime access occurred.

Suggested fixture level: compact. Effective fixture: compact, because only an
existing private static text classifier and its regression tests change; no
public API, schema, filesystem publication, environment or deployment changes.
Selected risk packs: contract/negative classification, bounded parsing,
compatibility of existing positive and wrapped claims, test evidence.
Not selected: live DB, network, browser, service activation and storage movement.
Two review seats: correctness+test-evidence and integration.

## Minimal change and must-preserve behavior

- Owner: `scripts/governance/audit_repo_entropy.py` existing topology negative
  boundary helpers. Extend the negative side narrowly for leading no/without
  governing coordinated database mutation/write and node access noun phrases
  joined by or/nor. Do not suppress arbitrary clauses containing a negative
  word, weaken positive detection, or add an evidence-path allowlist.
- Preserve clause boundaries: a negative claim must never exempt a separate
  affirmative claim in the same document, including quoted JSON evidence.
  If quoted sentence terminators require normalization, bound it to presentation
  punctuation and preserve supported wrapped affirmative claims.
- Regression owner: `tests/test_entropy_audit_script.py`, beside existing
  topology tests. Exercise public report output, not regex/source text.
- Keep the committed C4 evidence unchanged; do not reword it to satisfy the
  scanner, rewrite baseline counts, or change hard-gate eligibility/exit codes.
- Existing selector ownership for the auditor must select the entropy suite;
  no generic selector expansion in this repair. The independently observed
  scanned-evidence dependency gap is a separate follow-up, not part of this fix.

## Invariant matrix and evidence floor

| Surface | Inputs or transition | Required observable result |
| --- | --- | --- |
| Negative coordination | Actual merged negative limit, ordinary equivalent no/without coordination, JSON quoting | No writer finding; hard-gate PASS in minimal fixture |
| Positive authority | Direct affirmative and an emphatic non-negating prefix | Finding remains gate-eligible; hard-gate FAIL |
| Neighbor boundary | Negative beside a real positive, same document/JSON entries | Real positive remains a finding; negative does not acquire its neighbor's claim |
| Existing grammar | Prior explicit negatives, wrapped positive claims and other topology categories | Existing behavioral regressions remain unchanged |
| Current repository | All currently tracked evidence and source | Exact master-red repository hard-gate test passes |

Before implementation: independent read-only fixture review plus strict active
OpenSpec validation. Regression proof is red on pre-fix source and green after
the repair, using actual public `build_report` results and positive controls.
The parent runs local read-only minimized/full audit experiments, Ruff and strict
OpenSpec. Backend pytest runs on an isolated node-27 checkout: the full
`tests/test_entropy_audit_script.py` suite plus targeted selector ownership proof.
Review follows the compact two-seat gate, exact-head CI and final Gap Sweep.
The post-merge full master run remains the closing gate; PR targeted success is
not claimed as a full regression run. No new standalone test scaffolds retained.

## Non-goals and rollback

No production changes, node-22 operations, cold rollout, evidence sanitization,
new general natural-language parser, unrelated governance-category changes,
R1.4/R2–R5 implementation or issue/epic closure. Rollback is a source revert of
this classifier repair, not a change to production state or to historical proof.
