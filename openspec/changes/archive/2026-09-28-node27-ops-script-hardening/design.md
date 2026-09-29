# Design

## D1 #2638 start-display-api REPO_ROOT

Change surface: `scripts/ops/start-display-api.sh`、`tests/test_two_node_docker_runtime.py`（start-display-api harness `:5142-5256` 附近）、`tests/test_start_display_api_restart_anchor.py`。
- `script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)`，`REPO_ROOT=$(cd "$script_dir/../.." && pwd)`，不再调用 git（KISS，脚本在仓库里的位置固定）。头注释改成"以脚本位置为准，与 cwd 无关"。
- harness 把脚本复制到 `temp_repo/scripts/ops/start-display-api.sh` 并从那里执行，REPO_ROOT 自然等于 temp_repo；删除假 `git` 的 `rev-parse` 分支。这样即使在 node-27 上跑 pytest，脚本也碰不到活动 checkout 的 `display.env` / `.venv`（安全要求）。
- 新用例：cwd 为另一个带 `.git` 的临时仓库，断言打印的 `repo_root=`、source 的 `display.env`、安装的 unit 来源、pgrep 模式都指向脚本所在的仓库；另覆盖 cwd 不在任何仓库、cwd 就是本仓库两种情况。
- 调用方 `scripts/diagnostic/display-cold-waterfall.sh:86,103` 无需改动，行为自动变对。

Live receipt（node-27，用户要求）：
- `git pull --ff-only` 之后，在一个 git 临时仓库的 cwd 下执行 `bash /home/nwm/NWM/scripts/ops/start-display-api.sh`。
- 记录前后两组 `:8080` 与 yd-NWM `:8081` 的监听 PID（`ss -ltnp`）、`systemctl --user` 状态和 `/health`。
- 通过条件：`:8081` PID 不变，`:8080` 重启且 `/health` 200，`repo_root=/home/nwm/NWM`。

## D2 #2640 探针安装器 ERR trap

Change surface: `scripts/install_node22_refresh_timer_health.sh`、`tests/node22_refresh_timer_health_helpers.py`、`tests/test_node22_refresh_timer_health_installer.py`（现 871 行；新增用例后若超过 1000 行，按 large-file 规则另起分区文件，并在 PR 说明）。锚点以当前 master 为准：issue 写的 `:216` 现在是 `:278`；`--install` 的 trap（`:252`）是内联字符串，不是具名 handler，guard 放在该字符串开头。
- `:278`（issue 中的 `:216`）改为 `active=$("$systemctl_bin" --user is-active "$timer" 2>/dev/null || true); [[ "$active" == active ]]`。
- `enable_failure_restore` 首句、`--install` 内联 trap 字符串开头，都加 `[[ $BASHPID == "$$" ]] || exit 1`（与 #2294 兄弟脚本 `scripts/install_node22_scheduler_file_provider_refresh.sh:267,280` 同形）。
- fake systemctl 新增开关（例如 `NHMS_FAKE_IS_ACTIVE_NONZERO=1`）：`is-active` 结果不是 `active` 时以 3 退出，默认行为不变。
- 用例：
  - 探针 timer 在 `enable --now` 之后为 inactive 且返回 rc 3 → `--enable` 以非零退出、不输出 status line，restore 序列恰好一次，受保护单元的读取只有一轮；
  - `$BASHPID` guard：在子 shell 里触发失败，日志中没有额外的 restore。
- node-22 实机演练受 #1831 维护窗口约束，PR 中标为"待窗口补证"。

## D3 #2627 共享底图缓存

