# Design — clone-lineage admission predicate convergence

## Risk triage

- **Fixture level: `expanded`**（round 1 三个 reviewer 席位）。三个 issue 各自规模 S，合起来动的是
  **一条跨两个持久化面的读侧准入契约** + 一条写侧 fail-closed 闸。上调到 `high` 的条件（不成立）：
  改写存量数据、改 spec 的血缘定义方向、或触及 #1735 的作用域语义本身——三条都在 non-goals 里。
  下调到 `compact` 的条件（不成立）：改动落在单一模块内且无跨面契约。
  **与 issue 建议级别的偏离**：三个 issue 本身没有 `Suggested fixture level` 字段（它们不是
  `stage-change-pipeline` 0.16.0+ 产出，而是 PR #1738 审查衍生单），所以此处是首次定级，不是偏离。
- **Risk packs selected**：`invariant-state`（血缘承重字段的读写两侧不变量）、`correctness`
  （谓词收敛后的行选择等价性）、`test-evidence`（既有文本形状 oracle 的正负向翻转）。
- **Risk packs not selected**：`security-perf` —— 无鉴权面、无外部输入面；性能侧唯一可测的变化是
  「失败不缓存导致同一 pass 内失败键被重试若干次」，已在 D5 量化为
  `O(models × sources)` 上界且只在**已经出错**的路径上发生，不构成性能风险轴。

## 行号勘误（先于一切）

#1739 / #1740 引用的行号取自未合入的 `feat/issue-1735-lineage-scoped-cycle-completion @ 862d5249`，
master 上已漂移。**以下是 master 上的实际位置**，reviewer 与 implementer 一律以此为准：

| issue 里的引用 | master 实际位置 | 是什么 |
|---|---|---|
| `state_manager.py:884-889`（"DB 面 SQL"） | `packages/common/state_manager.py:934`（函数 `:897-942`） | `get_earliest_clone_row_for_model_source` —— **本次要放宽的那个** |
| —（issue 只提到 `:815` 起的兄弟） | `packages/common/state_manager.py:889`（函数 `:850-895`） | `get_latest_clone_row_for_model_source` —— publisher reader，**保持不动** |
| `state_manager.py:2738-2740` | `packages/common/state_manager.py:3705-3707`（函数 `:3662-3721`） | `_clone_entries_for_model_source` 的文件面过滤 |
| `scheduler_lineage.py:172-177` | `services/orchestrator/scheduler_lineage.py:172-177` | DB 面 `except Exception` |
| `scheduler_core.py:316-330 / 733-738` | `services/orchestrator/scheduler_core.py:341-355 / 754-763` | refresh 与缓存 |
| `scheduler_runtime.py:853-856` | `services/orchestrator/scheduler_runtime.py:872-875` | `db_free_required` 闸 |
| `tests/test_scheduler_lineage.py:591-597` | `tests/test_scheduler_lineage.py:593-600` | 文本形状断言（fingerprint 正向钉在 `:594`） |
| `tests/test_scheduler_backfill.py:2765-2767`（#1740 验收项 2） | `tests/test_scheduler_backfill.py:3024-3025` | `_refresh_db_free_file_providers()` 后 `_lineage_cutover_cache == {}` |

两个 reader 名字只差 `latest`/`earliest`，SQL 里那句 `clone_gate_fingerprint IS NOT NULL` 在两处**逐字
相同**——改错一处不会被任何测试立刻抓住（publisher 侧没有同款文本形状 oracle）。这是本 PR 最容易
出的错，单独列在这里。

## D1 — `clone_gate_fingerprint` 是纯 provenance（#1739 裁定）

**裁定**：`clone_gate_fingerprint` 记录的是「哪一道闸、以什么值放行了这次 clone」，是审计追溯字段；
**血缘准入只认 `cloned_from_model_id`**（且 `<> model_id`）。据此 DB 面
`get_earliest_clone_row_for_model_source` 去掉 `clone_gate_fingerprint IS NOT NULL`。

三条理由：

1. **与 spec 一致**。`fingerprint-gated-state-clone` 的场景 "Absent provenance means no lineage, not an
   error" 把「无血缘」键在 `cloned_from_model_id` 缺失上，**从未**把 fingerprint 列为准入条件。按现行
   spec，文件面的谓词才是合规的那个；收紧文件面等于改 spec 的血缘定义。
