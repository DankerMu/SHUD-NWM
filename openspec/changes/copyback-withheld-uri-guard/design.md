# Design: copyback-withheld-uri-guard

## Change surface

Single function: `_missing_upstream_forecast_artifact_evidence` copyback leg,
`services/orchestrator/scheduler_state_failure.py:618-677`. Helper
`_is_withheld_uri_placeholder` (:977-985) already exists and is reused as-is.
`_artifact_blocker_evidence` (:723) is reused unchanged (it already carries
reason/error_code/artifact_type/artifact_uri and sets
`stable_classifier=error_code`).

Risk triage: compact fixture. Pure adjudication logic, db-free, no I/O change
(the change REMOVES a probe call on one geometry), no persistence, no consumer
of the new reason exists yet. Highest risk is semantic drift against the
sibling forcing leg and against the repair predicate — both pinned by tests.

## Key decisions

1. **Placeholder handling mirrors the absent-reference arm, not the
   recorded-URI arm**: a real recorded URI that probes missing blocks
   unconditionally (evidence of corruption); a withheld reference carries no
   existence evidence at all, so with no copyback requirement there is nothing
   to determine and the leg emits no blocker. With `copyback_required` true it
   fail-closes with the distinct withheld reason. This is exactly the forcing
   leg's #1203 ruling ("takes the recovery path exactly like an absent
   reference") transplanted to a leg that has no recovery tier — the
   requirement decides, not the probe.
2. **New reason `copyback_source_withheld` / code `COPYBACK_SOURCE_WITHHELD`**
   (issue decision arm (a)). Cannot-determine ≠ determined-missing:
   `missing_copyback_source` claims a probe witnessed absence; for a
   placeholder that claim would be false and unfixable (the placeholder never
   exists in the store, so the blocker could never clear — the deadlock in the
   issue). Evidence shape: `artifact_type="copyback_source"`,
   `artifact_uri=<the placeholder>` (already redacted, hence evidence-safe, and
   it tells the operator the reference was withheld rather than absent),
   `artifact_exists=False` (meaning "not witnessed", per the
   `FORCING_VERSION_ROW_ABSENT` precedent), `unsafe_reason=None` (no probe
   ran).
3. **The withheld blocker stays outside the forcing repair channel** —
   `_decision_is_stable_missing_forcing_blocker` already rejects it via
   `artifact_type == "forcing_package_uri"` and `_MISSING_FORCING_BLOCKER_REASONS`;
   we change neither. Rationale: that channel authorizes a single-cycle forcing
   rebuild, which cannot clear a withheld copyback reference. A test pins the
   predicate rejection so a future edit cannot silently pull the withheld
   reason into the forcing channel.
   **No clearing path exists on the enabled plane, and this change does not
   pretend otherwise** (fixture review F1): the DB-free public read redacts
   every s3/published-shaped `*_uri` deterministically on every pass
   (`file_orchestration_journal.candidate_state:792` →
   `_sanitize_public_field:8549` →
   `scheduler_file_providers._sanitize_file_provider_scalar:2249`), the write
   side strips placeholders to `None` (`_strip_redaction_placeholders:8502`),
   the unredacted DB-backed read is pinned off. Manual retry is a PER-ARM
   escape hatch (round-1 CAND-D correction): the failure-state geometries
   this change pins ride `:277`/`:355`, both after the manual-retry return
   (`:269`), so a production-shaped manual-retry marker flips them to
   `(retry, manual_retry_requested)` — consistent with the live row-absent
   scenario's "evaluated before the guard" wording, which is scoped to
   failure-state candidates. Only the completed-stage resume arm
   (`scheduler_state_decision.py:237`, reachable solely with NO failure
   signal) runs before the manual-retry return and stays blocked despite a
   marker; that arm is pinned by a test. Manual retry bypasses rather than
   clears (the withheld reference persists; the blocker recurs on a renewed
   failure), and the `:237` arm has no operator path at all. The
   deliverable is truthful naming (cannot-determine, not determined-missing)
   plus a distinct code a future clearing mechanism can key on; the clearing
   mechanism itself is deferred to follow-up issue #1464 because it depends
   on a copyback write side that does not exist.
