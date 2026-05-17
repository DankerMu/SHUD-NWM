## Context

当前系统已有：

- Basins-backed model discovery/package/import/runtime/API/frontend fixture（M9）。
- Slurm gateway 的 real backend、fake-binary smoke，以及测试环境最小真实 Slurm smoke：cluster `shudhpc`，account `friends`，`CPU` 分区 job `5684` 在 `cn04` 完成。
- PostgreSQL/PostGIS/Timescale integration、API、orchestrator、data adapters、forcing、SHUD runtime adapter、output parser、tile publisher 和前端核心页面。

仍缺：

- 真实 SHUD solver/workload 通过 Slurm 作业链运行并回收日志/accounting。
- 真实对象存储或 production-like S3/MinIO 的持久化 URI 闭环。
- 真实气象源凭据、下载稳定性、QC 与 lineage 证据。
- staging 端到端从资料源到 API/前端展示的单 cycle 证据。
- 全国规模大河网、MVT、PostGIS/API latency 和前端加载证据。
- 生产 auth/RBAC、secret redaction、告警、runbook 和 rollback 证据。

## Goals / Non-Goals

**Goals:**

- 以 staging/production-like 环境为目标，建立可重复的生产闭环验证 lane。
- 将真实 Slurm、真实对象存储、真实气象源和真实 Basins copied data 串成最小可验收闭环。
- 将全国规模和运维安全风险拆成明确 issue，避免与单一实现任务混杂。
- 保持 fast CI、开发 fixture、M9 Basins 合同和 demo seed 不被外部环境耦合。

**Non-Goals:**

- 不认证全国 hydrological skill 或模型率定质量。
- 不在本 change 中解决 CLDAS 权限；只定义 restricted source 的配置/跳过/追踪合同。
- 不强制所有开发者具备 Slurm、S3、生产气象源或 Basins copied root。
- 不重做已完成的 Basins discovery/package/import 合同。

## Decisions

### 1. 用 opt-in production validation lane 承载真实环境

新增 `NHMS_RUN_PRODUCTION_CLOSURE=1` 或等价命令集合，只在具备真实依赖时执行。fast tests 继续使用 synthetic fixtures、fake Slurm 和 local object store。

### 2. Slurm 验证从最小 smoke 升级为 workload evidence

真实 Slurm issue 必须覆盖：

- `sinfo/squeue/sacct/scontrol` inspection。
- shared `/scratch` 或配置化 workspace 日志路径；不得依赖 compute-node-local `/tmp`。
- 单模型 SHUD smoke/workload。
- job array，至少包含一个成功和一个受控失败模型，验证 partial success。
- retry/cancel/log/accounting 与 DB/API monitoring 可追溯。

### 3. 对象存储和数据迁移以 copied root 为生产证据

M9 已实现 Basins discovery/package/import/migration-report 的 fast 与 opt-in 真实资产合同。M10 不重做这些能力；生产 Basins 迁移必须在 copied root 和 production-like object store 上复验，证明 package manifest、object URI、checksum、registry import、API/runtime consumption 和 failure cleanup 都指向稳定 object prefix，而不是开发源路径或 `data/Basins` symlink。

### 4. 真实气象源先做可控最小 cycle

GFS/IFS/ERA5 live 资料源各自需要凭据/URL/节流/重试/校验和 redaction。验收以一个小窗口 cycle 为主，覆盖 raw manifest、canonical product、forcing lineage 和 QC；CLDAS 未获权限时必须显式 skipped/restricted，不得伪造成功。

### 5. staging E2E 只要求受控范围，但证据链必须完整

端到端 issue 选一个最小流域或小模型集合，跑通 `download -> canonical -> forcing -> Slurm SHUD -> parse -> frequency/tile -> API/frontend`，保存 runbook 输出和 artifact manifest。范围小于全国规模，目标是闭环可追溯。

### 6. 全国规模和真实 MVT 是独立容量任务

全国性能 issue 不要求重新跑完整气象下载，而是用真实 registry/river network、published outputs 或 deterministic large fixtures 验证 PostGIS query plan、MVT tile、API p95、frontend load 和 memory/bundle 边界。

### 7. 生产运维安全作为 release readiness gate

