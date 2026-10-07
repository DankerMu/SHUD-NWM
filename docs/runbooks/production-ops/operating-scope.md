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

**默认 `/api/v1/basins` 的口径与判据（#2621 owner 裁决 1A，2026-09-29；替代 #1729 第 3 条的「默认参数 ⊆ manifest」）**：

- 裸 `/api/v1/basins`（不带 `has_display_product`）就是 `core.basin` 原始目录，只排除
  `basin_group = 'evidence-only'`（[`packages/common/model_registry_catalog.py`](../../../packages/common/model_registry_catalog.py)
  `list_basins` 的默认分支），含已退役流域**属预期**，API 不改；前端走的是 `has_display_product=true`
  （[`stores/overviewData.ts`](../../../apps/frontend/src/stores/overviewData.ts) 的 `fetchBasins`）。
- 验收口径是两条，缺一不可：
  1. `has_display_product=true` 集合 == node-22 `manifest-last.json` 的 `models[].basin_id` 集合；
  2. 默认目录 − manifest 的**每一项都没有 active `core.model_instance`**
     （按 `basin_version_id` 数，不按 `model_id`，理由见 §7.2 坑 1）。
- 机检用 [`scripts/basin_catalog_manifest_audit.py`](../../../scripts/basin_catalog_manifest_audit.py)：
  两个集合都直接调生产 `list_basins` 分页取满，不另写 SQL，口径不会漂；每个连接强制只读，
  `nhms_display_ro` 足够。node-27 上：

  ```bash
  cd /home/nwm/NWM && DATABASE_URL="$(grep '^DATABASE_URL=' infra/env/display.env | cut -d= -f2-)" \
    .venv/bin/python scripts/basin_catalog_manifest_audit.py \
    --manifest /home/ghdc/nwm/object-store/scheduler/registry/manifest-last.json \
    > /home/nwm/tmp/basin-catalog-audit-<ts>.json; echo "rc=$?"
  ```

  也可以直接传 `--database-url <DSN>`，但口令会留在 argv 和 shell 历史里，优先走 `DATABASE_URL`。
  exit **0** = 通过；**1** = 违规，receipt 的 `violations[]`、`display_minus_manifest` /
  `manifest_minus_display` 与 `catalog_extras[].active_models` 指出是哪一项；
  **2** = 配置或运行错误（manifest 缺失、不可解析或没有 basin_id，没给 DSN，或连库/查询失败），**不是通过**。
  rc=2 且 stdout 里没有 JSON receipt、stderr 只有一行 `database error: <异常类型>: ...` = 连库或查询出错
  （不打印 DSN），什么都没判定，修好连接后重跑。
  新流域上线窗口内 display 集合会暂时落后于 manifest，这时 exit 1 属预期——
  核对两个差集恰好是正在上线的流域即可，上线完成后必须回到 exit 0。
  receipt 注明了每页是独立快照，不是一次一致读。
- **退役流域 ≠ 改名残影，处置不同**：
  - **退役流域一律保留 `core.basin` 行**，不论有没有 run（`basins_neiliuqu` 一条 run 都没有，
    照样保留，见 §7.3）：`core.basin_version` 以 `NO ACTION` 外键引用 `core.basin.basin_id`，
    每个退役流域各有 1 行 `basin_version`，硬删要连 `basin_version` → `model_instance` →
    `hydro_run` 一起删，会毁掉血缘和上面的复活路径。退役照 §7.2 的五步做，并在本节补一条记录。
  - **只有 owner 点名、且已有后继 id 的同日改名残影**可以删，而且只能走 #2621 的 oneshot：
    [`scripts/ops/node27_2621_delete_rename_leftovers.sql`](../../../scripts/ops/node27_2621_delete_rename_leftovers.sql)
    经 [`scripts/ops/node27_oneshot_sql.py`](../../../scripts/ops/node27_oneshot_sql.py) 执行——
    默认 dry-run（结束即回滚），每次运行用一个新的 `--copy-dir`，
    `--set nhms.manifest_basins=<manifest 的 basin_id 逗号串>`；dry-run 与 apply 都要先停
    node-27 的 compression / timeseries-retention / autopipe 三个 user timer、跑完再启动
    （`systemctl --user list-timers` 前后记 receipt，步骤见 §7.5）；`--apply` 只在 owner 确认
    dry-run receipt 之后；恢复用 `node27_2621_delete_rename_leftovers_rollback.sql`，
    `--copy-dir` 指向 apply 那次的目录。脚本把 7 个 id、后继和各表计数写死，**不是通用删除工具**；
    再出现残影要新盘点、新裁决、新脚本。本次记录见 §7.5。

### 7.2 2026-08-25：zhaochen 系列退出业务化（#1701，owner 裁定不建后继）

`basins_zhaochen_{bst,mc,wem}` 三个流域整体退出生产。**owner 裁定「彻底退出，不建
basins_hys_* 后继」**，所以 #1701 原计划的「换 id + 状态延续」整条路作废——没有目标
包，就没有克隆对，`#1697` 的 `--transfer-mode recalibration` 不参与本次操作。

地理位置与名字无关，别按名字找：`bst` 在新疆天山（83.0–88.3°E / 41.5–43.3°N，
**9572 河段**），`mc` 在四川（103.8–104.0°E / 28.8–29.0°N，708 段），
`wem` 在 102.0°E / 34.1°N（308 段）。验收挑 tile 时按这三个 bbox 挑，不要按名字猜。

**本次五步全做了**（§7.1 的 hhe 退役漏了第 5 步，见 §7.1.1）：

