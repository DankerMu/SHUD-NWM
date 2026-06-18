## Context

SHUD-NWM 已完成 GFS+IFS 双源预报闭环（M1-M4），当前 `hydro.river_timeseries` 中存储了预报流量值，但缺少将流量转化为"多严重"的能力。数据库 `flood.flood_frequency_curve` 和 `flood.return_period_result` 表已在 M0 创建但未写入数据。前端预报曲线已支持 `frequency_thresholds` 占位字段但未渲染。

系统总体设计（§6.3）已定义 Hindcast 运行模式，模块设计（docs/modules/11）已定义频率分析方法学。本阶段将这些设计落地为可运行代码。

**约束**：
- ERA5 数据延迟约 5 天，hindcast 使用 ERA5 final 产品（延迟数月但质量最高）
- 30+ 年历史回放需要大量 HPC 时间，必须支持按年切片并行
- 频率拟合依赖 scipy，须确认 HPC 环境可用
- 前端预警页是系统 6 个主 Tab 之一，需与现有导航框架集成

## Goals / Non-Goals

**Goals:**
- Hindcast 可按年切片并行提交到 Slurm，产出 30+ 年 river_timeseries 样本
- P-III/GEV 拟合产出 flood_frequency_curve，通过 QC 后可被前端使用
- 每次 forecast run 自动计算 return_period_result 和 warning_level
- 前端预警地图页展示 7 级河段着色 + 统计面板 + TOP 排名
- 预警矢量瓦片支持全国级缩放渲染

**Non-Goals:**
- 不实现 POT/GPD 方法（P-III + GEV 满足 MVP，POT 作为后续增强）
- 不实现集合预报概率重现期（仅 deterministic）
- 不实现自动定时 hindcast 调度（由 operator 手动提交）
- 不实现频率曲线的在线编辑或人工修正
- 不实现预警推送通知（邮件/webhook，属于阶段 8）

## Decisions

### D1: Hindcast 按年切片运行

**选型**：Hindcast 按水文年切片（每年一个 Slurm 作业），切片之间可并行。

**理由**：
- 30 年连续运行单作业 wall-time 可能超出 Slurm 限制（通常 24-72h）
- 按年切片可在年级别重试失败作业，不影响其他年份
- 年最大值提取天然以年为粒度，切片边界与采样边界一致
- 单年 ERA5 forcing + SHUD 运行耗时约 10-30 分钟（取决于流域大小），控制在 Slurm wall-time 内

**备选**：连续 30 年一次运行。缺点：wall-time 超限、失败需全量重跑。

**实现细节**：
- hindcast 提交 API 接收 `start_time` 和 `end_time`（与总体设计 §6.3 一致），服务端从时间范围派生日历年列表
- 每个切片是独立的 `hydro_run`（run_type=hindcast），run_id 格式 `hindcast_era5_{model}_{year}`
- 切片之间无 state 传递（每年从 cold start 开始，或可选配置 state chaining）
- Slurm job array `--array=0-N` 并行提交

### D2: 频率拟合方法选择

**选型**：默认 P-III（Pearson Type III），备选 GEV（Generalized Extreme Value）。

**理由**：
- P-III 是中国水文行业标准方法（SL 44-2006《水利水电工程设计洪水计算规范》）
- GEV 是国际通用方法（USGS Bulletin 17C 推荐 Log-Pearson III，GEV 作为对比）
- scipy.stats 已有 `pearson3` 和 `genextreme` 分布实现，无需自建
- 默认 P-III，失败或单调性不通过时 fallback GEV；operator 可通过 CLI `--method` 显式指定

**备选**：仅 P-III。缺点：某些流域 P-III 拟合效果差（如极端值偏离大），GEV 作为 fallback 提高鲁棒性。

### D3: 年最大值提取——SQL vs Python

**选型**：SQL 窗口函数从 TimescaleDB 提取年最大值，Python 层做滑动平均和采样。

**理由**：
- 逐小时流量（duration=1h）的年最大值可直接 SQL：`MAX(value) ... GROUP BY EXTRACT(YEAR FROM valid_time)`
- 滑动平均（3h/6h/24h/72h/7d）需要窗口函数，TimescaleDB 的 `time_bucket` + window function 性能优于全量读入
- Python 层处理：(1) 按 duration 构造 SQL；(2) 过滤缺测率 >10% 的年份；(3) 将结果交给 scipy 拟合
- 不一次加载全国所有河段——按 model_id + river_segment_id 逐段处理