Change surface: `apps/api/routes/basemap.py`、`scripts/node27_mvt_cache_retention.py`、`infra/env/node27-mvt-cache-retention.example`、`infra/systemd/nhms-display-api.service`、`docs/runbooks/display-readonly-live-mvt.md`（retention 章节新增"天地图底图缓存"小节）、`docs/runbooks/node-27-bringup-checklist.md`（改写"basemap 不清理"的旧说法）、相关测试。retention 的 systemd unit 不需要 `UMask`，因为它只做 unlink/rmdir，不创建文件。
- **命中刷新**：`_read_cached_tile` 命中后，若 `now - st_mtime > 86400`，执行 `os.utime(path)`（times=None）；任何 `OSError` 吞掉，仍返回命中。规则与 yd 一致。
- **清理**：沿用现有 MVT retention 的 lane 与 timer，增加 basemap 阶段。
  - 删除对象：`<cache>/basemap/tianditu/<layer>/<z>/<x>/<y>`，其中 `z`、`x`、`y` 为纯数字、`layer` 在已知集合内，mtime < now − `NODE27_MVT_CACHE_RETENTION_BASEMAP_DAYS`×86400（默认 30，必须是正整数；非法值走现有 `preflight_blocked` 路径：输出 JSON summary 并 rc 2，不能裸 exit）。
  - 临时文件：NWM 的 `.<y>.<pid>.<tid>.<hex>.tmp`、yd 的 `tmp-<32hex>`，mtime < now − 86400。
  - 空目录自底向上删除，不删 `tianditu` 根和 layer 目录。
  - 不认识的文件名一律保留。
  - 路径安全：只在解析后的缓存根之下操作，不跟随 symlink（`lstat`；遇到 symlink 跳过并计数）。
- **开关叠加（优先级从高到低）**：`NODE27_MVT_CACHE_RETENTION_ENABLED=false`（整个 runner disabled，basemap 为 `mode: disabled`）> `NODE27_MVT_CACHE_RETENTION_PLAN_ONLY=true`（basemap 只做计数）> basemap 删除开关。`basemap/tianditu` 不存在时，basemap 阶段记一条 skip，不阻塞 `.pbf` 清理；basemap 阶段的错误进 `basemap.failed[]`，但不影响 `.pbf` 阶段。只要顶层 `failed[]` 或 `basemap.failed[]` 任一非空，进程 rc 就为 1，保证共享目录的清理故障能让 unit 变红。
- **删除开关**：`NODE27_MVT_CACHE_RETENTION_BASEMAP_DELETE` 必须显式为 `1` 才会删除；否则只做 dry-run 计数。summary 输出 `mode=dry_run|delete` 与每类计数。现有 `.pbf` 规则不变。
- **生产启用流程**：
  1. 部署后先以 dry-run 跑一次并记录计数；
  2. **停下来问用户**；
  3. 用户确认后，在 node-local `infra/env/node27-mvt-cache-retention.env` 设 `NODE27_MVT_CACHE_RETENTION_BASEMAP_DELETE=1` 并跑一次；
  4. 删除计数应与 dry-run 一致（允许两次运行之间的自然漂移，需说明）；
  5. `/api/v1/basemap/tianditu/...` 前后均 200。
- **umask**：`infra/systemd/nhms-display-api.service` 加 `UMask=0002`。它要等下一次安装并重启 unit 才生效（#2638 的 live receipt 重启就会装上新 unit），届时用 `grep Umask /proc/<pid>/status` 验证。
- **约定**写进 runbook 与 `basemap.py` 注释：
  - yd 以 bind 方式挂载该子树读写；
  - yd uid 10001，附加组 nwm(1005)，umask 002；
  - 目录 setgid、文件 664、属组 nwm；
  - 修改路径、布局或权限前须同步 yd 的 `docs/agent-ops.md`。
- **相互引用只做了本仓一侧**：yd `docs/agent-ops.md` 目前没有任何 basemap 引用，反向链接在 yd 仓。这属于本批 non-goal，记入 PR 偏离记录，并在 yd-viewer 立 follow-up。

## D4 #2590 published 重解析失败进入 residency lane

检测决策：采用"已记录的事实"，不用推断谓词。复用 #1781 的 `ops.ingest_recompute_decline`，不做 migration。node-27 实测：`nhms_display_ro` 对该表、`met.forcing_version` 和 `hydro.hydro_run.forcing_version_id` 都有 SELECT。

**写入（`scripts/node27_autopipeline.py::_process_run` 的 parse 分支，现 `:2197` 附近）**
- parse `rc != 0` 时，只看 `_run`（`:1780`，`capture_output=True`）返回的**完整 stderr**，不看截断后的 tail，也不看 stdout。
  - stderr 中只要出现 `Traceback (most recent call last):`，就视为解析不出码。`parser.py:269/298` 的 OSError/Exception 在标记 run 之后会 re-raise，以未捕获 traceback 退出；traceback 的末行（`OSError: ...`、`psycopg2.errors.X: ...`）以及 libpq 的 `DETAIL:` 行都不能拿来当错误码。
  - 否则逐行扫描，取第一个匹配 `^[A-Z][A-Z0-9_]+: ` 的行作为错误码。CLI 的已处理分支（argparse 与 click 两条路径）输出 `f"{error.error_code}: {error.message}"`，click 路径用的是 `SystemExit`，不会打出 traceback；前面有 warning 行也不影响。
  - 没有匹配行，就当作解析不出码。
