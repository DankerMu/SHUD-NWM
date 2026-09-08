# Tasks: display-cache-warm-header-trust（issue #2079）

Fixture level: expanded（repair intensity: high — auth/token/secret + public API + production config）
Upstream suggested level: none carried（issue 由 issue-scribe 立单，非 pipeline）；触发词 `token`/`secret`/`public API` 强制 expanded。
Project profile: NHMS（`openspec/project-profile.md`，无需更新）

## 0. 实测与裁定（orchestrator，已完成）

- [x] 0.1 node-27 本机 :8080 实测 receipt `docs/runbooks/receipts/2026-09-08-issue-2079-cache-warm-measurement-node27.md`：带头/不带头 TTFB 各 6 样本 × 2 端点、20 并发带头 vs 对照的 PG 并发活动查询峰值。
- [x] 0.2 裁定：采用「进程内 scope 标记 + 配置 token」双身份（design D1），**偏离** issue 推荐的纯 scope 标记，因 #2013 起 prewarm 是真实外部调用方且其 fixture 要求 publish 后旁路 stale 窗口。
- [x] 0.3 偏离记录：issue AC5 写的是生产 `127.0.0.1:8080` live receipt；本 change 改为隔离实例 `:8090`（design Context：活动树停在回滚分支，生产不 pull 不重启，部署随 #2162 窗口）。

## 1. 实现（implementer）

- [x] 1.1 `apps/api/runtime_mode.py`：`RuntimeConfig` 加 `display_cache_warm_token: str | None = field(default=None, repr=False)`；`load_runtime_config` 从 `NHMS_DISPLAY_CACHE_WARM_TOKEN` 读取（`strip()`，空 → `None`）；`public_dict()` 不含该字段。
- [x] 1.2 `apps/api/display_cache.py`：新增 `_WARM_SCOPE_KEY = "nhms_display_cache_warm"` 与 `_mark_warm_scope(app)`（design D2）；`_force_refresh` 改为 design D1 的两分支判定（bytes 比较 `hmac.compare_digest(header.encode("utf-8", "surrogateescape"), token.encode("utf-8", "surrogateescape"))`；`runtime_config` 走 `_display_readonly` 同款三层 `getattr`；缺 `app`/`state`/`runtime_config`/`scope`/`headers`、头值非 `str`、空 token、缺头一律 `False`，绝不抛）；`_replay_targets` 改用 `httpx.ASGITransport(app=_mark_warm_scope(app))` 且不再发头；模块 docstring `:11-12` 与 `DISPLAY_CACHE_FORCE_REFRESH_HEADER` 旁注释改为 token 语义（常量名保留，供 prewarm/测试引用）。
- [x] 1.3 `scripts/node27_mvt_prewarm.py`：`fetch_json` 按 design D4（env token → 头；无 token → 不发头 + 一次性 stderr warning）；`:66-76` 注释块改写；`CACHE_WARM_HEADER` 常量保留。
- [x] 1.4 env 模板：`infra/env/display.example` 加注释 + **注释掉的** `# NHMS_DISPLAY_CACHE_WARM_TOKEN=`（沿用 `display.example` 里 `# NHMS_PRECIP_MIRROR_ROOT=` 的可选键先例，附 `openssl rand -hex 32` 生成提示与「token 必须是 ASCII」一句）；`infra/env/node27-ingest.example` 在 `AUTOPIPE_MVT_PREWARM_*` 旁加同样注释掉的同名键与「必须与 display.env 同值；缺失则 prewarm 不发头并 warning；不要留占位值」注释。**（round-1 cand-01 修正：原文规定的生效占位值 `change-me-cache-warm-token` 会让按 `infra/README.two-node-docker.md:262` 逐字安装模板的部署把仓库公开值当 token，静默复原 #2079；unset 才是 design 声称的安全默认。）**

## 2. 测试（implementer，与实现同 PR；**能红的三条**——改名后的 `test_external_refresh_header_no_longer_bypasses_cache`、token 匹配、`_mark_warm_scope` 标记——改前跑红一次并贴红/绿输出；其余新用例在 master 上本就绿，不要求红）

- [x] 2.1 `tests/test_display_catalog_cache.py`：改写 `test_force_refresh_header_bypasses_cache` → `test_external_refresh_header_no_longer_bypasses_cache`（外部头 `refresh`、token 未配置 → 命中、loader 未调用；改前红）；新增：token 匹配 → 重算写回；token 不匹配 → 命中；token 已配置 + 头值 `"ab\u00e9"`（非 ASCII str）→ 命中且不抛（红锚不是 master，而是「按 str 调 `compare_digest`」的 mutant：实现后把 `.encode` 临时去掉跑一次应 `TypeError`，报告贴出）；token 未配置 + 任意头值 → 命中；无 `headers`/`scope` 属性的 request → `False`；`_mark_warm_scope` 标记 → 重算写回（token 未配置）；`_replay_targets` 端到端：真实 `FastAPI` 应用 + 路由调用 `display_catalog_cached`，先普通请求填缓存，再 `await _replay_targets(app, [path])`，断言 loader 第二次被调用且 store 更新（这一条改前用头也绿，改后用标记也绿——它钉的是「预热仍生效」）。`_request()` helper 需要能带 `scope`/`headers`。
- [x] 2.2 `tests/test_runtime_mode.py`：token 解析三态（值/空白/缺失）、`repr(config)` 与 `public_dict()` 不含值。
- [x] 2.3 `tests/test_node27_mvt_prewarm.py:940-955`：有 env → 头值 == token；无 env（`monkeypatch.delenv`）→ `get_header("X-nhms-cache-warm") is None` 且 `capsys` 捕获一条 warning，第二次调用不再重复。
- [x] 2.4 `scripts/select_ci_tests.py`：新增 `PathTestRule("apps/api/display_cache.py", ("tests/test_display_catalog_cache.py",))`（现状：`tests/test_display_cache.py` 不存在，同名派生够不到；`apps/api/**` 规则只买到 `test_api*.py`/`test_monitoring_api.py`）。`runtime_mode.py` 与 `node27_mvt_prewarm.py` 已由同名派生覆盖，不加规则。规则放 `PATH_TEST_RULES` 里 `apps/api/**` 条目旁（`scripts/select_ci_tests.py:2165-2172`，非 `stop_on_match`，顺序无关）。验证（`--changed-file` 接的是「每行一个改动路径」的文件，不是源码路径）：`printf 'apps/api/display_cache.py\n' | uv run python scripts/select_ci_tests.py` 的输出含 `tests/test_display_catalog_cache.py`。`tests/test_select_ci_tests.py` 无需同步：`apps/api` 不在 `DIRECTORY_RULE_AUDIT_PATHS`（`:7551-7564`），pattern 唯一，目标文件存在。

