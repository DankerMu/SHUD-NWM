# node-27 实测：`x-nhms-cache-warm` 头旁路 display 目录缓存的放大倍数（issue #2079，实施前）

- 日期：2026-09-08T14:30:07Z – 14:30:23Z；节点 node-27，本机 `127.0.0.1:8080`（**未打公网入口**）。
- 被测进程：生产 display API，活动树 `5a86841c`（`hotfix/node27-rollback-pre-2073`），`--workers 2`（`ps` 见 uvicorn master 命令行；脚本里 `pgrep` 只匹配到 master，故日志中的 `workers=1` 是计数方法伪影，不是实配）。`git diff 5a86841c origin/master -- apps/api/display_cache.py tests/test_display_catalog_cache.py infra/nginx/` 为空，所以本实测对 master 同样成立。
- 方法：脚本 `/home/nwm/tmp/measure-2079.sh`（日志 `/home/nwm/tmp/measure-2079.log`）；`curl -w '%{time_starttransfer}'`；PG 侧用 `display.env` 的只读角色 `nhms_display_ro` 每 0.1 s 轮询 `pg_stat_activity`（`usename=current_user and state='active'`，排除采样自身）4 s 取峰值。本文不含任何 DSN。

## 1. 单请求 TTFB（各 3 次预热后 6 样本）

| 端点 | 不带头（ms） | 带 `x-nhms-cache-warm: refresh`（ms） |
|---|---|---|
| `/api/v1/layers` | 1.9 / 1.8 / 3.1 / 3.6 / 2.0 / 3.0 | 91.5 / 91.2 / 86.3 / 87.8 / 91.0 / 73.5 |
| `/api/v1/runs` | 3.8 / 3.2 / 3.4 / 3.2 / 3.3 / 3.2 | 82.0 / 79.3 / 80.0 / 78.2 / 83.8 / 81.2 |

放大倍数约 30–40×。带头与不带头的 `/api/v1/layers` 响应体 sha256 不同（`request_id` 信封每次不同，属预期），说明带头请求确实走了 loader。

## 2. 20 并发

| 组 | PG 同角色并发活动查询峰值 | 20 个请求 TTFB 范围（ms） |
|---|---|---|
| 带头 | **8** | 74.9 – 189.6，全部 200 |
| 不带头（对照） | 0 | 17.9 – 24.5，全部 200 |

基线（采样前）活动查询 0。无 coalescing：20 个带头请求各自跑 loader，PG 侧并发被连接池（每 worker `NHMS_DISPLAY_DB_POOL_SIZE=4` + overflow 2）而不是缓存挡住。

## 3. 结论（供 fixture 与优先级）

- 门是真开着的：任意客户端一个头即可把每个目录请求从 ~3 ms 变成 ~85 ms，并把 DB 并发从 0 推到池上限。
- 绝对量纲今天不算灾难（冷 catalog ≈ 80–90 ms，非历史 receipt 的 400 ms/12 s）；issue 元信息里「并发放大有限可降 p2」的裁定留给维护者，本文只给数字。
- 修复形状见 `openspec/changes/display-cache-warm-header-trust/design.md`。
