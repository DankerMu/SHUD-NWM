## 1. Retry Manifest Runtime Roots

- [ ] 1.1 Resolve `object_store_root` and `object_store_prefix` for
  `download_source_cycle` manual retries from original job/runtime context.
- [ ] 1.2 Preserve `published_artifact_root` and
  `published_artifact_uri_prefix` when available.
- [ ] 1.3 Include resolved roots in the Slurm submission manifest without
  relying on workspace fallback.
- [ ] 1.4 Fail closed with a stable retry submission error when required
  shared-source roots cannot be resolved safely.

## 2. Slurm Env and Evidence

- [ ] 2.1 Add production-like test coverage where `WORKSPACE_ROOT` and
  `OBJECT_STORE_ROOT` differ and the rendered sbatch exports the object-store
  root.
- [ ] 2.2 Ensure retry submission success/failure events include bounded,
  redacted runtime-root resolution evidence.
- [ ] 2.3 Ensure retry API error payloads do not leak secret-bearing roots,
  URI userinfo, tokens, signatures, or credentials.
- [ ] 2.4 Preserve duplicate manual retry guard behavior and existing
  non-source retry compatibility.

## 3. Risk-Pack Evidence Map

- [ ] 3.1 Public API / CLI / script entry: cover retry API or direct service
  call returning submitted vs submission_failed for this contract.
- [ ] 3.2 Config / project setup: cover split `WORKSPACE_ROOT` and
  `OBJECT_STORE_ROOT` plus object-store prefix.
- [ ] 3.3 File IO / path safety / overwrite: cover that shared source retry
  writes/env-targets object-store root, not workspace root.
- [ ] 3.4 Schema / field names and provenance: cover manifest/event fields for
  `object_store_root`, `object_store_prefix`, published root/prefix, and
  runtime-root evidence.
- [ ] 3.5 Auth / permissions / secrets: cover redaction of secret-bearing
  runtime roots and prefixes.
- [ ] 3.6 Concurrency / shared state / ordering: cover duplicate active retry
  guard still prevents second manual retry.
- [ ] 3.7 Legacy compatibility: cover non-`download_source_cycle` retry still
  submits with existing behavior.
- [ ] 3.8 Error handling / rollback / partial outputs: cover missing required
  roots fail before Slurm submission and leave stable `submission_failed`
  evidence.
- [ ] 3.9 Slurm lifecycle / manifest provenance: cover the submitted manifest
  and rendered sbatch env carry the same root contract.

## 4. Tests and Documentation

- [ ] 4.1 Add direct retry-service regression for IFS shared source-cycle retry
  with split roots: manifest contains source/cycle identity and object-store
  roots/prefix.
- [ ] 4.2 Add Slurm render/gateway regression showing
  `export OBJECT_STORE_ROOT=<object-store>` rather than workspace root.
- [ ] 4.3 Add fail-closed regression for missing required object-store root:
  no gateway submission, retry row becomes `submission_failed`, stable error
  code, redacted details.
- [ ] 4.4 Add redaction regression for secret-bearing root/prefix in event and
  API-visible error evidence.
- [ ] 4.5 Update node-22/operator runbook with recovery guidance for legacy
  wrong-root retries and safe remediation steps.

## 5. Verification

- [ ] 5.1 Run `uv run --no-sync pytest -q tests/test_retry.py`.
- [ ] 5.2 Run focused Slurm gateway/template tests touched by this change.
- [ ] 5.3 Run `uv run --no-sync ruff check .`.
- [ ] 5.4 Run
  `openspec validate issue-384-retry-runtime-roots --strict --no-interactive`.