1. **注册表两份**（NFS canonical + `/scratch/frd_muziyao/nhms-prod` 本地 mirror）各移除 6 条
   `dg_*`（3 流域 × gfs/ifs），62 → 56，备份 `manifest-last.json.bak-zhaochen-retire-20260825`。
   当时是手工脚本；**现在同样的事用 `scripts/node22_model_succession.py` 的 `--kind remove_basin` 做**（#2757，
   见本节末尾「退役流域的 manifest 发布」）：每条要退的 `dg_*` 一个 `--remove`，同一流域的各 source
   必须一起移除，不需要 provision 回执。
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

   同一流域更换网格、保留新版本上线时，使用
   `AUTOPIPE_EXCLUDE_MODEL_IDS`（CLI 为 `--exclude-model-ids`），不要把整个
   流域加到 `AUTOPIPE_EXCLUDE_BASINS`。按**旧的完整 `basin_version_id`** 查询
   `core.model_instance`，把旧 baseline 和全部 `dg_*` 的完整 `model_id` 以
   逗号分隔写入；保留既有排除项，空项与重复项会忽略。匹配区分大小写，
   不支持前缀、通配符或流域名替代。该规则同时过滤历史运行和 Basins 清单的
   默认模型身份，避免旧运行先覆盖新版本身份、再重新激活旧网格。
   新 `model_id` 仍可为相同 `basin_id` 登记、入库与发布。版本排除配置生效后，
   无法读取完整模型身份的运行会报告 `identity` 阶段失败并隔离，不会借用
   清单身份绕过排除；其他运行继续。CLI 参数会覆盖对应环境变量。
   先停用自动入库 timer，等待正在执行的 service 自然结束，再保存配置和
   停用旧模型；完成后恢复 timer，并跨一轮完整自动入库核对旧版本仍无 active
   模型、新版本正常入库。历史运行的 `superseded` 处理及其他退役步骤仍须执行。

**不需要做的**（都核实过，别顺手做）：

- **不需要 retirement declaration**。`NHMS_SCHEDULER_REQUIRE_DIRECT_GRID=true` 让刷新走
  [`scripts/scheduler_refresh/runner.py`](../../../scripts/scheduler_refresh/runner.py)
  的 replay 分支，`previous_models_snapshot` 由
  `_load_previous_canonical_registry(registry_uri)` 直接读 manifest——手工删行之后
  previous 本身就是 56，分类器看到的是 `56/56 unchanged`，**根本不产生 `removed`**，
  `enforced` 门不会 refuse。`declared_retirements` 机制是给非 replay 路径用的。
- **不需要 geo 重建**。前端 geojson 刻意不 fetch（§7.1.1 末尾），成员判据在 DB 侧，
  正是第 5 步翻的那一行。
- **不需要维护窗口**。删行只停未来调度；已发布的包仍在 object store。当时（2026-08-25）的做法是动手前
  确认没有这 6 个 `dg_*` 在飞（那次在跑的是 `dg_3264c89a`/gfs 与 `dg_30a94855`/ifs，无交集）；
  现行工具不靠这一条把关，原因见本节末尾。

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

**退役流域的 manifest 发布（现行做法，#2757）。** 上面第 1 步现在由 `scripts/node22_model_succession.py` 的
`--kind remove_basin` 一条命令做完：停 timer、等在跑的 pass 结束、发布、refresh、再启动 timer，每步有回执、
可续跑、可 `--abort`。命令与读法见 [`recalibration-and-archive.md`](recalibration-and-archive.md) 的 5.7.4。
它只做 manifest 这一步：不碰 Basins 目录，也不做本节第 2 步起 node-27 上的事（run、排除名单、model 行）——
后者由 node-27 的退役工具用同一个 `--succession-id` 接着做，见 §7.6（第 4 步移目录仍然手工）。
各流域的 Slurm job name 相同，`squeue` 分不出要退的 `dg_*`，所以不靠「先确认队列里没有它们在飞」把关：
发布前已提交的 run 之后仍可能跑完入库，由 node-27 那一半处理（先加排除名单，再翻 `superseded`）。

**手工回退办法（#2738）：直接跑发布工具。** succession 工具用不了时（例如 hard stop 之后按 runbook 手工收尾），
同一件事可以直接用发布工具做；机制、回执与失败结局见 `recalibration-and-archive.md` 的 5.7.1。这时 timer 要
自己停、自己恢复：apply 期间调度器 timer 应处于停止状态，两份 manifest 不一致时 worker 会拒绝 submit。

```bash
# node-22，frd_muziyao
cd /scratch/frd_muziyao/NWM
set -a
. infra/env/compute.scheduler-provider-refresh.env   # 两份 manifest 路径、两个根、prefix、refresh 锁；不含 DB 变量
set +a
SUCCESSION_ID=<succession-id>                        # [A-Za-z0-9._-]{1,80}，一次退役一个
PUBLISH_ARGS=(
  --remove "<dg_* model_id>"                         # 每条要退的行一个；同一流域的 gfs 与 IFS 必须都写
  --operator-id "<operator>"
  --succession-id "$SUCCESSION_ID"
)

# 1) dry-run：不写 manifest、不备份、不取锁
cd /scratch/frd_muziyao/NWM && .venv/bin/python -m scripts.node22_publish_merged_scheduler_registry "${PUBLISH_ARGS[@]}"

# 2) 读回执：removed_model_ids 就是要退的全部行，row_count_after = row_count_before − 行数
cat "$NHMS_SCHEDULER_PROVIDER_STORE_ROOT/scheduler/succession/$SUCCESSION_ID/publish-dry-run.json"

# 3) 调度器 timer 先停（systemctl --user stop nhms-compute-scheduler.timer），确认 service 不在跑，再 apply；
#    参数与第 1 步逐字相同，只多一个 --apply。发布并跑完 refresh 之后再把 timer 恢复原状态
systemctl --user is-active nhms-compute-scheduler.timer nhms-compute-scheduler.service || true
cd /scratch/frd_muziyao/NWM && .venv/bin/python -m scripts.node22_publish_merged_scheduler_registry "${PUBLISH_ARGS[@]}" --apply
```

要退的 `model_id` 按 `basin_id` 从 canonical manifest 取（`dg_*` 是哈希、不含流域名，见上文第 1 个坑的
同一条教训）。发布后照旧手动跑一趟 refresh 重建 readiness（判据见 5.7.1 的「触发手动 refresh 的坑」）；
2026-10-05 生产 manifest 实测 132 行、13,353,454 B、329,431 个 JSON 值节点，上限 32 MiB / 800,000 节点，
移除只会让它变小。

**`--apply` 没有任何输出就死了**（被 `kill -9`、节点掉电等，来不及恢复也来不及写回执）：比对两份 manifest 的
sha256，不同就把两份都从本次的 `.bak-<succession-id>-<stamp>` 备份恢复。

### 7.3 2026-08-25：`basins_neiliuqu` 退出调度（补登，#2621）

**当时没有 issue 或 runbook 记录**，本条是 2026-09-29 按 node-27 只读核查补登的；
退出原因至今没找到书面记录，不要替它编理由。

