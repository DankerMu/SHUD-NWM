# 8.4 / #2517 — `query_indexes` 按 store 路由，node-27 实机部署 receipt（2026-09-20）

部署 `18f53e7f`（PR #2519 合并）到 node-27 并重启 `nhms-display-api`，证明公网不再返回那条假断言。

**`yd-display-api` 全程未动**（MainPID `3163766` 前后一致，:8081 200）。DB 访问只有一次 `BEGIN READ ONLY` 的 `pg_indexes` 目录查询，走 ingest_rw 车道、DSN 仅经 env。未触发任何 timer、pipeline 或压缩。

## 1. 时间线

```
10:18:42  基线：yd MainPID=3163766，抓两条路由的 before 载荷
          树 SHA 18f53e7f（本次部署前已 ff-only pull，工作树 clean）
10:18:4x  systemctl --user restart nhms-display-api.service
          就绪用时 3 s（/openapi.json 200）
          新 MainPID 3211239，ActiveState=active，Result=success
10:18:4x  抓 after 载荷 + 同窗口 pg_indexes
10:18:46  完成
```

## 2. 修复前后：公网 `/api/v1/mvp/qhh/latest-product` 的 forcing 条目

两条路由都用 `run_id` 严格定址，同 basin / 同 model / 同源：

| 路由 | `table` | `index` |
|---|---|---|
| legacy **before** | `met.forcing_station_timeseries` | `forcing_station_timeseries_qhh_latest_window_idx` |
| legacy **after** | **`met.forcing_station_timeseries_legacy`** | `forcing_station_timeseries_qhh_latest_window_idx` |
| narrow **before** | `met.forcing_station_timeseries` | `forcing_station_timeseries_qhh_latest_window_idx` |
| narrow **after** | `met.forcing_station_timeseries` | **`forcing_ts_version_variable_time_key_idx`** |

before 两行**完全相同**——这正是缺陷的形状：一个无参常量替两条路由作答。after 两行各自正确：legacy 腿修的是**表名**（索引名本就属于那条路由），narrow 腿修的是**索引名与 columns**（`forcing_version_key, variable_e, valid_time DESC`，`status` 相应改为 `covered_by_version_variable_time_key_index`）。

载荷存档（已剥 `request_id`）：`before-{legacy,narrow}.json` / `after-{legacy,narrow}.json`。

## 3. 同窗口目录实测，证明 after 两行为真

```
forcing_station_timeseries        | forcing_station_timeseries_narrow_pkey
forcing_station_timeseries        | forcing_ts_version_variable_time_key_idx
forcing_station_timeseries_legacy | forcing_station_timeseries_pkey
forcing_station_timeseries_legacy | forcing_station_timeseries_qhh_latest_window_idx
forcing_station_timeseries_legacy | forcing_station_timeseries_valid_time_idx
```

after 的两个 `(table, index)` 组对都在表内，before 的那个（`forcing_station_timeseries` × `..._qhh_latest_window_idx`）**不在**。

## 4. 没有附带漂移

对两份载荷做全树逐路径比对（下钻每个数组元素）：

```
[legacy] 值变化 1 条，键增删 0 条 —— query_indexes 之外的差异：NONE
[narrow] 值变化 5 条，键增删 3 条 —— query_indexes 之外的差异：NONE
```

narrow 侧键增删 3 条是 `columns` 数组由 6 元素变为 3 元素所致。**除 `query_indexes` 外，两条路由的响应逐值未变**——OpenAPI 形状未动，数据面未动。

## 5. 本 receipt 不主张的事

- 不主张修复消除了任何**性能**问题。`query_indexes` 只是诊断字段；窄腿真实的索引覆盖退化是 **#2516**，未修。
- 索引那一半的自动化 oracle 弱于表那一半：没有任何 rendered SQL 会写出索引名，仓内只能由 `tests/test_migrations.py` 的迁移归属守卫兜底（"迁移这么说"而非"目录这么说"）。**本 receipt §3 是目前唯一一次「目录这么说」的核对**，且是一次性的、不在 CI 里。
- `station_forcing_readiness` 的兄弟修复（同类缺陷，内部无 HTTP 路由）未经本次部署行使，仓内单测覆盖。

## 6. 旁记：本次部署前有非本会话的重启

`journalctl` 显示 `nhms-display-api` 在 **08:15:03 与 08:20:09** 被外部发起的 stop/start 重启过两次（`NRestarts=0`、`Result=success`，即非崩溃自愈）。不是本会话所为——本会话上一次动该 unit 是 2026-09-20 00:27:48 的 `000061` 窗口。两次日志都带 `Unit process ... remains running after unit stopped`，但部署时核查 cgroup 与 `:8080` 持有者，**无孤儿进程残留**。记录于此是因为它意味着该机器同期有其他操作者。
