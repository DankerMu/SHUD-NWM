# Design — file-journal read-blocked consumers (#2385 #2387) and the file lane's public URI evidence (#2306)

## Fixture level

`expanded`. Two of the three issues share one durable sentinel and two of the four touched consumers sit on a write lane (`mark_permanently_failed`, retry minting); the third changes a durable event payload's public shape.

## Governing invariant

**A refused journal read is never reported as an answer about the work.**

`_blocked_query_job` exists so a refused read stays PRESENT and non-terminal: duplicate-submission and active-cycle guards read it as in-flight and keep refusing. Every consumer therefore owes the row one of exactly two treatments:

1. **Refuse, classified** — surface the journal's own `reason`/`field` through the lane's stable error family; or
2. **Degrade, announced** — return the lane's "no evidence" value and log `reason`/`field`.

What no consumer may do is translate the row into a claim about the job or run — "nothing to retry" (#2385), "this job is non-transient" (#2387), "this job has no identity" (#2387 evidence 2).

The discriminator for the row is **`file_journal.status == "blocked"`**, shape-keyed, never `job_id` and never the status literal. Rationale:

- The by-run and by-cycle lanes (`:2145`, `:2112`) keep the real `run_id`/`cycle_id` and the default `job_id`; the by-id lane (`:2000`) keeps the real `job_id`. No single identity field discriminates all five lanes; the marker does.
- `_retry_job_for_stage_result` duck-types `self.repository`: when it has no `get_pipeline_job` it reads a plain `repository.jobs` mapping. A plain dict without `file_journal` must read as not-blocked, which a shape check gives for free.
- `pipeline_job_provenance.py:698` keys on the status literal instead, and **must**: its input is the publication projection, whose closed field allowlist `_PUBLICATION_JOB_FIELDS` (`file_orchestration_journal.py:13588-13608`) does not carry `file_journal`, so the marker is not available there at all. Pre-existing, untouched, and listed here so a reviewer does not read the asymmetry as newly introduced.

## D1 — the discriminator (#2385 + #2387 shared)

One module-level helper in `file_orchestration_journal.py`, defined next to `_blocked_query_job`, returning `True` only for a mapping whose `file_journal` is a Mapping with `status == "blocked"`, plus a sibling that extracts `(reason, field)` from that marker for the error translations. Both are imported by `chain_forecast_execution.py`.

Import direction is safe: `chain_forecast_execution` already imports `pipeline_job_provenance`, which imports `file_orchestration_journal` at runtime, and the journal never imports the chain execution module (verified: importing the journal alone does not load `services.orchestrator.chain_forecast_execution`). No new cycle, no facade forwarder: `chain_compat_static.py`'s inventory tracks `_retry_job_for_stage_result` as a symbol, and the symbol is not renamed or moved.

Why one helper rather than two local checks: the user's constraint for this batch is that the two consumer ends of the sentinel family must not overwrite each other. A single definition plus a single test module is the mechanical form of that constraint — a later change to the marker cannot satisfy one consumer and silently break the other.

## D2 — #2385: the manual-retry source selector

Order inside `_manual_retry_source_for_run`:

1. `jobs = query_pipeline_jobs_by_run(run_id)`;
2. if any row is blocked → `raise RetryEvidenceInvalidError(run_id, reason=<marker reason>, field=<marker field>)`;
3. `durable_run = _hydro_run_for(run_id)` wrapped in `except FileOrchestrationJournalError` → the same `RetryEvidenceInvalidError` translation;
4. everything else unchanged.

`RetryEvidenceInvalidError` is chosen over `RetryConflictError` because the run is not known to be busy: the lane knows only that it could not read. The error type already exists (409 `RETRY_EVIDENCE_INVALID`, safe details `run_id`/`journal_reason`/`journal_field`), already has a spec requirement, and the same function family already translates journal faults this way at `:11640` and `:11651`. Zero new error types, zero route-contract change.

The `job_id != "file_journal_read_blocked"` filter is **deleted**, not kept as belt-and-braces: after step 2 no blocked row can reach `safe_jobs`, and a dead filter re-establishes exactly the ambiguity this change removes (`job_id` looking like a discriminator). Deleting it also means the by-id-shaped row — real `job_id`, blocked marker — can no longer slip past, which the old filter would have let through.

Acceptance item 4 of #2385 ("`_blocked_query_job`'s declared dependency still holds") is satisfied by pinning the *new* dependency: the selector refuses on the row, and the three fail-closed guards named in that docstring (`_pipeline_job_conflicts_unlocked`, `_job_is_active`, `_file_auto_retry_job_can_be_reused`) keep their current verdicts on the unchanged row. The docstring sentence naming the `job_id` filter is rewritten to name the marker.

**Distinguishability is the point**: genuinely-empty → 404 `RETRY_NOT_FOUND`; active → 409 `RETRY_CONFLICT`; unreadable → 409 `RETRY_EVIDENCE_INVALID`.

## D3 — #2387: the automatic chain retry classifier

The blocked check goes on `record`, **after** the `store.get_job` short-circuit (the DB lane returns real rows there and must not be touched) and before the `PipelineJob` constructor. It raises `OrchestratorError("FILE_JOURNAL_READ_BLOCKED", <message>, {...reason, field, job_id...})`, mirroring the journal's own precedent at `:1949`/`:1970`.

Raising, not returning `None`: `_schedule_cycle_stage_retry` treats `None` as "no retry decision" and continues silently, which is strictly worse than today. The consequence of raising is **unchanged from today's behaviour**: a typed error already escapes from `handle_failed_job` (`chain_forecast_orchestrator_cycle.py:290`, reached from the unguarded `:260` call into this classifier) and is converted to a pass-level `production_orchestration_failed` — the repo prices that consequence in the comment at `:304-315`. What changes is the classification: the operator sees `FILE_JOURNAL_READ_BLOCKED` + `file_journal_record_limit_exceeded` instead of `file_journal_unsafe_identity field=run_id`, which today points at a dirty row that does not exist. Making that escape *handled* is a separate design question about chain pass-failure semantics and is a non-goal here.

Safety must move from accident to guard: the counterfactual test builds the row through `_blocked_query_job(error, run_id=..., cycle_id=...)` (the by-run/by-cycle shape) and asserts no `permanently_failed` write, no `_retry_1` row, and an unchanged durable row — i.e. the guarantee no longer depends on `_file_retry_job_record`'s identity sanitizer happening to raise.

Measured correction to the issue's counterfactual (implementation pass 1): on that row shape the identity sanitizer is indeed silent, but the write still does not land — the by-run/by-cycle lanes keep the **default** `job_id`, so `update_pipeline_job_status` refuses with `PIPELINE_JOB_NOT_FOUND` before persisting. So today's protection is a *second* accident rather than the first one the issue named. The conclusion is unchanged and the test pins it: with the guard removed the refusal comes from an identity/lookup accident that a future row shape can dissolve; with the guard in place it comes from the marker.

The two provenance readers (`_file_retry_event_runtime_root_candidates`, `_file_retry_previous_job_id`) degrade to the sibling's shape at `:12173`: empty `_RuntimeRootCandidateBatch` / `None` plus a `LOGGER.warning` carrying reason and field.

**The issue's premise for this half is stale; the corrected one is worse and must drive the tests.** Both readers are reached only from `_file_retry_runtime_root_candidates` (`:12005`/`:12009`/`:12048`) inside `_manual_retry_submission_request` (`:11886`), which `attempt_manual_retry` calls inside `try/except Exception` (`:11829-11838`) — *after* `_create_pending_manual_retry_job` has already minted and persisted a pending retry row. So a refused predecessor read never reaches the route as an unclassified 500. It is caught, `_retry_submission_error_code` (`retry.py:1274-1280`) finds no `.code` on `FileOrchestrationJournalError` and falls back to `SBATCH_SUBMISSION_FAILED`, and `_record_manual_retry_submission_failure` (`:12356`) **writes that pending row to `submission_failed`** with a fabricated gateway code and a `file_journal_missing_identity`-derived message. The durable retry row and the 503 both blame a gateway that was never called, for a journal read the operator is never told about.

The delta this change must produce, and what the tests assert: before, a blocked predecessor read yields a persisted `submission_failed` row with code `SBATCH_SUBMISSION_FAILED` and an identity-fault message; after, the walk skips the unreadable candidate, logs `reason`/`field`, and either resolves roots from the remaining candidates / the environment (no failure recorded at all) or records the runtime-root lane's own code. **This half is a degrade, not a refusal: it is NOT a zero-write case** — when the roots stay unresolved the lane still records a `submission_failed` row, as it does for any unsubmittable retry.

**Recorded trade-off (no user decision needed):** degrading keeps "blocked" behind "unresolved" on the wire — the same asymmetry #1945 deliberately accepted for the sibling reader. The alternative, raising into the route, would require widening `apps/api/routes/pipeline.py`'s except table, i.e. widening the route contract for a case that is already non-disclosing. The warning log is the discriminator, as it is for the sibling. Chosen: degrade.

## D4 — #2306: one rendering, at the journal's own boundary

The layering `_journal_record_for_write` documents is: the caller-boundary strip runs on the **raw** payload, and the journal's event sanitizer renders afterwards. The manual-retry writers break it by rendering first, so the strip meets the journal's own deliberate `[object-uri]`/`[uri]` output and nulls it (local-path placeholders survive only because they are not in `_PERSISTED_REDACTION_PLACEHOLDERS`).

Fix: drop the writer-side `_public_evidence(...)` wrappers; `insert_pipeline_event` → `_append_validated_record_unlocked` → `_public_pipeline_event_payload` already applies `_public_evidence` to the whole `details` mapping, so the rendering happens exactly once, after the strip. Verified preconditions:

- `insert_pipeline_event` (`:5903`) routes through `_append_validated_record_unlocked` (`:5936`) — it is **not** one of the five inline payload loops that bypass the event sanitizer. A writer among those loops could not use this fix.
- `_runtime_root_resolution_from_error` / `_runtime_root_contract_from_error` (`retry.py:1302`/`:1314`) return `_redacted_mapping(...)`: secrets and URL credentials removed, roots otherwise raw. That is the input the strip is meant to see.

Rejected alternative (the issue's option B): exempting this caller's subtree from the strip. It keeps a pre-rendered payload and opens a hole in #1592 D2b's single anti-laundering layer, which any future round-tripping caller can reach. The chosen fix removes the violation instead of exempting it.

**Intentional durable-shape consequence:** `private_recovery_payload` (`:9999`) is captured post-strip, pre-render. Its candidate paths (`_RUNTIME_ROOT_EVENT_CANDIDATE_PATHS`, `:576-585`) include `runtime_root_contract` but **not** `runtime_root_resolution`, so `runtime_root_contract` is the only key whose recovery content this change moves. Today it captures the writer's already-rendered values — `[local-path]` for every `*_root`/`*_path` key by the renderer's key rule (`public_evidence.py:66-79`), and `None` only where the value was URI-shaped and the strip nulled it — i.e. a recovery record with nothing to recover. After the fix it captures the real roots, and it lives under `private/runtime-root-recovery/`, not in the public event. This is pinned by a test rather than left as a side effect.

Precision on *why* that is right (review correction): for **these two producers** the record's reader is unreachable — `_pipeline_event_private_runtime_root_candidates` is called only from `_file_retry_event_runtime_root_candidates` (`:12279`), which returns early on the retry job's own `manual_retry_marker` and `continue`s past manual-retry submission events before that read. So the justification is not "the walk will consume it": it is that the record's contract is to hold the real roots the public event deliberately placeholds, and these two producers now satisfy that contract like the ordinary and migration producers already do. Suppressing the write instead would encode today's reader set into the record's meaning.

**Sibling writer at `:12332` (successful manual-retry submission):** same method family, same two keys, same defect. It is fixed in the same pass so the D2b layering holds for the whole writer family rather than half of it, and so the two events of one manual retry cannot persist the same root in two shapes. Note what is **not** a reason: `_file_retry_event_runtime_root_candidates` skips manual-retry submission events outright (`:12241`, via `_event_details_is_manual_retry_submission`, `retry.py:1453`), so that reader is never fed by this writer. The audit of both call sites is stated in the PR body, per the issue's "其他调用方可能同病（未逐一审计）".

Old events are not rewritten: entries already persisted as `null` stay `null`, and `submission_runtime_root_resolution` keeps returning what is recorded. Its docstring is updated to attribute the rendering to `_public_pipeline_event_payload`.

## Sibling surfaces (must keep their current behaviour)

| Surface | Why it is listed |
|---|---|
| `submission_runtime_root_resolution` `:12173` | The correct precedent for the degrade shape. Untouched except for the docstring's rendering attribution. |
| `pipeline_job_provenance.py:698` | Keys on the status literal; different projection; deliberately untouched. |
| `operator_released_reservation_recovery.py:52` | Marker-keyed precedent; untouched. |
| `chain_array_accounting.py:461-486` | Terminal-status allowlist already fails closed on the row. **Not** an instance of this bug; must stay unchanged. |
| `_pipeline_job_conflicts_unlocked`, `_job_is_active` `:13732`, `_file_auto_retry_job_can_be_reused` | Depend on the row staying PRESENT and non-terminal. Regression-pinned. |
| `_next_file_manual_retry_job_id_for_run` `:13142` | Silently ignores the sentinel and would allocate `{prefix}active`; effect-wise fail-closed downstream. Must be proven unreachable once the selector refuses first. |
| `scripts/node22_manual_retry_failed_runs.py:52` `_preview` | **Third caller of the selector**, on node-22's operator entrypoint, called at `:100` *outside* `main()`'s `try` (`:102`). Today a blocked read reaches it as `(None, None)` → `reason: no_retryable_failed_job` — the CLI face of #2385's misclassification. D2 turns that into an uncaught `RetryError` and no receipt, so this caller is changed with the selector, not after it. |
| `handle_failed_job` `:11174`, `schedule_auto_retry` `:11320` (def `:11254`), `mark_permanently_failed` `:11389` | Also consume `get_pipeline_job` rows that can be blocked, without a marker check. They stay fail-closed indirectly (reuse predicate / contract gate) and are **out of scope**: this change guards the entry `_retry_job_for_stage_result`, which is the only path by which a blocked row reaches them from the chain. Deliberately not "every consumer". |
| `_pipeline_event_target` `:6011-6015` | Write-path `get_pipeline_job` → `_source_id_from_job`; raises before any byte is written. Fail-closed, out of scope, listed so Review focus #1 is not read as covering it. |
| DB lane (`retry.py` `RetryService`) | Keeps persisting real roots and rendering on read. #2306 changes the file lane only. |

## Seams under test

- `repository._iter_pipeline_job_records_scoped` — the seam inside `query_pipeline_jobs_by_run`'s `try` (#2385, by-run lane).
- `repository._pipeline_job_for_id_unlocked` — the seam inside `get_pipeline_job`'s `try` (#2387, by-id lane, both evidence halves).
- `_blocked_query_job(...)` called directly with by-run/by-cycle identity — the counterfactual row shape (#2387).
- `POST /api/v1/runs/{run_id}/retry` through `TestClient` — the route's status code and body (#2385 409; #2387 evidence 2 the recorded failure's code and message, not a status-code change; #2306 URI values).
- The durable journal bytes and the private recovery record on disk (#2306, and the zero-write assertions for #2385 and #2387 evidence 1 — evidence 2 degrades and still records `submission_failed`, so its assertion is on the recorded code/message).

## Non-goals

- Changing `_blocked_query_job`'s row shape, default `job_id`, status literal, or reason token (#1953 owns the producer).
- Changing `query_pipeline_jobs_by_run`'s or `get_pipeline_job`'s try/except contract, or any record/byte budget (#1953).
- Registering journal reasons such as `file_journal_record_limit_exceeded` in the retry classifier.
- Handling the chain-side escape at `chain_forecast_orchestrator_cycle.py:290` so it no longer fails the pass.
- Rewriting historical events that already persisted `null`, or changing the DB lane's persist-raw/render-on-read split.
- `pipeline_job_provenance.py`'s status-literal keying.

## Review focus

1. Is the discriminator shape-keyed and do the four named consumers (selector, chain classifier, two provenance readers) use the single definition, with no second drifting copy? Consumers listed as out of scope in the sibling table are not in this check.
2. Does the #2385 refusal precede every use of `safe_jobs`, and are 404 / 409-conflict / 409-evidence-invalid still three distinguishable answers?
3. Does #2387's counterfactual test actually prove the guard carries the safety, i.e. would it fail if the guard were removed and the identity sanitizer were the only thing left?
4. Does #2306 persist no raw root in the public event; is `[local-path]` byte-identical to today's rendering (one render vs. two); and is the private recovery record's new `runtime_root_contract` content intended and pinned?
5. Do the file-lane and DB-lane 503 bodies now satisfy the *same* oracle, with the "file lane persists `None`" allowance removed rather than weakened?
