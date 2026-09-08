# node-27 receipt：display 目录缓存强制刷新信任边界（issue #2079 / PR #2168，tasks 4.2）

- 日期：2026-09-08T15:51:44Z – 15:58:12Z；节点 node-27；被测提交 `7ff03f6f`（PR #2168 head）。
- 执行方式：**未动生产**。`/home/nwm/NWM` 活动树（`hotfix/node27-rollback-pre-2073` = `5a86841c`）不 pull、不重启。
- 隔离实例：一次性 worktree `/home/nwm/tmp/wt-2079`（`git worktree add --detach`，自建 venv，Python 3.11.15），uvicorn `127.0.0.1:8090`、`--workers 2`、专用空 `NHMS_MVT_FILE_CACHE_DIR`；env 取 `infra/env/display.env`，叠加随机 `NHMS_DISPLAY_CACHE_WARM_TOKEN`（值不入本文）与 `DATABASE_URL` 追加 `application_name=nhms-2079-receipt`；`TMPDIR=/home/nwm/tmp`。
- 脚本 `/home/nwm/tmp/receipt-2079.sh`，日志 `/home/nwm/tmp/receipt-2079-7ff03f6f1421d723a558bdeb69f0778cb7af169b.log`。本文不含 DSN、不含 token。
- 结束后 worktree 与缓存目录已删，`git worktree list` 回到 4 条（均为节点上早已存在的他人 worktree）。

## 1. worktree pytest（tasks 4.1 集合）

`916 passed in 231.21s`（`tests/test_display_catalog_cache.py tests/test_runtime_mode.py tests/test_node27_mvt_prewarm.py tests/test_api_contract.py tests/test_hydro_display_mvt_scaling.py tests/test_select_ci_tests.py`；本地另含 `test_two_node_docker_runtime.py`、`test_node27_write_roles.py`，见 PR body）。

## 2. 端点选择说明

- `/api/v1/layers` 在本实例恒 500：master 代码查询 `rnv.geometry_generation`（#2031），而生产 DB 尚未施加该 migration（#2145 跟踪；uvicorn 日志 `psycopg2.errors.UndefinedColumn`，全程仅 1 次 500 即该探测请求）。
- 500 不入缓存，故不能作 warm/cold 判别；改用同样走 `display_catalog_cached` 的 `/api/v1/runs` 与 `/api/v1/layers/discharge/cycles?source=gfs`。
- 这一点同时是 #2145 的实证：活动树若在未施加 migration 的情况下拉到 master 并重启，`/api/v1/layers` 会整体 500。

## 3. 单请求 TTFB（各 3 次预热后 6 样本，ms）

| 端点 | 不带头 | 外部 `x-nhms-cache-warm: refresh` | 错 token | 正确 token |
|---|---|---|---|---|
| `/api/v1/runs` | 3.3–3.6 | 3.2–4.5 | 3.2–3.3 | 76.3–80.1 |
| `/api/v1/layers/discharge/cycles?source=gfs` | 2.8–3.0 | 2.8–2.9 | 2.7–2.9 | 45.7–48.4 |

(a) 外部 `refresh` 与错 token 均与不带头同量级（≤ 4.5 ms，对照实测 receipt 冷值 73–92 ms）；(b) 正确 token 回到冷路径量级：`/api/v1/runs` 76.3–80.1 ms 满足 fixture 4.2 的 ≥ 50 ms；`/api/v1/layers/discharge/cycles?source=gfs` 无实测冷基线，45.7–48.4 ms 按其自身 warm/cold 约 16× 分离判定为冷路径（PR #2168 偏离记录 11）。

## 4. 20 并发（`/api/v1/runs`）

| 组 | 本实例（`application_name=nhms-2079-receipt`）PG 并发活动查询峰值 | 20 个请求 TTFB（ms） |
|---|---|---|
| 外部 `refresh` | **0** | 16.0 – 21.0，全部 200 |
| 正确 token | 20 | 274.0 – 310.6 |

对照实施前实测：生产 :8080 带 `refresh` 20 并发时同角色峰值 8；修复后带 `refresh` 为 0，冷路径只对持 token 者开放。

## 5. `/api/v1/runtime/config` 不泄露

响应体中 token 出现次数：0。

## 6. 进程内预热存活（token 无关）

单次普通请求（15:56:11Z）后静置 110 s，轮询本实例 `pg_stat_activity.query_start`：出现 `15:56:40Z`、`15:57:25Z`（两簇相隔 45 s，与 `DISPLAY_CATALOG_WARM_INTERVAL_SECONDS` 一致；`15:55:57Z` 为预热前的探测请求）。进程内预热线程在真实 uvicorn `--workers 2` 下仍按 45 s 回放热 key，ASGI scope 标记在生产同形的中间件栈下有效。

## 7. 结论

四条断言全部成立：外部 `refresh`/错 token 命中缓存；正确 token 走冷路径；进程内预热存活；runtime/config 不含 token。生产部署（tasks 4.3）随 #2162 / #2145 维护窗口执行，见 #2162 评论。
