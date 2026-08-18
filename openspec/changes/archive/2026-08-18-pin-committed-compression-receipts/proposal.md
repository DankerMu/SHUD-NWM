## Why

Issue #1389：四份已提交历史 compression runner receipt 被
compression-receipt-budget-audit 的 Must-preserve 点名"在新 schema 下 valid"，
但无任何提交测试把真实文件喂进 schema——收紧 1.0/2.0 分支会让归档当场失效而测试全绿。

## What Changes

- `tests/test_node27_timeseries_compression.py`：glob 参数化校验
  `docs/runbooks/receipts/.../timeseries-compression/` 下 `dry-run-*` / `enforce-*`
  真实 receipt（按文件名前缀取集合，显式规避同目录同 schema_version 的
  `terminal-replay-*` live-evidence 家族）；另加 glob 非空计数守卫（≥4）。

## Non-Goals

- 不改脚本 / schema / 任何 receipt 文件；目录双家族混放问题（PR #1388 范围外）。

## Risk triage

- Fixture level: none（test-only）。Repair intensity: low。
- Risk packs: test-evidence selected；其余 not selected。

## Must preserve

- 既有 111 条测试全绿；receipt 文件零字节改动。
