# Design — scheduler-evidence-summary-lanes (#3170)

## Governing invariant

The scheduler evidence writer may shrink detail, but it rewrites a pass's status to `resource_limit_blocked` only when
no status-preserving projection fits `max_evidence_bytes`. A status-preserving projection keeps every field that a
status- or readiness-bearing reader consumes, and it never drops rows those readers count.

## Tier order (writer: `services/orchestrator/scheduler_evidence_payload.py::_serialized_evidence_within_limit`)

1. Full payload, indent-2. Unchanged.
2. Without `no_progress_circuit`, indent-2. Unchanged.
3. Admission projection (`_compact_admissible_pass_payload`), indent-2. Unchanged.
4. Candidate-list summary (`_non_blocking_summary_payload`):
   - **4a** indent-2: unchanged.
   - **4b** compact: new (What 1).
5. **New lane projection**: starts from the tier-4 payload and projects the lanes in "Lane projections" below.
   - **5a** indent-2, then **5b** compact.
   - `evidence_compaction` keeps `mode: non_blocking_summary`; `summarized_fields` = the candidate lists plus the
     projected lanes, in a stable order; the admission record stays nested under `admission`.
6. Fail-closed fallback (`_call_bounded_evidence_payload` → `_fit_bounded_evidence_payload`). Unchanged, except that
   `limit` gains `lane_bytes` (What 3).
   - `lane_bytes` is computed from the tier-5 payload when tier 5 ran, otherwise from the tier-4 payload.
   - Format: a list of at most 10 `{"key", "bytes"}` rows, by indent-2 bytes descending, then key ascending.
   - It is a bounded-field addition: `_fit_bounded_evidence_payload` may shed it before any existing floor field, and
     the terminal limit-compaction tier removes it with the rest of `limit`.

Tiers 1–4a are left untouched, so every artifact that fits today in those tiers is byte-identical. A golden test
compares the new writer's output with the `origin/master` writer's output for a within-limit pass, an admission-only
pass and an indent-fit summary pass. The old writer is pinned by bytes captured from `origin/master` into a fixture, not
by importing old code.

## Lane projections (tier 5)

The final set comes from the task 1.1 ranking. `model_run_evidence` is the expected leader.

- **`model_run_evidence`**: the readers are `services/production_closure/readiness_scheduler_evidence.py` and
  `services/orchestrator/operator_action_listing.py`. Keep every row, so `submitted_count` still equals the number of
  submitted rows (readiness :917-923, :1074-1076). Per row, keep:
  - identity: `candidate_id`, `source_id`, `cycle_time_utc` / `cycle_id`, `model_id`, `scenario_id`, `run_id`,
    `forcing_version_id`, and the `candidate_identity` block (readiness :671-678, :681-697);
  - nested `hydro_run.run_id`, and `forcing` / `forcing_version.forcing_version_id` (readiness :739-747);
  - status evidence `status` / `outcome` / `result` / `state` / `candidate_outcome` / `submitted`, plus one projection
    from which the readiness status-collection code (:1091-1121) derives the same status-value set as from the full
    row. `stage_statuses` and `stage_evidence` carry the same payload, so the projection keeps one of them;
  - on rows with `error_code`, every `_BOUNDED_MODEL_RUN_FAILURE_KEYS` field (#2570).

  `artifact_locations` duplicates `artifact_refs` and is dropped; `artifact_refs` stays only if a reader consumes it
  (verify). Any other row field is kept unless the ranking shows it is material and no reader consumes it. The
  implementer records the per-field keep/drop decision, with the reader evidence, in this file.
- **`restart_reconcile`**: if ranked, use the fallback's compact form, which already satisfies the #1797
  payload-driven lane property.
- **`slurm_cancellation_evidence`**, **`model_discovery`**, **`slurm_status_sync_proof`** and the proof blocks: only if
  ranked. List the readers by grepping consumers under `services/`, `scripts/` and `apps/`, and keep their fields. The
  `_compact_write_proof`-style fixed-key compaction is acceptable only for a block no status or readiness reader reads
  beyond those fixed keys.

## Readers that must keep their verdicts (sibling surfaces)

- `services/production_closure/readiness_scheduler_evidence.py`: model-run row validation and the submitted count.
- `services/orchestrator/operator_action_listing.py:86-91, 605-606`: a pass with no `limit` is evaluating. Use the
  equivalence table in `tests/test_scheduler_evidence_decidability.py`.
- `scripts/node22_scheduler_stall_health.py:1015-1020, 1561`: reads top-level `status`; unchanged reader.
- `services/orchestrator/production_contract.py:379`: reads top-level `status`.
- `services/orchestrator/retention_frontier.py`: bounded by `MAX_EVIDENCE_BYTES`; unchanged.
- The CLI and `SchedulerPassResult.status`: they reflect the written status, so a lane-projected pass returns
  `submitted`.

## Triage method (task 1.1)

Build a 132-candidate submitted pass locally from writer-produced rows (`candidate.to_dict()` and
`_candidate_model_run_review_evidence`, the same producers the scheduler uses), plus a production-shaped `retention`
block (about 2.38 MB of indent-2 on node-22 `planned` passes, compacted by tier 3). Then run tiers 3–4 and rank the
top-level keys by indent-2 bytes. Record the table and the script path. Production `planned` artifacts give these
reference values: `retention` 2,379,377 B; `model_discovery` 138,040 B; everything else under 4 KB. node-22 holds no
non-fallback 132-submit artifact.
