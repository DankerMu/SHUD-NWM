## Context

Issue #1714（`priority:medium`，pre-existing）。风险三元组：**生产配置面** + **多组件建连面** + **一次已发生的运维事故**。因此 fixture level = `expanded`，repair intensity = `high`。

## Goals / Non-Goals

**Goals**
- node-27 各生产组件的 DB 连接在 `pg_stat_activity` 中可按组件区分。
- 运维仍可通过 DSN 显式覆写标识（代码只给默认值）。
- 既有 DSN 校验面不被绕过、不被放松。

**Non-Goals**
- 不改 `scripts/node27_autopipeline.py:930` 那条 join 的 SQL / 访问路径（#1686 本体，issue 明写 **不得** 改）。
- 不改 retention 超时（#1664）、不改只读边界语义、不改写路径。
- 不给全仓约 40 个 `psycopg2.connect` 站点普遍挂标识。**范围线是「能否从某个在册组件的入口到达」，不是「文件在哪个目录」**（round-2 修复：`packages/common/display_watermark.py` 与 `packages/common/display_coverage.py` 位于 `packages/common/`，却在每个生产 tick 上替在册组件开连接，因此在范围内）。确认**够不到**在册组件入口、因而明确非目标的建连面：`packages/common/{state_manager,met_store,grid_registry_store,best_available,model_registry}.py`、`packages/common/forecast_store.py`（`display_coverage` 只 import 它的两个常量）、`workers/model_registry/basins_registry_import.py::_transaction`（autopipeline 只 import 收游标的 `_backfill_output_segment_geometry`）、`apps/api/routes/pipeline.py::_engine`（`hydro_display` 只 import `_ok`）、qhh 系脚本、`services/*`。如需覆盖另立 issue。
- 不下线 `NHMS_ALLOW_DATABASE_URL_INTEGRATION` 兼容后门（人裁定选了收窄版 (c)，纪律走文档不走机制）。

## Decisions

### D1 —— 用 `fallback_application_name`，不用 `application_name`

libpq 语义：`application_name` 一旦在 conninfo 出现即为终值；`fallback_application_name` 只在没有 `application_name` 时生效。代码传 fallback 即「给默认、不夺覆写」。

实测（`psycopg2.extensions.make_dsn`，本机 3.11 环境）：

```
make_dsn('postgresql://u:p@127.0.0.1:55432/nhms', fallback_application_name='nhms-autopipe')
  -> 'user=u password=p dbname=nhms host=127.0.0.1 port=55432 fallback_application_name=nhms-autopipe'
make_dsn('postgresql://u:p@127.0.0.1:55432/nhms?application_name=custom', fallback_application_name='nhms-autopipe')
  -> '... application_name=custom fallback_application_name=nhms-autopipe'
```

即 kwarg 与 URL 串正确合并，显式 `application_name` 与 fallback 共存且前者胜出。

### D2 —— 不新建 `packages/common` 共享 helper

新增共享 helper 会把 blast radius 从「8 个已知组件」扩到「任何 import 它的模块」，并触发 shared-helper 高强度审查面，对一个 S 规模改动不划算。做法：每个组件在模块内定义一个 `_APPLICATION_NAME` 常量并在自身建连处传参；`scripts/node27_autopipeline.py` 的 9 处 connect 收敛到一个模块级 `_connect(database_url, **kwargs)` 包装（**只加 kwarg，不改任何现有 connect 的其它参数**）。

**D2 补充（round-2）**：在册组件把连接**委托**给一个既有 `packages/common` helper 时，做法是给该 helper 一个**向后兼容的可选 `connect` 参数**（`fetch_display_watermark` 早就有；`refresh_all_run_display_coverage` 本单新增），调用方注入自己的模块级 `_attributed_connect` 包装。这**不是**新增共享 helper，D2 的禁令（不得向 `packages/common` 添加新模块）保持不变。默认值必须写成 `connect: Callable[..., Any] | None = None` 并在调用时解析 `psycopg2.connect`——写进签名默认值会在 import 时绑定，绕过既有测试对模块属性的 monkeypatch。

