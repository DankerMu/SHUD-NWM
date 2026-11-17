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
5. Lane projection: **deferred** (see "Deferred: tier 5 lane projection"). Not implemented in this change.
6. Fail-closed fallback (`_call_bounded_evidence_payload` → `_fit_bounded_evidence_payload`). Unchanged, except that
   `limit` gains `lane_bytes` (What 3).
   - **Source.** The most-projected status-preserving payload the writer tried: the tier-4 payload; otherwise, when `_non_blocking_summary_payload` returned `None` because there are
     no candidate lists, the payload handed to tier 4 (the admission projection when it ran, else the payload after
     tier 2).
   - **Measure.** Each top-level value is serialized on its own as
     `json.dumps(value, indent=2, sort_keys=True).encode("utf-8")`, and its length is recorded. This is not the value's
     nested contribution inside the document. A test pins this definition.
   - **Format.** A list of at most 10 `{"key": str, "bytes": int}` rows, sorted by bytes descending, then key
     ascending.
   - **Placement.** The writer adds it after `_fit_bounded_evidence_payload` has produced the fitted fallback product,
     and keeps it only if that product, with `lane_bytes` added, still fits in the same serialization (indent-2 or
     compact) the product already used. Otherwise it is omitted.
     - **Terminal-floor exclusion.** The terminal limit-compaction tier runs inside `_fit_bounded_evidence_payload`.
       When it has cut `limit` down to the reason-only form, the writer does not add `lane_bytes`. The writer adds it
       only while `limit` still carries `pre_limit_status`. This keeps the reason-only floor of "Bounded-evidence
       last-line invariants" intact, and a diagnostic never outlives the true status.
     - The `BoundedEvidencePayloadCallback` signature does not change, and neither does the output of the
       `scheduler_compat_runtime._bounded_evidence_payload` shim. The circuit stays "first thing shed at every layer"
       (`openspec/specs/production-scheduler-orchestration/spec.md:218-224`).
     - So the fallback product with `limit.lane_bytes` removed is byte-identical to the `origin/master` product. The
       exact `limit ==` assertions at `tests/test_production_scheduler.py:21139,23151` keep holding once they ignore
       `lane_bytes`, or stay unchanged when it is omitted.
     - When the terminal limit-compaction tier ran, `lane_bytes` is never added.
     - **Re-bounding.** The writer replaces the caller's mapping in place, so a later write can start from a fallback
       product. When the source's `limit` already carries a `lane_bytes` list, that list is carried through, not
       re-measured. A re-measure would name the product's own lanes, and the original overflow would be lost (the same
       rule as the #2570 failed total).

Tiers 1–4a are left untouched, so every artifact that fits today in those tiers is byte-identical.
- **Golden test.** It compares the new writer's output with bytes captured from the `origin/master` writer (stored as a
  fixture, not by importing old code) for three passes: a within-limit pass, an admission-only pass and an indent-fit
  summary pass.
  - Paths: it calls `_serialized_evidence_within_limit` directly with a fixed `artifact_path` (or uses a db-free config,
    which renders `[local-path]`). Outside db-free mode, `artifact_path_evidence` embeds the tmp path
    (`scheduler_evidence.py:496`).
- **Existing tests.** Tier 4b may now fit a payload that an existing test expects to fall back (for example
  the 2,600-byte incident payload in `tests/test_scheduler_evidence_decidability.py:358`). Each such test is listed as
  a deviation, and its bound is lowered or its intent restated. None is silently re-labelled as still green.
  - Result: no existing test changed outcome. The 2,600-byte incident payload still falls back after tier 4b.

## Deferred: tier 5 lane projection

Not implemented here. The triage did not reproduce the overflow (see "Triage result"), so nothing measured says which
lanes to project. If the E6 pass still falls back, its `limit.lane_bytes` names the lanes, and tier 5 is built from the
sections below under the same issue. Tier 5 would run for every pass status, not just `submitted`, starting from the
tier-4 payload, indent-2 then compact, keeping `evidence_compaction.mode: non_blocking_summary` with `summarized_fields`
listing the candidate lists plus every projected lane.

