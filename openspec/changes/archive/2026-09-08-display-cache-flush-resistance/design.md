# Design: display-cache-flush-resistance

## Context

- 现状（`apps/api/display_cache.py` @ `4c79cc39`）：`_store: dict[str, tuple[float, Any]]` `:44`、`_hot_paths: dict[str, tuple[str, float]]` `:46`；`_record_hot_path` `:106-115` 到顶 `clear()` 后登记；`_store_value` `:118-122` 同形；`display_catalog_cached` `:125-145`：非 display 角色直通 → force-refresh 分支（`:129-132`，loader + 无条件 `_store_value`）→ `_record_hot_path` `:133`（查找之前）→ 查 `_store`（`:135-142`，< STALE_MAX 即回）→ loader + `_store_value`。`_warm_loop` `:209-225` 每 45 s 取活跃窗口 1800 s 内**全部**热 path 交给 `_replay_targets` `:228-239` 串行回放（`_mark_warm_scope` 标记，每 path 120 s 超时）。
- 调用方（`display_catalog_cached` 全仓 5 个调用点）：`hydro_display.py:309`（`layers:{run_id}:{limit}:{offset}`，loader 整表构建后 `:307` 切片）、`:337`（`discharge-cycles:{source}`）、`:393-397`（`valid-times:{layer_id}:{run_id}:{source}:{cycle_key}`）、`forecast.py:114-124`（`runs:{basin_id}:{source}:{cycle}:{status}:{limit}:{offset}`；`store.list_runs` 返回 `{"items": [...], "total_count", "limit", "offset"}`，不匹配即空页）、`precip.py:193`（`precip-index:{source}:{instant}`，见 D2 末条）。`valid_times_for_layer`/`national_discharge_valid_times` 的返回 `model_dump()` 为 `{"valid_times", "items", "limit", "observed_count", "truncated"}`（`services/tiles/mvt.py:195-207`）。
- 测试面：`tests/test_display_catalog_cache.py::_seed_hot_path` `:55-58` 直接写 `_hot_paths["warm-guard"] = (path, monotonic)`；`test_warm_loop_replays_hot_paths` `:385-405` 断言 `replayed[0] == ["/api/v1/runs"]`；`test_replay_targets_still_refreshes_a_real_app_without_a_token` `:280-301` 读 `_store["e2e"][1]`；`tests/conftest.py:161` 每测清缓存 + `stop_display_catalog_warmer` 契约（`display_cache.py:173-206`，锁纪律：持锁只做快照，绝不持锁 join）。`tests/test_hydro_display_mvt_scaling.py`（`:1626,1726,2496,2611,2675,2700,2728,2929` 与 `_record` `:2746-2751,2783-2788`）、`tests/test_precip_overlay.py:1635` 以 **3 个位置参数**的替身 monkeypatch `hydro_display.display_catalog_cached`。
- 实测 oracle：`docs/runbooks/receipts/2026-09-08-issue-2078-cache-flush-measurement-node27.md`。生产 :8080 在 `5a86841c` 且仍尊重字面 `refresh` 头（#2079 未部署），所以冷 miss 能在生产上用只读 GET 量到；冲刷/回放段在隔离实例（master、`--workers 1`：一个进程 = 一份 store = 一条预热线程，可归因）。生产 `--workers 2` 时每 worker 各一份缓存与预热线程，放大系数 ×2，冲刷需分别命中两个 worker。
- OpenAPI：`openapi/nhms.v1.yaml` 手工维护且 `tests/test_openapi_drift.py::test_static_openapi_matches_runtime_schema` 要求与 `app.openapi()` 逐字相等——路由上加 `description=` 必须同步进 YAML。

## Goals / Non-Goals

**Goals**
- 公网请求无法用「很多个不同 key」把 `_store` 或 `_hot_paths` 整表清空；空结果的无界维度请求（runs 空页、valid-times 空列表）不产生缓存条目、不成为回放目标。
- 预热线程每 tick 的自伤有常数上界（≤ 32 条回放），且合法热 key 优先。
- 既有响应体逐字节不变；`/api/v1/layers` 越界 offset 有写进 OpenAPI 的明确契约。
- `clear_display_catalog_cache` / `stop_display_catalog_warmer` / `_force_refresh` 契约不变。

