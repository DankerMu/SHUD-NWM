# Harden node-27 timeseries retention against lock contention

## Why

`nhms-node27-timeseries-retention.service` 在 08-18 与 08-21 两次 exit 1（issue #1664）。
Phase 0 只读取证（node-27，receipt + PG server log 逐字，见 #1664 取证评论）给出的判决：

- **08-21 = `57014`**：`drop_chunks(... 'hydro.river_timeseries')` 于 05:15:00 起、
  05:20:00.772 被 `statement_timeout` 取消，CONTEXT `while locking tuple (0,18) in
  relation "dimension_slice"`。300.7 s 全部花在等锁上，一秒都没进实际删除。
- **08-18 = `40P01`**：不是超时，是 deadlock。server log 逐字给出对手方——
  `Process 298631: SELECT drop_chunks(... 'met.forcing_station_timeseries')` vs
  `Process 298616: INSERT INTO met.forcing_version (...)`。OID 解析：
  `24375 = met.met_station`、`24419 = met.forcing_version`。
- **压缩越线假设被推翻**，且有天然对照：唯一真正重叠的 08-19（压缩 04:25:00–05:28:53）
  retention **成功**；两趟失败的 08-18/08-21 压缩根本不在场。

机制是结构性的：`db/migrations/000005_met.sql:100,102` 把
`met.forcing_station_timeseries` FK 到 `met.forcing_version` 与 `met.met_station`；
`drop_chunks` 删 chunk ≡ `DROP TABLE`，要对被 FK 引用的 plain table 取
`AccessExclusiveLock`。对手方是 `OnUnitActiveSec=10min` 的常驻 autopipe 摄入——
**没有可避开的排期**。

因此 issue AC 3 的「不再间歇」分支不可达，唯一可达的终态是
**「失败快、失败可见、次日幂等自愈」**。本 change 交付这个终态的三件事。

## What Changes

1. **drop 阶段并列 `lock_timeout`**（`_default_drop_chunk`）。取值来自新 env
   `NODE27_TIMESERIES_RETENTION_LOCK_TIMEOUT_MS`（**默认 240_000**），config 期断言
   `0 < x < _DROP_TIMEOUT_MS`，越界以 `RETENTION_CONFIG_INVALID` fail-closed。
   锁等待从此**有界且自证归因**：撞满即 `55P03`（锁），而不是与「删得慢」混叠的 `57014`。
   **记录在案的偏离**：issue 解决思路建议 2–5 s，被 08-19 的 receipt 推翻（见 design D3）。
2. **锁类失败在 `refusal_reason` 里自证**。沿用 #1660 刚落地的前缀先例，
   在既有 wire code + `<hypertable_schema>.<chunk_name>` 之后插入分类段：
   `lock-contention(55P03)` / `lock-contention(40P01)`。
   **不新增 wire code**——`WIRE_CODES` 是跨 4 处（含已归档 #855 fixture）的
   byte-identical 契约，且 AC 只要求「归到锁而非泛超时」。
3. **失败可见**：新增 systemd template unit `nhms-node27-unit-failure-alert@.service`
   + 哑 wrapper，逐字复用 frontier 告警的本地 `sendmail -t -i` 通道与
   `NHMS_ALERT_EMAIL_TO/FROM`；retention 单元挂 `OnFailure=`。
4. **每块 drop 计时进 stderr 诊断**（无 schema 变更）。没有它，`lock_timeout`
   的取值永远无法调优；且 runbook `:3019-3021` 已经声称这个 instrumentation 存在
   （实际不存在），补上让文档变真。
5. **文档同步**：runbook §8.2/§8.6 写明「被锁失败可接受、次日幂等自愈」的判据与
   升级阈值；`infra/env/node27-timeseries-retention.example` 增新 env。

## Non-Goals

- **不动 H5 fail-closed**（整趟拒绝是设计）；**不加 in-run 重试**。issue 已划 out of scope。
- **不调排期、不加 `Conflicts=`**——取证已推翻压缩越线，08-19 是反证。
- **不动枚举/测量的 `_QUERY_TIMEOUT_MS = 60_000` 路径**；issue 边界写的是 drop 阶段。
- **不给其余 20 个单元挂 `OnFailure=`**（全仓 `grep -rn OnFailure infra/` = 0 命中）。
  这是已上报的范围外发现，template unit 让后续每单元只需一行，但本 PR 只挂 retention。
- **不碰 `scripts/node27_river_identity_backfill.py`** —— #1476 的地盘。
- **不改 1648 行的 frontier 告警状态机**，只复用其邮件通道形状。