**备选**：全量读入 Python 用 pandas 处理。缺点：大流域 1000+ 河段 × 30 年 × 逐小时 = 内存压力大。

### D4: 重现期计算触发方式

**选型**：在 Slurm 依赖链中 parse 阶段之后自动触发 frequency 阶段。

**理由**：
- M3 已建立 `download → canonical → forcing → forecast → parse → frequency → publish` 七阶段链
- frequency 作为 Slurm 依赖 `--dependency=afterok:$PARSE_JOB`，无需新增触发机制
- hydro_run 状态机已有 `frequency_done` 状态（`parsed → frequency_done → published`）
- 频率计算仅需查询 flood_frequency_curve 表和当前 run 的 max Q，耗时极短（秒级）

**备选**：API webhook 触发。缺点：引入异步通知机制，增加复杂度。

### D5: 前端预警瓦片渲染方案

**选型**：MapLibre + PBF vector tiles + data-driven styling。

**理由**：
- 系统已使用 MapLibre GL JS（docs/spec/06 §1），复用现有栈
- vector tiles 支持 `fill-color` 按 `warning_level` 属性动态着色，无需为每个等级生成独立瓦片
- PBF 格式比 GeoJSON 体积小 10x+，全国 10000+ 河段可流畅渲染
- tile endpoint `GET /api/v1/tiles/flood-return-period/{run_id}/{duration}/{valid_time}/{z}/{x}/{y}.pbf` 已在 API 设计中定义

**备选**：预生成 7 套 PNG 栅格瓦片（每级一套）。缺点：存储量大、无法动态切换等级。

### D6: Hindcast 数据隔离

**选型**：遵循总体设计 §6.3——hindcast 入库到同一 `river_timeseries` 表但 `run_type=hindcast`，不产生 StateSnapshot，不自动发布。

**理由**：
- 统一存储便于频率引擎直接 SQL 查询，无需跨表 JOIN
- `run_type` 字段已有 `hindcast` 枚举值，WHERE 过滤成本极低
- 不产生 StateSnapshot 避免污染业务 warm-start 链

### D7: 频率曲线版本绑定策略

**选型**：曲线与 `(model_id, river_network_version_id, river_segment_id, duration, method)` 唯一绑定，UNIQUE 约束已在 M0 建表时创建。

**理由**：
- 与总体设计 §5.5 一致：模型版本更新必须重算频率曲线
- 旧曲线 `quality_flag` 改为 `superseded_by_model_upgrade`，不删除
- `sample_period_start/end` 纳入唯一约束，支持扩展样本后生成新曲线

## Risks / Trade-offs

| 风险 | 影响 | 缓解 |
|---|---|---|
| ERA5 下载 30+ 年数据量大（约 50-100GB/流域） | HPC 存储压力、下载耗时长 | 按年切片下载，处理完删除原始文件保留 canonical；优先用 ERA5 已有 canonical（M2 可能已下载部分） |
| 小流域样本年数不足 | 高等级 Q（Q50/Q100）不可靠 | quality_flag = 'insufficient_sample'，前端标注"仅供参考"，不参与正式预警 |
| P-III 拟合在某些极端分布下失败 | 频率曲线无法生成 | 自动 fallback 到 GEV；两种都失败则 quality_flag = 'fit_failed'，跳过该河段 |
| 单调性违反（Q10 > Q20） | 预警等级映射混乱 | 自动修正：取相邻有效值线性插值，quality_flag = 'monotonicity_corrected' |
| 全国 10000+ 河段频率拟合耗时 | 首次计算耗时长 | Slurm job array 按流域并行；每河段拟合 <1 秒，1000 段 <20 分钟 |
| 新模型版本上线需重算所有曲线 | 运维操作复杂 | 提供 CLI `nhms-flood fit-curves --model-id`，一键重算；旧曲线自动 superseded |

## Open Questions

1. **水文年起止月份**：北方流域汛期 6-9 月 vs 南方 4-10 月，是否需要支持按流域配置 `flood_season` 参数？（当前默认全年样本，汛期模式作为 `parameters_json.season_filter` 可选项）
2. **Hindcast state chaining**：30 年按年切片时，是否需要年与年之间传递 state（即年末 state 作为下一年 init_state）？连续运行精度更高但增加串行依赖。（当前默认每年 cold start，operator 可选配置）
3. **瓦片缓存策略**：频率瓦片是否在每次 forecast run 后重新生成？还是仅在频率曲线更新时重算？（建议：return_period 瓦片每次 run 重算，frequency_curve 瓦片仅模型更新时重算）
