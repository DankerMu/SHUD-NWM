# 06B. 前端 UI 设计规范

版本：v0.2  
日期：2026-05-04  
适用文档：配合 `06_frontend_gis_design.md` 使用

---

## 1. 设计原则

- **信息密度优先**：水文业务系统面向专业用户，允许高信息密度，减少装饰空白
- **地图为主体**：地图区域始终占据视觉中心，面板为辅助
- **状态即色彩**：预警等级、运行状态、数据质量等用色彩一致表达
- **一致性**：所有页面共享导航、配色、间距、组件样式

---

## 2. 设计 Token

### 2.1 色彩体系

**主色（品牌色）：**

| Token | 色值 | 用途 |
|---|---|---|
| --color-primary-900 | #0A1929 | 顶部导航栏背景 |
| --color-primary-800 | #0D2744 | 导航栏 hover 态 |
| --color-primary-700 | #0F3460 | 侧边面板标题背景 |
| --color-primary-600 | #1565C0 | 主按钮、活跃 Tab |
| --color-primary-500 | #1E88E5 | 链接、图标激活色 |
| --color-primary-100 | #E3F2FD | 选中行背景、浅色标记 |
| --color-primary-50 | #F5F9FF | 面板背景色 |

**中性色：**

| Token | 色值 | 用途 |
|---|---|---|
| --color-neutral-900 | #1A1A2E | 正文文字 |
| --color-neutral-700 | #4A4A6A | 次要文字 |
| --color-neutral-500 | #8E8EA0 | 占位符、禁用文字 |
| --color-neutral-300 | #D0D0DD | 边框、分割线 |
| --color-neutral-100 | #F0F0F5 | 表格斑马纹、背景区分 |
| --color-neutral-50 | #FAFAFA | 页面底色 |
| --color-white | #FFFFFF | 卡片背景、面板背景 |

**状态色：**

| Token | 色值 | 用途 |
|---|---|---|
| --color-success | #4CAF50 | 成功、正常、active |
| --color-warning | #FF9800 | 警告、偏高 |
| --color-danger | #F44336 | 错误、失败、严重 |
| --color-info | #2196F3 | 信息、运行中 |

**重现期等级色（专用）：**

| 等级 | 色值 | 名称 |
|---|---|---|
| 常态 (< 2年) | #4FC3F7 | 浅蓝 |
| 偏高 (2-5年) | #81C784 | 浅绿 |
| 关注 (5-10年) | #FFD54F | 黄色 |
| 警戒 (10-20年) | #FFB74D | 橙色 |
| 高风险 (20-50年) | #FF8A65 | 深橙 |
| 严重 (50-100年) | #E57373 | 红色 |
| 极端 (> 100年) | #AB47BC | 紫红 |

**径流量色标（连续渐变）：**

```text
低流量  ━━━━━━━━━━━━━━━━━━━━  高流量
#E3F2FD → #90CAF9 → #42A5F5 → #1E88E5 → #FF9800 → #F44336
```

分 6 个等级：< 500、500-1000、1000-5000、5000-10000、10000-50000、> 50000 m³/s（可按流域缩放）

### 2.2 字体

```css
--font-family: 'PingFang SC', 'Microsoft YaHei', -apple-system, 'Helvetica Neue', sans-serif;
--font-mono: 'JetBrains Mono', 'Fira Code', 'Consolas', monospace;
```

| Token | 字号 | 字重 | 行高 | 用途 |
|---|---|---|---|---|
| --text-headline | 20px | 600 | 28px | 页面标题、河段名称 |
| --text-title | 16px | 600 | 24px | 面板标题、卡片标题 |
| --text-body | 14px | 400 | 22px | 正文、表格内容 |
| --text-caption | 12px | 400 | 18px | 辅助文字、时间戳、图例 |
| --text-metric | 24px | 700 | 32px | 大数值展示（统计摘要卡片） |
| --text-metric-sm | 18px | 600 | 26px | 中等数值（河段面板当前Q） |
| --text-mono | 13px | 400 | 20px | ID、代码、坐标值（等宽字体） |

### 2.3 间距

基于 4px 基数的间距体系：

| Token | 值 | 典型用途 |
|---|---|---|
| --space-1 | 4px | 图标与文字间距 |
| --space-2 | 8px | 紧凑内间距、列表行间距 |
| --space-3 | 12px | 卡片内元素间距 |
| --space-4 | 16px | 卡片内边距、面板区块间距 |
| --space-5 | 20px | 面板与面板间距 |
| --space-6 | 24px | 大区块间距 |
| --space-8 | 32px | 页面级间距 |

