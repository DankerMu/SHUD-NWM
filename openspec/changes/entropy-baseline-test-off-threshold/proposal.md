# entropy-baseline-test-off-threshold

## Why

master 的全量回归 `Unit Tests (full) (3)` 自 PR #2820 起连续红（#2885）：`tests/test_entropy_audit_baseline_writer_summary.py::test_entropy_baseline_writer_preserves_v1_trend_semantics_for_current_repo` 把 `cleanup_priorities` 钉成字面量，而其中 Playwright 条目的 `impact` 由当前仓库里 `broad-e2e-api-mock` finding 的条数经阈值（≥ 10 → `high`）派生。#2820 新增一个 mocked spec 把条数从 9 推到 10，`impact` 由 `medium` 变 `high`、排序随之变化。被测代码与分类逻辑都没错，错的是断言贴着阈值钉了仓库现状；这条红让 master 唯一的全量回归信号对其后所有合并失明。

## What Changes

- 只改 `tests/test_entropy_audit_baseline_writer_summary.py`：current-repo 用例继续钉稳定部分（route-token 与 orchestrator 两条整条、Playwright 条目的 `target` / `axis` / `effort`），Playwright 条目的 `impact` 改为取被测模块对同一份报告该组 findings 的计算结果，顺序改为断言排序规则而不是下标；阈值两侧（9 / 10）与排序由一条合成 findings 的用例用字面量钉住。

不改 `scripts/governance/**`、`.entropy-baseline/**`、`.github/workflows/**`、`apps/frontend/**`；不为让断言通过而减少前端 mock。design.md 省略（compact）。

## Triage

```text
Issue type: bugfix (test)
Fixture level: compact
Upstream suggested level: compact (agree)
Blast radius: 一条 pytest 用例；改坏时断言变松（条目丢失 / axis 变化不再被发现）
Selected risk packs: Legacy compatibility（稳定部分的判别力不得丢）
Evidence floor: 该文件在 master 上先红后绿；阈值两侧的合成用例；稳定部分的变异仍红；合并后 master 全量四片绿
```

## Impact

- 受影响文件：`tests/test_entropy_audit_baseline_writer_summary.py`。
- 受影响规格：`governance-entropy-baseline`。
