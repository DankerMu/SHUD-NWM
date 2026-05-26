# QHH/有限流域 MVP 上线实施计划

最后更新：2026-05-26

## 结论

当前最短路径不是重做系统，而是把已有 QHH 真实链路、已有前端路由、已有 pipeline/Slurm 运维接口收敛成两个 MVP 入口：

- `hydro-met`：QHH/有限流域水文气象展示页。
- `ops`：系统运维页。

MVP 定义为“QHH/有限流域水文气象展示 + 运维监控”。水文展示主变量是河段流量 `q_down`，不是水位 `stage`。气象展示来自 SHUD forcing 代站的 `PRCP`、`TEMP`、`RH`、`wind`、`Rn`、`Press`。运维展示数据下载、canonical、forcing、SHUD、parse、publish 的阶段状态，并支持失败 run 重启。

## 已具备基础

QHH 真实链路已有可复用证据：

- `data/Basins/qhh` 已校准模型可运行。
- 原始 `qhh.tsd.forc` 已 seed 386 个 forcing 站点。
- 真实 GFS/IFS `2026052100` 与 `2026052106` 已完成四个 `frequency_done` run。
- `qhh_gfs_2026052100_smoke` 已解析入库 11431 行、1633 个 SHUD 输出河段。
- `scripts/run_qhh_cycle.sh`、`scripts/run_qhh_cycle.sbatch`、`scripts/run_qhh_continuous.py` 可作为诊断、回归复现和证据采集入口。
- M20 已提供正式 backend production scheduler 路径：`uv run nhms-pipeline plan-production`。

关键代码现状：

- 河段流量曲线 API 已实现：`/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}/forecast-series`。
- 站点清单 API 已实现：`/api/v1/met/stations`。
- `workers/forcing_producer` 已生成并写入 `met.forcing_station_timeseries`，变量覆盖 `PRCP`、`TEMP`、`RH`、`wind`、`Rn`、`Press`。
- pipeline 运维 API 已实现：`/api/v1/pipeline/status`、`/api/v1/pipeline/stages`、`/api/v1/jobs`、`/api/v1/jobs/{job_id}/logs`、`/api/v1/runs/{run_id}/retry`、`/api/v1/runs/{run_id}/cancel`、`/api/v1/queue/depth`。
- 前端已有 `/meteorology`、`/forecast`、`/segments/:segmentId`、`/monitoring` 等可复用页面和组件。

## M21 实施基线

M21 GitHub Epic 为 #202，OpenSpec change 为 `openspec/changes/m21-qhh-hydro-met-ops-mvp/`。下游 issue 默认使用这个基线，不再重新定义 MVP 范围：

| 项 | 基线 |
| --- | --- |
| 首选模型 | `basins_qhh_shud` |
| 首选流域版本 | `basins_qhh_vbasins` |
| 首选河网版本 | `basins_qhh_rivnet_vbasins` |
| GFS 基准周期 | `2026-05-21T00:00:00Z`，run `qhh_gfs_2026052100_smoke` |
| GFS forcing version | `forc_gfs_2026052100_basins_qhh_shud` |
| GFS 解析证据 | 11431 行、1633 个 SHUD 输出河段、状态 `frequency_done` |
| forcing 站点基线 | 原始 QHH 386 个 forcing 站点，ID `qhh_forc_001` 到 `qhh_forc_386` |
| 并行源基线 | GFS 为主源，IFS 为并行源；`2026052100` 与 `2026052106` 作为已有真实多周期证据 |
| IFS 时效边界 | `00/12 UTC` 可作为完整 7 天候选；`06/18 UTC` 不足 7 天时必须标注实际可用时效，典型为 144h |

这个基线不是最终生产 readiness 声明。后续实现 issue 可以使用 deterministic fixture、local PostgreSQL 或 opt-in live QHH smoke 证明功能；任何未执行的 live GFS/IFS/Slurm/browser 步骤都必须记录具体缺失依赖，不能默认为已通过。

## #214 evidence freeze

Issue #214 的证据冻结入口是 [`docs/runbooks/qhh-mvp-smoke-evidence.md`](../runbooks/qhh-mvp-smoke-evidence.md)。它不改变 MVP 范围，只把已有 QHH diagnostic evidence、deterministic browser evidence、static validation 和 skipped/blocked live dependency 分开编号。

当前状态：