### 2.4 圆角

| Token | 值 | 用途 |
|---|---|---|
| --radius-sm | 4px | 小按钮、输入框、标签 |
| --radius-md | 8px | 卡片、弹窗、面板 |
| --radius-lg | 12px | 大面板、模态框 |
| --radius-full | 50% | 头像、圆形按钮 |

### 2.5 阴影

| Token | 值 | 用途 |
|---|---|---|
| --shadow-sm | 0 1px 3px rgba(0,0,0,0.08) | 输入框、小卡片 |
| --shadow-md | 0 2px 8px rgba(0,0,0,0.12) | 标准卡片、面板 |
| --shadow-lg | 0 4px 16px rgba(0,0,0,0.16) | 弹窗、悬浮面板 |
| --shadow-xl | 0 8px 32px rgba(0,0,0,0.20) | 模态框、全屏弹层 |

### 2.6 层级（z-index）

| Token | 值 | 用途 |
|---|---|---|
| --z-base | 0 | 地图层 |
| --z-panel | 100 | 侧面板、底部时间轴 |
| --z-overlay | 200 | 弹窗卡片、tooltip |
| --z-modal | 300 | 模态框 |
| --z-nav | 400 | 顶部导航栏 |
| --z-toast | 500 | 通知 toast |

---

## 3. 组件规范

### 3.1 顶部导航栏

```text
高度：56px
背景：--color-primary-900
Logo 区域：左侧 16px，Logo 32x32，系统名称 --text-title 白色
Tab 栏：居中排列，Tab 间距 0，每个 Tab 内边距 16px 24px
  - 默认态：白色文字 opacity 0.7
  - hover：白色文字 opacity 0.9，下方 2px 白色线
  - 活跃态：白色文字 opacity 1.0，下方 3px --color-primary-500 线
右侧信息：GFS 起报信息 --text-caption 白色 opacity 0.7，时间 --text-body 白色，用户头像 32x32 圆形
```

### 3.2 侧面板

```text
宽度：左侧 280px，右侧 320-360px（按页面定义）
背景：--color-white
边框：右侧/左侧 1px --color-neutral-300
内边距：--space-4（16px）
标题区域：
  - 高度 44px
  - 背景 --color-primary-50
  - 文字 --text-title --color-primary-700
  - 下方 1px 分割线
区块间距：--space-5（20px）
```

### 3.3 卡片

```text
背景：--color-white
圆角：--radius-md（8px）
阴影：--shadow-md
内边距：--space-4（16px）
标题：--text-title --color-neutral-900，下方 --space-3 间距

统计摘要卡片（大数值）：
  数值：--text-metric --color-primary-600
  标签：--text-caption --color-neutral-700
  卡片内排列：数值在上，标签在下，居中对齐
  卡片间距：--space-3（12px）
```

### 3.4 按钮

| 类型 | 背景 | 文字 | 边框 | 圆角 | 高度 |
|---|---|---|---|---|---|
| 主按钮 | --color-primary-600 | white | none | --radius-sm | 36px |
| 次按钮 | transparent | --color-primary-600 | 1px --color-primary-600 | --radius-sm | 36px |
| 文字按钮 | transparent | --color-primary-500 | none | --radius-sm | 36px |
| 危险按钮 | --color-danger | white | none | --radius-sm | 36px |
| 禁用态 | --color-neutral-100 | --color-neutral-500 | none | --radius-sm | 36px |

所有按钮内边距：12px 20px，字号 --text-body，字重 500

### 3.5 表格

```text
表头：
  背景 --color-neutral-100
  文字 --text-caption --color-neutral-700 字重 600
  高度 40px
  内边距 12px 16px
表体行：
  高度 44px
  内边距 12px 16px
  斑马纹：偶数行 --color-neutral-50
  hover：--color-primary-50
  选中：--color-primary-100，左侧 3px --color-primary-600 边框
边框：行间 1px --color-neutral-100
```

### 3.6 输入框与搜索框

```text
高度：36px
背景：--color-white
边框：1px --color-neutral-300
圆角：--radius-sm
内边距：8px 12px
focus 边框：1px --color-primary-500 + 0 0 0 2px rgba(30,136,229,0.2)
搜索框：左侧 search icon 16x16 --color-neutral-500
```

### 3.7 开关（Toggle）

```text
宽度：40px，高度：22px
关闭态：背景 --color-neutral-300，圆点 --color-white
开启态：背景 --color-primary-600，圆点 --color-white
过渡：0.2s ease
```

