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

## Issue #149 Fixture Overlay: Live Meteorology Ingestion + QC Closure

Fixture level: expanded

Why expanded:

- Touches opt-in production validation CLI, live/public source config, credential redaction, raw object downloads, canonical conversion, forcing production, QC evidence, best-available lineage, and source availability/error semantics.

Change surface:

- `services/production_closure/*` production met validation lane and docs.
- `workers/data_adapters/{gfs_adapter.py,ifs_adapter.py,era5_adapter.py,cli.py}` source discovery/download reuse points.
- `workers/canonical_converter/*`, `workers/forcing_producer/*`, and `packages/common/met_store.py` lineage/QC reuse points.
- `packages/common/object_store.py`, `packages/common/redaction.py`, and source identity helpers.
- Targeted adapter/canonical/forcing/production-closure tests, `docs/VALIDATION.md`, and `progress.md`.

Must preserve:

- Fast tests and default CI do not require external network, real GFS/IFS/ERA5/CLDAS credentials, real object storage, copied `/volume` data, PostGIS integration DB, real API, Slurm, or a live SHUD solver.
- Existing GFS, ERA5, and IFS adapter mock/fixture tests remain compatible.
- Existing canonical conversion, forcing production, best-available, API contracts, demo seed, and M9 Basins model contracts remain compatible.
- CLDAS remains restricted/skipped unless explicit credentials and implementation are present; evidence must not fabricate CLDAS success.
- Source credentials, signed URLs, userinfo, tokens, passwords, and credential-shaped query/path values never appear in stdout, logs, manifests, audit/evidence files, docs, or PR comments.

Must add/change for #149:

- A documented opt-in `validate-met` lane or equivalent command that records enabled source subset, credential/public-path mode, cached fallback policy, cycle window, object prefix, selected Basins model, CLDAS restricted reason, and evidence root under `artifacts/production-closure/<run_id>/met/`.
- Redacted source configuration templates for GFS, IFS, ERA5, and CLDAS with explicit `enabled`, `disabled`, or `restricted` status and stable blocker codes for missing inputs.
- Deterministic production-like cycle discovery/download evidence for available GFS/IFS/ERA5 sources, with optional live execution only when source-specific network/credential gates are explicitly enabled; evidence must include file count, byte count, checksum, retry count, source identity, cycle time, raw object URI, unavailable/incomplete source status, and per-source execution mode from `deterministic_fixture|live_executed|skipped|restricted|not_executed`.
- Bounded source behavior: manifest enumeration, per-file read/download size, retry/backoff, network timeout, source count, forecast-hour count, and evidence payload size must have deterministic limits for fast validation and recorded limits for live validation.
- Run-scoped object/evidence behavior: raw/canonical/forcing validation scratch objects and evidence bundles must stay under the current `run_id`, refuse cross-run overwrite, validate path containment, and require explicit force/cleanup behavior before replacing an existing same-run bundle.
- Canonical product evidence from the downloaded raw manifest, including variables, units, time axis, source cycle, object URI, checksum, and malformed/missing raw failure metadata.
- Forcing production and QC evidence for at least one Basins-backed model, including forcing URI/manifest, source lineage, required variable coverage, continuity, unit, missing-value, and range checks.
- Best-available lineage evidence that records selected source per valid time or explicit skipped/restricted reason without claiming live success for non-executed sources.

Issue #149 risk packs:

- Public API / CLI / script entry: selected - opt-in `validate-met` production closure command and existing source worker CLIs need stable JSON/error behavior.
- Config / project setup: selected - enabled source subset, credential/public path, cached fallback, cycle window, object prefix, selected model, and CLDAS restricted reason are preflight inputs.
- File IO / path safety / overwrite: selected - raw/canonical/forcing object writes, manifests, evidence bundles, cache fallback, and cleanup/idempotency must stay contained and avoid unintentional overwrite.
- Schema / columns / units / field names: selected - source config, raw manifest, canonical product metadata, forcing manifest, QC records, and best-available lineage fields must stay stable.
- Geospatial / CRS / shapefile sidecars: not selected - #149 uses an existing Basins-backed model and does not alter river geometry or CRS sidecars.
- Time series / forcing / temporal boundaries: selected - cycle windows, forecast hours, valid times, time-axis continuity, best-available selection, and forcing coverage are core acceptance.
- Numerical stability / conservation / NaN: selected - forcing QC must reject missing/non-finite/out-of-range variables and malformed source/canonical values; hydrologic skill certification remains non-goal.
- Solver runtime / performance / threading: not selected - #149 stops at forcing/QC readiness and does not run live SHUD solver workloads.
- Resource limits / large input / discovery: selected - live discovery/download, raw/canonical file sizes, retry loops, and manifest enumeration must be bounded for fast lanes.
- Legacy compatibility / examples: selected - existing mock adapters, demo GFS/IFS/ERA5 data, source-id normalization, and Basins fixture assumptions must continue to pass.
- Error handling / rollback / partial outputs: selected - unavailable source cycles, restricted CLDAS, partial downloads, failed canonical conversion, and failed forcing QC need stable evidence without corrupting successful sibling evidence.
- Release / packaging / dependency compatibility: selected - Linux/HPC environments may lack optional live-source dependencies or credentials; default validation must remain deterministic.
- Documentation / migration notes: selected - runbook, validation command, evidence file list, source credential policy, CLDAS restricted policy, and progress/validation docs must be updated.

Issue #149 required evidence:

- Preflight artifact: enabled sources, credential/public-path mode, cached fallback policy, cycle window, object prefix, selected model/version, CLDAS restricted reason, and evidence root -> redacted JSON with no secret-shaped values.
- Source config template test: GFS/IFS/ERA5/CLDAS config with credential-shaped values -> evidence reports enabled/disabled/restricted status and redacts secret values.
- Cycle discovery/download test: deterministic production-like cycle for at least one GFS/IFS/ERA5 source -> raw manifest with file count, byte count, checksum, retry count, source identity, cycle time, raw object URI, per-source execution mode, and stable unavailable/incomplete status for skipped sources.
- Bounds test: oversized source manifests/files/retry plans/evidence payloads -> stable blocker evidence before unbounded enumeration, reads, retries, or writes.
- Idempotency/path test: existing same-run and different-run raw/canonical/forcing/evidence objects -> no cross-run overwrite, explicit same-run force/cleanup behavior, and path containment enforcement.
- Canonical conversion test: raw manifest -> canonical product metadata with variable/unit/time-axis/source-cycle/object-URI/checksum evidence, and stable failure for malformed or missing raw inputs.
- Forcing/QC test: canonical products plus Basins-backed model -> forcing manifest/package URI and QC result for continuity, units, missing values, variable ranges, and pass/fail status.
- Best-available lineage test: executed and skipped/restricted source set -> selected source or explicit reason per valid time without fabricated CLDAS/live-source success.
- Redaction test: source endpoints, object prefixes, manifests, stdout, docs, and PR evidence containing token/password/signed-URL-shaped values -> emitted evidence replaces sensitive values with redaction markers.
- Local verification commands: OpenSpec strict validation, `uv run ruff check .`, targeted data adapter/canonical/forcing/QC/production-closure tests, and documented opt-in validation command when `NHMS_RUN_PRODUCTION_CLOSURE=1`.

Issue #149 non-goals / deferred:

- Real Slurm workload and SHUD accounting evidence are handled by #147 and #150.
- Production object-store copied-root migration is handled by #148; #149 consumes object prefix contracts only.
- Full staging source-to-frontend chain is handled by #150.
- National-scale MVT/query/frontend performance is handled by #151.
- Full production auth/RBAC/alert readiness is handled by #152; #149 still must avoid source credential leakage.

## Issue #150 Fixture Overlay: Staging End-to-End Forecast/Analysis Closure

Fixture level: expanded

Why expanded:

- Connects the already-landed production closure lanes for Slurm (#147), object storage (#148), and meteorology/QC (#149) into one source-to-frontend staging evidence bundle.
- Touches the opt-in production validation CLI, pipeline orchestration, SHUD output parsing/QC, flood frequency, tile publication, API contract checks, frontend smoke, durable evidence indexing, and redacted config capture.

Change surface:

- `services/production_closure/*` staging E2E validation lane, shared evidence helpers, and CLI dispatch.
- Existing Slurm/object-store/met production closure evidence readers and contracts from #147-#149.
- `workers/output_parser/*`, `workers/flood_frequency/*`, `services/tile_publisher/*`, API contract checks, and frontend smoke/playwright helpers where needed.
- Targeted production E2E/QC/API/frontend tests, `docs/VALIDATION.md`, and `progress.md`.

Must preserve:

- Fast tests and default CI do not require real external networks, production credentials, real object storage, copied `/volume` data, PostGIS integration DB, real Slurm, live SHUD solver, or a running frontend server.
- Existing #147/#148/#149 validation lanes and their evidence schemas remain compatible.
- API checks use existing identifier contracts derived from the evidence bundle; no new run_id-specific API filters are introduced unless this issue explicitly implements and documents that contract.
- Frontend smoke must not rely on mock API routes or local-only placeholder data when claiming staging-published data readiness.
- Bad SHUD output must fail before flood frequency, tile, API, or frontend publication evidence claims success.
- Secrets and signed URLs never appear in logs, evidence bundles, API/frontend payloads, docs, PR comments, or smoke screenshots.

Must add/change for #150:

- A documented opt-in `validate-e2e` lane or equivalent command that records source cycle, model set, DB target, object prefix, Slurm partition/account, frontend API base, selected dependency evidence roots, and evidence root under `artifacts/production-closure/<run_id>/e2e/`.
- A staging E2E runbook that explicitly selects source cycle, model set, object prefix, Slurm partition/account, DB target, API base, and frontend smoke mode.
- A deterministic fast path that consumes or synthesizes bounded #147/#148/#149-style evidence without requiring real services, while never claiming live DB/API/Slurm/frontend success unless those checks actually ran.
- Deterministic or consumed stage evidence for `download -> canonical -> forcing -> Slurm SHUD -> parse -> flood frequency -> tile publish -> API/frontend`, including status, blockers, input/output URIs, local artifact manifests, DB/object identifiers, Slurm jobs/logs, QC results, tile artifacts, frontend smoke lineage, and redacted config. The fast path must not claim live DB/object/API/frontend execution unless those checks actually ran.
- SHUD output QC evidence for malformed `.rivqdown`, NaN/Inf, missing required outputs, count mismatches, and time-axis mismatches, with stable error codes and downstream publication blockers.
- API evidence that starts from the closure evidence root and records existing contracts using derived `model_id`, `basin_version_id`, `segment_id`, `source/cycle_time`, `job_id`, and `layer_id`; live API execution is explicit and otherwise recorded as false or blocked.
- Frontend smoke evidence that records whether it used a real staging API, a deterministic local API fixture derived from the E2E evidence bundle, or a skipped/not_executed blocker; it must not claim staging frontend readiness from mock-only data.
- Run-scoped, idempotent evidence/object behavior with explicit same-run `--force` semantics and no cross-run overwrite.

Issue #150 risk packs:

- Public API / CLI / script entry: selected - opt-in `validate-e2e` command, API contract checks, and frontend smoke entrypoints need stable JSON/error behavior.
- Config / project setup: selected - source cycle, model set, DB target, object prefix, Slurm partition/account, frontend API base, dependency evidence roots, and evidence root are preflight inputs.
- File IO / path safety / overwrite: selected - durable evidence bundles, inherited object URIs, tile artifacts, SHUD logs, screenshots/results, and rerun behavior must stay run-scoped and contained.
- Schema / columns / units / field names: selected - stage manifest, derived identifiers, QC records, flood frequency outputs, tile metadata, API evidence, and frontend lineage fields must stay stable.
- Geospatial / CRS / shapefile sidecars: selected - tile publication and API/frontend map checks consume model/river/layer identifiers and must not corrupt existing geometry contracts.
- Time series / forcing / temporal boundaries: selected - source cycle, forcing valid times, SHUD output time axis, forecast series, frequency windows, and frontend timeline evidence are core acceptance.
- Numerical stability / conservation / NaN: selected - malformed/NaN/Inf/count/time mismatch SHUD outputs must block downstream publication.
- Solver runtime / performance / threading: selected - consumes Slurm SHUD job evidence and must preserve job/log/QC/runtime linkage from #147.
- Resource limits / large input / discovery: selected - bounded model set, artifact enumeration, API checks, tile output, frontend smoke, and evidence payloads need deterministic limits.
- Legacy compatibility / examples: selected - existing fast lanes, M9 Basins contracts, API identifiers, frontend tests, and demo fixtures must continue to pass.
- Error handling / rollback / partial outputs: selected - failed stage must stop dependent publication while preserving previous/sibling evidence and actionable blockers.
- Release / packaging / dependency compatibility: selected - Linux/HPC optional services and frontend toolchain availability must be handled by opt-in gates.
- Documentation / migration notes: selected - staging runbook, validation command, evidence file list, source-to-frontend lineage, and progress/validation docs must be updated.

Issue #150 required evidence:

- Preflight artifact: source cycle, model set, DB target, object prefix, Slurm partition/account, frontend API base, dependency evidence roots, and evidence root -> redacted JSON with stable missing-input errors and no secret-shaped values.
- Dependency evidence test: accepted #147/#148/#149 evidence inputs or deterministic equivalents -> validation records each dependency as consumed, skipped, or not_executed without fabricating live success.
- Stage manifest test: bounded chain input -> stage evidence lists `download`, `canonical`, `forcing`, `slurm`, `parse`, `frequency`, `tile`, `api`, and `frontend` statuses plus blockers and artifact URIs/manifests, with live execution flags false unless checks actually ran.
- API contract test: derived identifiers from the evidence bundle -> API evidence records existing model/detail/forecast/alert/job/log/tile metadata contracts or records `not_executed` with a stable reason; it must not invent run_id-only filters.
- Frontend smoke test: staging API or deterministic evidence-backed API fixture -> smoke evidence records source/model/run lineage and rejects mock-only placeholder success.
- SHUD output QC blocker test: malformed `.rivqdown`, NaN/Inf, missing output, count mismatch, or time-axis mismatch -> downstream frequency/tile/API/frontend publication is blocked for that run with stable error metadata and retained raw/log paths.
- Idempotency/path/redaction test: reruns, unsafe run IDs, credential-shaped API/object/slurm/frontend values, and existing evidence bundles -> no cross-run overwrite, explicit same-run force behavior, path containment, and redacted evidence/stdout/docs.
- Local verification commands: OpenSpec strict validation, `uv run ruff check .`, targeted production E2E/QC/API/frontend tests, frontend `corepack pnpm test && corepack pnpm build` when UI/generated types change, and documented opt-in `NHMS_RUN_PRODUCTION_CLOSURE=1 ... validate-e2e --evidence-root ...`.

Issue #150 non-goals / deferred:

- National-scale MVT/query/frontend performance thresholds are handled by #151; #150 only proves bounded staging closure.
- Production auth/RBAC/alert/rollback readiness is handled by #152; #150 still must avoid secret leakage and record redacted config.
- New live source credential acquisition is out of scope; #150 consumes #149 evidence or deterministic production-like source evidence.
- New permanent production deployment is out of scope; #150 emits staging/production-like validation evidence only.

## Issue #151 Fixture Overlay: National-Scale MVT and Performance Closure

Fixture level: expanded

Why expanded:

- Touches capacity/performance evidence, large geospatial fixtures, API query contracts, tile delivery semantics, frontend smoke/load evidence, and release-blocker reporting for production MVT readiness.

Change surface:

- A new opt-in `validate-scale` production closure lane under `services/production_closure/*` and the production closure CLI dispatch.
- API/tile/query evidence helpers for model listing, river bbox, flood alert map, forecast series, pipeline jobs/logs, and flood return-period tile metadata.
- Frontend smoke evidence that may be deterministic when frontend code is unchanged, but must record desktop/mobile breakpoints, load/render thresholds, and mock/live execution flags.
- `tests/test_production_scale_validation.py`, `docs/VALIDATION.md`, `progress.md`, and national-scale OpenSpec tasks/spec updates.

Must preserve:

- Fast tests and default CI do not require a real national dataset, PostGIS, MVT encoder, object storage, production credentials, live API, frontend preview server, or browser.
- Existing GeoJSON flood return-period endpoint and legacy `.pbf` redirect behavior remain truthful; the lane must not claim production MVT readiness when delivery is GeoJSON compatibility.
- Existing frontend flood-alert/map tests and generated API types remain compatible unless frontend code changes.
- Existing #147/#148/#149/#150 closure lanes and M9 Basins registry/object URI contracts remain valid.
- Secret values and signed URLs never appear in performance reports, thresholds, query evidence, frontend evidence, docs, or PR comments.

Must add/change for #151:

- A documented opt-in `validate-scale` lane or equivalent command that records dataset source, segment/model counts, bbox set, thresholds file/version, tile content-type expectation, frontend breakpoint set, and evidence root under `artifacts/production-closure/<run_id>/scale/`.
- A deterministic large fixture or consumed real imported dataset manifest with segment/model counts, geometry bounds, bbox sizes, and fixture generation mode; if counts fall below thresholds, readiness is blocked with stable metadata.
- A versioned thresholds artifact defining minimum segment/model counts, p95 API/query targets, max tile bytes, frontend load/render budgets, memory bounds, oversized bbox behavior, long time-range behavior, and object-listing bounds.
- Query/latency evidence for model listing, river segments bbox, flood alert summary/ranking/timeline/map, forecast series, pipeline jobs/logs, and tile metadata. Fast mode may use deterministic timing samples and query-plan fixtures, but must mark live DB/API execution false unless real checks ran.
- Tile delivery evidence that either validates `application/x-protobuf` MVT with bounded bytes/layer metadata, or emits an explicit release blocker that current GeoJSON compatibility delivery is not production MVT. A blocked MVT path must not claim production tile readiness.
- Frontend large-layer evidence for desktop and mobile breakpoints that records load/render/timeline/chart budgets, execution mode, lineage, and recoverable oversized/unavailable-layer behavior. Mock-only data must not be reported as live frontend readiness.
- Run-scoped idempotent evidence behavior, path containment, payload-size/resource bounds, and secret redaction for thresholds, query evidence, tile reports, screenshots/results, and environment metadata.

Issue #151 risk packs:

- Public API / CLI / script entry: selected - `validate-scale`, API/tile evidence queries, and frontend smoke/load entrypoints need stable JSON/error behavior.
- Config / project setup: selected - dataset source, thresholds file, bbox set, tile content-type expectation, frontend breakpoints, and evidence root are preflight inputs.
- File IO / path safety / overwrite: selected - large fixture manifests, thresholds, query plans, latency reports, tile reports, frontend results, and reruns must stay run-scoped and bounded.
- Schema / columns / units / field names: selected - threshold schema, query evidence schema, latency units, tile metadata, content types, and frontend timing/memory fields must remain stable.
- Geospatial / CRS / shapefile sidecars: selected - national river network bounds, bbox filtering, tile geometry metadata, and CRS assumptions are core #151 acceptance.
- Time series / forcing / temporal boundaries: selected - forecast series, flood alert valid times, long time ranges, and frontend timeline interactions must be bounded.
- Numerical stability / conservation / NaN: selected - latency percentile calculation, counts, byte sizes, and timing samples must reject malformed/non-finite values.
- Solver runtime / performance / threading: not selected - #151 consumes published/queryable outputs and does not run SHUD solvers.
- Resource limits / large input / discovery: selected - national-scale fixture size, bbox enumeration, object listings, tile byte bounds, API samples, frontend loads, and evidence payloads must be bounded.
- Legacy compatibility / examples: selected - existing GeoJSON tile compatibility, `.pbf` redirect, model registry, API contracts, and frontend tests must continue to pass.
- Error handling / rollback / partial outputs: selected - failed thresholds, oversized requests, missing MVT, or frontend load failures must produce stable blockers without claiming readiness.
- Release / packaging / dependency compatibility: selected - Linux/headless/browser optional tooling, PostGIS optionality, and frontend build dependencies must be explicitly gated.
- Documentation / migration notes: selected - validation command, MVT blocker/readiness semantics, thresholds, and progress/validation docs must be updated.

Issue #151 required evidence:

- Preflight artifact: dataset source (`real_imported|deterministic_large_fixture`), minimum segment/model counts, bbox set, thresholds file/version, tile content-type expectation, frontend breakpoints, and evidence root -> redacted JSON with stable missing/unsafe input errors.
- Dataset/fixture manifest: deterministic or consumed data source -> segment/model counts, geometry bounds, bbox sizes, fixture checksum, CRS/geometry assumptions, and generation/consumption mode.
- Threshold artifact test: input thresholds -> versioned JSON with p95 targets, max tile bytes, frontend load/render/memory budgets, oversized bbox behavior, long time-range behavior, object-list limits, and pass/fail semantics.
- Query/latency evidence test: bounded query samples -> model listing, river bbox, flood alert summary/ranking/timeline/map, forecast series, jobs/logs, and tile metadata evidence with row counts, plan text/hash, latency samples, p95, threshold comparison, and live execution flags.
- MVT/readiness blocker test: tile content expectation `application/x-protobuf` with current GeoJSON delivery -> release blocker artifact that identifies affected endpoints and states production tile readiness is not achieved; GeoJSON compatibility path remains truthful.
- Frontend large-layer evidence test: deterministic or real frontend smoke -> desktop/mobile breakpoint records, load/render/timeline/chart timing, memory budget status or not_executed/blocker, and no mock-only live-readiness claim.
- Resource/path/redaction test: oversized bbox, long time range, huge object listing, unsafe run IDs, symlinked evidence paths, credential-shaped API/object URLs, and reruns -> stable blockers, bounded payloads, no cross-run overwrite, and redacted evidence/stdout/docs.
- Local verification commands: OpenSpec strict validation, `uv run ruff check .`, targeted scale/API/tile/frontend-evidence tests, frontend `corepack pnpm test && corepack pnpm build` when UI/generated types change, and documented opt-in `NHMS_RUN_PRODUCTION_CLOSURE=1 ... validate-scale --evidence-root ...`.

Issue #151 non-goals / deferred:

- Production auth/RBAC/alert/rollback readiness is handled by #152.
- Real MVT implementation may be deferred only with an explicit release blocker; #151 must not silently pass production tile readiness on GeoJSON compatibility delivery.
- Full hydrologic skill validation and model calibration quality remain out of scope.
- Permanent production deployment and continuous load testing infrastructure are out of scope; #151 emits bounded validation evidence and blockers.

## Issue #152 Fixture Overlay: Production Ops, Security, and Runbook Readiness

Fixture level: expanded

Why expanded:

- Touches production configuration validation, backend authorization gates, audit/redaction evidence, alert/monitoring rules, rollback drills, and the final acceptance surface for Issues #147-#151.
- Must distinguish deterministic readiness evidence from real production enforcement without leaking credentials or claiming full auth completion when only a release gate is present.

Change surface:

- A new opt-in `validate-ops` production closure lane under `services/production_closure/*` and production closure CLI dispatch.
- Production configuration templates or deterministic template evidence for API, orchestrator, Slurm gateway, tile publisher, frontend, database, object store, source adapters, and workspace roots.
- Backend action authorization/audit evidence for model activation, rerun, cancel, QC override, source config change, and tile republish.
- Alert and rollback evidence helpers, validation docs, `progress.md`, and targeted production ops tests.

Must preserve:

- Fast tests and default CI do not require production identity providers, production credentials, real alert sinks, real object storage, real Slurm, live PostGIS/API/frontend services, or a running scheduler.
- Existing frontend RBAC gates remain compatible, but final readiness cannot rely on frontend-only RBAC for production-impacting backend actions.
- Existing #147/#148/#149/#150/#151 closure lanes and evidence schemas remain readable; #152 consumes their completion status without rewriting their evidence.
- Secret values and signed URLs never appear in config evidence, logs, audit rows, alert payloads, rollback reports, docs, PR comments, or frontend output.
- Deferred auth is explicit: if full backend auth is not implemented, the lane must emit a release-blocking artifact listing affected actions, fallback, required roles, residual risk, and removal criteria.

Must add/change for #152:

- A documented opt-in `validate-ops` lane or equivalent command that records auth mode, required roles, alert target or dry-run sink, deployment config source, rollback drill scope, dependency evidence status for #147-#151, and evidence root under `artifacts/production-closure/<run_id>/ops/`.
- Production config validation evidence for API, orchestrator, Slurm gateway, tile publisher, frontend, database, object store, source adapters, and workspace roots, with stable errors for missing/unsafe settings and no secret disclosure.
- Backend authorization evidence that either enforces role checks for production-impacting actions or blocks them behind an explicit release gate. Required actions: model activation, rerun, cancel, QC override, source config change, and tile republish.
- Audit evidence for allowed, denied, and release-blocked actions, including actor, role, target, previous/new state, decision, reason, lineage, and redacted secret-shaped fields; denied and release-blocked actions must not mutate state.
- Secret redaction regressions across config templates, logs, manifests, audit rows, API payloads, alert payloads, rollback evidence, docs/PR evidence, and frontend-facing outputs.
- Monitoring/alert evidence for source latency, Slurm queue backlog, failed basin retries, object-store failures, stale analysis state, tile errors, and API p95, including severity, sink/dry-run target, current value, threshold, runbook link, and recommended operator action.
- Rollback drill evidence for bad model activation, failed publish/import, failed source cycle, failed Slurm array, and bad tile release, including preconditions, commands, expected evidence, recovery outcome, residual risk, and dependency artifact references.
- Final dependency readiness evidence for #147-#151. The #152 final readiness summary must be release-blocked unless every required dependency surface is accepted; skipped, blocked, not_executed, or deterministic-only dependency summaries are allowed for fast-path fixture validation only when explicitly marked as non-live/non-final.
- Run-scoped idempotent evidence behavior with unsafe run ID rejection, symlink/path containment, explicit same-run `--force`, bounded payloads, and no cross-run overwrite.

Issue #152 risk packs:

- Public API / CLI / script entry: selected - `validate-ops`, authorization/audit simulation, alert evidence, and rollback drill commands need stable JSON/error behavior.
- Config / project setup: selected - auth mode, role map, alert target, deployment config source, service templates, dependency evidence roots, rollback scope, and evidence root are preflight inputs.
- File IO / path safety / overwrite: selected - config templates, audit reports, alert payloads, rollback drill reports, dependency evidence references, and reruns must stay run-scoped and bounded.
- Schema / columns / units / field names: selected - config, action decision, audit, alert, rollback, dependency status, and summary schemas must remain stable.
- Geospatial / CRS / shapefile sidecars: not selected - #152 validates ops/security controls and only references geospatial evidence from #151.
- Time series / forcing / temporal boundaries: selected - source latency, stale analysis state, API p95, and rollback drills reference time windows and freshness thresholds.
- Numerical stability / conservation / NaN: selected - alert thresholds, API p95 samples, stale-state ages, queue backlog counts, and evidence payload metrics must reject malformed/non-finite values.
- Solver runtime / performance / threading: selected - Slurm queue backlog and failed array rollback evidence must preserve #147/#150 runtime linkage without running solvers.
- Resource limits / large input / discovery: selected - alert sets, audit events, dependency evidence references, rollback drill lists, and evidence payloads must be bounded.
- Legacy compatibility / examples: selected - existing frontend RBAC tests, API contracts, production closure lanes, and validation docs must continue to pass.
- Error handling / rollback / partial outputs: selected - rollback drills and release-blocking auth fallback are core #152 acceptance criteria.
- Release / packaging / dependency compatibility: selected - Linux/CI fast path, deployment template portability, optional live auth/alert sinks, and frontend build compatibility must be explicit.
- Documentation / migration notes: selected - production config/runbook/rollback validation docs, known limits, and progress updates must be updated.

Issue #152 required evidence:

- Preflight artifact: auth mode, required roles, alert sink/dry-run target, deployment config source, rollback scope, dependency evidence roots/status, and evidence root -> redacted JSON with stable missing/unsafe input errors.
- Config template evidence: deterministic or supplied production config inputs -> service-by-service checks for API, orchestrator, Slurm gateway, tile publisher, frontend, database, object store, source adapters, and workspace roots, with unsafe/missing settings blocked and secrets redacted.
- Authorization gate evidence: action matrix for model activation, rerun, cancel, QC override, source config change, and tile republish -> allowed/denied/release-blocked decisions, required roles, stable error codes, `execution_mode` from `backend_route_executed|policy_simulated|release_blocked`, `live_backend_auth_executed`, and no mutation on denied or release-blocked actions.
- Audit/redaction evidence: allowed and denied actions plus secret-shaped config/API/frontend/audit values -> redacted audit rows, logs, manifests, payloads, docs, and PR-safe evidence.
- Monitoring/alert evidence: injected or deterministic source latency, Slurm backlog, failed basin retries, object-store failure, stale analysis state, tile error, and API p95 breach -> severity, metric, threshold, observed value, `execution_mode` from `live_sink_delivered|dry_run_sink|not_executed`, `live_alert_sink_delivered`, sink/dry-run target, runbook link, and operator action.
- Rollback drill evidence: bad model activation, failed publish/import, failed source cycle, failed Slurm array, and bad tile release -> command/precondition/evidence/recovery/residual-risk records, dependency artifact references, `execution_mode` from `live_drill|simulated_drill`, and `live_rollback_executed`.
- Dependency closure evidence: accepted or deterministic summaries for #147-#151 -> final #152 summary records each dependency as accepted, skipped, blocked, or not_executed without fabricating live execution, and final readiness is release-blocked unless every required dependency surface is accepted. Deterministic summaries must be labeled `deterministic_fixture` and `final_production_readiness_claimed=false`.
- Run-scoped idempotency/path/redaction test: reruns, unsafe run IDs, symlinked evidence roots, oversized evidence payloads, and credential-shaped auth/config/alert URLs -> no cross-run overwrite, stable blockers, bounded writes, and redacted output.
- Local verification commands: OpenSpec strict validation, `uv run ruff check .`, targeted production ops/auth/redaction/audit/monitoring/rollback tests, frontend tests/build only when UI or generated types change, and documented opt-in `NHMS_RUN_PRODUCTION_CLOSURE=1 ... validate-ops --evidence-root ...`.

Issue #152 non-goals / deferred:

- Full production identity-provider integration may be deferred only with an explicit release blocker; frontend-only RBAC is not sufficient for production backend readiness.
- Permanent deployment automation, live pager routing, and continuous alert delivery infrastructure are out of scope for the deterministic fast lane.
- Real production rollback execution is not required by default; deterministic drills must state when they are simulated and what live evidence would remove the blocker.
- Hydrologic skill validation, true production MVT implementation, and long-running load tests remain outside #152.
