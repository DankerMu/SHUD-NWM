# 06. 前端 GIS 设计

版本：v0.1  
日期：2026-04-30

## 1. 技术栈建议

```text
地图：MapLibre GL JS
曲线：ECharts 或 Plotly
状态管理：Pinia/Zustand/Redux 任选
前端框架：Vue 3 或 React
瓦片：Vector tiles + Raster/COG tiles
API：REST JSON
```

## 2. 页面布局

```text
顶部：系统名称、当前时间、起报周期、资料源状态
左侧：图层树、流域选择、scenario 选择
中间：全国地图
右侧：河段/站点详情面板
底部：时间轴、播放控制、图例
```

## 3. 底图

必须支持三种切换：地形、影像、矢量。

## 4. 图层分类

### 4.1 边界图层

- 一级流域边界。
- 二级流域边界。
- 当前启用 basin_version。
- 历史 basin_version，可选。

### 4.2 气象图层

- 降雨格点。
- 温度格点。
- 气象代站。
- best_available 气象产品。

### 4.3 水文图层

- 河段径流 `q_down`。
- 河段水位 `stage`。
- 洪水重现期。
- 预警等级。

## 5. 时间轴逻辑

前端不能生成固定小时序列。每个图层加载后调用：

```http
GET /api/v1/layers/{layer_id}/valid-times
```

并使用返回的 `valid_times[]` 控制时间轴。切换图层时，时间轴自动切换到新图层的有效时间列表。

## 6. Scenario 控件

至少支持：GFS、IFS、GFS + IFS 对比、Best Available。

## 7. 河段交互

### 7.1 hover

- 高亮河段。
- 显示简要信息：流域、河段 ID、当前 Q、当前 T 年一遇等级。

### 7.2 click

- 打开右侧详情面板。
- 请求 `/river-segments/{segment_id}/forecast-series`。
- 显示过去 7 天 analysis + 未来 7 天 GFS/IFS。
- 同图显示 Q2/Q5/Q10/Q20/Q50/Q100 阈值线。

## 8. 气象代站交互

点击气象代站后，请求：

```http
GET /api/v1/met/stations/{station_id}/series
```

展示：PRCP、TEMP、RH、wind、Rn、Press。

## 9. 图例设计

### 9.1 径流图层

可按绝对流量分级或按流域内分位数辅助配色。全国默认不建议仅按绝对 Q 配色，因为大江大河会压制中小流域变化。

### 9.2 重现期图层

```text
< 2 年       常态
2–5 年       偏高
5–10 年      关注
10–20 年     警戒
20–50 年     高风险
50–100 年    严重
> 100 年     极端
```

## 10. 性能要求

| 操作 | P95 目标 |
|---|---|
| 全国初始地图加载 | < 5 秒 |
| 图层切换 | < 2 秒 |
| 时间步切换 | < 1 秒，瓦片缓存命中 |
| 河段点击曲线 | < 2 秒 |
| 气象站点曲线 | < 2 秒 |

## 11. 前端状态模型

```typescript
interface MapState {
  activeBaseMap: 'terrain' | 'imagery' | 'vector';
  activeLayerId: string;
  activeScenarioIds: string[];
  activeValidTime: string;
  validTimes: string[];
  selectedBasinVersionId?: string;
  selectedSegmentId?: string;
  selectedStationId?: string;
}
```

## 12. 用户提示

前端必须清楚标注：当前资料源、当前起报周期、当前有效时间、Analysis/Forecast 分界线、IFS 06/18 周期是否不足 7 天、best_available 的实际来源。

## 13. 洪水预警总览页面

### 13.1 页面描述

独立的预警总览视图，以全国地图为主体，河段按重现期等级分级着色（配色方案同 9.2 节）。该页面聚焦于当前及未来预报时段内超警河段的全局态势感知。

### 13.2 顶部预警信息条

滚动显示当前超警河段摘要，每条包含：

- 河段名称 / 所属流域。
- 预警等级（如 "警戒""高风险""严重""极端"）。
- 当前预报流量 Q（m³/s）。
- 对应重现期（如 T=25 年一遇）。

信息条自动轮播，点击任意条目可跳转至该河段详情。

### 13.3 左侧预警统计面板

按等级分组统计当前时刻超警河段数量：

```text
常态      (< 2 年)      ——条
偏高      (2–5 年)      ——条
关注      (5–10 年)     ——条
警戒      (10–20 年)    ——条
高风险    (20–50 年)    ——条
严重      (50–100 年)   ——条
极端      (> 100 年)    ——条
```

点击任意等级行，地图自动筛选并高亮该等级河段。

### 13.4 右侧 TOP 排名面板

按重现期严重程度降序排名的前 N 条河段列表（默认 N=20），列包括：

- 排名。
- 河段名称 / segment_id。
- 所属流域。
- 当前预报 Q。
- 重现期 T 值。
- 预警等级标签。

点击列表行跳转至河段预报曲线详情面板。

### 13.5 地图交互

