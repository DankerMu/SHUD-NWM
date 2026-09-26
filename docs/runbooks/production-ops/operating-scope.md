**分册：当前运行口径（业务集变更记录）**

本页是当前生产值守手册的 §7 分册（#1103 拆分，正文逐字保留）。
索引与全部分册入口见 [`../current-production-ops.md`](../current-production-ops.md)。

## 7. 当前运行口径

This section is a live snapshot, not a permanent fact. Refresh it during handoff.

2026-06-22 verification found:

- node-27 `node27_autopipe` cron active every 10 minutes.
- Recent `/home/nwm/autopipe-logs/autopipe.log` runs discovered 300 runs, ingested 4 new runs,
  published 4, and refreshed 4 display coverage rows.
- node-27 display API listens on `127.0.0.1:8080`; local and public `/health`
  both returned `ok` after port alignment.
- node-22 Slurm Gateway process is active; node-22 diagnostic API `/health` on
  `:8001` returned `ok`.

### 7.1 2026-08-07：hhe 退出业务化（当时业务集 17 流域）

`basins_hhe`（全国级网格，43799 river segments）SHUD 参数待进一步校正
（单次 forecast 积分远超常规流域，见 #1295；gfs_2026072112 修复线在
forecast 运行超 2 小时后由操作员决定取消），暂时退出业务化。**退役当时**的生产业务集是 17 流域 × gfs/ifs 双源（34 行）——
这是 2026-08-07 的历史快照，不是当前值。当前业务集一律以
`jq '.models|length' manifest-last.json` 实测为准，口径见 §3.1 开头的 authority 段。

退役操作记录（均有备份，可逆）：

- node-22 scheduler registry：两份 `scheduler/registry/manifest-last.json`
  （NFS + `/scratch/frd_muziyao/nhms-prod` 本地）移除 hhe 的 2 条模型
  （`dg_edd58a2fe…`/gfs、`dg_2c13fd98…`/ifs，36→34），checksum 按
  canonical-JSON（排除 `checksum` 键、sort_keys 紧凑序列化的 sha256）
  重算；原件备份 `manifest-last.json.bak-hhe-retire-20260807`。
- node-27 DB：`hydro.hydro_run` 中 23 条 published 的
  `basins_hhe_vbasins` 行翻为 `superseded`（与 window-retirement 语义一致，
  ingest re-scan 会无条件跳过）；行级备份
  `/home/nwm/hhe-published-runs-backup-20260807.csv`。
- node-27 ingest：`infra/env/node27-ingest.env` 的
  `AUTOPIPE_EXCLUDE_BASINS=zhaochen_hhy,hhe`（备份同名 `.bak-hhe-retire-20260807`）。
- 展示验证：display API `/api/v1/basins?has_display_product=true` 返回
  17 个流域，不含 hhe。

复活路径：恢复 manifest 备份（或重新 provision registry）＋ 27 侧
`--force` 注册（其 register 步骤会把 `superseded` 翻回 active）＋
移除 `AUTOPIPE_EXCLUDE_BASINS` 中的 `hhe`＋把 baseline `core.model_instance`
行 `activate` 回来（见 §7.1.1，否则全国底图上没有这个流域的河网）。

#### 7.1.1 2026-08-25 补漏：baseline `core.model_instance` 必须一并 deactivate

上面 2026-08-07 的四步**漏了一步**，导致 hhe 退役 18 天后仍在公网底图上显示全部
43799 条河网：`core.model_instance` 的 baseline 行 `basins_hhe_shud` 一直是
`active_flag = t / lifecycle_state = active`（当时只处理了 registry 里的两条 `dg_*` 行）。
全国 river-network MVT 的成员判据就是这一条
（[`services/tiles/mvt.py`](../../../services/tiles/mvt.py) `postgis_tile_sql()` 里 river-network-national
的 `network_filter`，当前 `:764-765`：
`EXISTS (... model_instance mi WHERE mi.river_network_version_id = rnv.river_network_version_id AND mi.active_flag = true)`），
且 `national_river_network_source_version()`（同文件，当前 def `:1922`、方言谓词 `:1932`）的 digest 也只看 active 集合——
所以这条行不翻，tile 内容和 ETag 都不会变。

**退役流域时必须做的第 5 步**：把该流域的 baseline `basins_<slug>_shud` 行走
lifecycle 通道 deactivate。

