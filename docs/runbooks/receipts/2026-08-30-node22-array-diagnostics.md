# Node-22 Array Diagnostics Live Receipt

Captured: 2026-08-30

Scope: issues #1742 / #1539, PR #1901, OpenSpec task 2.3
(`array-diagnostics-hardening`).

Source of this receipt: local sanitized 0600 JSON
`.workplans/1742-1539/live/node22-receipt-summary.json`
(schema `nhms.node22.array_diagnostics_receipt.v1`). No credential,
Authorization header, or `gateway.env` content is present here or in that
JSON.

```text
source_head=4d5c0a06802b31187bf7c39dfb35a529536ca19d
source_archive_sha256=db2605b0e88d61fd1cf0c3325369bc8b651b5a6703db793c8a34bb1e2aa8f084
python=/scratch/frd_muziyao/NWM/.venv/bin/python (existing 3.12.7; no uv sync)
```

## Submission

Real node-22 `run_shud_forecast_array` job `39191`, array `0-1%2` (two
tasks), partition `CPU`, minimal resources (`nodes=1`, `ntasks=1`,
`cpus_per_task=1`, `memory_gb=1`, `walltime=00:02:00`).

Expected outcome is the template's existing `controlled_failure` branch,
not a successful solve and not a runtime defect. Both tasks converged
exactly as that branch requires:

```text
39191_0  FAILED  exit_code=2:0  2026-08-30T19:34:31 -> 2026-08-30T19:34:32
39191_1  FAILED  exit_code=2:0  2026-08-30T19:34:31 -> 2026-08-30T19:34:32
```

Exact index identities:

```text
task 0 -> model_id=issue1742_model_alpha  run_id=issue1742_run_alpha
task 1 -> model_id=issue1742_model_beta   run_id=issue1742_run_beta
```

## Neutral lane and exact manifest index

```text
manifest_index_path=/scratch/frd_muziyao/NWM-issue-1742-receipt/workspace/issue1742_receipt_20260830/manifests/forecast_receipt_index_20260830T113431420472.json
manifest_index_sha256=12ab460b4e4dbf7c2305895242a634fa1e0960d2c4704a63e87ece3b87220379
array_log_dir=/scratch/frd_muziyao/NWM-issue-1742-receipt/workspace/issue1742_receipt_20260830/array_logs/forecast_receipt_index_20260830T113431420472
directory_stem_equals_index_stem=true
member_identity_absent_from_path=true
```

The log-directory stem equals the immutable index stem
`forecast_receipt_index_20260830T113431420472`. The neutral lane contains
no member `model_id` or `run_id`.

## Task logs

All four files exist in the neutral directory. Empty stderr is a created
0-byte file, not a missing file (empty-blob SHA-256
`e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`):

```text
39191_0.out  110 bytes  sha256=d5d9b50e3b8f1b70fd5e2036a7ee94e62bd28d9562e0ce0eb874e806f488b285
39191_0.err    0 bytes  sha256=e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855
39191_1.out  110 bytes  sha256=d5d9b50e3b8f1b70fd5e2036a7ee94e62bd28d9562e0ce0eb874e806f488b285
39191_1.err    0 bytes  sha256=e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855
```

Both stdout streams contain the controlled-failure marker and the
`NON_FINITE_FLOW` signature. Both stderr streams exist at 0 bytes.

## Log interface and process-level restart

Pre-restart (gateway PID `2615436`):

```text
metadata_complete=true
task 0 identity_complete=true
task 1 identity_complete=true
```

Process-level restart `2615436 -> 2624572`, then:

```text
metadata_complete=false
task 0 identity_complete=true  (issue1742_model_alpha / issue1742_run_alpha)
task 1 identity_complete=true  (issue1742_model_beta / issue1742_run_beta)
```

In-memory record metadata is incomplete after restart, as required. Exact
task identity is still recovered from the immutable index: task 0 maps to
alpha, task 1 maps to beta. No guessed identity.

## Temporary receipt gateway vs active gateway

Temporary receipt gateway only:

```text
url=http://127.0.0.1:18042
DATABASE_URL absent
python=existing 3.12.7; no uv sync
healthy_before=true
healthy_after=true
stopped_after_receipt=true
credential_removed_after_receipt=true
```

It listened on loopback `127.0.0.1:18042` only. It had no `DATABASE_URL`
and did not connect to the node-22 archived DB.

Active production gateway was not restarted or modified:

```text
url=http://127.0.0.1:8001
pid=2486034
healthy=true
```

## Durable evidence

Remote durable evidence root:

```text
/scratch/frd_muziyao/NWM-issue-1742-receipt
```

Proven artifacts under that root, plus the sanitized summary:

- `receipt-summary.json` (local sanitized 0600 copy:
  `.workplans/1742-1539/live/node22-receipt-summary.json`)
- exact manifest index
  `workspace/issue1742_receipt_20260830/manifests/forecast_receipt_index_20260830T113431420472.json`
- array logs
  `workspace/issue1742_receipt_20260830/array_logs/forecast_receipt_index_20260830T113431420472/39191_{0,1}.{out,err}`
- sacct rows for `39191_0` and `39191_1` recorded in the summary
- log-interface pre-restart / post-restart observations recorded in the
  summary (`metadata_complete`, per-task `identity_complete`,
  controlled-failure marker, `NON_FINITE_FLOW`)

Temporary credential was deleted after capture. This receipt and the
sanitized JSON contain no credential.

## Verdict

PASS for OpenSpec task 2.3:

1. Forecast array with at least two members: live `run_shud_forecast_array`
   job `39191`, array `0-1%2`, tasks 0 and 1 (alpha / beta).
2. Log directory names no member: neutral lane
   `.../array_logs/forecast_receipt_index_20260830T113431420472` has
   `member_identity_absent_from_path=true` and stem equality with the
   exact index.
3. Two task ids map to their own model/run through the log interface:
   task 0 -> `issue1742_model_alpha` / `issue1742_run_alpha`, task 1 ->
   `issue1742_model_beta` / `issue1742_run_beta`, with
   `identity_complete=true` before and after the PID
   `2615436 -> 2624572` restart.
