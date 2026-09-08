# Tasks: display-cache-flush-resistance（issue #2078）

Fixture level: expanded（repair intensity: high — public API 契约 + 共享缓存 helper + 资源上限 + 并发共享状态）
Upstream suggested level: none carried（issue 由 issue-scribe 立单，非 pipeline）；触发词 `public API`/`resource limits`/`concurrency` 强制 expanded，矩阵为硬门。
Project profile: NHMS（`openspec/project-profile.md`，无需更新）

## 0. 实测与裁定（orchestrator，已完成）

- [x] 0.1 receipt `docs/runbooks/receipts/2026-09-08-issue-2078-cache-flush-measurement-node27.md`：生产 :8080 冷 miss p50/p95（layers 85.2/97.6 ms、runs 83.2/87.6 ms，各 10 样本）；隔离实例 256/255-key 冲刷复现（`/runs` 2 ms → 71–80 ms）；253-key 变体（不触发 clear）+ 进程内直接回放 254 条热 path 的 tick 代价。
- [x] 0.2 裁定：LRU（D1）+ 准入谓词（D2）+ 登记后置与命中计数（D3）+ 回放上界 32（D4）+ layers 后切片（D5）。
- [x] 0.3 偏离记录：issue AC2 的「422 或钳制 + `maximum`」→ 选「越界为空页 200、写进 `offset` description、不加 `maximum`」（D5）；issue AC6 的生产 live receipt → 隔离实例 `:8090`（活动树停在回滚分支，部署随 #2162）；issue AC1 的「垃圾 key 不进 `_hot_paths`」只对谓词为假的 key 成立——**非空**的垃圾页（如 `/api/v1/runs?offset=k&limit=1`）仍以 `hits=1` 登记、受 LRU 256 与回放上界 32 约束（D1/D4，部分偏离，如实记录）。

## 1. 实现（implementer）

- [x] 1.1 `apps/api/display_cache.py`：`_store`/`_hot_paths` 改 `OrderedDict` LRU（D1；`_MAX_ENTRIES` 不变，永不整表 clear）；`display_catalog_cached` 加 `*, cacheable=None`（D2；两条分支都过谓词；谓词为假 → 不存、不登记、`_forget(key)`）；热 path 登记后置 + `(path, last_access, hits)`（D3）；`DISPLAY_CATALOG_WARM_REPLAY_MAX = 32` 与 `_warm_loop` 排序取前 K（D4；锁内快照、锁外回放）；模块 docstring 的机制段补「准入/淘汰/回放上界」三句；`clear_display_catalog_cache`、`stop_display_catalog_warmer`、`_force_refresh`、`_mark_warm_scope`、`_replay_targets` 不改。
- [x] 1.2 `apps/api/routes/hydro_display.py`：`/api/v1/layers` key `layers:{run_id!r}`（round-1 A1）、loader 返回完整目录、缓存后切片、`offset` 的 `Query` 加 description（D5，文案见 design）；`valid-times` 传 `cacheable=lambda v: bool(v["valid_times"])`；`discharge/cycles` 不动。
- [x] 1.3 `apps/api/routes/forecast.py` `/api/v1/runs`：传 `cacheable=lambda page: bool(page["items"])`；key 的 `str | None` 维度改 `!r`（round-1 A1 同族，valid-times key 同步）；`_paginated_payload` 不动。
- [x] 1.4 `openapi/nhms.v1.yaml:1957-1964`：`/api/v1/layers` 的 `offset` 参数加 `description`（与 1.2 同文），**两处同文**——`parameters[].description`（参数级）与 `parameters[].schema.description`（schema 级）：本仓 FastAPI/Pydantic 对 `Query(description=)` 两处都发出，先例 `openapi/nhms.v1.yaml:248,251`（`search`）与 `:261,263`（`stream_order_min`）。`/api/v1/runs` 不动。随后 `cd apps/frontend && pnpm generate:api` 再生成 `apps/frontend/src/api/types.ts`（参数 description 会变成 JSDoc，`:3368` 附近）。

## 2. 测试（implementer，与实现同 PR；**能红的用例**——300 个不可缓存 key 后合法 key 仍在、255+hit+255 后合法 key 仍在、垃圾 key 不进 `_hot_paths`、回放上界 32——改前跑红一次并贴红/绿输出：用文件互换而不是 stash——把改后的 `display_cache.py` 复制到 scratch，`git show HEAD:apps/api/display_cache.py > apps/api/display_cache.py` 跑，再复制回来）