2. **与 D4 的方向性一致**。收紧会**拒掉**一行缺 fingerprint 的 clone 行；若它恰是最早那行，`t*` 就会
   **后移**，把模型从更多 cycle 里摘出去——这是 silent-hide 方向（缺口从此不再被报告）。放宽则最多
   留下一个响亮的 stuck gap。`lineage-scoped-cycle-completion` D4 明令禁止前者。
3. **fingerprint 的语义本身不承载存在性**。它回答「凭什么允许 clone」，不回答「这个身份何时出现」。
   把准入闸的证据挪来当存在性证据，是两个问题混用一个字段。

**对 #1738 当期第 3 条顾虑的回应**：放宽确实削弱一道针对半写行的 shadow-proof 守卫。但 shadow-proof
守卫的原始目的（publisher 侧防止 forecast/save-state 行遮蔽 clone 行）在放宽后依然成立——
forecast/save-state 路径两个 clone 字段都不写，剩下的 `cloned_from_model_id` 条件照样拒掉它们的行。fingerprint 那一条在 earliest reader 上只对
「写了 parent 没写 fingerprint」的半写行有区别，而对这种行，D4 要求的正是**留在 scope 里**。

## D2 — 兄弟 reader 保持更严，不跟随（#1739 裁定，用户拍板）

`packages/common/state_manager.py` 的 `get_latest_clone_row_for_model_source` 继续保留
`clone_gate_fingerprint IS NOT NULL`。**这是刻意的不对称，不是遗漏**，理由：

两个 reader 回答**不同的问题**。earliest reader 回答「这个身份何时出现」——存在性问题，答案会被用来把
cycle 摘出打分，答错的代价是静默隐藏缺口，所以取宽口径。latest reader 回答「我刚提交的是哪一行」——
publisher 在同一次写入事务里刚刚亲手写下了那行的 `clone_gate_fingerprint`（`_build_clone_row` 把它作为
必填 `str` 写出），所以要求 non-NULL 是一次**廉价的自检**：读回来没有 fingerprint，说明读到的不是我刚
写的那行。放宽它只会让 publisher 在异常情况下把一行不是自己写的行镜像进文件 state index，没有任何收益。

写在 reader docstring 里，以免下一个人「顺手对齐」。

## D3 — 收敛后两面仍存的分叉：`cloned_from_model_id` 规范化轴的**两种**形状（report, don't fix）

去掉 fingerprint 条件后，两面在 fingerprint 这一轴上完全一致，但**规范化轴仍分叉**：文件面
`_clone_entries_for_model_source` 对 `cloned_from_model_id` 先 `.strip()` 再判定
（该函数内的 `.strip()` 过滤），DB 面判的是原始字节（`get_earliest_clone_row_for_model_source`
的 WHERE 子句）。
该轴上有**两种**遮蔽形状，不是一种——第二种是 round-2 审查才发现的，早先「只剩一条分叉」的措辞是错的：

1. **空白串 parent**：文件面 strip 后判为空、跳过；DB 面两个合取项**都成立**
   （`cloned_from_model_id IS NOT NULL` 为 TRUE，`cloned_from_model_id <> model_id` 也为 TRUE——
   绑定的 `model_id` 不是那串空白），SQL 里没有任何一条拒它，于是**接受**。
   （顺带记明：`IS NOT NULL` 对 `<> model_id` 是**冗余**的——三值逻辑下 `x <> model_id` 为 TRUE
   蕴含 `x` 非 NULL，所以它改不了 WHERE 的真值；见 `tests/test_real_database_integration.py`
   那条用例的 docstring。）
2. **带空白的自指 parent**：`cloned_from_model_id = 'model_a_prime '` 而 `model_id = 'model_a_prime'`。
   文件面 strip 后判定为自指、跳过；DB 面 `cloned_from_model_id <> model_id` 是**逐字**比较（没有
   `btrim`），两串不等，于是**接受**。

后果比装饰性更重，且两种形状一致：这样一行若是最早行，DB 面 earliest reader 会**选中它**，随后
resolver `_from_clone_row`（`services/orchestrator/scheduler_lineage.py`）因
`predecessor_model_id` 为空（形状 1）或 strip 后自指（形状 2）而返回 `None`
→「无血缘」，**即便后面还有一行合法 clone 行**；文件面则跳过它、找到后面那行、给出 `t*`。同一模型两面
不同答案。**证据强度要说清，别把论证说成实证**：两面都**没有**为「带空白」的
`cloned_from_model_id` 建过任何用例——最接近的既有用例是逐字自指（`tests/test_scheduler_lineage.py`
的 `model_a_prime` 自指行），不带空白。文件面的结论由 `_clone_entries_for_model_source` 的 `.strip()`
推出，DB 面的结论由 SQL 谓词与 `_from_clone_row` 推出；本仓无活 PG，node-27 的 `hydro.state_snapshot`
整表为空，CI 的 `real-db-integration` 只跑了 fingerprint 轴那条用例。所以「两面 `t*` 分叉」是一条
**论证充分但无落盘实证**的结论（不是「从未发生过」，是「没有可复现的痕迹」），#2392 落地时应当顺手
补上那次实跑。