**Non-Goals**（带理由）
- 鉴权/限流：产品决策，超出缓存健壮性。
- `forecast_store.list_runs` 对 `basin_id`/`source`/`status` 做取值域校验：改公共语义（空页 200 → 422），且 `basin_id` 集合随注册变化；用「空页不缓存」达成同一目标而不改契约。
- 给 `/api/v1/runs` 的 `offset` 加 `maximum` 或改 OpenAPI：`limit` 已封顶 200，越界 offset 是空页 → 不缓存；契约不动。
- `data_sources.py`/`models.py`/`pipeline.py`/`state_snapshots.py` 的无界 offset：不进本缓存。
- 按 key 前缀配额：更多代码，单前缀内仍可冲刷，且不解决回放自伤；LRU + 准入谓词 + 回放上界三件套已覆盖三段代价链。
- 部署：活动树停在 `hotfix/node27-rollback-pre-2073`（#2162/#2145），生产不 pull 不重启；本 change 的 receipt 在隔离实例产出。

## Decisions

### D1. LRU 取代整表 clear
`_store` 与 `_hot_paths` 改为 `collections.OrderedDict`；命中/更新时 `move_to_end(key)`；插入新 key 且 `len >= _MAX_ENTRIES` 时 `popitem(last=False)` 淘汰最旧一条。全部在既有 `_lock` 内完成。`_MAX_ENTRIES = 256` 不变（实测 256 条冷 miss 的存量代价 ≈ 22 s 的 DB 时间，无需缩）。**诚实边界**：LRU 是卫生不是安全修复——一次 > 256 个**可缓存**不同 key 的突发仍会把期间未被访问的合法 key 淘汰出去，但代价是该 key 一次冷 miss（85 ms 量级）而不是整表归零加半小时回放；这一层的安全修复是 D2/D3/D4。

### D2. 准入谓词 `cacheable`
`display_catalog_cached(request, key, loader, *, cacheable: Callable[[Any], bool] | None = None)`。`value = loader()` 后：`cacheable is None or cacheable(value)` 为真 → 照旧 `_store_value` 并登记热 path；为假 → 不存、不登记，并调用 `_forget(key)` 把该 key 从 `_store` 与 `_hot_paths` 移除（**force-refresh 分支同样过谓词**：为真只 `_store_value`，为假 `_forget`；回放拿到空结果说明数据已过期或 key 本就是垃圾，继续保留旧值或继续回放都是错的）。force-refresh 分支**绝不**登记或刷新 `_hot_paths`（今天 `:129-132` 也不登记）：回放若刷新 `last_access`，热集合（含攻击者塞进来的 32 个可缓存 key）就永不过期，D4 的代价模型失去 1800 s 活跃窗口这个衰减项。谓词只看 loader 返回值，不看 request。非 display 角色直通时忽略谓词。
- `/api/v1/runs`：`cacheable=lambda page: bool(page["items"])`——自由文本过滤与越界 offset 的空页每次都真跑一次 `list_runs`（两条 SQL，实测 ≈ 43 ms），接受：这是今天冷 miss 的代价，只是不再换来一条能被回放的缓存条目。
- `/api/v1/layers/{layer_id}/valid-times`：`cacheable=lambda v: bool(v["valid_times"])`——交集外 `cycle` 的 fail-closed 空列表、无覆盖行的国家级空列表、非 discharge 图层的 `_empty_valid_times()` 都不进缓存。代价如实记：国家级无参形态在 fail-closed 期间每个请求都会跑一次 `_national_discharge_coverage_rows`（两条语句），改前该空列表会被缓存 ≤ 600 s——这是 Risks 里已接受的「空结果不缓存」代价；非 discharge 图层分支不落 SQL。谓词**不能**按「key 有界与否」缩窄到 `(source, cycle)` 形态（round-2 V1 否决）：非 discharge 图层的 `?run_id=<任意>` 在 `validate_identifier` 之前就返回空列表，若对它放开准入，就多出一条零 SQL、无界、可回放的 key 维度。
- key 里的客户端可控 `str | None` 维度一律 `!r` 插值（round-1 A1 的同族：`runs:{basin_id!r}:{source!r}:{cycle!r}:{status!r}:…`、`valid-times:{layer_id}:{requested_run_id!r}:{source!r}:{cycle_key!r}`）：`repr(None) == "None"` 保留缺省维度的拼写，而字面值 `"None"` 变成 `'None'`，不再与缺省折叠。不做这一步的后果比 layers 更重——`GET /api/v1/runs?basin_id=None` 命中无过滤条目后把它的热 path 改写成带过滤的 URL，预热回放得到空页、谓词为假、`_forget` 每 tick 把合法无过滤条目清掉。
- `/api/v1/layers/discharge/cycles`：不传谓词。`source` 是 `Literal`，key 空间为 2，空 `cycles` 缓存与否都无风险；保持不变以免改 timing。
- `/api/v1/layers`：不传谓词。D5 之后 key 空间 = 已存在且 display-ready 的 `run_id` 集合 + `None`（未知 `run_id` → `_require_display_ready` → 404 抛出 → `test_loader_errors_are_not_cached` 已钉「异常不缓存」），有界（未知 run → 404，已知但未 ready → 409，都是异常 → 不缓存）；「无 display-ready run → `[]`」是一条 key，照旧缓存。
- 第五个生产者 `apps/api/routes/precip.py:193`（key `precip-index:{source}:{instant}`）：`_require_mirrored_cycle`（`:166`）在缓存之前对未镜像的 cycle 抛出，key 空间以已镜像 cycle 为界；不传谓词、不改，只是与其余四条共享同一份 LRU 256 与回放 32 个名额。
- 替代「在 display_cache 里按 key 前缀判断空结果」否决：把路由的返回形状知识塞进缓存模块。
- 替代「loader 返回哨兵表示不缓存」否决：每个 loader 都要学一个新协议，谓词只在调用点一行。

