## Why

M9 已把 `data/Basins` 真实 SHUD 模型资产接入到发现、打包、registry、runtime/API/frontend fixture 层；测试环境也已证明真实 Slurm 集群能提交最小作业。但项目仍未完成生产环境闭环：真实 SHUD workload 尚未在 Slurm 上跑通，真实对象存储/生产数据迁移、真实气象源凭据、全国规模性能、生产监控告警和安全运维证据仍缺失。

本 change 将这些生产闭环缺口拆成可执行 issue，目标是在 staging/production-like 环境中形成可重复的证据链，而不是继续扩展 demo 或 placeholder 路径。

## What Changes

- 增加真实 Slurm + SHUD 生产 workload 验证：从 Basins-backed model package 到 `sbatch`、workspace、日志回收、`sacct` accounting、失败重试和 job array partial success。
- 增加真实对象存储与生产数据迁移闭环：Basins copied root、package publication、manifest、registry import、object URI、安全路径和迁移报告全部可复验。
- 增加真实气象源接入与 QC 闭环：GFS/IFS/ERA5 live credential/config、下载重试、manifest/QC、best-available lineage；CLDAS 保持后续受限源但预留配置合同。
- 增加 staging 端到端闭环：一个受控 cycle 从源资料发现到发布 API/前端曲线，并保留 run_id、forcing_version、Slurm job、object URI、QC 和 tile 证据。
- 增加全国规模与真实 MVT/性能证据：大河网/多模型查询、PostGIS query plan、tile publication、API latency 和前端加载边界。
- 增加生产运维与安全准备：配置模板、secret redaction、RBAC/auth 后端边界、监控指标、告警、runbook 和回滚流程。

## Capabilities

### New Capabilities

- `production-slurm-workload`: 在真实 Slurm 集群上运行可复验的 SHUD smoke/workload、job array、日志/accounting、失败重试和 partial success。
- `production-object-store-migration`: 用真实对象存储或 production-like local/S3 endpoint 完成 Basins copied root 迁移、package publication、registry import 和 URI/manifest 证据。
- `live-meteorology-ingestion`: 用真实源配置和凭据完成 GFS/IFS/ERA5 live cycle discovery/download/QC，并生成可追溯 raw/canonical/forcing lineage。
- `staging-end-to-end-closure`: 在 staging 中跑通至少一个受控流域或小模型集合的完整 forecast/analysis 发布链路。
- `national-scale-performance`: 形成全国规模河网、MVT、PostGIS/API/前端性能证据和容量边界。
- `production-ops-readiness`: 固化生产配置、权限、安全、监控告警、审计、runbook 和回滚验收。

### Modified Capabilities

- None. M9 `basins-runtime-consumption` and issue-126 `real-integration-test-matrix` are prerequisites and invariants for this change; M10 adds separate production-closure capabilities without redefining their completed fast/integration contracts.

## Impact

- Affects `services/orchestrator`, `services/slurm_gateway`, `workers/shud_runtime`, `workers/data_adapters`, `workers/canonical_converter`, `workers/forcing_producer`, `workers/output_parser`, `workers/flood_frequency`, `services/tile_publisher`, `apps/api`, `apps/frontend`, `infra/sbatch`, deployment/config docs, and validation docs.
- Requires new opt-in validation commands and staging runbooks; default `uv run pytest -q` and frontend fast checks remain self-contained.
- Requires production data handling policy: `/volume/data/nwm/Basins` must be copied for production migration evidence, not represented by a development symlink.
- Does not require nationwide hydrological skill certification, CLDAS credential acquisition, or a permanent production deployment in this change; those can be follow-up once the closure evidence lane exists.
- Keeps production validation opt-in: default fast backend/frontend checks must not require Slurm, production object storage, external networks, source credentials, copied Basins roots, or a live SHUD solver.
