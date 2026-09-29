# Tasks

## Risk packs

- [x] Public API / CLI / script entry — selected：start-display-api、安装器、retention CLI、autopipe；→ 1.x、2.x、3.2、4.1。
- [x] File IO / path safety / overwrite — selected：共享底图目录删除、harness 与活动 checkout 隔离；→ 1.2、3.2、3.3。
- [x] Config / project setup — selected：保留天数/删除开关 env、unit `UMask=0002`；→ 3.2、3.4、5.3。
- [x] Concurrency / shared state / ordering — selected：与 yd 共享目录（刷新与清理竞态）、trap 子 shell/主 shell；→ 2.2、3.1、3.3。
- [x] Error handling / rollback / partial outputs — selected：utime 失败吞掉、decline 写入失败保持 failed、restore 只执行一次；→ 2.2、3.1、4.1。
- [x] Documentation / migration notes — selected：runbook 共享约定、decline 处置步骤、头注释；→ 1.1、3.4、4.3。
- [ ] Schema / columns / units / field names — not selected：无 schema 变化（复用 `ops.ingest_recompute_decline`）。
- [ ] Auth / permissions / secrets — not selected：只读角色已有 SELECT（node-27 实测）；不改授权。
- [ ] Resource limits / large input / discovery — not selected：清理本身就是资源治理，由 3.2 覆盖。
- [ ] Legacy compatibility / examples — not selected：由 Must preserve 与各自回归测试覆盖。
- [ ] Release / packaging / dependency compatibility — not selected：无依赖变化。

## 1. #2638

- [x] 1.1 `REPO_ROOT` 以脚本位置解析，头注释如实描述。
- [x] 1.2 harness 改为复制脚本进 temp_repo 执行，删除假 git 的 rev-parse 依赖；新增三种 cwd 用例（另一个 git 仓库、非仓库、本仓库），断言 repo_root / env / unit 来源 / pgrep 模式；新用例对 master 脚本变红。
- [x] 1.3 node-27 live receipt（`receipts/2026-09-29-node27/display-restart-2638.log`，2026-09-29T01:04Z，head `f2657ed4e`）：非本仓 git cwd 下执行重启；`:8081` 监听 PID 前后一致；`:8080` 重启且 `/health` 200；日志 `repo_root=/home/nwm/NWM`；unit 生效 `UMask=0002`（`/proc/<pid>/status`）。

## 2. #2640

- [x] 2.1 `:278`（issue 写作 `:216`）先捕获再比较；`enable_failure_restore` 与 `--install` 内联 trap 加 `$BASHPID` guard。
- [x] 2.2（安装器测试文件现 871 行，超过 1000 行按规则分区）fake systemctl 的非零 `is-active` 开关，以及对应用例（restore 恰好一次、受保护单元读取一轮、子 shell 失败不 restore）；新用例对 master 脚本变红。
- [x] 2.3 `bash -n` 通过；现有安装器测试全绿。
- [ ] 2.4 node-22 实机 `--enable` 演练：待 #1831 窗口补证（本 PR 不执行）。

## 3. #2627

- [x] 3.1 basemap 命中刷新 mtime（>1 d 才刷新、<1 d 不刷新、`PermissionError` 仍命中）。
- [x] 3.2 retention basemap 阶段：30 d 瓦片、1 d 两种临时文件、空目录、未知文件保留、symlink 跳过、非法天数 exit 2、删除开关默认 dry-run、summary 字段；basemap 阶段遇到 `PermissionError` 时，`basemap.failed[]` 非空、exit 1，`.pbf` lane 照常清理。
- [ ] 3.3 node-27：部署后 dry-run 计数 receipt（**已完成**：`receipts/2026-09-29-node27/mvt-cache-retention-dry-run-2627.json`，`mode=dry_run`，planned 0，此时缓存 12069 个文件中没有超过 30 天的）；**停下来问用户**是否启用删除；确认后启用并跑一次，删除计数与 dry-run 对照；basemap 端点前后 200。
- [x] 3.4 runbook 共享目录约定（env 名 `NODE27_MVT_CACHE_RETENTION_BASEMAP_{DAYS,DELETE}`、开关优先级）（含 yd `docs/agent-ops.md` 引用、umask/setgid/属组、删除开关与启用步骤）；`basemap.py` 注释；env example；unit `UMask=0002`。

