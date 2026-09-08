# display 目录缓存的冲刷抵抗与预热回放上界（issue #2078）

## Why

`apps/api/display_cache.py` 的进程内目录缓存以 `_MAX_ENTRIES = 256`（`:41`）封顶，但到顶的动作是整表 `clear()`——`_store_value` `:120-121`、`_record_hot_path` `:113-114`——而且热 path 在缓存查找**之前**就被登记（`:133`），对每个非 force-refresh 请求无条件登记。四条目录 GET 的 key 里有客户端可控且无界的维度：`/api/v1/layers` 的 `offset`（`apps/api/routes/hydro_display.py:277`，key `:309`）、`/api/v1/runs` 的 `offset` 与自由文本 `basin_id`/`source`/`status`（`apps/api/routes/forecast.py:105,110-113`），以及 `/api/v1/layers/{layer_id}/valid-times` 的 `cycle`（`hydro_display.py:393-397`，交集外的 fail-closed 空 `valid_times: []` 是一条可缓存的 200）。display 节点上这些 GET 无鉴权、无限流（`apps/api/route_registry.py` 全角色注册）。

node-27 实测（`docs/runbooks/receipts/2026-09-08-issue-2078-cache-flush-measurement-node27.md`）：生产 :8080 冷 miss `/api/v1/layers` p50/p95 = 85.2/97.6 ms、`/api/v1/runs` 83.2/87.6 ms，命中约 2–4 ms（≈ 30×）；隔离实例（master，`--workers 1`）串行注入 256 个 `basin_id=junk-<i>` 后，刚被命中的 `/api/v1/runs` 立即回到 71 ms 冷路径（冲刷复现）。到顶那一次 `clear()` 把 `_hot_paths` 也清掉，所以恰好到顶的突发反而让预热线程无物可放——自伤放大需要差一条到顶的突发（`_hot_paths` 满而不清），进程内直接回放 254 条热 path 一个 tick 实测 **9.2 s**（36–38 ms/条），uvicorn 进程里的预热线程每 tick 确实回放全部热 path（receipt §5/§6）——一次 11 s 的注入换来 30 min 内约 5 min 的串行 DB 工作（tick 周期 = 45 s 等待 + 9.2 s 回放 ≈ 54 s，≈ 33 个 tick）。

## What Changes

- `apps/api/display_cache.py`
  - `_store` / `_hot_paths` 改为 `OrderedDict` LRU：命中 `move_to_end`，到顶 `popitem(last=False)` 淘汰最旧一条；永不整表 `clear()`（测试钩子 `clear_display_catalog_cache` 除外）。`_MAX_ENTRIES = 256` 不变。
  - `display_catalog_cached(request, key, loader, *, cacheable=None)`：新增可选谓词 `cacheable: Callable[[Any], bool]`。loader 返回值不满足谓词时**既不写 `_store` 也不登记 `_hot_paths`**，并把该 key 从两表移除（force-refresh 分支同样适用：回放拿到空结果即忘记该 key）。默认 `None` = 一律可缓存（既有调用方行为不变）。
  - 热 path 登记移到缓存查找**之后**、且只对可缓存结果登记：命中 → 命中计数 +1 并刷新时刻；miss 且可缓存 → 以计数 1 登记。`_hot_paths` 值变为 `(path, last_access, hits)`。
  - 预热线程每 tick 只回放活跃窗口内按「命中计数降序、最近访问降序」排序的前 `DISPLAY_CATALOG_WARM_REPLAY_MAX = 32` 条；自伤上界由此从「≤ 256 条 × 冷查询」变为「≤ 32 条 × 冷查询」（按实测 43–85 ms/条：≤ 2.7 s / 45 s tick）。
- `apps/api/routes/hydro_display.py`
  - `/api/v1/layers`：缓存 key 收敛为 `layers:{run_id}`，loader 返回**完整**目录（`_default_layer_catalog` 本来就整表构建），`layers[offset : offset + limit]` 的切片移到缓存之后。响应体逐字节不变；越界 `offset` 变成不落 DB、不产生缓存条目的空页 200，契约写进 OpenAPI `offset` 的 `description`（不加 `maximum`：目录长度不是常量，安全性来自 key 不再含 offset，而不是给 offset 封顶）。
  - `/api/v1/layers/{layer_id}/valid-times`：`cacheable=lambda v: bool(v["valid_times"])`——空 `valid_times`（fail-closed、无覆盖、非 discharge 图层）不进缓存、不进热 path。
  - `/api/v1/layers/discharge/cycles`：不变（`source` 是 `Literal["gfs","ifs"]`，key 空间为 2，空 `cycles` 照旧缓存）。
- `apps/api/routes/forecast.py` `/api/v1/runs`：`cacheable=lambda page: bool(page["items"])`——自由文本过滤命中空页不进缓存、不进热 path。key、`_paginated_payload`、OpenAPI 不变。
- `openapi/nhms.v1.yaml:1957-1964` `/api/v1/layers` 的 `offset` 参数加 `description`（参数级与 schema 级两处，与路由 `Query(..., description=...)` 同文，`tests/test_openapi_drift.py::test_static_openapi_matches_runtime_schema` 要求静态 == 运行时）；`apps/frontend/src/api/types.ts` 由 `pnpm generate:api` 再生成。
- `scripts/select_ci_tests.py`：`apps/api/routes/forecast.py` → `tests/test_forecast_api.py` 规则（该 suite 钉 `/api/v1/runs` 响应体，今天不在该路径的 PR 选测里）。
- 测试：`tests/test_display_catalog_cache.py` 新增 LRU/谓词/登记时机/回放上界用例；`_seed_hot_path` 改为三元组；`tests/test_hydro_display_mvt_scaling.py` 与 `tests/test_precip_overlay.py` 里 monkeypatch `display_catalog_cached` 的 3 位置参数替身加 `**_`（否则新关键字参数让它们 `TypeError`）；路由级用例钉 layers 后切片、runs/valid-times 空结果不入缓存。
- 文档：`docs/runbooks/display-readonly-live-mvt.md` 目录缓存段补「准入与淘汰」三句；receipt 两份（实测 + 修复前后对比）。
- **不做**：display 路由鉴权/限流（产品决策）；`forecast_store.list_runs` 自由文本取值域校验（会改语义：今天不匹配返回空页 200，收窄会变 422）；`data_sources.py`/`models.py`/`pipeline.py`/`state_snapshots.py` 的同形无界 `offset`（不进本缓存）；#2032 的磁盘瓦片缓存；按前缀配额（备选，见 design）；生产部署（活动树停在回滚分支，随 #2162 维护窗口）。

## Capabilities

### New Capabilities

（无）

### Modified Capabilities

- `display-catalog-cache-trust`：新增「准入与淘汰」「预热回放上界」「layers 分页在缓存之后」三条需求；既有「强制刷新身份」需求不变。
