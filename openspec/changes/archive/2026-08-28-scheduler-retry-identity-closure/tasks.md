Fixture level: expanded
Repair intensity: high
Upstream suggested level: absent (all three issues predate the pipeline sizing contract)
Issues: #1586, #1584, #1577 (one user-requested PR)

## Risk Packs Considered

- Public API / CLI / script entry: not selected — private helper behavior changes; public decision/evidence keys remain stable.
- Config / project setup: not selected — no setting or default changes.
- File IO / path safety / overwrite: not selected — no file operation changes.
- Schema / columns / units / field names: selected — existing floor-source and marker-detail fields become load-bearing; prove shape compatibility.
- Auth / permissions / secrets: not selected — candidate identity scope is data correctness, not access control.
- Concurrency / shared state / ordering: selected — retry, marker ordering, and blocker restore are persisted state-machine decisions.
- Resource limits / large input / discovery: not selected — row/event limits and selection remain unchanged.
- Legacy compatibility / examples: selected — legacy marker without `failed_stage`, old states without floor sources, and unchanged consumers need regression evidence.
- Error handling / rollback / partial outputs: selected — foreign/ambiguous evidence must fail closed without wrong block or duplicate mint.
- Release / packaging / dependency compatibility: not selected — no dependency/package change; Python 3.11 remains required.
- Documentation / migration notes: selected — update stale boundary descriptions and consumer matrix/runbook wording.
- NHMS Slurm production lifecycle / mock-vs-real parity: selected — retry decisions control forecast submission, with scheduler tests as oracle; no Slurm behavior change.
- Other NHMS domain packs: not selected — no geospatial, hydro-met, numerical, database-domain, provider-snapshot, run-manifest/QC, or published-artifact behavior changes.

## Required Evidence

- E1 (#1586): in-window non-authoritative suffixed forecast cohort row with attempt `N >= limit` -> strict-warm-start remains retry; raw state bytes/mappings remain unchanged.
- E2 (#1586): same contributor in-window vs out-of-window floor -> identical candidate-scoped attempt/decision; bare cycle wedge and candidate-owned attempt still block at limit.
- E3 (#1584 + #1179 E13e successor): real `candidate_state_from_rows` active source-cycle download failure geometry, without replacing top-level `run_id`, plus a non-authoritative carried floor -> blocker predicate true, restore/shared-aggregate branch preserves the blocker, and that same branch narrows the foreign floor.
- E4 (#1584): wrong source/cycle and row-absent controls -> blocker predicate false and no restore.
- E5 (#1577 + #1179 E11-v2): geometry B + newest adopted marker exact target + carried forecast floor `N`, no explicit marker attempt -> failed row remains truncated, all top-level stage keys remain empty, `_failed_stage` and `_candidate_failed_stage` remain `None`, while manual evidence alone yields `previous_attempt=N`, `new_attempt=N+1`; retry limit does not suppress manual mint.
- E6 (#1577): legacy marker without `failed_stage` still recovers from exact contributor id; a valid marker stage alias agrees by canonical identity; foreign, stale, no-source, alias-spelled/non-canonical floor keys, and the same identifier under disagreeing canonical stages do not infer or charge unrelated floors.
- E7 compatibility: existing #1179 E5/E13a-d/E13f/E15/E16, nameable/manual-pinned mint, pure-freshness selection, stage-less attempt, flat-channel, force-resubmit, failure-policy, and non-canonical-stage tests remain unchanged; E13e's narrowing assertion is retained under E3 on an unmodified real projection rather than its obsolete synthetic top-level-`run_id` swap.
- E8 red proof: new-behavior tests fail against pre-change runtime source in one batched run and pass after implementation; no `red-proof` stash remains.
- E9 local commands: `uv run pytest -q tests/test_production_scheduler.py -k "strict_warm_start or source_cycle_blocker or manual_retry or geometry_b or retry_attempt or floor or execution_cohort or cohort_authority"` (collect MUST include E1/E2); `uv run pytest -q tests/test_file_orchestration_journal.py -k "manual_repair or manual_retry"`; `uv run pytest -q tests/test_production_scheduler.py tests/test_orchestration_chain.py tests/test_file_orchestration_journal.py`; `uv run ruff check .`; `openspec validate scheduler-retry-identity-closure --strict --no-interactive`.
- E10 node-27 oracle: frozen PR head runs the focused scheduler test selection and the issue-specified scheduler suite on node-27; no node-22 receipt because sbatch/Slurm resource behavior is unchanged.

## 1. Candidate-Authoritative Attempt View (#1586)

- [x] 1.1 Add a copy-on-read candidate-authoritative attempt view at the strict-warm-start raw-state budget boundary, reusing existing row/floor authority predicates without running the full decision-state filter.
- [x] 1.2 Add E1/E2 regression tests for in-window/out-of-window cohort parity, bare-cycle and candidate-owned controls, and no raw-state mutation.

## 2. Source-Cycle Blocker Identity (#1584)

- [x] 2.1 Bind top-level blocker recognition to the concrete source-cycle download blocker row and remove dependence on candidate top-level `run_id`; fail closed without row proof.
- [x] 2.2 Retarget (do not delete) E13e's floor-narrowing assertion to E3's unmodified real projection, and add E4 foreign/absent-row controls covering restore and shared-cycle aggregate behavior.

## 3. Geometry-B Manual Mint (#1577)

- [x] 3.1 Recover an otherwise-unnameable canonical stage only inside manual-retry evidence composition, from the newest adopted marker's exact target identifiers intersecting authoritative floor-source contributors; do not widen `_failed_stage`, `_candidate_failed_stage`, or projection visibility, and preserve all fallback/precedence behavior when proof is absent or ambiguous.
- [x] 3.2 Flip only the geometry-B mint result in E5 while retaining its E11-v2 visibility assertions, and add E6 legacy/negative controls plus automatic-limit/manual-override evidence.

## 4. Compatibility and Documentation

- [x] 4.1 Audit every `_state_retry_attempt` consumer and the blocker/marker sibling surfaces against the Invariant Matrix; change no unrelated consumer.
- [x] 4.2 Update code docstrings and `docs/runbooks/failed-basin-retry.md` so #1586/#1584/#1577 are no longer documented as accepted active boundaries; retain #1579/#1572 exclusions.
- [x] 4.3 Run initial E7-E9, record red/green evidence and deviations, and mark each completed task.
- [x] 4.4 Run E10 on node-27 for the frozen PR head and capture the exact commit/command/result receipt (`evidence/node27-e10-531c22cd.md`).

## 5. Round-1 Verified Invariant Closure

- [x] 5.1 Add a dedicated manual-retry decision view that preserves only the newest exact authority-narrowed marker/floor lineage capsule across the shared-cycle strip; keep the ordinary E13b state stripped and geometry-B visibility unchanged.
- [x] 5.2 Narrow recovery identity sets to marker target ids versus contributor own ids; add predecessor/`details.job_id` spoof negatives and direct-match controls.
- [x] 5.3 Make both file-journal manual writers derive N+1 with `effective_retry_attempt`; add suffix-only and non-suffix writer-to-projection regressions.
- [x] 5.4 Add production decision-path E5, distinct explicit pin, newest-unmatched marker, and ordinary-state strip proofs; correct E9 selector so collect includes E1/E2.
- [x] 5.5 Close the Phase 6.2 capsule-owner negative coverage (foreign exact contributor and newest-unmatched-over-older-match), prove pre-narrow/continue mutants red, rerun the fix matrix, and record final evidence before round 2.
