# issue #2032 · MVT 文件缓存回收 + 锁生命周期 · node-27 merge 前 receipt（tasks 5.2）

取数时间：2026-09-08T13:33Z–13:37Z（UTC）。节点：node-27（`/home/nwm/NWM`）。
被测 head：`ebb9f13840872af4a3f4fab85ec9f144d3d5e712`（PR #2151，round-1 修复后）；对照 `origin/master` = `d1c628b4`。
方法：一次性 worktree `/home/nwm/tmp/wt-2032-pr`（PR head）与 `/home/nwm/tmp/wt-2032-master`（master），各自 `uv sync --all-extras --dev`（Python 3.11.15），
`TMPDIR=/home/nwm/tmp`；活动树与生产 display API（:8080）全程未动，结束后两个 worktree 已 `git worktree remove`。
同一脚本在修复前 head `05a75297` 上跑过一次（13 页 receipt 略），除 pytest 计数（799 → 810，新增 round-1 用例）外各项结果与本节逐字一致。
测量前提与裁定见 [2026-09-08-issue-2032-mvt-cache-measurement-node27.md](2026-09-08-issue-2032-mvt-cache-measurement-node27.md)。

## 1. Linux 端 pytest（PR worktree）

```
.venv/bin/python -m pytest tests/test_node27_mvt_cache_retention.py tests/test_mvt_tile_generation_lock.py \
  tests/test_hydro_display_mvt_scaling.py tests/test_node27_raw_retention.py tests/test_select_ci_tests.py -q
810 passed in 218.44s
```

本地 macOS 上唯一 skip 的用例（wrapper 的真实 `flock(1)` 争锁）在这里真实执行；`/proc/self/fd` 计数、`ELOOP` 分支同样走的是 Linux 路径。

## 2. plan-only 对真实缓存目录（`/home/nwm/.cache/nhms/mvt`，生产 display 进程同一根）

| 口径 | rc | execution_mode | planned | 按 kind | `precip/` 路径出现次数 | planned mtime max |
|---|---|---|---|---|---|---|
| 生产口径 14 天（cutoff 2026-08-25T13:37:01Z） | 0 | plan_only | 0 | — | 0 | — |
| `--retention-days 3`（cutoff 2026-09-05T13:37:01Z） | 0 | plan_only | 5067 | lock 2535 · pbf 2532 | 0 | 2026-09-05T07:14:46Z |

- 14 天 planned 0 是**预期**：测量 receipt 第 2 节的 mtime 直方图最早是 09-04（缓存根 09-04 才建），最早文件要到 09-18 才老化。
- 3 天口径选中的 2532 张 `.pbf` = 直方图 09-04（2165）+ 09-05 cutoff 前（367），逐张对上；2535 个 `.lock` 比 `.pbf` 多 3，是 09-04 那批无对应 `.pbf` 的遗留锁（424/异常路径留下的）。
- 两次运行 `counts.failed == 0`、`skipped == []`，`precip_root_untouched` = `/home/nwm/.cache/nhms/mvt/precip`；`planned[]/deleted[]/skipped[]/failed[]` 中 `precip/` 路径 0 条。
- 运行后实测缓存目录仍是 4380 `.pbf` / 4383 锁文件（plan-only 零删除）。

## 3. 隔离实例（PR head，:8090，`--workers 2`，空缓存目录 `/home/nwm/tmp/mvt-2032-pr-cache`，env 同 `infra/env/display.env`）

| 步骤 | 结果 | `.locks/**` 文件数 | `.pbf` 数 |
|---|---|---|---|
| 单次冷 miss（gfs 2026-09-07T12Z / q_down / 2026-09-08T00Z / z4/12/6） | 200 · 1 374 286 B · 2.13 s · `X-Tile-Cache: miss` | 0 | 1 |
| 424（gfs 2026-09-07T03Z，未发布周期） | 424 · 259 B · 0.061 s | 0 | 1 |
| 同一瓦片两并发冷 miss（ifs，`xargs -P2`） | req1 200 · 1.87 s · `hit`；req2 200 · 1.84 s · `miss`；两体 md5 相同（1 distinct） | 0 | 2 |
| uvicorn 日志 `tile generation lock` warning 数 | 0 | | |

- 并发一对里 `miss`/`hit` 各一：第二个请求在锁内重读缓存命中第一个的字节——跨进程 single-flight 语义不变，且两个 worker 进程争锁后 `.locks/**` 为 0（锁文件随 miss 结束 unlink）。
- 对照：master worktree 实例（:8091，同样 3 张冷 miss）结束后 `.locks/**` = 3、`.pbf` = 3——每个 miss 留一个永久锁文件，正是 issue 描述的增长源。

## 4. 双实例同刻对照（issue 验收「新旧两条全国流量路由仍 200 且字节不变」）

| 路由 | master :8091 | PR :8090 | ETag 相等 |
|---|---|---|---|
| `hydro-national/gfs/{cycle}/q_down/{vt}/4/12/6.pbf` | 200 · 1 374 286 B · `W/"m16-c845…f882"` (miss) | 200 · 1 374 286 B · `W/"m16-c845…f882"` (hit，第 3 节已生成) | 是 |
| `hydro-national/ifs/{cycle}/q_down/{vt}/4/12/6.pbf` | 200 · 1 374 055 B · `W/"m16-ec17…c411"` (miss) | 200 · 1 374 055 B · `W/"m16-ec17…c411"` (hit) | 是 |
| 旧 5 段 `hydro-national/q_down/{vt}/4/12/6.pbf` | 200 · 1 374 055 B · `W/"m16-ec17…c411"` (miss) | 200 · 1 374 055 B · `W/"m16-ec17…c411"` (miss) | 是 |

ETag 即 body sha256（`W/"m16-<sha256>"`），三对 HTTP 码与 ETag 逐一相等，字节数作为次要记录亦相等。

## 5. 卷与清理

- `df -h`：`/` 98G 78%（22G 可用）、`/home` 1.7T 84%（261G 可用），与测量 receipt 同量级；`/home/nwm/tmp` 下的隔离缓存目录与两个 worktree 已删除，`git worktree list` 回到运行前的 5 条（其余 4 条是节点上早已存在的他人 worktree，本次未动）。
- 生产 display API（:8080）与活动树 checkout（`5a86841c`，落后 master）全程未重启、未 pull；merge 后部署（tasks 5.3）另记。
