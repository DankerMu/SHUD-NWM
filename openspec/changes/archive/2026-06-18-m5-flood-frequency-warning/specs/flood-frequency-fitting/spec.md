## ADDED Requirements

### Requirement: 年最大值样本提取

系统 SHALL 从 `hydro.river_timeseries` 中为每个河段按 6 种 duration 提取年最大值序列。

#### Scenario: Duration=1h 直接提取

- **WHEN** 调用频率引擎为 river_segment_id 提取 duration=`"1h"` 的年最大值
- **THEN** SQL 查询 `SELECT EXTRACT(YEAR FROM valid_time) AS year, MAX(value) AS annual_max FROM hydro.river_timeseries WHERE ... AND variable='q_down' GROUP BY year`
- **AND** 返回 `[(year, annual_max)]` 列表

#### Scenario: Duration=24h 滑动平均提取

- **WHEN** 调用频率引擎为 river_segment_id 提取 duration=`"24h"` 的年最大值
- **THEN** 使用 24 小时滑动窗口计算平均流量，窗口步长 = model_output_interval
- **AND** 取每年滑动平均序列的最大值
- **AND** 返回 `[(year, annual_max_24h)]` 列表

#### Scenario: 缺测年份排除

- **WHEN** 某年的逐小时数据缺测率 > 10%
- **THEN** 该年不纳入样本
- **AND** 记入 `parameters_json.excluded_years` 列表

#### Scenario: Duration=3h/6h/72h/7d 滑动平均提取

- **WHEN** 调用频率引擎提取 duration=`"3h"` 的年最大值
- **THEN** 使用 3 小时滑动窗口计算平均流量，窗口步长 = model_output_interval
- **AND** 取每年滑动平均序列的最大值
- **AND** `6h`、`72h`、`7d` 遵循相同逻辑，窗口分别为 6h、72h、168h
- **AND** 窗口内缺测率 > 10% 的年份不纳入样本

#### Scenario: 全部 6 种 duration

- **WHEN** 对一个河段执行完整频率分析
- **THEN** 分别提取 `1h`、`3h`、`6h`、`24h`、`72h`、`7d` 六种 duration 的年最大值序列
- **AND** 每种 duration 独立生成一条 flood_frequency_curve 记录

#### Scenario: 所有年份缺测

- **WHEN** 所有年份的缺测率均 > 10%，有效样本数 = 0
- **THEN** 不执行分布拟合
- **AND** 写入 curve 记录：sample_size=0，q2-q100 全部为 null，quality_flag = `"no_valid_sample"`
- **AND** `parameters_json.excluded_years` 记录全部排除年份

---

### Requirement: P-III 分布拟合

系统 SHALL 使用皮尔逊 III 型分布拟合年最大值序列并计算 Q2-Q100。

#### Scenario: 正常拟合

- **WHEN** 年最大值样本量 ≥ 10 年
- **THEN** 使用 `scipy.stats.pearson3.fit()` 估计参数（skew, loc, scale）
- **AND** 计算 Q2/Q5/Q10/Q20/Q50/Q100 对应的分位数值
- **AND** `flood_frequency_curve.method` = `"P-III"`
- **AND** `parameters_json` 包含 `{"skew": ..., "loc": ..., "scale": ..., "n_samples": ..., "excluded_years": [...]}`

#### Scenario: 拟合失败 fallback 到 GEV

- **WHEN** P-III 拟合失败（scipy 抛出异常或参数无效）
- **THEN** 自动使用 `scipy.stats.genextreme.fit()` 重试
- **AND** 若 GEV 成功，`method` = `"GEV"`，`quality_flag` = `"p3_fallback_gev"`
- **AND** 若 GEV 也失败，写入 curve 记录：q2-q100 全部为 null，`quality_flag` = `"fit_failed"`，`ops.qc_result` 记录 severity=`"error"`

---

### Requirement: GEV 分布拟合

系统 SHALL 支持广义极值分布作为可选拟合方法。

#### Scenario: 显式指定 GEV

- **WHEN** CLI 或 API 指定 `method="GEV"`
- **THEN** 使用 `scipy.stats.genextreme.fit()` 拟合
- **AND** 计算 Q2-Q100
- **AND** `flood_frequency_curve.method` = `"GEV"`
- **AND** `parameters_json` 包含 `{"shape": ..., "loc": ..., "scale": ...}`

---

### Requirement: 样本量检查

系统 SHALL 按重现期等级检查最小样本年数。

#### Scenario: 样本充足

- **WHEN** 样本年数 ≥ 40 年
- **THEN** Q2 至 Q100 全部计算，quality_flag = `"ok"`

#### Scenario: 样本部分不足

