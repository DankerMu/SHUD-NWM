## REMOVED Requirements

### Requirement: Scenario 选择器控件
**Reason**: The checkbox selector (`components/ScenarioSelector.tsx`) was only mounted by the unrouted forecast side panel and is deleted (#3222). The single-map river popup always requests and draws both sources; it has no selector.
**Migration**: None. No user-visible surface is removed.

### Requirement: 数据请求参数传递
**Reason**: It specifies the request built by the deleted forecast store. The live request is built by `lib/hydroMet/riverForecast.ts`. The API route and its `scenarios` / `include_analysis` behaviour are not changed by this removal. The removed scenario "include_analysis 与多源兼容" (analysis appears once with several scenarios) is API behaviour that **no other spec states**; `tests/test_e2e_ifs.py` pins it. Recorded as a spec gap, not closed here.
**Migration**: None.

### Requirement: IFS 数据不可用时的降级显示
**Reason**: It specifies a hint next to the selector's IFS checkbox, which no longer exists. The river curve window draws the source that passed validation and lists the one that failed; that behaviour is pinned by `M11RiverForecastPanel.test.tsx` but **no spec states it** (`map-feature-popups` only specifies the whole-window empty state). Recorded as a spec gap, not closed here.
**Migration**: None.

## MODIFIED Requirements

### Requirement: 多源曲线渲染

系统 SHALL 满足「多源曲线渲染」要求。

图表同时渲染多个 scenario 的预报曲线，颜色和线型区分。

#### Scenario: GFS+IFS 双曲线显示

WHEN API 返回 GFS 和 IFS 两组 series 数据
THEN 图表显示：
  - 分析运行（analysis_true_field）：蓝色实线 `#2266cc`
  - GFS 预报（forecast_gfs_deterministic）：橙色实线 `#ef7d22`
  - IFS 预报（forecast_ifs_deterministic）：绿色虚线 `#2ca02c`

#### Scenario: 图例显示

WHEN 多条曲线可见
THEN 图例显示每条曲线的 scenario 名称和颜色标记
AND 图例格式：`分析（ERA5） ━ | GFS 预报 ━ | IFS 预报 ┅`

#### Scenario: Tooltip 联动

WHEN 用户悬浮在图表某一时间点
THEN tooltip 显示该时间点所有可见 scenario 的流量值
AND 格式：`时间: 2026-05-03 06:00 | 分析: 920.1 m³/s | GFS: 1100.2 m³/s | IFS: 1090.5 m³/s`

#### Scenario: 单 scenario 显示

WHEN 图表数据只含一个预报 scenario（仅 GFS 或仅 IFS）
THEN 渲染行为与当前单曲线模式一致
AND 不显示缺失 scenario 的图例项