`basins_neiliuqu`（黄河内流区，st_zhanghx 同批，见 #2364；同批另外 6 个黄河子流域都还在
manifest）：107.09–110.08°E / 37.26–40.55°N，**3404 河段**。2026-08-25T05:36Z 入库，
当天就退出调度：manifest 备份 `manifest-last.json.bak-neiliuqu-retire-20260825`
（`generated_at` 2026-08-25T12:18Z，56 行 / 28 流域，含它的 2 行 `dg_*`）之后的下一份备份
`manifest-last.json.pre-ksath2-20260826`（`generated_at` 15:29Z）已是 54 行 / 27 流域，不含它。

**五步现状（2026-09-29 核查）**：

| 步 | 现状 |
|---|---|
| 1 注册表 | NFS `manifest-last.json` 已移除，备份见上（与 manifest 同目录）。node-22 本地副本 `/scratch/frd_muziyao/nhms-prod`：核查结果见 2026-09-29 receipt |
| 2 `hydro.hydro_run` 翻 `superseded` | 不适用：名下 0 行 run，从没产出过 |
| 3 `AUTOPIPE_EXCLUDE_BASINS` | 已含 `neiliuqu`；改前备份 `infra/env/node27-ingest.env.bak-neiliuqu-retire-20260825T232709` |
| 4 目录 | 已移到 `/home/ghdc/nwm/Basins-retired/neiliuqu-retire-20260825/` |
| 5 `core.model_instance` deactivate | 3 行（`basins_neiliuqu_shud` + 2 个 `dg_*`）全部 `inactive` |

**`core.basin` 行保留**：零 run 也是退役流域，不是残影，不进 #2621 删除脚本（口径见 §7.1.1 末段）。

**复活路径**：同 §7.2——恢复 manifest 备份（或重新 provision）＋ 目录从
`Basins-retired/neiliuqu-retire-20260825/` 移回 ＋ `AUTOPIPE_EXCLUDE_BASINS` 去掉 `neiliuqu`
＋ 27 侧 `--force` 注册 ＋ baseline `basins_neiliuqu_shud` `activate` 回来。

### 7.4 2026-09-22：onboarding-19，SHJ 三子流域并入 `basins_shj`、`basins_xinanjiang_upstream` 退出（补登，#2621）

onboarding-19 上线新流域的同时，有 4 个流域退出调度，**当时同样没有 runbook 记录**，
本条 2026-09-29 补登。操作工作目录 `/home/nwm/nhms-onboarding-19-20260921/`（`receipts/`
里有 deactivate preflight 与 direct-grid provision 的 JSON）。

- **`basins_shj_{2shj,3shj,nj}` 并入 `basins_shj`**：`basins_shj` 2026-09-22T02:26:39Z 入库
  （slug `SHJ`），bbox 119.41–132.53°E / 41.72–51.66°N，与三个子流域 bbox 的并集
  （119.41–132.65°E / 41.71–51.66°N）基本重合；**29428 河段**，而三者合计 29858
  （3770 + 10079 + 16009）——是整体重新剖分，不是拼接，子流域的历史 run 接不到它的河段上。
  baseline `basins_shj_shud` active。
- **`basins_xinanjiang_upstream` 退出**：117.64–118.48°E / 29.47–30.09°N，**216 河段**，
  2026-06-19 入库。**没有后继 id**，按单纯退出记。

manifest 变化（都在 NFS registry 目录）：`manifest-last.json.pre-onboarding-19-20260922-0249Z`
（76 行 / 38 流域，含这 4 项）→ `manifest-last.json.pre-onboarding-19-rename-20260922-0420Z`
（92 / 46，4 项已去、`basins_shj` 已入）→ 改名之后 96 / 48（改名见 §7.5）。

**五步现状（2026-09-29 核查）**：

| 步 | 现状 |
|---|---|
| 1 注册表 | NFS manifest 已移除 4 项，备份见上。node-22 本地副本 `/scratch/frd_muziyao/nhms-prod`：核查结果见 2026-09-29 receipt |
| 2 `hydro.hydro_run` 翻 `superseded` | 2026-09-22 约 03:00Z 翻了 **798 行**（published 732 + succeeded 66），行级备份 `/home/nwm/onboarding19-retired-runs-backup-20260922.csv`。现存 superseded：shj_2shj 178、shj_3shj 163（另 1 failed）、shj_nj 163（另 1 failed）、xinanjiang_upstream 349（其中 55 行是更早的窗口退役）；四者已无 published |
| 3 `AUTOPIPE_EXCLUDE_BASINS` | 已含 `shj_2shj,shj_3shj,shj_nj,xinanjiang_upstream`；改前备份 `infra/env/node27-ingest.env.bak-onboarding19-retire-20260922` |
| 4 目录 | 已移到 `/home/ghdc/nwm/Basins-retired/onboarding19-20260922/{SHJ-2SHJ,SHJ-3SHJ,SHJ-NJ,xinanjiang_upstream}` |
| 5 `core.model_instance` deactivate | 4 个流域名下全部 `inactive`（shj_2shj 5 行，其余各 3 行）；baseline preflight 见 `receipts/deactivate.json`，4 个都是 `blockers: []` |

> **读 run 时间别被 `updated_at` 骗**：翻 `superseded` 的那次 UPDATE 没有动 `updated_at`，
> 这些行最大的 `updated_at` 是 2026-09-21 19:04–19:16Z（最后一批 run 自己的时间，备份 CSV
> 里就是这个值），不是翻转时间。翻转时间以备份文件 mtime（2026-09-22 03:00Z）为准。

**遗留项**：

- 前端静态 geojson 还带着 `basins_xinanjiang_upstream`：`national-basin-domain.geojson`
  1 个面要素、`national-basin-river.geojson` 216 个河段要素（2026-09-29 `jq` 实测；SHJ 三个
  子流域两份文件里都已没有）。不会被渲染（理由同 §7.1.1「静态 geojson 的残留」），但要照那段
  过滤一次，另起跟进，#2621 不改前端资产。

**复活路径**：同 §7.2。SHJ 子流域与 `basins_shj` 覆盖同一片区域，复活任何一个都要 owner
先裁定与 `basins_shj` 的取舍，不要直接恢复。

### 7.5 2026-09-29：同日改名残影删除（#2621 owner 裁决 2）

这 7 个不是退役流域，是 onboarding-19 同一天的改名残影：源目录 `DNZH-*` / `xinan*`
改名为 `SE-*` / `SW-*` 后重新导入，旧 id 的注册行没有清理。owner 裁决删除；退役流域不走这条路
（§7.1.1 末段）。

