我看了 `DankerMu/SHUD-NWM`。我的判断是：**当前项目已经具备完整的“方案设计基线”，但还没有进入“可启动、可测试、可集成”的工程基线阶段**。下一步不要继续单纯扩展方案文档，而应该把文档中的设计固化成代码骨架、数据库 migration、OpenAPI、样例数据、Mock 流水线和 CI 验收。

仓库目前组织得比较清楚：根目录 README 已经把项目分为 `docs/spec`、`docs/modules`、`docs/appendices`、`docs/research`、`docs/report` 和 `design`，并明确有总体设计、模块规格、数据库、API、Slurm、前端、UI 规范、气象数据梳理和 8 张前端效果图等内容。 Spec 入口也明确列出了已确认产品约束，包括数据源分阶段接入、前端按原生分辨率展示、Slurm + HPC 调度、洪水频率/重现期、GFS/IFS 分 scenario、系统名称等。

但我也检查了常见工程入口，当前未看到根目录下的 `package.json`、`pyproject.toml`、`docker-compose.yml` 或 `.github/workflows/ci.yml`。这说明现在更偏“设计仓库”，还不是“开发仓库”。

---

## 一、当前做得比较好的地方

### 1. 总体架构方向是对的

总体设计已经把系统定义成“气象资料接入—forcing 生产—真实场状态运行—预报运行—结果入库—洪水重现期产品—地图展示”的稳定流水线，并强调不是一次性脚本，而是可审计、可追溯、可回滚、可扩展的业务平台。

这个定位非常关键，说明项目已经避免了两个常见错误：

第一，把 SHUD 运行当作单独模型脚本。  
第二，把前端地图当作简单结果展示页面。

你现在的设计已经更接近业务化水文预报平台。

### 2. Analysis / Forecast / Hindcast 三类运行已经区分清楚

总体设计里已经明确：

- `analysis run` 用真实场或再分析 forcing 连续更新模型状态；
    
- `forecast run` 从最近 `StateSnapshot` 启动未来 7 天预报；
    
- `hindcast / replay` 用于历史回放、频率样本生产、事件复盘，不覆盖业务预报产品。
    

这比最初只做“过去 7 天 + 未来 7 天”更成熟。后续洪水频率、模型评估、历史事件复盘都能复用同一套运行引擎。

### 3. 数据库分区思路合理

数据库设计已经拆成：

```text
core   流域、模型、版本
met    气象资料、周期、canonical 产品、forcing
hydro  SHUD 运行、状态快照、河段结果
flood  频率曲线和重现期结果
map    瓦片发布、图层、样式
ops    作业、日志、质量控制、审计
```

这个拆分是合理的。核心表中也已经覆盖 `basin`、`basin_version`、`model_instance`、`forecast_cycle`、`hydro_run`、`river_timeseries`、`flood_frequency_curve`、`state_snapshot`、`forcing_version`、`canonical_met_product`、`tile_layer` 等关键对象。

### 4. Slurm / HPC 的基本作业链完整

Slurm 设计已经明确 Web/API 不直接运行 SHUD，Slurm 是重计算唯一入口，并定义了 8 类作业：

```text
download_source_cycle
convert_canonical
produce_forcing_array
run_shud_analysis_array
run_shud_forecast_array
parse_output_array
compute_frequency_array
publish_tiles
```

同时已经有 job array、dependency、resource profile、workspace、状态回写、幂等性、失败处理和安全约束。

这是项目后续全国化的核心基础。

### 5. 路线图已经基本可执行

开发路线图已经按 0–6 阶段组织：

```text
阶段 0：项目初始化
阶段 1：GFS + 单/双流域 Forecast 闭环
阶段 2：Analysis run 与 warm-start
阶段 3：Slurm 全国化
阶段 4：IFS 与多 scenario
阶段 5：洪水频率 / 重现期产品
阶段 6：CLDAS restricted → enabled
```

最终验收指标也明确了 GFS/IFS scenario、analysis/forecast 拼接、洪水重现期、流域版本、瓦片性能、单流域失败不阻断、任意曲线点可追溯等要求。

这个路线图可以直接转成 GitHub Milestones 和 Issues。

---

## 二、当前最需要补强的地方

### 1. 需要从“文档仓库”升级为“工程仓库”

这是最高优先级。

现在 README 和 Spec 都很好，但还缺少最小可运行工程结构。我建议把仓库调整为如下结构：

