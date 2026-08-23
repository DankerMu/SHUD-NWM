# Tasks

## 0. Evidence Floor

- [x] `uv run pytest -q tests/test_production_scheduler.py tests/test_scheduler_generation.py tests/test_state_manager_generation_history.py tests/test_scheduler_backfill_predecessor.py` 全绿（本地）
      —— **口径修正**：初稿写的是 `-k "cycle_completion or completion_verdict or warm_admission or init_state"`，
      该关键字集**选不中任何 D5/M4 测试**（`generation_scoped_history_signal_*`、
      `future_only_history_*` 一个词都不匹配），照那条跑会"以错误的理由变绿"。
      改为直接点名实际承载证据的四个 suite。**终值 2081 passed**（终审独立复跑核实）。
- [x] `uv run ruff check .` 全绿（本地）
- [x] `openspec validate fix-cycle-completion-terminal-before-warm-admission --strict --no-interactive` 全绿（本地）
- [x] **mutation check**，每一条都必须能把对应测试变红（各自先红后还原）。
      初稿写"三个"却列了四条，且随两轮审查发现又增补三条；实际交付 **M1-M7 + 两条附加**：
      1. **M1** 把 D1 的判定顺序改回 strict-early-return 优先 → 4 红
      2. **M2** 把 D2 的 `unverifiable` 塌回统一 `CONFLICT` → 6 红
         （含 candidate-loop 侧——D4 改为引用 verdict 判定式后的**反脱钩证据**：改一边两边同红）
      3. **M3** 去掉 D3 的 successor 容忍条件 → 3 红
      4. **M4** 去掉 D5 的 `valid_time <= cutoff` 过滤 → 6 红
      5. **M5** 把成因允许清单放宽为任意 not-ready → 16 红
      6. **M6** 只删 D4 的允许清单子句 → 4 红
      7. **M7** 把 D4 的 `_successor_state_proves_continuity` 换回
         `_successor_state_terminal_can_skip` → 1 红，恰好只红被修复的那条回归
      附加：D4 两子句全删 → 6 红；去掉 D4 的 terminal skip → 1 红
      M3 是本次放宽的安全销子，M5/M7 是两轮审查后新增的销子；缺任何一条对应放宽就没有 pin。
- [x] **must-preserve 回归**：真实不一致仍 gap、`absent` 语义不变、successor 不 ready
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

- [x] D1：`_cycle_completion_verdict` 第一分支判定顺序改为 terminal 优先
- [x] D2：`_terminal_init_state_verdict` 拆出 `unverifiable`，新增常量并纳入返回值域
- [x] D3：`unverifiable` 的容忍分支复用 `_successor_state_proves_continuity`
- [x] D4：`scheduler_candidates.py` warm-admission 分支之前先 `classify_candidate_state()`，
      terminal 终态短路
- [x] D5：`packages/common/state_manager.py` 的 `entries_for_model` 按
      `valid_time <= cutoff` 过滤；更新那段注释说明为何 `> cutoff` 不算历史
- [x] **D6（第二轮增补）**：`unverifiable` 收敛到**闭合成因允许清单**——不在清单内一律
      `conflict`，无 reason 亦然（fail closed）。清单只含
      `state_snapshot_index_exact_checkpoint_missing`；显式排除
      `state_snapshot_index_prior_checkpoint_missing_after_history`（历史存在而 checkpoint
      丢失，属 #1150/#1152 人工回填 population，是异常不是缺席）。
- [x] **D7（第三轮增补）**：D4 出口改为**引用** verdict 路径自身的两个判定式
      （`_terminal_init_state_verdict != CONFLICT` 且 `_successor_state_proves_continuity`），
      不再维护平行的窄谓词。消除了两处 receipt-vs-verdict 分歧，并使规则只有一个所有者。

## 2. Spec

- [x] `cross-cycle-warm-start-chaining` delta：判定顺序 + `unverifiable` 容忍规则 + D5 的
      history-existence 作用域（ADDED requirement）+ 允许清单规则
- [x] `strict-warm-start` delta：推翻 "verdict 路径 bypass helper 并保持今天的 gap 行为"

## 3. Follow-ups filed

- [x] #1776 — 根因 `exists_any_generation` 作用域：owner 指示不另案，已并入本 change 的 D5，
      issue 关闭并留言记录改动面实测。
- [x] #1782 — `unverifiable` 放宽对 strict not-ready 的**成因**不作区分（错代/损坏/已修复前驱
      也能靠可用后继判 complete）。独立 verifier 裁 PLAUSIBLE、DEFER：代码与规格一致，
      非 code-vs-spec 偏差，`design.md` 风险轴 (1) 已接受。修法方向为成因允许清单。
- [x] #1757 — retention 与 D5 的交互（裁剪掉某 (model, source) 在 cutoff 或更早的最后一条条目
      会使其冷启动而非阻塞）已作为设计输入留言路由，现网无此几何形状。