这条**不在本 PR 修**：它属于 `cloned_from_model_id` 的规范化轴，不是 #1739 裁定的 fingerprint 轴，
修它要动 SQL 或 DB 约束，属于另一次裁定。本 PR 的义务是：
`_clone_entries_for_model_source` 的 docstring **如实描述收敛后剩下的这两条分叉**（验收项 3 要求
docstring 不再描述已消除的分叉，而不是要求它宣称两面完全一致），并另立 follow-up issue（已立：**#2392**）。
node-27 live receipt 显示现网 `whitespace_only_parent = 0`。

三点必须一并写进 docstring，否则下一个人会拿本 PR 的措辞去改错地方：

1. **本次去掉 fingerprint 条件小幅扩大了这条分叉的暴露面**：在此之前「空白串 parent 且无 fingerprint」
   的行 DB 面根本选不中；之后它也能进入 `LIMIT 1` 的候选，从而可能遮蔽后面那行合法 clone 行。存量为零，
   但方向要如实记。
2. **spec delta 那句 "keyed on `cloned_from_model_id` alone — present, non-empty, and different" 里的
   non-empty，在 DB 面是由 resolver `_from_clone_row` 的 `.strip()` 满足的，不是由 SQL 满足的**。
   合规点落在 resolver 那一层。把这句话当成「SQL 也该加 `btrim(...) <> ''`」去改，会连带改变
   `LIMIT 1` 的选行，属于另一次裁定。
3. **`btrim(cloned_from_model_id) <> ''` 只关掉形状 1**。形状 2 还需要
   `btrim(cloned_from_model_id) <> model_id`。只补 emptiness 那一句的 follow-up 会看起来做完了、
   实际把第二种遮蔽原样留下——这一点必须写进 docstring，因为它正是下一个人最容易漏的半步。

为什么这条不算 #1739 的 in-scope：#1739 验收项 2 的原文是「对**同一行**给出同一答案（含缺 fingerprint
行）」。逐行看，两面对这两种行的答案本来就一致（都不成立血缘）；分歧出在 `LIMIT 1` 的**遮蔽**
层面，不在谓词轴上。

## D4 — 失败不缓存：`resolve_lineage_cutover` 抛异常（#1740 裁定，用户拍板）

**契约变更**：`resolve_lineage_cutover` 保持返回 `LineageCutover | None`，但在**解析失败**时抛
`LineageResolutionError`（新类型，携带 `model_id` / `source_id` / `reason`）。`None` 从此只表示一件事：
**解析成功，该 `(model_id, source_id)` 确实没有血缘**。

选它而不选「返回结果对象」的理由：结果对象要改全部 ~17 处调用点（每个 `is None` 断言变成
`.cutover is None`），而且**调用方仍可以只读 `.cutover`、无视 `.failed`**——那个有损面会原样回来。
抛异常时调用方**没法不处理**：不 catch 就炸，catch 就必须自己决定怎么处理失败。这正是 #1739 教训的
同一条：不要留两个口径，让下一个人挑。

哪些算失败、哪些算「确实无血缘」（这条边界必须钉死，否则又是一次静默塌缩）：

