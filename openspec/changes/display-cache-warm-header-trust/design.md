# Design: display-cache-warm-header-trust

## Context

- 现状：`display_cache.py:50-54` `_force_refresh` = `headers.get("x-nhms-cache-warm") == "refresh"`；`:80-83` 在查 store 前短路；`:186` 进程内预热带同一头回放。`apps/api/main.py` 的鉴权中间件只管 mutation，GET 直通；nginx 反代同机 `127.0.0.1:8080`，每个公网请求到应用时 `client.host` 都是 loopback，`X-Real-IP`/`X-Forwarded-For` 又是头——**来源不可信**。
- 调用方（与 issue 描述的差异）：除进程内 `_replay_targets` 外，`scripts/node27_mvt_prewarm.py::fetch_json`（#2013，`43ad7179`）对 `/cycles`、`/valid-times` 两跳带 `refresh`，由 autopipe 每 tick（约 10 min）在 publish 之后立刻以 `.venv/bin/python` 起进程、经 TCP loopback 调用；其理由是 publish 后不强制刷新可能「拿着上一周期的目录去预热」。issue 的推荐方案（纯 ASGI scope 标记）会把这条合法调用方一起关掉。
- 实测 oracle：`docs/runbooks/receipts/2026-09-08-issue-2079-cache-warm-measurement-node27.md`（生产 :8080 在 `5a86841c`；`git diff 5a86841c origin/master -- apps/api/display_cache.py tests/test_display_catalog_cache.py infra/nginx/` 为空，所以该实测对 master 有效）。
- 生产 `--workers 2`：进程内缓存与预热线程按 worker 各一份；loopback 调用只落到一个 worker。这一点本 change 不改也不恶化。

## Goals / Non-Goals

**Goals**
- 公网请求无论带什么头值都不能强制冷路径；进程内预热与 autopipe prewarm 的强制刷新能力保留。
- token 未配置是安全默认（等价于「只有进程内可强制刷新」），不是启动失败；prewarm 在无 token 时退化为 stale 窗口而非失败。

**Non-Goals**（带理由）
- nginx 剥头：需 root 改线上 conf，仓内 conf 不是线上真相，且不覆盖直连 :8080。
- 通用鉴权/限流：产品决策，超出「关掉一个特权头」。
- #2078 的 `_MAX_ENTRIES`/`clear()`/`offset`/自由文本 key：同模块另一缺陷，串行处理，本 PR 不碰。
- 活动 change `display-v2-national-timeline-precip-overlay` 的 `tasks.md:509` 与 `invariant-matrix-i5-2009.md:440,470` 措辞：后者把 `-H 'x-nhms-cache-warm: refresh'` 写成 receipt 方法，部署 token 后按字面执行只量到 warm 路径——记入偏离记录，不改活动 change。
- 生产部署（token 写入 `display.env` 与 `node27-ingest.env` + 重启）：活动树停在回滚分支（#2162/#2145），部署随该维护窗口；在此之前生产行为与今天相同（头仍被尊重）。

## Decisions

### D1. 两种身份、一个判定函数
`_force_refresh(request)` 返回 `True` 当且仅当：(a) `request.scope.get("state")` 里 `_WARM_SCOPE_KEY = "nhms_display_cache_warm"` 为 `True`；或 (b) `token = runtime_config.display_cache_warm_token` 非空且请求头 `x-nhms-cache-warm` 存在且 `hmac.compare_digest(header.encode("utf-8"), token.encode("utf-8"))` 为真——**必须比较 bytes**：Starlette 按 latin-1 解头（`starlette/datastructures.py`），`compare_digest` 对含非 ASCII 的 `str` 抛 `TypeError`（实跑证实），若按 str 比较，token 部署后任意客户端发一个 ≥0x80 字节的头值就能把每条目录 GET 打成 500；头值经 latin-1 解码不会含 lone surrogate；token 来自 `os.environ`（`surrogateescape` 解码，`display.env` 里一个非法 UTF-8 字节会变成 `\udcXX`），所以两侧统一用 `encode("utf-8", "surrogateescape")`：本 change 的两个来源——latin-1 解出的头值（≤ U+00FF）与 `os.environ` 的 `\udc80`–`\udcff`——都不抛（其它 lone surrogate 如 `\ud800` 仍会抛，但两个来源都产不出它；不加 try/except）。`runtime_config` 的读取走 `_display_readonly` 同款三层 `getattr`（`request.app.state.runtime_config`，`display_cache.py:45-47`）；缺 `app`/`state`/`runtime_config`、缺 `request.scope`、缺 `headers`、头值非 `str`，一律 `False`（`tests/test_display_catalog_cache.py::_request` 的 `SimpleNamespace` 既无 `.headers` 也无 `.scope`，既有 8 条用例必须继续绿）。其余（含字面 `refresh`、空 token、缺头、错 token）返回 `False`。
- 替代「进程内也用 token」否决：token 未配置时进程内预热会静默失去刷新能力，stale-while-revalidate 整条退化，且不会有任何报错。
- 替代「进程随机 secret 作为头值」可行但多一个值在两处流转；scope 标记不经网络、零配置，更直接。