| 残影 id（2026-09-22 导入，UTC） | 后继 id（同日导入，UTC，在 manifest，1 个 active 模型） |
|---|---|
| `basins_dnzh_mdzh`（02:24:48） | `basins_se_mdzh`（03:30:45） |
| `basins_dnzh_mj`（02:24:55） | `basins_se_mj`（03:30:51） |
| `basins_dnzh_mnzh`（02:25:02） | `basins_se_mnzh`（03:30:57） |
| `basins_dnzh_qtj`（02:25:09） | `basins_se_qtj`（03:31:04） |
| `basins_xinan_dulongjiang`（02:27:10） | `basins_sw_dulongjiang`（03:31:13） |
| `basins_xinan_lancangjiang`（02:27:24） | `basins_sw_lancangjiang`（03:31:27） |
| `basins_xinan_nujiang`（02:27:44） | `basins_sw_nujiang`（03:31:46） |

**删除范围（node-27 只读盘点，2026-09-29；删除脚本里写死的就是这组数）**：basin 7 /
basin_version 7 / mesh_version 7 / river_network_version 7 / model_instance 21（全部
inactive，baseline preflight 见 onboarding-19 工作目录 `receipts/deactivate-rename.json`）/
river_segment 85196 / river_segment_crosswalk 85471 / met_station 2290；普通表里的 hydro_run、
forcing_version、interp_weight、state_snapshot、run_display_coverage 都是 0。三张 hypertable
（`hydro.river_timeseries`、`met.forcing_station_timeseries{,_legacy}`）的零引用**不在盘点里**，
由脚本 Gate A2 在锁定之后逐个探测证明，结果见下面的 dry-run receipt；旁证是写入路径的不变量——
目标模型没有任何 hydro_run，`river_timeseries` 经 `run_key` 挂不到这些河段上。
目标站点的 `grid_snapshot_id` 只指向两个 `met.canonical_grid_snapshot`：`2f00657d-…`（gfs）与
`bcd58449-…`（IFS），各 1145 站——这是八张表唯一一条指向表外的外键，删除脚本把它写死成门禁，
回滚脚本在 COPY 之前断言这两个父行仍在（以及八张表的外键出边集合未变）。
**阻断门禁**（命中即 RAISE、一行不删）：上面各项零依赖，再加 `ops.pipeline_job.model_id` 与
`hydro.state_snapshot.cloned_from_model_id`。**只登记不删的只有两张**：`ops.audit_log`（entity_id
等值计数）与 `met.best_available_selection`（无外键，只有 text `forcing_version_id`，目标名下没有
forcing_version）。

**删之前已确认、删之后要记的（2026-09-29 node-27 只读实测）**：

- `/home/ghdc/nwm/Basins` 下已没有 `DNZH-*` / `xinan*` 源目录，seed 这一路不会再生出这 7 个 id。
- **但 run 目录还在**：`/home/ghdc/nwm/object-store/runs/` 下有 **14 个**
  `fcst_{gfs,ifs}_2026091700_dg_<目标 dg 模型>`（7 个 gfs + 7 个 ifs，14 个 dg 模型各一个），
  `input/manifest.json` 的 `identity.basin_id` 就是目标流域、没有 `basin_slug`。autopipe 的
  `_discover_runs` / `_basin_identity`（[`scripts/node27_autopipeline.py`](../../../scripts/node27_autopipeline.py)）
  由此推出 `basin_key`（如 `dnzh_mdzh`），不被排除就会进入 seed 阶段、按 run manifest 身份重新
  入库。`scheduler/state-index/index-last.json` 里也还有这 14 个 dg 模型各 1 条
  `usable_flag=true` 的状态（`valid_time` 2026-09-17T12Z）。
- 所以**删除后 `AUTOPIPE_EXCLUDE_BASINS` 里的
  `dnzh_{mdzh,mj,mnzh,qtj},xinan_{dulongjiang,lancangjiang,nujiang}` 必须保留**，不是死配置：
  这 7 个 id 挡住的正是上面 14 个 run 目录。复活路径靠「排除清单 + seed 目录缺席」两道一起关着，
  不是只靠 seed 目录缺席。只要这些 run 目录还在，就不能从排除清单里去掉它们（清理 run 目录与
  state-index 条目另起跟进，#2621 不动 object-store）。当前那一行与改名前备份
  `infra/env/node27-ingest.env.bak-onboarding19-rename-20260922` 的差异恰好是追加的这 7 个 id。

**执行方式**（步骤全文以 SQL 头注释为准）。node-27 **没有** TimescaleDB 的压缩/保留策略作业
（`timescaledb_information.jobs` 只有 Telemetry Reporter 与 Error Log Retention Policy，
2026-09-29 实测）；压缩、保留与注册表/ingest 写入都是 user systemd timer，dry-run 与 apply
前各停一次、跑完各启动一次：

```bash
cd /home/nwm/NWM && export TMPDIR=/home/nwm/tmp
T="nhms-node27-timeseries-compression nhms-node27-timeseries-retention nhms-node27-autopipe"
systemctl --user list-timers --all --no-pager            # 落 receipt：停之前
systemctl --user stop $(printf '%s.timer ' $T)
systemctl --user is-active $(printf '%s.service ' $T)    # 三个都必须是 inactive；在跑就等它跑完，不要 kill
# owner DSN 只放在 source 进来的私有 env 变量里，不进 argv
MANIFEST_BASINS="$(jq -r '[.models[].basin_id] | unique | join(",")' \
  /home/ghdc/nwm/object-store/scheduler/registry/manifest-last.json)"
# dry-run（默认回滚），copy 目录只给这一次用
.venv/bin/python scripts/ops/node27_oneshot_sql.py scripts/ops/node27_2621_delete_rename_leftovers.sql \
  --dsn-env <持有 owner DSN 的变量名> --copy-dir /home/nwm/tmp/2621-delete-dry-<ts>/ \
  --set nhms.manifest_basins="$MANIFEST_BASINS"
systemctl --user start $(printf '%s.timer ' $T)
systemctl --user list-timers --all --no-pager            # 落 receipt：启动之后
# owner 过目 dry-run receipt → 再按上面停三个 timer 并确认 inactive → 用新的 copy 目录 --apply，
# 并 --set nhms.probe_timeout_s=<dry-run 最大探测耗时 ×2，60..3600> → 启动 timer、记 list-timers
# → copy 目录复制一份离开 node-27
```

