## ADDED Requirements

### Requirement: node-27 生产 DB 连接携带组件级归因标识

node-27 上每一个生产数据库组件 SHALL 在建立连接时提供一个唯一标识该组件的默认 `application_name`，使 `pg_stat_activity` 可按组件归因。该默认值 MUST 通过 libpq 的 `fallback_application_name` 提供，因此 MUST NOT 覆盖运维在 `DATABASE_URL` 中显式配置的 `application_name`。

在册组件与标识：`scripts/node27_autopipeline.py` = `nhms-autopipe`；`scripts/node27_ingest_run.py` = `nhms-ingest-run`；`workers/output_parser` = `nhms-output-parser`；`scripts/node27_refresh_coverage.py` = `nhms-refresh-coverage`；`apps/api/routes/hydro_display.py` = `nhms-display-api`；`scripts/node27_timeseries_retention.py` = `nhms-ts-retention`；`scripts/node27_timeseries_compression.py` = `nhms-ts-compression`；`scripts/node27_raw_retention.py` = `nhms-raw-retention`。

本要求覆盖在册组件的**每一条**连接，包括它委托给共享 helper 打开的连接：范围线是「能否从在册组件的入口到达」，不是「建连代码写在哪个文件」。

#### Scenario: 未配置 application_name 的 DSN 取得组件默认标识

- **WHEN** 在册组件用一个不含 `application_name` query 参数的合法 node-27 `DATABASE_URL` 建连
- **THEN** 实际下发的 conninfo 携带 `fallback_application_name=<该组件的在册标识>`
- **AND** 该连接的其它连接参数（`connect_timeout`、`cursor_factory`、连接池参数）与引入本要求之前逐字相同

#### Scenario: 运维显式配置的 application_name 优先

- **WHEN** `DATABASE_URL` 显式带 `application_name=operator-override`
- **THEN** 连接的 `application_name` 为 `operator-override`
- **AND** 组件默认值仅作为 `fallback_application_name` 共存，不夺权

#### Scenario: 组件委托给共享 helper 打开的连接同样被归因

- **WHEN** 在册组件在其入口路径上调用一个自己打开数据库连接的共享 helper（如 watermark 只读查询、per-run coverage worker）
- **THEN** 该连接下发的 conninfo 同样携带 `fallback_application_name=<该组件的在册标识>`
- **AND** 该 helper 既有的 SQL、只读会话设置、`statement_timeout` 与 `connect_timeout` 逐字不变
- **AND** 未注入标识的既有调用方行为逐字不变

#### Scenario: 新增或改名建连面被静态守卫拦下

- **WHEN** 在册组件中出现一个未携带组件标识的数据库建连面，或某组件的标识被改成与在册清单不符的值
- **THEN** 静态守卫测试失败并指出该文件与期望标识

#### Scenario: 新的委托建连面被静态守卫拦下

- **WHEN** 在册组件的 import 闭包里出现一个自带数据库建连面、却未被归类的模块，或某个已归类为「已归因」的委托调用点丢掉了标识注入，或被委托的 helper 移除了注入 seam
- **THEN** 静态守卫测试失败并指出该组件、该模块，以及应补的归类（已归因 / 够不到）

### Requirement: DSN 校验面不因归因标识被放松

引入组件默认标识 SHALL NOT 改变既有 `DATABASE_URL` 校验结论。`application_name` 与 `fallback_application_name` 已在 `DATABASE_URL_ALLOWED_QUERY_KEYS` 中，本要求 MUST NOT 扩大该 allowlist，且非法 query key MUST 继续被拒绝。

#### Scenario: 合法的 application_name query key 仍被放行

- **WHEN** `DATABASE_URL` 带 `?application_name=x` 或 `?fallback_application_name=x`
- **THEN** `node27_autopipeline` 与 `node27_download_cycles` 的 DSN 预检均放行，不产出 `DATABASE_URL_QUERY_OVERRIDE_FORBIDDEN`

#### Scenario: 非法 query key 仍被拒绝

- **WHEN** `DATABASE_URL` 带一个不在 allowlist 中的 query key
- **THEN** 预检产出 `DATABASE_URL_QUERY_OVERRIDE_FORBIDDEN` blocker，行为与引入本要求之前一致

### Requirement: 取消生产库 backend 前必须先归因

生产运维文档 SHALL 载明：在生产库上执行 `pg_cancel_backend` / `pg_terminate_backend` 之前，MUST 先用 `pg_stat_activity.application_name` 确认该 backend 的归属；生产 tick（ingest/parser/retention/compression）MUST NOT 被随手取消。文档 SHALL 同时载明 integration 测试一律经 `NHMS_INTEGRATION_DATABASE_URL` 指定库，不依赖裸 `DATABASE_URL`。

#### Scenario: 运维查到一条长查询

- **WHEN** 运维在 `pg_stat_activity` 看到一条长时间 active 的语句
- **THEN** runbook 指示先读 `application_name` 归因到具体组件，再决定是否取消
- **AND** 若归因为生产 tick，runbook 明确禁止随手取消并给出替代处置
