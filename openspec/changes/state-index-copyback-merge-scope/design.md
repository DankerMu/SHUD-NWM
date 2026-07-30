# Design: state-index-copyback-merge-scope

## Risk triage

- **Fixture level: expanded**(共享 canonical provider 的唯一写者 + 生产恢复路径;错放宽 = 索引污染/对象复活,错收紧 = 链继续死)。
- 风险轴:①校验收窄的边界必须精确——只豁免"destination 侧历史 entry 的对象存在性",新增 entry 的完整性保证零回退;②不得复活已归档对象、不得为败北 entry 拷对象(与 node-27 mover 契约对齐 + index↔对象自洽);③replay 工具是生产 index 的显式变更入口,必须幂等 + 受 `authoritative_run_ids` 约束 + receipted + 空解析 fail-closed。

## 断链机制(诊断在案,#1189)

`merge_state_snapshot_index_copyback`(`packages/common/state_manager.py:1863-2036`)destination 侧三处全量对象校验/操作:

1. `:1958-1967` destination 读侧 `_validate_state_snapshot_index(verify_objects=True, object_store_root=destination_containment_root)` —— 574 个已归档对象 → `state_snapshot_index_object_missing` 冒泡(不在 `:1969-1975` except 名单)→ fail-closed。
2. `:2003-2015` `_copyback_state_checkpoint` 遍历 **`merged.values()`**(全部 entry):对已归档 entry,source(/scratch)仍有对象 → 会把它们**复活**回 NFS,与 mover 永久拉锯(当前因 1 先炸而不可达,若只修 1 即触发)。
3. `:2016-2025` `publish_state_snapshot_index(verify_objects=True, object_store_root=destination_containment_root)` —— 第三次全量对象校验,同样会炸。

