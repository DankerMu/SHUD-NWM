# Tasks: set-node27-db-retention-30d (#3106)

Fixture level: minimal
Risk packs: ops-config, destructive-lane (drop_chunks 不可逆)
Must preserve: runner 解析规则（strict positive int、空值走默认）；`configured_retention_window_days` 不提供默认值（coverage refresh 保持 fail-closed）；archive gate 只接受 `disabled`；`--dry-run` 优先于 ENFORCE。

## 1. 仓库
- [x] 1.1 `DEFAULT_RETENTION_WINDOW_DAYS = 30`，并更新相关注释
- [x] 1.2 env 模板改为 `NODE27_TIMESERIES_RETENTION_WINDOW_DAYS=30`，所有写 14 的注释改为 30，包括 `:23` 的 "the 14-day window" 和 `:26` 的 "Default 14"；`scripts/node27_timeseries_retention.py:4` docstring 中的 "default 14 d" 同步改为 30
- [x] 1.3 测试：默认值钉为 30；新增一条测试，确认模板里未注释的窗口值等于 `DEFAULT_RETENTION_WINDOW_DAYS`
- [x] 1.4 `docs/runbooks/tier-node27-timeseries-storage.md` 中所有"模板 14 / live 21"的说法改为当前事实（两者都是 30，2026-11-09 起，#3106）：policy banner（约 :186-195）、§8.1 step 2 警告（约 :7495-7510）、示例段落（约 :7985-7995）、`:7434-7435`、`:7783`，以及 `:3647` 的 `W − 21 d`
- [x] 1.4b 同一 runbook 的 "Backlog ceiling ≤6" 条目（约 :3772-3780）：它的前提是"21 天窗口 + timer 启用"，原文要求窗口一旦改动就重新推导。按以下事实重新写：
  - 窗口现为 30 天，timer 在 2026-11-05 至 11-09 之间是 disabled，11-09 由 #3106 重新启用；
  - 现行 chunk geometry 是 1 天（同 runbook "Three-day chunk geometry" 节）；
  - 未压缩存量的上界由压缩 cutoff `W − 2d` 加预报时效决定，与 DB 保留窗口无关，因为窗口 30d 大于压缩 lag 2d，retention 只会 drop 已经满足压缩条件的 chunk；
  - 2026-11-09 实测：river 未压缩 chunk 共 10 个（10-18..10-27）。
  - 原来的 ≤6 结论标为历史推导（7-day chunk，21d）。
- [x] 1.4c `docs/runbooks/production-ops/service-bringup.md:96-97`（"实测没有这个变量……现网值 21"）与 `infra/env/node27-ingest.example:85`（"Unset (… today's node-27 state)"）改为当前事实：2026-11-09 起 node-27 的 `node27-ingest.env` 已设为 30，与 retention env 相等
- [x] 1.4d `openspec/changes/tier-node27-timeseries-storage/design.md:2014-2016` 中"default `14`"一行就地加注："superseded by #3106: default 30"。这是冻结的设计记录，只加注，不改原文
- [x] 1.5 ADR 0002 头部补 policy amendment 2026-11-09

## 2. node-27 运维
- [x] 2.1 live `node27-timeseries-retention.env` 的 WINDOW_DAYS 从 21 改为 30；`node27-ingest.env` 补 WINDOW_DAYS=30
- [x] 2.2 `scripts/node27_timeseries_retention_once.sh --dry-run`，receipt 中 window_days=30。live env 的 `ENFORCE=1` 未注释，所以必须先确认 dry-run 结果，再做 2.3
- [x] 2.3 enable --now retention timer，`list-timers` 能看到下次触发

## Evidence Floor
- `uv run pytest -q tests/test_node27_timeseries_retention.py tests/test_node27_refresh_coverage_cli.py tests/test_storage.py` 通过；另外跑 `scripts/select_ci_tests.py` 按 diff 选出的测试集
- `uv run ruff check .` 通过
- node-27 dry-run receipt 摘要（window_days、cutoff、candidate 数、archive_gate.mode）贴到 issue
- timer enabled/active 的证据贴到 issue