### D2. 进程内标记的注入点
`_replay_targets` 不再发头，而是把 `app` 包成 `_mark_warm_scope(app)`：`async def _app(scope, receive, send)`——对 `scope["type"] == "http"` 的请求 `scope = dict(scope); state = dict(scope.get("state") or {}); state[_WARM_SCOPE_KEY] = True; scope["state"] = state`，再 `await app(scope, receive, send)`；交给 `httpx.ASGITransport(app=marked)`。`Request.state`/`request.scope["state"]` 在 Starlette 里就是这个字典，`ok_response` 读 `request.state.request_id` 的既有路径不受影响（键不同）。公网请求经 uvicorn 到达时 `scope["state"]` 由服务器构造（lifespan state 的拷贝），客户端无法写入。

### D3. token 的来源与不泄露
`RuntimeConfig` 加 `display_cache_warm_token: str | None = field(default=None, repr=False)`；`load_runtime_config` 从 `NHMS_DISPLAY_CACHE_WARM_TOKEN` 读取，`strip()` 后空即 `None`。`public_dict()` 不含它（`/api/v1/runtime/config` 与 OpenAPI patch 都以 `public_dict` 为准）。不做长度/熵校验：这是运维值，弱值的后果只是回到今天。

### D4. prewarm 侧
`fetch_json` 读 `os.environ.get("NHMS_DISPLAY_CACHE_WARM_TOKEN", "").strip()`：非空则发 `{header: token}`；空则不发该头，且进程内只打一次 stderr warning `prewarm: NHMS_DISPLAY_CACHE_WARM_TOKEN unset; discovery may see up to 45 s stale catalog`（模块级 `_warned` 标志）。summary 形状不变。`:66-76` 的注释块改写成 token 语义。

### D5. 部署形状
token 同值写入 `/home/nwm/NWM/infra/env/display.env`（display 进程）与 `/home/nwm/NWM/infra/env/node27-ingest.env`（autopipe→prewarm；`node27_autopipe_cron.sh:36` 硬拒 `display.env`），两文件已 0600；重启 display API。receipt 中不出现 token 值。**注入路径只有一条**：生产 display 进程由 `nhms-display-api.service` 的 `set -a; . display.env` 与 `scripts/ops/start-display-api.sh:41-44` 同法注入整份 env；`infra/compose.display.yml` 逐条列举 `environment:` 且**不是**当前 display 部署路径（`infra/env/display.example:1-3` 的 compose 用法是历史模板用途），本 change 不给 compose 加透传，也不改 `DISPLAY_AUDITED_INTERPOLATION_ENV`——若日后走 compose 部署，必须同时加透传并同步该 allow-list（`scripts/validate_two_node_docker_runtime.py:391-415`），否则 token 声明了却进不了应用，prewarm 发着应用不认的 token 静默退化（该缺口记入矩阵）。

## Risks / Trade-offs

- 生产在部署前保持今天的暴露；已知、已路由（#2162 维护窗口）。
- token 未配置 + prewarm：publish 后最长 45 s（热 key 由进程内预热刷新）或最长 600 s（冷 key）内 discovery 可能拿到旧周期 → 该 tick 预热旧周期，下一 tick 自愈；warning 可见于 autopipe 日志。
- `--workers 2` 下 token 请求只刷新命中的 worker——与今天相同。

## Invariant Matrix

