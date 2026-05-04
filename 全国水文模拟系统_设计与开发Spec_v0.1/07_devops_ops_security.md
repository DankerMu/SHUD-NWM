# 07. 部署、运维、安全与质量控制

版本：v0.1  
日期：2026-04-30

## 1. 环境划分

```text
dev       开发环境，允许 mock 数据和小流域样例。
staging   预生产，接近生产配置，使用受控数据子集。
prod      生产环境，仅发布 QC 通过的产品。
hpc       HPC 计算环境，通过 Slurm Gateway 接入。
```

## 2. 服务部署

建议容器化控制平面服务：

```text
api-service
orchestrator
slurm-gateway
metadata-worker
tile-publisher
frontend-web
```

HPC 侧可使用 Singularity/Apptainer 或模块环境固定 SHUD/rSHUD/依赖版本。

## 3. 日志

```text
业务日志：run_id、cycle_id、model_id、状态转换。
作业日志：Slurm stdout/stderr、SHUD 屏幕输出、解析日志。
审计日志：谁触发重跑、谁切换 active model、谁修改资料源配置。
```

## 4. 核心监控指标

```text
cycle_discovery_latency
raw_download_success_rate
forcing_generation_duration
shud_run_duration_by_model
shud_run_failure_rate
parser_failure_rate
published_product_latency
api_latency_p95
tile_cache_hit_rate
river_timeseries_ingest_rows_per_sec
```

## 5. 告警

- GFS/IFS 周期超过 SLA 未发布。
- 某个 active basin 连续两次 forecast failed。
- Analysis state 过旧。
- river_timeseries 入库量异常。
- 频率曲线缺失导致重现期无法计算。
- 对象存储写入失败。
- Slurm 队列积压超过阈值。

## 6. 数据质量控制

### 6.1 气象 forcing QC

- 降水非负。
- 温度范围合理。
- 相对湿度在 0–1。
- 风速非负。
- 气压范围合理。
- 时间轴连续或缺失明确标记。
- 单位转换记录在 lineage。

### 6.2 SHUD 输出 QC

- `.rivqdown` 存在且列数与 `N_riv` 一致。
- 时间步数与 run_manifest 一致。
- 不出现大规模 NaN/Inf。
- 关键出口河段流量不出现异常尖峰，异常只标记，不自动删除。

### 6.3 洪水频率 QC

- 样本年数满足最小阈值。
- 拟合参数有效。
- Q2 < Q5 < Q10 < Q20 < Q50 < Q100。
- 不满足时标记 `quality_flag`，前端显示“不可靠”或“不展示”。

## 7. 权限控制

| 角色 | 能力 |
|---|---|
| viewer | 查看已发布地图和曲线。 |
| analyst | 查看 QC 标识、历史版本、下载结果。 |
| operator | 触发重跑、取消作业、重新发布产品。 |
| model_admin | 注册模型版本、切换 active model。 |
| sys_admin | 管理数据源、用户、系统配置。 |

## 8. 安全边界

- 下载器只能访问白名单外部域和路径。
- Slurm Gateway 不接受任意命令。
- manifest 必须 schema 校验。
- Object URI 必须限制在系统 bucket/prefix。
- 不把数据源 token 写入作业日志。
- 前端只访问 API 和瓦片服务，不直接访问数据库。