```text
SHUD-NWM/
├── apps/
│   ├── api/                    # 后端 API 服务
│   └── web/                    # 前端 MapLibre Web 应用
│
├── services/
│   ├── orchestrator/           # 流水线编排服务
│   ├── slurm-gateway/          # Slurm 提交与状态同步
│   ├── tile-publisher/         # 瓦片发布服务
│   └── metadata-worker/        # 元数据、状态回写、发布索引
│
├── workers/
│   ├── data-adapters/          # GFS / IFS / ERA5 / CLDAS adapter
│   ├── canonical-converter/    # 标准气象产品转换
│   ├── forcing-producer/       # SHUD forcing 生产
│   ├── shud-runtime/           # SHUD 调用封装
│   ├── output-parser/          # .rivqdown / .rivystage 解析
│   └── flood-frequency/        # 重现期计算
│
├── packages/
│   ├── common/                 # 公共类型、错误码、配置
│   ├── schemas/                # JSON Schema / OpenAPI / Pydantic
│   └── nhms-client/            # 前端/脚本复用 API client
│
├── db/
│   ├── migrations/             # 正式 migration
│   ├── seeds/                  # 基础数据
│   └── fixtures/               # 测试样例
│
├── infra/
│   ├── docker-compose.yml      # 本地开发环境
│   ├── minio/
│   ├── postgres/
│   ├── slurm-mock/
│   └── observability/
│
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── e2e/
│   └── golden/
│
├── docs/
└── design/
```

你现在已有的 `docs/` 和 `design/` 可以保留，但必须补出 `apps/`、`services/`、`workers/`、`db/`、`infra/` 和 `tests/`。

否则后续团队很容易停留在“方案很完整，但不知道从哪里开始写代码”。

---

### 2. 数据库设计需要从“草案”变成“可执行 migration”

现在 `03_database_design.md` 和 `C_database_schema_draft.md` 已经有 SQL 草案，但还不是正式 migration。数据库草案也明确说明“正式建表脚本应通过 migration 工具管理”。

建议下一步补：

```text
db/migrations/
├── 000001_create_extensions.sql
├── 000002_create_schemas.sql
├── 000003_core_basin_model.sql
├── 000004_met_source_forcing.sql
├── 000005_hydro_run_timeseries.sql
├── 000006_flood_frequency.sql
├── 000007_map_tile_layer.sql
├── 000008_ops_qc_event_audit.sql
└── 000009_indexes_constraints.sql
```

目前数据库设计里有几个需要修正的点：

#### 2.1 `river_timeseries` 需要解决河段复合主键问题

当前 `core.river_segment` 的主键是：

```sql
PRIMARY KEY (river_segment_id, river_network_version_id)
```

但 `hydro.river_timeseries` 只存了：

```sql
river_segment_id
```

这会带来跨河网版本冲突。你要么把 `river_segment_id` 设计成全局唯一，比如：

```text
yangtze_rivnet_v12_riv_000001
```

要么在 `river_timeseries` 里补充：

```sql
river_network_version_id TEXT NOT NULL
```

并建立复合外键：

```sql
FOREIGN KEY (river_segment_id, river_network_version_id)
REFERENCES core.river_segment(river_segment_id, river_network_version_id)
```

我更建议采用 **全局唯一 river_segment_id + 显式存 river_network_version_id**，这样查询和追溯都清楚。

#### 2.2 `MetStation` 表需要正式纳入数据库核心设计

你在数据关系图和 forcing 模块中已经强调气象代站与 `BasinVersion / ModelInstance` 绑定，但 `03_database_design.md` 的 16 张核心表里没有完整的 `met.met_station` 表。forcing 模块 Spec 提到了 `met.forcing_station`、`met.interp_weight`、`met.forcing_station_timeseries`，但核心数据库文档没有完整定义。

建议补：

