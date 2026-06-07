# M26 node-27 display_readonly live receipt（EPIC #336 收尾）

- **节点/角色**：node-27 `nwm@210.77.77.27:32099`，`/home/nwm/NWM`，`display_readonly`。
- **代码**：master HEAD=`2f79baf`（含 #337–#341 全部 M26 前端改动）；node-27 `git pull --ff-only` 同步（pull 前 `git status --porcelain` 仅 untracked `.python-version`，master 不跟踪该文件，ff 安全）。
- **构建**：node-27 `corepack pnpm build` 重建 `apps/frontend/dist`（✓ built 16.07s，hash 与本地一致 vendor-map `D4r275RI`）。
- **服务**：node-27 既有本地实例 `.venv/bin/python -m uvicorn apps.api.main:app --host 127.0.0.1 --port 8080`（cwd=/home/nwm/NWM），FastAPI StaticFiles 按请求读盘 → 重建后即服务新 dist。**未** `docker compose up`（dev-phase 本地实例，符合 deploy gate）。
- **隧道**：本地 `ssh -L 8080:127.0.0.1:8080`；本地 `agent-browser --headed`（headless 无 WebGL，spec 要求 headed 验真实渲染）。
- **execution_mode**：`live_proof`（真实 display_readonly 服务 + 真实 DB 副本数据 + 真实浏览器渲染）。

## 平面身份（live）
`GET /api/v1/runtime/config` → `service_role=display_readonly`、`control_mutations_enabled=false`、`slurm_routes_enabled=false`、`queue_depth_mode=display_readonly_unavailable`。✅ 只读展示平面。

## 数据就绪（live，真实 DB 副本）
- `GET /api/v1/mvp/qhh/latest-product?source=GFS` → **ready**：basins_qhh / run `fcst_gfs_2026060100_basins_qhh_shud` / station_count 386 / segment_count 1633 / run_status frequency_done。
- `GET /api/v1/met/stations?model_id=basins_qhh_shud&basin_version_id=basins_qhh_vbasins` → total_count **386**，真实坐标（qhh_forc_001 @ 100.95°E, 36.25°N）。
- `GET /api/v1/basin-versions/basins_qhh_vbasins/river-segments` → 真实 FeatureCollection（QHH 河段，带 source_sha256 溯源）。

## Evidence Floor（§6.3 六项）逐项

| # | 项 | 结果 | 证据 |
|---|---|---|---|
| ① | 旧路由重定向到单页 `/`（语义参数 + 原始 search 保留） | ✅ **PASS（live-only）** | 7/7：`/hydro-met?source=GFS`→`/`；`/overview`→`/`；`/forecast`→`/`；`/meteorology`→`/?layer=met-stations`；`/flood-alerts`→`/?layer=flood-return-period`；`/basins/basins_qhh`→`/?basinId=basins_qhh`；`/segments/seg-001`→`/?segmentId=seg-001` |
| ② | 全屏地图无顶部导航 | ✅ **PASS** | `01-overview-fullscreen.png`：全屏底图（headed 真实 WebGL 渲染中国地图 + 地形/矢量瓦片 + 地名），无顶部导航栏；面板浮于地图 |
| ③ | basinId 切换 QHH↔Heihe 同页 | ✅ **PASS** | `02-basin-detail-heihe.png` / `04-qhh-detail-rivernet.png`：URL `?basinId=basins_heihe`/`?basinId=basins_qhh`，左侧切换为「流域详情」面板，**pathname 恒 `/`**（无路由跳转）；地图 dblclick 缩放交互生效（`05-qhh-zoomed.png`） |
| ④ | 气象代站图层 toggle + 点代站 popup 六要素 | ⚠️ **部分（数据 live 就绪 + 本地测试全覆盖；live 点击延后）** | 图层 toggle ✅（按钮「气象代站 点位代站聚合图层 / clustered GeoJSON」可切，`06-met-stations-clusters.png` 底部「图层 met-stations」）；代站数据 live 就绪（386 真实站点）。popup 六要素**绘制逻辑**由本地直测全覆盖（`stationSeries.test` 11 + `M11StationForcingPopup.test` 4 护栏，全绿于 2f79baf）。见下「部分项说明」 |
| ⑤ | 点河段 popup q_down 真实曲线 + 重现期状态 | ⚠️ **部分（同上）** | discharge 图层 ✅ 可选；river-segments + forecast-series 数据 live 就绪；popup q_down 曲线 + RP 三态 + ok:false 不画**绘制逻辑**由 `riverForecast.test` 5 + `M11RiverForecastPopup.test` 5 全覆盖（全绿于 2f79baf）。见下 |
| ⑥ | overlay 未注册如实显示「未注册」不伪造 | ✅ **PASS（核心诚实展示）** | 4 个 MVT 图层按钮全显示 **「Layer is not registered by the API.」**（河段径流/河段水位/洪水重现期/预警等级），`01`/`04` 右侧「当前图层 Discharge: Layer is not registered by the API」；从不伪造瓦片 |

**附加 live 确认**：EPIC 承诺的 3 类图层均present为可切换控件——河段流量 q_down(discharge)、洪水重现期(flood-return-period)、气象代站(clustered GeoJSON)。

## 部分项说明（④⑤ live 点击未入镜的诚实原因）
1. **`/api/v1/basins` 不暴露 bbox**（仅 basin_id/name/group/description/created_at）→ 选中流域时地图**无 bbox 可 fit**，诚实回退到全国尺度而**不伪造 zoom**（honest-display 正确：无数据不造视图）。需手动 dblclick 缩放才能逼近流域要素。
2. **CLI 合成事件难精确命中 WebGL canvas 上的单个要素**（386 站簇 / 1633 河段线），无法稳定点中触发 popup。这是**环境/工具链限制 + 后端 bbox 暴露缺口**，**非前端 popup 缺陷**。
3. popup 的诚实展示不变量（真实曲线 / ok:false 不画 / 严格身份 / RP 三态 / 缺 unit·坏 metadata·任一无效点不画 / product=null 诚实空态）在 `2f79baf` 上由**本地单测全绿覆盖**（lib 金标准直测 + 组件护栏），数据 live 就绪亦已 API 证实。
4. **后续闭合建议**：①后端 `/api/v1/basins` 或 overview 端点暴露 basin bbox（使地图自动 framing，#338 已就地化但依赖该数据）；② MVT overlay 424/409 已由 **#343** 跟踪；待 overlay 注册后 ④⑤ 可在自动 framing 下补 live 点击截图。该 bbox 缺口可并入 #343 或另开 ops issue。

## 裁决
EPIC #336 收尾 live 验证：**①②③⑥ + 平面身份 + 数据就绪 = live-PASS**（其中 ① 重定向矩阵、⑥ 未注册诚实态、平面只读身份均为本地 vitest **无法**验证、仅 live 可证之项，构成本 receipt 的核心增量价值）。④⑤ 的 popup **绘制不变量**由本地测试全覆盖、数据 live 就绪，仅 live 点击截图因 bbox/CLI 限制延后（归 #343/后续）。无伪造 PASS。

截图：`worklogs/node27-receipt/01..06-*.png`。
