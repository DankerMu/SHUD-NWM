## ADDED Requirements

### Requirement: Scenario 选择器控件

系统 SHALL 满足「Scenario 选择器控件」要求。

前端提供 scenario 复选框组，控制预报曲线可见性。

#### Scenario: 默认状态

WHEN 用户打开河段预报详情页
THEN 显示 scenario 选择器，包含 `☑ GFS ☐ IFS` 两个复选框
AND 默认仅选中 GFS
AND 仅请求并显示 GFS 预报曲线

#### Scenario: 选中 IFS

WHEN 用户勾选 IFS 复选框
THEN 请求 API `?scenarios=GFS,IFS`（包含所有已选 scenario）
AND 图表显示 GFS + IFS 两条预报曲线

#### Scenario: 取消 GFS

WHEN 用户取消 GFS 复选框，仅保留 IFS
THEN 请求 API `?scenarios=IFS`
AND 图表仅显示 IFS 预报曲线

#### Scenario: 至少保留一个 scenario

WHEN 用户尝试取消最后一个勾选的 scenario
THEN 操作被阻止或自动恢复，保证至少有一个 scenario 被选中

---

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

WHEN 只有一个 scenario 被选中（仅 GFS 或仅 IFS）
THEN 渲染行为与当前单曲线模式一致
AND 不显示缺失 scenario 的图例项

---

### Requirement: IFS 06/18 周期可用时效标注

系统 SHALL 满足「IFS 06/18 周期可用时效标注」要求。

06/18 周期的 IFS 预报仅有 6 天（144h），需在 UI 上明确标注。

#### Scenario: 06/18 周期标注

WHEN 当前显示的 IFS 预报来自 06 或 18 UTC 周期
AND API response 中该 series 的 `available_lead_hours` = 144
THEN IFS 曲线在第 144h（第 6 天）处终止
AND 曲线末端显示虚线垂直标注线
AND 标注文字："IFS 6d"（紧凑标签）

#### Scenario: 00/12 周期无标注

WHEN 当前显示的 IFS 预报来自 00 或 12 UTC 周期
THEN IFS 曲线延伸至第 168h（第 7 天），与 GFS 等长
AND 不显示时效标注

#### Scenario: GFS 不受影响

WHEN IFS 曲线显示 6d 标注
THEN GFS 曲线不受影响，始终显示完整 7 天

---

### Requirement: 起报时刻信息增强

系统 SHALL 满足「起报时刻信息增强」要求。

展示当前预报使用的数据源和起报时刻信息。

#### Scenario: 多源起报信息

WHEN 选中 GFS 和 IFS 两个 scenario
THEN 面板顶部显示：
  - `起报时刻: 2026-05-03 00:00 UTC`
  - `数据源: GFS, IFS`

#### Scenario: 起报时刻不一致

WHEN GFS 和 IFS 的最新可用周期时间不同（如 GFS 00Z 已到，IFS 00Z 未到，使用 IFS 18Z）
THEN 分别标注各源的起报时刻
AND 格式：`GFS: 05-03 00Z | IFS: 05-02 18Z`

---

### Requirement: API Response 多源元数据

系统 SHALL 满足「API Response 多源元数据」要求。

API response 的每个 series 元素需携带足够的元数据供前端渲染。

#### Scenario: per-series 元数据字段

WHEN API 返回多 scenario 的 forecast-series response
THEN 每个 series 元素包含：
  - `scenario_id`: 场景标识（如 `"forecast_gfs_deterministic"`）
  - `source_id`: 数据源（如 `"GFS"`, `"IFS"`）
  - `cycle_time`: 该 scenario 使用的起报时刻
  - `available_lead_hours`: 该 scenario 的最大可用预报时效（GFS=168, IFS 00/12=168, IFS 06/18=144）
  - `segment_role`: `"past_7_days"` 或 `"future_7_days"`

#### Scenario: multi-source latest 策略

WHEN issue_time=latest 且 scenarios=GFS,IFS
THEN 后端为每个 source 独立选取最新可用周期（per-source latest）
AND 不要求所有 source 使用同一 cycle_time
AND response 中每个 series 的 cycle_time 可能不同

---

### Requirement: 数据请求参数传递

系统 SHALL 满足「数据请求参数传递」要求。

前端 store 正确传递 scenarios 参数给 API。

#### Scenario: 多 scenario 请求

WHEN forecast store 发起 API 请求
THEN URL query 参数 `scenarios` = 已选中的 scenario 列表，逗号分隔
AND 值为大写源名称（`GFS`, `IFS`），不使用完整 scenario_id

#### Scenario: include_analysis 与多源兼容

WHEN `include_analysis=true` 且 scenarios 包含 GFS 和 IFS
THEN API 返回 analysis + GFS forecast + IFS forecast 三组 series
AND 分析数据仅出现一次（不因多 scenario 重复）

---

### Requirement: IFS 数据不可用时的降级显示

系统 SHALL 满足「IFS 数据不可用时的降级显示」要求。

IFS 预报数据尚未到达时的前端行为。

#### Scenario: IFS 数据未就绪

WHEN 用户选中 IFS 但当前 cycle_time 无 IFS 预报数据
THEN IFS 复选框旁显示灰色提示：`(暂无数据)`
AND 图表仅显示 GFS 曲线
AND 不显示错误弹窗

#### Scenario: IFS 数据后续到达

WHEN 用户已打开预报页且 IFS 数据随后到达
THEN 用户手动刷新或切换 scenario 后可看到 IFS 曲线
AND 不要求实时推送更新