```sql
CREATE TABLE met.met_station (
  station_id TEXT PRIMARY KEY,
  basin_version_id TEXT NOT NULL REFERENCES core.basin_version(basin_version_id),
  model_id TEXT NOT NULL REFERENCES core.model_instance(model_id),
  station_name TEXT,
  geom geometry(Point, 4490) NOT NULL,
  elevation_m DOUBLE PRECISION,
  station_role TEXT NOT NULL DEFAULT 'forcing_proxy',
  active_flag BOOLEAN NOT NULL DEFAULT true,
  properties_json JSONB NOT NULL DEFAULT '{}',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

再补：

```sql
CREATE TABLE met.interp_weight (
  weight_id BIGSERIAL PRIMARY KEY,
  source_id TEXT NOT NULL,
  grid_id TEXT NOT NULL,
  model_id TEXT NOT NULL,
  station_id TEXT NOT NULL REFERENCES met.met_station(station_id),
  variable TEXT NOT NULL,
  grid_cell_id TEXT NOT NULL,
  weight DOUBLE PRECISION NOT NULL,
  method TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (source_id, grid_id, model_id, station_id, variable, grid_cell_id)
);
```

这对性能非常关键，因为你后面全国化不能每次重新算格点到代站权重。

#### 2.3 外键和状态枚举需要更严格

目前多个字段还只是 `TEXT`，例如：

```text
hydro_run.basin_version_id
hydro_run.forcing_version_id
hydro_run.init_state_id
hydro_run.status
hydro_run.run_type
met.data_source.status
```

建议至少做两件事：

第一，补全外键。

第二，统一状态枚举或 lookup 表，例如：

```sql
CREATE TYPE hydro.run_status AS ENUM (
  'created',
  'queued',
  'running',
  'succeeded',
  'partially_succeeded',
  'failed',
  'cancelled',
  'published',
  'deprecated'
);
```

否则一旦业务运行后，`success`、`succeeded`、`done`、`complete` 这类状态混用会非常麻烦。

---

### 3. OpenAPI 还需要从“接口清单”变成“完整契约”

现在 `04_api_design.md` 有核心 API 清单、河段曲线响应、瓦片接口、模型资产接口、运维监控接口、洪水预警接口、角色和性能要求。 但 `E_api_openapi_draft.md` 目前只是很短的 OpenAPI 片段。

建议补一个完整的：

```text
openapi/nhms.v1.yaml
```

并包含：

```text
components.schemas
components.parameters
components.responses
components.securitySchemes
components.examples
```

重点补这些 schema：

```text
Basin
BasinVersion
ModelInstance
RiverSegment
MetStation
DataSource
ForecastCycle
ForcingVersion
HydroRun
RiverSeriesResponse
MetStationSeriesResponse
FloodFrequencyCurve
ReturnPeriodResult
TileLayer
PipelineStatus
SlurmJob
QcResult
ErrorResponse
```

另外，几个接口建议调整：

#### 3.1 河段接口必须带版本上下文

现在有：

```http
GET /api/v1/river-segments/{segment_id}
```

建议改为支持：

```http
GET /api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}
```

或者 query 中强制带：

```http
GET /api/v1/river-segments/{segment_id}?river_network_version_id=
```

否则流域版本切换后，前端点击历史河段会有歧义。

#### 3.2 增加数据血缘接口

系统最重要的卖点之一是可追溯。建议补：

```http
GET /api/v1/lineage/river-point?run_id=&segment_id=&valid_time=&variable=
GET /api/v1/lineage/forcing-point?forcing_version_id=&station_id=&valid_time=&variable=
GET /api/v1/lineage/product/{product_id}
```

返回内容包括：

```text
source_id
cycle_time
canonical_product_id
forcing_version_id
model_id
state_id
run_id
parser_job_id
qc_result_ids
published_layer_id
```

这样才能满足路线图里“任意前端曲线点可追溯到 run_id、forcing_version、source cycle”的最终验收要求。

#### 3.3 增加提交类接口的幂等键

对这些接口：

```http
POST /api/v1/hindcast/submit
POST /api/v1/jobs/{run_id}/retry
POST /api/v1/jobs/{run_id}/cancel
PUT /api/v1/models/{model_id}/active
```

建议支持：

```http
Idempotency-Key: xxx
```

避免运维页面重复点击导致重复提交 Slurm 作业。

---

### 4. Run Manifest 需要升级为正式 JSON Schema

附录 B 已经说明 run manifest 是 HPC 作业唯一输入契约，并给出了 forecast manifest 示例和校验规则。 这个方向非常好，但还需要进一步工程化。

建议补：

```text
schemas/run_manifest.schema.json
schemas/run_status.schema.json
schemas/qc_result.schema.json
schemas/slurm_job.schema.json
```

并在 CI 里校验所有示例 manifest。

Manifest 建议增加以下字段：

```json
{
  "schema_version": "1.0",
  "created_by": "orchestrator",
  "created_at": "2026-05-03T08:00:00Z",
  "request_id": "req_xxx",
  "trace_id": "trace_xxx",
  "idempotency_key": "idem_xxx",
  "workspace": {
    "local_root": "/work/nhms/run_workspace/...",
    "cleanup_policy": "keep_7_days"
  },
  "resource_profile": {
    "partition": "compute",
    "cpus_per_task": 32,
    "memory_gb": 128,
    "walltime": "06:00:00"
  },
  "qc_policy": {
    "block_on_forcing_qc_error": true,
    "block_on_output_shape_error": true,
    "allow_warning_publish": true
  }
}
```

现在的 manifest 对 SHUD 运行足够，但对调度、审计、资源画像、QC 阻断和清理策略还不够。

---

### 5. Slurm Gateway 需要补“本地 Mock 模式”

Slurm 设计已经很好，但开发初期不可能每个开发者都有 HPC。建议新增：

```text
infra/slurm-mock/
services/slurm-gateway/mock_backend.py
```

支持两种 backend：

```yaml
slurm_gateway:
  backend: mock   # mock | slurm
