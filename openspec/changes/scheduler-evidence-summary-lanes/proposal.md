# Proposal — scheduler-evidence-summary-lanes (#3170)

Refs #3170. The issue closes only with the E6 production receipt; if that pass still falls back, the deferred
tier 5 lands under the same issue.

## Why

Three node-22 passes on 2026-11-16/17 each submitted 132 models with
`execution_boundary=slurm_gateway_orchestration`:
- `scheduler_2026111604_a40cad94653b`
- `scheduler_2026111616_cb7ef2f492a9`
- `scheduler_2026111704_aef8ab65a422`

All three were rewritten to `status: resource_limit_blocked` with `limit.pre_limit_status: submitted`. The #1905
non-blocking summary tier summarizes only the three candidate lists. Every other top-level lane stays verbatim,
including `model_run_evidence` (132 rows that each carry `stage_statuses` and `stage_evidence`, plus `artifact_refs` and
`artifact_locations`). The summary also fits-checks only with indent-2 JSON, while the fallback fits-checks compact.

Consequences:
- The stall probe answers `pass_limit_blocked` for about two hours after each such pass.
- The probe's shape classifier does not count the 132 submits as progress.
- `list-operator-actions` reads the pass as non-evaluating.
- The readiness validator, applied to the parsed payload, reads it as review-blocked. The readiness file reader also has
  a separate 256 KiB cap, which this change does not touch (design.md, Residual risk).

The fallback keeps no per-lane size, so the artifact cannot say which lane overflowed. The #1905 design
(`openspec/changes/archive/2026-09-22-operator-action-decidability/design.md:12`) recorded this as a known residue.

Triage on node-22 (read-only `jq`) found no comparable artifact:
- every 132-submit pass fell back;
- `planned` passes are about 2.6 MB, of which `retention` alone is about 2.38 MB and is compacted by the admission
  projection;
- the `.pre_execution.json` sidecars are 522 bytes.

The lane ranking is therefore measured locally (task 1.1).

## What

1. **Compact retry inside the summary tier.** When the indent-2 serialization of the non-blocking summary does not fit,
   try `compact=True` before any further projection, matching every fallback tier. A pass that fits the summary tier
   with indent-2 today is byte-identical.
2. **Lane projection sub-tier — deferred.** Task 1.1 did not reproduce the overflow (design.md, "Triage result"):
   - the local 132-candidate reconstruction fits tier 3 on `origin/master`, and tier 4a is 4.41 MB;
   - real node-22 task rows carry candidate identity, so each model-run row keeps one task row per stage, as in the
     reconstruction;
   - tier 4b is about 0.66 of tier 4a, so it overflows only if production tier 4a exceeds about 7.5 MB.

   No measured data says which lanes a tier 5 should project. It is deferred until the E6 pass either stays
   `submitted` or falls back with `limit.lane_bytes` naming the lane. The reader analysis and keep-list are kept in
   design.md ("Deferred: tier 5").
3. **Lane byte attribution on fallback.** When the candidate-list summary does not fit either way and the fail-closed
   fallback applies, `limit` carries a bounded attribution: the top 10 top-level keys of the summary payload (or, for a
   pass with no candidate lists, of the payload handed to that tier), each with its indent-2 serialized byte count.
   - The terminal limit-compaction tier may drop it with the rest of `limit`.
   - `SchedulerEvidenceWriteError` stays fail-closed.
4. **Spec.** MODIFIED `runtime-evidence-and-operations` "Bounded evidence observability floor":
   - the candidate-heavy scenario is scoped to the candidate-list summary and covers the compact retry;
   - the fallback scenario adds the attribution, its placement, and its re-bounding rule.

## Non-goals

- `MAX_EVIDENCE_BYTES` does not change.
- The `SchedulerResourceLimitError` branch (`scheduler_runtime.py:1505-1539`) does not change.
- The stall probe's `pass_limit_blocked` semantics do not change; the fix is on the writer side.
- No node-22 journal or `.venv` changes.

## Must preserve

- A within-limit pass is byte-identical: no `limit`, no `evidence_compaction`.
- A pass that fits the admission projection, or the candidate-list summary with indent-2, is byte-identical to today.
- The fallback contract is unchanged:
  - `resource_limit_blocked`, `limit.reason`, `limit.pre_limit_status`;
  - the #1168 candidate summary rows, the #1797 `restart_reconcile` lanes and the #2402 breaker-released source cycles;
  - the #2570 failure projection and its shed order;
  - the terminal limit-compaction floor;
  - `SchedulerEvidenceWriteError`.
