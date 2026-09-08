# Design: mvt-tile-cache-lifecycle-retention

## Context

- 实测 oracle：`docs/runbooks/receipts/2026-09-08-issue-2032-mvt-cache-measurement-node27.md`。要点：`map.tile_cache` 0 行（display 角色仅 SELECT）；文件缓存 5 天 4380 `.pbf` / 757 MB，锁文件 4383 与 `.pbf` 逐日一一对应；压缩 chunk 内周期的冷 miss（1.6–2.7 s）与最新周期（1.8–2.9 s）同带宽；424 路径 40 ms。
- 现状代码：`services/tiles/mvt.py::_file_cache_path`（`<root>/<hh>/<sha>.pbf`）、`_file_cache_lock_path`（`<root>/.locks/<hh>/<sha>.lock`）、`_write_file_cache`（tmp `.<sha>.pbf.<pid>.tmp` + `os.replace`）、`tile_generation_lock`（进程内 `threading.Lock` 字典 + `flock`，`finally` 只 `LOCK_UN` 不 unlink）；`_safe_read_file_cache` 把 `OSError` 吞成 miss。
- 兄弟：`scripts/node27_raw_retention.py`（#2011）只进 `<root>/precip/`，锚定 `display_watermark − retention_days`，处于 #2100 的 rc=1 稳态；其测试 `test_cache_root_siblings_outside_precip_are_never_targets` 是本 change 排除关系的镜像。
- 五个瓦片图层共用 `tile_generation_lock` 与文件缓存；`yd-display-api` 用独立缓存目录。

## Goals / Non-Goals

**Goals**
- 文件缓存三种形状按墙钟 mtime 有界；`.locks/**` 在稳态只含在途 miss；回收路径可重复执行、带回执、有 plan-only 门。
- 跨进程 single-flight 不退化：锁文件自清理后，两个 worker 对同一 key 仍串行，且不会各自生成。

**Non-Goals**（每条带理由）
- DB 侧 `map.tile_cache` 回收：0 行且 display 角色无写权限，剪不到增长侧。
- key→身份 sidecar：引入必须与缓存一致的新状态，A 方案已足够。
- 424 负缓存：实测 40 ms/次；TTL 负缓存会让刚发布的周期在 TTL 内继续 424，收益小于新失效模式。
- `cycle` 入口收窄到已发布周期：实测未列出周期（08-25T00）返回 200 的**部分河网**瓦片，这是「未列周期是否可直连渲染」的语义决策而非成本项（冷 miss 空间的主轴是 z/x/y × valid_time）；由 issue-scribe 另立 issue，本 PR 不动路由。
- `precip/**` 剪枝（#2011 owns）、canonical/raw retention 语义、瓦片 SQL 性能、`#2030`、display 角色写权限。
- 缓存命中刷新 mtime（LRU）：不需要，mtime = 该代生成时间即足够；且读路径不做写。

## Decisions

### D1. 独立脚本 + 独立 unit，不做 raw retention 的第四条 lane
- 理由：(a) 本回收**不连 DB**，raw retention 的唯一 DB 触点是 watermark 读取；(b) 锚不同——墙钟 mtime 而非 `display_watermark`，因为剪掉的瓦片下次请求从 DB 再生成（miss），不是 404，所以 #2011 的 `L ≤ R − 1` 下限**不适用**；(c) raw retention 单元因 #2100 处于永久 rc=1，耦合会继承该噪声；(d) `RetentionTarget`/`_safe_target` 的两级目录模型与文件级剪枝不匹配。
- 替代（第四 lane）否决于上述四点；issue 允许两种宿主，此处记录选择理由。
- 不 import `scripts.node27_raw_retention`（会拖入 psycopg/watermark 依赖）；`_resolve_lane_root` 的 OSError 包裹模式以注释引用后同型实现。

### D2. 目标形状白名单 + 两级固定深度，排除靠构造而非靠黑名单
- 只枚举 `<root>/<hh>/` 与 `<root>/.locks/<hh>/`（`hh` 必须 `[0-9a-f]{2}` 且非 symlink 目录），文件名按三条正则精确匹配，`lstat` 必须是常规文件。`precip/` 不匹配 `[0-9a-f]{2}`，天然不被枚举；回执仍显式写 `precip_root_untouched` 供 AC 断言。
- `.tmp` 中间文件纳入：`_write_file_cache` 在崩溃时会留下 `.<sha>.pbf.<pid>.tmp`，与「只写不删」同类，形状精确、零成本纳入；同一 cutoff。
- 空的 `<hh>` 目录保留（固定 256 + 256，有界）。

