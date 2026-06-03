## ADDED Requirements

### Requirement: 预警地图页导航集成

系统 SHALL 在前端导航栏中添加"洪水预警" Tab。

#### Scenario: Tab 可见

- **WHEN** 用户登录系统（任意角色）
- **THEN** 顶部导航栏显示"洪水预警" Tab（位于"水文预报"之后、"产品监控"之前）
- **AND** 点击 Tab 切换到预警地图页

#### Scenario: 默认状态

- **WHEN** 进入预警地图页
- **THEN** 自动加载最新完成 frequency_done 的 forecast run 的重现期数据
- **AND** 地图显示全国范围，河段按 warning_level 着色

---

### Requirement: 河段预警着色

地图 SHALL 按 7 级预警等级为河段着色。

#### Scenario: 7 级配色方案

- **WHEN** 河段有 return_period_result 数据
- **THEN** 按以下配色渲染：
  - normal (T<2): 灰色 `#999999`
  - elevated (2≤T<5): 蓝色 `#4A90D9`
  - watch (5≤T<10): 黄色 `#F5C842`
  - warning (10≤T<20): 橙色 `#EF7D22`
  - high_risk (20≤T<50): 红色 `#E53935`
  - severe (50≤T<100): 深红色 `#B71C1C`
  - extreme (T≥100): 紫色 `#6A1B9A`

#### Scenario: 无频率曲线河段

- **WHEN** 河段没有重现期结果（warning_level = null）
- **THEN** 河段渲染为浅灰色虚线 `#CCCCCC`，区别于 normal 等级

#### Scenario: Hover 交互

- **WHEN** 鼠标 hover 河段
- **THEN** 高亮该河段并显示 tooltip：河段名/ID、预报 Q 值、重现期 T 值、预警等级

#### Scenario: Click 交互

- **WHEN** 点击河段
- **THEN** 右侧面板展开该河段详情
- **AND** 调用 `/flood-alerts/timeline` 获取时间线数据
- **AND** 调用 forecast-series 获取预报曲线 + frequency_thresholds

---

### Requirement: 左侧预警统计面板

预警页左侧 SHALL 显示各等级河段数量统计。

#### Scenario: 统计面板内容

- **WHEN** 预警数据加载完成
- **THEN** 左侧面板按等级分组显示：
  ```
  极端 (T≥100)    — N 条
  严重 (50≤T<100) — N 条
  高风险 (20≤T<50) — N 条
  警戒 (10≤T<20)  — N 条
  关注 (5≤T<10)   — N 条
  偏高 (2≤T<5)    — N 条
  正常 (T<2)      — N 条
  ```
- **AND** 每行左侧显示对应颜色圆点
- **AND** 排序从严重到正常（降序）

#### Scenario: 点击等级筛选

- **WHEN** 点击某个等级行（如"高风险"）
- **THEN** 地图自动筛选并高亮该等级的河段
- **AND** 其他等级河段半透明或隐藏
- **AND** 再次点击取消筛选

---

### Requirement: 右侧 TOP 排名面板

预警页右侧 SHALL 显示按重现期降序排名的河段列表。

#### Scenario: 排名列表内容

- **WHEN** 预警数据加载完成
- **THEN** 右侧面板显示 TOP 20 河段列表（调用 `/flood-alerts/ranking`）
- **AND** 每行包含：排名序号、河段名/ID、所属流域、预报 Q（m³/s）、重现期 T 值、预警等级标签（带颜色）

#### Scenario: 点击排名行

- **WHEN** 点击 TOP 排名中某行
- **THEN** 地图自动平移至该河段并 zoom in
- **AND** 弹出该河段的详情面板（预报曲线 + 时间线）

#### Scenario: 排名面板可配置

- **WHEN** 用户修改排名显示数量
- **THEN** 支持切换 TOP 10/20/50
- **AND** 支持按流域过滤

---

### Requirement: 时间步切换

预警页 SHALL 支持查看不同预报时刻的预警快照。

#### Scenario: 时间轴

- **WHEN** 预警页加载
- **THEN** 底部显示时间轴，列出当前 forecast run 的所有有效时刻
- **AND** 默认选中 max_over_window 时刻（即整体最严重时刻）

#### Scenario: 切换时刻

- **WHEN** 用户在时间轴上选择某个 valid_time
- **THEN** 地图着色更新为该时刻的 warning_level
- **AND** 左侧统计面板同步刷新
- **AND** 右侧 TOP 排名同步刷新

#### Scenario: 播放模式

- **WHEN** 用户点击播放按钮
- **THEN** 自动逐时刻推进，每步间隔 1 秒
- **AND** 地图着色、统计、排名同步更新
- **AND** 可随时暂停

---

### Requirement: 预警信息滚动条

预警页顶部 SHALL 显示超警河段摘要滚动条。

#### Scenario: 滚动内容

- **WHEN** 存在 warning 及以上等级的河段
- **THEN** 顶部信息条滚动显示：河段名 / 所属流域 / 预警等级 / Q 值 / T 值
- **AND** 自动轮播，每条停留 3 秒

#### Scenario: 点击跳转

- **WHEN** 点击滚动条中某条信息
- **THEN** 地图跳转至该河段并展开详情面板

#### Scenario: 无超警河段

- **WHEN** 所有河段 warning_level 为 normal 或 elevated
- **THEN** 信息条显示"当前无超警河段"

---

### Requirement: 预警矢量瓦片发布

系统 SHALL 发布预警重现期矢量瓦片。

#### Scenario: 瓦片端点

- **WHEN** 调用 `GET /api/v1/tiles/flood-return-period/{run_id}/{duration}/{valid_time}/{z}/{x}/{y}.pbf`
- **THEN** 返回 PBF 格式矢量瓦片
- **AND** 每个河段 feature 包含属性：river_segment_id, return_period, warning_level, q_value

#### Scenario: Data-driven styling

- **WHEN** 前端加载瓦片图层
- **THEN** MapLibre 使用 data-driven `line-color` 表达式按 `warning_level` 属性着色
- **AND** `line-width` 根据 return_period 值动态调整（T 越大线越粗）

#### Scenario: 瓦片生成触发

- **WHEN** frequency 阶段完成（hydro_run status = `"frequency_done"`）
- **THEN** 自动生成该 run 的预警矢量瓦片
- **AND** tile_layer 记录写入 `map.tile_layer` 表，layer_type = `"flood_return_period"`

#### Scenario: 缩放级别

- **WHEN** 用户在地图上缩放
- **THEN** 瓦片在 zoom 0-14 范围内可用
- **AND** 低缩放级别（0-6）仅显示高危及以上河段，高缩放级别（7+）显示所有有结果的河段

---

### Requirement: 河段详情面板

点击河段后 SHALL 展开详情面板显示预报曲线和预警信息。

#### Scenario: 详情面板内容

- **WHEN** 点击某河段
- **THEN** 右侧展开面板包含：
  1. 河段基本信息（名称、所属流域、河段长度）
  2. 当前预警等级（大字 + 颜色标识）
  3. 预报曲线图（analysis + forecast），叠加 Q2-Q100 水平参考线
  4. 预警时间线图（逐时刻 return_period 变化）

#### Scenario: 频率曲线叠加

- **WHEN** 预报曲线图渲染
- **THEN** Q2/Q5/Q10/Q20/Q50/Q100 作为水平虚线叠加在曲线图上
- **AND** 每条线用对应 warning_level 颜色渲染
- **AND** 右侧标注 Q 值和等级名称