| 情形 | 归类 | 理由 |
|---|---|---|
| `provider is None` | **无血缘**（可缓存） | 进程生命周期内恒定的条件（`DATABASE_URL` 未设 / db-free 无 provider），latch 它是正确行为；`tests/test_scheduler_backfill.py` 的 `test_db_plane_provider_construction_failure_is_remembered` 钉的就是这个 |
| `model_id` / `source_id` 为空 | **无血缘**（可缓存） | 入参问题，重试不会变 |
| provider 两个 accessor 都没有 | **无血缘**（可缓存） | 鸭子类型不匹配，恒定 |
| `get_earliest_clone_row_for_model_source` 抛异常 | **失败** | 瞬时 DB 抖动的入口，正是 #1740 的主因 |
| `get_earliest_clone_row_for_model_source` 返回 `None` | **无血缘**（可缓存） | 读成功、该 pair 没有 clone 行 |
| `clone_lineage_signal` 抛异常 | **失败** | 该方法号称不抛，但契约不能靠它自觉；见下方「确定性入参错误」一条 |
| `clone_lineage_signal` 返回 `status == "blocked"`，reason 为 `state_snapshot_index_missing` | **无血缘**（可缓存） | 见 D4a——这是「从没发布过索引」的正常冷态，不是故障 |
| `clone_lineage_signal` 返回 `status == "blocked"`，reason 为其它（unreadable / malformed_json / not_object / size_limit / 校验或新鲜度失败） | **失败** | 文件面**不抛异常**，而是返回 `{"status": "blocked", "has_lineage": False, ...}`（`clone_lineage_signal` 的 blocked 分支）。不把这些认成失败，文件面就还是今天那个「索引坏了 = 无血缘」的静默塌缩 |
| `clone_lineage_signal` 返回非 Mapping | **失败** | provider 契约被破坏，不是「没有血缘」 |
| 签名 ready 但 `has_lineage=False` | **无血缘**（可缓存） | 正常答案 |
| 签名 ready、有血缘但字段不可解析（predecessor 空 / 时间解析不出 / 自指） | **无血缘**（可缓存） | 已由 D1/#1738 裁定为「这行不成立血缘」，不是读失败 |

**确定性入参错误按失败处理，且刻意不收敛**：`clone_lineage_signal` 的
`_normalize_state_index_source_id(...)`（`clone_lineage_signal` 内）落在它自己的
`try/except StateManagerError`（包住 `_load_index_snapshot(allow_empty=False)` 的那一圈）**之外**，
非法 `source_id` 会直接向上抛，按表落入
「失败」。这与表里「入参为空 ⇒ 可缓存」看似冲突，是刻意的：空 `source_id` 由 resolver 自己在入口挡掉、
不代表配置有错；而一个**非法**（非空但不合规）的 `source_id` 表示有人把一个没经过规范化的 id 喂进了
血缘解析，每次被问到就重试一次、重新 warn 一次，正是它应得的响度——把它缓存成「无血缘」会让这条缺陷
永久静音。代价是该 key 永不收敛，这是明知的选择，不是遗漏。

**这条非法 `source_id` 从哪来（round 2 审查修正，原文说的「调度器配置里的真缺陷」是错的）**：配置面进不来。
config 的 `source_id` 在构造期就过 `normalize_source_id`（`scheduler_runtime_roots.py:538` 的
`_normalize_sources`），不合规在**配置时**就抛，到不了血缘解析。唯一的活路由是
`scheduler_backfill_predecessor.py:141`：它的 `source_id` 取自**持久化 journal 行**里的
`selected_predecessor.source_id`（`:107`），而不是 config。且还要同时满足
`cycle_id` 为空/缺失——`:121-131` 那段在 `declared_cycle_id` 非空时会先调 `cycle_id_for(source_id, ...)`，
非法 `source_id` 在那里就 `ValueError` → `continue`，根本走不到 resolver。所以这条非收敛路径的前提是
**一行被手工改坏的持久化 journal 行：`source_id` 非法且 `cycle_id` 空/缺**。
另外这只在文件面成立：DB 面一个非法 `source_id` 只是 SQL 匹配不到行 ⇒ `None` ⇒ 照常进缓存。

**db-free 面也跟着变**（#1740 的备选方案里点名要评估这一面）：文件面今天靠每 pass 清缓存兜底，但
「索引坏了」在缓存生命周期内同样会被当成无血缘复用。改后它归入失败、不进缓存，行为只会更响亮。

## D4a — 为什么「索引缺失」不是失败

`clone_lineage_signal` 用 `allow_empty=False` 调 `_load_index_snapshot`
（`clone_lineage_signal` 内），而 `_read_payload` 在**索引文件不存在**时就抛
`state_snapshot_index_missing`（`_read_payload` 的两处 raise）。把整个 `status == "blocked"` 一刀切
成失败会踩一个大坑：**一套从未发布过 state-clone 索引的 db-free 部署是健康的，不是故障的**——它只是
没有任何 clone。一刀切后，这种系统的每个 `(model_id, source_id)` 在每 pass 都会抛一次、warn 一次，
永不收敛；而 node-22 的生产 scheduler 恰恰是 db-free 面。那不是「更响亮」，那是把噪声当信号。
（**适用面见下方 D4a-1 的修正**：在 db-free-required 生产面上，这种「从没发布过索引」的部署其实会先被
运行时 preflight 挡住，根本走不到血缘解析——本段描述的噪声场景比原文窄，但裁定本身不变。）

