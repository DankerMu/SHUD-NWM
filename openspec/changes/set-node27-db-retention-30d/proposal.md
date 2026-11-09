# Proposal: node-27 DB 时序保留窗口统一为 30 天（#3106）

## Why

owner 决定（2026-11-09）：node-27 的 PG 时序数据按 30 天保留，与 #3087 中 forcing/ 和 runs/ 的 30 天口径一致。

目前同一个窗口有四个不一致的值：
- 仓库默认值 `DEFAULT_RETENTION_WINDOW_DAYS` 是 14；
- env 模板写的是 14；
- node-27 的 live retention env 是 21；
- node-27 的 `node27-ingest.env` 没有设置这个变量，coverage refresh 因此走 fail-closed。

runbook §8.1 专门警告：照模板重新拷贝会把 live 窗口收窄，造成不可逆的多删。另外，retention timer 自 11-05 重建后一直是 disabled，PG 数据目前没有保留上限。

## What Changes

- `packages/common/storage.py`：`DEFAULT_RETENTION_WINDOW_DAYS` 从 14 改为 30。
- `infra/env/node27-timeseries-retention.example`：发布值从 14 改为 30，注释同步更新。
- 测试：钉住新默认值为 30，并钉住"模板里未注释的窗口值等于共享默认值"，防止两者再漂移。
- runbook `docs/runbooks/tier-node27-timeseries-storage.md`：开头的 policy banner 写明当前 live 值为 30（2026-11-09 起）；§8.1 step 2 和对应示例段落改成"模板与 live 均为 30"，不再保留"模板 14 / live 21"的说法。
- ADR 0002 头部补一条 policy amendment，记录 2026-11-09 DB 保留窗口改为 30 天。
- node-27 运维（不入仓）：两份 live env 设为 30，跑一次 dry-run receipt，然后启用 timer。

## Impact

- 运行时：live env 显式设置了窗口，所以默认值变化不改变 node-27 当前行为。窗口变宽只会减少删除，方向是保守的。
- 容量：30 天稳态下 PG 约为"未压缩窗口约 285G + 已压缩部分 30 天 × 约 9G/天"。
- display coverage refresh：`node27-ingest.env` 补上窗口变量后，会**开启** #2504 D6 的窗口外收敛。`river_valid_time_end` 早于 `watermark − 30d` 的 populated 行在刷新时可能被写成 0，这是预期收敛，见 `docs/runbooks/production-ops/service-bringup.md` 中"窗口外收敛"一节。11-05 重建后 DB 最老的数据是 10-12，按当前 watermark 算没有行受影响。
- 两份 env 必须相等。ingest 值如果小于 retention 值，会放开 retention 还没 drop 的那一段。本次两份都设为 30；原 retention 值是 21，ingest 原本未设，所以无论先改哪份都只会更保守。
- 不改 runner 的逻辑、锁、archive gate 或 receipt schema。
