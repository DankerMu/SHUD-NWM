## Risk Triage

```text
Issue type: bugfix (ops enablement)
Project profile: NHMS (openspec/project-profile.md)
Blast radius: medium
Fixture level: expanded
Repair intensity: high
Upstream suggested level: absent (hand-written issue; expanded 由强制触发词 `CLI`/`entrypoint`/`config`/`example` + profile 的生产配置面推导)
Why:
- 触及 8 个 node-27 生产组件的建连面（entrypoint / CLI 强制触发），含 2 个被委托的既有 packages/common helper
- 触及 infra/env `*.example` 模板（example 强制触发）
- 生产配置面 + 一次已发生的运维事故 -> repair intensity high
- 验收标准 1 是部署门后的实机收据，需证据分期（design.md D5）
OpenSpec change: node27-connection-attribution (generated)
Evidence floor:
- uv run ruff check .
- uv run pytest -q tests/test_node27_connection_attribution.py 及各组件既有套件
- openspec validate node27-connection-attribution --strict --no-interactive
- node-27 只读建连探针收据（合并前）+ 部署后 pg_stat_activity 全 unit 收据（合并后）
```

## Risk Packs

| Pack | 选择 | 理由 |
|---|---|---|
| Public API / CLI / script entry | selected | 8 个组件的建连面均在 CLI/服务入口上；标识必须不改变任何参数面 |
| Config / project setup | selected | `infra/env/node27-*.example` + display.example 注释；生产 DSN 口径 |
| File IO / path safety / overwrite | not selected | 本 change 零文件读写行为改动 |
| Schema / columns / units / field names | not selected | 零 schema、零 SQL、零列改动 |
| Auth / permissions / secrets | selected | DSN 携带凭据；新 kwarg 不得破坏 `node27_timeseries_retention.py:765` 依赖的凭据脱敏，也不得放松只读角色边界 |
| Concurrency / shared state / ordering | selected | `_engine` 的 `@lru_cache`；autopipe 的 flock + 子进程并发建连 |
| Resource limits / large input / discovery | not selected | 不改连接池大小、不改超时、不改批量 |
| Legacy compatibility / examples | selected | `.example` 模板改动；既有不带标识的运维 DSN 必须继续可用 |
| Error handling / rollback / partial outputs | selected | 建连失败路径的错误文案与脱敏不得回退 |
| Release / packaging / dependency compatibility | not selected | 无新依赖；`fallback_application_name` 是 libpq 既有能力 |
| Documentation / migration notes | selected | runbook 处置段是验收标准之一 |
| 地理空间 / CRS / basin 几何 | not selected | 无关 |
| 水文气象时序 / forcing 窗口 | not selected | 无关 |
| SHUD 数值运行时 | not selected | 无关 |
| PostGIS / TimescaleDB 域行为 | not selected | 不触碰 hypertable / 压缩 / PostGIS 语义 |
| Slurm 生产生命周期 | not selected | node-22 面，无关 |
| 外部气象 provider | not selected | 无关 |
| run manifest / QC 溯源 | not selected | 无关 |
| 已发布 NHMS 制品 / display 身份 | selected | display API 建连面在内，须证明只读边界与身份摘要零影响 |

## Tasks

- [x] **T1** `scripts/node27_autopipeline.py`：新增模块级 `_APPLICATION_NAME = "nhms-autopipe"` 与 `_connect(database_url, **kwargs)` 包装（内部 `psycopg2.connect(database_url, fallback_application_name=_APPLICATION_NAME, **kwargs)`），把 9 处 `psycopg2.connect`（`:876 :922 :1038 :1065 :1113 :1146 :1359 :1405 :1551`）改为调用它。**除新增 kwarg 外，任何一处的既有参数不得改动。**
- [x] **T2** 其余 6 个组件各自挂上标识：
  - `scripts/node27_ingest_run.py:235` → `nhms-ingest-run`（保留 `cursor_factory=RealDictCursor`）
  - `scripts/node27_refresh_coverage.py:83` → `nhms-refresh-coverage`
  - `workers/output_parser/parser.py` 的 `_connect` → `nhms-output-parser`（保留 `connect_timeout`）
  - `apps/api/routes/hydro_display.py:137` `_engine` → `connect_args={"fallback_application_name": "nhms-display-api"}`，**其余 `create_engine` 参数逐字不变**
  - `scripts/node27_timeseries_retention.py:615/741/805` → `nhms-ts-retention`（保留 `cursor_factory`）
  - `scripts/node27_timeseries_compression.py:479/523/573/592` → `nhms-ts-compression`（保留 `connect_timeout` / `cursor_factory` / 具名 cursor 路径）