所以分界按 blocker reason 走，不按 `status` 走：

- `state_snapshot_index_missing` ⇒ **无血缘**（可缓存）。「没有索引」与「索引里没有这个 pair」在
  语义上是同一个答案：该模型没有 clone 血缘。
- 其余 reason（读不出来 / JSON 坏 / 不是对象 / 超限 / 校验或新鲜度不过）⇒ **失败**。这些都表示
  「索引本该有内容，但我拿不到」，正是 #1740 要求区分出来的那一类。

reason 从 `_first_state_index_blocker_reason`（`state_manager.py`）取，`clone_lineage_signal`
已经把它放进返回值的 `reason` 字段（blocked 分支填这个 reason，ready 分支恒为 `None`），
resolver 直接读该字段即可，无需新接口。

### D4a-1 — 「`state_snapshot_index_missing` 太宽」的反驳（round 2 审查，已静态核对，未实跑）

审查意见：`state_snapshot_index_missing` 不只覆盖「从没发布过」，也覆盖「索引路径上某一级目录不可达」
（例如 NFS 掉了），于是一次真故障会被这条静默分支伪装成健康冷态。**该意见被推翻**，理由是一条本来
没写进设计、却承重的前置事实：**索引路径本身在任何血缘解析之前，已由 db-free 运行时 preflight 裁决过**。

链路（逐条核对过）：

1. `_lineage_provider`（`scheduler_core.py:739-740`）只在 `config.db_free_required` 为真时选文件面
   provider，否则走 DB repo。也就是说 `clone_lineage_signal` 这条分支在生产里**只在 db-free 模式下可达**
   （`db_free_required` 就是 `scheduler_db_free_required` 的 property，`scheduler_config/config.py:657-659`）。
2. 同一个开关下，`db_free_runtime_preflight()` 把 `_DB_FREE_PATH_SPECS` 逐条送进 `_db_free_path_check`
   （`scheduler_config/config.py:763-769`），而该表里**就有索引本身**：
   `("scheduler_state_index", "NHMS_SCHEDULER_STATE_INDEX", "file")`（`:52`）。
3. `kind="file"` 的判定里，父目录不存在 ⇒ `db_free_required_path_parent_missing`
   （`scheduler_config/db_free.py:307-308`）；叶子文件不存在 ⇒ `db_free_required_path_not_found`（`:347-348`）；
   还有 symlink / 不可读 / 越界 / realpath 失败各自的 blocker。
4. 任一 blocker ⇒ 整个 pass 在取锁前就 `preflight_blocked`，
   `execution_boundary: "db_free_runtime_preflight_blocked"`（`scheduler_runtime.py:668-700`，字面量在 `:698`）。
5. 生产配置的索引路径正是那条 NFS 根下的本地路径
   （`infra/env/compute.scheduler-dbfree.env.example:50`），所以 NFS 掉了就落在第 3 条里。

所以 NFS 类故障的结局是 `preflight_blocked`——比任何 warn 都响，而且根本走不到血缘解析。
另外，publisher 发布时会**自建父目录链**（`_write_state_index_bytes` → `provider_atomic.py:374`
`atomic_write_bytes_no_follow` → `safe_fs.py:133` 的 `_open_parent_dir(..., create=True)`），
所以「合法根下面缺一级中间目录」和「从没发布过」本来就是同一个状态，不是两件事。

**同时必须诚实记一条**：第 3 条里「叶子文件不存在」也会 block。这意味着在 db-free-required 的生产面上，
D4a 开头那句「一套从没发布过索引的部署会每 pass 抛一次 warn 一次」其实**到不了**——那种部署会先被
preflight 更响地挡住。D4a 的裁定依然成立，但它的适用面比原文窄，真正剩下的可达路径是两条：
preflight 与解析之间的 TOCTOU（索引在窗口内被删），以及任何不经 preflight 直接构造
`FileStateSnapshotIndexRepository` 的嵌入方（测试、未来的非 db-free-required 调用方）。
在这两条路径上，「没有索引」与「索引里没有这个 pair」仍是同一个答案，把它判成失败只会在**什么都没坏**
的时候制造一条永不收敛的 warn。这条 reason 之所以可以静默，正是因为它能伪装的那一类故障已经在上游
被更响地挡掉了——这是它安全的**理由**，必须写下来，否则下一个人只会看到「一个过宽的字面量走了静默分支」。

## D5 — 失败不缓存的成本上界，以及为什么不选「每 pass 清缓存」