- **WHEN** 样本年数 = 25 年
- **THEN** 整条 curve 的 quality_flag = `"partial_sample"`
- **AND** `parameters_json.sample_quality` 记录 per-threshold 状态：
  ```json
  {
    "Q2": {"min_required": 10, "met": true, "quality_flag": "ok"},
    "Q5": {"min_required": 10, "met": true, "quality_flag": "ok"},
    "Q10": {"min_required": 15, "met": true, "quality_flag": "ok"},
    "Q20": {"min_required": 20, "met": true, "quality_flag": "ok"},
    "Q50": {"min_required": 30, "met": false, "quality_flag": "insufficient_sample"},
    "Q100": {"min_required": 40, "met": false, "quality_flag": "insufficient_sample"}
  }
  ```
- **AND** Q50/Q100 仍计算但不参与 warning_level 判定

#### Scenario: 样本严重不足

- **WHEN** 样本年数 < 10 年
- **THEN** 所有 Q 值的 quality_flag = `"insufficient_sample"`
- **AND** 整条 curve 的 quality_flag = `"insufficient_sample"`

---

### Requirement: 单调性校验与修正

拟合结果 SHALL 满足 Q2 < Q5 < Q10 < Q20 < Q50 < Q100。

#### Scenario: 单调性通过

- **WHEN** 拟合结果 Q2=1200, Q5=1800, Q10=2300, Q20=2900, Q50=3700, Q100=4500
- **THEN** quality_flag 保持 `"ok"`

#### Scenario: 单调性违反并修正

- **WHEN** 拟合结果 Q10=2500 > Q20=2400（违反 Q10 < Q20）
- **THEN** 自动尝试 fallback 方法（P-III → GEV）
- **AND** 若仍违反，取相邻有效值线性插值修正：Q20 = (Q10 + Q50) / 2
- **AND** quality_flag = `"monotonicity_corrected"`
- **AND** `parameters_json.monotonicity_corrections` 记录修正详情

---

### Requirement: 频率曲线入库

拟合结果 SHALL 写入 `flood.flood_frequency_curve` 表。

#### Scenario: 正常入库

- **WHEN** 频率曲线拟合完成且通过 QC
- **THEN** 写入 flood_frequency_curve，字段包括：curve_id, model_id, river_network_version_id, basin_version_id, river_segment_id, duration, method, sample_period_start, sample_period_end, sample_size, parameters_json, q2-q100, unit=`"m3/s"`, quality_flag
- **AND** curve_id 格式为 `ffc_{model_id}_{rnv_id}_{segment_id}_{duration}_{method}_{sample_start}_{sample_end}`（包含完整唯一维度）

#### Scenario: 幂等写入

- **WHEN** 相同 `(model_id, river_network_version_id, river_segment_id, duration, method, sample_period_start, sample_period_end)` 的曲线已存在
- **THEN** 更新 q2-q100、parameters_json、quality_flag
- **AND** 不创建重复记录（UNIQUE 约束）

#### Scenario: QC 结果写入

- **WHEN** 频率曲线完成 QC
- **THEN** 在 `ops.qc_result` 表写入检查记录
- **AND** qc_checkpoint = `"flood_frequency"`
- **AND** checks_json 包含 sample_size_check、monotonicity_check、fit_validity_check

---

### Requirement: 模型版本更新时曲线重算

当新 model_instance 上线时，系统 SHALL 支持重算所有关联河段的频率曲线。

#### Scenario: 旧曲线标记 superseded

- **WHEN** 为新 model_id 重算频率曲线
- **THEN** 旧 model_id 的曲线 quality_flag 改为 `"superseded_by_model_upgrade"`
- **AND** 旧曲线不删除，保留审计和对比用途

#### Scenario: CLI 重算

- **WHEN** 执行 `nhms-flood fit-curves --model-id yangtze_shud_v13`
- **THEN** 为该模型所有河段 × 6 种 duration 重新拟合频率曲线
- **AND** 输出统计：total_segments, succeeded, failed, skipped

---

### Requirement: 频率引擎 CLI

系统 SHALL 提供 CLI 命令用于频率曲线拟合。

#### Scenario: 全模型拟合

- **WHEN** 执行 `nhms-flood fit-curves --model-id yangtze_shud_v12`
- **THEN** 为该模型所有河段计算频率曲线
- **AND** 输出进度条和最终统计

#### Scenario: 单河段拟合

- **WHEN** 执行 `nhms-flood fit-curves --model-id ... --segment-id ... --duration 24h`
- **THEN** 仅计算指定河段指定 duration 的频率曲线

#### Scenario: Dry-run 模式

- **WHEN** 执行 `nhms-flood fit-curves --model-id ... --dry-run`
- **THEN** 输出将要计算的河段列表和预估耗时
- **AND** 不写入数据库