- QHH GFS/IFS `2026052100`、`2026052106` 保持为 live diagnostic/reproduction evidence，不升级为 formal scheduler readiness。
- `/hydro-met` browser smoke 使用 mocked `/api/v1/**` deterministic evidence，覆盖 latest-product、station inventory、station-series 六个 forcing 变量、`q_down` forecast-series、GFS/IFS 和 IFS 144h shorter horizon。
- `/ops` controlled failure/retry evidence 继续引用 #213 deterministic runbook，不声明 live Slurm/QHH retry。
- OpenSpec、OpenAPI/API type、frontend test/build、markdown/static 和 opt-in live smoke 都必须在 #214 evidence matrix 中保留 command、artifact path、mode 和 claim boundary。
- 未执行的 live GFS/IFS/Slurm/browser/IdP/alert/rollback 步骤保持 skipped 或 blocked；不能作为内部 MVP 之外的 final production readiness 证明。

## 关键缺口

1. 气象代站真实曲线读取 API 缺失。OpenAPI 已声明 `/api/v1/met/stations/{station_id}/series`，但 FastAPI 当前未实现，测试中仍标记为 deferred。
2. `/meteorology?tab=stations` 当前使用前端 fixture contract 和 unavailable 状态，不消费真实 `met.forcing_station_timeseries`。
3. 河段曲线是 `q_down` 流量曲线，不能在 MVP 中称为水位曲线。
4. `/forecast` 默认河网加载是有限预览，不是全国全量河段展示。MVP 应聚焦 QHH/有限流域。
5. qhh 诊断脚本不能写成生产 scheduler dependency。正式运维闭环应对接 backend orchestrator 的 `plan-production` 路径。
6. 前端 retry/cancel 按钮当前依赖 dev role override，正式环境仍需 live IdP；内部 MVP 可先使用受控 operator header/dev role 验收，但要标明边界。

## MVP 范围

### P0 数据范围

| 项 | MVP 决策 |
| --- | --- |
| 流域 | QHH/有限流域 |
| 水文变量 | `q_down` 流量，单位 `m3/s` 或 `m³/s` |
| 气象变量 | `PRCP`、`TEMP`、`RH`、`wind`、`Rn`、`Press` |
| 气象来源 | GFS 主源，IFS 并行源 |
| IFS 时效 | `00/12 UTC` 可作为完整 7 天候选；`06/18 UTC` 不足 7 天时前端必须标注可用时效 |
| ERA5 | 不作为 MVP 近实时主源，可留作 analysis/history 后续 |
| CLDAS | 不纳入 MVP，显示 restricted 或隐藏 |
| 洪水频率曲线 | 不作为 MVP 阻塞；QHH 当前可继续标记 `no_frequency_curve` |
| 全国 MVT/PBF | 不纳入 MVP 阻塞 |
| final production readiness | 不纳入内部 MVP 阻塞 |

### 两个入口

`/hydro-met` 或现有页面收敛后的“水文气象展示”入口：

- 顶部选择：QHH、GFS/IFS、latest/cycle_time。
- 左侧：气象代站列表和河段列表。
- 中间：MapLibre 站点和河网。
- 右侧：站点 forcing 曲线和河段 `q_down` 曲线。
- 元信息：`source_id`、`cycle_time`、`forcing_version_id`、`run_id`、`valid_time` 范围、单位、`quality_flag`、不可用原因。

`/ops` 或现有 `/monitoring` 收敛后的“系统运维”入口：

- source/cycle selector。
- 总状态卡：download、convert、forcing、SHUD、parse、publish。
- stage 进度条。
- jobs 表。
- Slurm `job_id`、状态、开始/结束时间、耗时。
- 日志弹窗。
- failed/submission_failed/permanently_failed 的重启按钮。
- queue depth 和简单成功率/耗时趋势。

## 后端实施计划

### P0-1 实现 station series API

新增：

```text
GET /api/v1/met/stations/{station_id}/series
  ?forcing_version_id=
  &model_id=
  &source_id=GFS
  &cycle_time=2026-05-21T00:00:00Z
  &variables=PRCP,TEMP,RH,wind,Rn,Press
  &from=
  &to=
  &limit=2000
```

返回建议：

```json
{
  "station_id": "qhh_forc_001",
  "forcing_version_id": "forc_gfs_2026052100_basins_qhh_shud",
  "source_id": "gfs",
  "cycle_time": "2026-05-21T00:00:00Z",
  "model_id": "basins_qhh_shud",
  "series": [
    {
      "variable": "PRCP",
      "unit": "mm",
      "points": [
        {
          "valid_time": "2026-05-21T03:00:00Z",
          "value": 0.2,
          "quality_flag": "ok"
        }
      ],
      "truncated": false
    }
  ]
}
```

实现点：

