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