- 通道：`POST /api/v1/models/{model_id}/lifecycle`，`operation=deactivate`，
  `override_missing_active=true`（退役后该 basin_version 为零 active，属合法终态，
  数据里已有先例），非空 `reason`，actor 角色需 `sys_admin`
  （[`packages/common/model_registry.py:2925`](../../../packages/common/model_registry.py)
  的 `MISSING_ACTIVE_RISK` / `OVERRIDE_REQUIRES_SYS_ADMIN` 前置判据）。
- node-27 的 `:8080` 是 display-readonly 部署（`nhms_display_ro` +
  `NHMS_DISPLAY_DISABLE_CONTROL_MUTATIONS=true`），**不要**为了这一次操作放开写权限。
  用仓库自身代码在 27 上以 owner DSN 进程内调用同一个 store 方法即可——
  route 只是 `store.model_lifecycle_operation(...)` 的薄封装：

  ```python
  decision = trusted_internal_policy_decision(
      "models.deactivate", target_type="model_instance",
      target_id=MODEL_ID, actor_id="ops:<who>-<date>", roles=("sys_admin",))
  store.preflight_model_operation(MODEL_ID, operation="deactivate",
      policy_decision=decision, override_missing_active=True, reason=REASON)
  # blockers 非空一律中止，不要改用 trusted_internal=True 硬闯
  store.model_lifecycle_operation(...)   # 同参数
  ```

  跑之前 `env -u NHMS_AUTH_MODE -u AUTH_BACKEND`，只 source
  `infra/env/node27-ingest.env`（`display.env` 里的 `NHMS_AUTH_MODE=production`
  会把 CLI 证据路径判成 `release_blocked`）。deactivate 不做任何继任者提升，
  manifest / state-index post-commit publisher 生产上未挂载（默认 no-op），无调度侧副作用。