- [x] **T3** 行为测试：在册组件在无 `application_name` 的 DSN 下，实际下发的 conninfo 含 `fallback_application_name=<在册名>`（用 monkeypatch 捕获 `psycopg2.connect` 实参 + `psycopg2.extensions.make_dsn` 断言合并结果；display API 断言 `create_engine` 的 `connect_args`）。输入：`postgresql://u:p@127.0.0.1:55432/nhms`；期望输出：各组件在册名字。
- [x] **T4** 覆写与校验面测试：
  - DSN 带 `?application_name=operator-override` → `make_dsn` 结果中 `application_name=operator-override` 与 fallback 共存
  - `node27_autopipeline` 与 `node27_download_cycles` 的 DSN 预检对 `application_name` / `fallback_application_name` 放行（零 blocker）
  - 对一个 allowlist 外的 query key 仍产出 `DATABASE_URL_QUERY_OVERRIDE_FORBIDDEN`（与改前逐字一致）
- [x] **T5** static meta-guard：测试持有 `(文件路径, 期望标识)` 的 8 条在册清单，AST 遍历各文件的 `psycopg2.connect` / `create_engine` 调用，断言每一处都带上该组件的标识常量；新增未挂标识的建连面或标识改名即失败。
- [x] **T6** `docs/runbooks/current-production-ops.md` 新增处置段：`application_name` 归因表（8 个在册名 + `psql` = 人工会话 + TimescaleDB 后台 worker）、委托建连面同样带名的说明、「cancel 前先归因、生产 tick 不得随手取消」、integration 一律走 `NHMS_INTEGRATION_DATABASE_URL`。
- [x] **T7** `infra/env/node27-ingest.example`、`node27-timeseries-compression.example`、`node27-timeseries-retention.example`、`node27-raw-retention.example`、`display.example` 各加一行注释：组件已自带默认 `application_name`，如需覆写在 DSN 加 `?application_name=<name>`。
- [x] **T8** 回归证据：`Invariant Matrix` 的每条 regression row 逐条给出证据或引夹具说明为何越界；特别是「未列入清单的兄弟建连面零 diff」用 `git diff --stat` 佐证。
- [x] **T9** 非目标自证：`scripts/node27_autopipeline.py:930` 的 `_already_ingested_runs` SQL 逐字不变（`git diff` 该区间为空）。

### Round-2 修复（cross-review CONFIRMED / FIX_NOW，同一失效类：委托建连面）

- [x] **T10** `packages/common/display_watermark.py::fetch_display_watermark` 的调用方注入具名 connect：`node27_timeseries_retention.py`、`node27_timeseries_compression.py`、`node27_raw_retention.py` 各定义模块级 `_attributed_connect`（内部 `psycopg2.connect(..., fallback_application_name=_APPLICATION_NAME, ...)`，psycopg2 懒 import）并以 `connect=` 传入。helper 的 SQL / `set_session(readonly=True, autocommit=False)` / `SET LOCAL statement_timeout = '5s'` / `connect_timeout=5` 逐字不变。
- [x] **T11** `packages/common/display_coverage.py::refresh_all_run_display_coverage` 新增**向后兼容**的可选 `connect: Callable[..., Any] | None = None`（调用时解析 `psycopg2.connect`，不在签名默认值里绑定），透传到 `refresh_one`；`node27_refresh_coverage.py` 传入 `_attributed_connect`，使 `--all` 下最多 8 条并发 worker 连接全部具名。不传 `connect=` 的既有调用方零改动。
- [x] **T12** 注册第 8 个组件 `scripts/node27_raw_retention.py` = `nhms-raw-retention`（自有 `infra/env/node27-raw-retention.example`，唯一 DB 触点即上述委托 watermark 查询），并同步 T5 清单、runbook 归因表、env example 注释。
- [x] **T13** T5b 委托建连面 guard（关掉失效类，而非只补两个调用点）：
  - discovery —— 遍历每个在册组件的一等公民 import 闭包，收集**任何**自带建连面的模块（识别 `psycopg2.connect` / `create_engine` 的调用**与裸属性引用**，后者正是 `display_watermark.py` 逃过 round-1 的形状）；
  - classification —— `DELEGATED_CONNECT_CLOSURE` 单一真源把每个发现的模块判为 `attributed`（AST 断言组件每个调用点都传 `connect=_attributed_connect`，且 helper 仍保留 keyword-only `connect=None` seam，且 `_attributed_connect` 运行时确实盖上在册名）或 `unreachable`（记录够不到的理由）；
  - 新增委托、掉了归因、helper 丢 seam、闭包里冒出新的建连模块，任一即红。
