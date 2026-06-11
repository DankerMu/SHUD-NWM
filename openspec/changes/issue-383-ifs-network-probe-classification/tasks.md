## 1. Adapter Classification

- [x] 1.1 Update IFS availability probing so `NetworkDownloadError`, DNS/name-resolution, timeout, and equivalent probe failures are not returned as plain `False`.
- [x] 1.2 Preserve genuine 404/unpublished as `status=unavailable`, `reason=source_cycle_unavailable`, `classifier=unavailable`, and `retryable=true`.
- [x] 1.3 Preserve forbidden source behavior as `status=forbidden`, `classifier=forbidden`, and `retryable=false`.
- [x] 1.4 Add redacted attempted mirror evidence with array entries shaped like `source`, `uri`, `status`, `error_class`, and `error_message`; probe failures must use `status=probe_failed`, `reason=source_cycle_probe_failed`, `classifier=network_error`, and `retryable=true`.
- [x] 1.5 Bound attempted mirror evidence before exposure, preserving `source`, `uri`, `status`, `error_class`, `error_message`, total attempted count, and omitted attempted count.
- [x] 1.6 Preserve all-mirror rate limiting as `status=rate_limited`, `reason=source_cycle_rate_limited`, `classifier=rate_limited`, and `retryable=true`.

## 2. CLI and Scheduler Evidence

- [x] 2.1 Ensure `nhms-ifs download --cycle-time 2026-06-08T00:00:00Z` emits a non-misleading JSON payload for network/probe failures: `status=probe_failed`, `reason=source_cycle_probe_failed`, `classifier=network_error`, `retryable=true`, `files=0`, `total_bytes_written=0`, and attempted mirror entries for AWS/Azure/Google/ECMWF.
- [x] 2.2 Preserve CLI failure exit behavior for a blocked download while changing the emitted payload away from `status=unavailable` for network/probe failures.
- [x] 2.3 Ensure scheduler/readiness evidence consumes the new probe-failure classification as retryable and does not convert it into definitive source unavailable or manual-only terminal evidence.
- [x] 2.4 Keep successful discovery, all-404 source-latency evidence, and forbidden source evidence backward compatible.
- [x] 2.5 Ensure public `nhms-ifs download --cycle-time ...` entry coverage emits one bounded JSON payload for probe failures while exiting non-zero.
- [x] 2.6 Ensure QHH diagnostic scripts preserve typed `probe_failed` and `rate_limited` state instead of overwriting it as generic failed.

## 3. Risk-Pack Evidence Map

- [x] 3.1 Public API / CLI / script entry: cover `nhms-ifs download --cycle-time` payload and exit behavior for network/probe failure.
- [x] 3.2 Config / project setup and external provider reproducibility: cover configured mirror order AWS/Azure/Google/ECMWF and source-cycle identity `IFS 2026060800 f000`.
- [x] 3.3 Schema / field names and provenance: cover exact `status`, `reason`, `classifier`, `retryable`, `attempted_sources`, `files`, and `total_bytes_written` fields.
- [x] 3.4 Auth / permissions / secrets: cover redaction for credential-like URL userinfo and token/signed query values in URI and error message evidence.
- [x] 3.5 Discovery and resource limits: cover all-mirror DNS/network failure, all-mirror 404, mixed 404+DNS, and forbidden probes.
- [x] 3.5a Discovery evidence bounding: cover unsafe configured source labels, many configured fallback sources, and long exception strings.
- [x] 3.6 Legacy compatibility: cover unchanged discovered-cycle upsert only on success, all-404 unavailable behavior, forbidden non-retryable behavior, no DB enum/schema migration, no Slurm gateway contract change, and no GFS/canonical/forcing/frontend behavior change.
- [x] 3.7 Error handling / Slurm lifecycle: cover scheduler evidence remaining retryable and not definitive source unavailable for probe failures, while genuine unavailable cycles keep existing scheduler semantics.
- [x] 3.7a Diagnostic runner lifecycle: cover local QHH continuous runner preserving typed retryable IFS blocker state written by the cycle script.
- [x] 3.8 Documentation / migration notes: cover node-22 operator recovery guidance for compute-node network failure.

## 4. Tests and Documentation

- [x] 4.1 Add regression test input where AWS/Azure/Google/ECMWF probes for `IFS 2026060800 f000` each raise DNS/name-resolution/network errors; expected output is `available=false`, `status=probe_failed`, `reason=source_cycle_probe_failed`, `classifier=network_error`, `retryable=true`, no forecast-cycle upsert, and four attempted-source evidence entries.
- [x] 4.2 Add regression test input where AWS/Azure/Google/ECMWF probes all raise `FileUnavailableError`; expected output remains `available=false`, `status=unavailable`, `reason=source_cycle_unavailable`, `classifier=unavailable`, and no forecast-cycle upsert.
- [x] 4.3 Add regression test input with mixed `FileUnavailableError` and DNS/network errors; expected output is probe failure, not definitive source unavailable, with both not-found and network attempt evidence preserved.
- [x] 4.4 Add forbidden-source regression coverage; expected output remains `status=forbidden`, `classifier=forbidden`, `retryable=false`.
- [x] 4.5 Add CLI or scheduler evidence regression coverage showing attempted mirrors and concrete redacted error type/message; include an input URI or error string with userinfo/query token and assert secrets are absent.
- [x] 4.6 Update node-22 runbook notes for operator recovery after compute-node network failure during shared source-cycle download.
- [x] 4.7 Add regression coverage for bounded attempted-source evidence and unsafe source labels.
- [x] 4.8 Add backfill-mode scheduler regression coverage that a `probe_failed` gap keeps `status=probe_failed`, `reason=source_cycle_probe_failed`, `classifier=network_error`, `retryable=true`, no DB enum write, and selection reason `source_cycle_probe_failed_does_not_consume_source_budget`.
- [x] 4.9 Add focused QHH diagnostic runner/static coverage proving typed `probe_failed` is preserved.

## 5. Verification

- [x] 5.1 Run focused IFS adapter/CLI tests, for example `uv run --no-sync pytest -q tests/test_ifs_adapter.py`.
- [x] 5.2 Run focused scheduler evidence tests touched by this change.
- [x] 5.3 Run `uv run --no-sync ruff check .`.
- [x] 5.4 Run `openspec validate issue-383-ifs-network-probe-classification --strict --no-interactive`.