### D3. 锁文件删除的并发安全：runner 侧 `LOCK_NB`，API 侧 unlink-before-release + inode 复核
- runner：对 `.lock` 用 `os.open(path, O_RDONLY | O_NOFOLLOW | O_CLOEXEC | O_NONBLOCK)`（**无 `O_CREAT`**——删除器绝不能重建文件；`O_NONBLOCK` 让换进来的无写端 FIFO 不能把 runner 挂死——unit 是 `TimeoutStartSec=0`、wrapper 是 `flock -n`，一次挂死等于此后每个 tick 都 exit 0 跳过；`ENOENT` 即 `already_gone`，`ELOOP`/`EMLINK` 即 `not_regular_file`）取 fd，**先 `fstat` 判 `S_ISREG`**（非常规 → `not_regular_file`，目录/FIFO 在任何 flock 之前就被归类），再 `flock(LOCK_EX|LOCK_NB)`，拿不到记 `lock_held` 跳过；拿到后**再比一次身份**——`os.fstat(fd)` 与 `os.lstat(path)` 的 `(st_dev, st_ino)` 不等或 `ENOENT` 即 `already_gone`、不 unlink（窗口：runner 打开旧 inode I 后，持有者完成并 unlink，新 miss 重建为 inode J 并进入生成；runner 对孤儿 I 的 `LOCK_NB` 会成功，若此时按路径 unlink 会删掉活锁 J）；身份相等才 unlink，然后释放。这使「删除正被持有的锁」不可能发生。`.pbf`/`.tmp` 直接 `os.unlink`，不打开。`.locks` lane 级跳过用兄弟 `_resolve_lane_root` 的词汇：缺失 → `locks_root_missing`（新建缓存根的正常状态，不是错误），symlink/非目录 → `locks_root_unsafe` + `detail: path_is_symlink|path_not_directory|path_unavailable`。**枚举失败不是空 lane**：根、`.locks`、任一 `<hh>` 的 `scandir` 抛 `OSError`（EACCES/ESTALE）→ `failed[]` 一条 `{path, kind: null, reason: enumeration_unavailable, error, error_type}`、其余目录照常、rc 1——否则不可读的根会产出 `completed/production_execute/failed []` 的绿回执而缓存无限增长（round-1 verified cand-01）；单个条目在 `scandir` 与 `stat` 之间消失是并发 miss 的正常竞态，静默跳过。
- API：`tile_generation_lock` 在 `finally` 里**先 unlink 后 `LOCK_UN`**。顺序是硬约束：若先释放后 unlink，等待者可能在释放后取得锁、复核通过（路径仍指向同一 inode）、然后路径被持有者 unlink——等待者持有一个已 unlink 的 inode，下一个到来者创建新 inode 并同时生成。
- 获取：`open("a+b")` → `flock(LOCK_EX)` → `fstat(fd)` 与 `stat(path)` 比 `(st_dev, st_ino)`；不等或 `ENOENT` 即关闭重开。`_TILE_LOCK_REACQUIRE_LIMIT = 8` 定义为**尝试总数**（每次尝试 = open + flock + 比较；`_lock_path_identity` 调用次数 == 上限），耗尽则 `logger.warning` 后无锁执行——退化为重复生成（`os.replace` 原子，字节幂等），绝不挂起请求；每条失败分支都关闭 fd，比较阶段抛出的任何异常（如探针的 `PermissionError`）也先关 fd（同时释放 flock）再传播，且**放弃时不 unlink 路径**（它可能已是他人的活锁），交给 runner 按 mtime 兜底。
- 进程内 `_LOCAL_TILE_LOCKS` **不改**：它已是 `weakref.WeakValueDictionary[str, threading.Lock]`（`services/tiles/mvt.py::_LOCAL_TILE_LOCKS`），条目在最后一个持有者/等待者的强引用消失时自动移除，因此「有界于在途 miss」今天已成立（fixture review 实测：正常返回与异常返回后 `key in _LOCAL_TILE_LOCKS` 均为 `False`）。本 change 只用测试钉住该事实；唯一已知的非确定性是异常 traceback 仍钉住 generator frame 期间条目会多活一会儿（`pytest.raises` 作用域内），测试断言必须在该作用域之外做。
- inode 复核的 stat 调用经模块级探针 `_lock_path_identity(path) -> tuple[int, int] | None`（`ENOENT` → `None`）与 `_lock_file_identity(fd) -> tuple[int, int]` 进行，测试只 monkeypatch 前者，不打全局 `os.stat`。
- runner 与等待者的交互：runner 用 `LOCK_NB` 只删**无人持有**的锁；一个正阻塞在该 inode 上的等待者不持锁，因此 runner 可能删掉它阻塞的文件——等待者随后取得的是已 unlink 的 inode，靠 inode 复核转到新文件。且 14 天口径下在途锁文件不可能早于 cutoff，该交错只在遗留锁文件被旧代码复用时理论可达。
- 混合版本窗口（部署重启前旧代码 worker 与新代码 worker 并存）：旧代码等待者可能持有已 unlink 的 inode 而不复核 → 与新到来者重复生成一次，原子替换、无损；记为 known limit，随重启消失。