- [x] 2.1 `tests/test_display_catalog_cache.py`：`_seed_hot_path` 改三元组；新增 design Invariant Matrix 前 8 行对应用例（LRU 存活/淘汰/从不为空、不可缓存不入两表且 loader 每次调用、命中计数、force-refresh 空结果 `_forget`、force-refresh 可缓存结果不动 `_hot_paths` 的 `(last_access, hits)`、`_warm_loop` 前 32 与 monkeypatch 为 2）；非 display 角色谓词不被调用；既有用例全绿。
- [x] 2.2 `tests/test_hydro_display_mvt_scaling.py`：既有 `display_catalog_cached` 替身（`:1626,1726,2496,2611,2675,2700,2728,2929`、`_record` `:2746-2751,2783-2788`）加 `**_`，断言不改；新增 display 角色路由用例（参照 `tests/test_precip_overlay.py:1889-1915` 的 `create_app({"NHMS_REQUIRE_SERVICE_ROLE": "true", "NHMS_SERVICE_ROLE": "display_readonly", ...})` 建法，或对 `app.state.runtime_config` 做 `dataclasses.replace(display_readonly=True)`——选能让 display 边界检查通过的最简者）：layers 后切片三断言（`offset=1&limit=1`、越界空页 200、三次分页 loader 一次且 key 为 `layers:None`）；valid-times 交集外 cycle 空列表不入缓存、有覆盖 cycle 入缓存。
- [x] 2.3 `tests/test_precip_overlay.py:1635` 替身加 `**_`。
- [x] 2.4 `/api/v1/runs` 空页不缓存 / 非空缓存：放 `tests/test_display_catalog_cache.py`（用 `FastAPI` + `forecast.router` + `get_forecast_store` override 的最小应用，`app.state.runtime_config` 走 `SimpleNamespace(display_readonly=True, display_cache_warm_token=None)`，与 `:280-301` 同法）。
- [x] 2.5 CI 选测：`printf 'apps/api/display_cache.py\napps/api/routes/hydro_display.py\napps/api/routes/forecast.py\nopenapi/nhms.v1.yaml\n' | uv run python scripts/select_ci_tests.py` 已含 `tests/test_display_catalog_cache.py`、`tests/test_hydro_display_mvt_scaling.py`、`tests/test_api_contract.py`、`tests/test_openapi_drift.py`、`tests/test_openapi_31_contract.py`、`tests/test_precip_overlay.py`（orchestrator 已验证，`scripts/select_ci_tests.py:2178` 规则存在）；但**不含** `tests/test_forecast_api.py`（`/api/v1/runs` 响应体的钉子，`:253-260` 的 `list_runs` 替身）。新增 `PathTestRule("apps/api/routes/forecast.py", ("tests/test_forecast_api.py", *CONNECTION_ATTRIBUTION_TESTS))` 放在 `:2182` 规则旁，并把该路径从 `CONNECTION_ATTRIBUTION_ROUTE_PATHS` 移出（实现时发现重复 pattern 被 `test_path_rule_duplicate_patterns_are_allowlisted_decisions` 守卫禁止，按 `forecast_store.py` 先例合并；PR 偏离记录 1）；`printf 'apps/api/routes/forecast.py\n' | uv run python scripts/select_ci_tests.py` 输出含 `tests/test_forecast_api.py` 且两条 attribution suite 仍在；`tests/test_select_ci_tests.py` 绿。

## 3. 文档（implementer）

- [x] 3.1 `docs/runbooks/display-readonly-live-mvt.md`：目录缓存段补「准入与淘汰」小段（空结果不缓存、LRU 256、回放每 tick ≤ 32 条按命中排序、layers 越界 offset 为空页），链接实测 receipt；每行 ≤ 300 字符。

## 4. 验证（orchestrator）

