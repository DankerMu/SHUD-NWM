# 项目进度

最后更新：2026-05-16，测试环境。

用途：作为跨 session 继承的项目真实进度索引，压缩记录“已实现什么、与设计/效果图还有什么差距、还缺什么数据”。项目有实质性进展时必须同步更新本文，保持 200 行以内。

## 当前状态

- Epic #120 已完成并关闭；子 issue #121-#126 全部关闭。
- 最新合并工作：PR #132，merge commit `ccc7f9bfaea4b5dfb125bdd5b8a4c36ca1ac1c88`。
- 基于 `data/Basins` 已创建 M9 OpenSpec change：`openspec/changes/m9-basins-model-assets/`；GitHub Epic #133，子 issue #134-#139；#134 Basins discovery inventory CLI 已实现。
- CI 覆盖 markdown lint、OpenAPI lint、JSON Schema 校验、真实 PostgreSQL/PostGIS/Timescale 集成、后端测试、前端 build/test、bundle size。
- #126 后本地基线：`uv run pytest -q` -> `586 passed, 3 skipped`；真实 DB integration 为显式 opt-in，GitHub CI 已跑通。
- 当前有效代码入口：`apps/api`、`apps/frontend`、`services/orchestrator`、`services/slurm_gateway`、`services/tile_publisher`、`workers/*` 下划线包、`infra/sbatch`。
- 已清理 legacy 占位目录：`apps/web`、hyphenated worker/service 目录、`workers/sbatch_templates`；后续不要在这些路径恢复实现。

## 后端 / 数据链路已实现

- FastAPI 后端已实现 forecast、models、pipeline、hindcast、flood alerts、best-available、state snapshots、data-source 等路由。
- 数据库 migration `000001`-`000014` 已覆盖 schema、enum、core/met/hydro/flood/map/ops 表、索引、pipeline 字段、enum remediation、best-available lineage。
- OpenAPI 契约位于 `openapi/nhms.v1.yaml`，前端类型由该文件生成。
- JSON Schema 已覆盖 run manifest、run status、QC result、pipeline job，并有 examples 校验。
- GFS、ERA5、IFS adapter 已实现并通过 mock/test 覆盖；IFS 多源预报能力已接入。
- Canonical conversion、forcing production、SHUD runtime adapter、output parser、state manager、洪水频率拟合、重现期计算、tile publisher 已实现。
- Orchestrator 支持 forecast/analysis/hindcast 链路、Slurm job array、retry/cancel 一致性、partial success、publish stage、pipeline persistence。
- Real Slurm gateway 已实现 `sbatch`、`sacct`、`scancel`、`sinfo`、array job、日志读取、模板白名单，并有 fake-binary smoke。
- 真实 DB 集成测试已覆盖从零迁移、幂等迁移、确定性 seed、API/空间查询、worker chain、fake real-Slurm 边界。

## 前端已实现

- 有效前端为 `apps/frontend`：Vite + React + TypeScript + MapLibre + ECharts + Zustand + OpenAPI-generated types。
- 已实现路由：
  - `/`：预报河网地图、河段选择、预报侧栏。
  - `/flood-alerts`：洪水预警统计、排名、ticker、地图、时间轴、详情。
  - `/monitoring`：流水线监控工作台、阶段、作业表、队列摘要、趋势面板、operator RBAC gate。
- Forecast UI 支持 GFS/IFS scenario 选择、多曲线图、analysis/forecast 区分、来源/周期归因、IFS 144h 可用时效标注。
- Flood warning UI 使用 API 数据加载，支持预警等级过滤、时间轴播放、排名、河段详情、API-base-aware tile URL。
- Monitoring UI 支持 pipeline status/jobs 轮询、source/cycle 选择、作业筛选/分页、日志弹窗、队列深度、趋势组件。
- 前端测试覆盖关键组件、API base 行为、route preview、mock API E2E、build、bundle size。

## 设计 / 效果图缺口

- 设计文档与效果图描述的是更完整的 GIS 产品，目前前端只有 3 条主路由。
- `docs/spec/06_frontend_gis_design.md` 与 `design/ui` 中仍缺或未完整对齐：
  - 效果图 1：全国总览，含左侧总览面板、中央全国地图、右侧指标面板、底部时间轴。
  - 效果图 2：独立流域详情 drill-down 页面。
  - 效果图 3：预报曲线详情页，含顶部 KPI、气象代站列表、forcing 图表、多源主图、洪水频率侧栏。
  - 效果图 5：气象空间栅格展示页。
  - 效果图 6：气象代站查询页。
  - 效果图 7：流域/模型资产管理页。
  - 效果图 8：产品监控布局已有功能雏形，但视觉与交互未完全按 spec 对齐。
- 当前 forecast 页面有地图和侧栏，但不是完整全国总览/流域 drill-down 交互模型。
- 当前 flood warning 页面覆盖核心业务流，但 vector tile contract 仍偏兼容方案，不是真正完整 MVT 生产路径。
- 当前 monitoring 页面可用且信息密度较高，但仍缺少部分 spec 级运维能力，例如 restart 后真实 Slurm 元数据追溯证明、完整资产 lineage 导航。
- RBAC 目前主要是前端 gate + dev/test override 约定，不是完整生产身份认证/授权系统。

## 数据缺口