漂移防护交给 static meta-guard 测试（见 tasks.md T5/T5b）：T5 持有 `(文件, 期望名字)` 清单并 AST 校验各文件内的建连面；T5b 再遍历每个在册组件的一等公民 import 闭包，把**任何自带建连面的模块**要么判为 `attributed`（组件注入了具名 connect，且 helper 仍保留注入 seam）要么判为 `unreachable`（记录够不到的理由）。新增建连面、新增委托、改名、或 helper 丢掉 `connect` seam，任一即红。

### D3 —— display API 走 SQLAlchemy `connect_args`

`apps/api/routes/hydro_display.py:137` 的 `create_engine` 走 psycopg2 DBAPI，`connect_args={"fallback_application_name": "nhms-display-api"}` 透传到 DBAPI connect。`_engine` 带 `@lru_cache`，标识是常量、不入 cache key，无缓存穿透问题。

### D4 —— 命名口径

`nhms-<组件>`，全小写连字符，均 ≤ 63 字节（PostgreSQL `application_name` 截断边界 NAMEDATALEN-1）。autopipe 的三个子进程（`node27_ingest_run` / `output_parser` / `node27_refresh_coverage`）各自独立命名——这正是运维需要的相位级归因，而不是把整条 tick 混成一个名字。

### D5 —— 证据分期（部署门）

验收标准 1「各生产 unit 在 `pg_stat_activity` 呈现可区分 `application_name`」**在合并前不可能兑现**：它要求代码已跑在生产 systemd unit / display 容器里。绝不把未合并分支的代码投进生产 autopipe timer 或 display 容器。

- **合并前**（Phase 2/8 自审依据）：单测 + 校验面透传测试 + meta-guard；外加一次 node-27 分支 worktree 的**只读建连探针**（一次性脚本调用，另一 psql 会话观察到该连接带新名字），证明机制成立。
- **合并后**（部署步骤，收据回帖到 PR/issue）：`git pull` + 重启各 unit 后，一条覆盖全部生产 unit 的 `pg_stat_activity` 输出。

## Invariant Matrix

**Governing invariant**：node-27 每一条生产 DB 连接在 `pg_stat_activity` 中携带一个**唯一标识其组件**的 `application_name`；该默认值由代码提供，且**永不覆盖**运维在 DSN 中显式配置的 `application_name`；引入该默认值不得改变任何一条既有连接的其它连接参数、事务语义或 DSN 校验结论。

**Source-of-truth identity/contract**：各组件模块内的 `_APPLICATION_NAME` 常量 + `DATABASE_URL_ALLOWED_QUERY_KEYS`（`application_name` / `fallback_application_name` 已在册）。

**Surfaces**
- Producers（自有建连面）：`scripts/node27_autopipeline.py::_connect`（9 处 connect 收敛点）、`scripts/node27_ingest_run.py:235`、`scripts/node27_refresh_coverage.py:83`、`workers/output_parser/parser.py::_connect`、`apps/api/routes/hydro_display.py::_engine`、`scripts/node27_timeseries_retention.py:615/741/805`、`scripts/node27_timeseries_compression.py:479/523/573/592`
- Producers（**委托建连面** —— 在册组件入口可达、但连接开在 import 进来的 helper 里；round-2 补齐）：
  - `packages/common/display_watermark.py::fetch_display_watermark`（内部 `connect(dsn, connect_timeout=5)`）—— 由 `node27_timeseries_retention.main()`、`node27_timeseries_compression.main()`、`node27_raw_retention.main()` 触达，是每个生产 tick 的**第一条**连接
  - `packages/common/display_coverage.py::refresh_all_run_display_coverage` 内的 `refresh_one`（每 run 一条连接，`ThreadPoolExecutor` 最多 8 并发）—— 由 `node27_refresh_coverage.main(--all)` 触达，而 `--all` 在**每个** autopipe tick 上跑（`scripts/node27_autopipe_cron.sh:229`）
  - 归因手段：各组件模块级 `_attributed_connect(*args, **kwargs)` 包装（内部 `psycopg2.connect(..., fallback_application_name=_APPLICATION_NAME, ...)`）经 helper 的可选 `connect=` 参数注入；helper 自身的 SQL、只读会话、`statement_timeout`、`connect_timeout` 逐字不变
  - `scripts/node27_raw_retention.py` 没有任何自有建连面，其**唯一** DB 触点就是上面这条委托 watermark 查询
