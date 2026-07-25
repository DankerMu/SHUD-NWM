# Tasks — pin-env-override-state-lineage-blocks (#1109)

Fixture level: compact（S 规模：单测试文件 ~60 行 fixture edit + 两处
docstring + 1 条 ADDED requirement）。
Risk triage：test-only、无生产面；主要风险 (a) reshape 后仍未到达承诺分
支而在另一层短路（缓解：断言钉 typed reason + decision 双值，错分支即
红）；(b) 假绿——reshaped 守卫测试天然 post-change 绿，需 mutation
probe 证明其对上游分支变更有判别力；(c) 违反 single-value pin invariant
（R1-A4：no OR-set）；(d) 越界改动 `services/orchestrator/`（issue 明令
禁止）。Must-preserve：peer
`test_env_override_does_not_admit_declaration_less_cutover` 逐字不动；
整文件全绿；`grep -n "in {" tests/test_scheduler_generation.py` 行数不
增。Seams under test：§8 gate 的 predecessor-lookup 与
generation-match 分支在 env=false 下的 block 行为。Not-selected packs：
concurrency、migration、schema（无此类面）。

## 1. Implementation

- [x] 1.1 T15(b) reshape（`tests/test_scheduler_generation.py:1119-1252`
      `test_env_override_does_not_admit_missing_predecessor`）。机理更
      正（评审 P2）：现 fixture 的 declaration 其实 **in-window**（past
      tolerance 24h，`effective=00Z` vs `now=18Z`），stale 分类来自
      declaration `generation` 字段失配——"调 window"是空操作。正确杠
      杆：承诺 reason 是 `scheduler_generation_gate.py:464` 的兜底，成
      立需 (i) provider `strict_warm_start_evidence.reason ==
      "state_snapshot_index_exact_checkpoint_missing"`，(ii)
      `history_exists=True`。播种：用
      `_write_db_free_state_index_fixture` 布一条 **current-generation**
      条目，坐标写死——`valid_time="2026-05-21T00:00:00Z"`（**严格早于**
      候选 cycle 12Z，且不与候选同 valid_time，否则落 lineage-mismatch
      类 reason）、`model_package_checksum` == 候选
      `resource_profile.package_checksum`；expected predecessor key 上
      无条目。declaration 保持可加载、`effective_cycle_utc=
      2026-05-21T00:00:00Z`（**勿用 12Z**：等于候选 cycle 时若播种失败
      会 COLD_DECLARED_CUTOVER admit 触发 pytest.fail）。断言改为
      `blocked[0].reason ==
      "state_snapshot_index_prior_checkpoint_missing_after_history"` 且
      decision == `TransitionDecision.BLOCK_PREDECESSOR_PENDING`（单值
      钉）。docstring 重写，删除 drift 备注。
- [x] 1.2 T15(c) reshape（`:1258-1370`
      `test_env_override_does_not_admit_wrong_generation_checkpoint`）。
      **expected predecessor key 坐标更正**（评审 P1）：key =
      `(valid_time=候选 cycle_time, cycle_id=cycle-12h, lead_hours)`，
      即 `valid_time="2026-05-21T12:00:00Z"`,
      `cycle_id="gfs_2026052100"`, `lead_hours=12`
      （`packages/common/state_manager.py:1382-1391`）。**主路线取 (e)
      分支**（评审推荐低风险路径）：在该 key 上布一条 **OLD
      generation** 条目（`_old_generation_state_entry`，显式传坐标），
      并**另布一条 current-generation 条目**（`valid_time <
      2026-05-21T12:00:00Z`、非 expected key、checksum == 候选）使
      `exists_current_generation=True`——此时 declaration 绑定不参与判
      定，declaration 仅需可加载 + in-window。fallback（仅当 (e) 实测
      不可达才用，需报偏离）：(d) 分支全套 checksum 绑定——OLD 条目
      `old_package_checksum="a"*64`（必须 hex64）、declaration
      `old_checksum="a"*64` / `new_checksum="b"*64` /
      `generation=derive_generation("b"*64)`、模型
      `resource_profile.package_checksum="b"*64`。断言改为
      `blocked[0].reason == "state_snapshot_index_generation_mismatch"`
      且 decision == `TransitionDecision.BLOCK_WRONG_GENERATION`。
      docstring 同步重写。
- [x] 1.3 参照 pattern（只读，评审 P3 更正）：播种 helper 在
      `tests/test_production_scheduler.py:20134`
      （`_old_generation_state_entry`）与 `:20175`
      （`_write_db_free_state_index_fixture`）；注意 `:627-679` 的
      wrong-generation 测试是合成 `_HistorySignal` **直调**
      `evaluate_transition_decision`、不做 state-index 播种，只可参照
      其断言风格，不可照搬 fixture 结构。peer `:531-619` 逐字不动。
      构造提示（评审确认轮）：`publish_state_snapshot_index` 默认
      `verify_objects=True`，显式 entries 必须指向真实对象且 checksum
      匹配——最省事的构造是复用 `_old_generation_state_entry(roots,
      old_package_checksum=..., state_id=..., valid_time=...,
      cycle_id=..., lead_hours=12)`（自带对象写入；传候选 checksum 即
      为 current-generation 条目）；`_write_db_free_state_index_fixture
      (entries=None)` 的默认条目恰落在 expected key 上，T15(b) 不可直
      接用默认。

## 2. Tests (requirement-driven)

- [x] 2.1 两个 reshaped 测试通过：`uv run pytest -q
      tests/test_scheduler_generation.py::test_env_override_does_not_admit_missing_predecessor
      tests/test_scheduler_generation.py::test_env_override_does_not_admit_wrong_generation_checkpoint`。
- [x] 2.2 判别力证明（mutation probe，隔离 scratch 副本、逐字节还原；
      站点具名——评审 P2：该 reason 有三处来源，只改一处可能留下另一
      半断言仍绿的假证明）：(a) mutate
      `services/orchestrator/scheduler_generation_gate.py:464` 的兜底
      reason 字面量 → T15(b) 必红；(b) mutate 其
      `BLOCK_PREDECESSOR_PENDING` decision 映射 → T15(b) 必红；(c)
      mutate `services/orchestrator/scheduler_generation.py` 中 (d)/(e)
      两处 `BLOCK_WRONG_GENERATION` 返回（至少覆盖 (e) 站点）→ T15(c)
      必红。工作树本体禁止改动生产代码。
- [x] 2.3 整文件回归：`uv run pytest -q
      tests/test_scheduler_generation.py` 全绿；peer 测试零改动零回归。
- [x] 2.4 invariant 守卫（评审 P3 命令修正）：
      `grep -c "in {" tests/test_scheduler_generation.py` 与
      `git show origin/master:tests/test_scheduler_generation.py |
      grep -c "in {"` 计数相同（single-value pin 保持，无新增
      OR-set）。两个 reshaped 断言均为单值等式。

## 3. Verification (issue 验收标准)

- [x] 3.1 `openspec validate pin-env-override-state-lineage-blocks
      --strict --no-interactive` 通过。
- [x] 3.2 `uv run pytest -q tests/test_scheduler_generation.py` 全绿
      （附计数）。
- [x] 3.3 `uv run ruff check .` 通过。
- [x] 3.4 scope 核查：`git diff --name-only origin/master...HEAD` 仅
      `tests/test_scheduler_generation.py` + 本 openspec change。
