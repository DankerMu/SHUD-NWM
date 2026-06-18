## 1. Slurm Forecast Runtime Contract

- [x] 1.1 Add a failing contract test that runs the forecast array manifest index through `nhms-shud-runtime execute --manifest-index --task-id` and proves missing `runs/{run_id}/input/manifest.json` fails today.
- [x] 1.2 Define the per-task manifest index fields and runtime manifest fields required for forecast execution.
- [x] 1.3 Implement per-basin forecast runtime manifest creation before forecast array submission, including idempotent `hydro_run` creation or update.
- [x] 1.4 Ensure forecast array task retry/re-execution does not create duplicate/conflicting hydro run records.
- [x] 1.5 Add regression tests that assert every active basin task has a complete runtime manifest before Slurm submission.
- [x] 1.6 Add orchestrator status tests proving missing, unreadable, or invalid runtime manifests mark the pipeline job failed and do not advance the cycle to forecast success or publish.

## 2. Retry Execution Contract

- [x] 2.1 Add a failing API/integration test proving manual retry currently creates a pending job without executable Slurm submission or consumer progress.
- [x] 2.2 Choose retry execution mode: direct Slurm submission from API/orchestrator or durable pending-job consumer.
- [x] 2.3 Implement retry execution mode with `slurm_job_id`, `submitted_at`, status, and pipeline event updates.
- [x] 2.4 Add stale pending retry handling so active guards cannot deadlock recovery.
- [x] 2.5 Add concurrency/idempotency tests proving duplicate retry submission is prevented.
- [x] 2.6 Add response contract tests for `queued`, `submitted`, and `running` retry execution statuses, including error behavior when no execution path is available.

## 3. Publish Delivery Contract

- [x] 3.1 Add a failing test proving `publish-tiles` no-op success can currently let a pipeline appear complete.
- [x] 3.2 Decide release behavior: real publish artifact, explicit skipped non-success state, or `failed_publish`.
- [x] 3.3 Implement selected publish behavior in `nhms-pipeline publish-tiles`, sbatch handling, and orchestrator status mapping.
- [x] 3.4 Replace `test_publish_tiles_command_exists` with tests asserting real publish side effects or explicit non-success behavior.
- [x] 3.5 Update tile/publication docs and OpenAPI to match implemented table names, endpoint format, content type, status mapping, and release behavior.

## 4. Flood Product Readiness and API Contract

- [x] 4.1 Add backend tests for flood alert and flood map endpoints with `hydro_run.status='published'`.
- [x] 4.2 Define a named flood-product-ready status set including at least `frequency_done` and `published`.
- [x] 4.3 Update flood alert/map gating to use the named ready status set and preserve not-ready errors for incomplete runs.
- [x] 4.4 Fix OpenAPI `SuccessEnvelope` so endpoint-specific `data` schemas can be arrays or objects without `allOf` type conflict.
- [x] 4.5 Fix OpenAPI `IssueTime` to document `latest` plus ISO datetime, regenerate frontend API types, and add a freshness/contract test.
- [x] 4.6 Add regression tests for `frequency_done`, `published`, non-ready without rows, non-ready with stray rows, and ready-status set guard behavior.

## 5. Data Integrity and Storage Isolation

- [x] 5.1 Add failing tests showing best-available selections for different model/basin/forcing dimensions overwrite each other when they share `valid_time + variable`.
- [x] 5.2 Add a forward migration and repository/API changes so best-available selections preserve forcing/model/basin lineage or implement a documented global aggregation rule.
- [x] 5.3 Add illegal `forecast_hours` tests for GFS, IFS, and ERA5: negative values, non-step values, hours beyond source-specific limits, and ERA5 24+.
- [x] 5.4 Implement shared or per-adapter forecast-hour validation before manifest persistence.
- [x] 5.5 Add object-store prefix isolation tests for matching S3 prefixes, mismatched S3 prefixes, and bare object keys.
- [x] 5.6 Reject mismatched S3 URIs when `OBJECT_STORE_PREFIX` is configured without breaking valid bare keys.

