# 2026-11-15 追赶收尾：retention 真删切换与生产配置收据

追赶期（2026-11 上旬）为腾空间和加速补跑，两端改了几项生产配置。本文记录追平后的终态、
删除收据和回滚路径。仓库里的 `infra/env/*.example` 默认值**不变**，本文只记录生产实例与
模板的差异。

## 1. 生产配置终态（与 example 的差异）

| 端 | 文件 | 键 | example 默认 | 生产终态 | 切换时间 (UTC) | 备份 |
|---|---|---|---|---|---|---|
| node-22 | `infra/env/compute.scheduler-dbfree.env` | `NHMS_RETENTION_DRY_RUN` | `true` | `false` | 2026-11-13T16:34:41Z | `.bak-dryrun-true-20261113T1634Z` |
| node-22 | 同上 | `NHMS_RETENTION_EXTRA_ROOTS_ENABLED` | `false` | `true` | 2026-08-23 起已开 | — |
| node-22 | 同上 | `NHMS_RETENTION_EXTRA_ROOTS_DAYS` | `30` | `14` | 2026-11-14T06:46:56Z | `.bak-extra30-20261114T0646Z` |
| node-22 | 同上 | `NHMS_SCHEDULER_LOOKBACK_HOURS` | `32` | `32`（追赶期 336，已恢复） | 2026-11-15T11:20:21Z | `.bak-lookback336-20261115T1120Z` |
| node-27 | `/etc/nhms/node27-canonical-retention.env` | `NODE27_RAW_RETENTION_FORCING_DAYS` | `30` | `14` | 2026-11-14T14:22:55Z | `node27-canonical-retention.env.bak-forcing30-20261114` |

- node-22 的备份都放在 env 同目录下（`/scratch/frd_muziyao/NWM/infra/env/`）。
- node-22 三次切换都在 pass 边界做：先停 timer，等 service 变 inactive，再备份并 `sed` 改一行，
  校验 diff 恰为 2 行，最后由 trap 恢复 timer。日志在
  `/scratch/frd_muziyao/nhms-prod/workspace/repairs/flip-{retention,extra14,lookback32}.log`。
- `NHMS_RETENTION_DAYS=14`（主 object-store 的 raw/canonical/forcing/runs）与 example 一致，
  `NHMS_SCHEDULER_CYCLE_LAG_HOURS=16` 也与 example 一致。

## 2. node-22 删除收据（scheduler pass 内 retention 段）

证据目录：`/scratch/frd_muziyao/nhms-prod/workspace/scheduler/evidence/`。

| pass | extra_roots 天数 | deleted | failed | freed |
|---|---|---|---|---|
| `scheduler_2026111316_65e6021baf5c` | 30 | 17745 | 0 | 1.28 TB（1281286725468 B） |
| `scheduler_2026111400_4e2cc4c3c4dd` | 30 | 270 | 0 | 15.6 GB |
| `scheduler_2026111404_4311ae9f7656` | 30 | 138 | 0 | 9.7 GB |
| `scheduler_2026111406_7c30639f24ee` | 14 | 8044 | 0 | 461 GB（460905621884 B） |
| `scheduler_2026111416_6eb535c561c0` | 14 | 406 | 0 | 25.0 GB |
| `scheduler_2026111504_f0425612f723` | 14 | 406 | 0 | 25.0 GB |
| `scheduler_2026111511_321a4126a8cf` | 14 | 812 | 0 | 50.0 GB |
| `scheduler_2026111512_6e32e4d4f6c8` | 14 | 406 | 0 | 25.1 GB |

- 每次切换前都跑过带外 dry-run（`services.orchestrator.cli cleanup`，默认 dry-run）核对计划。
  切 14 天前那次计划为 7776 项 / 445.6 GB，计划内没有任何受保护条目；
  跳过原因为 `within_retention_window` / `pipeline_frontier_exempt` / `static_asset_protected`。
- 稳态：每推进一个 cycle，大约删 406 项 / 25 GB。
- 已知问题：上表中执行过工作的 pass，顶层 status 都是 `resource_limit_blocked`
  （`limit.reason=evidence_size_limit_exceeded`）。原因是 evidence 超过硬编码的
  `MAX_EVIDENCE_BYTES=5_000_000`（`services/orchestrator/scheduler_evidence.py`）后被摘要化，
  retention 段不受影响。这个问题单独跟踪，不在本收据范围内。

## 3. node-27 forcing lane 收据

日志：`/var/log/nhms-node27-canonical-retention/`。

| run | forcing_cutoff | deleted（其中 forcing/） | failed | freed |
|---|---|---|---|---|
| `raw-retention-20261114T142732Z.json`（切 14 天后的首次真删） | 2026-10-26T00:00:00Z | 84（72） | 0 | 170 GB（169835391381 B） |
| `raw-retention-20261115T033501Z.json`（每日定时） | 2026-10-29T00:00:00Z | 24（12） | 0 | 28.9 GB |

- 真删前用 plan-only 演练过一次：`raw-retention-20261114T142543Z.json`，rc=0。
- 手工运行时 `LOCK_PATH` 位于 `/run/nhms-node27-canonical-retention/`（属于 systemd
  RuntimeDirectory，service 不在运行时该目录不存在）。手工演练前需要
  `sudo install -d -o frd_muziyao -g root -m 755 /run/nhms-node27-canonical-retention`。

## 4. 回滚

- node-22：在 pass 边界（停 timer，等 service inactive）把对应 `.bak-*` 文件 `cp -p` 回
  `compute.scheduler-dbfree.env`，然后恢复 timer。恢复 `DRY_RUN=true` 后不再真删。
- node-27：`sudo cp -p /etc/nhms/node27-canonical-retention.env.bak-forcing30-20261114 /etc/nhms/node27-canonical-retention.env`。
  下一次定时运行即恢复 30 天窗口；只想暂停 forcing 删除时，调大 `NODE27_RAW_RETENTION_FORCING_DAYS`
  即可（上限 3650）。
- 已删除的数据无法回滚。被删的只有 `runs/`、raw/canonical/forcing 中间态和 forcing 树，
  published 产物与静态网格始终保留（见 `two-node-deployment-overview.md` 的预报数据保留清理一节）。