`download`、`raw-retention`、`mvt-cache-retention` 等 timer 只动文件、不写这些表，不停。
探测超时就停下报 owner，不要自己加大超时重跑。回滚：
`scripts/ops/node27_2621_delete_rename_leftovers_rollback.sql`，`--copy-dir` 指向 apply 那次的目录；
它在 COPY 之前拒绝三种情况：目标行仍在、八张表外键出边集合漂移、两个 grid snapshot 父行缺失。

**删后 receipt 要记**：§7.1.1 末段的 audit（预期 exit 0，9 个 extra）；公网 `/api/v1/basins`
默认 57、display 48、manifest 48——display API 的目录缓存（`apps/api/display_cache.py`）TTL 60 s、
stale 最长 600 s、每 45 s 预热回放，所以隔 ≥ 60 s 读两次、直到读到 57 再记；
`/api/v1/basins/<删除项>/versions` 返回 404；`GET /api/v1/layers` 里 `river-network` / `discharge`
的 `metadata.source_generation`（即 `national_river_network_source_version` /
`national_discharge_source_version`，只摘要 active 模型）与 `cache_version` 删前删后不变；
目标 run 目录数（14）、state-index 目标条目数（14）删前删后不变；`AUTOPIPE_EXCLUDE_BASINS`
与 `.bak-onboarding19-rename-20260922` 的 diff 删前删后一致（恰好 7 个 id）。

**执行结果：见 receipt（待填）**——dry-run / apply 的 receipt 路径、各步与各探测耗时、
三个 timer 停启前后的 `list-timers`、上面的删后各项，在生产执行完成后填入。

### 7.6 node-27 的流域退役工具（#2757）

§7.2 五步里的第 2、3、5 步（run 翻 `superseded`、加排除名单、deactivate model 行）现在由
[`scripts/node27_retire_basin.py`](../../../scripts/node27_retire_basin.py) 一条命令按固定顺序做完，
每步有回执、可续跑。第 1 步仍是 node-22 的 `--kind remove_basin`
（[`recalibration-and-archive.md`](recalibration-and-archive.md) 5.7.4），第 4 步（移走 Basins 目录）
**工具不做**，见下文「仍然手工的」。

**顺序：先 node-22，后 node-27，用同一个 `--succession-id`。** 工具在任何步骤之前检查
`<回执根>/<succession-id>/step-finish.json` 的 `outcome` 是 `completed`、`plan.json` 的 `kind` 是
`remove_basin`、plan 的 `removes` 里至少有一个 id 是这个 basin version 名下的 `core.model_instance` 行，
并且 canonical manifest 里**已经没有这个流域的任何行**（本 basin version 的行，以及同一 `basin_id` 其它
version 的行——排除名单按流域生效，流域还有 version 在调度时不能退；同一流域换网格不是退役，走 §7.2 坑 2
末尾的 `AUTOPIPE_EXCLUDE_MODEL_IDS`）。任何一项不满足就拒绝，什么都不写。
node-22 那一半因 hard stop 而手工收尾的 succession 没有 `step-finish.json`：node-27 这几步也照 §7.2 手工做。

工具的四步：

```text
exclude（把流域 key 追加进 AUTOPIPE_EXCLUDE_BASINS，等一轮在这之后才开始的 autopipe）
  -> supersede（succeeded / parsed / published 的 run 行备份成 CSV 并翻 superseded，同一个事务）
  -> deactivate（该 basin version 全部 active 的 model 行：先全部 preflight，再逐行走 lifecycle）
  -> verify（再等一轮完整的 autopipe，回读：没有 active 行、没有候选 run、key 还在名单里）
```

```bash
# node-27，nwm
cd /home/nwm/NWM && export PATH=$HOME/.local/bin:$PATH TMPDIR=/home/nwm/tmp
# 环境两种装法，任选其一。工具要求进程里的 DATABASE_URL 与 env 文件里那一行逐字相同，否则拒绝
# (a) source ingest env 文件
set -a; . infra/env/node27-ingest.env; set +a
# (b) 只取需要的两行
export DATABASE_URL="$(grep '^DATABASE_URL=' infra/env/node27-ingest.env | cut -d= -f2-)"
export OBJECT_STORE_ROOT="$(grep '^OBJECT_STORE_ROOT=' infra/env/node27-ingest.env | cut -d= -f2-)"

SUCCESSION_ID=<succession-id>                        # 与 node-22 的 remove_basin 相同
BASIN_VERSION_ID="<完整的 basin_version_id>"
RETIRE_ARGS=(
  --succession-id "$SUCCESSION_ID"
  --basin-version-id "$BASIN_VERSION_ID"             # 一次一个，精确匹配，不是流域名也不是前缀
  --operator-id "<operator>"
  --reason "<退役原因，写进回执与审计日志>"
)
LOG=/home/nwm/tmp/retire-$SUCCESSION_ID-$BASIN_VERSION_ID.log   # 带 basin version：同一 succession 的第二个流域不覆盖第一个的日志

# 1) dry-run：不改任何文件、不写回执、不提交任何写事务；读它打印的 JSON
cd /home/nwm/NWM && env -u NHMS_AUTH_MODE -u AUTH_BACKEND uv run --no-sync python -m scripts.node27_retire_basin "${RETIRE_ARGS[@]}"

# 2) 报告无误、owner 过目后 detached 执行（两次等 autopipe，各最多 30 分钟，见下文「等 autopipe 等的是什么」）；看 $LOG 与回执目录
cd /home/nwm/NWM && { setsid nohup env -u NHMS_AUTH_MODE -u AUTH_BACKEND uv run --no-sync python -m scripts.node27_retire_basin "${RETIRE_ARGS[@]}" --apply > "$LOG" 2>&1 < /dev/null & }
```

`basin_version_id` 从 `core.basin_version` 按 `basin_id` 查出来再抄，不要手拼。`--env-file` 默认是本 checkout 的
`infra/env/node27-ingest.env`，`--receipt-root` 默认 `<OBJECT_STORE_ROOT>/scheduler/succession`，
`--autopipe-wait-seconds` 默认 1800。

- **开始之前就拒绝的（两种模式都一样，此时还没连库）**：进程环境里有 `NHMS_AUTH_MODE` 或 `AUTH_BACKEND`
  （lifecycle 调用只能带 ingest 环境，所以上面两条命令都套了 `env -u`）；进程的 `DATABASE_URL` 与 env 文件里的
  不是同一个值（env 文件必须恰好有一行不带引号、不带 `export` 的 `DATABASE_URL=<值>`——这把「写哪个库」和
  「改哪份排除名单」绑在一起，演练库配不上生产 env 文件）；已有另一个实例在跑（独占锁 `<env 文件>.retire-lock`）。