## 3. 文档（implementer）

- [x] 3.1 `docs/runbooks/display-readonly-live-mvt.md`：预热/目录缓存段补「强制刷新身份」小节——两种身份、token 两处 env 同值、缺失时的退化、receipt 链接。

## 4. 验证（orchestrator）

- [x] 4.1 本地：`uv run ruff check .`；`uv run pytest tests/test_display_catalog_cache.py tests/test_runtime_mode.py tests/test_node27_mvt_prewarm.py tests/test_api_contract.py tests/test_hydro_display_mvt_scaling.py tests/test_select_ci_tests.py tests/test_two_node_docker_runtime.py tests/test_node27_write_roles.py -q`（后两个是 CI selector 对 `infra/env/**` / `infra/env/node27-*.example` 改动会跑的 suite，且实读模板）；`openspec validate display-cache-warm-header-trust --strict --no-interactive`。
- [x] 4.2 node-27（merge 前，一次性 worktree，不动活动树与生产 :8080；先 `mkdir -p /home/nwm/tmp && export TMPDIR=/home/nwm/tmp`，#1765）：worktree 自建 venv 跑 4.1 的 pytest；隔离 uvicorn :8090（`--workers 2`，env = `display.env` + `NHMS_DISPLAY_CACHE_WARM_TOKEN=<随机值，不入 receipt>` + `DATABASE_URL` 追加 `application_name=nhms-2079-receipt`）：(a) 预热 3 次后，外部 `x-nhms-cache-warm: refresh` 与错 token 各 6 样本 TTFB 与不带头同量级（≤ 10 ms；对照实测 receipt 冷值 73–92 ms；不用 PG 活动查询做 (a) 的判据——进程内预热 tick 会落进采样窗口）；(b) 正确 token 6 样本回到冷路径量级（≥ 50 ms）；(c) 单次访问后静置 100 s，`pg_stat_activity`/`pg_stat_statements` 不可用时用 `pg_stat_activity` 轮询 `application_name='nhms-2079-receipt'` 的 `query_start`，观察到 ≥ 2 个相隔约 45 s 的查询簇（进程内预热仍在跑）；(d) `/api/v1/runtime/config` 响应体不含 token。receipt `docs/runbooks/receipts/2026-09-08-issue-2079-cache-warm-trust-node27.md`。
- [ ] 4.3 部署（merge 后，**deferred 到 #2162 维护窗口**，与 #2145 同次重启）：token 写入 `display.env` 与 `node27-ingest.env`（0600，同值）→ `bash scripts/ops/start-display-api.sh` → 下一 autopipe tick 的 prewarm 日志无 token warning → 生产 :8080 外部 `refresh` 不再冷。本 PR 只在 #2162 上留评论列出这三步，不执行。

## Evidence Floor

本地 4.1 全绿 + `openspec validate` 严格通过；node-27 4.2 receipt（外部 `refresh` 命中、token 冷、进程内预热存活、runtime/config 不泄露）；4.3 记录为 deferred 并在 #2162 留评论。

## Risk packs considered

- Public API / CLI / script entry: **selected** — 四条目录 GET 的旁路条件收紧；prewarm CLI 行为改为 token；响应体不变（`test_api_contract.py`）。
- Config / project setup: **selected** — 新 env 键、两处同值、缺失时的安全默认与退化路径。
- File IO / path safety / overwrite: not selected — 无文件写入。
- Schema / columns / units / field names: not selected — 响应体、prewarm summary schema 不变。
- Auth / permissions / secrets: **selected** — 本 change 的主题；`compare_digest`、不进 `public_dict`/`repr`、receipt 不含值。
- Concurrency / shared state / ordering: **selected** — 预热线程/`_lock` 路径不变但调用面改了；`--workers 2` 语义记录。
- Resource limits / large input / discovery: not selected — #2078 的地盘。
- Legacy compatibility / examples: **selected** — 字面 `refresh` 退役；env example 同步；活动 change `display-v2-…/tasks.md:509`、`tasks.md:32` 与同 change `invariant-matrix-i5-2009.md:440,470`（把 `-H 'x-nhms-cache-warm: refresh'` 写成 receipt 方法；新代码运行起即只量 warm 路径，与 token 无关）措辞记入偏离记录。
- Error handling / rollback / partial outputs: **selected** — token 缺失 → 退化非失败；warning 一次。
- Release / packaging / dependency compatibility: not selected — 仅 stdlib `hmac`。
- Documentation / migration notes: **selected** — runbook + env example + #2162 评论。
- Domain packs（geospatial/time-series/numerical/PostGIS/Slurm/provider/manifest/artifact identity）: not selected — 不触及。