- `packages/common/forecast_store.py` 增加 `station_series(...)`。
- `apps/api/routes/data_sources.py` 增加 `/met/stations/{station_id}/series`。
- `openapi/nhms.v1.yaml` 更新 schema，不再把该 route 标为 deferred。
- `apps/frontend/src/api/types.ts` 重新生成。
- 后端测试覆盖正常查询、变量过滤、时间范围、limit/truncated、站点不存在、forcing_version 不存在、`model_id + source_id + cycle_time` 自动解析 forcing_version。

### P0-2 验收 forcing station timeseries 完整性

当前 forcing producer 已写入 `met.forcing_station_timeseries`。MVP 要补的是可观测验收和索引：

- 验证 QHH 386 站点均有 `PRCP`、`TEMP`、`RH`、`wind`、`Rn`、`Press`。
- 验证每个变量的 `unit`、`native_resolution`、`quality_flag` 非空或有明确默认。
- 验证 GFS/IFS 四个已完成 forcing_version 均可查。
- 增加查询索引：

```sql
CREATE INDEX IF NOT EXISTS forcing_station_series_lookup_idx
ON met.forcing_station_timeseries
(station_id, variable, valid_time DESC);

CREATE INDEX IF NOT EXISTS forcing_station_series_source_cycle_idx
ON met.forcing_station_timeseries
(source_id, valid_time DESC);
```

如 station series API 需要按 forcing_version 高效查询，可优先使用现有主键 `(forcing_version_id, station_id, variable, valid_time)`，再根据实际 query plan 补 `(station_id, forcing_version_id, variable, valid_time DESC)`。

### P0-3 最新可展示产品聚合

新增轻量聚合接口，避免前端手工拼 `run_id`、`forcing_version_id`、`cycle_time`：

```text
GET /api/v1/mvp/qhh/latest-product?source=GFS
```

返回：

```json
{
  "basin_id": "basins_qhh",
  "model_id": "basins_qhh_shud",
  "basin_version_id": "basins_qhh_vbasins",
  "river_network_version_id": "basins_qhh_rivnet_vbasins",
  "source_id": "GFS",
  "cycle_time": "2026-05-21T00:00:00Z",
  "run_id": "qhh_gfs_2026052100_smoke",
  "forcing_version_id": "forc_gfs_2026052100_basins_qhh_shud",
  "station_count": 386,
  "segment_count": 1633,
  "status": "frequency_done"
}
```

可先由现有 `/api/v1/runs`、`/api/v1/models`、`met.forcing_version` 拼装，但建议后端提供稳定聚合，减少前端多接口竞态。

### P0-4 正式 pipeline/orchestrator 运维闭环

MVP 不把 `scripts/run_qhh_continuous.py` 写成生产依赖。正确路径：

- 继续保留 qhh 脚本作为诊断、复现、证据采集入口。
- 用 `nhms-pipeline plan-production --plan` 承担正式候选生成、preflight、提交、状态、重试和 readiness evidence。
- 确保 QHH cycle 的每个 stage 都写入 `ops.pipeline_job`：download、convert、forcing、forecast/SHUD、parse、frequency/publish。
- 确保 `log_uri` 指向 Slurm login node 可读的共享路径。
- controlled failure 后 `/api/v1/runs/{run_id}/retry` 能生成新 job 并刷新状态。

## 前端实施计划

### P0-5 水文气象展示页

推荐新增 `/hydro-met`，也可以先在 `/meteorology` 和 `/forecast` 上收敛导航入口。页面只暴露 MVP 范围，不展示未接入功能的入口。

数据流：

1. 请求 latest product。
2. 请求 `/api/v1/met/stations?model_id=basins_qhh_shud`。
3. 请求 QHH 河网和必要河段列表。
4. 点击站点时请求 station series。
5. 点击河段时请求 forecast-series，变量固定 `q_down`，scenarios 按 GFS/IFS 选择。

页面规则：

- 空数据不画假曲线。
- `quality_flag != ok` 的点要可视化或在 tooltip 中标注。
- station series 超 limit 时显示 `truncated`。
- IFS 时效不足 7 天时标明实际结束时间。
- 所有“水位”文案改为“流量”或“河段流量”。

### P0-6 运维页收敛

复用 `/monitoring`，或新增 `/ops` 作为简化入口。MVP 只保留：

- source/cycle selector。
- stage cards。
- jobs table。
- log modal。
- retry button。
- queue depth。
- success rate / duration 简图。

按钮逻辑：

```text
job.status in failed/submission_failed/permanently_failed
  -> 显示“重启”
  -> POST /api/v1/runs/{run_id}/retry
  -> 刷新 jobs/status/stages
```

