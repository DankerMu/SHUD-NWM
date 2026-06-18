## Context

M0 交付了项目骨架、25 张数据库表（6 schema、4 ENUM、4 hypertable）、OpenAPI 契约、Mock Slurm Gateway、Demo 种子数据和 CI 流水线。系统具备基本运行条件，但无真实数据流入和预报产出能力。

M1 需要在此基础上打通从 GFS 气象数据获取到前端预报曲线展示的完整 Forecast 闭环。目标流域：长江流域 demo（M0 种子数据），目标规模：1 个 GFS 周期（00Z 或 12Z），7 天预报时效，10-50 条河段。

**约束**：
- M1 仅接入 GFS 单一数据源，不接 IFS/ERA5/CLDAS
- 使用 Mock Slurm Gateway（`backend=mock`），不接真实 Slurm 集群
- Cold-start 运行（无 `.cfg.ic` 初始状态），不实现 Analysis warm-start
- 前端最小化：河网底图 + 河段点击 + 曲线，不实现时间轴、图层树、scenario 对比等完整功能

## Goals / Non-Goals

**Goals:**
- GFS 00/06/12/18 UTC 四个周期可被发现，指定周期的 GRIB2 数据可下载
- 下载的 GRIB2 转为 7 个标准变量的 canonical 产品
- Canonical 产品经插值生成 SHUD `.tsd.forc` forcing 文件
- Demo model_instance 注册并可被 SHUD runtime 使用
- SHUD `shud_omp` 执行并产生 `.rivqdown` 输出
- `.rivqdown` 解析为 m³/s 流量入库 `hydro.river_timeseries`
- Slurm 作业链五阶段按依赖顺序串行执行（mock 模式）
- 前端点击河段弹出 7 天预报流量曲线

**Non-Goals:**
- IFS/ERA5/CLDAS 数据源接入（M4/M2/M6）
- Analysis run 和 warm-start（M2）
- 多流域并行提交和 partial success（M3）
- 洪水频率/重现期计算（M5）
- Scenario 对比曲线（M4）
- 前端时间轴、图层树、底图切换等完整 GIS 功能（横切任务）
- 真实 Slurm 集群对接（M3）

## Decisions

### D1: GFS 下载策略——直接 HTTP 轮询 NOMADS

**选型**：通过 HTTPS 直接下载 `nomads.ncep.noaa.gov` 的 GRIB2 文件，按 latency_rule 轮询直到所有 forecast hours 可用。

**备选方案**：
- AWS Open Data（S3 直接拉取）：延迟更低但需额外 AWS 凭证管理
- GDS 订阅推送：需要申请，不适合初期快速验证

**理由**：NOMADS 是公开数据源，无需额外权限，适合 M1 最小闭环验证。后续可在 adapter 层替换下载通道。

### D2: Canonical 格式——NetCDF4

**选型**：canonical 产品使用 NetCDF4 格式存储。

**备选方案**：
- Zarr：适合云存储分块读取，但 M1 单流域无此需求
- COG (Cloud-Optimized GeoTIFF)：适合瓦片但不适合时序

**理由**：NetCDF4 是气象领域标准格式，xarray/cfgrib 原生支持，开发成本最低。存储在对象存储中用标准 `s3://` URI 引用。

### D3: 格点→代站插值——IDW 反距离加权

**选型**：格点到气象代站使用 IDW（Inverse Distance Weighting）插值，权重预计算后存入 `met.interp_weight` 表。

**备选方案**：
- Bilinear：精度略高但实现复杂度增加
- Nearest neighbor：太粗糙

**理由**：IDW 实现简单且满足 M1 验证需求。权重预计算后，每次 forcing 生产只需矩阵乘法，性能无瓶颈。M3 全国化后可升级插值方法。

### D4: SHUD 执行方式——直接 subprocess 调用 shud_omp

**选型**：SHUD runtime adapter 通过 `subprocess.run()` 调用 `shud_omp` 可执行文件。