### D3. 登记时机与命中计数
`_record_hot_path` 从查找之前（`:133`）移到结果确定之后，且只在**普通路径**上结果可缓存时调用（force-refresh 分支不登记、不刷新，见 D2）：命中 → `_hot_paths[key] = (path, now, hits + 1)`；miss 且可缓存 → `(path, now, 1)`。`_hot_paths` 与 `_store` 同为 LRU（D1）。`test_display_catalog_cache.py::_seed_hot_path` 改写为三元组 `(path, monotonic, 1)`，`_warm_loop` 解包三元组。

### D4. 预热回放上界
`DISPLAY_CATALOG_WARM_REPLAY_MAX = 32`（模块常量，测试可 monkeypatch）。`_warm_loop` 每 tick 在锁内快照活跃窗口内的 `(hits, last_access, path)`，按 `hits` 降序、`last_access` 降序排序后取前 32 条，释放锁再回放（锁纪律与今天相同）。定量（receipt §1 + §5/§6）：实测每条回放 36–38 ms（runs，master 代码进程内）～ 85 ms（layers 冷 p50——`5a86841c` 生产代码路径的读数；master/PR 代码的 `/api/v1/layers` 在该 DB 上因 #2145 缺迁移而 500，无同版本基线），32 条 ≤ 1.2–2.7 s / 45 s tick（≈ 3–6 %，上界百分比待 #2145 落地后重测），而今天 254 条实测 9.2 s / tick（≈ 20 %；layers 型 key 按 85 ms 估 ≈ 22 s / 48 %），持续到 1800 s 活跃窗口过期。结构上界 `active[:32]` 与这些数字无关。合法热 key 的 `hits` 随每次命中递增，只要每次访问都命中（`_store` 条目仍在且未过 `STALE_MAX`）就排在只被访问过一次的 key 之前（一旦 `_store` 侧过期/被淘汰而下一次访问变成 miss，`hits` 按 D3 归 1，重新起算）；被 LRU 淘汰后下一次访问以 `hits=1` 重新登记，与同计数的垃圾 key 按最近访问排序（它更新）仍能进前 32。**诚实边界**：D4 封顶的是回放的**量**，不是回放的**身份**——`hits` 由无鉴权的命中（2–4 ms 一次）递增且不衰减，攻击者用 32 个可缓存的非空 key 各命中几百次就能占满 32 个名额，让真实热 key 退化为 stale-while-revalidate（≤ 600 s 回 stale，之后阻塞重算一次）。无鉴权下不存在抗操纵的排序键；本 change 不试图解决身份问题（那是鉴权/限流的地盘），只保证被占满时的代价是「≤ 32 × 冷查询 / tick + 真实热 key 一次冷 miss」而不是整表归零加半小时回放。

