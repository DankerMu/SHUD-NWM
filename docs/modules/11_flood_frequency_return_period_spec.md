# 11. 洪水频率与重现期模块：开发 Spec

版本：v0.1  
日期：2026-04-30

## 1. 开发目标

交付可测试、可部署、可观测的 **洪水频率与重现期模块**，满足总体设计中关于数据血缘、版本管理、Slurm/HPC 解耦和前端发布的要求。

## 2. 功能需求

### 2.1 必须实现

- 生成年最大值或 POT 样本。
- 拟合 P-III/GEV/POT-GPD 等方法。
- 保存 Q2/Q5/Q10/Q20/Q50/Q100。
- 对预报 Qmax 计算 return period。
- 输出 warning_level。

### 2.2 应实现

- 支持 dry-run 模式，只生成计划和 manifest，不写正式产物。
- 支持 force-rerun，但必须写审计日志。
- 支持按 run_id/cycle_id/model_id 精确重跑。
- 支持结构化日志和 request_id/job_id 关联。

### 2.3 暂不实现

- 人工编辑生产数据。
- 跳过 QC 直接发布。
- 未经版本管理覆盖历史结果。

## 3. 输入

```text
上游：历史 SHUD 模拟结果、river_timeseries、Model Registry。
必要上下文：environment, operator, request_id, trace_id
配置：config/{env}.yaml + secrets manager
```

## 4. 输出

```text
下游：前端重现期图层、预警分析、API。
状态：created/running/succeeded/failed/published
日志：结构化 JSON lines
元数据：写入相关数据库表
大文件：写入对象存储或 HPC workspace
```

## 5. 数据库/存储影响

- `flood.flood_frequency_curve`
- `flood.return_period_result`
- `hydro.river_timeseries`

实现要求：写数据库必须在事务中完成；大文件写入成功后再更新对象 URI；时序数据写入必须支持 upsert 或先删后写，但禁止产生重复主键；对象存储写入必须记录 checksum/etag。

## 6. 接口

- `CLI nhms-flood fit-curves --model-id`
- `CLI nhms-flood compute-return-period --run-id`
- `GET /api/v1/river-segments/{id}/frequency-thresholds`

## 7. 频率分析方法学规范

### 7.1 默认方法与可选方法

```text
默认方法：P-III（皮尔逊 III 型）年最大值法
可选方法：
  - GEV（广义极值）年最大值法
  - POT（超阈值）+ GPD（广义 Pareto）
```

方法选择记录在 `flood_frequency_curve.method` 字段。

### 7.2 Duration 提取规则

`duration` 定义了从连续时间序列中提取极值样本的时间窗口：

```text
duration = 1h   → 取每年逐小时流量序列的最大值
duration = 3h   → 取每年 3 小时滑动平均流量的最大值
duration = 6h   → 取每年 6 小时滑动平均流量的最大值
duration = 24h  → 取每年 24 小时滑动平均流量的最大值
duration = 72h  → 取每年 72 小时滑动平均流量的最大值
duration = 7d   → 取每年 7 天滑动平均流量的最大值
```

滑动窗口步长等于模型输出步长（`model_output_interval`）。窗口内缺测率 > 10% 的年份不纳入样本，计入 `parameters_json.excluded_years`。

### 7.3 样本要求

```text
最小样本年数（按重现期等级）：
  Q2 / Q5   → ≥ 10 年
  Q10       → ≥ 15 年
  Q20       → ≥ 20 年
  Q50       → ≥ 30 年
  Q100      → ≥ 40 年

不满足最小样本量时：
  - 该等级的 Q 值仍可计算但 quality_flag = 'insufficient_sample'
  - 前端展示时标注"样本不足，仅供参考"
  - 不参与 warning_level 判定
```

### 7.4 汛期与全年样本选择

```text
默认：全年样本（annual maximum）
可选：汛期样本（flood_season_only）
  - 汛期定义按流域配置，如南方 4-10 月、北方 6-9 月
  - 记录在 parameters_json.season_filter
```

### 7.5 单调性与外推约束

```text
1. 必须满足 Q2 < Q5 < Q10 < Q20 < Q50 < Q100
2. 如果拟合结果违反单调性：
   a. 尝试其他方法（P-III → GEV）
   b. 如仍违反，取相邻有效值的线性插值修正
   c. 修正后 quality_flag = 'monotonicity_corrected'
3. 超出 Q100 的外推：不提供点估计，仅返回 return_period > 100
4. return_period 插值方法：对数线性插值（log T vs Q）
```

### 7.6 模型版本更新规则

```text
新 model_instance 上线后，必须重算所有关联河段的频率曲线：
  - 新曲线绑定新 model_id
  - 旧曲线保留但 quality_flag 改为 'superseded_by_model_upgrade'
  - 旧曲线不删除，保留审计和对比用途
```

## 8. 配置项

```yaml
flood_frequency_return_period:
  enabled: true
  dry_run: false
  max_retries: 3
  retry_backoff_seconds: [60, 300, 900]
  log_level: INFO
  workspace_root: /work/nhms
  object_store_prefix: s3://nhms
```

## 9. 测试要求

### 8.1 单元测试

- manifest schema 校验。
- 参数校验。
- 错误码映射。
- 幂等逻辑。

### 8.2 集成测试

- 使用 mock 数据源或小流域样例完成一次端到端调用。
- 验证数据库状态转移。
- 验证对象存储路径和 checksum。
- 验证失败重试和失败终态。

### 8.3 回归测试

- 固定一个历史周期和测试流域，比较输出行数、时间轴、关键统计值。
- 新版本不得破坏已发布 API 字段。

## 10. 性能要求

- 支持按流域/周期并发运行。
- 不在内存中一次性加载全国全部河段时序。
- 大文件以流式处理或分块处理为主。
- 指标和日志写入不应成为主流程瓶颈。

## 11. 安全要求

- 禁止把 token、密码、下载凭证写入日志。
- 所有外部输入必须校验。
- 文件路径必须限制在配置的 workspace/object prefix 内。
- 对外 API 必须执行鉴权和授权。

## 12. 验收清单

- [ ] 频率曲线与 model_id/river_segment_id 强绑定。
- [ ] Q2<Q5<Q10<Q20<Q50<Q100。
- [ ] 样本不足时 quality_flag 明确。
- [ ] 预报 run 完成后能自动计算未来 7 天最大重现期。

## 13. Definition of Done

- 代码合并到主分支。
- 单元测试和集成测试通过。
- 文档和配置示例更新。
- 可在 staging 环境完成一次成功运行。
- 指标、日志、错误码可在运维界面或日志系统中查询。
