## Context

CI 顶层 concurrency 当前使用：

```yaml
group: ci-${{ github.workflow }}-${{ github.event.pull_request.number || github.ref }}
cancel-in-progress: true
```

PR 使用 number 聚合并取消旧 run 是正确省钱策略；push/master 与 workflow_dispatch 则都退回同一个 `github.ref`，所以后续 master push 会取消正在运行的全量 pytest。GitHub concurrency 还只保留一个 pending run：即使仅把 `cancel-in-progress` 改成 false，新 run 仍会替换同组 pending run，不能满足“每次 master 回归都保留”。GitHub 官方 context 定义中 `github.run_id` 对每个 workflow run 在仓库内唯一，并在 rerun 时保持不变。

实证：run 32390417404 的 master full job 被后续 push 取消；manual run 32391122276 在约 38 分钟后被另一个 master push 取消。第二个 run 证明 manual 与 push 也错误共享组。

Fixture level: **expanded**（CI entrypoint + concurrency/shared ordering 是 mandatory expanded triggers）。Repair intensity: **high**（共享 CI 控制面；错误会静默取消唯一全量回归）。

## Goals / Non-Goals

**Goals:**

- PR：相同 PR 的后续 push 仍取消旧 run。
- 非 PR：每个 workflow run 使用唯一 group，push/manual 互不取消且不发生 pending replacement。
- workflow 文件改动在 PR 上真实执行契约测试，而不是因自身不命中 backend filter 而 skip。
- 以 mutation red proof 证明 `github.ref` fallback、全局 `true` cancellation、缺失 self-route 都会被测试打红。

**Non-Goals:**

- 不承诺 master runs 串行；唯一 group 允许并行，换取不丢回归。
- 不调整 `Unit Tests (full)` 的 45 分钟 timeout；真实完成耗时待本变更合并后的 master receipt 测量，若超时另报。
- 不增加 schedule，也不修 #1644 的路径过滤 OpenAPI 基线红。
- 不改变 Governance workflow；其 report-only run 不承载唯一全量 pytest oracle。
- 不解决 #1632 marker 套件实机测量。

## Decisions

### D1 — PR number / non-PR run_id 双轨 group

使用一个条件表达式：

```yaml
group: ci-${{ github.workflow }}-${{ github.event_name == 'pull_request' && github.event.pull_request.number || github.run_id }}
```

PR number 保持现有 supersession identity。push 与 workflow_dispatch 没有可安全共享的业务 identity，使用 run_id 唯一化；这同时消除 running cancellation 与 pending replacement。`github.sha` 也可唯一化，但 rerun/同 commit 的不同 dispatch 语义更含糊；run_id 正是 workflow-run identity。

### D2 — 仅 PR 允许 cancel-in-progress

```yaml
cancel-in-progress: ${{ github.event_name == 'pull_request' }}
```

这条不是单独的完整修复，必须与 D1 同时存在。D1 防 pending replacement，D2 防 running cancellation。

### D3 — workflow self-routing 是发布契约的一部分

`.github/workflows/ci.yml` 加入 `changes.backend` filter；`scripts/select_ci_tests.py` 加 exact path rule，选择 `tests/test_select_ci_tests.py`。两层都需要：只有 filter 没 selector 会走 zero-assertion collect-only；只有 selector 没 filter，targeted job 不启动。

### D4 — 测试钉语义，不引入 YAML parser authority

现有 CI 契约测试就在 `tests/test_select_ci_tests.py`，且已通过文本切片检查 workflow 中无法本地执行的 GitHub expression。新增测试：

- 提取唯一顶层 concurrency block，钉住 PR-number / run-id / conditional-cancel 三个载重点并拒绝 `github.ref` fallback；
- 通过真实 `select_tests` 行为证明 workflow path 精确选择 meta-guard suite；
- 从 `changes` 的 backend literal block确认 workflow path 会打开 targeted job。

不使用 PyYAML 解析 GitHub workflow：YAML 1.1 会把 `on` 解析成布尔值，且 parser 不执行 GitHub expression。

## Invariant Matrix

Governing invariant: PR superseded runs MAY cancel；每个 push/manual CI run MUST 保留到自身终态，且对 concurrency policy 的修改 MUST 在 PR 上执行 assertion-level contract tests。

- Source of truth: `.github/workflows/ci.yml` 顶层 `concurrency` 与 `changes.backend` filter。
- Producers: GitHub `pull_request` / `push` / `workflow_dispatch` events and contexts.
- Validators/preflight: `tests/test_select_ci_tests.py` workflow block + selector behavior tests。
- Storage/cache/query: GitHub Actions concurrency scheduler；仓库无本地持久态。
- Public entrypoint: CI workflow triggers。
- Downstream consumers: `Unit Tests (full)`、targeted `Unit Tests`、merge 后 master regression。
- Failure paths/stale state: running cancellation、pending replacement、workflow self-change skip/collect-only。
- Evidence/audit: focused pytest、selector output、PR CI receipt、合并后 master run terminal receipt。

Regression rows:

- same PR number + new push -> same group + old PR run cancelled。
- two master pushes or manual runs -> different run_id groups + neither cancelled by policy。
- ci.yml-only PR -> backend true + selector meta-guard suite executes assertions。
- mutate run_id fallback to ref / conditional cancellation to true / remove self-route -> contract test red。

## Risks / Trade-offs

- [Master merges can run full suites concurrently] → intentional; losing all but one post-merge oracle is worse. Hosted-runner capacity queues outside this workflow group rather than cancelling repository evidence.
- [Full suite may exceed 45 minutes] → record actual first terminal receipt; timeout is a separate measured follow-up, not guessed here.
- [GitHub expression drift] → exact contract test plus live PR workflow parse/receipt.
- [Workflow-only change could bypass its test] → D3 closes both filter and selector legs.

## Migration Plan

1. Add contract tests and selector/self-filter routes; prove tests red against current workflow.
2. Change concurrency expressions; run focused local tests and PR CI.
3. Merge after review; watch the resulting master run to terminal state. A later master push must not cancel it; absence of overlap is still evidence that the run completes, not proof of collision handling.
4. Rollback by reverting the three-file change if GitHub rejects the expression or PR supersession stops working.