```

Mock 模式下：

```text
submit_job()
cancel_job()
get_job_status()
fetch_logs()
```

都返回模拟状态，并能驱动前端第 8 张“任务调度与运行监控”页面。

这样阶段 0 就能交付一个完整的本地演示环境，不必等 HPC 接入。

---

### 6. Forcing 生产模块需要补“变量转换科学细节”

现在 forcing 模块 Spec 已经要求生成：

```text
PRCP, TEMP, RH, wind, Rn, Press
```

并输出 `.tsd.forc + CSV`。 数据产品文档也定义了标准变量和 SHUD forcing 对应关系。

但这里还需要补更细的科学和工程规则：

#### 6.1 降水单位和时间步必须严格定义

SHUD forcing 中 `PRCP` 是按什么单位？文档里写了 `mm/day 或按 SHUD 配置换算`，这个还不够硬。

建议统一规定内部 canonical 为：

```text
prcp_amount_mm_per_step
```

forcing 输出时再根据 SHUD 配置转换：

```text
PRCP = mm/day
```

或项目约定的 SHUD 输入单位。必须写明：

```text
GFS APCP 累计量 → 相邻 forecast hour 差分 → 时段降水
负差分 → QC 标记 / 置零策略
小时降水 → SHUD PRCP 单位换算
```

#### 6.2 Rn 净辐射要明确计算方案

很多数据源直接没有 `net_radiation`。建议在文档中把 `Rn` 分成三级来源：

```text
Level 1: 数据源直接提供 net radiation
Level 2: shortwave_down + longwave_down - upward components 推算
Level 3: 使用短波辐射 + 经验公式近似
```

并写入：

```text
lineage_json.radiation_method
quality_flag
```

否则模型结果中的 ET 和径流会受明显影响，但前端用户不知道 forcing 的不确定性。

#### 6.3 相对湿度 RH 的来源要分清

CLDAS 可能提供比湿，GFS/IFS 可能有相对湿度或比湿。建议定义：

```text
如果 source 提供 RH：直接转换到 0–1
如果 source 提供 specific humidity：用温度、气压计算 RH
如果缺气压：使用 surface_pressure 或标准大气近似，并标记 quality_flag
```

---

### 7. SHUD Runtime 需要补最小可运行样例

当前设计对 SHUD 运行逻辑是完整的，但还缺一个“能跑通的样例流域”。

建议尽快新增：

```text
examples/
├── basins/
│   └── demo_yangtze_subbasin/
│       ├── model_package/
│       ├── forcing_sample/
│       ├── expected_output/
│       └── README.md
```

最小样例不需要大流域，甚至可以是：

```text
几十个 mesh 单元
几十条河段
3–5 个气象代站
24–72 小时 forcing
```

并配一个命令：

```bash
nhms-demo run-forecast --example demo_yangtze_subbasin
```

验收标准：

```text
1. 生成 run_manifest.json
2. 生成 .tsd.forc + CSV
3. 调用 SHUD 或 mock SHUD
4. 输出 .rivqdown / .rivystage
5. 入库 river_timeseries
6. API 查询曲线
7. 前端页面显示
```

这会让项目从“设计方案”进入“能跑的最小闭环”。

---

### 8. Output Parser 需要补 fixtures 和 golden tests

输出解析模块已经要求解析 CSV 或 DAT、`.rivqdown` 从 `m3/d` 转 `m3/s`、检查列数与河段数一致、支持单独重跑。

建议补测试夹具：

```text
tests/fixtures/shud_outputs/
├── valid_rivqdown_ascii/
├── valid_rivystage_ascii/
├── missing_rivqdown/
├── wrong_column_count/
├── contains_nan/
├── contains_inf/
├── binary_output_sample/
└── expected_river_timeseries.parquet
```

并明确 parser 的行为：

```text
缺文件：failed_output_check，阻断入库
列数不一致：failed_output_shape，阻断入库
局部 NaN：quality_flag=warning，可入库但不发布或标注
单位转换：保留 raw_value 和 converted_value 是否需要？
重复解析：先 delete run_id 对应旧结果，或用 upsert
```

我建议 `river_timeseries` 增加：

```sql
raw_value DOUBLE PRECISION,
raw_unit TEXT,
parser_version TEXT
```

这样后续发现单位或解析规则问题时，可以定位影响范围。

---

### 9. 洪水频率模块需要补“方法学规范”

目前洪水频率模块要求生成年最大值或 POT 样本，拟合 P-III/GEV/POT-GPD，保存 Q2/Q5/Q10/Q20/Q50/Q100，对预报 Qmax 计算 return period。

这还需要变成一份更严格的“水文频率产品规范”。

建议新增：

```text
docs/spec/10_flood_frequency_methodology.md
```

内容包括：

```text
1. 默认方法：P-III annual maximum，或 GEV annual maximum
2. 可选方法：POT + GPD
3. 样本要求：最小年数、缺测比例、异常年处理
4. 时间尺度：1h / 3h / 6h / 24h / 72h / 7d
5. 汛期/全年样本选择规则
6. Q2–Q100 单调性修正规则
7. 超出 Q100 的外推边界
8. return_period 插值方法
9. warning_level 映射规则
10. 不确定性和置信区间
11. quality_flag 规则
12. 新模型版本上线后频率曲线重算规则
```

尤其要补 `duration` 语义。现在 `flood_frequency_curve` 有 `duration` 字段，但还没定义每个 duration 怎么从时间序列提取。例如：

```text
duration = 1h: 小时流量最大值
duration = 24h: 24 小时滑动平均流量最大值
duration = 72h: 72 小时滑动平均流量最大值
```

对洪水预警来说，这个非常重要。

---

### 10. 前端还需要补“接口契约 + 状态机 + 空数据状态”

前端页面规格和 UI 图已经比较完整。README 里也把 8 张效果图和前端文档做了对应关系。

现在要补的不是更多效果图，而是：

```text
1. 每个页面的数据加载流程
2. 每个组件的 API contract
3. loading / empty / error / degraded 状态
4. 图层有效时间 valid_times[] 的状态管理
5. GFS/IFS/Best Available 切换后的缓存策略
6. QC 标识和数据血缘展示方式
7. 大规模河网瓦片交互性能策略
```

建议在前端 Spec 里增加一节：

```text
页面状态机
```

例如河段曲线页：

```text
idle
  → loadingSegment
  → loadingSeries
  → loaded
  → partialLoaded
  → error
  → staleData