- **读 dry-run 报告看什么**：`would_be_refused` 为空；`preconditions` 里的 `basin_id`、`basin_key`、
  `removed_model_rows` 是要退的那个流域；**第一次在生产 apply 之前，把 `basin_key` 与 autopipe 自己对该流域用的 key 对一遍**
  （autopipe 每轮打进日志的 JSON 摘要里该流域各条目的 `basin_key`；它由 run manifest 的 `basin_slug` 或 `basin_id` 推出，
  工具只看 `core.basin_version.basin_id`，两者不同时排除项挡不住该流域，先停下报 owner）；`steps.exclude.would` 说要不要改 env 文件；`steps.supersede` 的
  `row_count` / `status_counts` 是真实语句跑出来又回滚的结果（有前置条件被拒时这一步是 `not run`，不向库发写语句）；
  `steps.deactivate.active_model_rows` 每行 `preflight_blockers` 都是 `[]`；`autopipe_unit_now` 是 unit 的一次读数，
  dry-run 不等。
- **一个 succession 退几个流域**：node-22 的 `remove_basin` 可以一次移除几个流域的 model；node-27 这边**每个
  basin version 跑一次**，同一个 `--succession-id`、不同的 `--basin-version-id`，一个跑完再跑下一个（锁会拒绝并行）。
  各自的回执与备份互不相干，都在自己的 `retire-<basin_version_id>/` 目录里。
- **exclude 只改一行**：env 文件必须是普通文件（不是符号链接）、权限恰好 0600、属主是当前用户，并且恰好有一行
  `AUTOPIPE_EXCLUDE_BASINS=<逗号串>`（不带引号、行尾没有注释），这个变量名在文件里**只能出现这一次**（注释里、`export`、
  `declare`、`unset`、同一行的第二处赋值里出现都算），它的上一行不能以反斜杠结尾，否则这一步失败、文件不动。key 是 `basin_id` 经 autopipe 自己的规范化得到的（`basins_huai` -> `huai`），名单里已有同一个 key
  的任何写法时不写文件。改之前先写备份 `<env 文件>.bak-<succession-id>-<basin_version_id>`（0600，里面有
  `DATABASE_URL`，所以放在 env 文件旁边而不是回执根）；同名备份已存在时内容与 env 文件相同就沿用，不同就失败，从不覆盖。
- **等 autopipe 等的是什么**：工具不停、不启动任何 unit，只每 15 秒读一次 `nhms-node27-autopipe.service`
  （`LoadState` 不是 `loaded` 直接失败）。生产上一轮 autopipe 比 timer 周期长（2026-10-07 实测约 13 分钟对 10 分钟），
  **一轮接一轮**，unit 永远是 `activating`、上一轮的退出码马上被覆盖，所以工具按**启动时间戳**跟轮次：改完 env 文件之后才
  启动的第一轮是候选，它以两种方式之一算数——
  - **看到它停下**（`ended: seen_at_rest`）：自己退出、退出码 0 或 1（1 = 有流域的 run 或 seed 失败，名单照样读全了）。
    退出码 2（bootstrap / preflight 被拦）或被信号杀掉的不算，丢掉这个候选、接着跟下一轮；停下时 autopipe 的锁文件还被占着
    （有人在 systemd 之外手工跑着一轮，systemd 那一轮只是跳过）也不算。
  - **被下一轮接上**（`ended: followed`）：没见它停下，但后面的读数出现了更晚的启动时间戳。oneshot unit 不会自己重叠，
    所以候选已经结束；退出码读不到，回执里记 `unknown`，锁也不探测（锁在接上的那一轮手里）。
  所以一次等待在生产上通常要**跨两次启动**：改完之后的下一轮启动、再到它的后一轮启动，按 13 分钟一轮最长约 26 分钟，
  默认 1800 秒够用但不宽裕。**超时**（消息里写着它在跟哪一轮，或「还没有轮次启动」）不是出错：env 文件已经改好、备份已写，
  同一条命令重跑会从新的参考点重新等，不会再改 env 文件、不会写第二份备份；轮次更慢时加大 `--autopipe-wait-seconds`。
- **失败与续跑**：某步失败时退出码非零，目录里多一份 `retire-failed-<stamp>.json`（`step`、`reason`、
  `completed_steps`；deactivate 失败时还有 `rows_done` 与每行的 preflight / 返回状态）。排除原因后**同一条命令重跑**，
  有回执的步骤跳过。工具不回退任何一步。

**读回执**（`<回执根>/<succession-id>/retire-<basin_version_id>/`）：

| 文件 | 看什么 |
|---|---|
| `retire-exclude.json` | `basin_key`、`file_changed`、`env_backup`、`sha256_before` / `sha256_after`、`autopipe_round`（候选轮的启动 monotonic 微秒、`ended` 是 `seen_at_rest` 还是 `followed`、`exit_code` / `exit_status`（`followed` 时是 `unknown`）、探测的锁路径、`candidates_dropped`、因锁被占而跳过的轮次） |
| `hydro-run-backup.csv` | 被翻转的 run 行翻转之前的整行（`COPY ... WITH CSV HEADER`），复活时用 |
| `retire-supersede.json` | `row_count` = `updated_row_count`、`status_counts`、CSV 的 `run_backup_sha256`；`recovered_from_existing_backup` 为 `true` 表示这份回执是续跑时按已有 CSV 补写的 |
| `retire-deactivate.json` | `model_rows[]` 每行的 `preflight_warnings`、`status`（`allowed` 或 `already_current`）与 `audit_log_id` |
| `retire-verify.json` | `active_model_row_count` 0、`candidate_run_count` 0、`key_in_exclusion_list` true，以及第二轮 autopipe |

> 翻 `superseded` 的 UPDATE 只改 `status`，不动 `updated_at`（同 §7.4 的提醒）：翻转时间看 `retire-supersede.json`
> 的 `generated_at`，不要看 run 行。

**「备份 CSV 已经在、候选 run 还有」**。supersede 重跑时发现 `hydro-run-backup.csv` 已存在：名下已没有
`succeeded` / `parsed` / `published` 的 run，就是上次事务提交了、回执没写成，工具按这份 CSV 补写回执，什么都不改。
还有候选 run 时工具失败并把两件事都报出来，由人判断——要么上次在提交之前被杀（CSV 是孤儿，一行都没翻），要么提交之后
又有 run 入库（排除名单没生效，先查这个）。处置：把 CSV 的 `run_id` 与表里对一遍，