### 3.8 标签（Tag/Badge）

| 类型 | 背景 | 文字 | 用途 |
|---|---|---|---|
| 状态-成功 | rgba(76,175,80,0.12) | --color-success | active、published |
| 状态-运行中 | rgba(33,150,243,0.12) | --color-info | running、downloading |
| 状态-警告 | rgba(255,152,0,0.12) | --color-warning | warning、elevated |
| 状态-失败 | rgba(244,67,54,0.12) | --color-danger | failed、error |
| 状态-禁用 | --color-neutral-100 | --color-neutral-500 | cancelled、deprecated |

圆角 --radius-sm，内边距 4px 8px，字号 --text-caption

### 3.9 Tooltip

```text
背景：--color-neutral-900 opacity 0.92
文字：--text-caption --color-white
圆角：--radius-sm
内边距：8px 12px
阴影：--shadow-lg
箭头：6px 三角
最大宽度：320px
出现延迟：200ms
动画：fadeIn 150ms
```

### 3.10 弹窗卡片（Popup Card）

```text
背景：--color-white
圆角：--radius-md
阴影：--shadow-lg
最大宽度：360px
标题区：--text-title，下方 1px 分割线
内容区：--text-body，字段名 --color-neutral-700，字段值 --color-neutral-900
底部操作区：上方 1px 分割线，按钮右对齐，间距 --space-2
指向箭头：12px，与地图点位对齐
```

### 3.11 时间轴

```text
容器高度：64px
背景：--color-white
上边框：1px --color-neutral-300

时间条：
  高度 4px
  背景 --color-neutral-200
  已过去区间 --color-primary-500
  未来区间 --color-neutral-200
  Analysis/Forecast 分界线：2px dashed --color-warning

滑块指示器：
  宽度 2px 高度 24px --color-primary-600
  顶部圆点 8px --color-primary-600

播放控制按钮组：
  按钮尺寸 32x32，圆角 --radius-sm
  图标 16x16 --color-neutral-700
  hover 背景 --color-neutral-100
  active 背景 --color-primary-100

日期标注：
  --text-caption --color-neutral-500
  关键时刻（00Z、12Z）加粗
```

---

## 4. 图表配置规范

### 4.1 通用 ECharts 配置

```javascript
const baseChartOption = {
  textStyle: {
    fontFamily: 'PingFang SC, Microsoft YaHei, sans-serif',
    fontSize: 12,
    color: '#4A4A6A'
  },
  grid: {
    top: 40,
    right: 24,
    bottom: 32,
    left: 56,
    containLabel: false
  },
  tooltip: {
    trigger: 'axis',
    backgroundColor: 'rgba(26,26,46,0.92)',
    borderColor: 'transparent',
    textStyle: { color: '#FFFFFF', fontSize: 12 },
    padding: [8, 12]
  },
  legend: {
    top: 8,
    right: 16,
    textStyle: { fontSize: 12, color: '#4A4A6A' },
    itemWidth: 16,
    itemHeight: 3,
    itemGap: 16
  },
  xAxis: {
    axisLine: { lineStyle: { color: '#D0D0DD' } },
    axisTick: { show: false },
    axisLabel: { fontSize: 11, color: '#8E8EA0' },
    splitLine: { show: false }
  },
  yAxis: {
    axisLine: { show: false },
    axisTick: { show: false },
    axisLabel: { fontSize: 11, color: '#8E8EA0' },
    splitLine: { lineStyle: { color: '#F0F0F5', type: 'dashed' } }
  }
};
```

### 4.2 河段预报曲线配置

```javascript
const forecastSeriesConfig = {
  analysis: {
    name: 'Analysis 实况',
    type: 'line',
    lineStyle: { width: 2, color: '#1E88E5' },
    itemStyle: { color: '#1E88E5' },
    smooth: false,
    showSymbol: false
  },
  gfs: {
    name: 'GFS 预报',
    type: 'line',
    lineStyle: { width: 2, color: '#FF9800' },
    itemStyle: { color: '#FF9800' },
    smooth: false,
    showSymbol: false
  },
  ifs: {
    name: 'IFS 预报',
    type: 'line',
    lineStyle: { width: 2, color: '#4CAF50', type: 'dashed' },
    itemStyle: { color: '#4CAF50' },
    smooth: false,
    showSymbol: false
  },
  // 频率阈值线
  thresholdLines: [
    { name: 'Q2',   value: null, color: '#90CAF9' },
    { name: 'Q5',   value: null, color: '#42A5F5' },
    { name: 'Q10',  value: null, color: '#FFD54F' },
    { name: 'Q20',  value: null, color: '#FFB74D' },
    { name: 'Q50',  value: null, color: '#E57373' },
    { name: 'Q100', value: null, color: '#AB47BC' }
  ],
  // Analysis/Forecast 分界线
  currentTimeMark: {
    type: 'line',
    lineStyle: { color: '#FF9800', type: 'dashed', width: 1 },
    label: { show: true, formatter: '当前时刻', color: '#FF9800', fontSize: 11 }
  }
};
```