### D4. 回执与门沿用兄弟约定
- `NODE27_MVT_CACHE_RETENTION_ENABLED`（默认 true）/ `NODE27_MVT_CACHE_RETENTION_PLAN_ONLY`（默认 false）——不用 `DRY_RUN` 名（#1407 理由：历史 `*_DRY_RUN=true` 残留会静默归零删除）。
- `--summary-path` JSON（未给时打印到 stdout，与兄弟一致；runner **不读** `NODE27_MVT_CACHE_RETENTION_SUMMARY_PATH`——wrapper 保留该 env override 并总是传 flag——相对路径按 cwd 解析、不设 blocker，blocker 集合与 spec Req 2 一致）、`execution_mode` 三值、rc 0/1/2、`already_gone` 归 skipped；`skipped[]` 条目统一 `{path, kind, reason}`（`kind` 为 `pbf|tmp|lock`，lane 级跳过为 `null`），`mtime` 为 RFC3339 UTC 秒精度字符串；回执另带 `precip_root_untouched`。
- **与兄弟的显式偏离**：不复用 `node27_raw_retention._env_int` 的「非法值静默回落默认」——`NODE27_MVT_CACHE_RETENTION_DAYS` / `--retention-days` 解析失败或 `< 1` 一律 `preflight_blocked` rc 2（spec 要求）；`--retention-days 0` 不得因 `or` 短路落回 env 默认。根路径按显示进程口径 `Path(root).expanduser()`（**不 resolve**，枚举也用未 resolve 的路径），且必须绝对、不得为 `/`、`is_symlink()`（lstat）为假、真实目录；symlink 检查必须在任何 `resolve()` 之前——兄弟 `_safe_resolved_dir` 先 `resolve(strict=True)` 再 `is_symlink()` 的分支是死代码（resolve 后永不为 symlink），不得照抄。`--reference-time` 非 RFC3339 同样是 preflight blocker（`reason: not_rfc3339`），否则会在写回执前抛出，正是兄弟为 `OSError` 包裹所记录的「陈旧回执」失效模式。`<root>/.locks` 本身若为 symlink/非目录，只跳过锁 lane（lane 级 `skipped` 条目），因为 `os.scandir` 会跟随 symlink 而把删除面带出缓存根。blocked 回执与正常回执用同一 sink（文件或 stdout）。
- env 模板逐字复制「取显示进程实际值，不是 `display.example` 的 `/tmp/...`」警告。

### D5. 部署顺序（merge 后，独立 receipt）
1. node-27 活动树 `git pull --ff-only`；2. 安装 env（0600）+ unit + timer；3. 首跑 `PLAN_ONLY=true` 看 planned 集合（生产口径 14 天在 2026-09-18 前应为 0 planned，用 `--retention-days 3` 再跑一次 plan-only 证明 09-04 批次可选中、零 precip 路径）；4. `bash scripts/ops/start-display-api.sh` 重启让锁改动生效；5. 观察 `.locks/**` 计数在下一次 prewarm 后不再增长。回滚：`systemctl --user disable --now` timer，或 `ENABLED=false`。
- merge 前的 node-27 receipt 用一次性 worktree + 隔离 uvicorn（**`--workers 2`**，与生产 `NHMS_DISPLAY_WORKERS` 默认一致——单 worker 只走进程内 `threading.Lock`，碰不到 flock / inode 复核这条本 change 的全部生产触发面），不 pull 活动树、不重启生产实例（活动树落后 master 142 个提交，从 PR 分支重启等于部署无关改动）。「新旧两条全国流量路由仍 200 且字节不变」用**双实例同刻对照**证明：同一 DB 上并排起 master worktree（:8091）与 PR worktree（:8090），各自空缓存，对同一组 URL（新路由 gfs/ifs 与旧 5 段路由各一张 z4）冷打一次，比对 HTTP 码与 `ETag`（`W/"m16-<sha256(body)>"`，等价 `X-Tile-Checksum`）——字节数只作次要记录，两张等长不同的瓦片会骗过长度比对；旧路由跟随各河网最新 run，跨时间比对不稳定，同刻对照才是正确 oracle。

