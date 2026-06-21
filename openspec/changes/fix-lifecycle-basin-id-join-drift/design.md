## Context

PR [#596](https://github.com/DankerMu/SHUD-NWM/pull/596) closed `list_models` and `get_model_internal` JOIN drift but issue [#599](https://github.com/DankerMu/SHUD-NWM/issues/599) carries two sibling methods on the lifecycle path:

- `_fetch_active_model_for_scope` at [packages/common/model_registry.py:1929-1953](packages/common/model_registry.py:1929) — `SELECT mi.* FROM core.model_instance mi`, no JOIN.
- `_update_model_lifecycle_state` at [packages/common/model_registry.py:2289-2305](packages/common/model_registry.py:2289) — `UPDATE ... RETURNING *`, no JOIN.

Both feed `_model_public_projection` for `POST /api/v1/models/{id}/lifecycle`. The OpenAPI [openapi/nhms.v1.yaml:2406-2412](openapi/nhms.v1.yaml:2406) declares `basin_id` on `ModelInstance` (nullable), so the wire passes validation but the frontend admin UI ([apps/frontend/src/stores/modelAssets.ts:642,654-655](apps/frontend/src/stores/modelAssets.ts:642)) sees empty values.

`_fetch_model_lifecycle_row` at [packages/common/model_registry.py:1897-1927](packages/common/model_registry.py:1897) is the canonical JOIN shape (basin_version + basin + river_network_version + mesh_version). Goal: bring the two siblings to the same shape so all lifecycle-row producers are uniform.

## Goals / Non-Goals

**Goals**
1. Populate `basin_id`/`basin_name` for both `_fetch_active_model_for_scope` and `_update_model_lifecycle_state` outputs, matching `_fetch_model_lifecycle_row` shape.
2. Add regression tests asserting SQL contains expected JOINs + projection (mirror PR #596 test pattern).
3. Anchor the invariant in `model-activation-deactivation` spec so future SQL drift fails loudly via test.

**Non-Goals**
- 不修改 `_fetch_model_lifecycle_row`（已是 canonical 形态）。
- 不改 OpenAPI `ModelInstance` schema（已 nullable，仅填充行为）。
- 不修改 `_model_public_projection`（pass-through，无需变化）。
- 不动 frontend `modelAssets.ts`（消费已正确，缺的是 backend 填充）。
- 不引入 real-DB pytest 依赖（mock-cursor unit test 足以覆盖 SQL drift class，与 PR #596 模式一致；real-DB integration test 留给 issue #598）。

## Decisions

### Decision 1: 用 CTE 包 `_update_model_lifecycle_state` 而非 follow-up re-fetch

**Choice**: 把 `UPDATE ... RETURNING *` 包在 `WITH updated AS (...)` CTE 里，外层 `SELECT u.*, b.basin_id, b.basin_name, ...` 完成 JOIN。

**Alternatives**:
- **A. 跟 issue (b) option：UPDATE 后单独再 `_fetch_model_lifecycle_row`**：会 +1 round-trip。CTE 单语句单 round-trip，audit-insert 路径依赖 `updated` row 形状立即可用，CTE 让 row 形状与 `_fetch_model_lifecycle_row` 在单语句内对齐更干净；同时避免后续 SELECT 步骤造成事务边界混乱。否决。
- **B. UPDATE 后用 `_model_public_projection` 补字段**：projection 是 pass-through 不能 query SQL，唯一选项是再触发 SQL，等价于 A。否决。
- **C. 添加 trigger 在 model_instance UPDATE 时自动 propagate basin_id**：跨越关系数据库正常模式，破坏 normalization；basin_id 是 dimension 数据通过 JOIN 推导，不应物化到 model_instance。否决。

### Decision 2: 测试只 mock cursor 不引 real-DB

**Choice**: 镜像 PR #596 在 [tests/test_model_registration.py:3660](tests/test_model_registration.py:3660) 的 `test_list_models_exposes_basin_id_and_basin_name_from_join` 模式，mock cursor 注入 fake row，断言 cursor.execute(SQL) 含 `JOIN core.basin_version` + `JOIN core.basin` + `b.basin_id` + `b.basin_name`。

**Alternatives**:
- **Real-DB integration test**：本 change 不引（与 issue #598 重合，#598 是专项 real-DB pytest fixture 工作）。
- **Snapshot test of `_model_public_projection` output**：projection 没改，无须 snapshot；改的是输入。

### Decision 3: spec 位置 — 加 ADDED requirement 而非 modify 现有

**Choice**: 在 `model-activation-deactivation` spec 加新 ADDED requirement *Lifecycle response basin scoping consistency*，独立 scenario *Lifecycle response basin_id consistency across internal SQL paths*。

**理由**: 现有 requirements 描述 lifecycle 状态机和并发；basin_id consistency 是 *response contract* 维度的新约束，应该独立 requirement 而非塞进 lifecycle-state requirement，避免混淆 spec 语义层级。

## Risks / Trade-offs

| 风险 | 缓解 |
|---|---|
| CTE 在 psycopg2 driver / TimescaleDB 行为差异 | 项目其他地方已有 CTE 用法（grep `WITH .* AS (` packages/）；CTE 是标准 SQL 行为，TimescaleDB 不影响。 |
| `UPDATE ... RETURNING` + CTE 在同事务内重复扫描 | 单语句执行计划，PG 优化器会复用 row；compute cost 实测可忽略（lifecycle 转换非热路径）。 |
| 测试只断 SQL 文本，可能漏掉 driver-level 字段 mapping bug | mock-cursor 是 PR #596 已采纳模式；本 change 不引 real-DB 是因 #598 专项跟踪；如未来 real-DB 真出 bug 由 #598 covers。 |
| SQL 重写引入 lock 行为变化（`_fetch_active_model_for_scope` 的 `FOR UPDATE` clause） | 保留 `lock_clause` 在 SELECT 末尾，**严格 mirror `_fetch_model_lifecycle_row` 形态（不加 `OF mi`）**。理由：(1) 所有现存 `_fetch_active_model_for_scope` callers 都用 `for_update=False`（grep 验证），lock_clause 是空字符串路径；(2) canonical `_fetch_model_lifecycle_row` 在 `for_update=True` 调用 (line 1522) 下已使用相同形态多年无 lock 升级事故，沿用同一形态保证 parity，避免本 PR 引入与 canonical 不同的锁定语义；(3) 如未来真出现 lock contention，应作为独立 follow-up 同时修两个方法（保持对称）。 |

## Migration Plan

1. 本地 ruff + pytest（新单测 + 既有相关）通过
2. PR merge 后无需 node-27 deploy（pure backend SQL refactor，wire contract 不变）
3. 回滚：单文件 SQL 改动 + 测试，直接 git revert
