# MVT 瓦片文件缓存回收 + 瓦片生成锁生命周期（issue #2032）

## Why

`NHMS_MVT_FILE_CACHE_DIR` 下的 MVT 瓦片文件缓存与 `.locks/**` 锁文件从 M16 起只写不删：node-27 实测 5 天 4380 张 `.pbf` / 757 MB、4383 个锁文件，增长由 autopipe 每 tick 的 prewarm 主动推动（≈ 876 张 / 151 MB 每天），且 #2007 把全国流量瓦片的 key 空间乘上了 `(source, cycle)` 身份轴。DB 侧 `map.tile_cache` 为 0 行、display 角色只有 SELECT，所以生产增长全部落在文件侧。实测见 `docs/runbooks/receipts/2026-09-08-issue-2032-mvt-cache-measurement-node27.md`。

## What Changes

- 新增 `scripts/node27_mvt_cache_retention.py`（仅 stdlib，不连 DB）：按**墙钟 mtime** 剪 `<root>/<hh>/<sha256>.pbf`、`<root>/.locks/<hh>/<sha256>.lock` 与 `<root>/<hh>/.<sha256>.pbf.<pid>.tmp` 三种精确形状，绝不进入 `<root>/precip/**` 或任何其它子树；`NODE27_MVT_CACHE_RETENTION_ENABLED` / `_PLAN_ONLY` 门、`--summary-path` JSON 回执、`--retention-days` / `--reference-time` 覆盖；rc 0/1/2 与兄弟 `node27_raw_retention.py` 同语义。
- 新增 wrapper `scripts/node27_mvt_cache_retention_once.sh`、user 级 systemd `infra/systemd/nhms-node27-mvt-cache-retention.{service,timer}`、env 模板 `infra/env/node27-mvt-cache-retention.example`。
- `services/tiles/mvt.py::tile_generation_lock`：锁文件在释放前 unlink（先 unlink 后 `LOCK_UN`），获取时以 `(st_dev, st_ino)` 复核 flock 到的 inode 仍是路径上的 inode，不是则重开（有界重试，耗尽则记 warning 后无锁生成，绝不挂起）。进程内 `_LOCAL_TILE_LOCKS` 已是 `weakref.WeakValueDictionary`（条目随最后一个持有者/等待者释放而消失，有界于在途 miss），**不改**，只用测试钉住这一既有事实。跨进程 single-flight 语义保持。
- 测试：`tests/test_node27_mvt_cache_retention.py`（形状/排除/门/回执/rc/unit 文件）、`tests/test_mvt_tile_generation_lock.py`（跨进程 spawn 争锁、释放后锁文件不存在、无重叠生成、进程内字典有界）；`scripts/select_ci_tests.py` 路由覆盖新文件。
- 文档：`docs/runbooks/display-readonly-live-mvt.md` 增加缓存回收小节；实测 receipt 已随本 change 提交。
- **不做**（记录理由，见 design.md）：DB 侧回收、key→身份 sidecar、424 负缓存、`cycle` 入口收窄（另立 issue）。

## Capabilities

### New Capabilities

- `mvt-tile-cache-lifecycle`: MVT 瓦片文件缓存（`.pbf` / `.tmp` / `.lock`）的有界生命周期——回收脚本的目标形状、排除树、门与回执，以及瓦片生成锁的自清理与 inode 复核契约。

### Modified Capabilities

（无：`mvt-tile-contract` 的路由/缓存 key/ETag 需求不变；`canonical-precip-copyback` 的 precip 剪枝需求不变，本 change 只在其旁边加一条互不进入的回收路径。）

## Impact

- 代码：`services/tiles/mvt.py`（`tile_generation_lock`、`_LOCAL_TILE_LOCKS`）、新脚本/wrapper/unit/env 模板、两个新测试文件、`scripts/select_ci_tests.py`（若路由表需要显式条目）。
- 受影响面：五个瓦片图层（`hydro`、`hydro-national` 新旧两条、`river-network`、`river-network-national`、`met-stations`）共用 `tile_generation_lock`；`scripts/node27_raw_retention.py` 的 precip lane 与本回收互为排除（各自只碰自己的子树）。
- 部署：node-27 需安装 env（0600）+ unit + timer，并重启 `nhms-display-api` 让锁改动生效（merge 后部署步骤，见 tasks.md 第 5 节）。`yd-display-api` 用独立缓存目录，不受影响。
- 不改 DB、不改 OpenAPI、不改前端、不改 node-22。
