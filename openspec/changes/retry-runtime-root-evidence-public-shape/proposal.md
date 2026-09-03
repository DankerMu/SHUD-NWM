# Manual-retry 503 `runtime_root_resolution` renders one public shape on both lanes

## Why

`POST /api/v1/runs/{run_id}/retry` answers a `submission_failed` attempt with a
structured 503 whose `error.details.runtime_root_resolution` is read through
`context.service.submission_runtime_root_resolution(job.job_id)`
(`apps/api/routes/pipeline.py:560`). The two lanes render that one field
differently, in opposite directions:

- **DB lane over-exposes (#1961).** `RetryService.submission_runtime_root_resolution`
  (`services/orchestrator/retry.py:736-749`) returns the persisted evidence
  through `_redacted_mapping` (`:1920`, `redact_payload`) only. `redact_payload`
  owns secrets and URL credentials, not filesystem paths, so
  `resolved.*.value` carries the absolute local root verbatim
  (`/srv/nhms/workspace`) and `rejected[].value` carries the credential-stripped
  URL. Reproduced with the repo's own fixture (`tests/test_retry.py:1746`): the
  503 body contains `/srv/nhms/workspace` and the test is green because it
  asserts only secrets.
- **File lane over-scrubs (#1965).** The file lane persists
  `_public_evidence(evidence)` at write (`file_orchestration_journal.py:11724`,
  `:12586`) and returns it unchanged at read (`:11517`). `_sanitize_public_field`
  (`:13077`) applies its `_path`/`_root` key rule (`:13083-13084`) to the
  **whole value**, so the nested mapping under `resolved.object_store_root` /
  `resolved.published_artifact_root` collapses to the bare string
  `"[local-path]"`, losing `present`, `source` and `same_as_workspace`, while
  `resolved.workspace_dir` (no key match) keeps `{present, source, value: "[local-path]"}`.
  Same mapping, two JSON types for sibling keys.

Both lanes build the evidence from one function
(`retry.py:1828` `_runtime_root_resolution_evidence`, `redact_payload`-ed at
construction), so the divergence is entirely in public rendering. #1945's
acceptance (aligned 503 shape across lanes, no absolute paths in the body) is
satisfiable only when both halves are fixed, and the `ManualRetryService`
Protocol landed by PR #1963 otherwise freezes the fork.

Exposure: latent, operator-only. `display_readonly` answers 409 before the 503
arm (`pipeline.py:188`, `_raise_display_manual_action_if_needed` at `:236`); the reachable configuration is a
`compute_control` API process with `DATABASE_URL`, behind
`require_retry_control_action`. Severity is contract asymmetry plus a
defence-in-depth gap (DB) and lost provenance (file), not an anonymous leak.

Line cites are against `origin/master` `f9a1345f`; symbol names are
authoritative.

## What changes

1. **One public renderer, cycle-free (D1).** The journal's public-scrub
   family (`_public_evidence` … `_sanitize_public_text_token`,
   `file_orchestration_journal.py:13059-13166`) moves verbatim into a new leaf
   module `services/orchestrator/public_evidence.py` whose imports are limited
   to `packages.common.redaction` and `scheduler_state_common` (neither pulls
   `retry.py`; probed). The journal re-imports the same names, so existing
   importers (`file_orchestration_migration.py:34`,
   `tests/test_production_scheduler.py:13501`) keep resolving. The two
   dependencies the leaf cannot take — retry's `_safe_error_message` (`:1276`)
   and providers' `_sanitize_file_provider_scalar` (`scheduler_file_providers.py:2258`)
   — are carried as local equivalents, pinned by a parity test.
2. **File lane: `_path`/`_root` key + Mapping value recurses (D2, #1965).**
   Inside the moved `_sanitize_public_field`, a Mapping value under a
   path-shaped key is rendered through `_sanitize_public_evidence` instead of
   being replaced; the inner `value` still becomes `[local-path]`, and
   `present` / `source` / `same_as_workspace` survive. Scalar values under
   those keys are byte-identical (`runtime_root_contract` stays flat).
3. **DB lane: read-side public rendering (D3, #1961).**
   `RetryService.submission_runtime_root_resolution` returns
   `_public_evidence(_redacted_mapping(evidence))`. Persisted event details are
   untouched; `_runtime_root_resolution_from_error` (`:1295`, feeds the durable
   surface) is untouched.
4. **Tests (D4).** One shape helper asserted on both lanes' route responses;
   DB route red proof from the issue's repro; persisted-surface invariants
   (`tests/test_retry.py:1209/:1242/:1245`,
   `tests/test_file_orchestration_journal.py:4469`) unchanged; file-lane
   write-side pin extended; legacy bare-string passthrough on the file-lane
   reader; idempotency and classifier parity in the leaf.

D2 must land in the same commit as D3: applying `_public_evidence` on the DB
side without the recursion would collapse the DB lane's `_root` mappings too.

## Non-goals

- The persisted DB evidence (real roots in event details; asserted at
  `tests/test_retry.py:1209/:1242/:1245`) and `_runtime_root_resolution_from_error`.
- `redact_payload` semantics and the secrets assertions at `tests/test_retry.py:1783-1800`.
- `runtime_root_contract` flattening (`[local-path]` scalar; `tests/test_file_orchestration_journal.py:4469`).
- Sequence values under `_path`/`_root` keys (the issue scopes to Mapping values; none exist in the repo).
- `scheduler_file_providers._sanitize_file_provider_scalar` (`:2258`) and
  `_sanitize_file_provider_evidence_scalar` (`:2249`) keep their own copy of
  the scalar classifier and the whole-value `_root` rule: the module is 2271
  lines, outside `.large-file-guard.json`, and no runtime-root evidence flows
  through it. Reported as a residual duplicate, pinned by a parity test, not
  consolidated here.
- Rewriting historical file-lane events (bare-string `object_store_root`);
  readers tolerate both shapes (documented in the reader's docstring).
- Wiring the file lane into the production API.

## Issues

Closes #1961, closes #1965. Both originate from PR #1963 (#1945) side
findings, pre-existing at master. Neither carried an upstream
`Suggested fixture level`; triaged here as `compact`.