- 不要动 `core.basin_version`（其 `active_flag` 由 importer 恒置 false，对计算面无权威、
  展示面只是「默认选中版本」的选择器，全 false 时是 no-op；
  权威归属见 [`docs/spec/03_database_design.md` §5.2 / §5.5 的 `active_flag` 注记](../../spec/03_database_design.md#52-corebasin_version)），
  也不要动已经 inactive 的 `dg_*` 行。一行一操作。

**验收 receipt（必须前后对照，只翻旗不算修好）**：

| 项 | 2026-08-25 实测 |
|---|---|
| `/api/v1/layers` river-network `source_generation` | `…:34c95f183d39f2504658:25` → `…:5cd1a080b67a0da5d6ca:24` |
| active `model_instance` / active river networks | 25 → 24，diff 只少 `basins_hhe_shud` 一行 |
| tile `river-network-national/5/25/12.pbf` | 65023 B → 10443 B |
| tile `…/6/50/24.pbf` | 14023 B → 467 B |
| tile `…/4/12/6.pbf` | 67700 B → 31865 B |
| 公网 `https://test.nwm.ac.cn` 同一 tile | 10443 B（与内网一致，nginx 无陈旧缓存） |
| `ops.audit_log` | `log_id=14`，`models.deactivate` / `sys_admin` / `basins_hhe_shud` |

tile 前后字节数不变 = 缓存问题，回去查 `source_version` 与 nginx，不要直接宣布完成。

**静态 geojson 的残留（2026-08-25 已清，#1701 一并处理）**：

两份 `apps/frontend/public/geo/*.geojson` 曾长期留着已退役流域的几何。它们确实
**不会被渲染**（`withStaticBasinBoundaries()` 只对**服务端已返回的** basin 按
basinId 查表回填，而 basinId 来自 `has_display_product=true`；river 那份前端
**刻意不 fetch**，见
[`useNationalBasinGeo.ts:37`](../../../apps/frontend/src/pages/m11/useNationalBasinGeo.ts)
并有测试钉住），但把退役流域的几何继续投递给浏览器没有道理，已清：

| 文件 | before | after |
|---|---|---|
| `national-basin-river.geojson` | 45.0 MB / 59702 features（43799 为 hhe，5294 为 zhaochen） | **8.85 MB / 10609** |
| `national-basin-domain.geojson` | 0.50 MB / 18 features | **0.34 MB / 14** |

清理只过滤 `properties.basin_id`，不重建；退役新流域时照做一次即可。

**`core.basin` 的退役行不要删**：`core.basin_version` 以 `NO ACTION` 外键引用
`core.basin.basin_id`，每个退役流域各有 1 行 `basin_version`，硬删要连
`basin_version` → `model_instance` → `hydro_run` 一起删，会毁掉血缘和上面的复活路径。
裸 `/api/v1/basins`（不带 `has_display_product`）就是 `core.basin` 原始目录，
含已退役流域**属预期**；前端走的是 `has_display_product=true`
（[`stores/overviewData.ts`](../../../apps/frontend/src/stores/overviewData.ts) 的 `fetchBasins`）。

### 7.2 2026-08-25：zhaochen 系列退出业务化（#1701，owner 裁定不建后继）

`basins_zhaochen_{bst,mc,wem}` 三个流域整体退出生产。**owner 裁定「彻底退出，不建
basins_hys_* 后继」**，所以 #1701 原计划的「换 id + 状态延续」整条路作废——没有目标
包，就没有克隆对，`#1697` 的 `--transfer-mode recalibration` 不参与本次操作。

地理位置与名字无关，别按名字找：`bst` 在新疆天山（83.0–88.3°E / 41.5–43.3°N，
**9572 河段**），`mc` 在四川（103.8–104.0°E / 28.8–29.0°N，708 段），
`wem` 在 102.0°E / 34.1°N（308 段）。验收挑 tile 时按这三个 bbox 挑，不要按名字猜。

**本次五步全做了**（§7.1 的 hhe 退役漏了第 5 步，见 §7.1.1）：

1. **注册表两份**（NFS + `/scratch/frd_muziyao/nhms-prod` 本地）各移除 6 条
   `dg_*`（3 流域 × gfs/ifs），62 → 56。checksum **复用仓库自己的
   `scheduler_file_provider_refresh._prospective_registry_content`** 重算，
   不要手写 canonical 序列化；写入后立刻用 `_load_previous_canonical_registry`
   原地回读校验（sha 相符 + 56 行）才算成功。备份
   `manifest-last.json.bak-zhaochen-retire-20260825`。
2. **node-27 `hydro.hydro_run`**：552 条 published（184 × 3）翻 `superseded`，
   行级备份 `/home/nwm/zhaochen-published-runs-backup-20260825.csv`。
3. **`infra/env/node27-ingest.env`**：`AUTOPIPE_EXCLUDE_BASINS` 追加
   `zhaochen_bst,zhaochen_mc,zhaochen_wem`（保留原有 `zhaochen_hhy,hhe`），
   备份同名 `.bak-zhaochen-retire-20260825`。
4. **目录**：`zhaochen/` 两棵树各自 `mv` 到
   `<root>/Basins-retired/issue-1701-20260825/`（各 4.2 G，同盘 rename），不删。
5. **baseline `core.model_instance` deactivate ×3**：`basins_zhaochen_{bst,mc,wem}_shud`
   走 §7.1.1 的进程内 lifecycle 通道，`override_missing_active=true`，一行一操作，
   preflight `blockers` 非空一律中止（本次三个都是 `[]`，唯一 warning
   `COPIED_ROOT_EVIDENCE_MISSING` 与 deactivate 无关）。

**两个把这次退役做返工的坑（§7.1 / §7.1.1 的模板里没有，务必照做）**：

1. **按 `basin_version_id` 查 `model_instance`，绝不要按 `model_id`。**
   direct-grid 变体行的 `model_id` 是哈希（`dg_e8ced3a5…`），**不含流域名**，
   `where model_id like '%<slug>%'` 会把它们全部漏掉，只查到 3 行 baseline
   `_shud`。本次实际有 **9 行**（3 个 `_shud` + 6 个 `dg_*`，其中 3 个 `dg_*`
   是 active）。只关 `_shud` 会出现一个骗人的中间态：`source_generation` 确实
   从 `:31` 掉到 `:28`、tile 确实归零——因为那一刻 `dg_*` 恰好也没在 active 集里——
   然后下一轮 autopipe 一跑，河网全回来了。正确查法：

   ```sql
   select model_id, basin_version_id, active_flag, lifecycle_state
     from core.model_instance
    where basin_version_id like '%<slug>%'
    order by active_flag desc, model_id;
   ```

   §7.1.1 那句「不要动已经 inactive 的 `dg_*` 行」只在 hhe 的情形下成立
   （hhe 的 dg 行本就全 inactive）；**active 的 `dg_*` 行必须一起 deactivate**。

2. **先加 `AUTOPIPE_EXCLUDE_BASINS`，再 deactivate——顺序反了会被翻回来。**
   `nhms-node27-autopipe.timer` 每 10 分钟一轮，
   [`node27_autopipe_cron.sh:19/109`](../../../scripts/node27_autopipe_cron.sh)
   每轮重新 source `infra/env/node27-ingest.env`，其 register 步骤会把
   `superseded` / `inactive` 翻回 active。本次 19:46:02 deactivate、19:48:47
   autopipe 就把 3 个 `dg_*` 重新激活了，而排除项 19:48:50 才落盘——**差 3 秒**。
   验收必须**跨至少一轮完整 autopipe** 再读数（本次 19:58:40 那轮跑完后
   zhaochen active = 0、active 总数 28，才算数）。

**不需要做的**（都核实过，别顺手做）：

- **不需要 retirement declaration**。`NHMS_SCHEDULER_REQUIRE_DIRECT_GRID=true` 让刷新走
  [`scripts/scheduler_refresh/runner.py`](../../../scripts/scheduler_refresh/runner.py)
  的 replay 分支，`previous_models_snapshot` 由
  `_load_previous_canonical_registry(registry_uri)` 直接读 manifest——手工删行之后
  previous 本身就是 56，分类器看到的是 `56/56 unchanged`，**根本不产生 `removed`**，
  `enforced` 门不会 refuse。`declared_retirements` 机制是给非 replay 路径用的。
- **不需要 geo 重建**。前端 geojson 刻意不 fetch（§7.1.1 末尾），成员判据在 DB 侧，
  正是第 5 步翻的那一行。
- **不需要维护窗口**。删行只停未来调度；已发布的包仍在 object store。动手前确认
  `squeue` 里没有这 6 个 `dg_*` 在飞即可（本次在跑的是 `dg_3264c89a`/gfs 与
  `dg_30a94855`/ifs，无交集）。

**验收 receipt（2026-08-25 实测）**：

| 项 | before | after |
|---|---|---|
| 注册表 `entry_count`（4 个 provider 全部） | 62 | **56** |
| 刷新分类 `refresh_20260825T114421Z_16c2c2f7df62` | — | `56/56 unchanged`，`removed 0`，`refused 0`，`package_changed 0` |
| `generation` | `manifest-c76de906099f` | `manifest-c5d926ff02b8` |
| active `core.model_instance` / active river networks | 31 / 31 | **28 / 28** |
| `/api/v1/layers` river-network `source_generation` | `…:a4559c13156eb0ea8b29:31` | `…:2f80d8c240118084e6fa:28` |
| `/api/v1/basins?has_display_product=true` | 24（含 3 个 zhaochen） | **21**（无 zhaochen） |
| tile `6/47/23`、`7/94/47`、`8/188/94`（bst） | 有内容 | **0 B** |
| tile `7/100/53`、`8/201/106`（mc） | 有内容 | **0 B** |
| 仍有内容的 tile 里 `zhaochen` 字符串出现次数 | — | **0**（`5/23/11`、`8/200/102`、`9/401/204`、`4/12/6`） |
| 公网 `test.nwm.ac.cn` 同 tile 字节 | — | 与内网一致（nginx 无陈旧缓存） |
| `ops.audit_log` | — | `log_id` 15/16/17，`models.deactivate` / `sys_admin` |

> **读 `source_generation` 有个坑**：deactivate 之后立刻读可能仍是旧值（显示 API 的
> 连接池会话快照）。不要据此判定「没生效」——先用 SQL 直接数 digest 行数
> （`JOIN core.model_instance ... WHERE mi.active_flag = true`），DB 是真值。
>
> **tile 字节不变不一定是缓存**。`cache_key` 含 `tile.source_version`
> （[`services/tiles/mvt.py`](../../../services/tiles/mvt.py) `cache_key()`，当前 def `:299`、
> `"source_version": tile.source_version` 在 `:305`），source_version 一变缓存键必变，
> 取到的就是新生成的 tile。字节不变要先确认挑的 tile **真的覆盖**该流域——
> 本次第一轮就是按名字猜到 98°E/34°N，五个 tile 全部不覆盖，白测一遍。

**复活路径**：恢复两份 manifest 备份（或重新 provision）＋ 目录从
`Basins-retired/issue-1701-20260825/` 移回＋ 27 侧 `--force` 注册（会把
`superseded` 翻回 active）＋ `AUTOPIPE_EXCLUDE_BASINS` 去掉三项＋ baseline
`core.model_instance` 三行 `activate` 回来（少这一步 = 底图上没有河网）。