auth/RBAC 后端边界、secret redaction、配置模板、monitoring metrics、alerts、audit 和 runbook 必须在合并前形成文档和测试证据。若完整身份系统超出范围，issue 必须至少交付可替换的 backend enforcement seam 和 explicit non-goal。

## Risk Triage Fixture

Fixture level: expanded

Project profile: SHUD / production closure.

Selected risk packs:

- Public API / CLI / script entry: selected - 新增 opt-in production validation 命令、runbook 和 API monitoring evidence。
- Config / project setup: selected - Slurm partition/account、object store endpoint、source credentials、workspace roots、secret loading。
- File IO / path safety / overwrite: selected - copied Basins root、object store writes、workspace cleanup、log/artifact retention。
- Schema / columns / units / field names: selected - production manifests、QC records、lineage fields、monitoring payloads。
- Geospatial / CRS / shapefile sidecars: selected - national river network、MVT、PostGIS geometry queries。
- Time series / forcing / temporal boundaries: selected - live cycle discovery、forecast/analysis valid time、forcing continuity。
- Numerical stability / conservation / NaN: selected - SHUD output QC must reject NaN/Inf and malformed `.rivqdown`; skill certification remains non-goal。
- Solver runtime / performance / threading: selected - real SHUD Slurm workload, `cpus_per_task`, `SHUD_THREADS`, walltime and exit codes。
- Resource limits / large input / discovery: selected - nationwide segments, large tiles, object manifests, Slurm arrays and logs。
- Legacy compatibility / examples: selected - M9 Basins model aliases, `tailanhe/focing`, demo seed compatibility, fake lanes preserved。
- Error handling / rollback / partial outputs: selected - failed basin does not block others; object/DB cleanup and retry evidence。
- Release / packaging / dependency compatibility: selected - Linux/HPC env, solver binary/module, frontend build, deployment config。
- Documentation / migration notes: selected - runbook, production copied-data requirement, validation matrix and progress update。

Risk packs not selected:

- None. This change deliberately covers production closure surfaces and therefore uses expanded review.

Must preserve:

- `uv run pytest -q` does not require real Slurm, object storage, external network, production credentials, copied Basins root, or real SHUD solver.
- M9 Basins discovery/package/import fast and opt-in smoke contracts remain valid.
- Existing frontend pages and generated OpenAPI types continue to build.
- Secret values never appear in logs, audit rows, PR evidence, object manifests, or frontend output.

Required evidence:

- OpenSpec strict validation.
- Real Slurm smoke/workload logs with shared-storage stdout/stderr and `sacct` evidence.
- Object store package/manifest checksums and registry/API consumption against production-like URI prefix.
- Live source cycle evidence with redacted config and QC/manifest artifacts.
- Staging E2E runbook output mapping every product to run_id/source cycle/model/version/object URI.
- National-scale query/tile/API/frontend performance report.
- Ops/security readiness checklist and rollback drill evidence.

## Preflight Inputs

Every M10 implementation issue must record its preflight decision before code changes:

- Slurm: cluster/account/partition, shared workspace root, solver binary/module, selected Basins model, and allowed walltime.
- Object store: target type (`s3|minio|local-production-like`), endpoint/root/prefix, credential source, cleanup policy, and copied Basins root.
- Meteorology: enabled source subset, credentials/public path, allowed cached fallback, cycle time window, and CLDAS restricted reason if not enabled.
- Staging E2E: source cycle, model set, DB target, object prefix, Slurm partition/account, and evidence output path.
- National scale: real imported dataset or deterministic fixture source, minimum segment/model counts, bbox sizes, thresholds file, and tile content-type expectation.
- Ops/security: auth mode, required production roles, alert target or dry-run sink, and rollback drill scope.

## Validation Command Contract

Each sub-issue should either implement or consume explicit opt-in commands using this shape:

```bash
openspec validate m10-production-closure --strict --no-interactive
NHMS_RUN_PRODUCTION_CLOSURE=1 uv run nhms-production validate-<lane> --evidence-root artifacts/production-closure/<run_id>
uv run ruff check .
uv run pytest -q <targeted-tests>
```

Frontend or E2E issues also include:

```bash
cd apps/frontend
corepack pnpm test
corepack pnpm build
```

