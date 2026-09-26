# Tasks — cohort-identity-manifest-contract (#2542, #2557, #2546, #2555, #2539)

## Risk packs

| Risk pack | Status | Scope | Evidence |
|---|---|---|---|
| Public API / CLI / script entry | selected | `run_manifest.schema.json` public contract | 5.x |
| Config / project setup | not selected | — | — |
| File IO / path safety / overwrite | not selected | journal writes go through the existing writers | — |
| Schema / columns / units / field names | selected | new master field `retry_attempt_floors`; run manifest schema properties | 1.x, 5.x |
| Auth / permissions / secrets | not selected | — | — |
| Concurrency / shared state / ordering | selected | attempt refresh under the journal lock; sibling cohort conflict; dispatch order | 2.x, 3.x, 4.x |
| Resource limits / large input | selected (light) | bounded floors list; convert member list | 1.2, 3.1 |
| Legacy compatibility / examples | selected | legacy masters without floors; in-flight manifests; example JSON | 1.4, 5.x |
| Error handling / rollback / partial outputs | selected | the release entrypoints | 2.x |
| Release / packaging | not selected | — | — |
| Documentation / migration notes | selected | spec text; runbook row for the #2555 decision change if one exists | 6.1 |

## 1. #2542 per-member charge

- [x] 1.1 Add the `retry_attempt_floors` constant and normaliser in `retry_identity.py`. Register it at all four points (closed constructor, `ACCEPTED_SUBMIT_MASTER_ORDINARY_UPSERT_FIELDS`, merge tuple, `normalize_accepted_submit_evidence`). Make it first-write frozen on reclaim/re-entry, and strip it at the manual-retry clone boundary.
- [x] 1.2 `_reserve_cycle_stage` writes the floors of the forecast cohort master from each active basin's `retry_attempt_floor`.
- [x] 1.3 Reconcile sets each member's per-model `retry_count`:
  - floors absent or empty: the shared charge `eff`;
  - otherwise `min(eff, own_base + (eff - (max(floors)+1)))`, where `own_base` is `own_floor+1` for a listed member and `0` for an unlisted one.
  - if `eff < max(floors)+1`, fall back to the shared charge `eff`.
- [x] 1.4 Tests:
  - Flip `test_mixed_floor_cohort_mints_past_every_member_and_charges_the_shared_attempt`. The master is still `_retry_2`. A gets `retry_count==1` and a retry decision next pass. B gets `retry_count==2` and is blocked next pass.
  - Limit 3, A floor 0, B floor 2: A's next attempt is 1, B's is 3.
  - A legacy master without the field keeps the shared charge.
  - The field round-trips through the constructor, and reclaim does not overwrite the first write.
  - A mixed-floor inline auto-retry (`_retry_2` fails, `_retry_3` minted in the same call) charges A 2 and B 3.
  - A mixed-floor version of the `failure_lane_*_stays_within_the_limit` test passes.
  - A single-model (one-member cohort) charge after an occupied-id skip is unchanged (pin).
  - A post-deploy cohort with no floored member (empty list) keeps the shared charge.
  - An unlisted member already charged 2 under an older prefix still reads 2 next pass (pin).
  - The manual-retry clone does not carry floors.
  - A master with floors whose effective attempt is ≤ max(floors) (reused id) keeps the shared charge (pin).
- [x] 1.5 Update the job-retry-mechanism requirement and scenario (spec delta).

## 2. #2557 hydro attempt refresh

- [x] 2.1 Write a real-path red test for a same-`run_id` rerun (a non-retriable existing row, then second staging at attempt 2). It covers the durable `submission_attempt` and the reject/permit/demote release of the active row at attempt 2. Record whether it reproduces.
- [x] 2.2 (N/A: the harmful active-row state did not reproduce on the covered production paths; see 2.3 and the PR triage note) If it reproduces, do a monotonic refresh of `submission_attempt` (and `run_manifest_uri`) in `create_hydro_run_from_basin`. It reads and appends inside `_write_hydro_run`'s lock, never from an unlocked read. Status and identity stay unchanged. Add tests: no lowering; status unchanged; another attempt's row is still not released.
- [x] 2.3 If it does not reproduce, add a pinning test or comment explaining why, and record it in the PR.