### Keys tier 5 keeps verbatim

Tier 5 never projects these keys, because status-, alerting- or decidability-bearing readers consume them:
- `status` and `counts`. Do not reuse the fallback's `_compact_counts`: the stall probe's `classify_pass`
  (`scripts/node22_scheduler_stall_health.py:944-1023`) reads `submitted_count` / `blocked_candidate_count` /
  `skipped_candidate_count`.
- `progress_guard`. Without it, the probe classifies the pass `SHAPE_NEUTRAL`.
- `started_at`, `finished_at` and `execution_boundary`.
- `source_cycles`, and the scope keys `backfill`, `operator_filters`, `filters`, `sources`, `cycle_window` and
  `runtime_config`. `operator_action_listing` uses them for decidability and scope completeness. The sibling spec
  `openspec/specs/production-scheduler-orchestration/spec.md:1008` requires a `non_blocking_summary` pass to be "read
  exactly as the same pass written in full".
- `readiness`, `review_contract`, `schema_version` and `pass_id`.
- `execution_mode`, `no_mutation_proof`, `freshness` / `stale` and `final_production_readiness_claimed`, which
  readiness reads.

### Lane projections (tier 5)

The final set comes from the task 1.1 ranking. `model_run_evidence` is the expected leader.

- **`model_run_evidence`**: the reader is `services/production_closure/readiness_scheduler_evidence.py`.
  `operator_action_listing` does not read this lane. Keep every row, so `submitted_count` still equals the number of
  submitted rows (readiness :917-923, :1074-1076). Per row, keep:
  - identity: `candidate_id`, `source_id`, `cycle_time_utc` / `cycle_id`, `model_id`, `scenario_id`, `run_id`,
    `forcing_version_id`, and the `candidate_identity` block (readiness :671-678, :681-697);
  - nested `hydro_run.run_id`, `run.run_id`, the id fallbacks `hydro_run.id` / `run.id` / `forcing.id` /
    `forcing_version.id`, and `forcing` / `forcing_version.forcing_version_id` (readiness :739-763);
  - `execution_attempted` (readiness :980-992);
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
- `scripts/node22_scheduler_stall_health.py`: `classify_pass` (:944-1023) reads `status`, the `counts` fields and
  `progress_guard`; the `pass_limit_blocked` window check is at :1561. The reader does not change.
- `services/orchestrator/production_contract.py:379`: reads top-level `status`.
- `services/orchestrator/retention_frontier.py`: bounded by `MAX_EVIDENCE_BYTES`; unchanged.
- The CLI and `SchedulerPassResult.status`: they reflect the written status, so a compact-summary pass returns
  `submitted`.

## Residual risk

The readiness file reader caps scheduler evidence at `MAX_SCHEDULER_EVIDENCE_BYTES = 256 KiB` (readiness :41,
:304-311). In production, a multi-megabyte compact-summary artifact is therefore still refused as
`PRODUCTION_READINESS_SCHEDULER_EVIDENCE_TOO_LARGE` when readiness reads the file. The claim that readiness reads such a
pass consistently holds for the validator applied to the parsed payload (`_scheduler_evidence_errors`), which is what E3
tests. Raising that cap is out of scope.

## Triage method (task 1.1)

Build a 132-candidate submitted pass locally from writer-produced rows (`candidate.to_dict()` and
`_candidate_model_run_review_evidence`, the same producers the scheduler uses), plus a production-shaped `retention`
block (about 2.38 MB of indent-2 on node-22 `planned` passes, compacted by tier 3). Then run tiers 3–4 and rank the
top-level keys by indent-2 bytes. Record the table and the script path. Production `planned` artifacts give these
reference values: `retention` 2,379,377 B; `model_discovery` 138,040 B; everything else under 4 KB. node-22 holds no
non-fallback 132-submit artifact.

## Triage result (task 1.1)