The concrete CLI name may change during implementation, but every issue must leave a documented command, required environment variables, evidence output location, and fast-regression commands.

## Issue Breakdown

Planned Epic: "M10 Production Closure".

Suggested sub-issues:

1. Real Slurm + SHUD workload closure.
2. Production object store + Basins copied-data migration closure.
3. Live meteorology ingestion + QC closure.
4. Staging end-to-end forecast/analysis closure.
5. National-scale MVT/performance closure.
6. Production ops/security/runbook readiness.

Dependency map:

- Issues 1, 2, and 3 can start independently after their preflight inputs are available.
- Issue 4 depends on accepted minimum evidence from Issues 1, 2, and 3.
- Issue 5 depends on Issue 2 plus either real imported national data or an approved deterministic large fixture.
- Issue 6 can start early, but final acceptance depends on evidence and rollback surfaces from Issues 1-5.

## Open Questions

- Production object store target for first closure: real S3/MinIO endpoint or production-like filesystem object store with the same URI contract?
- First real SHUD workload model: smallest Basins model for turnaround or operationally important basin?
- Which live source credentials are available first: GFS public path, IFS configured access, ERA5/CDS token, or only a subset?

## Issue #147 Fixture Overlay: Real Slurm + SHUD Workload

Fixture level: expanded

Why expanded:

- Touches Slurm CLI/script entrypoints, production `infra/sbatch` templates, shared log paths, retry/cancel semantics, solver runtime resources, and job-array partial-success behavior.

Change surface:

- `services/slurm_gateway/*`, `services/orchestrator/*` Slurm submission/monitoring evidence paths.
- `workers/shud_runtime/*` execution manifest/runtime evidence paths.
- `infra/sbatch/*` production SHUD workload templates and docs.
- `tests/test_real_slurm_gateway.py`, `tests/test_slurm_array_contract.py`, targeted production-closure validation tests, `docs/VALIDATION.md`, and `progress.md`.

Must preserve:

- Fast tests and default CI do not require real Slurm, a live SHUD solver, copied Basins root, object-store credentials, external network, or production secrets.
- Existing fake-binary Slurm tests, M3 job-array partial success, retry/cancel error-code classification, and template whitelist behavior remain compatible.
- Existing monitoring/API contracts continue to expose job ID, status, exit code, logs, and array task status without requiring new run_id-specific API filters.
- Secrets are not rendered into sbatch scripts, logs, manifests, audit/evidence files, or PR evidence.

Must add/change for #147:

- A documented opt-in `validate-slurm` lane or equivalent command that records preflight inputs and writes evidence under `artifacts/production-closure/<run_id>/slurm/`.
- Production SHUD workload template coverage for manifest-driven inputs, `cpus_per_task`, `SHUD_THREADS`, walltime, shared stdout/stderr, workspace, and object URI fields.
- Evidence parsing/reporting for real or fake `sacct` output with job ID, state, exit code, elapsed, node list, partition, array task details, retry/cancel state, and redacted environment metadata.
- Controlled array fixture with one successful task and one failing task that proves successful outputs remain publishable and failed task metadata is actionable.

Issue #147 risk packs:

- Public API / CLI / script entry: selected - opt-in production closure command, Slurm CLI boundary, and sbatch template entrypoints need stable command/error behavior.
- Config / project setup: selected - cluster/account/partition, shared workspace root, solver binary/module, walltime, resources, and evidence root are preflight inputs.
- File IO / path safety / overwrite: selected - shared stdout/stderr, workspace, object URI, run logs, and evidence bundle paths must stay contained and durable.
- Schema / columns / units / field names: selected - Slurm evidence fields, runtime manifest fields, and monitoring/API job metadata must remain stable.
- Geospatial / CRS / shapefile sidecars: deferred to #148/#151 - #147 consumes a Basins-backed package but does not alter geometry schemas.
- Time series / forcing / temporal boundaries: deferred to #149/#150 - #147 records cycle/model identity but does not change source-cycle conversion or forcing semantics.
- Numerical stability / conservation / NaN: selected - SHUD workload evidence must surface malformed output/QC failure as a blocked downstream publication; hydrologic skill certification remains non-goal.
- Solver runtime / performance / threading: selected - real SHUD submission must capture `cpus_per_task`, `SHUD_THREADS`, walltime, memory, solver binary/module, and exit/runtime evidence.
- Resource limits / large input / discovery: selected - array concurrency, log size, shared workspace, and long-running Slurm command boundaries must be bounded.
- Legacy compatibility / examples: selected - fake Slurm, existing M3 templates, demo lanes, and M9 Basins package fixtures must continue to pass.
- Error handling / rollback / partial outputs: selected - controlled failure, retry, cancel, and partial success are core #147 acceptance criteria.
- Release / packaging / dependency compatibility: selected - Linux/HPC Slurm CLI availability and production template paths are release-readiness inputs.
- Documentation / migration notes: selected - runbook, validation command, evidence file list, and progress/validation docs must be updated.

