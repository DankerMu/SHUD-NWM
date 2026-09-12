# Proposal: apply-node27-pending-schema-migrations (#2145)

## Why

`#2031`（PR #2149，merge `60ab5528`）把两个全国 digest 改成投影
`core.river_network_version.geometry_generation`，但该 change 的 `design.md` D6 刻意不在生产上跑 DDL，
因此「施加迁移 + 重启 display + live receipt」这一步在合并后无人承接。

node-27 实测（2026-09-12，只读）：

- 活动树 `/home/nwm/NWM` = `a8db554d`，**已含** `60ab5528`（`git merge-base --is-ancestor` 判 YES）。
- 活库 `core.river_network_version` 的列集合是
  `basin_version_id, checksum, created_at, river_network_version_id, river_network_version_key,
  segment_count, source_uri, version_label` —— **`geometry_generation` 不存在**。
- 生产 display 进程被 `nhms-display-api.service.d/60-reslice-pin-original-5a86841c.conf` 钉在
  `/home/nwm/NWM-reslice-original-5a86841c`（`5a86841c`，**不含** `60ab5528`）。

也就是说：**当前没有 500，唯一原因是那条 reslice pin 把生产钉在了旧代码上**。pin 一撤（#2162 的活）
或活动树被任何理由重启，三条读路由与 `/api/v1/layers` 立刻全 500（issue #2145 正文 + PR #2190 已在活库上实测复现同一个 500）。

写侧同样硬依赖：`workers/model_registry/basins_registry_import.py::_backfill_output_segment_geometry`
在改写 geometry 的同一事务里 `UPDATE ... SET geometry_generation = geometry_generation + 1`；
列不在时 `UndefinedColumn` 让整个 import 事务回滚。

但写侧**当前同样被那条 pin 挡着，不是活风险**——这是执行窗口里用一手证据推翻的先前判断，记在此处而非悄悄改掉：
同一个 drop-in `60-reslice-pin-original-5a86841c.conf` **也覆盖 `nhms-node27-autopipe.service`**
（实测 `systemctl --user show ... -p DropInPaths` 只有这一条；其 `ExecStart=` 先清空再指向
`/home/nwm/NWM-reslice-original-5a86841c/scripts/node27_autopipe_cron.sh`，并设
`PYTHONPATH` / `NODE27_AUTOPIPE_REPO` / `NODE27_AUTOPIPE_ENV_FILE` 全部指向该固定树），
而该树的 `workers/model_registry/basins_registry_import.py` 里 `geometry_generation` **一次都不出现**
（活动树出现 1 次）。2026-09-12 18:42 CST 在跑的那个 tick，进程命令行实测就是
`/home/nwm/NWM-reslice-original-5a86841c/.venv/bin/python .../scripts/node27_autopipeline.py`。

所以读写两侧由**同一条 pin** 同时围栏；#2162 撤 pin 的那一刻两侧一起变活。
这不改变本单的动作（先把迁移施加上，让 #2162 撤 pin 时两侧都有列可读可写），
只是把「写侧已经在流血」修正为「写侧和读侧一样，是 #2162 撤 pin 后立刻流血」。

## What Changes

**零代码改动。** 本 change 的交付物是一次 node-27 实机运维动作与其 live receipt：

1. 在一个 writer-timer 停机窗口内，对活库施加**待施加集合**（见下，3 个文件而非 1 个），
   走 `packages.common.migrate`（autocommit 逐语句 + 写 `public.schema_migrations` 账本；
   `psql -f` 不写账本，会让下一次 bring-up 静默重放）。
2. 迁移前后各跑一次角色审计：施加前 audit-only（`do_roles=off do_ownership=off do_audit=on
   strict_audit=on`），施加后跑完整 `scripts/node27_provision_write_roles.sh` 并确认审计干净（#1774 §9.6）。
3. 用活动树 `a8db554d` 自身的 `.venv` 在 **:8090** 起一次性 uvicorn，对施加前/后各跑一遍
   三条读路由与 `/api/v1/layers`，把「施加前 500 / 施加后非 500」作为本 receipt 自带的红→绿证据；
   并重跑 `/home/nwm/tmp/receipt2033/` 的比对脚本，补上 #2145 欠 PR #2190 的那两层瓦片字节/ETag/cache-key 恒等比对。
4. receipt 落 `docs/runbooks/receipts/2026-09-12-issue-2145-geometry-generation-migration-node27.md`，并在 #2031 回链。

### 待施加集合（实测，已按 issue step 2 对账）

账本 56 行、最高 `000055`；磁盘 `db/migrations/` 与 master **逐字节相同**
（`git diff a8db554d 716c4678 -- db/migrations/` 为空），差集是 **3 个**文件：

| 文件 | 来源 issue | 语句 | 幂等 |
|---|---|---|---|
| `000056_hydro_run_parsed_at.sql` | #1789（CLOSED） | `ALTER TABLE hydro.hydro_run ADD COLUMN IF NOT EXISTS parsed_at TIMESTAMPTZ` | 是 |
| `000057_river_network_version_geometry_generation.sql` | #2031（CLOSED，本单目标） | `ALTER TABLE core.river_network_version ADD COLUMN IF NOT EXISTS geometry_generation INTEGER NOT NULL DEFAULT 0` | 是 |
| `000058_hot_timeseries_chunk_interval_3d.sql` | #2210（CLOSED） | `set_chunk_time_interval(...)` ×2，仅影响**新建** chunk | 是（文件自述可重跑） |

