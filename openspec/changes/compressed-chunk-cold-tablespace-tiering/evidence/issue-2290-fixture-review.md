# #2290 physical-parent admission fixture review

- Parent scope: #1895 / #1891; dependency #2224 merged via PR #2248.
- Reviewer: `ColdParentFixtureReview`, independent read-only leaf.
- Suggested fixture: expanded; repair intensity/effective tier: high.
- Reviewed artifacts: shared proposal/design/tasks/spec delta plus
  `fixtures/issue-2290.md`, before implementation.
- Verdict: **pass**; missing axes: none; required additions: none.
- Invariant Matrix: all eight surfaces have concrete owners and scenario evidence.
- Confirmed axes: narrow-vs-wide-with-keys discrimination; parent OID/Timescale ID
  binding from one catalog observation; immutable parent vs mutable compressed
  sibling; all caller/revalidation/reconciliation seams; digest/wire compatibility;
  actual ranges and forcing compatibility; legacy exclusion with or without a
  legacy relation; non-superuser isolated engine proof; direct owner/removal closure.
- Cardinality/default-six/G3 count belongs to dependent #2291. The intermediate
  preparation state does not authorize production G0; both children must merge.
- `openspec validate compressed-chunk-cold-tablespace-tiering --strict
  --no-interactive`: PASS after the fixture review.
- No source/runtime implementation, backend tests, production DB access or
  production G0-G8 execution is claimed by this fixture acceptance record.

Complete returned reviewer report is retained in
`.workplans/issue-2290/review/fixture-review.json`. Implementation and final-head
isolated verification remain required; shared task4.0B is not completed here.
