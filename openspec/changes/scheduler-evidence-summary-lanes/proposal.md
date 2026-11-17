# Proposal — scheduler-evidence-summary-lanes (#3170)

Closes #3170.

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
2. **Lane projection sub-tier.** When the summary (indent and compact) still does not fit, project the remaining lanes
   that grow with pass size. Keep the true status and add no `limit` block.
   - `model_run_evidence`: every row is kept, so the row count still equals `submitted_count`.
     - Each row keeps the fields the readiness reader consumes: identity keys, the `candidate_identity` block, nested
       `hydro_run.run_id`, and `forcing` / `forcing_version.forcing_version_id`.
     - It also keeps the status evidence: `status` / `outcome` / `result` / `state` / `candidate_outcome` /
       `submitted`, and a single projection of `stage_statuses` / `stage_evidence` / `task_results` /
       `task_results_summary.status_counts` that yields the same status values.
     - Rows that carry `error_code` keep the #2570 `_BOUNDED_MODEL_RUN_FAILURE_KEYS`.
     - Fields that duplicate another field in the same row are dropped.
   - Further lanes are projected only if the task 1.1 ranking shows they matter; the ranking is recorded in design.md.
     - `restart_reconcile` reuses the fallback's compact form, which keeps the #1797 lanes.
     - `slurm_cancellation_evidence` and `model_discovery` have no fallback projection: the fallback drops the former
       and empties the latter. A projection for either must keep every field a named reader consumes (design.md
       lists the readers).
     - No lane is dropped outright on this tier.
   - `evidence_compaction.mode` stays `non_blocking_summary`, and `evidence_compaction.summarized_fields` lists every
     lane actually projected.
   - The tier tries indent-2 first, then compact.
3. **Lane byte attribution on fallback.** When the lane projection still does not fit and the fail-closed fallback
   applies, `limit` carries a bounded attribution: the top-N top-level keys of the projected payload, each with its
   indent-2 serialized byte count.
   - The terminal limit-compaction tier may drop it with the rest of `limit`.
   - `SchedulerEvidenceWriteError` stays fail-closed.
4. **Spec.** MODIFIED `runtime-evidence-and-operations` "Bounded evidence observability floor":
   - the candidate-heavy scenario is scoped to the candidate-list summary;
   - new scenarios cover the compact retry, the lane projection, and the fallback attribution.

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
  - `readiness_scheduler_evidence` validates a lane-projected pass with no `submitted_count_model_run_evidence_mismatch`,
    `*_missing_*` or `*_identity_mismatch` finding;
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
  Expected output at the production bound `MAX_EVIDENCE_BYTES = 5_000_000`:
  - on `origin/master`, the reconstruction exceeds the bound at tier 4a (indent-2) and tier 4b (compact), and each
    size is recorded;
  - on the new code, tier 5 writes it with `status: submitted`, and its size is recorded.

  If the reconstruction does not overflow at the production bound, report that as a finding rather than tuning the
  input until it does.
- [ ] E3 Tests, red on the old code where they assert new behaviour:
  - a ≥132-row `model_run_evidence` pass with a reduced `max_evidence_bytes` that overflows the candidate summary but
    fits the lane projection is `submitted` in the artifact, in `SchedulerPassResult.status` and on the CLI;
    `evidence_compaction.mode == "non_blocking_summary"`, `summarized_fields` names the projected lanes, there is no
    `limit`, and `submitted_count` equals the number of model-run rows;
  - an indent-overflow / compact-fit summary is written compact with the true status;
  - `readiness_scheduler_evidence._scheduler_evidence_errors(projected) == _scheduler_evidence_errors(full)`, following
    `tests/test_scheduler_evidence_decidability.py:463-474`, for two passes:
    - a `submitted` pass;
    - a non-submitted pass whose rows carry `error_code`, partial and `execution_attempted`;
  - `list-operator-actions` reads the lane-projected pass as evaluating, with output equivalent to the full pass;
  - the stall probe's `classify_pass` returns `SHAPE_PROGRESS` with `submitted == 132` for the lane-projected artifact,
    and the pass does not enter the `pass_limit_blocked` window (`scripts/node22_scheduler_stall_health.py:1561`);
  - a lane projection that still overflows falls back to `resource_limit_blocked` with `limit.pre_limit_status` and the
    bounded lane attribution;
  - the fallback product with `limit.lane_bytes` removed is byte-identical to the `origin/master` fallback product;
  - adding `lane_bytes` never switches the serialization from indent-2 to compact;
  - a payload that reaches the terminal floor keeps `limit == {"reason": "evidence_size_limit_exceeded"}` in the artifact
    written through `write_evidence`;
  - the `lane_bytes` measure (each value serialized on its own, indent-2, UTF-8 bytes) is pinned;
  - the existing #1168 / #1797 / #2402 / #2570 fallback tests stay green, or the test is listed as a deviation where
    tier 4b/5 now fits its payload;
  - within-limit, admission-only and indent-fit summary passes are byte-identical to `origin/master` output (golden
    comparison).
- [ ] E4 The issue's verification set passes locally and in a node-27 temporary worktree:
  `tests/test_scheduler_evidence_decidability.py`, the `tests/test_production_scheduler.py` evidence-limit subset,
  `tests/test_node22_scheduler_stall_health.py`, `tests/test_production_readiness_validation.py`,
  `tests/test_operator_action_listing.py`, plus the selector set for the diff.
- [ ] E5 `openspec validate scheduler-evidence-summary-lanes --strict --no-interactive`.
- [ ] E6 After merge and a node-22 pull at a pass boundary: the next 132-submit pass has `status: submitted` and
  `evidence_compaction.mode: non_blocking_summary`, and the stall probe does not report `pass_limit_blocked` for it.
  The receipt goes in the archive PR.
