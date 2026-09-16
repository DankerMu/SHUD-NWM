# node-22 运维待办列举面（#1186）

## 为什么

db-free 调度器在五条终态决策上写 `manual_retry_required: true`，但 node-22 上**没有任何面把它们列出来**：它们终止在无人读的 evidence JSON 里。运维要知道"现在有没有人该动手"，只能手写 jq 去翻最新那趟 pass 文件——而这个临时口径的盲区正是它自己无法表达的那条：**空输出不等于没有待办**。

本 change 提供只读子命令 `list-operator-actions`：扫描证据根下最近 N 趟终态 pass，列出那五条决策的待办，并用退出码区分三种语义——`0` 确实没有待办、`1` 有待办、`3` **无法判定**。第三种是这个面存在的理由：一趟被裁剪、不可读、或范围被收窄的 pass，不构成"没有待办"的证据。

## 背景：本 change 是 PR #2398 的拆分产物

PR #2398（第 7 批 #1186/#1543/#1555/#1768/#1820）在第 5 轮交叉审查触到轮次上限，用户裁决拆分。确认物写/读侧（#1543/#1555/#1768/#1820）已作为 PR-A 由 `node22-operator-reentry-confirmation` 合并（merge commit `2ee1f53ed`）。本 change 是 PR-B，承接列举面本身与 round-5 未闭合的三条 finding。

`.review-gate-issues.json` 记录 #1186 已在 #2398 触顶，因此本 PR **开局即处于升级状态**：`depth` / `noise` 型 retro 需要记录在案的用户裁决才能注册。用户 2026-09-16 的裁决是**照常开、进门再问**。目标是 round 1 一次干净。

## 做什么

1. 恢复 `services/orchestrator/operator_action_listing.py` 与 `tests/test_operator_action_listing.py`（由 `53c39b99c` 从 PR-A 移出，内容取自 `ca22d18f9`），重新挂上 `cli.py` 的 `list-operator-actions`。
2. 修 round-5 未闭合的三条：
   - **r5-01**（可判为 P1）：窄范围 pass 会清零 `hidden_after_decidable` 并给出假 `exit 0`。
   - **r5-00**、**r5-03**（覆盖）：排序表缺行，对应的变异当前存活。
3. 把 `node22-control-plane-manual-recovery.md` 里 PR-A 留下的**过渡口径**（手写 jq 直读 evidence）整段替换为 `list-operator-actions` 的用法与退出码表。

## 不做（non-goals）

- 不提供**执行**入口。本面只读：不写 journal、不提交作业、不改任何决策。执行通道是 PR-A 已合并的 `confirm-operator-reentry`。
- 不改那五条决策本身的产生条件，也不改 bounded summarization 的保留键集合（那是 PR-A 的面，本面是它的消费方）。
- 不修 #2400 / #2402 / #2403 / #2404 / #2405 / #2393 —— 拆分计划已列为两个子 PR 都不修的遗留项。