- 河段按重现期等级着色，配色同 9.2 节。
- hover 高亮河段并 tooltip 显示河段名、Q、T 值。
- click 打开右侧详情面板，请求 `/river-segments/{segment_id}/forecast-series`，展示预报曲线及重现期阈值线。

### 13.6 时间控制

支持查看不同预报时刻的预警快照：

- 时间轴列出当前预报周期内所有有效时刻。
- 切换时刻时，地图着色、统计面板、TOP 排名同步刷新。
- 支持播放模式，自动逐时刻推进。

## 14. 流域与模型资产管理页面

### 14.1 页面描述

管理流域版本、河网版本、mesh 版本、率定版本、SHUD 代码版本。面向模型管理员（version_admin 及以上角色），提供模型资产的浏览、版本切换和对比能力。

### 14.2 左侧导航：流域列表树

- 按一级流域 → 二级流域 → 模型实例三级树结构组织。
- 支持搜索和筛选（按流域名称、basin_version_id）。
- 当前启用的模型实例以高亮标识。

### 14.3 中间区域：模型实例详情卡片

选中某个模型实例后，展示以下属性：

```text
basin_version_id
river_network_version_id
mesh_version_id
calibration_version_id
shud_code_version
河段数（segment_count）
节点数（node_count）
流域面积（area_km2）
active_flag
```

active_flag 为 true 时以绿色标识，表示当前用于业务预报的版本。

### 14.4 右侧区域：版本时间线

展示该流域的模型版本演进历史：

- 纵向时间线，每个节点代表一个模型版本。
- 节点标注版本号、创建时间、创建人、变更摘要。
- 当前启用版本以特殊标识突出。
- 点击历史版本可查看详情卡片并与当前版本对比。

### 14.5 底部区域：模型资产产品列表

列表展示与当前模型实例关联的产品资产：

- forcing 版本（forcing_version_id、数据源、时间范围）。
- 状态快照（latest state snapshot、创建时间）。
- 频率曲线绑定（frequency_curve_version_id、拟合方法、样本年数）。

### 14.6 地图嵌入

页面右下角嵌入小地图组件：

- 展示当前选中流域的边界多边形。
- 叠加河网矢量图层。
- 支持缩放和平移，不承载交互查询功能。

### 14.7 操作权限

- version_admin 及以上角色可执行：切换 active model（变更 active_flag）、查看版本对比 diff。
- 普通用户仅可浏览，不可修改。

## 15. 产品监控与运行状态页面

### 15.1 页面描述

面向运维人员的流水线监控仪表盘，实时展示当前预报周期的作业执行状态、性能趋势和异常告警。

### 15.2 顶部摘要条

```text
当前起报周期：2026-05-04T00Z（示例）
总作业数：128
成功：110    失败：3    运行中：12    等待：3
Slurm 队列深度：15
```

各状态计数以对应颜色标识（成功绿、失败红、运行中蓝、等待灰）。

### 15.3 左侧流水线视图

当前周期各阶段状态卡片，按执行顺序纵向排列：

```text
download → canonical → forcing → shud_run → parse → frequency → publish
```

每个卡片显示：

- 阶段名称。
- 状态标识（成功 ✓ / 失败 ✗ / 进行中 ◉ / 等待 ○）。
- 耗时（已完成阶段显示实际耗时，进行中显示已用时间）。
- 流域完成率（如 85/128 = 66%）。

卡片之间以箭头连接，表示流水线依赖关系。

### 15.4 中间区域：作业列表表格

| 列名 | 说明 |
|---|---|
| run_id | 运行唯一标识 |
| model_id | 模型实例 ID |
| run_type | forecast / analysis / reforecast |
| scenario | GFS / IFS / best_available |
| status | queued / running / success / failed / cancelled |
| slurm_job_id | Slurm 作业 ID |
| submitted_at | 提交时间 |
| duration | 运行时长 |
| 操作 | 查看日志 / 重跑 |

支持按 status、run_type、scenario 筛选，按 submitted_at 或 duration 排序。

### 15.5 右侧区域：性能与成功率趋势

- **性能趋势图**：近 7 天各阶段平均耗时折线图，横轴为起报周期，纵轴为耗时（分钟），每条线代表一个阶段。
- **成功率趋势图**：近 7 天每个起报周期的作业成功率，横轴为起报周期，纵轴为成功率百分比。

### 15.6 状态机映射说明

forecast_cycle 状态到流水线阶段卡片的映射关系：

| forecast_cycle 状态 | 流水线阶段 |
|---|---|
| discovered / downloading | download |
| raw_complete | canonical |
| canonical_ready | forcing |
| forcing_ready | shud_run |
| forecast_running | shud_run |
| parsed_partial / parsed_complete | parse / frequency |
| published | publish |

前端根据 forecast_cycle 当前状态自动高亮对应的流水线阶段卡片。

### 15.7 操作权限

- operator 及以上角色可执行：触发单阶段重跑、取消运行中的作业。
- 普通用户仅可查看状态和日志，不可触发操作。