### 4.3 降水柱状图配置

```javascript
const precipBarConfig = {
  type: 'bar',
  barWidth: '60%',
  itemStyle: {
    color: '#42A5F5',
    borderRadius: [2, 2, 0, 0]
  },
  label: { show: false },
  emphasis: {
    itemStyle: { color: '#1E88E5' }
  }
};
```

### 4.4 双轴折线图配置（温度+湿度）

```javascript
const dualAxisConfig = {
  temperature: {
    type: 'line',
    yAxisIndex: 0,
    lineStyle: { color: '#F44336', width: 1.5 },
    itemStyle: { color: '#F44336' },
    showSymbol: false,
    smooth: true
  },
  humidity: {
    type: 'line',
    yAxisIndex: 1,
    lineStyle: { color: '#4CAF50', width: 1.5 },
    itemStyle: { color: '#4CAF50' },
    showSymbol: false,
    smooth: true
  }
};
```

### 4.5 洪水频率曲线配置

```javascript
const frequencyCurveConfig = {
  xAxis: { type: 'log', name: '重现期 T (年)', min: 1.5, max: 200 },
  yAxis: { type: 'value', name: '流量 Q (m³/s)' },
  fittedCurve: {
    type: 'line',
    smooth: true,
    lineStyle: { color: '#1565C0', width: 2 },
    showSymbol: false
  },
  confidenceBand: {
    type: 'line',
    areaStyle: { color: 'rgba(21,101,192,0.1)' },
    lineStyle: { color: 'rgba(21,101,192,0.3)', type: 'dashed', width: 1 }
  },
  currentPeak: {
    type: 'scatter',
    symbolSize: 10,
    itemStyle: { color: '#F44336', borderColor: '#FFFFFF', borderWidth: 2 }
  }
};
```

### 4.6 环形图配置（Slurm 队列）

```javascript
const donutConfig = {
  type: 'pie',
  radius: ['55%', '75%'],
  center: ['50%', '50%'],
  label: {
    show: true,
    position: 'center',
    formatter: '{value}\n{name}',
    fontSize: 20,
    fontWeight: 700,
    color: '#1A1A2E'
  },
  data: [
    { name: '运行中', itemStyle: { color: '#1E88E5' } },
    { name: '等待中', itemStyle: { color: '#FFB74D' } },
    { name: '空闲', itemStyle: { color: '#E0E0E0' } }
  ]
};
```

### 4.7 迷你折线图（Sparkline）

```javascript
const sparklineConfig = {
  grid: { top: 4, right: 4, bottom: 4, left: 4 },
  xAxis: { show: false },
  yAxis: { show: false },
  series: [{
    type: 'line',
    smooth: true,
    showSymbol: false,
    lineStyle: { width: 1.5, color: '#1E88E5' },
    areaStyle: { color: 'rgba(30,136,229,0.08)' }
  }],
  tooltip: { show: false }
};
```

### 4.8 阶段耗时柱状图（横向）

```javascript
const stageDurationConfig = {
  type: 'bar',
  orient: 'horizontal',
  barWidth: 16,
  itemStyle: {
    borderRadius: [0, 4, 4, 0],
    color: function(params) {
      const colors = ['#42A5F5','#66BB6A','#FFA726','#EF5350','#AB47BC','#26C6DA','#8D6E63'];
      return colors[params.dataIndex % colors.length];
    }
  }
};
```

---

## 5. 图标规范

采用线性图标风格，推荐 Lucide Icons 或 Remix Icon 图标集。

### 5.1 常用图标映射

