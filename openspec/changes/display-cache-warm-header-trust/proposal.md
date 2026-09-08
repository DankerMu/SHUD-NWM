# display 目录缓存的强制刷新信任边界（issue #2079）

## Why

`apps/api/display_cache.py::_force_refresh` 只看请求头 `x-nhms-cache-warm: refresh` 的取值，不看来源与身份；display 节点上所有 GET 无鉴权、无限流，nginx（`infra/nginx/test.nwm.ac.cn.conf`）也不剥这个头，所以公网任意客户端都能逐请求旁路 `display_catalog_cached`，把目录端点打回冷路径。node-27 本机 2026-09-08 实测（`docs/runbooks/receipts/2026-09-08-issue-2079-cache-warm-measurement-node27.md`）：`/api/v1/layers` 与 `/api/v1/runs` 不带头 TTFB 2–4 ms、带头 73–92 ms（≈ 30–40×）；20 并发带头请求让 display 角色在 PG 上的并发活动查询峰值 8（对照组 0），全部 200、最慢 190 ms。量纲不算灾难，但门是真开着的，且修复很小。

## What Changes

- `apps/api/display_cache.py`：强制刷新只在两种身份下生效——(1) 进程内预热线程 `_replay_targets` 经 `httpx.ASGITransport` 回放时在 ASGI `scope["state"]` 里打的标记（网络侧不可伪造，无需配置）；(2) 请求头 `x-nhms-cache-warm` 的值与配置的 `NHMS_DISPLAY_CACHE_WARM_TOKEN` 以 `hmac.compare_digest` 相等。token 未配置时**任何**头值都不生效；字面值 `refresh` 不再是特权值。
- `apps/api/runtime_mode.py::RuntimeConfig`：新增 `display_cache_warm_token: str | None`（`repr=False`，不进 `public_dict`，故 `/api/v1/runtime/config` 不泄露）。
- `scripts/node27_mvt_prewarm.py::fetch_json`：从 `NHMS_DISPLAY_CACHE_WARM_TOKEN` 取值作为头值；未配置时不发该头、向 stderr 打一条 warning、发现流程照常（退化为 ≤ 45 s 的 stale 窗口，即 #2013 之前的行为，而不是失败）。这是对 issue「无外部调用方」前提的**修正**：#2013（`43ad7179`）起 prewarm 的两跳发现请求就带这个头，且其 fixture 明文要求 publish 后立刻旁路 stale 窗口。
- env 模板：`infra/env/display.example` 与 `infra/env/node27-ingest.example` 各加 `NHMS_DISPLAY_CACHE_WARM_TOKEN`（autopipe 只 source `node27-ingest.env` 且硬拒 `display.env`，所以是两处、同值）。
- 测试：`tests/test_display_catalog_cache.py::test_force_refresh_header_bypasses_cache` 改写为「外部 `refresh` 命中缓存、loader 不被调用」+ token/进程内标记两条阳性用例（AC 明说改写不删）；`tests/test_node27_mvt_prewarm.py` 的头断言改为 token 语义；`tests/test_runtime_mode.py` 钉 token 解析与不泄露。
- 文档：`docs/runbooks/display-readonly-live-mvt.md` 目录缓存/预热段补 token 语义与两处 env 的部署步骤。
- **不做**：nginx 剥头（线上 conf 非仓内真相、需 root、绕反代直连仍中招）；display 路由通用鉴权/限流；#2078 的 key 空间/容量策略；`display-v2-national-timeline-precip-overlay/tasks.md:509` 与 `invariant-matrix-i5-2009.md:440,470` 的历史措辞（活动 change，记入偏离记录不改）。

## Capabilities

### New Capabilities

- `display-catalog-cache-trust`：display 目录缓存「谁能强制刷新」的信任边界——进程内预热标记与配置 token 两种身份，其余请求一律按 TTL/stale 规则命中。

### Modified Capabilities

（无：`overview-data-contracts` 只引用缓存存在，不规定旁路身份；目录端点响应体与 key 不变。）
