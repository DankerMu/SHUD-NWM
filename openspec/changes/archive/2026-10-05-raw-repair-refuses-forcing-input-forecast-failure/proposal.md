# Raw-manifest repair retries refuse a forcing-input forecast failure (#2727)

```text
Issue type: bugfix
Fixture level: compact
Upstream suggested level: absent
Blast radius: a candidate that should be blocked for a human keeps an automatic retry that cannot repair it
  (today), or a raw-manifest repair retry that could have recovered a candidate is refused (regression to avoid)
Selected risk packs: Error handling / retry policy; Legacy compatibility (others not selected, see tasks.md)
Evidence floor: strict-lane and default-lane regression tests red before / green after; existing raw-repair,
  #2439, #2670 and #2719 tests still pass; ruff
```

## Why

`_missing_raw_manifest_repair_evidence` and `_repaired_raw_manifest_downstream_retry_evidence`
(`services/orchestrator/scheduler_state_failure.py`) grant an automatic retry ahead of the permanent-failure guard
and never ask whether the failure is the runtime rejecting the forcing package. Reproduced on master (2026-10-05):
on the strict warm-start lane the escalator rewrites `retry_downstream_after_raw_repair` into a `forecast`
restart, which re-stages the rejected package; for `repair_missing_raw_manifest` execution stays full-chain but
the evidence is rewritten to claim a forecast restart. On the default lane both stay full-chain, but under the
same run id that retry adopts the already-succeeded convert/forcing stages and regenerates nothing. In every
case the retry cannot repair a rejected forcing package; it spends attempts and delays the hand-off to an operator.

Owner decision (2026-10-05): treat it like #2719 - refuse the retry and let the candidate be blocked. The
alternative of exempting the full-chain shape from the strict escalator was implemented and withdrawn (it turns
the forecast restart into a no-op retry and removes the forced forecast replay that post-forecast raw-repair
retries rely on); see the issue comment.

## What changes

- Both channels return `None` when `failure["permanent"]` and `_failed_stage(state) in NATIVE_SHUD_STAGE_ALIASES`
  and `_forcing_input_failure(state)` - the #2719 predicate, applied (as there) only to a permanent failure. All
  four acceptance codes are always permanent (none is in `TRANSIENT_ERROR_CODES`). The `permanent` conjunct keeps
  today's decision for the one non-permanent shape in which the two error-code readers disagree (no top-level
  code, a transient `last_error`, a forcing code only on the failed job row), which would otherwise fall to the
  generic retry and restart at `forecast`. The ladder then offers the candidate to
  the model-package refresh channel, which refuses the same case since #2719, and the permanent-failure guard
  blocks it.
- The `raw_input_reingestion` refusal sets (`_REMEDY_NON_CAUSAL_*`) are not edited; the new check is a separate
  statement placed after the structural gates, next to the existing remedy check.
- The strict escalator (`scheduler_candidates.py`) is not touched.

Consequence, accepted with the decision: when `repair_missing_raw_manifest` abstains nothing else repairs the
missing raw manifest (the downstream channel needs the manifest present; the source raw-manifest restart needs it
ready). The candidate is blocked with the raw manifest still missing; the way out is an operator manual retry.

## Must preserve

- Raw-repair retries of every other failure keep today's decision on both lanes, including a post-forecast
  failure (for example `state_save_qc`) and a non-forcing-input forecast failure (`SHUD_FAILED`).
- A forcing-input code at a failed stage outside `NATIVE_SHUD_STAGE_ALIASES` (for example `failed_stage="forcing"`)
  keeps today's raw-repair retry.
- `DIRECT_GRID_TSD_FORC_TOO_LARGE` is not a forcing-input code (matcher unchanged).
- Existing raw-repair tests in `tests/test_production_scheduler.py`; the #2439, #2670 and #2719 tests.
- Known and accepted (same as #2719): `_forcing_input_failure` reads a broad error-code scan, so a stale forcing
  code can refuse a retry; the top-level `error_code` wins when present.

## Non-goals

The strict escalator; `failure_classifier` / `TRANSIENT_ERROR_CODES`; the `_forcing_input_failure` matcher; the
`resume_downstream_after_durable_shud` channel (conclusion recorded on the issue: not a defect in this sense);
whether a "full-chain" retry under an unchanged run id should regenerate forcing (separate question, recorded on
the issue). `design.md` is omitted (compact fixture).
