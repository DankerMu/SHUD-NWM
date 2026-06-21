## Context

PR [#602](https://github.com/DankerMu/SHUD-NWM/pull/602) made discharge always national in `/api/v1/layers`. Downstream consequence at [services/tiles/mvt.py:863-873](services/tiles/mvt.py:863):

```python
source_refs = (
    {}
    if national_discharge
    else _layer_source_refs(...)
)
```

`national_discharge = layer_id == "discharge"` (post-#602), so the `else` branch is unreachable for `layer_id == "discharge"`. But `_layer_source_refs` body at [services/tiles/mvt.py:975](services/tiles/mvt.py:975) still has:

```python
"run_id": run_id if layer_id != "river-network" else None,
```

— a generic non-river-network rule that *would* include run_id for discharge if a future refactor reintroduces the path. PR #602 spec invariant *Discharge catalog cache identity is run-agnostic* (`source_refs == {}`) depends on the short-circuit; nothing at `_layer_source_refs` boundary enforces it.

Separately, [docs/spec/04_api_design.md:94-104](docs/spec/04_api_design.md:94) (dated 2026-05-06) lists historical tile endpoints without the post-#602 disposition (hydro-national canonical, `/hydro/{run_id}/...` deeplink-only). Source of truth lives in `openspec/specs/mvt-tile-contract/spec.md`; the historical doc needs reconciliation.

## Goals / Non-Goals

**Goals**
1. Make the "discharge never reaches `_layer_source_refs`" invariant testable + loud — entry assertion + unit test.
2. Reconcile `docs/spec/04_api_design.md` with current canonical endpoints.
3. Surface the invariant in `mvt-tile-contract` spec (new scenario under existing requirement) so the assertion is anchored.

**Non-Goals**
- 不修改 `_layer_source_refs` 函数体的 `layer_id != "river-network"` 逻辑（对其他 layer 仍正确：flood-return-period / warning-level 都要 run_id）。
- 不删除 `_layer_source_refs` 函数本身——它对 flood / warning / river-network 仍有用。
- 不改 OpenAPI schema 或公开契约。
- 不动 node-27 任何运行配置。
- 不触碰 `/api/v1/tiles/hydro/{run_id}/...` 直接深链路由实现。

## Decisions

### Decision 1: 选择 assertion 而非 dead-branch removal

**Choice**: 在 `_layer_source_refs` 入口加 `assert layer_id != "discharge"`，带一行 message 引用 spec invariant + 短路点。

**Alternatives**:
- **A. 删 `layer_id != "river-network"` 中 discharge 路径**：但该三元运算符不是 discharge-specific，是 "any layer that isn't river-network gets run_id"。删它会破 flood / warning。否决。
- **B. 用 `raise ValueError(...)` 而非 `assert`**：`assert` 在 production `-O` flag 下被剥除，但项目其它地方一致用 `assert` 处理 invariant guard（grep `assert.*invariant` 验证）；保持一致。同时 assertion 比 ValueError 更便于 reviewer 一眼看出"这是 invariant 不是用户输入校验"。
- **C. 加 type hint `Literal["river-network", "flood-return-period", "warning-level"]`**：mypy 不严格，runtime 不报；不如 assertion 直接。

### Decision 2: spec scenario 位置

放在已有的 `mvt-tile-contract` *MVT tile API contract* requirement 下，作为新 scenario *Discharge layer never reaches `_layer_source_refs`*。理由：紧贴 PR #602 引入的 *Discharge canonical URL is national across all callers* scenario，形成"canonical URL 不变量 + 函数边界 assertion 闭环"的连贯叙事。

### Decision 3: 测试位置 — 选 `tests/test_flood_alerts_api.py` 或新建?

`_layer_source_refs` 是 `services/tiles/mvt.py` 内部函数；既有测试覆盖：
```bash
grep -l "_layer_source_refs\|layer_source_refs" tests/
```
若已有覆盖，直接邻近加；否则放 `tests/test_flood_alerts_api.py`（既有 layers catalog 测试块）。Stage 1 由 implementer 决定，本设计不强制。

## Risks / Trade-offs

| 风险 | 缓解 |
|---|---|
| `assert` 在 `python -O` 下被剥除 | 项目不在 production 用 `-O`（无证据用此模式），且 invariant 由 spec + 单测双重保护；assertion 主要作"开发期/CI 期 fail-loud"用。 |
| `docs/spec/04_api_design.md` 是 8 周前 stale doc, 后续可能整篇 redoo | 本变更只动 hydro tile endpoint 列表的两行；不重写文档；未来整篇 redo 时按 openspec/specs/ 为 source of truth 同步即可。 |
| 引入测试的 fixture 冗余 | 测试只 `import _layer_source_refs` 直接调；无 SQL/session fixture 需求；体量 ≤10 行。 |

## Migration Plan

1. 本地 ruff + pytest（新单测 + 既有相关）通过
2. PR merge 后无需 node-27 deploy（pure refactor，无 runtime 行为变化）
3. 回滚：单行 assertion + 文档恢复，直接 git revert
