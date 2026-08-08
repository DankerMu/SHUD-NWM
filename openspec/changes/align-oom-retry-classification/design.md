# Design: OUT_OF_MEMORY reclassified non-transient (direction A)

## D1. Ruling record (AC1)

Direction **A** — code aligns to the approved spec. Full grounds in
proposal.md (Ruling section): under Slurm's standard cgroup-enforced
`--mem` accounting, OOM semantics weaken B's contention argument
(caveat recorded there); the issue's own `git log -S` archaeology refutes the
"deliberate pin" hypothesis (spec text and code set co-appear in the
bulk-import commit `35ae1b96`, no earlier introduction point);
conservative-default consistency with `spec.md:166-171`; the per-code
budget compromise is new design, not alignment. The trade-off accepted
and disclosed: a genuinely incidental OOM loses self-healing and needs a
manual retry — the spec's stated position.

## D2. Surface disposition (AC3 — all five surfaces adjudicated: the issue's four sibling copies plus the P1-1-discovered downstream-resume guard)

| Surface | Disposition |
|---|---|
| `retry.py` `TRANSIENT_ERROR_CODES` (`:27`) | REMOVE `OUT_OF_MEMORY`; ADD it to `NON_TRANSIENT_ERROR_CODES` (`:37-46`) — membership there is what `is_transient_error` / `is_retryable_failure` read, and the existing set/test shape distinguishes "known non-transient" from unknown-default |
| `retry.py` `failure_classifier` (`:163-173`) | REMOVE from the `transient_slurm_runtime` membership; add a dedicated `resource_configuration` branch (`if code == "OUT_OF_MEMORY"`) — the label is descriptive journal vocabulary only (repo grep: no production consumer branches on `transient_slurm_runtime`; only tests assert OOM's label), and letting OOM fall to `unknown_failure` would misstate a code the spec explicitly classifies |
| `scheduler_state_types.py` `TRANSIENT_RETRY_REASON_CODES` (`:57-70`) | REMOVE `OUT_OF_MEMORY`. Measured real effects, exactly two (fixture-review P2-1 correction — retryability itself comes from `retry.py` sets via `classify_failure`/`is_retryable_failure`, NOT this set): (a) `_permanent_reason` (`scheduler_state_failure.py:198-207`, membership test at `:205` runs only after `retryable is False` already holds) now labels OOM `permanent_failure_guard` instead of `retry_limit_exhausted`; (b) the `:291` recompute gate loses its transient-arm path for OOM (explicit-membership arm remains, row 4) |
| `scheduler_state_failure.py` `_MISSING_FORECAST_OUTPUT_RECOMPUTE_CODES` (`:266-274`) | **KEEP `OUT_OF_MEMORY`** — this set is not a transience classification: it already holds explicitly non-transient members (`PARSE_TASK_FAILED`, `PUBLISH_TASK_FAILED`, `STATE_SAVE_QC_TASK_FAILED`) and gates the "durable forecast output is missing → recompute the forecast stage" remedy, i.e. output-absence remediation, not the failed job's auto-retry budget. Removing OOM here would newly block an unrelated recovery channel — scope creep beyond the spec clause being enforced. Post-change, an OOM-failed downstream job reaches this gate ONLY via explicit set membership (no longer via the `TRANSIENT_RETRY_REASON_CODES` arm at `:291`) — behavior preserved by intent, test-anchored (D4.4) |
| `scheduler_state_failure.py` `_downstream_failure_restartable` (`:603-611`) | **BLOCK OOM** (fixture-review P1-1: without this, the downstream-resume channel overwrites the failure to `retryable=True, permanent=False` BEFORE the `:244` permanent check, and an OOM'd downstream stage with durable SHUD output present still gets `automatic_retry_allowed: True` — measured pre/post identical). The guard's refusal sets gain OOM: reason-code set gains `OUT_OF_MEMORY`, classifier set gains `resource_configuration` (mirrors the existing `malformed_input`/`policy_blocked` style). Ruling ground: this channel re-runs the SAME failed stage under the SAME memory config — the deterministic re-failure the spec's MUST NOT targets. Distinct from row 4 by remedy: row 4 recomputes a MISSING upstream artifact (restart from forecast); this row would directly re-execute the OOM-failed stage. Broader pre-existing tension — the channel also resumes unknown-default non-transient codes (e.g. `PARSE_FAILED`, existing anchor `test_production_scheduler.py:10720-10755`) against the spec's unknown-defaults clause — is OUT of scope for a one-code alignment and routed at tasks 5.0 |

`scheduler_state_compat.py:27` re-exports the types module symbol —
follows automatically, no edit.

Indirect consumer, disclosed intended flip (fixture-review P2-4):
`services/production_closure/slurm_validation.py:1721` reports
`"transient": is_transient_error(error_code)` inside retry-cancel
evidence, where `error_code` can be `OUT_OF_MEMORY` via
`map_slurm_error_code` (`:1134-1135`); post-change the field truthfully
flips to `False` — that is the ruling working as intended, not a
regression (existing `test_production_slurm_validation.py` suite stays
green, verified by in-memory simulation in fixture review).

## D3. Behavior delta boundary

- `handle_failed_job` path (DB plane): OOM → `should_auto_retry` False →
  permanent failure immediately; no `schedule_auto_retry`, no backoff
  events. Manual retry (`MANUAL_RETRY_SOURCE_STATUSES`) untouched.
- db-free scheduler plane: `_failure_policy_payload` (`:84-109`) derives
  retryable/permanent from `classify_failure` → `retry.py` sets, so OOM
  yields `retryable=False`, `permanent=True`; `_permanent_reason` labels
  it `permanent_failure_guard` (P2-1 corrected mechanism); with the D2
  row-5 block, the downstream-resume overwrite no longer rescues OOM —
  verdicts flip to `manual_retry_required` in BOTH geometries (durable
  output present or not), except the row-4 recompute channel.
- Third retry plane (P3-3): `file_orchestration_journal.py`
  `FileRetryService.retry_policy_for_job`/`handle_failed_job`
  (`:6548-6580`) follow automatically via `classify_failure`; its test
  module rides the evidence floor (simulation-verified green — its
  `transient_slurm_runtime` anchor at `:3246` does not use OOM).
- Missing-forecast-output recompute channel: unchanged (D2 row 4).
- Production-closure evidence field flip disclosed in D2 (indirect
  consumer note).
- No journal/DB backfill (forward-acting classification; issue ruling).
- Known limit, pre-existing and repo-wide: the `auto_retry_skipped`
  event payload demanded by `spec.md:154,170` is implemented for NO
  non-transient code; not added here for one code (routed at tasks 5.0).

## D4. Test plan

Rewrites (the 5 pins, each asserting the SPEC behavior post-change):

1. `test_real_slurm_gateway.py:1029-1035`
   `test_slurm_error_codes_align_with_retry_sets` → asserts
   `OUT_OF_MEMORY in NON_TRANSIENT_ERROR_CODES` and
   `not in TRANSIENT_ERROR_CODES`.
2. `test_retry.py:88-107` OOM-auto-retries case → becomes
   "OOM does NOT auto-retry": `policy["auto_retry"] is False`, no retry
   event, classifier == `resource_configuration`, permanent immediately.
3. `test_retry.py:144-157` OOM-exhausts-limit case → obsolete premise
   (no budget to exhaust); replaced by an immediate-permanent-failure
   assertion (attempt 0, no backoff consumed).
4. `test_production_scheduler.py::`
   `test_candidate_state_transient_runtime_failure_retries_failed_scope_with_reuse_evidence`
   (currently `:16946` — anchor by NAME, line numbers in this 30k-line
   file drift; P2-3) drops `OUT_OF_MEMORY` from the transient-runtime
   parametrize (leaving `NODE_FAILURE`, `SLURM_RESERVATION_LOST`) and
   gains a sibling non-transient case asserting the
   manual-retry-required verdict.
5. `test_production_scheduler.py::`
   `test_candidate_state_permanent_or_exhausted_failure_blocks_auto_retry`
   (currently `:17143`) — the `("OUT_OF_MEMORY",
   "retry_limit_exhausted")` row's expected reason becomes
   `permanent_failure_guard` (the label `_permanent_reason` actually
   produces post-change, D2 row 3).

New anchors:

- **Parity anchor (AC4)**: a test that reads
  `openspec/specs/job-retry-mechanism/spec.md` (explicit `pathlib` path
  from repo root — the archived 2026-06-18 copy of this spec can never
  be picked up), finds the "Non-transient error codes block auto-retry"
  scenario header (unique in the main spec), and extracts the FIRST
  backtick token of each `  - ` bullet line between the header and its
  first THEN (P3-2: bullet-anchored parsing — a windowed
  all-backticks grab would swallow `pipeline_job` from the WHEN line).
  Asserts `OUT_OF_MEMORY` is in that list AND in
  `NON_TRANSIENT_ERROR_CODES` AND NOT in `TRANSIENT_ERROR_CODES` /
  `TRANSIENT_RETRY_REASON_CODES` — the drift cannot reopen on either
  side without this test going red. Lives in `test_retry.py`.
- **D2-row-4 anchor** (P2-2: the two halves have DIFFERENT red-proof
  expectations): half 1 — OOM + missing durable forecast output on a
  downstream stage still yields `retry_missing_forecast_output` —
  green pre- AND post-change (boundary preservation); half 2 — the same
  OOM WITHOUT the missing-output geometry is manual-retry-required —
  RED pre-change (today's code retries it: the existing
  `..._transient_runtime_failure_retries_failed_scope...` pin asserts
  `retry_failed`). Kills the "remove OOM from the recompute set too"
  over-reach mutant and pins the D2 boundary from both sides.
- **D2-row-5 anchor (P1-1)**: OOM downstream failure WITH durable SHUD
  output present → assert the POSITIVE verdict (round-2 measurement):
  decision `blocked` with reason `permanent_failure_guard`,
  `manual_retry_required: True`, `automatic_retry_allowed: False` —
  not merely "NOT retry_downstream" (a negative-only assertion would
  stay green if OOM ever fell to some third wrong decision) —
  RED pre-change (measured: today the channel yields
  `resume_downstream_after_durable_shud` with automatic retry allowed
  unless the guard blocks OOM). Constructible from the existing
  helpers/samples around `test_production_scheduler.py:10720-10755`
  (`_candidate_state_decision` + candidate fixtures).
- Red-proof protocol: rewritten tests 1-5 + parity anchor + D2-row-5
  anchor + D2-row-4 half 2 run against pre-change source and fail on
  the defect; D2-row-4 half 1 green pre- and post-change (stated in
  brief with output).

Evidence floor (issue Verification):
`uv run pytest -q tests/test_retry.py tests/test_real_slurm_gateway.py
tests/test_production_scheduler.py tests/test_orchestration_chain.py
tests/test_file_orchestration_journal.py tests/test_production_slurm_validation.py`
(orchestration_chain hosts the OOM state-mapping fixtures — currently
`:6598,:6634`, P2-3 — the issue marks unaffected; proving they stay
green IS the non-regression evidence. file_orchestration_journal is the
third retry plane, P3-3; production_slurm_validation covers the P2-4
indirect consumer. Known pre-existing local failure:
`test_db_free_slurm_storage_root_check_masks_symlink_loop_path` — macOS
symlink-loop env issue, red before any change, disclose in PR body);
`uv run ruff check .`;
`openspec validate align-oom-retry-classification --strict --no-interactive`;
final sweep table in the PR body (AC2's "无残余矛盾点"): every hit of
`grep -rn "OUT_OF_MEMORY"` AND of the indirect-consumer sweep
`grep -rn "is_transient_error\|is_retryable_failure\|classify_failure\|TRANSIENT_ERROR_CODES\|TRANSIENT_RETRY_REASON_CODES"`
(P2-4: literal-only grep cannot see consumers without the literal)
mapped to a D2 disposition row or the non-goals list.

## Non-goals

- #1160, other codes' classification audit, per-code retry budgets,
  the `auto_retry_skipped` event payload (tracked separately), Slurm
  raw-state → error_code mapping surfaces (spec-constrained elsewhere:
  `real_backend.py:137,150-151`, `slurm_validation.py:80`,
  `run_qhh_continuous.py:57`).