### D5. `/api/v1/layers` 分页移到缓存之后
key `layers:{run_id!r}`（round-1 A1：`SAFE_TILE_IDENTIFIER_RE` 放行字面值 `None`，`f"layers:{run_id}"` 会把 `?run_id=None` 与不传 run_id 折叠成同一 key——缓存了全国目录后该请求命中返回 200 + 全国目录而冷路径 404，且命中会把 `_hot_paths["layers:None"]` 的 path 改写成 404 路径劫持回放；`repr(None) == "None"` 使全国 key 字面量仍是 `layers:None`，run-scoped key 变为 `layers:'run-x'`；master 上默认分页的 `layers:None:100:0` 同样折叠，pre-existing，顺带修），loader 返回完整目录的 `model_dump()` 列表，路由对缓存值做 `[offset : offset + limit]` 后 `_ok`。切片结果与今天 loader 内切片逐字节相同。越界 offset：从缓存值切出 `[]`，200，`data: []`，不落 DB、不产生新条目。契约写进 `Query(default=0, ge=0, description="Zero-based offset into the run's layer catalog; an offset at or beyond the catalog length yields an empty `data` page (HTTP 200).")` 与 YAML `:1957-1964` 同文——YAML 里要写**两处**：`parameters[].description` 与 `parameters[].schema.description`（本仓 FastAPI/Pydantic 对 `Query(description=)` 两处都发出，先例 `search` `:248,251` / `stream_order_min` `:261,263`；只写一处 `test_static_openapi_matches_runtime_schema` 即红）。前端 `apps/frontend/src/api/types.ts` 由 `pnpm generate:api` 再生成（description 变 JSDoc）。**偏离 issue AC2**（「422 或钳制，写进 OpenAPI；若加 `le=` 则补 `maximum`」）：选「越界为空页」而非 422（今天就是 200 空页，改 422 会改公共契约且前端分页器按空页停），且**不加** `maximum`——目录长度不是常量，封顶数字既没有依据也不再需要（安全性来自 key 不含 offset）。记入 PR 偏离记录。

### D6. `cacheable` 与既有 monkeypatch 替身
路由把 `cacheable=` 作为关键字参数传入；`tests/test_hydro_display_mvt_scaling.py` / `tests/test_precip_overlay.py` 的替身 `lambda _request, _key, load: load()` 与 `_record(_request, key, load)` 改为接受 `**_`（`_record` 继续只记 key）。这些用例的断言一字不改。

### D7. 观测
不加日志/指标：淘汰是常态路径，逐条打日志只会在攻击时放大 I/O。receipt 的 oracle 是 TTFB（冲刷）+ 进程内 `_replay_targets`/`_store_value` 钩子计数（回放上界，receipt §6 的 `probe_app.py` 法）；PG `pg_stat_activity` 采样已证明在 uvicorn 进程里看不到回放（receipt §4），不再使用。

## Risks / Trade-offs

