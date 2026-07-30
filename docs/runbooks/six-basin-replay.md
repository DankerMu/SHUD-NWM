# Runbook — 六流域生产回放(node-22 + node-27)

> 关联:issue #1164 change 2 · OpenSpec `openspec/changes/six-basin-production-replay/`
> (design.md 为权威)。本 runbook 只覆盖**执行序 / 中断处置 / 回滚**;语义与
> 取舍理由见 design.md,不在此重复。

## 0. 这是什么

用**当前**的 packaged-IC bootstrap + strict warm-start 语义,把 6 个目标流域
(IFS 6 个 dg model id + GFS 6 个 dg model id)在 2026070500..2026072100 共 33 个
时次的预报**原地重跑并替换**,再让 node-27 重新摄入、失效瓦片、live 复核。

三条不可越界的红线(design.md Must-preserve 全表为准):

1. 六流域外 12 个模型的 run / 状态 / journal / 展示**零字节变化**。
2. journal 只追加 —— 全程不删改任何 journal 文件。
3. 生产 env 文件与 timer unit 文件**不改**;回放走独立 env 文件,回放期间 timer 停机。

## 1. 前置条件(逐条勾完再动手)

- [ ] node-22 上 `systemctl --user stop nhms-compute-scheduler.timer`,并确认
      `systemctl --user is-active nhms-compute-scheduler.timer` 为 `inactive`。
      清场工具自己也会探活;探测失败按 active 处理(fail-closed),此时**不要**
      绕过,先把 timer 状态弄确定。
- [ ] node-22 `/scratch/frd_muziyao/NWM` 已 `git pull --ff-only` 到本次回放分支,
      `.venv` 可用(`uv sync --all-extras --dev`)。
- [ ] `infra/env/compute.replay.env` 已由 `infra/env/compute.replay.env.example`
      落盘,mode 0600,`NHMS_RETENTION_ENABLED=false`,replay 三元组已填本次
      source 的 6 个 model id。
- [ ] `NHMS_REPLAY_ARCHIVE_ROOT` 所在文件系统可写且余量充足(清场工具会 df 预检)。
- [ ] 没有正在跑的 scheduler pass(`journal` 根下无新鲜 `.locks`;清场工具会检查,
      600s 内更新的锁视为活跃并拒绝)。

## 2. 执行序(单源串行:先 IFS 全序列,再 GFS;两源不并行)

### 2.1 清场(每源一次,先于该源的所有回放时次)

```bash
cd /scratch/frd_muziyao/NWM
set -a; . infra/env/compute.replay.env; set +a

# (a) dry-run:零写,打印将被移除的双 lane 条目与对象三分状态
#     不要用 `| tee`:管道会吞掉退出码,拒绝(exit 2/3)会留下一个 0 字节文件
#     且看起来"成功"。用重定向 + 显式 rc,stderr 一并落盘(拒绝理由写在 stderr)。
.venv/bin/python -m scripts.replay_state_scope_reset \
  --source IFS --model-id <id1> ... --model-id <id6> \
  > /ghdc/data/nwm/recovery/ifs-reset-dryrun.json 2>&1; rc=$?; echo "rc=$rc"

# (b) 人审 dry-run 输出:条目数 = 6 scope 的历史时次数;
#     确认没有任何非目标 scope / legacy basins_* 条目出现在移除清单里

# (c) 执行
.venv/bin/python -m scripts.replay_state_scope_reset \
  --source IFS --model-id <id1> ... --model-id <id6> --enforce \
  > /ghdc/data/nwm/recovery/ifs-reset-enforce.log 2>&1; rc=$?; echo "rc=$rc"
```

`rc` 必须逐次读:0 才继续,2/3 按下表处置。工具自己写的 receipt 在归档目录里
(`.../six-basin-replay-<ts>/reset-receipt.json`),上面那个 `.log` 只是 stdout/stderr 抄本。

退出码语义:

| exit | 含义 | 处置 |
|---|---|---|
| 0 | `completed`,双 lane 已写回并读回校验通过 | 继续回放 |
| 2 | `refused`,**零变更**(timer 活跃 / 探测失败 / 索引不可读 / 归档不可写 / 空间不足 / journal 锁新鲜) | 修掉 reason 再重跑,不要绕 |
| 3 | `commit_uncertain`,已有 lane 写入但读回校验没通过 | **不得重跑**;先按 §4 回滚,人工核对两 lane 一致后再决定 |

清场只移除**索引条目**并归档其字节;`states/` 下的状态对象一律不删(归档目录里
另存一份拷贝作保险)。归档目录形如
`$NHMS_REPLAY_ARCHIVE_ROOT/six-basin-replay-<YYYYMMDDTHHMMSSZ>/`,内含
`scratch-index-before.json`、`nfs-index-before.json`、`<lane>-removed-entries.json`、
`objects/<state_id>.bin`、`reset-receipt.json`。**receipt 路径与 sha256 必须归档并回贴 issue**
—— 回放驱动器的"旧半 state 字段"唯一合法来源就是它。

### 2.2 回放(每源一次,两阶段 raw-manifest 姿态)

`2026070500 / 070512 / 070600 / 070612 / 070700` 这 5 个时次已无 raw manifest
(raw 保留期从 070712 起),而 dg forcing 包完整。discovery 侧的 replay 分支会以
forcing 在场性准入,但**候选级** raw-manifest 门是另一处、replay 覆写不碰它。因此:

| 阶段 | `--start-cycle`..`--end-cycle` | `NHMS_SCHEDULER_REQUIRE_NFS_RAW_MANIFEST` |
|---|---|---|
| Phase 1 | `2026070500`..`2026070700`(5 时次) | `false` |
| Phase 2 | `2026070712`..`2026072100`(28 时次) | `true` |

Phase 2 必须打回 `true`:GFS 2026070712 的 repair 参数组只在
「raw manifest required 且 ready」时自我授权。

两个阶段的 `--origin-cycle` 都保持默认 `2026070500`:bootstrap 强断言绑定的是
**回放序列原点**,不是本次调用的第一个 cycle。Phase 2 从 070712 起跑,它的首个
cycle 是 warm 时次,不该、也不会被当作 packaged-IC 首时次断言。

清场 receipt 的 `outcome` 必须是 `completed`:驱动器对 `commit_uncertain` /
`refused` 的 reset receipt 直接拒跑(exit 2,`reset_receipt_not_completed`)——
半提交的清场之上不允许开始不可逆替换。

```bash
# dry-run(默认):现场存量核定 + 逐 cycle 预捕获计划,零提交
scripts/ops/node22_six_basin_replay.sh \
  --source IFS --model-id <id1> ... --model-id <id6> \
  --start-cycle 2026070500 --end-cycle 2026070700 \
  --reset-receipt /ghdc/data/nwm/recovery/six-basin-replay-<ts>/reset-receipt.json \
  --receipt-path /ghdc/data/nwm/recovery/ifs-replay-phase1.json

# 执行
scripts/ops/node22_six_basin_replay.sh ... --execute
```

Phase 1 跑完把 env 里的 `NHMS_SCHEDULER_REQUIRE_NFS_RAW_MANIFEST` 改回 `true`,
再跑 Phase 2(`--start-cycle 2026070712 --end-cycle 2026072100`,另给一个
`--receipt-path`)。GFS 序列同法;`gfs` + `2026070712` 由驱动器自动切 repair 参数组
(`NHMS_SCHEDULER_REPAIR_MISSING_FORCING=1` + `_CYCLE_TIME`),无需人工干预。