Issue #147 required evidence:

- Preflight input fixture: cluster/account/partition, shared workspace/log root, solver binary/module, selected model/package URI, walltime/resource profile, and evidence root -> redacted preflight artifact with no secrets.
- Shared-log sbatch rendering test: manifest with workspace/object roots/resources -> script uses shared stdout/stderr, exports `SHUD_THREADS`/`OMP_NUM_THREADS`, and does not render secret values.
- Slurm accounting parser test: `sacct` rows containing job ID, state, exit code, elapsed, node list, and partition -> evidence report records stable fields and maps terminal failures to stable codes.
- Job array partial-success test: two task manifests, one controlled success and one controlled failure -> success remains publishable; failure records task ID, stderr path, retry count, and failure stage.
- Retry/cancel test: retryable Slurm failure and explicit cancel input -> monitoring or persisted evidence records retry/cancel state without mutating successful outputs.
- Redaction test: env/config/log/audit/evidence containing token/password/signed URL-shaped values -> emitted evidence replaces sensitive values with redaction markers.
- Local verification commands: OpenSpec strict validation, `uv run ruff check .`, targeted backend tests for Slurm gateway/orchestrator/runtime changes, and the documented opt-in validation command when `NHMS_RUN_PRODUCTION_CLOSURE=1`.

Issue #147 non-goals / deferred:

- Production object-store migration and copied-root enforcement are handled by #148.
- Live meteorology source discovery/download/QC is handled by #149.
- Full staging E2E source-to-frontend closure is handled by #150.
- National-scale MVT/query/frontend performance is handled by #151.
- Full production auth/RBAC/alert/rollback readiness is handled by #152, while #147 still must avoid secret leakage.

## Issue #148 Fixture Overlay: Production Object Store + Basins Copied-Data Migration

Fixture level: expanded

Why expanded:

- Touches CLI validation lane, object-store publish/overwrite paths, copied Basins migration evidence, package manifests, registry import, cleanup/rollback, and symlink/path safety.

Change surface:

- `services/production_closure/*` production object-store validation lane and docs.
- `workers/model_registry/basins_discovery.py`, `basins_package.py`, `basins_registry_import.py`, and `cli.py` reuse points.
- `packages/common/object_store.py`, `packages/common/redaction.py`, and model registry API/runtime consumption evidence.
- `tests/test_basins_package_publication.py`, `tests/test_basins_registry_import.py`, targeted production-closure object-store tests, `docs/VALIDATION.md`, and `progress.md`.

Must preserve:

- Fast tests and default CI do not require real S3/MinIO, production credentials, copied `/volume` data, PostGIS integration DB, or real SHUD solver.
- Existing M9 discovery, publish, migration-report, registry import, activation, runtime/API/frontend Basins contracts remain compatible.
- `data/Basins` symlink remains acceptable for development discovery/package smoke, but cannot be accepted as production copied-root evidence.
- Object manifests, audit/API evidence, logs, and PR comments must not expose credentials, userinfo, tokens, signed query strings, or development-only source paths as runtime package sources.

Must add/change for #148:

- A documented opt-in `validate-object-store` lane or equivalent command that records object-store target/root/prefix, credential source, cleanup policy, copied Basins root, selected model/version, and evidence root under `artifacts/production-closure/<run_id>/object-store/`.
- Reuse M9 `basins-migration-report` to accept copied roots and reject symlink-only roots with stable error evidence before package/import work.
- Reuse Basins package publication against production-like object storage and verify manifest/object bytes/checksums from the stored objects.
- Reuse registry import-source preparation plus deterministic API-contract/runtime evidence to prove model package consumption uses object URI prefix, not `data/Basins` or `/volume/...` source paths. Fast evidence prepares local import sources and marks live DB import/API execution as `not_executed`; when live registry inputs are explicitly enabled, validation must run the registry DB import and block on missing or failed import evidence instead of claiming local-only success.
- Add cleanup/rollback evidence for failed publish/import attempts and prove no model becomes active implicitly.

Issue #148 risk packs:

- Public API / CLI / script entry: selected - opt-in production closure command and reused `nhms-model` commands need stable JSON/error behavior.
- Config / project setup: selected - object-store target/root/prefix, credential source, cleanup policy, copied root, selected model/version, and evidence root are preflight inputs.
- File IO / path safety / overwrite: selected - copied root, object writes, manifest output, cleanup/quarantine, symlink rejection, and overwrite/idempotency are core acceptance.
- Schema / columns / units / field names: selected - migration report, package manifest, registry import report, API/runtime evidence fields, checksums, and URI lineage must stay stable.
- Geospatial / CRS / shapefile sidecars: selected - registry import consumes Basins GIS sidecars and must preserve M9 geometry/CRS safety.
- Time series / forcing / temporal boundaries: selected - package manifests include forcing metadata/time coverage; #148 does not produce live forcing.
- Numerical stability / conservation / NaN: not selected - no solver/output numerical behavior changes; #147/#150 cover malformed output/QC.
- Solver runtime / performance / threading: selected - runtime smoke must prove object URI package staging without requiring real solver execution.
- Resource limits / large input / discovery: selected - copied Basins tree traversal, object manifest size, forcing samples, and GIS/SHUD evidence bounds must remain bounded.
- Legacy compatibility / examples: selected - `tailanhe/focing`, `input/<alias>`, zhaochen nested models, development symlink discovery, and M9 test fixtures must continue to work.
- Error handling / rollback / partial outputs: selected - failed publish/import cleanup evidence and no implicit activation are core acceptance.
- Release / packaging / dependency compatibility: selected - Linux/object-store path behavior and optional PostGIS integration availability must remain compatible.
- Documentation / migration notes: selected - runbook, validation command, evidence file list, and copied-not-symlink production requirement must be updated.

Issue #148 required evidence:

- Preflight artifact: object-store target/root/prefix, credential source, cleanup policy, copied Basins root, model/version, and evidence root -> redacted JSON with no secret-shaped values.
- Copied-root migration test: copied synthetic Basins root -> migration report with file count, byte count, inventory checksum, source/target metadata, and `production_ready=true`.
- Symlink-root rejection test: symlink Basins root -> stable blocker/error evidence and no production-ready bundle or package/import writes.
- Stored-object verification test: publish package to production-like local object store -> manifest URI/package URI/per-file checksums/package checksum verified by rereading object bytes.
- Registry/API/runtime consumption test: default fast evidence prepares registry import sources and API/runtime contract smoke, while opt-in live registry evidence imports to the configured DB when enabled -> object URI prefix is used and development source paths are not runtime package sources; evidence must not claim live DB/API success unless that integration actually ran.
- Cleanup/rollback test: simulated publish/import failure after partial work -> evidence lists written keys/rows, cleanup/quarantine result, and active model state remains unchanged.
- Redaction test: endpoint/root/prefix/manifest/API evidence containing credential-shaped URI values -> emitted evidence removes userinfo/query/fragment/secrets.
- Local verification commands: OpenSpec strict validation, `uv run ruff check .`, targeted Basins package/registry/runtime/API tests, and documented opt-in validation command when `NHMS_RUN_PRODUCTION_CLOSURE=1`.

Issue #148 non-goals / deferred:

- Real Slurm workload and SHUD accounting evidence are handled by #147.
- Live meteorology source discovery/download/QC is handled by #149.
- Full staging source-to-frontend chain is handled by #150.
- National-scale MVT/query/frontend performance is handled by #151.
- Full production auth/RBAC/alert readiness is handled by #152; #148 only proves no implicit activation and safe rollback evidence.