- [ ] 4.1 本地：`uv run ruff check .`；`uv run pytest tests/test_display_catalog_cache.py tests/test_hydro_display_mvt_scaling.py tests/test_precip_overlay.py tests/test_api_contract.py tests/test_forecast_api.py tests/test_openapi_drift.py tests/test_openapi_31_contract.py tests/test_openapi_response_conformance.py tests/test_select_ci_tests.py tests/test_node27_mvt_prewarm.py -q`；`cd apps/frontend && pnpm check:api-types`；`npx --yes markdownlint-cli2 docs/runbooks/display-readonly-live-mvt.md docs/runbooks/receipts/2026-09-08-issue-2078-*.md`；`openspec validate display-cache-flush-resistance --strict --no-interactive`。
- [ ] 4.2 node-27（merge 前，一次性 worktree，不动活动树与生产 :8080；`export TMPDIR=/home/nwm/tmp`）：worktree 自建 venv 跑 4.1 的 pytest；隔离 uvicorn `:8090`（`--workers 1`，`application_name=nhms-2078-receipt`）修复前（master）/修复后（PR head）各跑同一脚本：(a) 255 个 `basin_id=junk-<i>`（谓词为假）注入后 `/api/v1/runs` 3 样本仍 ≤ 10 ms（改前 71–80 ms 冷）；(a2) 300 个互不相同且**非空**的 `/api/v1/runs?offset=<k>&limit=<l>` 组合（`offset < total_count`，`limit` 1..200；谓词为真、可缓存）分两批注入、两批之间命中一次合法 key → 合法 key 之后仍 ≤ 10 ms（LRU 命中刷新位置）；一批 300 个中途不碰合法 key → 合法 key 一次冷 miss 后恢复，且期间任何时刻 `/api/v1/layers/discharge/cycles?source=gfs` 这类早先缓存的 key 最多也只冷一次（never-empty 的实机形态）；(b) 回放上界：用实测 receipt §6 的 `probe_app.py` 钩子法（包装模块把 `DISPLAY_CATALOG_WARM_INTERVAL_SECONDS` 压到 5 s、在 `_replay_targets`/`_store_value` 上挂文件日志，`uvicorn probe_app:app`），顺序：先命中合法 key ≥ 2 次（`hits ≥ 2`）→ (a) 的 255 条谓词为假 key → (a2) 的 300 条**可缓存**非空分页（`total_count` 来自一次前置 `GET /api/v1/runs?limit=1`，`offset < total_count`）→ 观察 ≥ 3 个 tick：修复前 targets 是整个存活热集合（before 实测每 tick 37–124 条、其中最多 110 条垃圾，被期间的 `clear()` 截断；独立进程实测 254 条 ≈ 9.2 s）；修复后每 tick `n ≤ 32`、第一条是合法 key、`junk=0`（谓词为假的 key 一条都不在 targets 里）——上界由可缓存 key 真正压到，不是 n=1 的空洞成立（不用 PG 采样：receipt §4 证明它在 uvicorn 进程里看不到回放）；(c) `/api/v1/layers?offset=999999999` 200 且 `data == []`（注意生产 DB 缺 000057 迁移时 `/api/v1/layers` 在 master 代码上 500——#2145；若 (c) 因此 500，记录并以本地路由用例为准）；(d) 20 并发 junk 突发全部 200、无 500。receipt `docs/runbooks/receipts/2026-09-08-issue-2078-cache-flush-resistance-node27.md`。
- [ ] 4.3 部署（merge 后，**deferred 到 #2162 维护窗口**，与 #2079/#2145 同次重启）：本 PR 只在 #2162 上留评论，不执行。

## Evidence Floor

本地 4.1 全绿 + `openspec validate` 严格通过 + 红→绿输出贴在 PR body；node-27 4.2 修复前后对比 receipt（冲刷不再发生、回放 ≤ 32、越界 offset 空页、突发无 500）；4.3 记录为 deferred 并在 #2162 留评论。

## Risk packs considered

- Public API / CLI / script entry: **selected** — `/api/v1/layers` 越界 offset 契约进 OpenAPI + 前端 `types.ts` 再生成；四条路由响应体逐字节不变（`test_api_contract.py`、`test_forecast_api.py`、`test_hydro_display_mvt_scaling.py`、`test_openapi_drift.py`）。
- Config / project setup: not selected — 无新 env 键。
- File IO / path safety / overwrite: not selected — 无文件写入。
- Schema / columns / units / field names: **selected** — OpenAPI 静态 == 运行时；`_hot_paths` 值形状变更只有测试 helper 消费。
- Auth / permissions / secrets: not selected — `_force_refresh`/token 不改（#2079 已收）。
- Concurrency / shared state / ordering: **selected** — `_lock` 内 LRU/计数/快照，锁外回放；`stop_display_catalog_warmer` 锁纪律不变。
- Resource limits / large input / discovery: **selected** — 本 change 主题：LRU 256、准入谓词、回放上界 32、layers key 收敛。
- Legacy compatibility / examples: **selected** — 既有 monkeypatch 替身签名（`**_`）；`_seed_hot_path` 三元组。
- Error handling / rollback / partial outputs: **selected** — loader 抛出不缓存（不变）；回放空结果 `_forget`；> 32 热 key 的退化路径。
- Release / packaging / dependency compatibility: not selected — 仅 stdlib `collections.OrderedDict`。
- Documentation / migration notes: **selected** — runbook 小段 + 两份 receipt + #2162 评论。
- Domain packs（geospatial/time-series/numerical/PostGIS/Slurm/provider/manifest/artifact identity）: not selected — 不触及。
