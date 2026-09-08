## Why

PR #2126 的独立 verifier 确认 C4 两个输入错误场景可同时命中，而权威规格未写分类优先级。实现已经冻结为 receipt-path preflight → URL → pin；本变更只把该事实写成唯一规范结果，避免 #1895 验收误判。

## What Changes

- 明确 receipt path 或 frontend/API URL 缺失时，BLOCKED 优先于同次 basin/segment pin 错误。
- pin 的 FAIL CONFIG_INVALID 场景仅在 receipt path 和两个 URL 都已提供时适用。
- 不修改 owner/config/lane、schema、状态闭集或任何测试。

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `c4-live-display-evidence`: 补齐组合无效输入的分类优先级。

## Impact

仅 OpenSpec 权威规格。Closes #2130；不关闭 #1895/#1891，不执行 node-27/live。fixture level: none（docs/spec-only），design.md exempt。Minimal mergeable slice: atomic — 单个 requirement 的两条 WHEN 消歧，无法再拆。