- **失败不缓存的成本（DB 面）**：同一个 pass 内，一个失败的 `(model_id, source_id)` 会被重试**每次被
  问到的次数**。**这个次数按 cycle 放大，不是「三个消费点各一次」**——`_completion_scope`
  （`services/orchestrator/scheduler_discovery.py`）收的是单个 `CycleDiscovery`、内部逐 model 调
  resolver，而它自己又被 `cycle_completion_status` 与 `lineage_scoped_out_evidence` 逐 cycle 各调一次；
  `scheduler_candidates.py` 同样按 `(model, discovery)` 调。所以上界是
  `O(models × cycles × 车道)`（cycles 已经把 sources 这一维含进去了），不是
  `O(models × sources × 3)`。按 `max_cycles_per_source` 的常见量级，这比「每 pair 三次」高一到两个
  数量级——缓存原本正是把这些吸收成每 pair 一次。**仍然接受**：它只在已经出错的路径上发生，稳态
  （无失败）零额外查询，而被换掉的是一个跨整个进程生命周期的静默错答案。
  （这条式子第一版写成 `O(models × sources × 消费点数)`，漏了 cycles 因子，由 Phase 7 复核纠正；
  留着这句是因为 #1740 验收项 4 要的就是「代价被显式记录」，记错了等于没记。）
- **失败不缓存的成本（db-free 面，逐条记明，#1740 验收项 4）**：文件面的失败不是逐 key 偶发，而是
  **整面同时**——索引一旦坏掉，每个 pair 都 blocked。叠加两条放大器：(1) `_load_index_snapshot` 只在
  成功时写缓存（`_load_index_snapshot` 末尾），失败路径**每次重读并重校验整份索引文件**；(2) D6 不做
  warn 去重。于是索引损坏期间每 pass 产生 `O(models × cycles × 车道)` 次整索引读取与同量 warn 行——
  与上一条同因，同样按 cycle 放大。
  **明确接受这个代价**，三条理由：(a) D4a 已经把最常见也最良性的那一类（索引从未发布）划出失败，
  剩下的都是索引真的坏了——那时 scheduler 已经处于降级态，重复读取的绝对成本远小于「静默按无血缘调度」
  的代价；(b) node-22 的生产 scheduler 是 oneshot timer（`docs/runbooks/current-production-ops.md`），
  一个进程一个 pass，重复只在 pass 内；(c) 备选方案「db-free 面按 pass 记忆失败结果」需要在缓存里再引入
  一种『失败态』条目，而这正是 #1740 要消灭的东西——为一个降级态省几次文件读，换回一个新的状态机分支，
  不划算。**未实测**：models × sources 的真实规模与索引条目数没有取数，上面给的是形状不是绝对值。
- **「每 pass 无条件清缓存」被否的两条代价**（#1740 推荐方案的原文风险，用户已拍板不走）：
  1. DB 面每 pass 每 `(model_id, source_id)` **新增**一次 `get_earliest_clone_row_for_model_source` 查询，
     即便一切正常；
  2. **新使**中途重标定在 DB 面可见——这是语义变更（今天 DB 面一个进程内 `t*` 是稳定的），不是纯 bugfix，
     会把一个「缓存生命周期」问题变成一个「调度期一致性」问题。
- 因此 `scheduler_runtime.py:872-875` 的 `if self.config.db_free_required:` 闸**不动**，
  `_refresh_db_free_file_providers` 也不拆——`tests/test_scheduler_backfill.py:3024-3025` 无须改。

## D6 — 可观测信号（#1740 验收项 3）

`_lineage_cutover_for_model_source` catch 到 `LineageResolutionError` 时打一条结构化
`logger.warning`，字段至少含 `model_id` / `source_id` / `reason`，并返回 `None`（调用侧语义不变）。

- **为什么是日志而不是 pass evidence**：把失败送进 pass evidence 需要改
  `lineage_cutover_for_model_source` 这个 callable seam 的签名，它被
  `scheduler_discovery.py:174`、`scheduler_candidates.py:209`、`scheduler_backfill_predecessor.py:73`
  三处消费；为一条诊断信号改三处消费契约不划算。#1740 验收项原文是「日志或 pass evidence 至少有一处
  信号」。
- **不做去重**：同一 pass 内同一 key 可能打若干条相同 warn。这是已出错路径上的重复，属于**响亮**的一侧；
  加去重缓存则等于把「失败状态」又存回进程里，正好是本 issue 要消灭的东西。