内部 MVP 可以使用受控 dev role/header 验收。正式生产环境必须补 live IdP 后再开放真实操作权限。

## 验收标准

1. `GET /api/v1/met/stations?model_id=basins_qhh_shud` 返回 QHH 代站列表，数量接近 386。
2. 任一 QHH 站点 `GET /api/v1/met/stations/{station_id}/series` 返回 `PRCP`、`TEMP`、`RH`、`wind`、`Rn`、`Press`。
3. station series 返回真实 `forcing_version_id`、`source_id`、`cycle_time`、`unit`、`quality_flag`。
4. 任一可用 QHH 河段 forecast-series 返回非空 `q_down` 曲线。
5. GFS 和 IFS 曲线可同时显示；IFS 可用时效不足 7 天时明确标注。
6. 水文气象页面能从 latest product 自动加载，不要求用户输入 `run_id`。
7. 地图能显示站点和河段，点击后右侧曲线刷新。
8. 运维页能显示 download、convert、forcing、SHUD、parse、publish 阶段。
9. jobs 表能显示 Slurm `job_id`、status、started_at、finished_at、duration。
10. 日志弹窗能打开失败或成功 job 的 stdout/stderr。
11. 构造一个 failed run 后，点击“重启”能生成 retry job。
12. 一次完整 QHH GFS cycle smoke 从下载到前端展示全通过。

## 工作分解

### P0 必须完成

| 编号 | 工作 | 交付物 | 验收 |
| --- | --- | --- | --- |
| P0-1 | 冻结 MVP 数据范围 | QHH、GFS、IFS、`q_down`、6 个 forcing 变量 | 文档和前端文案一致 |
| P0-2 | station series API | `/api/v1/met/stations/{station_id}/series` | 返回真实点列、unit、quality_flag |
| P0-3 | QHH forcing timeseries 完整性验收 | 查询脚本/测试/索引 | 任一站点可查 6 个变量 |
| P0-4 | latest product 聚合 | `/api/v1/mvp/qhh/latest-product` 或等价聚合 | 前端无需手填 ID |
| P0-5 | 水文气象展示页 | `/hydro-met` 或收敛后的 `/meteorology` | 站点曲线 + 河段 `q_down` 曲线可展示 |
| P0-6 | 正式 pipeline job 对接 | QHH stage 写入 `ops.pipeline_job` | `/ops` 能看到真实流程 |
| P0-7 | retry 按钮闭环 | 前端调用 `/runs/{run_id}/retry` | controlled failed run 可重启 |
| P0-8 | 真实 smoke | 一轮 cycle 全链路 | 下载 -> forcing -> SHUD -> parse -> 展示 -> 运维全通 |

### P1 建议 MVP 前完成

| 编号 | 工作 | 说明 |
| --- | --- | --- |
| P1-1 | GFS/IFS raw mirror manifest | 防止外部源抖动影响 SHUD |
| P1-2 | IFS 06/18 时效标注 | 只到 144h 时必须提示 |
| P1-3 | 日志归档规范 | Slurm stdout/stderr 进入统一 `log_uri` |
| P1-4 | OpenAPI + frontend types | 避免前后端漂移 |
| P1-5 | 数据空值/QC 可视化 | 曲线断点、缺测区间、QC flag |
| P1-6 | E2E 测试 | `/hydro-met` + `/ops` browser smoke |

### P2 不阻塞 MVP

| 工作 | 原因 |
| --- | --- |
| 全国所有流域 | QHH/有限流域先行 |
| 水位 `stage` | 当前真实主变量是 `q_down` |
| CLDAS | 权限和自动下载能力未闭环 |
| ERA5 近实时 | 有迟滞，更适合 history/analysis |
| 真实全国 MVT/PBF | 需要目标环境 PostGIS/national proof |
| live IdP / alert sink / rollback proof | 属于最终 production readiness，不阻塞内部 MVP |

## 推荐实施顺序

1. 确认 QHH 最新真实 run、forcing_version、station_count、segment_count。
2. 实现 station series API。
3. 补 QHH forcing timeseries 完整性测试和必要索引。
4. 实现 latest product 聚合。
5. 改造水文气象展示页。
6. 让正式 orchestrator 路径写齐 QHH stage job/status/log。
7. 收敛 monitoring 为 MVP ops 页。
8. 做 controlled failure + retry 验收。
9. 跑完整 GFS/IFS QHH smoke。
10. 冻结 MVP 文档、演示脚本和 release checklist。

最关键的两件事是：先补真实 station series API，再把运维页绑定到正式 pipeline/orchestrator 产生的 job/status/log/retry。前者决定展示页是否真实可用，后者决定运维页是否真实可控。