- Validators/preflight：`scripts/node27_autopipeline.py:188-194` + `_database_preflight`、`scripts/node27_download_cycles.py:40-46,83-88`（**只读不改**，仅需证明结论不变）
- Storage/cache/query：`apps/api/routes/hydro_display.py::_engine` 的 `@lru_cache`（标识为常量，不入 key）
- Public routes/entrypoints：display API 全部路由（经 `get_hydro_display_session`）；上列各脚本的 `main()`
- Frontend/downstream consumers：无（`application_name` 不进任何响应体、不进任何持久化行）
- Failure paths/rollback/stale state：各 connect 的异常路径 —— 传入 kwarg 不得改变既有 `connect_timeout` / `cursor_factory` / 凭据脱敏错误文案（`node27_timeseries_retention.py:765` 明写依赖脱敏）
- Evidence/audit/readiness：`docs/runbooks/current-production-ops.md` 处置段；PR 上的合并前只读探针收据与合并后部署收据

**Regression rows**
- autopipe 以不带 query 串的合法 node-27 DSN 建连 → 连接成功，conninfo 含 `fallback_application_name=nhms-autopipe`
- DSN 已显式带 `?application_name=operator-override` → 校验器仍放行（key 在册），且 `application_name` 为 `operator-override`（fallback 不夺权）
- DSN 带非法 query key（如 `?host=evil`）→ 仍被 `DATABASE_URL_QUERY_OVERRIDE_FORBIDDEN` 拒绝，行为与改前逐字一致
- display API `_engine` 构造 → `connect_args` 含 fallback 名，且 `pool_size` / `max_overflow` / `pool_pre_ping` / `pool_recycle` 参数逐字不变
- retention 连接失败路径 → 错误文案仍脱敏，不因新 kwarg 泄露凭据
- compression 的具名 server-side cursor 路径（`:479`）→ `cursor_factory` 与 `connect_timeout` 不变，仅多一个 kwarg
- retention / compression / raw-retention 的 `main()` 在不注入 `now` 的生产形态下 → watermark 那条连接下发 `fallback_application_name=<组件在册名>`，且 `connect_timeout=5` 与 `display_watermark.py` 的 `set_session(readonly=True, autocommit=False)` / `SET LOCAL statement_timeout = '5s'` 逐字不变
- `node27_refresh_coverage.main(["--all", ...])` → `main()` 自身连接 **加上每一条 per-run worker 连接**都带 `fallback_application_name=nhms-refresh-coverage`
- 不传 `connect=` 的既有 `refresh_all_run_display_coverage` 调用方（`tests/test_display_coverage_parallel.py` 等）→ 行为逐字不变，`monkeypatch.setattr(display_coverage.psycopg2, "connect", ...)` 仍然生效
- 在册组件 import 闭包里新出现一个自带建连面的模块 → T5b discovery 断言变红，必须显式归类为 `attributed` 或 `unreachable`
- 未列入清单的兄弟建连面（`packages/common/state_manager.py:937` 等，以及确认够不到在册入口的 `forecast_store` / `basins_registry_import` / `apps/api/routes/pipeline.py`）→ 保持零 diff，行为不变

## Boundary Surfaces Checklist

- 共享 helper 根：**无新增模块**（D2）；`display_watermark.py` / `display_coverage.py` 只加向后兼容的可选 `connect` 参数，既有调用方零改动
- 公共入口：8 个组件的 `main()` / FastAPI 依赖注入；标识不改变任何 CLI 参数面
- 读面：display API 只读连接——不得因新 kwarg 触碰 `NHMS_DISPLAY_DISABLE_CONTROL_MUTATIONS` / 只读角色边界
- 写/删除/覆写面：本 change 零涉及
- staging/publish/rollback 面：本 change 零涉及
- producer/consumer 证据边界：runbook 处置段是唯一新增的运维契约面
- stale-state/幂等边界：`_engine` 的 `lru_cache`（D3）
- 未变更的下游消费者：在册组件够不到的 `packages/common/*` 模块与 qhh 系脚本的建连面（明确非目标，须零 diff）