`000056` 的列**在库上已经存在**（2026-09-12 实测：`hydro.hydro_run.parsed_at` 存在，且 `/api/v1/runs` 的行键已含
`parsed_at`），但文件名不在账本——即它曾被某条**不写账本**的路径施加过。**具体成因未确证**，单次观测（列在、账本不在）推不出唯一来源；
有据的候选至少两条：runbook 点名的 `psql -f`，以及 `infra/env/node27-timeseries-compression-replay.example`
头注所述该 lane supervisor 会跑的 `migration_apply`（`psql --file <migration>`）/ `pg_restore`。两种成因下运维后果相同。
因此本次对它的施加是一次 **no-op DDL + 账本补登**：`ADD COLUMN IF NOT EXISTS` 不改变任何东西，账本从此与磁盘一致。
连带结论：`#2222`（`/api/v1/runs` 投影 `h.*`）担心的响应形状变化**不会发生**，该键早已在线。

issue step 2 要求「差集若不是仅一个新文件则停下并按 #2048 对账」——已对账，结论是**继续施加**：

- 另有 **7 个账本行磁盘无文件**（`000007_flood.sql`、`000015`、`000017`、`000020`、`000031`、`000034`、`000036`），
  这正是 #2048 记录的 `b97c16e2 "Remove retired frequency display pipeline"` 追溯删除已应用迁移所致，**不是本次新增的漂移**。
- `packages/common/migrate.py:149` 的循环只遍历 `MIGRATIONS_DIR.glob("*.sql")` 并按「文件名不在账本」判 pending，
  **账本多出的行根本不会被访问**，因此这 7 行不改变本次施加行为，也不会让施加中止。
- 3 个待施加文件里没有一个属于那 7 个版本号，也没有版本号复用。

## Non-Goals

- 不改任何代码、不改 #2031 的 digest 语义。
- **不动 `60-reslice-pin-original-5a86841c.conf`（它同时钉住 display 与 autopipe 两个 unit）、
  不重启 `:8080` 生产 display、不 `git pull`**——
  活动树在 `hotfix/node27-rollback-pre-2073` 且无 upstream，把它恢复到 master 并解钉重启是 **#2162** 的窗口。
  在钉着旧树（无该列）的服务上重启，对本单的 oracle 零信息量。
- 不做缓存清理/淘汰（归 #2032），不碰 node-22，不碰 `yd-*` 实例。
- 不做 `000056` 的数据回填（`scripts/backfill_hydro_run_parsed_at.py` 是独立的人看着跑的步骤，见 #1789 迁移头注）。
- 不因 `000058` 改压缩 lag / 保留期 / per-tick bound。

## Risk triage

- Fixture level: **`none`**（零代码 diff；交付物是运维动作 + receipt + 本 fixture）。
  Upstream suggested level: **absent**——#2145 是 #2031 实现前勘察时手写立的 follow-up 单，无该字段（`预估规模 S：纯运维执行 + receipt，无代码改动`）。
  mandatory expanded 触发词（`migration` / `column` / `schema`）的判定对象是 **diff / change surface**，本单无 diff；
  这些词描述的生产动作风险由下列已选风险包承载（与 #2016 同构先例：`display-v2-national-timeline-precip-overlay/tasks.md` 的 `### #2016` 一节）。
- Repair intensity: **不适用**——零代码改动，无 implementer、无 fix pass。
- Risk packs 与 evidence mapping 见 `tasks.md`。
- `design.md` 按 `none` 级豁免（`issue-risk-contract.md` Fixture Level Rules）。

## Must preserve

- `public.schema_migrations` 里已有的 56 行一行不动。机制是 `packages/common/migrate.py:167` 的
  `if migration_has_been_applied(...): skipped += 1; continue` —— 已施加的文件在 apply 之前就被跳过，
  `record_migration`（`:132-137`）对它们根本不会被调用（其 `ON CONFLICT DO NOTHING` 分支在本次施加中永不触发）。
- `5a86841c` 的 reslice pin、`display.env`、`:8080` 生产进程、`yd-*` 实例（独立库 `:55434`）全程不动。
- `core.river_network_version` 的 **44** 行（2026-09-12 实测；#2031 receipt 记的 38 是历史读数，不沿用）内容不变，
  新列一律取 `DEFAULT 0`。
- `000058` 不改变任何既有 chunk 的范围、行与压缩状态。

## Seams under test

- `public.schema_migrations`：施加前后的行集合（判据是 000056/000057/000058 三行**存在**，
  总数按 `56 → 59` 的 **+3 增量**读，不写死绝对值——#2048 可能先行对账）。
- `information_schema.columns` 对 `core.river_network_version.geometry_generation` 的
  `data_type / is_nullable / column_default`。
- `timescaledb_information.dimensions` 对两张 hypertable 的 `time_interval`。
- 三条读路由 + `/api/v1/layers` 在 :8090 上施加前/后的 HTTP 状态码。

## Evidence mapping

见 `tasks.md` Evidence Floor。本单的 oracle 全部在 node-27 实机（真实活库），本地只闭 `openspec validate` 与 markdownlint。