- 当且仅当以下两条同时成立时，才调用 `_decline_blocked_recompute(..., reason_code='PUBLISHED_REPARSE_FAILED', detail='<CODE>: <redact 后的 stderr 尾部>')`：
  1. 该 run 当前 `status = 'published'`（在同一连接里只读查询）；
  2. 错误码是**确定性**的，即能解析出来，且不以 `OUTPUT_PARSE_` 开头。bare `OutputParsingError` 码，如 `MODEL_RIVER_FILE_MALFORMED`、`RIVQDOWN_*`，正是 lane docstring 列出的"永不自愈"形态。
- `OUTPUT_PARSE_*` 码一律视为可能瞬时，保持 `outcome="failed"`、下一 tick 重试。这与 `:2141-2146` 中 #1781 的"其余失败必须继续失败"原则一致。这类码包括：
  - `OUTPUT_PARSE_DB_ERROR`，即 2026-09-19 自愈 burst 的错误码；
  - `OUTPUT_PARSE_COMPRESSED_CHUNK_GUARD_FAILED`、`OUTPUT_PARSE_COMPRESSED_CHUNK_BLOCKED`；
  - `OUTPUT_PARSE_IDENTITY_KEY_MISSING`，保守起见同样当作可能瞬时。

  `OUTPUT_PARSE_OS_ERROR` 与 `OUTPUT_PARSE_RUNTIME_ERROR` 只写进 DB，不会出现在 stderr，它们以 traceback 形式到达，按上一条规则归为解析不出码。解析不出码的情况同样保持 failed 并重试。
- `OUTPUT_PARSE_COMPRESSED_CHUNK_BLOCKED` 本批不裁决新路径，保持现状：继续重试。published run 如果永久卡在它上面，仍然是 rc=1，但 status 不会变成 failed。这一残留写入 PR 偏离记录，并立 follow-up。
- detail 固定以 `<ERROR_CODE>: ` 开头；先 redact 尾部再拼接，保证 redact 不会吃掉前缀。
- 写入失败时保持 `outcome="failed"`，沿用 `_decline_blocked_recompute` 的现有契约。
- 非 published run 与非确定性失败的行为都不变。
- 下一 tick：现有 decline 读取（`_declined_runs`，按 `(run_id, init_state_id, product_mtime)` 匹配，不看 reason）会把该 run 排除出重试。新证据会自动重开。
- `status` 与 `parsed_at` 都不变，满足 #1789 的不降级约束。

**lane（`scripts/node27_parse_failure_residency_alert.py`）**
- observe 查询在现有 `status='failed' AND updated_at > floor` 之上 UNION 一个 decline 来源：

  ```sql
  SELECT DISTINCT ON (d.run_id) d.run_id, d.declined_at AS updated_at, split_part(d.detail, ':', 1) AS error_code, ...
  FROM ops.ingest_recompute_decline d JOIN hydro.hydro_run h USING (run_id)
  WHERE d.reason_code = 'PUBLISHED_REPARSE_FAILED'
    AND d.declined_at > floor
    AND (h.parsed_at IS NULL OR h.parsed_at < d.declined_at)
  ORDER BY d.run_id, d.declined_at DESC
  ```

  `FailingRun.updated_at` 取 `declined_at`，因此 `evaluate` 按 `updated_at > floor` 的二次过滤同样成立。同一 run_id 同时出现在两个来源时只保留一行，以 failed 来源为准。
- `FailingRun` 新增 `classification: Literal['failed','published_reparse','legacy_store_refused']`，每次 observe 都重新计算，不写入 state。报告 JSON 的每个 run 都带上 `classification` 和错误码。
- 因为不再重试，`declined_at` 不会续期。要让告警恰好发一次，前提是 `threshold + lane cadence（30 min）< retry_liveness`。lane 增加配置校验：`threshold >= liveness` 视为配置错误（exit 2）。cadence 耦合写进 runbook。默认值是 threshold 2 h、liveness 6 h。
- 运维处置写进 runbook：修复产物（新证据会自动重开），或删除该 decline 行以强制重试。