- **落点**：`services/orchestrator/scheduler_core.py` 已 1008 行，超过
  `scripts/governance/audit_repo_entropy.py:123` 的 `STRUCTURAL_FILE_BUDGET_MANDATORY_OVER_LINES = 1000`
  （report-only 审计）。该文件的新增必须压到最小（catch + warn + return，约 5 行）；logger 与异常渲染
  放 `scheduler_lineage.py`。

## D7 — 自克隆闸的位置与形状（#1741）

新 scope 常量 `_SELF_CLONE_TARGET = "self_clone_target"`，闸**排在闸 0（no-reverse-clone）之前**，
紧跟 `_build_audit_context(...)`（`_refuse` 需要 audit context）：

```python
if m0_model_id == m1_model_id:
    return _refuse(audit_recorder, audit_context, scope=_SELF_CLONE_TARGET)
```

- **为什么必须排在最前**：验收项 3 要求「即使 `m1_forcing_mapping_manifest` 合法、gate 输入非退化，
  自克隆仍先被拒」。排序测试因此必须喂**合法的 direct-grid manifest + 非空 bytes**，断言拿到的是
  `self_clone_target` 而不是 `reverse_clone_target_not_direct_grid`——否则这条测试根本没在测排序。
- **比较口径**：逐字 `==`。不做 `.strip()` / 大小写折叠——model_id 在全仓是逐字键（`state_snapshot_id`
  直接拼接它），引入规范化会让这条闸与 id 铸造口径分叉，正是 D3 那类问题。
- **已知限制 1（记录，不修）**：`_refuse` 硬编码 `refusal_code =
  STATE_CLONE_COLD_START_APPROVAL_REQUIRED`，对一个「调用方传错参数」的拒绝语义上不贴切
  （它本意是「需要人工批准冷启动」）。改 refusal code 会动所有既有 refusal 的稳定错误码契约
  （docs §11.3 clause 2），远超本 issue 边界。新 scope 在 audit 记录里已经把原因说清楚。
- **已知限制 2（记录，不修；写在这里是为了不被误读成 #1741 的漏洞）**：逐字比较意味着
  `m0='model_a_prime '` / `m1='model_a_prime'` **不会**被这道闸拒掉。但 #1741 真正要防的伤害——
  把源行**原地覆写**——在这种情况下依然不会发生，两条互相独立的理由：
  1. m1 带空白时，`_build_clone_row` 连 `state_id` 都铸不出来：`state_snapshot_id`
     （`state_snapshot_id`）走 `_safe_path_component`（`_safe_path_component`，
     `_SAFE_PATH_COMPONENT = ^[A-Za-z0-9_.-]+$`）不接受空白字符，直接 `ValueError`，一行都不会写。
  2. m0 带空白时，铸出的 `state_id` 逐字嵌的是**干净的 m1**，而源行是另一个 `model_id` 的行；
     upsert 按 `state_id` 主键落地，写的是新行，不是源行。
  残留物恰好就是 D3 的**遮蔽形状 2**：新行 `model_id='model_a_prime'` 而
  `cloned_from_model_id='model_a_prime '`（`_build_clone_row` 逐字抄 `source_snapshot.model_id`，
  `state_clone.py:738`），DB 面 SQL 会把它放进 `LIMIT 1`、resolver 再把它判掉。那是
  `cloned_from_model_id` 规范化轴（#2392 / D3 注 3）的事，不是这道闸的事——所以
  **不动这道闸的逐字比较**。
- 模块 docstring 的 refusal scope 清单同步新增该 scope，并把计数改对。`packages/common/state_clone.py`
  里写死 "six" 的地方共 **4 处**，不是 2 处，全部要改：`:24`（模块 docstring "The six distinguished
  refusal scopes are"）、`:55`（"adds a seventh scope"）、`:205`（`StateCloneAuditRecorder` docstring
  "across the six refusal scopes"）、`:223`（`StateCloneResult` "the six fix-forward scopes plus
  `STATE_COMPATIBILITY_UNEQUAL`"）。其中 `:223` 还有分类问题：它按「fix-forward scopes + 一个
  recalibration scope」二分，而自克隆 scope 对**两个 transfer_mode 都生效**，塞不进这个二分——改写成
  「与 mode 无关的身份闸 + 六个 fix-forward scope + 一个 recalibration scope」。

## D8 — Must-preserve behavior（回归面）

1. `get_latest_clone_row_for_model_source` 的 SQL **逐字不变**（D2）。
2. `_lineage_cutover_cache` 的值类型保持 `LineageCutover | None`——
   `tests/test_scheduler_backfill.py:2866/2970` 直接 seed `= None`，语义仍是「已解析，无血缘」。