## Risks / Trade-offs

- [runner 与 API 同时操作同一 `.pbf`] → API 只写新文件（fresh mtime）、读旧文件；读侧 `_safe_read_file_cache` 把并发 unlink 引起的 `OSError` 当 miss，回退到生成。
- [14 天口径下遗留锁文件要到 09-18 才开始老化] → 遗留 4383 个 inode 只占 3% inode 的 0.004%，不是事故；receipt 记老化日期。
- [重试耗尽走无锁分支] → 无需外力：持有者每次交接（unlink + `LOCK_UN`）让阻塞在该 inode 上的每个等待者花掉一次尝试，第 k 个同 key 跨进程等待者约需 k 次，所以 ≥ `_TILE_LOCK_REACQUIRE_LIMIT + 1` = 9 个同 key 跨进程竞争者即可触顶——与 `NHMS_DISPLAY_WORKERS` 耦合（`start-display-api.sh` 上限 4，但 `nhms-display-api.service` 直接透传 env 不设上限）。后果是重复生成一次，不是事故；调大 worker 数时要记得这条。
- [prewarm 并发（线程池）× 2 worker] → 正是 flock 路径的生产触发面；跨进程测试用 `multiprocessing` spawn 覆盖。

## Invariant Matrix

- Governing invariant: 显示 API 在 `NHMS_MVT_FILE_CACHE_DIR` 下、`precip/` 之外创建的每个文件都有有界寿命——锁文件只在一次在途 miss 持有期间存在，`.pbf`/`.tmp` 在 mtime 早于 cutoff 后被且仅被 MVT 回收 runner 删除，runner 只触碰三种精确形状、绝不进入 `precip/**`；跨进程 single-flight 在锁自清理后仍成立。
- Source-of-truth identity/contract: 路径形状来自 `_file_cache_path` / `_file_cache_lock_path` / `_write_file_cache`（`<hh>` = sha256 前两位，文件名 = 完整 sha256）；锁活性以 `(st_dev, st_ino)` 判定；年龄以 `lstat().st_mtime` 对墙钟 cutoff 判定。
- Producers: `services/tiles/mvt.py::_write_file_cache`（`.pbf` + `.tmp`）、`tile_generation_lock`（`.lock`）；`services/precip/cache.py`（`precip/**`，兄弟，不动）。
- Validators/preflight: runner 的根校验（`expanduser` 后绝对、非 `/`、存在、非 symlink、目录；days 严格整数 ≥ 1，无静默回落）、三条正则、`lstat` 常规文件、`hh` 目录非 symlink、`.lock` 以 `O_RDONLY|O_NOFOLLOW|O_CLOEXEC|O_NONBLOCK` 打开且在 flock 前先 `fstat` 判 `S_ISREG`（非常规 → `not_regular_file`）；API 侧 inode 复核（`_lock_path_identity` / `_lock_file_identity`）。
- Storage/cache/query: `_read_file_cache` / `_safe_read_file_cache`（并发 unlink → `OSError` → miss）、`read_cached_tile_response`。
- Public routes/entrypoints: `apps/api/routes/hydro_display.py::_cached_or_generated_mvt_response` 服务的五条路由（`hydro`、`hydro-national` 旧 5 段与新 `{source}/{cycle}`、`river-network`、`river-network-national`、`met-stations`）；新 CLI `scripts/node27_mvt_cache_retention.py` 与 wrapper。
- Frontend/downstream consumers: `scripts/node27_mvt_prewarm.py`（只走 HTTP，不感知锁/文件）；前端（无变化）；`scripts/node27_raw_retention.py` precip lane（同根不同子树）。
- Failure paths/rollback/stale state: producer 在锁内抛 424/413/500 → 锁仍 unlink；重试耗尽 → warning + 无锁；runner unlink 遇 `ENOENT` → `already_gone`；锁被持有 → `lock_held`；路径被换成 symlink/目录/FIFO → `not_regular_file`；根/`.locks`/`<hh>` 枚举失败 → `failed[enumeration_unavailable]` rc 1（plan-only 同样）；探针在 flock 后抛非 ENOENT 的 `OSError` → 先关 fd 再传播；根不安全 → `preflight_blocked` rc=2、零删除；`ENABLED=false` → disabled 零删除。
- Evidence/audit/readiness: `--summary-path` JSON；unit 文件测试；node-27 receipt（plan-only 两个口径 + 隔离 uvicorn 的锁计数）。
- Regression rows:
  - runner + 混合树（三种 aged 形状、fresh `.pbf`、aged `precip/**`、非 hex 目录、非 sha 文件名、symlink、目录冒充 `.pbf`）→ 只删三种 aged 形状，其余全部存在，回执无 `precip/` 路径。
  - runner + `PLAN_ONLY=true` → planned 同集合、deleted 空、文件全在、rc 0。
  - runner + 被另一进程 flock 的 aged `.lock` → `skipped[lock_held]`、文件仍在、其它目标照删。
  - runner + 根缺失/symlink/非目录/days=0 → `preflight_blocked` rc 2、零删除。
  - runner + 计划后消失的目标 → `skipped[already_gone]`、rc 0。
  - `tile_generation_lock` + producer 正常返回 / 抛异常 → 锁文件不存在（新行为，改前红）；`_LOCAL_TILE_LOCKS` 在 `pytest.raises` 作用域外无该 key（既有 WeakValueDictionary 行为，钉住不改）。
  - 新旧两条 hydro-national 路由在 master 与 PR 双实例同刻对照 → 同 HTTP 码、同 `ETag`（字节数次要）。
  - 两进程争同一 key（spawn）→ 后者在前者结束后才进入、无重叠、结束后 `.locks/**` 为空。
  - inode 复核：等待者阻塞的 inode 被持有者 unlink → 等待者在新 inode 上重新获取（不是在已 unlink 的 inode 上执行）。
  - 重试耗尽（monkeypatch stat 恒不等）→ 有界退出、warning、块仍执行。
  - （round-1）runner + 不可读 `<hh>` / 不可读根 → `failed[enumeration_unavailable]`、同级照删、rc 1（plan-only 亦 rc 1）。
  - （round-1）runner + collect 后锁路径 unlink / 换成 symlink / 目录 / 无写端 FIFO → `already_gone` / `not_regular_file`×3，替换物仍在，FIFO 用例即时返回。
  - （round-1）`tile_generation_lock` + `fcntl.flock` spy → `LOCK_UN` 时锁路径已不存在（交换顺序即红）。
  - （round-1）探针抛 `PermissionError` → 异常传播、fd 计数不变、路径可被新 `flock(LOCK_NB)` 取到。
  - （round-1）`st_mtime == cutoff` 样本存活（`>=`→`>` 变异即红）；相对 `--summary-path` 可写、env 变量不改 sink。
  - unchanged sibling：`tests/test_hydro_display_mvt_scaling.py` 全部通过（五图层缓存 key/single-flight 再读语义不变）；`tests/test_node27_raw_retention.py` 全部通过；两个 runner 在同一根上互不列出对方路径。