每个 cycle 的驱动器内部序列(全自动,失败即停):
预捕获(旧 manifest/输出 sha256 + **无条件**记 forcing 包与模型包 checksum +
旧终态 job id 基线)
→ 预 stage(NFS→scratch,逐文件 sha256 校验;`verified != true` 直接停机,
`source_absent` 仅 GFS 070712 豁免)
→ 提交(单 pass,`--cycle-time` 精确定位,不传 `--max-passes`)
→ 等待(每个模型出现**基线之外的新终态 job id** + NFS 索引里 `valid_time = T+12h`
的后继条目 checksum 与 reset receipt 归档值**不同**;默认 90min/cycle 超时。
两条腿都不看时间戳:索引条目的 `created_at` 生产上恒为 null,旧 run 的终态 journal
记录也在清场后留存)
→ 后捕获(`--origin-cycle`(070500)行强断言 `init_mode=3` /
`packaged_calibrated_state` / `packaged_ic_checksum` 非空;键一致断言
`river_network_version_id` + 输出段数 + 输出文件清单)。

替换 receipt 每完成一个 cycle 就原子重写一次(未完成时 `outcome=in_progress`),
中途崩溃不会丢已完成行。

### 2.3 node-27 侧(回放全部绿之后)

顺序不可换:压缩块普查/解压 → 等 autopipe re-ingest → 瓦片失效 → live 验证。

#### 2.3.0 TimescaleDB 压缩块普查与解压(必须先做)

`parser.py` 的替换 DELETE 经 `check_batch_targets_uncompressed` fail-closed:
只要与写入窗口相交的 chunk 还是压缩态,整批 re-ingest 失败。回放窗口的展示跨度是
`[2026-07-05T00Z, 2026-07-28T00Z)`(末次 cycle 2026072100 + 168h 预报跨度),
两张 hypertable 都要查。

```sql
-- 普查:与回放展示窗口相交的压缩 chunk(两张表一起出)
SELECT chunk_schema, chunk_name, hypertable_schema, hypertable_name,
       range_start, range_end
FROM timescaledb_information.chunks
WHERE hypertable_schema || '.' || hypertable_name
        IN ('hydro.river_timeseries', 'met.forcing_station_timeseries')
  AND is_compressed
  AND range_start < TIMESTAMPTZ '2026-07-28T00:00:00Z'
  AND range_end   > TIMESTAMPTZ '2026-07-05T00:00:00Z'
ORDER BY hypertable_name, range_start;
```

命中行为空 → 跳过本节。非空则**逐 chunk**调用解压工具(它一次只处理一个 chunk,
六个 target 参数全部必填,取值逐字来自上面普查结果的同名列):

```bash
# 每个 chunk 一次调用;--receipt-path 必须是全新路径(工具对已存在的 receipt 路径拒跑)
DATABASE_URL='postgres://nhms_owner@127.0.0.1:55432/nhms' \
uv run python -m scripts.node27_timeseries_decompression_replay \
  --database nhms \
  --mutation-head-sha "$(git -C /home/nwm/NWM rev-parse HEAD)" \
  --hypertable-schema hydro --hypertable-name river_timeseries \
  --chunk-schema _timescaledb_internal --chunk-name _hyper_3_7_chunk \
  --range-start 2026-05-28T00:00:00Z --range-end 2026-06-04T00:00:00Z \
  --receipt-path ~/receipts/decompress-<hypertable>-<chunk_name>.json
```

- `--mutation-head-sha`:node-27 工作树 `/home/nwm/NWM` 当前 HEAD(与 live receipt 对齐)。
- **终止条件**:全部命中 chunk 解压完后**重跑一遍普查 SQL**,直到零命中才继续 re-ingest。
- receipt 里出现 `failure.mutation_state: indeterminate`:**不要重跑同一 chunk**
  ——先用普查 SQL 读该 chunk 的 `is_compressed` 真实态;已是 `false` 就记录并继续,
  仍是 `true` 才换一个新的 `--receipt-path` 重试。`failed_before_mutation` 可直接重试(换 receipt 路径)。
- 单 chunk 手工路径与再压缩安排见 `docs/runbooks/tier-node27-timeseries-storage.md:1305-1335`。

#### 2.3.2 瓦片失效

