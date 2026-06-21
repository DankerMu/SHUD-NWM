## Context

`/api/v1/layers` 当前对 `discharge` 层根据 caller 传不传 `run_id` 切换 tile URL 模板（[apps/api/routes/flood_alerts.py:456](apps/api/routes/flood_alerts.py:456)、[services/tiles/mvt.py:740-765](services/tiles/mvt.py:740)、[:856-860](services/tiles/mvt.py:856)；`/api/v1/layers` 由 `_default_layer_catalog`（[apps/api/routes/flood_alerts.py:2278](apps/api/routes/flood_alerts.py:2278)）装配）：

- caller 不传 `run_id` → `national=True` → `national_discharge = national and layer_id == "discharge"` → 国家级模板 `/api/v1/tiles/hydro-national/q_down/{valid_time}/{z}/{x}/{y}.pbf`，每流域 latest run 在 server SQL 内 `DISTINCT ON (river_network_version_id)` 选取，**全 basin 河段同屏**。
- caller 传 `run_id` → `national=False` → discharge 走 `/api/v1/tiles/hydro/{run_id}/q_down/...`，只渲染该 run 所属 basin 的河段。

前端 `loadOverview` 分两阶段：
1. **mapBootstrap**（[apps/frontend/src/stores/overviewData.ts:1240](apps/frontend/src/stores/overviewData.ts:1240)）调 `fetchLayers(null)`，spec scenario *Bootstrap minimal request set* 已固化此契约。
2. **enrichment**（同文件 [:1331](apps/frontend/src/stores/overviewData.ts:1331)、[:1511](apps/frontend/src/stores/overviewData.ts:1511)）调 `fetchLayers(useSingleRunFloodSurfaces ? latestRun?.run_id : null)`，目的是把 `flood-return-period` / `warning-level` 的 `metadata.valid_times` 收紧到 latestRun 的实际有效时间集合。`useSingleRunFloodSurfaces = query.source !== 'compare'` 在默认 `source='best'` 下为 true，于是 enrichment 拿到的 layer 列表把 discharge 模板覆盖成单 run 形式。

结果：默认视图 enrichment 完成后，discharge layer 永远绑死 latestRun 所属 basin，其他 basin 河段在 tile 层根本没下发。node-27 实测 latestRun 是 `fcst_ifs_2026062000_basins_qhh_shud` → 只 qhh 可见、heihe 不可见。

约束：
- 不能动 flood-return-period / warning-level 的 per-run tile URL —— 这两层本质就是 per-run 产物。
- 不能让 mapBootstrap 的 `fetchLayers(null)` 失效或回退，spec 已固化。
- 不能让前端 enrichment 不带 `run_id`：flood 层依赖它取 per-run valid_times（PR #583 metadata-first 改造之后 valid_times 直接来自 layer.metadata，仍需 run_id 路径产出）。
- 直接深链 `/api/v1/tiles/hydro/{run_id}/q_down/...` 的路由仍存在（[apps/api/routes/flood_alerts.py:1059](apps/api/routes/flood_alerts.py:1059)），不能下线，只是公开 catalog 不再暴露。national 路由在 [apps/api/routes/flood_alerts.py:1119](apps/api/routes/flood_alerts.py:1119)，本变更只动 catalog 装配。

## Goals / Non-Goals

**Goals**
1. `/api/v1/layers` 对 `discharge` 层永远返回 national 模板（带或不带 `run_id` 都一样）。
2. flood-return-period / warning-level / river-network 行为完全不变。
3. mapBootstrap + enrichment 两次 `fetchLayers` 拿到的 discharge tile URL 完全一致，避免 enrichment 覆盖 bootstrap 后再次刷新 MapLibre source。
4. 添加 regression unit test 覆盖"带 run_id 调用 catalog → discharge 仍是 national 模板、flood/warning 仍是 per-run 模板"。
5. 在 spec 把"discharge tile URL 始终是 national"固化为不变量。

**Non-Goals**
- 不修改前端代码。
- 不删 single-run hydro tile 路由（`/api/v1/tiles/hydro/{run_id}/q_down/...`），仍是合法直接深链入口。
- 不动 valid_time 计算逻辑（national_discharge_valid_times vs valid_times_for_layer 选择）；现状 `national_discharge` 分支已自动取 national valid_times，本变更不引入新逻辑。
- 不修 `useSingleRunFloodSurfaces` 名字或语义。前端继续按既有方式调用，本变更纯后端兼容方向收紧。
- 不为 heihe basin 翻 `basin_version.active_flag`——active_flag=false 不是阻塞因素（qhh 也是 false 仍然能用），属于 data lifecycle 治理范畴，留 follow-up。

## Decisions

### Decision 1: 后端单点改 `_default_layer_catalog` 里 `national_discharge` 表达式

**Choice**：`apps/api/routes/flood_alerts.py:2302`（位于 `_default_layer_catalog` 函数体内）将 `national_discharge = national and layer_id == "discharge"` 改为 `national_discharge = layer_id == "discharge"`。函数签名 `_default_layer_catalog(session, *, run_id, ..., national: bool = False)` 保持不变（前者仍被 `/api/v1/layers?run_id=...` 路径以 `national=run_id is None` 调用，只是 discharge 不再依赖该布尔）。

**Alternatives considered**：