## 6. OpenSpec and Delivery Traceability

- [x] 6.1 Fix M4 OpenSpec requirement headings so `uv run openspec validate m4-ifs-multi-source --strict` passes.
- [x] 6.2 Audit README/ROADMAP referenced evidence and either track the referenced files or explicitly defer/exclude them.
- [x] 6.3 Run strict validation for M4, M5, M6, M7, and M8 changes.
- [x] 6.4 Run three-way Codex/codeagent review for this M8 change; fix all P0 findings before issue creation.
- [x] 6.5 Run backend verification with `uv run pytest -q` and targeted tests for Slurm manifest, retry, publish, flood, OpenAPI, data-integrity, and object-store contracts.
- [x] 6.6 Run frontend verification where API type regeneration or flood map behavior changes.
- [x] 6.7 Record `git status --short --untracked-files=all` evidence after tracking or explicitly deferring referenced files.
- [x] 6.8 Create one Epic and 4-6 delivery-oriented GitHub child issues linked to this change.
- [x] 6.9 Record issue links and final verification evidence below.

## Issue Traceability

- Epic: https://github.com/DankerMu/SHUD-NWM/issues/109
- Slurm forecast runtime: https://github.com/DankerMu/SHUD-NWM/issues/111 -> https://github.com/DankerMu/SHUD-NWM/pull/115
- Retry execution + publish delivery: https://github.com/DankerMu/SHUD-NWM/issues/112 -> https://github.com/DankerMu/SHUD-NWM/pull/116
- Flood/API contract: https://github.com/DankerMu/SHUD-NWM/issues/110 -> https://github.com/DankerMu/SHUD-NWM/pull/117
- Data integrity/storage: https://github.com/DankerMu/SHUD-NWM/issues/113 -> https://github.com/DankerMu/SHUD-NWM/pull/118
- OpenSpec and verification: https://github.com/DankerMu/SHUD-NWM/issues/114 -> https://github.com/DankerMu/SHUD-NWM/pull/119

## Verification Evidence

- `uv run openspec validate m4-ifs-multi-source --strict 2>&1` -> `Change 'm4-ifs-multi-source' is valid`
- `uv run openspec validate m5-flood-frequency-warning --strict 2>&1` -> `Change 'm5-flood-frequency-warning' is valid`
- `uv run openspec validate m6-system-hardening-alignment --strict 2>&1` -> `Change 'm6-system-hardening-alignment' is valid`
- `uv run openspec validate m7-second-review-remediation --strict 2>&1` -> `Change 'm7-second-review-remediation' is valid`
- `uv run openspec validate m8-fourth-review-remediation --strict 2>&1` -> `Change 'm8-fourth-review-remediation' is valid`
- README/ROADMAP audit: all referenced evidence/task files and image artifacts checked from `README.md` and `docs/ROADMAP.md` exist in the worktree; no missing placeholder or deferral was required.
- `git status --short --untracked-files=all`: OpenSpec M4-M8 changes, `docs/ROADMAP.md`, `docs/images/roadmap_*.png`, worker/service README files, `AGENTS.md`, and `apps/web/README.md` are present as untracked worktree artifacts; `uv.lock` is modified.
- `.venv/bin/python -m pytest tests/ -x -q 2>&1 | tail -5`:
  - `/Users/danker/Desktop/Hydro-SHUD/NWM/.venv/lib/python3.12/site-packages/sqlalchemy/engine/default.py:949: DeprecationWarning: The default datetime adapter is deprecated as of Python 3.12; see the sqlite3 documentation for suggested replacement recipes`
  - `cursor.executemany(statement, parameters)`
  - `-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html`
  - `513 passed, 352840 warnings in 236.73s (0:03:56)`