- [x] **T14** 行为测试走真实入口：`retention.main(argv=[])` / `compression.main([])` / `raw_retention.main([])`（均不注入 `now`，即生产 CLI 形态）探针断言 watermark 那条连接带在册名 + `connect_timeout=5`；`node27_refresh_coverage.main(["--all", "--workers", "4", ...])` 断言 `main()` 自身连接与**每一条** worker 连接都带 `nhms-refresh-coverage`。

## Verification

```bash
uv run ruff check .
uv run pytest -q tests/test_node27_connection_attribution.py
# 既有套件（仓内实际文件名，已按实施时对齐）
uv run pytest -q tests/test_node27_autopipeline_preflight.py tests/test_node27_autopipeline_handoff.py \
  tests/test_node27_download_cycles.py tests/test_node27_ingest_run.py \
  tests/test_node27_timeseries_retention.py tests/test_node27_timeseries_compression.py \
  tests/test_node27_timeseries_compression_capture.py tests/test_node27_timeseries_compression_supervisor.py \
  tests/test_node27_timeseries_compression_benchmark.py tests/test_node27_timeseries_compression_prearm.py \
  tests/test_node27_timeseries_decompression_replay.py tests/test_node27_timeseries_compression_live_evidence.py \
  tests/test_output_parser.py tests/test_output_parser_cli.py tests/test_output_parser_dual_write.py
# display 面（导入 apps/api/routes/hydro_display.py 的套件）
uv run pytest -q tests/test_api_contract.py tests/test_direct_grid_display_cutover_flip.py \
  tests/test_direct_grid_display_cutover_model_resolution.py tests/test_direct_grid_display_cutover_history.py \
  tests/test_display_publish_status_only.py tests/test_hhe_mvt_binding.py \
  tests/test_hydro_display_mvt_scaling.py tests/test_openapi_drift.py
# round-2：第 8 个在册组件 + 被委托的 packages/common helper 的既有套件
uv run pytest -q tests/test_node27_raw_retention.py tests/test_display_coverage_parallel.py \
  tests/test_display_coverage_refresh.py tests/test_display_watermark.py
openspec validate node27-connection-attribution --strict --no-interactive
```

注：`tests/test_node27_autopipeline.py` / `tests/test_hydro_display_api.py` 在仓内不存在，
已替换为上列实际文件。`tests/test_node27_timeseries_compression_benchmark.py::test_before_and_after_slices_merge_into_exact_live_evidence_contract`
把工作树文件与 `git rev-parse HEAD` 的 blob 逐字节比对，因此在改动未 commit 前必红；
commit 后转绿（证据见实施报告）。

## Evidence Mapping

| 验收标准（issue #1714） | 证据 | 时机 |
|---|---|---|
| 1. autopipe/display/parser/retention 呈现可区分 `application_name` | 合并前：node-27 分支 worktree 只读建连探针 + 另一 psql 会话观察到新名字；合并后：`git pull` + 重启各 unit 后一条覆盖全 unit 的 `pg_stat_activity` 输出 | 分期（design.md D5） |
| 2. DSN 校验面不被绕过，且有 `application_name` 透传与非法 key 的测试覆盖 | T3 / T4 / T5 / T5b(T13) / T14 | 合并前 |
| 3. runbook 增加「cancel 前先归因」处置 | T6 | 合并前 |
| 4. 全量 pytest 纪律裁定 | 人裁定 = **收窄版 (c)**：不立「全量 pytest」纪律（实测该威胁模型不成立 —— `tests/conftest.py:190` 无 opt-in 时无条件 skip 全部 integration；`:224/:238` 每次建 `nhms_it_<uuid>` throwaway 库并 drop，从不写生产 `nhms` 库）。落到 T6 的两句 runbook 纪律。 | 合并前 |

## Non-Goals（自审用）

- 不改 `node27_autopipeline.py:930` 的 join SQL（#1686）
- 不改 retention 超时（#1664）
- 不覆盖 `packages/common/*`、`services/*`、qhh 系脚本的建连面
- 不下线 `NHMS_ALLOW_DATABASE_URL_INTEGRATION`
- 不改服务端 / 角色级 `statement_timeout`