| 业务对象 | 图标名称 | 尺寸 |
|---|---|---|
| 流域 | map-pin-area / layers | 16px/20px |
| 河段 | git-branch / route | 16px/20px |
| 气象站 | thermometer / cloud-rain | 16px/20px |
| 预报运行 | play-circle / loader | 16px |
| 预警 | alert-triangle | 16px/20px |
| 模型版本 | git-commit / package | 16px |
| 时间/周期 | clock / calendar | 16px |
| 下载 | download / cloud-download | 16px |
| 设置 | settings / sliders | 16px |
| 搜索 | search | 16px |
| 筛选 | filter | 16px |
| 刷新/重跑 | refresh-cw | 16px |
| 查看详情 | external-link / arrow-right | 16px |
| 日志 | file-text | 16px |

### 5.2 图标使用规则

- 导航 Tab 图标：20px，与文字间距 --space-1
- 面板内按钮图标：16px，与文字间距 --space-1
- 状态图标：16px，颜色跟随状态色
- 图标默认颜色 --color-neutral-700，hover --color-primary-600

---

## 6. 动效与过渡

### 6.1 通用过渡

| 场景 | 属性 | 时长 | 缓动 |
|---|---|---|---|
| 按钮 hover/active | background, color | 150ms | ease |
| 面板展开/收起 | width, transform | 250ms | ease-out |
| 卡片 hover 抬起 | box-shadow, transform | 200ms | ease |
| Tab 切换内容 | opacity | 200ms | ease-in-out |
| 图层切换地图 | opacity | 300ms | ease |
| tooltip 出现 | opacity | 150ms | ease |

### 6.2 地图动效

| 场景 | 方法 | 参数 |
|---|---|---|
| 全国→流域 flyTo | map.flyTo() | duration: 1500ms, curve: 1.42 |
| 流域→河段 flyTo | map.flyTo() | duration: 1000ms, curve: 1.2 |
| 河段 hover 高亮 | paint 属性过渡 | line-width +2, line-opacity 增强 |
| 时间步切换瓦片 | source 更新 | 交叉淡入 300ms |

### 6.3 图表动效

```javascript
const chartAnimation = {
  animation: true,
  animationDuration: 800,
  animationEasing: 'cubicInOut',
  animationThreshold: 2000  // 超过 2000 个数据点关闭动画
};
```

---

## 7. 状态设计

### 7.1 加载态

```text
骨架屏策略：
  - 面板内容：灰色矩形脉冲动画，高度匹配实际内容
  - 地图区域：底图先加载，矢量瓦片叠加 loading spinner
  - 图表区域：灰色矩形 + 中央 loading spinner
  - 表格：行级骨架屏，显示 5 行占位

Loading Spinner：
  尺寸 24px
  颜色 --color-primary-500
  类型 circular（旋转圆环）
```

### 7.2 空状态

```text
容器居中显示：
  图标 64px --color-neutral-300
  标题 --text-title --color-neutral-700
  描述 --text-body --color-neutral-500
  操作按钮（可选）

示例文案：
  - 无河段数据："该流域暂无已发布的预报数据"
  - 无预警："当前无超警河段"
  - 无作业："当前周期暂无运行作业"
  - 搜索无结果："未找到匹配的站点"
```

### 7.3 错误态

```text
内联错误：
  红色边框 1px --color-danger
  背景 rgba(244,67,54,0.04)
  图标 alert-circle 16px --color-danger
  文字 --text-body --color-danger
  重试按钮（可选）

全屏错误：
  居中图标 + 标题 + 描述 + 重试按钮
  标题 "数据加载失败" / "服务连接异常"
```

### 7.4 缺失/受限态

```text
数据源受限（如 CLDAS 权限未开通）：
  图层 toggle 旁标注 "受限" 标签（--color-warning 色）
  hover tooltip："CLDAS 数据权限尚未开通，当前使用 ERA5 + GFS 替代"

IFS 时效不足：
  曲线末端截断处标注虚线 + "IFS 06Z 仅覆盖至 144h" 文字标识
```

---

## 8. 响应式规范

### 8.1 最小支持分辨率

```text
最小：1440 × 900（面板不折叠）
推荐：1920 × 1080
大屏：2560 × 1440
```

### 8.2 断点行为

| 宽度范围 | 行为 |
|---|---|
| ≥ 1920px | 标准三栏布局，所有面板展开 |
| 1440-1919px | 三栏布局，右侧面板宽度缩至 280px |
| 1280-1439px | 左右面板可折叠（点击按钮展开/收起），默认仅展开左侧 |
| < 1280px | 不推荐使用（显示提示"请使用更大屏幕"） |

### 8.3 面板折叠

```text
折叠按钮：面板边缘中间，12px 宽竖条 + 箭头图标
折叠动画：width 250ms ease-out
折叠后：仅显示 40px 宽图标栏（显示各区块图标）
```
