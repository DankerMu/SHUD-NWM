## Why

新流域回填自 2026-08-08T00:00Z 起完全停滞，不会自愈（#1775）。

`_cycle_completion_verdict`（`services/orchestrator/scheduler_discovery.py:279`）把
strict-warm-start 准入判定排在 terminal 终态判定**之前**：:311 在 strict 不 ready 时
直接 `return "gap"`，:326 的 `candidate_state_decider` 永远到不了。warm-start 准入回答
的是"该不该**发起**这一趟运行"，对一趟**已经跑完**的 cycle 没有意义——但它排在了
"这趟是不是已经成功"之前，于是一个已完成的 cycle 被永久判为 `gap`，回填窗口
（`scheduler_discovery.py:716`，`remaining_gaps[:1]`）再也推不动。

node-22 只读探针实测：2026-08-08T00:00Z 上 22 个 model **全部** `terminal_hydro_success`；
而 2026-08-08T12:00Z 上新流域 `strict_ready=true`，只差被放出去跑。

## What Changes

- 完成度判定改为 **terminal 终态优先于 warm-start 准入**：terminal-decision 判定移到
  strict/successor 早退之前。
- 拆开 `_terminal_init_state_verdict` 今天用一个 `conflict` 表达的两种含义：
  (a) strict 解析未指名任何 state——**无从比较**；(b) `terminal_init_state_match` 判出的
  **真实不一致**。(a) 获得独立 verdict `unverifiable`，(b) 仍是硬 gap。
- (a) 的容忍规则沿用 `absent` 分支已有的同一条物理连续性标准：当且仅当
  `_successor_state_proves_continuity` 为真时判 `complete`。不发明新的宽松度。
- 候选循环同型缺陷：`scheduler_candidates.py:437` 的 `continue` 排在
  `classify_candidate_state()`（:438）之前，terminal 终态短路提前。
- **根因**：`packages/common/state_manager.py:1470-1482` 的 §8 history-existence
  按 `valid_time <= cutoff` 收窄——一趟运行自己的产出不再算作"它有前驱"的证据。

## Capabilities

- `cross-cycle-warm-start-chaining` — 完成度判定的判定顺序与 (a) 的容忍规则。
- `strict-warm-start` — 推翻 "strict 未指名 candidate_state 时 verdict 路径 bypass helper
  并保持今天的 gap 行为" 这条已记录决定。

## Non-Goals

- 不改 cohort 级 all-or-nothing copyback 门控。
- 不改回填窗口选择策略（`remaining_gaps[:1]` 是设计使然，不是缺陷）。