## 4. #2590

- [x] 4.1 autopipe：published run 且错误码是确定性的（可解析、非 `OUTPUT_PARSE_*`）时，写 `PUBLISHED_REPARSE_FAILED` decline，detail 以 `<CODE>: ` 开头；瞬时码或解析不出码时不写，继续重试；decline 写入失败保持 failed；非 published 路径不变。
- [x] 4.2 lane：watched set 并入 decline 来源（DISTINCT ON，updated_at 取 declined_at，parsed_at 条件）；报告带错误码与来源；`threshold >= liveness` 配置错误 exit 2；兄弟残留 `legacy_store_refused`（LEFT JOIN forcing_version）只首次告警、不 re-alert，由测试固定。
- [x] 4.3 真实 PG（node-27 disposable DB）回归：register → parse → publish → 重写（mtime > parsed_at）→ 注入确定性 parse 失败。
  - 修复前 pin：`status` 仍为 published、`updated_at` 已续期、`parsed_at` 不变、run 不在 `default_observe` 中。
  - 修复后：写入 decline，run 进入 watched set，超过阈值后 exit 1、报告带错误码，下一 tick 不再重试。
  - 负例（legacy skipped、`parsed_at IS NULL` cohort、正常 recompute、#1781 decline）不进入集合。
  - published run 遇瞬时码（`OUTPUT_PARSE_DB_ERROR`）→ 不写 decline 行，下一 tick 仍重试。
  - 偏离（PR #2691 偏离记录 2）：注入发生在 `_run` 子进程边界，用确定性 stderr 模拟 parse 失败，没有构造真实 SHUD 产物；`_process_run`、decline 写入与读取、lane 查询都在真实 PG 上执行。
  - stderr 首行是 warning、次行才是 `MODEL_RIVER_FILE_MALFORMED: ...` → 仍识别为确定性，写 decline。
  - traceback 形态的 stderr 不写 decline，下一 tick 仍重试。两个用例：`Traceback ...\nOSError: [Errno 116] Stale file handle`，以及带 `DETAIL:` 行的 psycopg traceback。
  - legacy_store_refused run：首次越阈值 exit 1；fake clock 越过 realert_interval（>24h）仍在集合 → exit 0，不再告警。
  - status 不降级。
  - runbook 写明处置步骤。

- [x] 4.4 follow-up（#2690；DankerMu/yd-viewer#368）：`OUTPUT_PARSE_COMPRESSED_CHUNK_BLOCKED` 下 published 重解析的永久 rc=1 残留（本批不裁决）；yd-viewer `docs/agent-ops.md` 反向链接（yd 仓）。

## 5. 收尾

- [x] 5.1 本地：ruff、相关 pytest、`bash -n`、openspec validate。
- [x] 5.2 node-27 disposable-DB pytest（4.3 与 retention/basemap 相关测试）。
- [ ] 5.3 node-27 部署（ff pull）后的 live receipt（1.3、3.3）：1.3 已完成，3.3 的 dry-run 已完成；启用删除仍待用户确认，目前未启用。

## Evidence Floor

- [x] `uv run ruff check .`
- [x] `uv run pytest -q tests/test_two_node_docker_runtime.py -k start_display tests/test_start_display_api_restart_anchor.py tests/test_node22_refresh_timer_health_installer.py tests/test_node27_mvt_cache_retention.py tests/test_node27_parse_failure_residency_alert.py tests/test_node27_ingest_run.py` 以及 basemap 路由测试、autopipeline 测试
- [x] `bash -n scripts/install_node22_refresh_timer_health.sh scripts/ops/start-display-api.sh`
- [x] `openspec validate node27-ops-script-hardening --strict --no-interactive`
- [x] node-27 真实 DB pytest（`TMPDIR=/home/nwm/tmp`）
- [ ] node-27 live receipt：display API 重启（`:8081` 不受影响）已完成，basemap dry-run 已完成；删除需经用户确认，尚未启用
- [ ] node-22：待 #1831 窗口补证
