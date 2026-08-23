## 1. Pure implementation extraction

- [x] 1.1 Add `services/orchestrator/scheduler_discovery_evidence.py` for the pure source-discovery evidence, redaction, horizon, cycle-hour filter, and bounded window-discovery implementations; add no dependency, configuration, persistence, or product semantics.
- [x] 1.2 Keep composite helpers in `scheduler_discovery.py`; retain every dependency-bearing historical name/signature as a thin wrapper that injects current owner symbols at call time, and direct-alias only leaves proven to read no external global.
- [x] 1.3 Preserve the existing adapter `TypeError` fallback, evidence schemas, redaction, horizon precedence/defaults, UTC normalization, ordering, limit boundary, and exact `SchedulerResourceLimitError` reason/details.
- [x] 1.4 Audit the complete production consumer inventory: `scheduler.py`, `scheduler_candidates.py`, `scheduler_candidate_runtime.py`, `scheduler_compat_runtime.py`, `scheduler_runtime.py`, `scheduler_backfill_predecessor.py`, `scheduler_core.py`, and `scheduler_models.py`; preserve direct owner imports, import-time error-class aliases, facade attributes, runtime calls, historical import seams, and facade alias identity without consumer migration unless a discriminating compatibility test requires a mechanical change.

## 2. Durable owner-dependency mutation matrix

- [x] 2.1 Add committed data-driven tests proving composite-to-composite lookup: replacing owner `_source_cycle_evidence` is observed by `_cycle_hour_not_allowed_evidence`.
- [x] 2.2 Add committed tests proving composite-to-leaf/recursion/constants lookup: `_source_cycle_evidence` and `_source_discovery_evidence_safe` observe owner secret helper, status helper, nested helper, recursive call, sensitive-key regex, time/evidence helpers as applicable.
- [x] 2.3 Add committed tests proving `_source_secret_text_safe` observes owner `redact_payload` and `SOURCE_DISCOVERY_SENSITIVE_TEXT_RE`, including distinguishing outputs that default behavior cannot satisfy.
- [x] 2.4 Add committed tests proving `_filter_allowed_cycle_hours` observes owner `_ensure_utc`.
- [x] 2.5 Add committed tests proving `_duplicate_cycle_evidence` and `_backfill_deferred_evidence` each observe owner `_ensure_utc`, `_format_utc`, and `cycle_id_for`.
- [x] 2.6 Add committed tests proving `source_horizon_metadata` observes owner `_ensure_utc` and `normalize_source_id`.
- [x] 2.7 Add committed tests proving `discover_source_window` observes owner `MAX_DISCOVERED_CYCLES` and a replacement `SchedulerResourceLimitError`, while retaining exact limit details and legacy adapter fallback.
- [x] 2.8 Add/retain facade inventory, object identity, historical import, runtime signature, default evidence/redaction/horizon/order, and error-semantic tests; each binding row must fail if its dependency-bearing wrapper is changed to direct re-export/static capture.

## 3. Requirement-driven bite proof

- [x] 3.1 Before production implementation, run the new mutation/parity batch against master and record RED failures caused by the missing extracted module/binding contract; leave no `red-proof` stash or temporary artifact.
- [x] 3.2 After implementation, deliberately substitute direct alias/static capture for each independent wrapper family in one batched temporary mutation and record that the corresponding committed row turns RED; restore production and test files byte-for-byte, then rerun GREEN.
- [x] 3.3 Prove default output tests are independent of binding tests: representative source evidence remains GREEN only with unchanged schema/redaction/horizon/order/error semantics, while sentinel rows uniquely prove owner lookup.

## 4. Structural and scope gates

- [x] 4.1 Confirm `scheduler_discovery.py <= 1000` lines and `scheduler_discovery_evidence.py <= 1000` lines using the working tree and staged index before commit.
- [x] 4.2 Confirm `.large-file-guard.json` has no diff and no exemption is added; run the normal commit hook rather than bypassing it.
- [x] 4.3 Audit the complete diff for the issue non-goals: no cohort init-state accessor, completion verdict identity, terminal-first, §8.7, quarantine, breaker, file-journal, accepted-submit, DB, Slurm, sbatch, SHUD, or display behavior.
- [x] 4.4 Compare old AST/runtime signatures, facade alias inventory/object identity, and consumer import seams against master; every removed/rewritten behavior must have an equivalent owner wrapper/pure implementation plus a discriminating test.

## 5. Evidence floor

- [x] 5.1 Run `uv run pytest -q tests/test_production_scheduler.py`; expected: all tests pass, including every data-driven owner mutation and default parity row.
- [x] 5.2 Run `uv run pytest -q tests/test_scheduler_backfill.py tests/test_scheduler_backfill_predecessor.py tests/test_scheduler_generation.py tests/test_file_orchestration_journal.py`; expected: adjacent scheduler/journal behavior remains unchanged.
- [x] 5.3 Run `uv run ruff check .`; expected: zero findings.
- [x] 5.4 Run `openspec validate scheduler-discovery-evidence-owner-compat --strict --no-interactive`; expected: valid change.
- [x] 5.5 Run `git diff --check origin/master...HEAD`; expected: no whitespace errors and only the declared extraction, tests, and fixture files.
- [x] 5.6 Record local-only oracle routing: this change touches no DB/display/Slurm runtime, so node-22 and node-27 live receipts are explicitly not merge gates.

## 6. Invariant and compatibility audit

- [x] 6.1 Complete the Invariant Matrix and boundary checklist across every moved-global family and all eight unchanged production consumer modules; record changed, clean, or out-of-scope for each surface.
- [x] 6.2 Verify selected risk-pack evidence: schema/fields, secret redaction, resource-limit/discovery boundary, legacy facade/import compatibility, typed error behavior, and external provider defaults each map to a passing test.
- [x] 6.3 Report every implementation deviation with what changed, why, and affected surfaces; state `No deviations` explicitly if none.