- Governing invariant: 只有进程内预热线程（ASGI scope 标记）或持有配置 token 的请求能让 `display_catalog_cached` 跳过 store 查找并重算；其它任何请求（含头值 `refresh`、错 token、token 未配置时的任意值）都按 TTL/stale 规则命中；非 display 角色照旧直通 loader。
- Source-of-truth identity/contract: `scope["state"]["nhms_display_cache_warm"] is True`；`RuntimeConfig.display_cache_warm_token`（来自 `NHMS_DISPLAY_CACHE_WARM_TOKEN`）与头 `x-nhms-cache-warm` 的 `compare_digest`。
- Producers: `display_cache._replay_targets`（标记）、`scripts/node27_mvt_prewarm.py::fetch_json`（token 头）。
- Validators/preflight: `display_cache._force_refresh`；`runtime_mode.load_runtime_config`（strip→None）。
- Storage/cache/query: `_store`/`_hot_paths`/`_store_value`（不改）。
- Public routes/entrypoints: `hydro_display.py` `/api/v1/layers`、`/layers/discharge/cycles`、`/layers/discharge/valid-times`；`forecast.py` `/api/v1/runs`；`/api/v1/runtime/config`（不得出现 token）。
- Frontend/downstream consumers: 前端不发该头（不变）；autopipe prewarm（改为 token）；`docs/runbooks/display-readonly-live-mvt.md` 读者。
- Failure paths/rollback/stale state: token 未配置 → 外部永不刷新、进程内照旧；prewarm 无 token → 不发头 + warning + 继续；错 token → 命中。
- Evidence/audit/readiness: 单测（红→绿）；node-27 隔离实例 receipt（`refresh` 命中、token 冷、进程内预热仍每 45 s 打 PG）。
- Regression rows:
  - display 角色 + 已缓存 key + 外部头 `refresh` → 命中，loader 未调用（改前红）。
  - display 角色 + 已缓存 key + 头值 == 配置 token → 重算并写回；随后普通请求命中新值。
  - display 角色 + 头值 ≠ token / token 未配置且头值任意 / 缺头 → 命中。
  - display 角色 + token 已配置 + 头值含 ≥0x80 字节（latin-1 解出的非 ASCII `str`，如 `"ab\u00e9"`）→ 命中、不抛、loader 未调用（改前若按 str 比较会 `TypeError`）。
  - `_request()` 式无 `headers`/`scope` 的 request 对象 → `_force_refresh` 为 `False`，既有用例不变。
  - compose 部署路径（非当前）：`infra/compose.display.yml` 不透传该键 → 应用侧 token 为 `None`、外部永不刷新；记录为已知缺口，不在本 change 处理。
  - 进程内 `_replay_targets` 回放（token 未配置）→ 重算并写回（`_mark_warm_scope` 标记生效；改前用头，改后用标记，两者都须让该用例绿）。
  - 非 display 角色 + 任意头 → 直通 loader（不变）。
  - `load_runtime_config`：`NHMS_DISPLAY_CACHE_WARM_TOKEN=" abc "` → `"abc"`；缺失/空白 → `None`；`public_dict()` 与 `repr(config)` 不含 token 值。
  - prewarm `fetch_json`：env 有 token → 头值 == token；无 token → 无该头 + 一条 warning（多次调用只一条）+ 返回体照常解析。
  - `stop_display_catalog_warmer` / `clear_display_catalog_cache` 契约不变（既有用例全绿）；`tests/test_api_contract.py`、`tests/test_hydro_display_mvt_scaling.py` 全绿。

## Boundary-surface checklist

- Shared helper roots: `display_cache._force_refresh`（改）、`_replay_targets`（改）、`RuntimeConfig`（加字段）。
- Public entrypoints: 四条目录 GET（行为：外部不再可旁路）；`/api/v1/runtime/config`（不变，且不得新增泄露）。
- Read surfaces: 缓存命中路径不变。
- Write/overwrite surfaces: `_store_value` 调用点不变，只是触发条件收紧。
- Producer/consumer evidence boundaries: prewarm summary schema 不变。
- Stale-state boundaries: token 缺失时的退化路径（见 Risks）。
- Unchanged downstream consumers: 前端、`scripts/diagnostic/display-cold-waterfall.sh`（靠重启制冷，不发头）。