**source 侧校验(`:1923-1932`)不在改动面**:对 source index **全量** entry(过滤前)`verify_objects=True` against reference root(/scratch)——保持逐字节不变(must-preserve #1;对称雷见"接受的残余风险")。

## 修复(destination 侧三处一致收窄)

1. destination 读侧:`verify_objects=False`。fixture review 已核实(F-checklist 1):此时 `_normalize_state_index_entry` 只走纯路径运算(不碰文件系统),不再抛 `object_missing`;**保留的校验**= schema_version、JSON 复杂度、payload checksum、entry 上限、必填字段、source_id 归一、URI 安全、身份与 state_id 唯一性;`state_snapshot_index_unreadable`/`not_object` fail-closed 不变。(注:该函数不含 lineage 校验,勿虚构。)
2. checkpoint 拷贝循环:遍历**"胜出并进入 merged 的 source entry"**——精确定义:`entry for key, entry in source_entries.items() if merged.get(key) == entry`(fixture review F2)。**不是**裸 `source_entries.values()`:对象 key 是身份元组的纯函数(同一身份 = 同一 `state_uri`),若为在 merge 冲突中**败北**的 source entry(`current_created > source_created` 时 merged 保留 destination entry)拷对象,会把 source 字节覆盖到共享对象上而 index 记录的是 destination entry 的 checksum → index↔对象永久失洽,且改动 3 关掉了 publish 兜底。`allow_replace` 判定式原样保留(收窄后:`authoritative_runs is not None` 时对胜出 source entry 恒真,`is None` 时恒假——与改前对同一批 entry 取值逐一相同)。evidence counts(`checkpoint_*_count`)随之指本次胜出集;**既有测试断言会变**(见 Evidence mapping,F5)。
3. publish:`verify_objects=False`(仅 merge 内这一个调用点实参;函数默认值与其他调用点不动,must-preserve #8)。保留:size 上限、payload checksum、身份唯一性、结构校验。新增 entry 对象完整性由 `_copyback_state_checkpoint` 的 source 读取 checksum 校验 + 写后读回比对(`:2057-2090`)保证,零回退。

**为何不是"剪枝 index"**:调度器/refresh 均按 split-root 用 /scratch 解析对象(`docs/runbooks/current-production-ops.md:369-375`),NFS 对象仅是 14 天滚动镜像;历史 entry 留在 index 中对所有已知消费者无害(fixture review checklist 7c 已核实调度器读路径确实以 /scratch 解析对象),剪枝是另一份契约决策(follow-up,见 Non-goals)。

## 恢复路径(必需,非可选)

修好 merge 后,072000 的 36 条 entry 仍不会自然进入 NFS index:其 copyback 已记 failed 终态,不会重试;而链死 → 再无新 `state_save_qc` 事件。**无 replay 则修复不收敛。**

新 script `scripts/scheduler_state_index_copyback_replay.py`(实现细节按 fixture review F3/F4/F6/F7/F9/F11/F13 钉死):

- **根与路径**:只暴露两个根——`--reference-root`(默认 env `OBJECT_STORE_ROOT`)与 `--destination-root`(默认 env `NHMS_OBJECT_STORE_COPYBACK_ROOT`);两份 index 路径固定派生为 `<root>/scheduler/state-index/index-last.json`,**不**单独暴露 index 路径旗标;复刻 `run_tree_copyback.py:44-60` 的 root 相等/重叠守卫(`object_root == target_root` 拒绝、`_paths_overlap` 拒绝)。prefix 默认 env `OBJECT_STORE_PREFIX`。
- **选择**:`--run-ids`(逗号分隔)或 `--cycle`(**可重复**)二选一。`--cycle` 解析:遍历 **source index** entries,匹配 `entry["cycle_id"]`(**平铺顶层可选字段**,可能为 None,须跳过;**没有** `lineage` 子对象)收集 `entry["run_id"]`。输入归一:cycle 串按生产 `cycle_id_for` 规则归一(source 段小写,如 `ifs_2026072000`;`workers/data_adapters/base.py:46-48`),工具对用户输入做同样小写归一以免大小写空匹配。
- **空解析 fail-closed**(F9):解析结果为空集合、或解析到的 run_id 在 source index 中无对应 entry → **非零退出 + 结构化原因,不得调用 merge、不得写 index**(merge 对空 authoritative 集是"静默成功只刷 generated_at",必须在工具层挡住)。
- **dry-run 机制**(F6):默认 dry-run;dry-run **不调用** merge(merge 无 dry-run 形参且在锁内无条件 publish)——只读加载两份 index(`read_provider_snapshot` 只读),做集合预览:解析出的 run_ids、source 中命中的 entry、其中 destination 缺失的 entry 数;receipt 标注 `mode=dry_run` 且为**advisory preview**(不模拟 merge 冲突语义)。语义口径:"index 内容不变 + 无对象拷贝"(锁文件/目录确保类副作用不在禁止面)。`--enforce` 才调用真实 merge(同一代码路径)。
- **receipt**(F7):落盘根由 env `NHMS_SCHEDULER_COPYBACK_REPLAY_RECEIPT_ROOT` 指定(enforce 模式必需;目录 0700,沿用 refresh receipt 纪律),文件含 `schema_version` 常量、mode、解析 run_ids、entry 前后计数、copied/reused/replaced、时间戳。
- **执行身份**(F11):必须以 provider 属主(node-22 上为 `frd_muziyao`)执行——provider 锁要求锁父目录 `st_uid == geteuid()`、CAS 替换要求 preimage uid 匹配(`provider_atomic.py:209-210/:297-298`),其他身份会以不透明 fail-closed 失败。写入 runbook。
- 边界:仅 state-index;不触 journal、不触 registry/canonical-readiness、不改任何 pipeline 行。

## Must-preserve(不得回退)

1. source 侧校验逐字节不变:`:1923-1932` 对 source index **全量 entry(authoritative 过滤前)** `verify_objects=True` against reference root——坏 source entry 依然进不来。spec delta 口径与此一致(F1)。
2. merge 冲突语义逐字节不变(`:1977-2001`:authoritative 覆写、later created_at 胜、同时刻不同字节 fail-closed)。
3. `_copyback_state_checkpoint` 的 checksum 校验、containment、no-follow 原子写、写后读回不变。
4. destination 结构校验 fail-closed 不变(`unreadable`/`not_object`)。
5. 锁 + preimage CAS 语义不变(`provider_destination_lock`、`expected_preimage`;replay 与 refresh 经同一 `.index-last.json.lock` 互斥,fixture review checklist 7d 已核实)。
6. **不复活 + 不为败北者拷对象**:不在本次**胜出** source 集中的 entry,其对象一律不读不写(新回归锁,含 F2 的败北 source entry 场景)。
7. refresh 续期(`scripts/scheduler_file_provider_refresh.py:720-725`)与调度器读取路径零改动。
8. `publish_state_snapshot_index` 其余调用点行为不变:`:1617` 续期与 refresh `:984` 均依赖默认 `verify_objects=True` ——**不改函数默认值**,只改 merge 内这一个调用点实参。
9. **发布集必须仍是全量 merged**(F10):"收窄"只作用于对象校验与 checkpoint 拷贝,`publish` 的 entry 列表仍为 `merged` 全集(destination 历史 ∪ 胜出 source)。误把发布集收窄到 source 集 = canonical index 从 1645 条被削到 36 条、全部 warm-start 历史灭失(不可逆)。回归锁:`published_entry_count == destination_count + net_new_count`。

## Seams under test

- `merge_state_snapshot_index_copyback` destination 侧三处收窄(`state_manager.py:1958-1967/2003-2015/2016-2025`),胜出集定义(F2)。
- replay script 入口(双根派生 + 守卫、cycle 归一与解析、空集 fail-closed、dry-run 只读预览、enforce+receipt)。
- 消费面:`run_tree_copyback.py:97` 唯一生产调用点(**总是**传 `unique_run_ids`,`None` 分支无生产调用者),签名不变;`checkpoint_*_count` 进 pipeline_event details(`chain_forecast_execution.py:658-664`)与 `tests/test_run_tree_copyback.py` 断言(F5,需同步更新)。

## 判定表(核心)

| destination 历史 entry 对象 | source 新 entry 对象(/scratch) | 结果 |
|---|---|---|
| 在 | 校验通过 | merge + copy 新 entry(现状语义) |
| **已归档(缺)** | 校验通过 | **merge 成功;历史 entry 原样保留进发布集;其对象不复活(本次修复)** |
| 任意 | 缺/checksum 不符 | fail-closed(source 侧全量校验 `:1923-1932` 不变) |
| destination index 损坏(非 JSON/非 object) | 任意 | fail-closed(不变) |
| 任意 | source entry 在冲突中**败北**(destination created_at 更晚) | 发布 destination entry;**败北 source entry 的对象不拷**(F2 新锁) |

## Evidence mapping

- 单测:
  - `tests/test_state_manager.py`(或就近新文件):红前——destination 含对象缺失的历史 entry + source 新 entry,merge 今日抛 `state_snapshot_index_object_missing`;修复后 merge 成功、仅胜出新 entry 对象被拷、历史 entry 原样进发布集(entry_count 守恒断言,must-preserve #9)、**已归档对象未被复活**(目标路径仍不存在,负测锁)、**败北 source entry 对象未被拷**(F2 负测锁);幂等重放(二次 merge counts 全 reused/entries 不变);must-preserve #8 锁(其他 publish 调用点行为不变)。注意:**全仓现无任何测试直接调用 merge**,既有回归锁全在 `tests/test_run_tree_copyback.py`。
  - `tests/test_run_tree_copyback.py`(F5,**必须**列入验证门):两处既有 counts 断言随胜出集语义同步更新(`test_state_index_copyback_merges_split_root_checkpoint_only_in_private` 的 `checkpoint_reused_count` 1→0;`test_state_index_copyback_ignores_derived_entry_evidence_for_same_identity` 同类),并补"destination-only entry 不被读写"正断言。
  - replay script 单测:cycle 小写归一 + 平铺 `cycle_id` 解析(None 跳过)、空解析非零退出且零写、dry-run 零 index/对象变更、enforce 幂等、receipt 字段与 0700 纪律、root 相等/重叠守卫。
- 实机(tasks 4.x,node-22,以 `frd_muziyao` 执行):replay dry-run → enforce `--cycle gfs_2026072000 --cycle ifs_2026072000`(小写)→ NFS index entry_count 以 receipt 前后计数为准(**预期 +36**;若偏离先读数再分支)+ receipt 在案;随后自然 pass:072000 verdict complete、072012 候选提交;**验收(用户裁定口径)**:连续两个完整 warm-start pass;同时下一次自然 `state_save_qc` copyback 成功(journal 无新 `OBJECT_STORE_COPYBACK_STATE_INDEX_FAILED`)。

## 接受的残余风险(具名)

- NFS index 中 574 条历史 entry 的对象长期缺位于 NFS(对象在 27 侧归档区):对所有已知消费者无害(split-root 解析),但任何**未来**新增的"按 NFS 根校验全量 index"的消费者会踩同一坑——由 follow-up(index 归档标记/剪枝契约)治理,本次只除当前雷。
- **对称雷(F14,显式接受)**:merge 的 source 侧仍对 /scratch 全量 source entry(现 1681 条)`verify_objects=True`,且 index 永不剪枝——/scratch 任何一个老对象丢失/purge 会以同样方式(`state_snapshot_index_object_missing`)再次锁死唯一写者。当前前提成立(每日 refresh 以 verify_objects=True 对 /scratch 全量校验且成功)。保持不变是 must-preserve #1 的代价;监控/剪枝归 follow-up。
- replay 工具信任 /scratch index 为 source of truth(与生产 copyback 同一信任模型);/scratch 对象若损坏,checksum 校验 fail-closed(不变)。

## Non-goals

- 不剪枝/不标记 NFS index 历史 entry(follow-up 契约决策)。
- 不改 node-27 mover、不改 refresh、不改 run-tree 先行的 copyback 顺序(IO 浪费点,follow-up)。
- 不做 copyback 连续失败告警面(follow-up,与 #1186 观测性同族)。
- 不改调度器 index 读取拓扑(NFS canonical 维持)。
- 不给 merge 函数加 dry-run 形参(dry-run 在工具层只读实现)。

## Risk packs

- Selected: persistence-compat(index/对象零意外变更 + entry_count 守恒)、production-parity(node-22 replay + 双 pass live receipt)、state-machine-invariants(merge 冲突语义锁 + 胜出集边界)。
- Not selected: security(无新权限面;replay 走既有 containment/锁/属主纪律)、perf(拷贝范围只减不增)——理由:改动纯粹收窄既有 IO。