- 空页/空列表不再缓存：合法但空的查询（例如前端探测一个尚未发布的 cycle）每次落 DB 一次；量级 = 今天的冷 miss（43–85 ms），且这些请求本来在 60 s TTL 后也要重算。接受。
- LRU 下 > 256 个可缓存不同 key 的突发仍能挤掉合法 key（一次冷 miss）；可缓存的无界维度只剩 `/api/v1/runs` 上**非空**的 `offset`/`limit` 组合（受 `total_count` 与 `limit ≤ 200` 约束，实际 key 数 ≤ total_count × 200）与 `valid-times` 上有数据的 `cycle`（受已发布 cycle 集合约束）。已知、已量化、记入矩阵。
- 回放上界 32：若真实热 key 超过 32 个（自然增长或被攻击者用高 `hits` 的非空垃圾 key 占满名额，见 D4 诚实边界），排名靠后的热 key 退化为 stale-while-revalidate（≤ 600 s 内回 stale，之后阻塞重算）。当前前端目录 key 数个位数。
- `_hot_paths` 值形状变化：仅测试 helper 引用它（`grep` 全仓无其它消费者）。

## Invariant Matrix

- Governing invariant: 任何公网请求都不能整表清空 `_store`/`_hot_paths`；只有产生**可缓存结果**的请求才新增缓存条目或回放目标；每 tick 回放 ≤ `DISPLAY_CATALOG_WARM_REPLAY_MAX` 条且命中计数高者优先；四条路由响应体逐字节不变。
- Source-of-truth identity/contract: `display_catalog_cached(..., cacheable=)` 的谓词（runs `items` 非空、valid-times `valid_times` 非空）；`layers:{run_id!r}` 的完整目录 + 缓存后切片；`_hot_paths[key] = (path, last_access, hits)`。
- Producers: 四条目录 GET（`hydro_display.py` layers/cycles/valid-times，`forecast.py` runs）+ `precip.py:193` precip-index（key 空间以已镜像 cycle 为界，不改）；`_replay_targets`（force-refresh 分支，同受谓词约束）。
- Validators/preflight: `Query(ge=/le=)`、`validate_identifier`、`Literal` source、`_validated_national_valid_time_selector`（422 在缓存之前）；本 change 的 `cacheable` 谓词。
- Storage/cache/query: `display_cache._store`/`_hot_paths`（OrderedDict LRU）、`_store_value`/`_record_hot_path`/`_forget`、`_warm_loop` 排序快照。
- Public routes/entrypoints: `/api/v1/layers`（key 收敛 + 后切片 + OpenAPI description）、`/api/v1/runs`、`/api/v1/layers/{layer_id}/valid-times`、`/api/v1/layers/discharge/cycles`（不变）；全部在每个角色下注册（`route_registry.py`）。
- Frontend/downstream consumers: 前端目录抓取（body 不变）；`scripts/node27_mvt_prewarm.py`（token 头语义不变）；`docs/runbooks/display-readonly-live-mvt.md` 读者。
- Failure paths/rollback/stale state: loader 抛出 → 不缓存、不登记（不变）；回放拿到空结果 → `_forget`；> 256 可缓存 key 突发 → 最旧淘汰、合法 key 一次冷 miss；热 key > 32（含攻击者用高 `hits` 非空 key 占满名额）→ 排名靠后者退化为 stale-while-revalidate；回放量始终 ≤ 32 × 冷查询 / tick。
- Evidence/audit/readiness: 实测 receipt（修复前）+ 隔离实例修复前后对比 receipt；单测红→绿。
- Regression rows:
  - display 角色：合法 key `k` 命中一次后，写入 300 个**不可缓存**（谓词为假）的不同 key → `k` 仍在 `_store`，300 个 key 都不在 `_store`/`_hot_paths`，每个 key 的 loader 恰好被调用一次（不缓存）。（改前红：第 256 个把 `k` 清掉。）
  - display 角色：写入 255 个可缓存 key、命中 `k`、再写入 255 个可缓存 key → `k` 仍在 `_store`，`len(_store) == 256`，最早的 key 已淘汰。（改前红：整表清空。）
  - display 角色：连续写入 300 个可缓存 key、期间不访问 `k` → `k` 被淘汰但 `len(_store) == 256`、`_store` 从未为空（LRU 诚实边界，钉「不整表清空」）。
  - `_hot_paths`：同上两条的热 path 版本；不可缓存 key 永不登记；可缓存 miss 以 `hits=1` 登记；命中递增 `hits`。
  - force-refresh（warm scope 标记）回放一个已缓存 key、loader 这次返回不可缓存值 → key 从 `_store` 与 `_hot_paths` 移除，随后普通请求调用 loader 而不是回旧值。
  - force-refresh 回放一个热 key、loader 返回可缓存值 → `_store` 更新，`_hot_paths[key]` 的 `(last_access, hits)` 原样不动（回放不延长自己的活跃窗口）。
  - `_warm_loop`：40 条活跃热 path（1 条 `hits=5` + 39 条 `hits=1`）→ `_replay_targets` 收到恰好 32 条且第一条是 `hits=5` 的 path；`DISPLAY_CATALOG_WARM_REPLAY_MAX` 被 monkeypatch 为 2 时收到 2 条。
  - `test_warm_loop_replays_hot_paths` / `test_stop_warmer_reports_join_timeout_without_resetting_state` 用三元组 `_seed_hot_path` 照旧绿；`_replay_targets` e2e 用例（`_store["e2e"][1]`）照旧绿；`stop_display_catalog_warmer` / `clear_display_catalog_cache` 契约不变。
  - `/api/v1/layers`（display 角色，`_default_layer_catalog` 替身返回 3 条）：`offset=1&limit=1` → 第二条；`offset=5` → 200 且 `data == []`；三次不同分页只让 loader 跑一次且 `_store` 只含 `layers:None`；同参数响应体与改前逐字节相同（对比 `git show origin/master` 版本的切片结果或直接断言切片等式）。
  - `/api/v1/runs`（display 角色，store 替身）：空页 → `_store`/`_hot_paths` 不含该 key、第二次请求再次调用 store；非空页 → 缓存、第二次不调用 store。
  - `/api/v1/layers/discharge/valid-times?source=gfs&cycle=<交集外>` → 200 + `valid_times: []` 且不进 `_store`/`_hot_paths`；有覆盖的 cycle → 缓存；`test_valid_times_cache_key_collapses_spellings_and_separates_identities` 等 key 用例不变。
  - `/api/v1/layers/discharge/cycles`：空 `cycles` 照旧缓存（不传谓词；用例断言该路由不传 `cacheable`）。
  - `/api/v1/layers`（display 角色）：先缓存全国目录，再 `GET /api/v1/layers?run_id=None` → 404 且 `_hot_paths["layers:None"]` 的 path 仍是 `/api/v1/layers`。
  - 普通路径谓词为假且该 key 已有（过期的）`_store`/`_hot_paths` 条目 → 两表都移除。
  - `/api/v1/runs?basin_id=None`（display 角色，无过滤条目已缓存）→ 走 store 得到空页（不是缓存的无过滤页），无过滤条目仍在 `_store`，其热 path 仍是 `/api/v1/runs`；`/api/v1/layers/discharge/valid-times?run_id=None` → 404 且国家级热 path 不变。
  - 非 display 角色 + `cacheable=` → 直通 loader（谓词不被调用）。
  - `openapi/nhms.v1.yaml` == `app.openapi()`（`test_openapi_drift.py`）；`tests/test_openapi_31_contract.py`、`tests/test_api_contract.py` 全绿。

## Boundary-surface checklist

- Shared helper roots: `display_cache.display_catalog_cached`（签名加关键字参数）、`_store_value`/`_record_hot_path`（LRU + 时机）、新 `_forget`、`_warm_loop`（排序 + 上界）。
- Public entrypoints: 四条目录 GET；OpenAPI `/api/v1/layers` `offset` description。
- Read surfaces: 缓存命中路径多一次 `move_to_end` 与 `_hot_paths` 计数更新（锁内 O(1)）。
- Write/overwrite surfaces: `_store_value` 由「到顶清空」改为「淘汰最旧」；`_forget` 新增删除路径。
- Producer/consumer evidence boundaries: 响应体不变；prewarm summary 不变。
- Stale-state boundaries: 空结果不缓存；回放空结果即忘记；> 32 热 key 的退化。
- Unchanged downstream consumers: 前端、`scripts/node27_mvt_prewarm.py`、`scripts/diagnostic/display-cold-waterfall.sh`。