- Demo seed 是确定性的长江样例：15 条河段、5 个气象代站、GFS/IFS 预报样本、洪水曲线、run/tile/pipeline 记录、对象存储占位 artifact。
- 开发环境已通过 `data/Basins -> /volume/data/nwm/Basins` 软链接接入河网/流域等 Basins 数据；这是开发期依赖，不是可迁移 artifact。
- `data/Basins` 已补入 13 个 SHUD 模型目录：`qhh`、`heihe`、`kashigeer`、`weiganhe`、`xinanjiang_upstream`、`hetianhe`、`qinyijiang`、`keliya`、`tailanhe`、`zhaochen/{WEM,HHY,MC,BST}`。
- 每个模型基本包含 `input/<model>/` SHUD 运行包：`*.cfg.para`、`*.cfg.ic`、`*.cfg.calib`、`*.sp.mesh`、`*.sp.riv`、`*.sp.rivseg`、`*.sp.att`、`*.para.{soil,geol,lc}`、`*.tsd.{forc,lai,mf,rl}` 和 `gis/{domain,river,seg}.shp`。
- `CALIB/` 提供约 20 组优选率定参数；`forcing/` 提供 CMFD 历史气象格点 CSV（`tailanhe` 目录名为 `focing`，接入时需清洗或兼容）。
- 这些数据可把当前 `model_package_uri`、mesh/river network/model registry、SHUD runtime dry/smoke、forcing 文件格式校验从 placeholder 推进到真实资产样例。
- 后续生产环境迁移必须复制 `/volume/data/nwm/Basins` 的实际数据到目标环境，不能只迁移软链接。
- 仓库内仍未内置这些真实资产；生产对象存储打包、校验和、版本登记和迁移脚本尚未实现。
- 外部真实气象下载通过 adapter/mock 测试覆盖；没有提交可作为生产 fixture 的 live GFS/IFS/ERA5 数据包。
- CLDAS 仍是权限受限/后续工作；未实现 CLDAS adapter、数据质量检查、best_available 生产路径。
- Worker-chain smoke 使用本地 `LocalObjectStore`，未覆盖真实 MinIO/S3。
- Slurm smoke 使用 fake binaries，未连接真实 Slurm 集群。
- 尚缺生产规模性能证据：全国矢量瓦片、大河网、全国 7 天逐小时预报、真实数据库 query plan/压测。

## M9 Basins 资产发现进展

- 已新增 `nhms-model discover-basins`：支持 `--basins-root`、`NHMS_BASINS_ROOT`，CLI 参数优先，Basins 子命令开发默认 `data/Basins`。
- 已实现结构化 inventory JSON，包含 root/symlink 元数据、直接与 `zhaochen/*` 嵌套模型、basin slug 与 `input/<shud_input_name>` alias、必需 SHUD/GIS 文件、轻量 checksum、建议 registry IDs、forcing/CALIB 计数、status/quirks 和默认 publish/import eligibility。
- 已兼容 `forcing/` 与 legacy `focing/`；冲突时优先 `forcing/` 并记录 `BASINS_FORCING_DIR_CONFLICT` warning；缺 `*.tsd.rl` 默认标记 `partial` 且不可默认发布/导入。
- 已递归忽略 `.DS_Store`、`@eaDir`、`*@SynoEAStream`，并对模型目录、`input/<alias>`、GIS 必需文件、`CALIB/`、`forcing/focing` 和 checksum 路径统一执行 Basins root containment；越界 symlink 使用 `BASINS_SYMLINK_OUTSIDE_ROOT`，不会读取外部文件且 inventory 不可导入。
- `forcing/` 与 `CALIB/` 计数已改为流式文件遍历，避免生产规模目录发现时一次性物化全部文件路径。
- 已补 synthetic discovery 测试矩阵和 opt-in 真实 `data/Basins` smoke；真实 smoke 仅在 `NHMS_RUN_BASINS_SMOKE=1` 且路径存在时运行，预期 13 个模型。

## 已知技术风险 / 注意事项

- 当前仍未完成生产级真实环境闭环：真实 Slurm 集群、真实对象存储、真实气象源凭据、全国规模数据和压测证据仍需专项验证。
- 若生产要求真实 `application/x-protobuf` MVT，需要把洪水 tile 从当前 GeoJSON 兼容交付升级为 PostGIS tile clipping + MVT 编码，并同步 API/OpenAPI/前端合同。
- 生产身份认证/授权尚未完成；当前 RBAC 主要是前端 gate + dev/test role override。
- 历史 OpenSpec proposal/tasks 保留当时路径和任务状态用于审计，不作为当前开发入口；判断完成度以源码、测试、README 和本文为准。
- 工作区可能存在 `dist/`、`node_modules/`、`__pycache__`、`.codex/` 等生成/本地文件；不要误 stage。历史 legacy 占位目录已删除，若旧文档仍提到它们，按当前有效代码入口为准。

## 常用验证命令

- 后端快速：`uv run pytest -q`
- Lint：`uv run ruff check .`
- OpenSpec 示例：`openspec validate issue-126-real-integration-test-matrix --strict --no-interactive`
- 真实 DB integration：`NHMS_RUN_INTEGRATION=1 NHMS_INTEGRATION_DATABASE_URL=postgresql://nhms:nhms_dev@localhost:5432/nhms uv run pytest -q -m integration`
- 前端：`cd apps/frontend && corepack pnpm test && corepack pnpm build && corepack pnpm check:bundle`
- 完整验证说明：`docs/VALIDATION.md`

## 下一步优先级

- 先明确下一条主线：生产数据接入、前端效果图对齐、CLDAS 启用、真实 MVT tile、生产 auth/RBAC。
- 如果做前端对齐，优先补资产管理、气象空间展示、气象代站查询，因为这些是缺失路由，不只是样式差距。
- 如果做数据就绪，优先基于 `data/Basins` 实现模型资产扫描/打包/登记：导入 basin、river network、mesh、model_instance，生成对象存储 package URI、checksum 和迁移清单。
- 如果做生产化，优先验证真实 Slurm 集群、真实对象存储、真实气象源凭据与下载稳定性。