- Consumers keep their verdicts:
  - `readiness_scheduler_evidence` validates a compact-summary pass with the same findings as the full pass;
  - `operator_action_listing` reads it as evaluating, with output equivalent to the full pass
    (`tests/test_scheduler_evidence_decidability.py` equivalence table);
  - the stall probe and `production_contract` see `submitted`.

## Fixture level

expanded. Risk packs: evidence-contract (status visibility), reader-compat (readiness, operator-actions, stall probe,
production_contract), size-bound (hard bound and fail-closed floor).

## Evidence Floor

- [ ] E1 `uv run ruff check --no-cache .`; `git diff --check`; entropy hard gate `hard_gate_failing_count 0`.
- [ ] E2 Triage: a local reconstruction of a 132-candidate submitted pass, built from writer-produced rows plus a
  production-sized `retention` block, ranks the top-level lanes by indent-2 bytes after the admission projection and the
  candidate-list summary. The ranking table and the reconstruction script are recorded in design.md / the PR.
  Measured at the production bound `MAX_EVIDENCE_BYTES = 5_000_000` (design.md, "Triage result"):
  - on `origin/master` the reconstruction does not overflow: tier 3 4,987,248 B (written there), tier 4a 4,414,304 B,
    tier 4b 2,934,290 B;
  - on the new code it is again written at tier 3 (4,987,233 B);
  - the real node-22 task-row shape is recorded next to it, and the non-reproduction is reported as a finding; the
    input was not tuned to overflow.
- [ ] E3 Tests, red on the old code where they assert new behaviour:
  - a real 132-candidate submitted pass (the triage reconstruction) written with `max_evidence_bytes` between its
    tier 4b and tier 4a sizes is `submitted` in the artifact, in `SchedulerPassResult.status` and on the CLI; it is
    written compact, `evidence_compaction.mode == "non_blocking_summary"`, there is no `limit`, and `submitted_count`
    equals the number of model-run rows;
  - `readiness_scheduler_evidence._scheduler_evidence_errors(compact_summary) == _scheduler_evidence_errors(full)`,
    following `tests/test_scheduler_evidence_decidability.py:463-474`;
  - `list-operator-actions` reads the compact-summary pass as evaluating, with output equivalent to the full pass;
  - the stall probe's `classify_pass` returns `SHAPE_PROGRESS` with `submitted == 132` for the compact-summary artifact,
    and the pass does not enter the `pass_limit_blocked` window (`scripts/node22_scheduler_stall_health.py:1561`);
  - a summary that still overflows compact falls back to `resource_limit_blocked` with `limit.pre_limit_status` and the
    bounded `limit.lane_bytes`;
  - the fallback product with `limit.lane_bytes` removed is byte-identical to the `origin/master` fallback product;
  - adding `lane_bytes` never switches the serialization from indent-2 to compact;
  - a payload that reaches the terminal floor keeps `limit == {"reason": "evidence_size_limit_exceeded"}` in the artifact
    written through `write_evidence`;
  - the `lane_bytes` measure (each value serialized on its own, indent-2, UTF-8 bytes) is pinned, its source is the
    payload handed to tier 4 when there are no candidate lists, and a re-bounded fallback product carries its existing
    `lane_bytes` through;
  - the existing #1168 / #1797 / #2402 / #2570 fallback tests stay green, or the test is listed as a deviation where
    tier 4b now fits its payload;
  - within-limit, admission-only and indent-fit summary passes are byte-identical to `origin/master` output (golden
    comparison).
- [ ] E4 The issue's verification set passes locally and in a node-27 temporary worktree:
  `tests/test_scheduler_evidence_decidability.py`, the `tests/test_production_scheduler.py` evidence-limit subset,
  `tests/test_node22_scheduler_stall_health.py`, `tests/test_production_readiness_validation.py`,
  `tests/test_operator_action_listing.py`, plus the selector set for the diff. The new test file is selected when the
  writer, the scheduler runtime, the readiness reader, the stall probe or the operator-action listing changes.
- [ ] E5 `openspec validate scheduler-evidence-summary-lanes --strict --no-interactive`.
- [ ] E6 After merge and a node-22 pull at a pass boundary, the next 132-submit pass either:
  - has `status: submitted` and `evidence_compaction.mode: non_blocking_summary`, and the stall probe does not report
    `pass_limit_blocked` for it — #3170 closes with this receipt in the archive PR; or
  - falls back with `limit.lane_bytes` naming the overflowing lanes — tier 5 is then implemented under #3170 from
    those numbers and the deferred design.