## Boundary-surface checklist

- Shared helper roots: `services/tiles/mvt.py::tile_generation_lock`（改）、新探针 `_lock_path_identity` / `_lock_file_identity`、`_LOCAL_TILE_LOCKS`（不改，钉住）；`_file_cache_path`/`_file_cache_lock_path`/`_write_file_cache`/`_safe_read_file_cache`（不改，形状被 runner 依赖——若日后改形状必须同步 runner 正则，任务里加一条把三条形状常量与 mvt.py 的路径构造用同一测试钉住）。
- Public entrypoints: 新 CLI + wrapper + unit/timer；五条瓦片路由行为不变。
- Read surfaces: runner 的 `os.scandir`/`lstat`（只两级）；API 读缓存。
- Write/delete/overwrite surfaces: runner `os.unlink`（三形状）；API unlink 自己的锁文件。
- Staging/publish/rollback: 无（回滚 = 禁用 timer / `ENABLED=false`）。
- Producer/consumer evidence boundaries: summary JSON ↔ receipt ↔ jq 判据（env 模板给出）。
- Stale-state/idempotency boundaries: `already_gone`；重复 tick 幂等；混合版本窗口的一次性重复生成。
- Unchanged downstream consumers: prewarm、前端、raw retention precip lane、`yd-display-api`（独立目录）。

## Migration Plan

见 D5。无 schema、无 OpenAPI、无前端改动；回滚只需停 timer 并回退 `mvt.py`（旧代码对已被 unlink 的锁文件无感知：`open("a+b")` 会重新创建）。

## Open Questions

无——方案由实测裁定，剩余语义问题（未列周期是否可直连渲染）已路由为独立 issue。
