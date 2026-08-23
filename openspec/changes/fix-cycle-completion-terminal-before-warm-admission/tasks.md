# Tasks

## 0. Evidence Floor

- [ ] `uv run pytest -q tests/test_production_scheduler.py tests/test_scheduler_generation.py tests/test_state_manager_generation_history.py tests/test_scheduler_backfill_predecessor.py` 全绿（本地）
      —— **口径修正**：初稿写的是 `-k "cycle_completion or completion_verdict or warm_admission or init_state"`，
      该关键字集**选不中任何 D5/M4 测试**（`generation_scoped_history_signal_*`、
      `future_only_history_*` 一个词都不匹配），照那条跑会"以错误的理由变绿"。
      改为直接点名实际承载证据的四个 suite。
- [ ] `uv run ruff check .` 全绿（本地）
- [ ] `openspec validate fix-cycle-completion-terminal-before-warm-admission --strict --no-interactive` 全绿（本地）
- [ ] **三个 mutation check**，每一个都必须能把对应测试变红：
      1. 把 D1 的判定顺序改回 strict-early-return 优先 → 红
      2. 把 D2 的 `unverifiable` 塌回统一 `CONFLICT` → 红
      3. 去掉 D3 的 successor 容忍条件（`unverifiable` 无条件判 complete）→ 红
      4. 去掉 D5 的 `valid_time <= cutoff` 过滤 → 红
      第 3 条是新放宽的安全销子；缺了它这次放宽就没有 pin。
- [ ] **must-preserve 回归**：真实不一致仍 gap、`absent` 语义不变、successor 不 ready
      仍 gap、`match` 路径不变、非 terminal 仍 gap —— 各一条断言。
- [ ] **node-22 部署后收据**（按序，全部必须满足）：
      1. 下一趟 pass `blocked_candidate_count` 28 → 0
      2. `2026-08-08T00:00:00Z` 出现在 skipped 且 reason 为 terminal
      3. **`2026-08-07T12:00:00Z` 的 BLOCKED 行不存在** —— 口径注意：那些 cycle
         新流域压根没跑过，永远不会变成 terminal success；它们是因为 0800 判完成、
         前驱回退不再发生而**消失**，不是"判为完成"
      4. 窗口选中 `2026-08-08T12:00:00Z`，每 source 提交 7 成员 forecast array
- [ ] **第二个 cycle warm start 收据**（node-22 manifest）：`quality=fresh`、
      `state_id` 非空、`lineage.cycle_id=*_2026080800`、`init_mode=3`

## 1. Implementation

- [ ] D1：`_cycle_completion_verdict` 第一分支判定顺序改为 terminal 优先
- [ ] D2：`_terminal_init_state_verdict` 拆出 `unverifiable`，新增常量并纳入返回值域
- [ ] D3：`unverifiable` 的容忍分支复用 `_successor_state_proves_continuity`
- [ ] D4：`scheduler_candidates.py` warm-admission 分支之前先 `classify_candidate_state()`，
      terminal 终态短路
- [ ] D5：`packages/common/state_manager.py` 的 `entries_for_model` 按
      `valid_time <= cutoff` 过滤；更新那段注释说明为何 `> cutoff` 不算历史

## 2. Spec

- [ ] `cross-cycle-warm-start-chaining` delta：判定顺序 + `unverifiable` 容忍规则 + D5 的
      history-existence 作用域（ADDED requirement）
- [ ] `strict-warm-start` delta：推翻 "verdict 路径 bypass helper 并保持今天的 gap 行为"

## 3. Follow-ups filed

- [x] #1776 — 根因 `exists_any_generation` 作用域：owner 指示不另案，已并入本 change 的 D5，
      issue 关闭并留言记录改动面实测。
