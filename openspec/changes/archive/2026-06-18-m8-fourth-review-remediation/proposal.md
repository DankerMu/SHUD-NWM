## Why

The fourth deep review found release-blocking gaps that survived the M7 remediation pass. The most serious failures are production-path mismatches: the Slurm forecast array can reach `nhms-shud-runtime` without per-run manifests, manual retry creates queued jobs that are never submitted or consumed, and the publish stage reports success while doing no work. These can make the system appear healthy while forecasts, retries, or tiles are not actually produced.

The review also found API and traceability gaps: flood-alert endpoints reject already published runs, OpenAPI schemas conflict with implemented response shapes, `issue_time=latest` is implemented but undocumented, M4 OpenSpec deltas do not validate under strict parsing, and several delivery artifacts remain untracked.

This change turns those fourth-review findings into an explicit remediation stage with executable acceptance criteria and GitHub issue traceability.

## What Changes

- Fix the production Slurm forecast-array contract so every forecast task has a valid runtime manifest and matching `hydro_run` state before execution.
- Make manual retry executable by submitting retry work or implementing a durable pending-job consumer with deadlock protection.
- Replace publish-stage no-op success with a real publish implementation or a fail-fast documented non-success state.
- Allow flood-alert and flood-return-period map APIs to read all product-ready run states, including published runs.
- Fix data integrity gaps in best-available dimensional keys, forecast-hour manifest validation, and object-store prefix isolation.
- Converge OpenAPI with implementation for success envelopes and `issue_time=latest`.
- Repair M4 OpenSpec strict-validation format and bring delivery artifacts into version-control traceability.
- Add verification gates and issue links for this remediation stage.

## Capabilities

- `slurm-forecast-runtime-contract`: Forecast array tasks MUST receive or derive complete runtime manifests and valid hydro run records.
- `retry-execution-contract`: Manual retry MUST result in executable work, not a stranded pending record.
- `publish-delivery-contract`: Publish stage MUST either produce verifiable delivery artifacts or fail/skips explicitly without being counted as successful publication.
- `flood-product-readiness-contract`: Flood alert and map endpoints MUST treat product-ready terminal states consistently.
- `data-integrity-storage-contract`: Shared stores and data adapters MUST preserve model/basin/source isolation and reject invalid manifest or URI inputs.
- `api-openspec-traceability-contract`: OpenAPI, OpenSpec validation, generated types, docs, and repository tracking MUST converge with implemented behavior.

## Impact

- Touches orchestrator, Slurm gateway integration, SHUD runtime manifest preparation, pipeline retry/cancel routes, flood alert routes, tile publication, OpenAPI, frontend generated types, OpenSpec changes, docs, and tests.
- Requires targeted contract tests plus full backend regression through `uv run pytest -q`.
- Requires GitHub issues linked to this OpenSpec change before release acceptance.
