# Tasks — clone-lineage admission predicate convergence

## 1. #1739 — DB 面谓词收敛到「纯 provenance」

- [x] 1.1 `packages/common/state_manager.py:934`（函数 `get_earliest_clone_row_for_model_source`,
      `:897-941`）删除 SQL 里的 `AND clone_gate_fingerprint IS NOT NULL`；保留
      `cloned_from_model_id IS NOT NULL` 与 `cloned_from_model_id <> model_id`，保留
      `ORDER BY valid_time ASC, created_at ASC`，保留「无 `usable_flag` 过滤」。
- [x] 1.2 同函数 docstring：把「Same shadow-proof `clone_gate_fingerprint IS NOT NULL` filter as the
      publisher's reader」改写为裁定后的口径——血缘准入只认 `cloned_from_model_id`，
      `clone_gate_fingerprint` 是纯 provenance（design D1），并点名 shadow-proof 目的由
      `cloned_from_model_id IS NOT NULL` 独立成立。
- [x] 1.3 `packages/common/state_manager.py:850-894` 的 `get_latest_clone_row_for_model_source`
      **SQL 逐字不改**；在其 docstring 里补一句刻意不跟随的理由（design D2：它回答「刚提交的是哪一行」，
      publisher 自己刚写下该 fingerprint，non-NULL 是一次廉价自检）。
- [x] 1.4 `packages/common/state_manager.py:3662-3721` 的 `_clone_entries_for_model_source` docstring：
      删掉「两面在 fingerprint 轴上分叉」的整段描述（分叉已消除），改为如实记录**收敛后剩下的唯一一条
      分叉**——本面 `.strip()` 并跳过空白串 `cloned_from_model_id`，DB 面 `IS NOT NULL` 接受它
      （design D3），并注明该轴不在本次裁定范围内、已另立 follow-up。
- [x] 1.5 `tests/test_scheduler_lineage.py:594` 的正向文本钉翻成负向钉：
      `assert "clone_gate_fingerprint" not in captured["statement"]`；其余三条断言
      （`ORDER BY valid_time ASC, created_at ASC`、`cloned_from_model_id IS NOT NULL`、
      `cloned_from_model_id <> model_id`）与 `usable_flag` 负向钉保持不变。更新该测试的 docstring
      说明为什么是负向钉。
- [x] 1.6 文件面对称用例（新增，`tests/test_scheduler_lineage.py`）：喂一条
      `cloned_from_model_id` 存在、`clone_gate_fingerprint` 缺失的 index entry，断言
      `clone_lineage_signal` 给出 `has_lineage=True` 且 `t*` 取该行——与 1.1 之后的 DB 面同答案。
- [x] 1.7 DB 面对称用例（新增）：用 `_fetch_optional` monkeypatch 之外的 stub repository 断言
      earliest reader 在「缺 fingerprint 的 clone 行」上**不再落选**（行为断言，与 1.5 的文本形状断言
      互为补充，避免只剩一条文本 oracle）。

## 2. #1740 — 失败不缓存

- [x] 2.1 `services/orchestrator/scheduler_lineage.py` 新增 `LineageResolutionError(Exception)`，
      携带 `model_id` / `source_id` / `reason`，加入 `__all__`。
- [x] 2.2 `resolve_lineage_cutover`：按 design D4 / D4a 的表把「失败」从「无血缘」里分出来——
      accessor 抛异常、`clone_lineage_signal` 返回非 Mapping、或返回 `status == "blocked"` **且 reason
      不是 `state_snapshot_index_missing`** ⇒ 抛 `LineageResolutionError`；`provider is None`、入参为空、
      两个 accessor 都不存在、DB 面 `row is None`、`status == "blocked"` 且 reason 为
      `state_snapshot_index_missing`、`has_lineage=False`、字段不可解析 ⇒ 仍返回 `None`。
      docstring 同步改写（当前那句「A provider that is None, exposes neither accessor, **or fails**,
      yields None」必须改，它正是被裁掉的口径）。
- [x] 2.3 模块 docstring 的 Boundaries 段：「Absent provenance — and an unreadable index or an absent
      provider — mean "no lineage", never an error」这一条必须改——**unreadable index 现在是 error**。
- [x] 2.4 `services/orchestrator/scheduler_core.py:754-763` 的 `_lineage_cutover_for_model_source`：
      catch `LineageResolutionError` ⇒ 调用 `scheduler_lineage` 侧的记录 helper、**不写缓存**、返回
      `None`；成功路径原样写缓存。**logger 与 warn 渲染都住在 `scheduler_lineage.py`**（该模块负责这条
      信号的措辞，scheduler_core 只负责决定「不缓存」）——`scheduler_core.py` 全文没有 logging import，
      且已 1008 行、超 report-only 结构预算阈值（design D6），新增必须压到最小。
- [x] 2.5 可观测信号：warn 至少含 `model_id` / `source_id` / `reason`；不做去重（design D6）。
      测试用 `caplog` 钉在 `scheduler_lineage` 的 logger 上。
- [x] 2.6 回归测试（新增，`tests/test_scheduler_backfill.py`）：DB 面 fake provider 的
      `get_earliest_clone_row_for_model_source` **第一次抛、第二次成功**，断言
      (a) 第一次返回 `None` 且 `_lineage_cutover_cache` 里**没有**该 key，
      (b) 第二次返回正确的 `LineageCutover`，
      (c) `caplog` 里有那条 warn。