```

以及空状态文案：

```text
无可用预报：该河段当前周期未发布
IFS 时效不足：06/18 UTC 周期仅可展示至 144h
频率曲线缺失：该河段当前模型版本未生成频率曲线
QC 未通过：forcing 时间轴不连续，结果未发布
```

这些空状态对真实系统非常关键。

---

### 11. 质量控制已经有框架，但要补“可执行 QC 规则”

DevOps 文档已经写了 QC 触发时机、阻断规则、QC 结果表、告警与人工复核。 这很好。

下一步要把 QC 规则变成可执行配置：

```yaml
qc:
  canonical:
    required_variables:
      - prcp
      - temp
      - rh
      - wind
      - rn
    temp_degC_range: [-60, 60]
    rh_range: [0, 1]
    wind_speed_range: [0, 80]
    prcp_non_negative: true

  forcing:
    max_missing_ratio_per_station: 0.02
    max_consecutive_missing_steps: 3
    block_on_required_variable_missing: true

  shud_output:
    required_files:
      - rivqdown
      - rivystage
    max_nan_ratio: 0.001
    require_column_count_match: true

  flood_frequency:
    min_sample_years:
      q20: 20
      q50: 30
      q100: 40
    enforce_monotonic_thresholds: true
```

并补 CLI：

```bash
nhms-qc check-canonical --cycle-id
nhms-qc check-forcing --forcing-version-id
nhms-qc check-shud-output --run-id
nhms-qc check-frequency --model-id
```

这样 QC 才能真正进流水线。

---

### 12. 需要建立“问题、里程碑、验收”闭环

当前路线图已经有阶段和验收，但 GitHub Issues 为空。建议马上把路线图拆成 Milestones 和 Issues。

建议 Milestones：

```text
M0 工程骨架与本地可启动
M1 GFS + 单流域 Forecast 闭环
M2 Analysis + Warm Start
M3 Slurm 全国化
M4 IFS + 多 Scenario
M5 洪水频率 / 重现期
M6 CLDAS 启用与 Best Available
```

每个 Milestone 下面建 Epic，例如 M1：

```text
Epic: GFS Adapter
Epic: Canonical Converter
Epic: Forcing Producer
Epic: Model Registry
Epic: SHUD Runtime Adapter
Epic: Output Parser
Epic: River Timeseries API
Epic: Frontend 河段点击曲线
Epic: MVP Demo Dataset
```

每个 Issue 都绑定：

```text
输入
输出
验收标准
测试要求
依赖模块
相关文档链接
```

这样团队开发时不会迷失在大量设计文档里。

---

## 三、我建议优先补的 12 个文件 / 目录

按优先级，我建议下一轮直接补这些：

```text
1. pyproject.toml 或 package.json
2. docker-compose.yml
3. .github/workflows/ci.yml
4. db/migrations/000001_init.sql
5. schemas/run_manifest.schema.json
6. openapi/nhms.v1.yaml
7. infra/minio/README.md
8. infra/slurm-mock/README.md
9. examples/demo_basin/
10. tests/fixtures/
11. scripts/dev_bootstrap.sh
12. docs/adr/
```

其中最关键的是：

```text
docker-compose.yml
db/migrations/
schemas/
openapi/
examples/
tests/
```

这几个一补，项目就能从“设计阶段”进入“开发阶段”。

---

## 四、建议的下一版仓库目标：v0.3

我建议你把下一版定义成：

```text
SHUD-NWM v0.3：工程化启动基线
```

目标不是完成业务功能，而是做到：

```text
1. 本地能启动 PostGIS / TimescaleDB / MinIO / Redis / API stub / Web shell
2. 数据库 migration 可重复执行
3. Run Manifest JSON Schema 可校验
4. OpenAPI 可生成 API 文档
5. 有一个 demo basin 样例
6. 有 mock GFS cycle
7. 有 mock Slurm pipeline
8. 前端 8 个页面至少能用 mock API 加载骨架
9. CI 能跑 lint / schema validate / migration check / unit tests
10. README 增加“一键启动开发环境”
```

建议 v0.3 验收命令设计成：

```bash
git clone https://github.com/DankerMu/SHUD-NWM.git
cd SHUD-NWM
./scripts/dev_bootstrap.sh
docker compose up
make migrate
make seed-demo
make run-mock-cycle
```

然后浏览器打开：

```text
http://localhost:3000
```

能看到前端首页，并能点击一个 demo 河段显示 mock 曲线。

---

## 五、最终建议

你的方案现在最大的问题不是架构不完整，而是**工程落地边界还没有冻结**。

我建议下一步按这个顺序做：

```text
第一优先级：工程骨架
  docker-compose、db migrations、schemas、OpenAPI、CI

第二优先级：最小样例流域
  demo basin、mock GFS、mock forcing、mock SHUD output、golden tests

第三优先级：M1 闭环
  GFS adapter → canonical → forcing → Slurm/mock Slurm → SHUD/mock SHUD → parser → API → frontend

第四优先级：真实 SHUD 接入
  SHUD container / Apptainer、真实 model package、state_to_ic、output parser fixtures

第五优先级：科学产品规范
  洪水频率方法学、forcing 变量转换规范、QC 可执行规则
```

一句话总结：**SHUD-NWM 的设计已经够完整，下一步要避免继续“扩文档”，而是把它压成一个可运行的 v0.3 工程基线。**