```bash
# 瓦片失效:dry-run 默认;删除范围 = (source_id, valid_time ∈ 回放展示窗口)
# 注意:hydro 图层的 map.tile_cache.source_id 存的是 **run_id**
# (apps/api/routes/hydro_display.py:313-321 传 source_id=run_id),不是 "IFS"/"gfs"。
# 所以 run scope 直接从替换 receipt 的 rows[].run_id 采,精确、零猜测;
# 全国聚合图层(`/` 默认视图)另有固定 source_id `hydro-national`,必须显式补上,
# 否则它会继续吐由旧数据 join 出来的瓦片。
uv run python -m scripts.node27_invalidate_tiles \
  --from-replacement-receipt ~/receipts/ifs-replay-phase2.json \
  --source-id hydro-national \
  --window-start 2026-07-05T00:00Z --window-end 2026-07-28T00:00Z \
  --receipt-path ~/receipts/ifs-tile-invalidation.json
# 复核 dry-run 的 candidate_rows / file_cache_* 计数后加 --execute
```

`--window-end` 取 `末次 cycle 2026072100 + FORECAST_HORIZON_HOURS(168h) = 2026-07-28T00Z`,
不是末次 cycle 本身:末次 run 的预报瓦片 valid_time 一直铺到 T+168h,按 cycle 时间截断
会让约 93% 的最新瓦片存活。

> Note(national digest 前提):全国聚合层的缓存键摘要按**每条河网的全局最新 run**
> 取值(`services/tiles/mvt.py:1126-1163`)。本次回放之所以能靠 key 轮换生效,是因为
> 回放窗口终点恰是冻结的 frontier(2026072100),回放 run 就是最新 run,ingest 无条件
> bump `updated_at`。**timer 重启、frontier 前进之后这个前提不再成立**——那时再回放
> 历史时次,必须另想失效手段,不能指望 national key 自动轮换。

文件缓存目录必须给(`NHMS_MVT_FILE_CACHE_DIR` 或 `--file-cache-dir`);确无文件缓存
的部署显式加 `--no-file-cache`——不给就拒跑,因为只删 DB 行会让文件缓存继续吐旧瓦片。
给了就必须指对:目录不存在 / 不是目录 / 探测不了,一律 exit 2 拒跑
(`file_cache_dir_absent` / `file_cache_dir_undeterminable`),不会退化成"每个 key 都判缺失"。
删除顺序是「先文件后 DB 行」,某个 key 的文件腿判不了(exit 3 `incomplete`)时两腿都
不动,receipt 列出 `blocked_cache_keys`,修因后原样重跑即可。exit 4
(`failed_partial_mutation`)表示**已经删过文件**但 DB 删除或 receipt 落盘没完成:
stdout 上的 receipt 里 `unlinked_file_cache_paths` 是权威清单,按它核对后再决定重跑。

其余步骤见 `openspec/changes/six-basin-production-replay/tasks.md` §6 与
design.md D6;live receipt 按 `docs/runbooks/node-27-bringup-checklist.md` C1-C4 风格。

## 3. 中断处置

驱动器**不自动重试、不自动跳过**。任一失败立即停机,把中断点写进替换 receipt 的
`interruption` 字段,退出码 3。停机原因:

| reason | 含义 | 处置 |
|---|---|---|
| `forcing_source_absent` | 该 cycle 某模型在 NFS 上没有 forcing 包(非 repair cycle) | 先确认 NFS 挂载与保留期;补齐或把该模型移出本次 scope,再续跑 |
| `forcing_staging_unverified` | 预 stage 失败或校验不过(`stage_failed` / `verify_failed`) | 看 `interruption.detail.rows[].detail`;修 scratch 空间/权限后续跑。**不要**手工拷文件绕过校验 |
| `submission_failed` | 该 cycle 的 `plan-production --submit` 非零退出 | 看 pass stdout/journal;修因(Slurm 拒收等)后按下面"续跑" |
| `convergence_timeout` | 超时内没等到「每模型新终态 job id」+「后继 state checksum 已变」 | 先查 Slurm 队列是否仍在跑;`detail.unreplaced_successors` 指出哪些模型的 state 还是旧值。**不要**盲目加大超时反复重投 |
| `state_index_undeterminable` | NFS 状态索引读不了(不是"没有新条目",是判不了) | 查 NFS 挂载与索引文件完整性;判不了绝不当作未收敛以外的任何结论,修好再续跑 |
| `first_cycle_bootstrap_assertion_failed` | `--origin-cycle`(070500)行新半不是 bootstrap 形态 | 严重:说明 packaged-IC 契约没生效。停止全序列,回滚该源,查变更 1 契约 |
| `key_consistency_drift` | 新 run 的 `river_network_version_id` / 输出段数 / 输出文件清单与旧半不一致 | 严重(R3):旧键行不会被 parser 删除条件覆盖,会残留。停止,人工介入;**不要**自动删旧键行 |

