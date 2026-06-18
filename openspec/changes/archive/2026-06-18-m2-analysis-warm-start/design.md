## Context

M1 实现了 GFS forecast 冷启动闭环：GFS 下载 → canonical → forcing → SHUD forecast → 解析入库 → 前端曲线。但冷启动 forecast 缺乏真实初始状态，水文模型需要 spin-up 才能收敛，导致预报前期精度较差。

M2 引入 Analysis Run 和 Warm-start 机制：用 ERA5 再分析资料驱动 SHUD 持续运行，维护真实场状态（StateSnapshot），Forecast 从最新状态启动，前端展示过去 7 天（analysis）+ 未来 7 天（forecast）拼接曲线。

当前约束：
- ERA5 约 5 天迟滞，最近 5 天需用 GFS analysis/short forecast 或 GDAS 补位（CLDAS 权限未开通）
- StateSnapshot 与 hydro_run 存在循环依赖（run 产生 state，state 作为下一 run 的 init），init_state_id 不加外键
- M2 仅单流域验证，全国化在 M3

## Goals / Non-Goals

**Goals:**
- ERA5 adapter 可用，支持 CDS API cycle discovery + GRIB 下载 + canonical 转换
- Analysis run pipeline 可用，ERA5 forcing → SHUD analysis → StateSnapshot
- State Manager 可查询最近可用状态，forecast 可 warm-start
- 前端展示 analysis + forecast 拼接曲线
- best_available_selection 表有数据，来源可追溯

**Non-Goals:**
- CLDAS 接入（M6）
- IFS 多 scenario 对比（M4）
- 全国多流域并行（M3）
- ERA5T → final 自动版本替换（后续优化）
- Hindcast/Replay 批量回放（M5）

## Decisions

### D1: ERA5 下载方式选择 CDS API

| 选项 | 优点 | 缺点 |
|---|---|---|
| **CDS API（选定）** | 官方推荐，按需裁剪区域/变量/时间 | 请求排队可能慢 |
| earthkit | 更高级封装 | 额外依赖，CDS API 足够 |
| 直接 HTTP | 简单 | ERA5 不支持 NOMADS 式直接 HTTP |

ERA5 不像 GFS 有公开 HTTP 直下通道，CDS API 是唯一稳定途径。请求拆分（按天/按变量）+ 重试策略缓解排队问题。

### D2: ERA5 Canonical 转换——露点→RH、辐射累计量差分

ERA5 不直接提供 RH，提供 2m_temperature + 2m_dewpoint_temperature。RH 由露点公式计算：
```
e_s = 6.112 × exp(17.67 × T / (T + 243.5))
e_d = 6.112 × exp(17.67 × Td / (Td + 243.5))
RH  = clamp(e_d / e_s, 0, 1)
```

ERA5 辐射变量（ssr, str）为累计 J/m²，需按时段差分后除以秒数转 W/m²。ERA5 adapter metadata 声明 `accumulation_type='since_midnight'`，转换器按此执行差分。

### D3: Analysis Run 调度策略——ERA5 到达触发

Analysis run 不需要实时性，采用 ERA5 数据到达触发（非定时轮询）：
1. ERA5 adapter cycle discovery 发现新数据可用
2. 触发 ERA5 forcing production
3. 触发 SHUD analysis run
4. 生成 StateSnapshot

如果 ERA5 延迟超过阈值（如 7 天未更新），analysis run 使用 GFS analysis 补位，quality_flag 标记为 `degraded_source`。

### D4: StateSnapshot 存储设计

StateSnapshot 本质是 SHUD 的 `.cfg.ic` 文件（二进制/文本 initial condition），存储到对象存储 `states/{model_id}/{valid_time}/`。数据库 `hydro.state_snapshot` 记录元数据（state_id, model_id, run_id, valid_time, state_uri, checksum, usable_flag）。

usable_flag 的管理：
- Analysis run 成功后 state_snapshot 初始 usable_flag=false
- QC 检查通过后设为 true
- 简化 QC：检查 `.cfg.ic` 文件存在且大小 > 0，checksum 匹配
- 过旧（valid_time 距当前 > 配置阈值如 14 天）时，不主动禁用，但 forecast 标记 degraded

### D5: Forecast Warm-start 选择策略

```
SELECT state_id, state_uri, valid_time
FROM hydro.state_snapshot
WHERE model_id = $1
  AND usable_flag = true
  AND valid_time <= $2
ORDER BY valid_time DESC
LIMIT 1
```

找到 state 时：manifest 中 `initial_state.ic_file_uri` 填入 state_uri，`runtime.init_mode=3`。
无可用 state 时：fallback 到 cold-start（`runtime.init_mode=1`），run_manifest 中 `initial_state.quality='cold_start_no_state'`。
注意：init_mode 和 quality 信息存储在 run_manifest JSON 中（遵循 Appendix B 嵌套结构），不新增 hydro_run 表列。

### D6: 前端拼接策略

前端 forecast-series 接口返回两段数据：
- `analysis_true_field` scenario：过去 7 天
- `forecast_gfs_deterministic` scenario：未来 7 天

前端按 scenario 分颜色渲染，在 issue_time 处画分界线。时间重叠部分（如果有）以 forecast 为准。

### D7: best_available_selection 写入时机

每次 analysis run 完成后，遍历其 forcing 使用的 valid_time 范围，写入 best_available_selection：
- `selected_source`: 实际使用的数据源（ERA5 或 GFS fallback）
- `fallback_order`: 当时的优先级链路
- 采用 UPSERT 语义（ON CONFLICT (valid_time, variable) DO UPDATE）

## Risks / Trade-offs

| 风险 | 缓解 |
|---|---|
| ERA5 CDS API 请求排队时间不可控（可能数小时） | 请求拆分为按天批次 + 异步提交 + 超时重试；首次批量下载可手动辅助 |
| ERA5 约 5 天迟滞，最近时段无再分析数据 | GFS analysis/short forecast 补位 + quality_flag 标记 degraded_source |
| state_snapshot ↔ hydro_run 循环依赖 | init_state_id 不加 FK，应用层保证引用完整性 |
| 前端拼接 analysis + forecast 时间对齐 | 后端接口明确 scenario 和 valid_time，前端按 scenario 分段渲染 |
| ERA5T 后续被 final 替换可能导致版本混乱 | M2 暂不实现自动替换，记录 source_version 字段备用 |

## Open Questions

1. ERA5 下载区域裁剪参数——需确认中国区域 [55, 70, 15, 140] 是否覆盖所有流域
2. Analysis run 频率——按天（每天一个 ERA5 切片）还是按多天（如 7 天一跑，连续接力）？建议首批按天，后续可调
3. 首次 spin-up 策略——第一个 analysis run 无初始状态，需要 cold-start + 足够长的 spin-up 窗口（建议 ≥90 天 ERA5）