**兄弟残留（legacy-store-refused 的 failed run）**
- #1991（`:2162-2173` 注释，fixture `I12-1991.md` R2.2）刻意决定：forcing 的 legacy 拒绝**不写** decline 行，本批不推翻。
- lane 对 `status='failed'` 来源做 `LEFT JOIN met.forcing_version f ON f.forcing_version_id = h.forcing_version_id`。`f.timeseries_store = 'legacy'` 的 run 单独归类为 `legacy_store_refused`：
  - 照常参与常驻判定，达到阈值时首次告警（exit 1，报告里单列）；
  - 之后**不做 re-alert**。机制：`evaluate`（`:377-409`）在判断 re-alert 时，若 `run.classification == 'legacy_store_refused'` 且 state 里该 run 已有 `alerted_at`，则永不再触发，不管过了多久 `realert_interval`。run 离开观测集合时 state 条目照旧删除（`:395-399`），重新进入就重新计时。
- 这个分类不需要确认"拒绝确实发生过"：拒绝仅在 handoff envelope 存在时才会发生，缺 handoff 时 run 会真实 parse 失败。无论哪种情况，首次告警都照常发出，只是省掉 24h 重复。误分类最多少了重复告警，不会藏掉任何失败，满足"不产生每 24h 重复且无法消除的告警"。
- 残留：lane 用的 `hydro_run.forcing_version_id` 来自 run manifest，拒绝按 handoff envelope 中的 forcing_version_id 判定，两者理论上可能不同。影响同样只是重复告警的多与少，写入 design、不修。
- node-27 实测（2026-09-28）：当前 `failed` 且 forcing 为 legacy 的 run 为 0 行。

**负例**（都不进 decline 来源）：
- legacy-store-refused 的 `skipped` published recompute（不会 parse）；
- `parsed_at IS NULL` 的 legacy cohort（parse 成功时 `parsed_at` 会写入；失败但错误码非确定性时不写 decline）；
- parse 成功的正常 recompute；
- #1781 的压缩块 decline 行（reason 不同）。

Must preserve:
- `start-display-api.sh`：规范调用 `cd /home/nwm/NWM && bash scripts/ops/start-display-api.sh` 的行为不变；#2282 的扫杀锚点语义不变。
- 探针安装器：成功路径与 `--install` 行为不变；restore 语义不变。
- basemap：未命中与写入路径不变；`.pbf` retention 规则与 summary 字段不变（只新增 basemap 字段）。
- autopipe：非 published run 的失败路径不变；#1781 decline 的读写契约不变；lane 的 exit 码与状态文件格式不变（只新增类别）。

Governing invariants:
- 重启脚本只作用于脚本自身所在的 checkout。
- 底图清理只删除规则明确命中的文件，且只在显式启用时才删除。
- 每一次 published 重解析确定性失败，都会形成一条运维能看到的持久事实，而且不会形成无法清除的每日告警。

Sibling surfaces:
- `scripts/diagnostic/display-cold-waterfall.sh`（调用 start 脚本）
- `scripts/install_node22_scheduler_file_provider_refresh.sh`（#2294 已修，同形参照）
- yd-viewer basemap 反代（外部仓库，只读参照）
- `_record_decline` 的其它调用方（#1781 压缩块拒绝）
- frontier-stall / coverage-freshness lane（同一只读 DSN）

Non-goals：扫杀模式本身；unit 文件写死 `/home/nwm/NWM`；yd-viewer 仓库改动；缩放级别限制；放宽 `FAILABLE_RUN_STATUSES`；给 autopipe unit 加 `OnFailure=`；node-22 实机执行（#1831 窗口）。

Review focus：
1. harness 不再可能碰到活动 checkout；
2. 清理的路径安全和删除开关；
3. #2590 decline 语义没有吞掉瞬时失败以外的东西，告警只发一次、可清除；
4. `$BASHPID` guard 与先捕获再比较的写法；
5. live receipt 证明 `:8081` 没有被波及。