4. **No new repair machinery** (non-goal): the geometry is latent behind three
   verified blocks (no production write side for the four copyback keys;
   `_pipeline_job_row` allowlist drops them; DB-backed leg pinned off by
   `NHMS_SCHEDULER_DB_FREE_REQUIRED=true`). Building an operator authorization
   channel for it now would be speculative. The distinct error code is the
   actionable surface.
5. **Ordering inside the leg**: the withheld check runs after
   `_first_artifact_uri` and before any probe — the probe is never taught
   about placeholders and the redaction boundary is never bypassed (same
   phrasing the spec already uses for the forcing leg).
6. **Alias resolution is NOT re-scanned past a placeholder** (fixture review
   F3): `_first_artifact_uri` (:1017-1023) returns the first non-empty value in
   container-major priority order, and a placeholder is non-empty, so it wins
   the resolution and shadows any real reference recorded under a
   lower-priority key/container. That is the intended ruling: a placeholder
   proves the state crossed the redaction boundary; a lower-priority alias
   that survived unredacted is an echo of unknown provenance, and probing it
   would bypass the withheld ruling on the authoritative reference. The guard
   therefore fires on the RESOLVED value only — no continue-scan. Pinned by a
   test (placeholder in the state-level key + real URI in a lower-priority
   container → withheld blocker, no probe).

## Must preserve

- Real (non-placeholder) copyback URI behavior byte-identical: probe, then
  `missing_copyback_source` / `COPYBACK_SOURCE_MISSING` on missing, including
  the `unsafe_reason="object_store_root_unconfigured"` fail-closed ruling
  (#1365 D3) and the deeper-directory fail-open caveat comment.
- Absent reference (`None`/`""`) + required → unchanged
  `missing_copyback_source` blocker with null artifact reference.
- Forcing leg untouched; all #1203/#1365 tests stay green.
- `scheduler_state_decision.py:470` (stage inference over the same keys)
  untouched.
- Zero existing test assertions modified.

## Seams under test

- `scheduler_module._candidate_state_decision(candidate, state)` — the same
  seam every existing copyback blocker test uses
  (tests/test_production_scheduler.py:9890-9920 pattern).
- `_decision_is_stable_missing_forcing_blocker` — direct predicate call with a
  withheld-copyback decision (mirrors existing predicate tests if present,
  else via the repair-policy path used by existing tests).

## Test plan (requirement-driven)

1. Placeholder (`[object-uri]`) + `copyback_required=True` (restart stage
   `copyback` and/or `_copyback_source_required` state) → blocked with
   `copyback_source_withheld` / `COPYBACK_SOURCE_WITHHELD`; never
   `COPYBACK_SOURCE_MISSING`; probe not invoked (assert via monkeypatched
   `_artifact_uri_missing_status` sentinel that raises/records).
2. Placeholder + not required → decision is NOT a copyback blocker (leg
   returns None; overall decision falls through to whatever the state
   otherwise yields).
3. Parametrize 1-2 over every member of `EVIDENCE_REDACTION_PLACEHOLDERS`
   (acceptance criterion 1 says "任一值").
4. Withheld decision fed to `_decision_is_stable_missing_forcing_blocker` →
   False.
5. Non-regression: real URI missing → `COPYBACK_SOURCE_MISSING` unchanged
   (existing tests already pin; add none unless a new geometry is needed).
6. Forcing placeholder behavior unchanged (existing #1203 tests are the pin;
   run them).
7. Alias-shadowing ruling (decision 6): placeholder in the state-level
   `copyback_source_uri` + real URI in a lower-priority container → withheld
   blocker, probe not invoked.
8. Redaction round-trip premise (fixture review verification gap): an
   s3-shaped `copyback_source_uri` written through the journal reads back as
   `[object-uri]` from `candidate_state` — pins the geometry the guard
   exists for (journal-level test; may live next to existing
   public-read-boundary tests if that file is the better host).

## Risks to watch

- The four-key alias list: the guard must apply to whichever key
  `_first_artifact_uri` resolved, not just `copyback_source_uri`.
- `copyback_required` has two arms (restart stage set membership OR
  `_copyback_source_required(state)`); tests should cover at least one of
  each or document why one suffices.
- Blocker evidence consumers: `blocked_missing_upstream_artifact` decisions
  flow into candidate rejection evidence; grep for consumers switching on
  `reason`/`error_code` to confirm none hard-codes the copyback pair
  exhaustively.
