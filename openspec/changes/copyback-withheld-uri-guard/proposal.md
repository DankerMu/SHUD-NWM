# Proposal: copyback-withheld-uri-guard

## Why

Issue #1367: the copyback leg of `_missing_upstream_forecast_artifact_evidence`
(`services/orchestrator/scheduler_state_failure.py:618-677`) probes whatever
URI it finds under the four copyback keys with no redaction-placeholder guard,
while its sibling forcing leg (:472) was taught by #1203 that
`EVIDENCE_REDACTION_PLACEHOLDERS` values are WITHHELD references, not probeable
URIs. A redacted copyback URI (`[object-uri]`) would always probe as missing and
emit `missing_copyback_source` / `COPYBACK_SOURCE_MISSING` — a blocker that the
stable missing-forcing repair predicate
(`scheduler_candidates.py:1460-1481`, `artifact_type == "forcing_package_uri"`)
rejects, so it has NO repair channel: a deadlock. Latent today (no production
writer of the copyback keys, journal allowlist drops them, DB-backed leg pinned
off), but it materializes as an uncleareable deadlock the moment a write side
appears. PR #1366 removing the forcing leg's early return made this leg
reachable under the placeholder geometry for the first time.

**Semantics ruling (issue's open decision (a)/(b))**: the issue recommends (a)
(cannot-determine family) and its own acceptance criterion 3 forbids (b) — the
fallback arm "把 placeholder 等同于引用缺席" still lands on
`missing_copyback_source` when copyback is required, violating "断言不产出
`COPYBACK_SOURCE_MISSING`（或产出的是新的可清除 reason）". (a) is therefore the
only acceptance-satisfying option; adopted here and recorded as a deviation
note (issue readiness was needs-triage on exactly this point).

## What Changes

- `services/orchestrator/scheduler_state_failure.py` copyback leg: a URI that
  is a redaction placeholder never reaches `_artifact_uri_missing_status`.
  Placeholder + copyback required → new blocker reason
  `copyback_source_withheld`, error code / stable classifier
  `COPYBACK_SOURCE_WITHHELD` (cannot-determine ≠ determined-missing, aligned
  with #1203's `FORCING_VERSION_ROW_ABSENT` ruling). Placeholder + not
  required → no blocker (mirrors the absent-reference arm).
- No change to `_decision_is_stable_missing_forcing_blocker` (the new reason
  must stay OUTSIDE the forcing repair channel — the forcing-rebuild remedy
  cannot clear a withheld copyback reference). A regression test pins the
  predicate's rejection.
- **Recorded deviation from the issue's recommended fix**: the issue asks for
  "一条可清除路径" for the new reason. Fixture review (F1) proved that no such
  path is reachable on the only enabled plane today: the DB-free public read
  (`FileOrchestrationJournal.candidate_state:792` →
  `_sanitize_public_field:8549` →
  `scheduler_file_providers._sanitize_file_provider_scalar:2249`) redacts
  every s3/published-shaped `*_uri` deterministically on every pass, the
  write side strips placeholders back to `None`
  (`_strip_redaction_placeholders:8502`), and the unredacted DB-backed read
  (`chain_repository_state.candidate_state:440`) is pinned off by
  `NHMS_SCHEDULER_DB_FREE_REQUIRED=true`. Manual retry is a per-arm escape
  hatch (round-1 CAND-D correction — the fixture originally misattributed
  the arms): the guard has four call sites
  (`scheduler_state_decision.py:237/:277/:298/:355`) and manual retry
  (`:269`) pre-empts the last three. The geometries this change pins and
  tests are failure-state candidates and ride `:277`/`:355` — for them a
  manual retry request IS the operator escape hatch (it bypasses the blocker
  and re-submits; the withheld reference itself is untouched, so the blocker
  recurs if the retry fails again). Only the completed-stage resume arm
  (`:237`, requires NO failure signal, hence disjoint from the tested
  geometries) is evaluated before the manual-retry return and stays blocked
  despite a marker — pinned by a test. A durable clearing mechanism (one
  that clears the withheld reference rather than bypassing it, and covers
  the `:237` arm) depends on a copyback
  write side that does not exist (out of scope per the issue's own boundary).
  What this change delivers is therefore: the probe never fires on
  placeholders, the blocker truthfully says cannot-determine instead of
  determined-missing, and the un-clearability is named and routed — the
  clearing mechanism itself is tracked in follow-up issue #1464, not a claim.
- Runbook entry for `COPYBACK_SOURCE_WITHHELD`
  (`docs/runbooks/current-production-ops.md`): what the blocker means, that
  the forcing rebuild does NOT apply, and that `unsafe_reason` is null
  because no probe ran — so the existing §8.5 triage rows (keyed on
  `unsafe_reason`) are not misread as covering it.
- Regression tests in `tests/test_production_scheduler.py` covering the
  placeholder × required geometry matrix, the alias-resolution ruling, the
  redaction round-trip premise, and forcing-leg non-regression.

## Capabilities

- `job-retry-mechanism`: MODIFIED requirement "Missing upstream artifacts
  SHALL demote failure-state retries to a stable repair-eligible blocker" —
  copyback withheld-reference ruling appended to the body (three appended
  passages: the withheld ruling, its non-clearing disposition, the
  alias-resolution ruling) + 3 new scenarios
  (withheld+required blocks with distinct reason; withheld+not-required does
  not block; withheld blocker stays outside the forcing repair channel).

## Impact

- `services/orchestrator/scheduler_state_failure.py` (copyback leg only).
- `tests/test_production_scheduler.py` (new tests; zero existing assertions
  modified), plus the journal round-trip pin's host file per the design test
  plan.
- `docs/runbooks/current-production-ops.md` (new `COPYBACK_SOURCE_WITHHELD`
  entry).
- Out of scope (issue boundary): copyback write side, journal allowlist,
  forcing three-tier fallback, probe path-shape rules (#1365),
  `scheduler_state_decision.py:470` (stage inference, does not probe).
- Non-goals: new operator repair/clearing channel for the withheld blocker in
  THIS change (YAGNI for a triple-blocked latent geometry whose clearing
  mechanism depends on a nonexistent write side; explicitly recorded as a
  deviation from the issue's recommendation and tracked in follow-up issue
  #1464 — see What Changes).