## 3. #2546 convert members

- [x] 3.1 Add `convert` to `_MEMBER_RECORDING_DOWNSTREAM_STAGES`.
- [x] 3.2 Tests, using the exact shapes (a multi-member default cohort with a model-less convert row; a one-member override unit whose convert row carries `model_id`, per `chain_runtime_utils.py:66-72`):
  - The default cohort has only its convert row active, and the override unit is not blocked (no `PIPELINE_ALREADY_ACTIVE`).
  - A true duplicate is still blocked.
  - A convert row with members is not an accepted-submit master or forcing identity.
  - A sibling's convert failure does not reach the override candidate's decision.

## 4. #2555 dispatch order

- [x] 4.1 (Spec: MODIFIED existing run-manifest-missing requirement.) On the None lane, a manifest-missing terminal skip consults `_journal_predecessor_identity_quarantine` first. A non-None result takes over and consults the forcing witness. Otherwise the existing manifest-missing retry is emitted.
- [x] 4.2 Tests, all through `build_candidates`, None lane:
  - Stale lineage with the manifest missing gives the quarantine retry decision, and `canonical_quarantine_rerun_model_ids` includes the model.
  - At the breaker threshold the result is `blocked_journal_predecessor_identity_quarantine`, with no forecast submission.
  - Matching token, no recorded id, or a different base key gives a byte-identical `retry_terminal_run_manifest_missing`.
  - The #2396 forcing-witness tests stay green.

## 5. #2539 manifest contract

- [x] 5.1 Schema: declare the 9 keys. Declare builder-nullable fields nullable (cold start `ic_file_uri`; packaged IC `state_id`/`ic_file_uri`). Make `runtime.executable` optional. Update the example if needed.
- [x] 5.2 The builder writes top-level `schema_version: "1.0"`. The validator keeps its behavior, with only schema-declared required paths and no runtime jsonschema check, and never rejects a manifest it accepted before. `build_forecast_run_manifest` is out of scope; its violations are recorded only.
- [x] 5.3 Tests:
  - The real db-free `input/manifest.json` for warm-start, cold-start and packaged-IC basins produces zero violations with `FormatChecker`.
  - An injected undeclared key fails the check.
  - `_PRE_EXISTING_UNDECLARED_MANIFEST_KEYS` is emptied or removed.
  - `tests/test_api_contract_pipeline_ops.py` stays green.
- [x] 5.4 Check the analysis and cycle-stage manifests against the schema and record findings only.

## 0. Tooling

- [x] 0.1 Add `services/orchestrator/chain_manifests.py` and `services/orchestrator/accepted_submit_identity.py` to `.large-file-guard.json` `exclude`, with a rationale (same precedent as PR #2584). Put the new #2542 tests in a new file (`tests/test_cohort_member_charge.py`) and keep other new tests out of non-excluded files that would exceed 1000 lines.

## 6. Docs

- [x] 6.1 Update any runbook that documents the shared cohort charge or the manifest-missing retry lane.

## Evidence Floor

- **Local:**
  - Behavior-changing tests are red on origin/master source and green after the change. Pin tests are labelled and green on both: 1.4 legacy, single-model and unlisted-member pins; 3.2 true duplicate and not-a-master; 4.2 byte-identical; 5.3 contract tests where origin already conforms; 2.3.
  - `uv run ruff check .` passes.
  - `openspec validate cohort-identity-manifest-contract --strict --no-interactive` passes.
  - `uv run pytest -q tests/test_retry_mint_floor.py tests/test_api_contract_pipeline_ops.py tests/test_production_scheduler.py tests/test_orchestration_chain.py tests/test_select_ci_tests.py` passes, plus every test file importing a changed module.
  - `check-jsonschema` passes on `schemas/examples/run_manifest.example.json`.
- **node-27** (`TMPDIR=/home/nwm/tmp`): the same suites, plus `-k "quarantine or terminal_run_manifest"`.
- **Pending until after deploy:** the #2546 node-22 check of HLJ `submission_failed` == 0 over split passes.
