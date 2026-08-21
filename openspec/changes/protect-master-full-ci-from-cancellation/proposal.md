## Why

Master 的全量 pytest 是 PR 定向测试之后唯一的全仓回归，但当前所有 master push 与手动 dispatch 共用 `refs/heads/master` concurrency group 且 `cancel-in-progress: true`；后续合并会取消仍在运行的全量车道，GitHub 同组 pending replacement 还会静默丢掉排队 run。#1119 的同一回归连续三次未完成，证明这不是假设。

## What Changes

- PR run 继续按 PR number 分组并取消 superseded run。
- push-to-master 与 workflow_dispatch 改用唯一 `github.run_id` 分组，既不互相取消，也不替换同组 pending run。
- 让 `.github/workflows/ci.yml` 自修改命中 backend paths-filter，并由 targeted selector 路由到 CI contract suite。
- 加静态契约测试钉住 group/cancel truth table 与 self-routing，防止一行回退重新静默打断 master 全量车道。

## Capabilities

### New Capabilities

- None.

### Modified Capabilities

- `ci-contract-baseline`: CI concurrency 与 workflow-self-change 的可执行验证契约。

## Impact

- 修改 `.github/workflows/ci.yml`、`scripts/select_ci_tests.py`、`tests/test_select_ci_tests.py`。
- 不改 pytest marker、全量 suite 内容、45 分钟 job timeout、schedule、生产代码或依赖。