```sql
select status, count(*) from hydro.hydro_run where basin_version_id = '<basin_version_id>' group by status;
```

CSV 里的 `run_id` 在表里仍是原状态 = 上次没提交，把 CSV 改名挪开（不要删），重跑同一条命令，工具会重新备份并翻转；
CSV 里的行已是 `superseded` 而候选 run 是别的 `run_id` = 提交之后新入库的，先确认 key 在排除名单里、等一轮 autopipe，
再把 CSV 改名挪开后重跑（新的 CSV 只含新行，两份都留着）。

**verify 失败（被翻回去了）**。失败消息会点名是哪一项：model 行又 active、有 run 回到候选状态、或 key 不在名单里。
原因只有三种：读了旧名单的那一轮 autopipe、key 不在名单时跑的一轮、有人手工强制入库。先把排除项补回去，再重跑同一条命令；
已经完成的三步不会重做——**包括 exclude 的那次等待**：重跑只做 verify 自己的一次等待（再跨一轮 autopipe）然后回读。
需要重新 supersede / deactivate 时把对应的 `retire-<步名>.json`（以及 supersede 的 CSV）改名挪开。

**仍然手工的**（工具跑完会把这三条打在 stderr 上）：

- **Basins 目录**：照 §7.2 第 4 步 `mv` 到 `<root>/Basins-retired/<标签>/`，两棵树都移，不删。
- **前端静态 geojson**：仍带着该流域的要素，不渲染但要过滤一次（§7.1.1「静态 geojson 的残留」）。
- **排除项不能删**：只要 object store 里还有该流域的 run 目录，`AUTOPIPE_EXCLUDE_BASINS` 里的 key 就必须留着（§7.5 的同一条理由）。

工具**从不写** `core.basin`、`core.basin_version`、run 的 `updated_at`，不移任何 Basins 目录，不改任何 unit 的状态。

**手工复活（工具不做）**。顺序与退役相反，都从本次的备份出发，做之前 owner 先裁定：

1. node-22：把流域的 `dg_*` 行重新发布进两份 manifest（重新 provision 后走 5.7.3 的 `add_basin`，或恢复 `remove_basin`
   那次的 manifest 备份）；Basins 目录从 `Basins-retired/` 移回。
2. `core.model_instance`：当时 active 的行 = 该 basin version 目录里**每一份** `retire-failed-*.json` 的 `rows_done`
   **加上** `retire-deactivate.json` 的 `rows_done` 的并集——deactivate 中途失败过时，最后那份回执只列续跑那次处理的行，
   之前已经关掉的行只在失败回执里。逐行走 §7.1.1 的 lifecycle 通道 `activate` 回来（baseline 那一行少了 = 底图上没有河网）。
3. `hydro.hydro_run`：按 `hydro-run-backup.csv` 里每行的 `run_id` 与 `status` 把状态改回去（只改 `status`，
   仍然不动 `updated_at`），在一个事务里做，改的行数与 CSV 行数相同再提交。
4. 最后才从 `AUTOPIPE_EXCLUDE_BASINS` 去掉该 key：对照 `.bak-<succession-id>-<basin_version_id>` 只改那一行
   （**不要整份还原备份**——其后别的退役追加的 key 会一起丢），然后跨一轮完整 autopipe 再读数。

### 7.7 清理被取代 model 的插值权重（#2699）

每一代 Direct Grid 都注册自己的 `core.model_instance`、`met.met_station` 与 `met.interp_weight` 行，上一代的没人删。
owner 裁定（2026-10-07）：只保留最新可展示的那一代，被取代的各代与旧的 `basins_*_shud` 的权重删掉、不是停用。
[`scripts/node27_purge_superseded_weights.py`](../../../scripts/node27_purge_superseded_weights.py) 只删
`met.interp_weight`，默认 dry-run。

**一个 model 的权重只有同时满足下面六条才会被删**（在 SQL 里用服务器时钟判定，窗口是
`now() - make_interval(days => --min-idle-days)`，默认 30 天；NULL 时间戳不算「窗口内」）。报告里的类别按这个顺序取第一个命中的：

| 类别 | 含义（命中即保护） |
|---|---|
| `current` | 是某个 `(basin_version_id, lower(source_id))` 最新可展示 forecast run（`succeeded` / `parsed` / `published`，`cycle_time` 非空；先比 `cycle_time` 再比 `run_id`）所用的 model |
| `in_manifest` | `model_id` 在 canonical manifest `<OBJECT_STORE_ROOT>/scheduler/registry/manifest-last.json` 里 |
| `recent_run` | 它有 `hydro.hydro_run` 行（任何类型、任何状态）的 `cycle_time` / `start_time` / `created_at` / `updated_at` 在窗口内 |
| `recent_forcing` | 它有 `met.forcing_version` 行的 `created_at` 在窗口内（forcing 与权重先于 run 行落库） |
| `recent_weights` | 它有权重行的 `created_at` 在窗口内（刚重建过） |
| `recently_created` | 它的 `core.model_instance.created_at` 在窗口内（或查不到该行） |
| `purgeable` | 以上都不命中 |

规则**不看** id 前缀、`active_flag`、`lifecycle_state`：旧 baseline 行按设计仍是 `active` 却什么都不产出。

```bash
# node-27，nwm；环境装法同 §7.6（DATABASE_URL 必须与 env 文件里那一行逐字相同，否则拒绝）
cd /home/nwm/NWM && export PATH=$HOME/.local/bin:$PATH TMPDIR=/home/nwm/tmp
export DATABASE_URL="$(grep '^DATABASE_URL=' infra/env/node27-ingest.env | cut -d= -f2-)"
export OBJECT_STORE_ROOT="$(grep '^OBJECT_STORE_ROOT=' infra/env/node27-ingest.env | cut -d= -f2-)"
# 默认回执根在 <OBJECT_STORE_ROOT>/scheduler/ 下，node-27 的 nwm 无写权限（2026-10-07 实测被拒）；用 nwm 自有的 NFS 目录
PURGE_ARGS=(--operator-id "<operator>" --reason "<原因，写进回执>" --receipt-root /home/ghdc/nwm/archive/weight-purge)
LOG=/home/nwm/tmp/weight-purge-$(date -u +%Y%m%dT%H%M%SZ).log

# 1) dry-run：只读连接，不建任何文件或目录，不发任何写语句；stdout 的 JSON 就是交给 owner 的回执
cd /home/nwm/NWM && uv run --no-sync python -m scripts.node27_purge_superseded_weights "${PURGE_ARGS[@]}"

# 2) owner 对 dry-run 的数字明确批准之后才 apply；detached，看 $LOG 与回执目录
cd /home/nwm/NWM && { setsid nohup uv run --no-sync python -m scripts.node27_purge_superseded_weights "${PURGE_ARGS[@]}" --apply > "$LOG" 2>&1 < /dev/null & }
```