**理由**：M1 使用 Mock Slurm Gateway，实际执行在本地或 Docker 中。sbatch 模板定义好，但实际提交通过 mock gateway 模拟。为 M3 真实 Slurm 对接预留接口。

### D5: 前端技术栈——MapLibre GL JS + ECharts

**选型**：地图使用 MapLibre GL JS，预报曲线使用 ECharts。

**备选方案**：
- Leaflet + D3：灵活但开发量大
- Mapbox GL JS：非开源，有 token 限制

**理由**：MapLibre 开源免费，矢量瓦片性能好。ECharts 中文社区活跃，时序曲线功能成熟。参见 `docs/spec/06_frontend_gis_design.md` 和 `docs/spec/06B_frontend_ui_design_spec.md`。

### D6: 作业链编排——Lazy Submission + Mock Gateway

**选型**：M1 的 5 个作业阶段使用 lazy submission（每个 stage 成功后才提交下一个），通过 Mock Slurm Gateway 执行。

**备选方案**：
- 预提交全部 + `afterok` 依赖：Slurm 原生支持，但 failure 语义与 "不提交后续 stage" 矛盾
- 调度器全量编排（Airflow/Prefect）：过重，M1 不需要

**理由**：M1 单流域单周期无需 job array 或并行扇出。Lazy submission 语义清晰：成功才继续、失败即停止。M3 全国化时可升级为 job array + partial success。

## Risks / Trade-offs

- **[GFS 数据不可用]** → 缓解：提供 mock GFS 数据生成器，可在无网络环境下运行完整闭环
- **[SHUD 可执行文件不可用]** → 缓解：提供 mock shud_omp 脚本，生成固定格式的 `.rivqdown` 输出
- **[GRIB2 解析性能]** → 缓解：M1 只处理单个周期，性能不是瓶颈；cfgrib + xarray 是成熟方案
- **[前端 MapLibre 初始化复杂度]** → 缓解：M1 只需最小页面（底图+河段图层+点击弹窗），不实现完整 8 页面体系
- **[Mock Gateway 与真实 Slurm 行为差异]** → 缓解：mock 接口严格按 `docs/spec/05_slurm_hpc_design.md` 定义的状态机实现；M3 切换时只需替换 backend

## Open Questions

- GFS 下载是否需要在 M1 阶段就实现 fallback 多通道？**决定：M1 只实现 NOMADS 单通道**，fallback 在 M3 全国化时添加。
- 前端是否在 M1 就使用矢量瓦片加载河网？**决定：M1 直接加载 GeoJSON**（demo 河段 < 50 条），M3 全国化时切换瓦片。
- `.rivqdown` 时间步长是否与 forcing 时间步长一致？需要确认 SHUD 配置中 `model_output_interval` 的默认值。

## 审核修复记录

以下 P0 问题已在 Stage 4 修复中解决：

1. **DB 列名全面对齐**：所有 spec 和 tasks.md 中的表名、列名、FK 引用已对齐 `docs/spec/03_database_design.md` 权威定义
2. **ENUM 值对齐**：hydro.run_status（created→staged→submitted→running→succeeded→parsed→...）和 met.cycle_status（discovered→downloading→raw_complete→...）
3. **API 路径修正**：forecast-series 路径改为 `/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}/forecast-series`
4. **river_timeseries.variable**：统一为 `q_down`（非 `discharge`），对齐 DB §6.1 查询模式
5. **forcing_station_timeseries**：改为长表格式（variable/value/unit 每行一条）
6. **model_instance**：使用 `active_flag` 布尔值，非状态枚举
7. **Slurm 提交策略**：改为 lazy submission（逐 stage 提交），解决 afterok 与 failure-abort 矛盾
8. **hydro_run 生命周期**：orchestrator 统一创建和管理，runtime/parser 只更新自己负责的状态转换
9. **wind 公式**：明确 sqrt(u² + v²)
10. **tasks.md 结构**：新增 Group 0（基础设施/依赖）和 Group 10（E2E 验收测试）
11. **CLI 入口**：为 GFS download 和 canonical convert 补充 CLI（nhms-gfs, nhms-canonical），供 sbatch 模板调用