- **A. 前端 enrichment 拆开**：mapBootstrap `fetchLayers(null)` 留着，enrichment 改成只 fetch flood 层 valid_times（per-layer endpoint），不再覆盖 catalog。代价：前端需要 hooks 出新的"只取 valid_times 不取 catalog"路径，触动 PR #583 metadata-first 收敛后的形状。否决理由：前端面更广、回归风险高，修不在根因点。
- **B. 后端拒收 discharge 的 run_id 参数**：在 `/api/v1/layers?run_id=...` 收到时显式把 discharge 排除出 run-scoped 处理。和 A 类似，代价是 caller 的 run_id 语义被部分否决。否决理由：违反"caller 传什么就用什么"的简单契约，需要额外文档。
- **C. 走 caller 标志 + 前端不变（本决策）**：单点改后端表达式，前端无需感知。**最小爆炸半径**，对 caller 完全向后兼容（带 run_id 仍可用，只是 discharge 模板永远 national）。

**Reason**：根因是 `national_discharge` 表达式错误地把"是否 national"耦合到 caller 的 `national` 标志；正确语义是"discharge 永远 national"。改这一个布尔表达式即解。

### Decision 2: 缓存 / ETag 影响

`layer_metadata` 计算 `version`（用于 ETag）时，`source_refs={}` 当 `national_discharge=True`（[services/tiles/mvt.py:863-873](services/tiles/mvt.py:863)）。带 run_id 调用 catalog 时，discharge 的 ETag 不再依赖 run_id，与不带 run_id 调用的 discharge ETag 一致——这是期望行为：客户端拿到的就是同一个 national tile，缓存键应一致。

Tile 内容随新 run 上线时由 URL 中的 `{valid_time}` 自然换 key（national_discharge_valid_times 会返回新的 valid_time 集合）。**不需要额外缓存清洗**。

### Decision 3: 新增 regression test 形状

放在 `tests/test_flood_alerts_api.py`（已有 `_default_layer_catalog` + `layer_metadata` 测试块），用 FakeSession + FakeRun fixture，断言：

```python
# 1. /api/v1/layers?run_id=<X> → discharge.tile_url_template === hydro-national + maplibre_source_layer == 'hydro' + 'basin_id' in properties
# 2. /api/v1/layers?run_id=<X> → flood-return-period.tile_url_template 仍含 {run_id} 占位符；river-network 模板严格相等
# 3. 同时跑 runless + run-scoped → discharge.metadata.source_refs == {} + metadata.version 字节相同（cache identity）
```

不引入 real-DB 依赖；逻辑校验已足够。real-DB integration 留作 follow-up（见 [#598](https://github.com/DankerMu/SHUD-NWM/issues/598)）。

### Decision 4: spec 增量位置

放在 `openspec/specs/overview-data-contracts/spec.md`：

- 这是 default `best+discharge` overview 已有的 owner spec（已有 *Bootstrap minimal request set* / *Default discharge run selection* 等相邻 requirement）；新增 *Default discharge tile URL is national* requirement 紧贴邻居，**避免 spec 分裂**。
- 不在 `mvt-tile-contract` 加，因为那是 layer-agnostic 的瓦片端点契约，加 discharge-specific 条款会破坏 generality。
- 不在 `frontend-mvt-layer-consumption` 加，因为修复是后端单点，**前端契约不变**。

## Risks / Trade-offs

| 风险 | 缓解 |
|---|---|
| 直接深链 `/api/v1/layers?run_id=<X>` 历史调用方期望 discharge 是单 run 模板 | 历史 caller 只有前端 enrichment（已知）；外部脚本若依赖该形状属于不被支持的私下契约。changelog 显式说明；提供 hydro single-run 深链路由 [`/api/v1/tiles/hydro/{run_id}/...`](apps/api/routes/flood_alerts.py:1059) 仍可用。 |
| Cache key 改变（discharge ETag 不再依赖 run_id）→ 已部署客户端可能拿旧缓存 | ETag 变化即触发条件请求；前端 CDN 不持久缓存 `/api/v1/layers` 响应（display_catalog_cached TTL ~14s）；node-27 部署后 14s 内全清。 |
| `display_catalog_cached` 路由级缓存 key 仍含 `run_id`（`f"layers:{run_id}:{limit}:{offset}"`，[apps/api/routes/flood_alerts.py:463](apps/api/routes/flood_alerts.py:463)）→ 同 14s 窗口内 N 个不同 run_id 调用产 N 份缓存；discharge 部分内容相同但 flood/warning 部分仍按 run_id 切分 | 期望行为，不改：route-level cache 必须按 run_id 切分以保 flood/warning per-run 正确；discharge 部分内容相同但占用空间小（per-layer ~kB 级），TTL ~14s 自然回收。spec scenario "Discharge catalog cache identity is run-agnostic" 显式说明 per-layer ETag 才是绑定契约。 |
| 仍存在 `basin_version.active_flag=false` 隐患（heihe + qhh 都 false 但 qhh 能用 = 该字段当前在 tile 路径里其实没起 gate 作用）| 本变更不动 active_flag 语义；data lifecycle drift 单独 follow-up。 |
| flood-return-period 单测被本变更顺带触动的可能 | regression test 显式断言 flood / warning template 含 `{run_id}` 占位符，CI 直接抓回归。 |

## Migration Plan

1. 本地 + CI 跑新 regression test → green。
2. PR merge 后，node-27 `git pull --ff-only` → restart uvicorn → curl 双侧验证（带/不带 run_id 调用 discharge tile URL 均为 hydro-national）。
3. 浏览器实拍 receipt：放大到 heihe basin → 河段可见 + 单段可点击调出曲线。
4. 失败回滚：单行改动，直接 `git revert <sha>` + 重启。

## Open Questions

- **None blocking**。`useSingleRunFloodSurfaces` 这个 flag 名是否值得在后续 cleanup PR 里改成更准确的 `useSingleRunFloodSurfacesOnly` 或拆成 `floodSurfacesScopedToLatestRun` —— 留作命名 follow-up，不在本变更内。
