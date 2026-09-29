# Proposal

## Why

批 OPS：node-27/node-22 运维脚本加固，四单一个 PR。

- **#2638**：`scripts/ops/start-display-api.sh:22-29` 用不带 `-C` 的 `git rev-parse --show-toplevel` 解析 `REPO_ROOT`，结果取决于调用者的 cwd。#2282 / PR #2637 之后，`REPO_ROOT` 同时是 uvicorn 扫杀的锚点，所以从另一个 git checkout 内调用时，会扫杀、source、装 unit 到那一套。同机的 yd-NWM `:8081` 正是 #2282 那次事故的受害者。
- **#2640**：`scripts/install_node22_refresh_timer_health.sh` 以 `set -E` 运行，ERR trap 会在 `:216` 的命令替换子 shell 里先触发一次，再在主 shell 里触发一次，restore 因此执行两遍。现有 fake systemctl 对 `is-active` 永远返回 0，这个问题测不出来。
- **#2627**：NWM 与 yd-viewer 共用的天地图底图缓存（`$NHMS_MVT_FILE_CACHE_DIR/basemap/tianditu`）只增不减、没有上限；共享目录约定只隐含在代码里；display API 的 umask 0002 没有写进 unit。
- **#2590**：一个已 `published` 的 run，同 run_id 重写产物后，如果重解析确定性失败：
  - `mark_run_failed` 对 `published` 是 no-op，`parsed_at` 停在旧值；
  - autopipe 于是每个 tick 都重试并 rc=1，永远如此；
  - 这个 run 的 status 永远不会变成 `failed`，所以不在 #2529 residency lane 的 watched set 里——永久 rc=1，却没人看见。

## What Changes

- **#2638**：
  - `REPO_ROOT` 固定为脚本自身位置的 `../..`，不再调用 git；
  - 头注释如实描述；
  - 测试 harness 改为把脚本复制进临时仓库再执行（不再依赖假 `git`）；
  - 新增"cwd 在另一个 git 仓库"用例。
- **#2640**：
  - `:216` 先捕获再比较；
  - 两个 trap handler 首句加 `[[ $BASHPID == "$$" ]] || exit 1`；
  - fake systemctl 增加开关，非 active 时以 3 退出；
  - 用例要求 restore 序列恰好出现一次。
- **#2627**：
  - `basemap.py` 命中时，若文件 mtime 早于 now − 1 d，执行 `os.utime`；失败吞掉，不影响响应。
  - `node27_mvt_cache_retention.py` 增加 basemap 子树清理：
    - 删除 mtime 超过保留天数（默认 30、可配置、非法值拒绝运行）的 `<layer>/<z>/<x>/<y>` 瓦片；
    - 删除超过 1 d 的两种临时文件（NWM 的 `.<y>.<pid>.<tid>.<hex>.tmp`、yd 的 `tmp-<32hex>`）；
    - 顺带清空目录；
    - 输出与现有 retention 同构的 summary。
    - **删除默认关闭**：未显式启用时只做 dry-run 计数。
  - `infra/systemd/nhms-display-api.service` 加 `UMask=0002`。
  - runbook 写明共享目录约定，并引用 yd `docs/agent-ops.md`。
- **#2590**：
  - autopipe 在 parse 以**确定性错误码**失败、且该 run 当前为 `published` 时（瞬时码与 traceback 继续重试），写一行 `ops.ingest_recompute_decline`，`reason_code = 'PUBLISHED_REPARSE_FAILED'`，detail 带解析错误码。这利用 #1781 已有的"按证据记录拒绝"表：
    - 同一证据（run_id, init_state_id, product_mtime）不再每 tick 重试，rc=1 循环终止；
    - 新证据会自动重开。
  - residency lane 的 watched set 增加谓词：`reason_code = 'PUBLISHED_REPARSE_FAILED'` 的 decline 行，`declined_at` 在 liveness 窗口内，且此后该 run 没有成功解析过（`parsed_at` 为空或早于 `declined_at`）。
  - `status` 不动，满足 #1789 不降级约束。
  - 不需要 migration；`nhms_display_ro` 已有该表的 SELECT 权限（node-27 实测）。

## Triage

```text
Issue type: bugfix + enhancement (ops)
Fixture level: expanded
Upstream suggested level: absent（四单均未给）；命中 CLI/script entry、file IO/delete、production config、shared state、状态机/告警语义
Blast radius: node-27 display API 重启（误杀 yd-NWM :8081 / 覆盖 user unit）、共享底图目录误删（yd 共用）、autopipe 重试语义与告警、node-22 探针安装器 restore
Selected risk packs: Public API / CLI / script entry; File IO / path safety / overwrite; Config / project setup; Concurrency / shared state / ordering; Error handling / rollback / partial outputs; Documentation / migration notes
Evidence floor: 见 tasks.md Evidence Floor
```
