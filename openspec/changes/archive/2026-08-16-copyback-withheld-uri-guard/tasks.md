# Tasks: copyback-withheld-uri-guard

## 1. Implementation

- [x] 1.1 Guard the copyback leg: `_is_withheld_uri_placeholder(copyback_uri)`
      short-circuits before any `_artifact_uri_missing_status` call
      (`services/orchestrator/scheduler_state_failure.py:618-677`).
- [x] 1.2 Withheld + required → `_artifact_blocker_evidence` with reason
      `copyback_source_withheld`, error code `COPYBACK_SOURCE_WITHHELD`,
      `artifact_type="copyback_source"`, `artifact_uri=<placeholder>`,
      `artifact_exists=False`; withheld + not required → no copyback blocker.
- [x] 1.3 Comment records the cannot-determine ruling, that no clearing path
      exists on the enabled DB-free public-read plane (clearing mechanism
      deferred to follow-up issue #1464), aligned with the forcing leg's
      #1203 wording.

## 2. Tests (tests/test_production_scheduler.py)

- [x] 2.1 Withheld + required blocks with `copyback_source_withheld` /
      `COPYBACK_SOURCE_WITHHELD`, never `COPYBACK_SOURCE_MISSING`; probe
      not invoked (sentinel).
- [x] 2.2 Withheld + not required emits no copyback blocker.
- [x] 2.3 Placeholder coverage parametrized over every
      `EVIDENCE_REDACTION_PLACEHOLDERS` member.
- [x] 2.4 `_decision_is_stable_missing_forcing_blocker` returns False for the
      withheld-copyback decision.
- [x] 2.5 Existing copyback + forcing placeholder tests untouched and green
      (zero existing assertions modified).
- [x] 2.6 Alias-shadowing ruling pinned: placeholder in state-level key +
      real URI in a lower-priority container → withheld blocker, no probe.
- [x] 2.7 Redaction round-trip premise pinned: s3-shaped
      `copyback_source_uri` written through the journal reads back as
      `[object-uri]` from `candidate_state` (host file per design test plan
      item 8).

## 3. Spec delta

- [x] 3.1 MODIFIED `job-retry-mechanism` requirement (copyback
      withheld-reference passages + 3 scenarios) — byte-faithful to the live
      requirement except the intended additions.

## 4. Docs

- [x] 4.1 Runbook entry for `COPYBACK_SOURCE_WITHHELD` in
      `docs/runbooks/current-production-ops.md`: meaning (withheld reference,
      cannot-determine), forcing rebuild does NOT apply, `unsafe_reason` null
      because no probe ran (so the §8.5 forcing triage rows do not cover it),
      clearing mechanism tracked in follow-up issue #1464.

## Evidence Floor

- `uv run pytest -q tests/test_production_scheduler.py` green (acceptance
  file; includes all new tests).
- `git ls-files '*.py' | xargs uv run ruff check` green.
- `openspec validate copyback-withheld-uri-guard --strict --no-interactive`
  valid.
- Issue #1367 acceptance items 1-5 each mapped to a test or artifact in the
  PR body.
- Red proof: new tests red against the unguarded leg (pre-implementation or
  via mutation), committed evidence in the PR body's deviation/evidence
  section.