3. `provider is None` 路径不算失败（D4 表）——
   `test_db_plane_provider_construction_failure_is_remembered` 与
   `test_db_plane_provider_is_constructed_once_and_memoized` 必须原样绿。
4. `_refresh_db_free_file_providers` 不拆、`db_free_required` 闸不动——
   `tests/test_scheduler_backfill.py:3024-3025` 原样绿。
5. `fingerprint_gated_state_clone` 的现有调用方**一个都不改**（它们各自已守卫）；
   `tests/test_state_clone.py` / `test_state_clone_recalibration.py` / `test_state_clone_cutover_hook.py`
   已有用例全绿。
6. `is_pre_cutover` 的严格比较（`cycle_time < t*`）与 `lineage_scoped_out_record` 的字段形状不动。

**刻意翻转的既有 oracle（与上面六条相反，必须改，改不到就是漏）**：
`tests/test_scheduler_lineage.py:249-268` 的 `test_clone_lineage_signal_unreadable_index_is_no_lineage`
钉的正是本次要推翻的契约——它喂一份坏 JSON（`{not json` ⇒ reason `state_snapshot_index_malformed_json`，
按 D4a 属于**失败**侧），然后断言 `resolve_lineage_cutover(...) is None`。改后这一行必然抛异常。
连带 `tests/test_scheduler_lineage.py:13` 的模块 docstring（"absent / unreadable provenance is 'no
lineage', never an error"）也过时——`unreadable` 现在是 error。

这是本 PR **两条**被有意打红的既有断言之一。另一条是 master `tests/test_scheduler_lineage.py:594` 的
正向 fingerprint 文本钉（`assert "clone_gate_fingerprint IS NOT NULL" in captured["statement"]`）：
任务 1.1 从 SQL 里去掉那句条件，必然把它打红，任务 1.5 把它翻成负向钉。两条都单独列在这里，
以免混进「回归」里被误判。

`resolve_lineage_cutover` 全仓 15 处调用点的分类（B 项枚举）：生产唯一 1 处
`services/orchestrator/scheduler_core.py:760`（catch 后对调用侧语义不变）；`tests/test_scheduler_lineage.py`
13 处，其中仅 `:249-268` 一处受影响，其余喂的都是 ready / 无血缘 / 无 accessor 的 provider；
`tests/test_scheduler_generation.py:5305` 1 处，走「无血缘」分支，不受影响。

## Seams under test

- `resolve_lineage_cutover(provider, model_id=, source_id=)` —— 两个 plane 共用的鸭子类型 seam；
  本次给它加一条异常出口。
- `_lineage_cutover_for_model_source(model_id, source_id)` —— scheduler 侧缓存 seam，返回类型不变。
- `PsycopgStateSnapshotRepository._fetch_optional` —— 既有的 SQL 文本形状 seam
  （`tests/test_scheduler_lineage.py:579-585` 的 monkeypatch），本次用它做负向钉。
- `fingerprint_gated_state_clone(...)` 的 `StateCloneResult(refused, refusal_scope)` +
  `repository.upsert_state_snapshot` 的零调用断言。

## Evidence mapping

| 验收项来源 | 证据 |
|---|---|
| #1739 裁定留痕 | 本文件 D1 + D2 + D3 |
| #1739 两面同答案 | `tests/test_scheduler_lineage.py` 负向文本钉 + 文件面对称用例（两者都不执行 SQL） |
| #1739 谓词本身的可执行 oracle | `tests/test_real_database_integration.py::test_real_clone_row_readers_disagree_about_a_null_fingerprint_row` —— 真实 PG 上跑两个 reader，同时钉裁定、D2 不对称与 ASC/DESC 排序（tasks 1.8 / EF-4） |
| #1739 docstring 不再描述已消除的分叉 | `_clone_entries_for_model_source` docstring 改写（D3） |
| #1739 live 计数 | `docs/runbooks/receipts/2026-09-15-issue-1739-clone-provenance-count-node27.md` |
| #1740 失败后续 pass 可重解析 | 新回归测试：会抛一次再成功的 fake provider |
| #1740 失败可区分 | `logger.warning` + 新测试断言 `caplog` |
| #1740 决策留痕 | 本文件 D5（成本上界 + 否掉每-pass 清缓存的两条代价） |
| #1740 db-free 无回归 | `uv run pytest tests/test_scheduler_backfill.py -q` |
| #1741 两 mode 拒绝 + 排序 + audit + docstring | `tests/test_state_clone.py` 新增三条 |
