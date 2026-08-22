## Why

node-27 生产库的每一条应用连接 `application_name` 都是空串（issue #1714 证据(3) 实机 `pg_stat_activity` 输出）：autopipe ingest tick、display API 只读连接、parser、retention/compression runner、人工会话在同一张表里无法区分，唯一可用信号 `usename` 又被 autopipe/parser/retention 共用的 `nhms` 角色抹平。

后果已实际发生一次：取证者把一条 `dur=00:06:59` 的 active 语句误判为自己的 pytest 会话并 `pg_cancel_backend`，实际打掉的是每 10 分钟一次的生产 ingest tick（`rc=1`，本轮 ingest 未完成）。归因信号缺失把一次例行诊断变成一次生产作业中断。

`scripts/node27_autopipeline.py:190` 与 `scripts/node27_download_cycles.py:42` 的 `DATABASE_URL_ALLOWED_QUERY_KEYS` **早已允许** `application_name` / `fallback_application_name` —— 允许写、但没有任何组件真的去写。

## What Changes

- node-27 各生产 DB 组件在建连时传入 `fallback_application_name=<组件名>`，使 `pg_stat_activity.application_name` 可区分：
  - `scripts/node27_autopipeline.py` → `nhms-autopipe`
  - `scripts/node27_ingest_run.py` → `nhms-ingest-run`
  - `workers/output_parser` → `nhms-output-parser`
  - `scripts/node27_refresh_coverage.py` → `nhms-refresh-coverage`
  - `apps/api/routes/hydro_display.py` → `nhms-display-api`
  - `scripts/node27_timeseries_retention.py` → `nhms-ts-retention`
  - `scripts/node27_timeseries_compression.py` → `nhms-ts-compression`
- 选用 `fallback_application_name` 而非 `application_name`：libpq 语义下运维在 DSN 里显式写的 `application_name` 仍然优先，代码只提供默认值，**不夺走**运维的覆写能力（实测见 design.md）。
- `docs/runbooks/current-production-ops.md` 增加一段处置纪律：取消任何 backend 前先按 `application_name` 归因；生产 tick 不得随手取消；integration 测试一律走 `NHMS_INTEGRATION_DATABASE_URL`。
- `infra/env/node27-*.example` 注释说明该默认值与覆写方式。

**非破坏性**：零 schema 变更、零 SQL 变更、零 DSN 校验逻辑变更（两个 allowlist 已含这两个 key）。

## Capabilities

**New Capabilities**
- `node27-connection-attribution` —— node-27 生产 DB 连接的归因标识契约。

**Modified Capabilities**
- 无。既有 DSN 校验能力（`DATABASE_URL_QUERY_OVERRIDE_FORBIDDEN` / `DATABASE_URL_ENDPOINT_NOT_NODE27`）行为不变，本 change 只新增归因要求。

## Impact

- 代码：上列 7 个建连面（autopipeline 9 处 connect 走一个模块级 `_connect` 包装；其余各自 1-4 处）。
- 文档：`docs/runbooks/current-production-ops.md`、`infra/env/node27-{ingest,timeseries-compression,timeseries-retention}.example`、`infra/env/display.example`。
- 测试：`application_name` 透传与非法 query key 仍被拒的行为测试；建连面 static meta-guard（防止新增/改名建连面漏挂）。
- 部署：验收标准 1（各 unit 在 `pg_stat_activity` 呈现可区分名字）**是部署门后的收据**，见 design.md「证据分期」。