Script: `/private/tmp/claude-501/-Users-danker-Desktop-Hydro-SHUD-NWM/6be3a7b3-0de2-4be3-bbeb-e7bf6b830fc6/scratchpad/3170_triage.py`
(run with `uv run python <script> --json <out>` from the repo root; it is also attached to the PR).

Reconstruction:
- A real non-dry-run `ProductionScheduler.run_once()` pass: 66 registered models × 2 sources (`gfs`, `IFS`), one
  cycle each, gives 132 submitted candidates (`counts.candidate_count` 132, `source_cycle_count` 2, 66 per source, as in
  the incident passes). Every row comes from the scheduler's own producers (`candidate.to_dict()`,
  `_candidate_model_run_review_evidence`). The pass is written with an unbounded `MAX_EVIDENCE_BYTES`, so the in-memory
  payload is the full writer payload.
- The orchestrator is the only fake. It returns the four stages listed by the node-22 `timing` block (`convert`,
  `forcing`, `forecast`, `state_save_qc`), array task rows keyed like `chain_array_evidence.stage_task_result_evidence`
  (sacct `Elapsed/MaxRSS/AveRSS/AllocTRES`), and `candidate_outcomes` keyed like
  `chain_array_evidence.candidate_outcomes`. Model ids and package URIs use the node-22 lengths.
- A `retention` block shaped like the node-22 `planned` pass `scheduler_2026111700_e392bd8f16ce` (406 planned + 406
  deleted + 10,978 skipped entries, padded to the node-22 mean compact entry sizes) is injected afterwards. It is
  2,544,826 B as indent-2 on its own, against 2,379,377 B on node-22 (+7%). The first run omitted `skipped`. The
  corrected run moved tier 3 by 47 B, so the retention difference does not affect the result.
- The tiers of `origin/master` (`4b89961c2`) are replayed one by one at `MAX_EVIDENCE_BYTES = 5_000_000`.

Tier sizes on `origin/master` (UTF-8 bytes of the serialized document):

| Tier | Bytes | Fits 5,000,000? |
|---|---:|---|
| 1 full, indent-2 | 7,654,008 | no |
| 2 without `no_progress_circuit`, indent-2 | 7,653,873 | no |
| 3 admission projection, indent-2 | 4,987,248 | **yes** (12,752 B headroom) |
| 4a candidate-list summary, indent-2 | 4,414,304 | yes |
| 4b candidate-list summary, compact (not a tier on `origin/master`) | 2,934,290 | yes |

The `origin/master` writer therefore writes this reconstruction at tier 3 with `status: submitted`, with no `limit` and
no `non_blocking_summary` marker. Re-run on the new writer (tiers 4b and `lane_bytes` added, no tier 5), it is again
written at tier 3: 4,987,233 B. The ±15 B difference comes from the random pass id and the timestamps.

Top-level lanes of the tier-4a payload, each value serialized on its own with indent-2 (top 12 of 52 keys):

| Key | Bytes |
|---|---:|
| `model_run_evidence` | 4,049,206 |
| `candidates` (summary rows) | 75,242 |
| `model_discovery` | 41,427 |
| `runtime_config` | 3,174 |
| `resolved_runtime_roots` | 1,872 |
| `progress_guard` | 1,657 |
| `production_contract` | 1,250 |
| `source_cycles` | 1,148 |
| `evidence_pre_execution` | 856 |
| `execution_write_proof` | 826 |
| `retention` (compacted) | 770 |
| `lock` | 705 |

Every other key is under 700 B.

`model_run_evidence` per-field totals over all 132 rows, each value on its own with indent-2 (mean row 28,993 B):

| Field | Bytes | Field | Bytes |
|---|---:|---|---:|
| `stage_evidence` | 1,120,776 | `artifact_locations` | 57,552 |
| `stage_statuses` | 1,120,776 | `artifact_refs` | 57,552 |
| `resource_summary` | 505,380 | `residual_blockers` | 35,244 |
| `production_identity_contract` | 156,024 | `review_contract` | 26,928 |
| `candidate_outcome` | 73,484 | `display` | 17,292 |
| `quality_states` | 61,248 | `forcing` | 16,236 |
| `outputs` | 60,588 | every other field | < 13,000 each |