续跑时还可能撞上**调度侧**的拒绝(不是驱动器停机原因,出现在 pass stdout 里):

| 现象 | 含义 | 处置 |
|---|---|---|
| 候选被 `PIPELINE_ALREADY_ACTIVE` / `active_duplicate_pipeline` 挡下 | 上一次该 cycle 的 pipeline 还被 journal 记为活跃(上次中断时 Slurm 作业没走到终态) | 先查该 cycle 的 `hydro_run.status` 与 journal `pipeline-jobs/`(以及 `squeue`)确认没有真在跑的作业;确属残留活跃态才处理,**不要**直接重投。作业仍在跑就等它到终态再续跑 |

续跑:

```bash
scripts/ops/node22_six_basin_replay.sh ... --resume-from <上次 receipt 路径> --execute
```

续跑是**证据校验后跳过**,不是盲跳:只有当 receipt 里该 (model, cycle) 行已
`completed`、且当前索引中对应 state 在场且 checksum 与 receipt 记录一致时才跳过;
校验不过就重做该 cycle。

## 4. 回滚(从归档恢复)

回滚粒度是"整源",按与执行相反的顺序做:

1. **停**:确认没有驱动器在跑;timer 仍处停机。
2. **状态索引回滚**:把归档目录里的 `nfs-index-before.json` /
   `scratch-index-before.json` 原样写回各自 lane 的索引路径(路径记在
   `<lane>-removed-entries.json` 的 `index_path` 字段里)。先 NFS 后 scratch
   ——与清场顺序相反,保证中间态的错误方向仍是 bootstrap 不可达(fail-closed)。
3. **状态对象**:清场从不删对象,通常无需恢复;若确认某 `state_id` 对象已丢,
   从 `objects/<state_id>.bin` 拷回其在 `states/` 下的原路径。
4. **run 树**:回放已就地覆盖 `runs/<run_id>/`。旧 run 的字节**没有**全量归档
   (只有 manifest 与输出清单的 sha256 在替换 receipt 里)——因此 run 层面的
   "回滚"实际是**用替换 receipt 的旧半 sha256 判定差异并接受新结果**,或按 §2
   重新回放一遍。这一点在开工前就要跟需求方说清楚。
5. **node-27**:若已 re-ingest,DB 行同样只能靠再次 ingest 收敛;瓦片缓存被删的
   行会按当前数据自然重建,无需回滚动作。
6. 回滚完成后,替换 receipt 与 reset receipt **一并保留**并回贴 issue,不要删。

## 5. 收尾清单

- [ ] 全部 receipt(reset ×2、replacement ×N、tile invalidation、node-27 live)归档并回贴 #1164。
- [ ] 负验证在案:六流域外 12 模型的 run / 索引条目抽样 sha256 回放前后一致。
- [ ] node-22:`systemctl --user start nhms-compute-scheduler.timer`;确认次一自然 pass
      frontier 前进、回放时次走 `completed_duplicate_pipeline` 跳过、无 replay env 泄漏。
- [ ] 两端工作树回到 master:node-22 `/scratch/frd_muziyao/NWM` 与 node-27
      `/home/nwm/NWM` 各自 `git status --porcelain` 干净后 `git checkout master &&
      git pull --ff-only`(本变更**不合并进 master**,分支执行完即弃)。
- [ ] node-22 上 `infra/env/compute.replay.env` 删除或移出运行路径,确认生产 env
      文件未被改动(`git status` 对 `infra/env/` 无变更)。