参数：`--env-file`（默认本 checkout 的 `infra/env/node27-ingest.env`）、`--receipt-root`（默认
`<OBJECT_STORE_ROOT>/scheduler/weight-purge`）、`--min-idle-days`（默认 30；低于 21 即生产 forcing 保留期，拒绝）、
`--pause-seconds`（model 之间的停顿，默认 2.0）、`--max-models`（本次最多处理几个 model，先小批试跑时用）。

- **连库之前就拒绝的**：`--operator-id` / `--reason` 为空；`--min-idle-days` 低于 21；进程的 `DATABASE_URL` 不是 env 文件里
  那唯一一行不带引号的 `DATABASE_URL=<值>`；manifest 不存在、读不了、没有 `models` 列表（或列表为空）、有哪一行没有非空的
  `model_id`；`--apply` 时已有另一个实例（独占锁 `<env 文件>.weight-purge-lock`，dry-run 不取锁）。
- **读 dry-run 报告看什么**：`server_version`；`classes` 每一类的 `models` / `rows`（`current` 的 model 数应当等于 manifest
  里 `dg_*` 的个数）；`purgeable_models` 逐个 model 的行数；`total_rows` 与 `largest_model` 就是一次 apply 的写入量
  （最大的那个 model 是单个事务的大小）；`model_ids_an_apply_cannot_render` 必须是 `[]`（id 不符合
  `^[A-Za-z0-9_.-]{1,128}$` 的 model，apply 会在它那里失败停下）。
- **apply 对每个 purgeable model 做什么**（按 `model_id` 排序，每个 model 一个 READ COMMITTED 事务）：`lock_timeout` 10 秒；
  对该 model 每个 `(source_id, grid_id)` 取权重写入方（forcing producer、forcing domain handoff）用的同一把 advisory 锁；
  在锁内重新判定一次（此前先重读 manifest——node-22 会改写它；已受保护的记为 `skipped`，继续下一个）；
  **备份与删除是同一条语句** `COPY (DELETE ... RETURNING ...) TO STDOUT`，写进 `weights-<model_id>.csv`（0600）；
  该 model 必须一行不剩、CSV 至少一行，fsync 之后才提交。任何数据库错误（含等锁超时）或校验失败都**停下整次运行**：
  之前的 model 保持已删，之后的没动。
- **工具从不写** `met.met_station`、`core.model_instance`、`hydro.*`、`met.forcing_version`、任何 timeseries 表；
  不发 `VACUUM` / `ANALYZE` / `TRUNCATE`（空间由 autovacuum 按自己的节奏回收）。

**读回执**（`<回执根>/purge-<UTC 时间戳>/`，全部独占创建、从不改写；重跑是新目录，已删的 model 不再是候选）：

| 文件 | 看什么 |
|---|---|
| `model-<nnnn>.json` | 每个处理过的 model 一份：`status`（`purged` / `skipped`）、`rows`、`backup`、`sha256`、`class`（`skipped` 时是保护它的类别；`no_weight_rows` = 轮到它时已经没有权重行） |
| `weights-<model_id>.csv` | 被删的行原样（`COPY ... WITH CSV HEADER`，含 `weight_id`），恢复时用 |
| `purge-receipt.json` | 正常结束：operator、reason、`thresholds`、`manifest_at_start.sha256`、`classes_at_start`、`totals`、`models`、`models_not_reached`（被 `--max-models` 挡下的） |
| `purge-failed.json` | 失败停下：`failed_model`（`model_id`、`error`、`outcome`、`backup_kept`）、已完成的 `models`、`models_not_attempted` |

两份终态回执只会有一份。`failed_model.outcome`：`rolled_back` = 该 model 一行没删、备份已移除；`unknown` = COMMIT 调用本身
没返回，**删没删不知道**，备份留着——再跑一次 dry-run，该 model 不在任何类别里（没有权重行了）就是删了；`committed` = 删了、
备份留着，只是其后的回执或校验没写成。

**恢复一个 model 的权重**。先把该 model 现有的行删掉（producer 可能已经重建了一部分，唯一键会拒绝整个 copy），再用
**客户端** `\copy`（服务器端 `COPY FROM '<file>'` 在容器里看不到 NFS 路径）；`weight_id` 是 BIGSERIAL，写回原 id 没问题：

```sql
begin;
delete from met.interp_weight where model_id = '<model_id>';
\copy met.interp_weight (weight_id, source_id, grid_id, model_id, station_id, variable, grid_cell_id, weight, method, created_at, grid_signature, active_flag, superseded_at, grid_snapshot_id) from '<回执目录>/weights-<model_id>.csv' with csv header
select count(*) from met.interp_weight where model_id = '<model_id>';  -- 与 model-<nnnn>.json 的 rows 相同再提交
commit;
```

forcing producer 为某个 model 产出时也会重建它的权重（delete + insert），所以仍在产出的 model 不需要手工恢复。
恢复回来的行带着原来的 `created_at`：该 model 若此后没有变成受保护的（进 manifest、有窗口内的 run / forcing 等），下一次 `--apply` 会把它再删一遍。

[`scripts/register_grid_snapshot_drift.py`](../../../scripts/register_grid_snapshot_drift.py) 更新权重行时**不取**上面那把 advisory 锁：
它在跑时工具等的是行锁，等到 `lock_timeout`（10 秒）就在那个 model 停下——两者不要同时跑。

**这次没做的**：

- **`met.met_station` 行不删**（owner 同日裁定）。`met.forcing_station_timeseries` 与 `…_legacy` 引用
  `met.met_station` 而没有以 station 列打头的索引，每删一行 station 都要把两张表各扫一遍；等 legacy 表下线（#1993）、
  RAID 链路修好之后再做。`met.interp_weight` 没有入向外键，按 `model_id` 走索引删。
- **`core.model_instance`、run、forcing version 都不删**；工具不排程，每次由人执行。
- **全量 display-coverage 刷新**之后，被清掉权重的 model 的旧 run 会显示 station 覆盖为零——这些 run 的序列本来就已被保留期
  清掉了，不是新问题。