- [x] 2.7 db-free 面对称测试（新增）：`clone_lineage_signal` 返回
      `{"status": "blocked", "has_lineage": False, ...}` 且 reason 为**非** `state_snapshot_index_missing`
      时，同样不进缓存、同样打 warn、同样在下一次调用重解析。
- [x] 2.7a D4a 的反向用例（新增）：索引文件**不存在** ⇒ reason `state_snapshot_index_missing` ⇒
      返回 `None`、**进缓存**、**不打 warn**。这条钉的是「从未发布过索引的健康 db-free 部署不会每 pass
      刷 warn」，是 D4a 的核心断言，缺了它 D4a 就只是一句话。
- [x] 2.7b **刻意翻转既有 oracle**：`tests/test_scheduler_lineage.py:249-268` 的
      `test_clone_lineage_signal_unreadable_index_is_no_lineage` 改为 `pytest.raises(
      LineageResolutionError)` 并改名（`..._is_a_resolution_failure`）；同步修
      `tests/test_scheduler_lineage.py:13` 的模块 docstring（"absent / unreadable provenance is 'no
      lineage', never an error" 已过时——`unreadable` 现在是 error，`missing` 仍是 no lineage）。
      这是本 PR 唯一一条被有意打红的既有测试（design D8 末段），不是回归。
- [x] 2.8 不改 `scheduler_runtime.py:872-875` 的 `db_free_required` 闸、不拆
      `_refresh_db_free_file_providers`（design D5）；确认
      `tests/test_scheduler_backfill.py:3017-3025` 与
      `test_db_plane_provider_construction_failure_is_remembered` /
      `test_db_plane_provider_is_constructed_once_and_memoized` 原样绿。

## 3. #1741 — 自克隆 fail-closed 闸

- [x] 3.1 `packages/common/state_clone.py` 新增 scope 常量
      `_SELF_CLONE_TARGET = "self_clone_target"`（与 `_REVERSE_CLONE_TARGET_NOT_DIRECT_GRID`
      同处、同风格，`:154` 附近）。
- [x] 3.2 在 `fingerprint_gated_state_clone` 里、`_build_audit_context(...)` 之后、闸 0 之前插入
      `if m0_model_id == m1_model_id: return _refuse(..., scope=_SELF_CLONE_TARGET)`；逐字比较，
      不做 strip / 大小写折叠（design D7）。附一段说明「守卫为什么住在闸里而不是调用方」的注释。
- [x] 3.3 docstring 计数：`packages/common/state_clone.py` 里写死 "six" 的共 **4 处**，全部要改——
      `:24`（模块 docstring 的 scope 清单，新增该 scope 的条目）、`:55`（"adds a seventh scope"）、
      `:205`（`StateCloneAuditRecorder`）、`:223`（`StateCloneResult`）。`:223` 的「fix-forward scopes +
      recalibration scope」二分结构装不下新 scope（它 mode-无关），按 design D7 改写。
- [x] 3.4 `tests/test_state_clone.py` 新增：`transfer_mode='fix_forward'` 下 `m0_model_id ==
      m1_model_id` ⇒ `refused=True`、`refusal_scope == "self_clone_target"`、
      `repository.upsert_state_snapshot` **零调用**、audit recorder 收到一条 refusal 记录。
- [x] 3.5 同上 `transfer_mode='recalibration'`（含 `m1_recorded_hydrologic_core_fingerprint=None`
      的跳过 cross-check 分支）。
- [x] 3.6 排序测试：喂**合法 direct-grid manifest + 非空 state_schema/solver_config bytes**，断言拿到
      `self_clone_target` 而非 `reverse_clone_target_not_direct_grid` / `degenerate_gate_inputs`
      （design D7——否则这条测试没在测排序）。

## 4. 文档与收尾

- [x] 4.1 live receipt 已落盘：
      `docs/runbooks/receipts/2026-09-15-issue-1739-clone-provenance-count-node27.md`（已完成，
      随本 PR 提交）。
- [ ] 4.2 D3 的空白串跨面分叉立 follow-up issue（report, don't fix）。issue body 已起草并随
      实现报告交回；开单本身留给 PR 作者执行（实现子任务不代为产生外部副作用）。
- [x] 4.3 `openspec validate clone-lineage-admission-predicate-convergence --strict --no-interactive` 通过。

## Evidence Floor

- **EF-1** `uv run pytest tests/test_scheduler_lineage.py -q` 全绿（含 1.5 的负向钉、1.6/1.7 新用例，
  以及 2.7b 刻意翻转后的那条——该文件**需要改动**，不是「除 1.5/1.6/1.7 外不用动」）。
- **EF-2** `uv run pytest tests/test_scheduler_backfill.py -q` 全绿（含 2.6/2.7 新用例，且 2.8 列出的三条
  既有测试未改动而仍绿）。
- **EF-3** `uv run pytest tests/test_state_clone.py tests/test_state_clone_recalibration.py
  tests/test_state_clone_cutover_hook.py -q` 全绿。
- **EF-4** `uv run pytest tests/test_state_manager.py -q` 全绿（DB reader 改动的直接回归面）。
- **EF-5** `uv run ruff check .` 清洁。
- **EF-6** `uv run openspec validate clone-lineage-admission-predicate-convergence --strict
  --no-interactive` 通过。
- **EF-7** node-27 live receipt 已提交且如实标注其弱 oracle 性质（整表为空，不是「多行中零半写」）。
