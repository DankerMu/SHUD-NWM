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
- [x] 1.4 `packages/common/state_manager.py` 的 `_clone_entries_for_model_source` docstring：
      删掉「两面在 fingerprint 轴上分叉」的整段描述（分叉已消除），改为如实记录**收敛后剩下的
      `cloned_from_model_id` 规范化轴上的两种遮蔽形状**（design D3）——(1) 空白串 parent；
      (2) 带空白的自指 parent（SQL 的 `<> model_id` 是逐字比较）——并注明该轴不在本次裁定范围内、
      已另立 follow-up #2392，以及 `btrim(...) <> ''` 只关得掉第一种。
      **round-2 修正**：原文写的「唯一一条分叉」是错的，第二种形状同样存在——**已论证，未实跑**
      （对 SQL 谓词与 `.strip()` 的静态推演；两面都没有为空白填充的行建过用例，本仓无活 PG、
      node-27 表为空、CI 的 real-db 用例也没建这类行）。
- [x] 1.5 `tests/test_scheduler_lineage.py:594` 的正向文本钉翻成负向钉：
      `assert "clone_gate_fingerprint" not in captured["statement"]`；其余三条断言
      （`ORDER BY valid_time ASC, created_at ASC`、`cloned_from_model_id IS NOT NULL`、
      `cloned_from_model_id <> model_id`）与 `usable_flag` 负向钉保持不变。更新该测试的 docstring
      说明为什么是负向钉。
- [x] 1.6 文件面对称用例（新增，`tests/test_scheduler_lineage.py`）：喂一条
      `cloned_from_model_id` 存在、`clone_gate_fingerprint` 缺失的 index entry，断言
      `clone_lineage_signal` 给出 `has_lineage=True` 且 `t*` 取该行——与 1.1 之后的 DB 面同答案。
- [x] 1.7 DB 面 **downstream** 用例（新增，`tests/test_scheduler_lineage.py`
      `test_db_plane_missing_gate_fingerprint_still_resolves_lineage`）：stub repository 交出一行
      `clone_gate_fingerprint=None` 的 clone 行，断言 resolver 仍解析出 `t*`。
      **它证明的是 reader 的下游那一半**——stub 取代了 reader 本身，SQL 一行都没执行，所以它**不是**
      1.1 那条谓词的 oracle。（round-2 修正：原文声称它是「行为断言……避免只剩一条文本 oracle」，
      不成立；真正的谓词 oracle 是 1.8。）
- [x] 1.8 DB 面**真实执行**用例（新增，`tests/test_real_database_integration.py`
      `test_real_clone_row_readers_disagree_about_a_null_fingerprint_row`）：在真实 PostgreSQL 上
      seed 一行 `cloned_from_model_id` 非空、`clone_gate_fingerprint` **NULL** 的 clone 行，断言
      (a) `get_earliest_clone_row_for_model_source` **返回它**（#1739 裁定），
      (b) `get_latest_clone_row_for_model_source` **不返回它**（design D2 的刻意不对称），
      (c) 再补一行**更晚且带 fingerprint** 的 clone 行后，earliest 仍是 NULL-fingerprint 那行、
      latest 是新那行（ASC/DESC 排序），
      (d) 端到端过一次 `resolve_lineage_cutover`。
      落点选 `tests/test_real_*.py` 是为了命中 `.github/workflows/ci.yml` 的 `database` path filter
      （`packages/common/state_manager.py` **不在**该 filter 里），让本 PR 真的触发
      `real-db-integration`（"SQL Migration Dry Run"）。本地无 PG 时按既有 `integration_database_url`
      fixture 干净 SKIP。

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
      返回 `None`、**进缓存**、**不打 warn**。这条钉的是 D4a 的核心断言——「没有索引」与「索引里没有
      这个 pair」是同一个答案。（适用面按 design D4a-1 收窄：db-free-required 生产面上这种部署会先被
      运行时 preflight 挡住，真正可达的是 TOCTOU 与不经 preflight 的嵌入方；resolver 该有的行为不变。）
