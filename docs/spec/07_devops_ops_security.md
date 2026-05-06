# 07. 部署、运维、安全与质量控制

版本：v0.2  
日期：2026-05-06

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
- 不满足时标记 `quality_flag`，前端显示”不可靠”或”不展示”。

### 6.4 QC 流水线集成规范

#### 6.4.1 QC 触发时机

| QC 检查点 | 触发阶段 | 触发条件 | 对应数据流转图位置 |
|---|---|---|---|
| 原始资料完整性 QC | download 完成后 | raw data 落盘成功 | 数据接入与标准化阶段 |
| Canonical 转换 QC | canonical convert 完成后 | 变量名/单位/时间轴合规检查 | 数据接入与标准化阶段 |
| Forcing QC | forcing production 完成后 | 6.1 中全部检查项 | Forcing 生产阶段 |
| SHUD 输出 QC | output parser 完成后 | 6.2 中全部检查项 | 输出解析阶段 |
| 洪水频率 QC | frequency 计算完成后 | 6.3 中全部检查项 | 输出解析阶段 |

#### 6.4.2 阻断规则

| QC 失败类型 | 阻断行为 | 说明 |
|---|---|---|
| 原始资料文件数不足 | 阻断该 cycle 的 canonical 转换 | 可配置最小文件数阈值 |
| Canonical 变量缺失 | 阻断该 cycle 的 forcing 生产 | 按必选/可选变量分级 |
| Forcing 时间轴不连续 | 阻断该 basin 的 SHUD 运行 | 不阻断其他 basin |
| SHUD 输出列数不一致 | 阻断该 run 的入库和频率计算 | 保留原始输出 |
| 频率曲线非单调 | 该河段频率曲线标记为 unreliable，不阻断发布 | 前端显示质量标记 |

对于非阻断型失败，标记 quality_flag 但继续下游流程。

#### 6.4.3 QC 结果存储

```sql
CREATE TABLE ops.qc_result (
  qc_id BIGSERIAL PRIMARY KEY,
  qc_checkpoint TEXT NOT NULL,
  target_type TEXT NOT NULL,
  target_id TEXT NOT NULL,
  run_id TEXT,
  cycle_id TEXT,
  passed BOOLEAN NOT NULL,
  severity TEXT NOT NULL DEFAULT 'info',
  checks_json JSONB NOT NULL,
  message TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX qc_result_target_idx ON ops.qc_result (target_type, target_id, created_at DESC);
```

checks_json 示例：

```json
{
  “checks”: [
    {“name”: “prcp_non_negative”, “passed”: true},
    {“name”: “temp_range”, “passed”: true},
    {“name”: “rh_range”, “passed”: false, “detail”: “station HMT-Y2-0234: RH=1.05 at 2026-05-01T06:00Z”},
    {“name”: “time_continuity”, “passed”: true}
  ],
  “summary”: “3/4 passed”
}
```

#### 6.4.4 QC 告警与人工复核

- severity 为 error 的 QC 失败自动触发告警（邮件/webhook）。
- severity 为 warning 的记录到 qc_result，运维仪表盘可见。
- 人工复核通过运维监控页面，operator 可查看 QC 详情并决定是否手动放行。
- 手动放行接口：`POST /api/v1/qc/{qc_id}/override`，需 operator 角色，记录操作审计。

> **可执行 QC 规则的落地计划**：当前本节定义了 QC 的触发时机、阻断逻辑、结果存储和人工复核流程。具体的可执行 QC 规则（变量范围阈值、缺测比例上限、列数校验等）和 QC CLI 工具（`nhms-qc check-canonical`、`nhms-qc check-forcing` 等）将在 M0/M1 工程落地阶段以 YAML 配置文件形式补充。

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