### Finding: the reconstruction does not overflow at the production bound

E2 expected tier 4a and tier 4b to exceed 5,000,000 B on `origin/master`. Neither does:
- Tier 3 already fits. Its 12,752 B headroom is not robust: lanes known to be undersized here exceed it.
  - `model_discovery` is 41,427 B here against 138,040 B compact on node-22, with `selected_model_count` 66 against 132.
  - `timing` is 622 B against 7,251 B.
  - `journal_read_attribution` is 364 B against 9,457 B.
- The robust statement is tier 4a: 4,414,304 B is about 586 KB under the bound. Adding those known deltas (roughly
  110–150 KB) still does not reach it.
- The node-22 incident passes did overflow tier 4a. The reconstruction is therefore at least about 586 KB short of
  production in a lane this triage cannot identify. `model_run_evidence` is 92% of the reconstructed tier-4a payload, and
  its bulk (`stage_statuses` + `stage_evidence` 2.24 MB, `resource_summary` 0.5 MB) comes from the hand-shaped
  orchestrator boundary. Known divergences:
  - one 66-basin cohort per source cycle, where node-22 ran a 1 + 65 split;
  - four stages taken from the node-22 `timing` block, where `M3_STAGES` defines six (`parse` and `publish` are not in
    that block);
  - invented sacct accounting values;
  - exactly one identity-matched task row per stage (`matching: candidate_identity`). A `bounded_sample` match keeps up to
    16 rows per stage;
  - `execution_boundary` relabelled to `slurm_gateway_orchestration` after a run without Slurm execution enabled;
  - the full `candidates` rows and `source_cycles` carry the fake providers' `state_evidence`. node-22 keeps no
    non-fallback 132-submit artifact to compare against.
- Tier 4b is 2,934,290 B, a compact/indent ratio of about 0.66. At that ratio, tier 4b overflows only if tier 4a exceeds
  about 7.5 MB. So the compact retry (task 1.2) alone may have kept the incident passes non-blocking.

### Production row shape (node-22, read-only `jq`)

node-22 keeps no non-fallback 132-submit artifact (the evidence directory holds only `planned` passes and the three
fallback products). The real model-run row inputs were read from the cycle journal instead
(`journal/gfs/2026111612.jsonl`, the cycle of `scheduler_2026111616_cb7ef2f492a9`):
- a forecast-array `status_change` event carries 65 `task_results` rows of about 985 B compact each;
- every row carries `candidate_id`, `run_id`, `model_id` and `forcing_version_id`, so the writer matches by
  `candidate_identity` and keeps one task row per stage per candidate — the same matching as the reconstruction, not
  the 16-row `bounded_sample`;
- the stage-level `accounting` and `resource_metrics` are about 22 B each;
- the cycle ran 2 convert, 2 forcing, 68 forecast and 3 state-save jobs (one 65-basin cohort plus single-basin jobs,
  and a `state_save_qc_retry_1`), so candidates of the retried cohort may carry a fifth (retry) stage that the
  reconstruction lacks. Whether the writer's `stage_statuses` include it was not checked.

Inference, not measured: if the cohort candidates do carry a fifth stage, it adds about 25% to `stage_statuses` and `stage_evidence` (2.24 MB here), roughly
the 0.59 MB the reconstruction is short. That puts production tier 4a a little over 5 MB and tier 4b near 3.4 MB,
well under the bound. Tier 4b would overflow only if production tier 4a were above about 7.5 MB, which needs model-run
rows about 70% larger than the measured shape gives.

### Decision

Per E2, the input was not enlarged after this result was seen. Tier 5 (task 1.3) is deferred (see "Deferred: tier 5
lane projection"); E6 decides whether it is needed. Tasks 1.2 and 1.4
are implemented:
- 1.2 is justified by the measured 4.41 MB → 2.93 MB drop.
- 1.4 is justified because neither this triage nor any node-22 artifact can name the overflowing lane. With no tier 5,
  the `lane_bytes` source is the tier-4 payload (see "Tier order", item 6).