- [x] 2.7b **刻意翻转既有 oracle**：`tests/test_scheduler_lineage.py:249-268` 的
      `test_clone_lineage_signal_unreadable_index_is_no_lineage` 改为 `pytest.raises(
      LineageResolutionError)` 并改名（`..._is_a_resolution_failure`）；同步修
      `tests/test_scheduler_lineage.py:13` 的模块 docstring（"absent / unreadable provenance is 'no
      lineage', never an error" 已过时——`unreadable` 现在是 error，`missing` 仍是 no lineage）。
      这是本 PR **两条**被有意打红的既有断言之一（design D8 末段），不是回归；另一条是 1.5 翻转的
      `tests/test_scheduler_lineage.py:594` 正向 fingerprint 文本钉——1.1 从 SQL 去掉那句条件即打红它。
- [x] 2.8 不改 `scheduler_runtime.py:872-875` 的 `db_free_required` 闸、不拆
      `_refresh_db_free_file_providers`（design D5）；确认
      `tests/test_scheduler_backfill.py:3017-3025` 与
      `test_db_plane_provider_construction_failure_is_remembered` /
      `test_db_plane_provider_is_constructed_once_and_memoized` 原样绿。
- [x] 2.9 D4 表里两条只在代码里出现过一次的 reason 字面量补钉（新增，`tests/test_scheduler_lineage.py`）：
      鸭子类型 stub 的 `clone_lineage_signal` 抛异常 ⇒ `reason == "clone_lineage_signal_failed"`
      且 `__cause__` 是原异常；返回非 Mapping ⇒
      `reason == "clone_lineage_signal_contract_violation"`。下游链路已由兄弟用例
      （`earliest_clone_row_read_failed`）钉住，这两条补的只是字面量本身。

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
- [x] 4.2 D3 的 `cloned_from_model_id` 归一化轴**两种**遮蔽形状（空白串 parent + 带空白自指 parent）
      立 follow-up issue（report, don't fix）：**#2392**（标题与范围已按 round-2 补充，含
      `btrim(...) <> model_id` 这第二个必需条件——只补 emptiness 那半步会看起来做完了）。
- [x] 4.3 `openspec validate clone-lineage-admission-predicate-convergence --strict --no-interactive` 通过。

## Evidence Floor

- **EF-1** `uv run pytest tests/test_scheduler_lineage.py -q` 全绿（含 1.5 的负向钉、1.6/1.7 新用例，
  以及 2.7b 刻意翻转后的那条——该文件**需要改动**，不是「除 1.5/1.6/1.7 外不用动」）。
- **EF-2** `uv run pytest tests/test_scheduler_backfill.py -q` 全绿（含 2.6/2.7 新用例，且 2.8 列出的三条
  既有测试未改动而仍绿）。
- **EF-3** `uv run pytest tests/test_state_clone.py tests/test_state_clone_recalibration.py
  tests/test_state_clone_cutover_hook.py -q` 全绿。
- **EF-4** DB reader 改动的**可执行** oracle：
  `NHMS_RUN_INTEGRATION=1 NHMS_INTEGRATION_DATABASE_URL=... uv run pytest
  tests/test_real_database_integration.py::test_real_clone_row_readers_disagree_about_a_null_fingerprint_row -q`
  通过（node-27 实机，或 CI 的 `real-db-integration` / "SQL Migration Dry Run" job）。
  本地无 PG 时该用例干净 SKIP，**SKIP 不算满足本项**。
  原 EF-4 写的是 `uv run pytest tests/test_state_manager.py -q`，那是一张**空网**：该文件从未引用
  `get_earliest_clone_row_for_model_source`，revert 掉 1.1 也照样全绿。它仍作为宽泛回归面跑
  （见下方 EF-4a），但不再冒充本次谓词改动的证据。
- **EF-4a** `uv run pytest tests/test_state_manager.py -q` 全绿（宽泛回归面，非本次谓词的 oracle）。
- **EF-5** `uv run ruff check .` 清洁。
- **EF-6** `uv run openspec validate clone-lineage-admission-predicate-convergence --strict
  --no-interactive` 通过。
- **EF-7** node-27 live receipt 已提交且如实标注其弱 oracle 性质（整表为空，不是「多行中零半写」）。
