# Design —— ENOENT 非严格兜底的家族级裁定

## 风险分诊

| 轴 | 取值 | 依据 |
|---|---|---|
| Fixture 级别 | **compact** | 零运行时行为改动（D2）。issue 自估 M 建立在「真有教条分裂、需对齐 9 处站点」之上，该前提被 D1 证伪。 |
| 必须保持的行为 | 全部 19 个权威成员的现有裁决逐字不变 | 本 change 不碰实现；`.py` 改动只有注释与 tracker 指针 |
| 被测接缝 | 「调用 `os.path.realpath` 的函数集合」 | D3；形状无关，故重写站点不会让守卫漂移 |
| 选中的 risk pack | spec/doc 一致性、闭合集合完整性 | 本 PR 通篇是规范文本与引用，两者正是它唯一能出的错 |
| 未选 | 并发、性能、安全边界、迁移 | 无运行时行为改动 |

### 继承自 PR #1626 复盘的三条硬约束（本 PR 与它同一家族，且同样通篇是文本）

1. **对 `os.path.realpath` 只写有界断言，永不写全称。** #1626 两次把全称断言写进
   `openspec/specs/`，两次被证伪（嵌 NUL 值 → `ValueError`；相对路径 + cwd 已删除 →
   `FileNotFoundError`）。**本 change 初稿复发了这条**（fixture 评审 F2）：
   delta spec 写了无豁免从句的 "Every function … SHALL"，而合并当天仓里就有三个反例。已改为有界形式。
2. **任何 `#N` / 归档路径 / file:line 落笔前必须现证。** **本 change 初稿也复发了这条**
   （fixture 评审 F7）：承重引用 `scheduler_state_failure.py:1399` 实为 `:1401`
   （`:1399` 是 `if not allowed:`），而本节初稿恰恰自称「每条引用均已逐行打开核对」——
   **那句自述本身是假的**。已改正，并删去该自述，改为把核对结果落在 EF-6。
3. **孤儿指针审计必须双边。** #1626 审了 #1427 那条指针却没 grep #1400，合并即假。
   **本 change 初稿第三次复发**（fixture 评审 F4）：只计划改两份 spec，而全仓另有 5 处
   tracker 语义的活引用。已并入 A-2。
   本 change 的 tracker 终态一律指向 **ADR**，不指向任何 issue——本 PR 会关闭 #1627。

> 三条硬约束我自己写下、又当场违反了三条中的三条，且都是由**人**（fixture 评审席）抓的，
> `openspec validate --strict` 三条一条也抓不到。这一事实本身写进 ADR 的「已知限制」。

## D1 —— 裁定：家族从来只有一条原则，它只是写在没人会去找的地方

issue 的前提是「三套相反教条并存，从未在家族层裁定过一次」。**后半句是对的**——
原则写过两次，但每次都只裁本 lane 并把与邻接 lane 的分歧**记录走而非解决**。
完整论证见 **ADR 0009「背景」**，本节不复述（本 change 初稿在此复述了一份，
两份随后各自漂移：ADR 改了、这里没改，被交叉审查第 2 轮以 P1 抓到）。出处：

> `openspec/changes/archive/2026-08-16-runtime-root-safety-symlink-loop/design.md:41-43`（#1401）
> 「artifact-guard lane 的残留后果面是 verdict 路由口味；本 lane 是 manifest fail-open
> ——**后果面不同，裁决不同**。」

另一半在更早的归档里：

> `openspec/changes/archive/2026-08-10-symlink-loop-errno-detection/design.md:216-218`（#1332）
> 「…and nothing unsafe is admitted (dangling entries fail `is_dir()`/existence checks downstream)」

即：**立场 B 的正当性从来不是「环路无所谓」，而是「下游会挡」**；#1401 不是推翻它，
是发现自己那条腿**没有下游**，于是同一条原则给出相反结论。三个立场是一条原则的三个实例。

### 裁定（唯一口径）——家族层第一次作出

> **被裁决的那条路径，只要在任何据其归一化产物作出的判断被提交之前，
> 会被对内核解引用一次，ENOENT 非严格兜底就可以容忍；否则必须 loop-filtered 复查。**

fail-open 的形状**见 ADR 0009「决策」**（那里写了两种：从未解引用，以及解引用后在动作前失效）。
本节初稿写「有且只有一个」，被 ADR 自己的已知限制 3 反驳——不在此复述，以 ADR 为准。
第一种正是 #1401 的 manifest 腿：把归一化路径写进提交 manifest、据此宣告「产物在此」，全程不 stat。

### 三条从句（析取，任一成立即可容忍）

初稿只给了前两条，而 D2 表实际用了四类理由，**其中三类不落在任何一条从句上**
（fixture 评审 F3）。一个自称唯一口径的裁定若判不了自己的普查表，新站点作者照样读不出
该照哪套写——正是 #1627 要治的病。补足如下，每条都点名它裁决的表行：

1. **下游解引用。** 被裁决的路径在任何据其归一化产物作出的判断被提交之前，
   被探测（`exists()` / `lstat` / `open`），且那里的失败会改变裁决。
   探测点可以是原值而非归一化产物——内核解析使二者在探测能成功的输入上等价
   （`scheduler_preflight.py:634` 探 `path`、`:635` 用 `resolved`；
   `scheduler_state_failure.py:1401` 探 `path`、包含判定用 `_realpath_or_none` 产物）。
   探测确实改变裁决：`scheduler_preflight.py` 的 `SLURM_PREFLIGHT_<FIELD>_NOT_VISIBLE` 分支在 `visible=False` 时返回
   `SLURM_PREFLIGHT_<FIELD>_NOT_VISIBLE` blocker。
2. **可证重合。** 归一化产物与后续动作实际作用的路径，在**任何该动作能够成功的输入**上重合。
   **该从句只对静态输入量化，不覆盖 check-then-act 竞态**（见 D5.2），援引它必须写明前提。
3. **仅作包含基底 / 比较操作数。** 归一化产物只被用作包含判定的基底或身份比对的操作数，
   自身从不承载「该路径存在或可用」的断言；而**被判定的那一方在裁决提交前已被解引用**
   ——安全性到此为止。初稿在此加了一条「可检查性质」兜底，**它对一半输入为假**，
   已由 ADR 0009 从句 3 一节收窄并留档，本节不复述。这正是 #1332 那句话的机制。

## D2 —— 普查：19 个权威成员，逐行归到具名从句，零错位

### 权威怎么取（这一步是本 change 最贵的一课）

issue 的表（9 处未复查 + 2 处已复查）在四周内就烂了：文件搬家两处
（`scheduler_config.py` 拆成包、`basins_package.py` 改名 `basins_package_source_io.py`）、
一处归因错误、以及若干未覆盖站点。**手抄的普查一定漂移**——PR #2440 烧掉三轮审查买回的不变量。

但按**形状**反算同样不够。本轮先写了一个 AST 形状匹配器（`try` 内 strict realpath +
handler 内非 strict 兜底），它：

- 漏掉 `scheduler_config/db_free.py:174` `_db_free_loop_filtered_realpath`——复查在 handler
  **之后的第二个 try** 里；
- 放宽以后又把 `path_modes.py:116` 误判成已复查（匹配到同一函数**另一条臂**的 strict 调用）；
- **漏掉 `services/orchestrator/retry.py::_local_runtime_root_safety`——issue 自己列为立场 A
  头号站点的那一个。**

形状匹配器编码的是作者猜的形状，**仍然是誊写**。真正闭合且形状无关的权威只有一个：

> **调用了 `os.path.realpath` 的 (模块, 函数) 对。**

该集合今天 **19** 个成员（`services/`、`workers/`、`packages/`、`apps/` 四棵树），
由 fixture 评审席独立 AST 复算**逐行一致**。范围今天足够：`scripts/`、`db/`、`infra/`
的真调用点为 0（`scripts/node27_timeseries_budget_preflight.py:39-49` 的 realpath
在字符串字面量内），`tests/` 不发布。

**取键法的闭合性不再靠散文断言，而由守卫强制**（B-7）。本节初稿写过
「全仓无 `from os.path import realpath`、无 `import posixpath`、无 `os.path` 本地别名」，
**中间那半句今天就是假的**——`workers/forcing_producer/file_store.py:6` 就有 `import posixpath`。
那句话是从 fixture 评审席的报告里誊来的，我没有自己测；这是同一条不变量在本 PR 内的
**第八次复发**，而这次被誊写的是我自己派出去的评审员的散文。
实测结论与之相反且更简单：`posixpath.realpath(x)` 仍是 attr 为 `realpath` 的 `ast.Attribute`，
**权威扫描照样取得到**，所以 `import posixpath` 与各类 `os` / `os.path` 别名扩面而不藏事，
守卫不必也不应断言它们不存在。真正击穿取键法的是另外三种形状
（裸名 import、非调用位置的属性引用、`getattr` 动态查找），B-7 断言的正是那三种。

**但权威是按「生产者」取键的，而判据问的是「消费者」**（fixture 评审 F5）。
经 helper 归一化的消费者永远进不了这个集合，今天已有三个：
`db_free.py::_db_free_path_check`（经 `_db_free_loop_filtered_realpath`）、
`scheduler_state_failure.py::_local_artifact_path_is_allowed` 与
`::_local_artifact_allowed_roots`（经 `_realpath_or_none`）。
故本 design 明确：**权威集合是守卫的嗅探边界，不是裁定的覆盖边界**；
ADR 必须把 wrapper 的消费者显式列出。

### 19 个成员的处置（本 SHA 快照；**承重的是 D3 的守卫，不是这张表**）

| 成员 | 立场 | 依据从句 | 解引用/基底证据 |
|---|---|---|---|
| `retry.py::_db_free_selector_allowed_roots` | A 已复查 | — | #1400/#1626 |
| `retry.py::_db_free_selector_path_rejection` | A 已复查 | — | #1400/#1626 |
| `retry.py::_local_runtime_root_safety` | A 已复查 | — | #1401：manifest 腿无下游 |
| `scheduler_config/db_free.py::_db_free_loop_filtered_realpath` | A 已复查 | — | #1626 |
| `scheduler_config/db_free.py::_db_free_allowed_roots_and_blockers` | B | 1 | 下游 blocker 阶梯 |
| `scheduler_config/db_free.py::_db_free_path_identity` | B | 1 | **守卫白名单第 3 项**（无 strict 臂）。**config 提供的那一侧**操作数以 `canonical_root_blocker is None` / `raw_root_blocker is None` 为前提（`scheduler_config/config.py:792`/`:806`/`:818`），blocker 来自 `_db_free_path_check` 的真探针（`_db_free_path_check` 内的 `parent.lstat()`、`path.exists()`、`is_symlink()/is_dir()` 四处真探针）。另一侧是模块常量（`config.py:788-790` 无门调用），**从未被探测**——交叉审查证伪了初稿「两操作数均以真探针为前提」的写法 |
| `scheduler_config/path_modes.py::_resolve_config_path_for_mode` | B | 1 | 8 个 root 字段在使用点被 `open()`/`lstat()` 解引用（D6） |
| `scheduler_preflight.py::_preflight_allowed_roots` | B | **3** | 仅作包含基底；被判定路径在 `:634` 被 `exists()/is_dir()` 探 |
| `scheduler_preflight.py::_storage_root_check` | B | 1 | `:634` `.exists() and .is_dir()`，`:651-661` 据此出 blocker |
| `scheduler_runtime_roots.py::_scheduler_allowed_roots_and_blockers` | B | **3** | 同 `_preflight_allowed_roots` |
| `scheduler_runtime_roots.py::_canonical_parent` | B（裸 `except OSError`） | 1 | 产物进 preflight 路径，`:304` `lstat` |
| `scheduler_runtime_roots.py::_canonical_path` | B（裸 `except OSError`） | 1 | 同上 |
| `scheduler_runtime_roots.py::_optional_config_path` | B（裸 `except OSError`） | **3** | 产物经 `scheduler_config/config.py::_optional_config_path_for_mode` 变成 `allowed_storage_roots`，**只作包含基底**、自身从不被探；被判定的那条路径才被解引用。初稿写从句 1、理由「同上」，指的是 `_canonical_parent` 产物那次 `lstat`——而 `_scheduler_root_check` lstat 的是 `path`，**不是 `allowed_roots`**，是另一个值。实现任务实测后拒绝写这条已被自己证伪的理由，编排者采纳其定性 |
| `scheduler_runtime_roots.py::_require_safe_directory_final_component` | B | 1 | 构造期硬守卫，产物随即被 open |
| `scheduler_state_failure.py::_realpath_or_none` | C（**该 lane 被他处描述为残留，非本函数自述**） | 1 | **实核为 B**：`:1401` `path.exists()`；`job-retry-mechanism/spec.md:1585-1589` 已写成 SHALL |
| `basins_discovery.py::_safe_resolve_under_root` | B | 1 | 下游 containment / `relative_to` |
| `basins_package_source_io.py::_resolve_package_path` | B | 1 | 其余 errno 抛 `BASINS_PACKAGE_PATH_UNRESOLVABLE` |
| `journal_scope_census.py::_require_output_outside_root` | B | **2** | **守卫白名单第 1 项**（无 strict 臂）。D5.2；非 ENOENT-兜底形状，issue 表未收 |
| `shud_preflight.py::check_shud_executable` | B | 1 | **守卫白名单第 2 项**（无 strict 臂）。`_is_stub_basename(real)`（`shud_preflight.py:164`）判 stub 后，该可执行文件随即真的被执行 |

**结论：零错位站点。** 没有要对齐的对象，故本 change 不含运行时行为改动。
立场 C 的措辞按验收项**保留**，ADR 记明它已被实核为立场 B 的实例。

## D3 —— 防复发锚：断言违规者集合为空，不是断言成员清单

**不做**一张 19 行的处置注册表 + 双向 `stale == []` 钉。理由是 PR #2440 的直接教训：
那里的处置表**本身就是规格**（每个键的运维可见行为），而这里的处置是**散文论证**。
一个只检查「每个函数都有一行」的钉，验的是行的**存在**，不是所引裁决**存在、可达、独立**。
行文照样烂，而钉照样绿——正是 C1 抓到的假闭合形状。**绿在漂移上的钉比没有钉更坏。**

守卫改为断言家族的**机械**不变量（#1626 教训 6：断言违规者集合，不要断言成员元组）。
**共两条**，第二条是交叉审查第 1 轮的 P0 逼出来的：

> **断言一**：权威集合中的每个成员，其 `os.path.realpath` 调用里**至少有一处带 `strict=True`**
> （或进具名白名单）。
> **断言二**：权威集合中的每个成员，函数体内写有处置标记——admit 者 `ADR 0009 clause N`，
> 复查者 `ADR 0009 loop-filtered`。无标记者集合为空。

断言二**不是**上面拒绝的那张注册表：注册表与代码分离，站点改名挪走它都不动；
而标记写在函数体内、随函数移动删除，无法与它所标注的站点漂移。
「recorded at the site」本来就是存在性要求，用存在性检查强制它是同义的。
它断言「具名了从句」，不断言「从句选对了」——后者仍是四问设计评审。详见 ADR 0009「守卫」。

**具名白名单恰好三项**（初稿写两项，fixture 评审 F1 实测第三项今天就会让守卫变红）：

| 豁免 | 依据从句 | 理由 |
|---|---|---|
| `journal_scope_census::_require_output_outside_root` | 2 | D5.2 |
| `shud_preflight::check_shud_executable` | 1 | 判 stub 后该可执行文件真的被执行 |
| `scheduler_config/db_free.py::_db_free_path_identity` | 1 | 只比较自身产物；config 侧操作数以真探针出的 blocker 为前提，另一侧是 `config.py:788-790` 无门传入的模块常量（从未被探测，风险低但断言须准）；`:174-177` 自述无拒绝通道 |

白名单三项以外的任何增长都要过评审。守卫抓得住「有人加了第 20 个站点、只写非严格形」，
**抓不住**「作者忘了下游裁决」——后者是设计评审的事，D1 三条从句就是那份评审清单。
这个分工是刻意的，写进 ADR，不留含混。

## D4 —— errno 分流是正交问题，不在本裁定内

issue 把「裸 `except OSError` vs errno 分流」和「兜底要不要复查」混在一处。它们正交。
`scheduler_runtime_roots.py:681-688` 的 D2 反论（「errno 分流一无所获」）只回答前者。

**初稿把裸写法三行的「定性」指向本节，而本节自认与对齐问题正交，等于没回答**
（fixture 评审 F3）。定性已移入 D2 表（三行均依从句 1，证据为 `:304` 的 `lstat`）。
本节只留一句对裸写法本身的评价：**在同一条解引用保证下它更宽而不更险**
（ESTALE/EACCES 也被折叠进兜底），值得一句注释，不值得一次改动。

## D5 —— report-only（按仓规：报告，不修）

1. **`.resolve()` 面共 152 个 (模块, 函数) 对**（同四棵树，经 fixture 评审独立复算相符）。
   本家族的立论是「`Path.resolve()` 在受支持解释器区间内不能当环路谓词用」，
   而该面远超 issue 点名的 `_safe_preserve_final_component` 一处。**本 change 未逐一核过**，
   不下判断，立单路由。（初稿称 `production_closure` 占 94，评审复算为 59 对 / 98 调用点；
   该数字不承重，已删除而非改写。）
2. `journal_scope_census::_require_output_outside_root` 的**判据/动作分离**
   （在 `_require_output_outside_root` 内判 `os.path.realpath(target)`，
   而写入发生在 `census_job_id_scope` 返回之后对同一 `target` 的落盘处）：静态输入上无逃逸，
   fixture 评审逐类试破未果（悬空 symlink、中间组件缺失 + `..`、中间组件 ELOOP、
   父目录含 symlink + 末组件缺失，四类全部方向正确或响亮失败）。
   **但从句 2 只对输入量化，对 TOCTOU 无声**：判定与写入之间隔着
   `census_job_id_scope`，而该文件自述该 census 在 node-22 上「takes minutes」。
   援引从句 2 必须写明前提「单次操作者 CLI、无并发写者」。ADR 已写明。
3. 同一函数内那次 `os.path.realpath(target)` 无保护：`--output` 为相对路径且 cwd 已删除
   → `FileNotFoundError`；含内嵌 NUL → `ValueError`。两者都会在一个自述
   「1 on a typed failure」的命令上留 traceback。既存问题，本 change 零运行时改动，立单。

## D6 —— 验收项要求「单独定性」的两个子族

- **`_safe_preserve_final_component`（`services/orchestrator/scheduler_config/path_modes.py:91-95`）**
  ——**定性已在本 design 内完成，不再条件化给实现任务**（初稿把它挂在 B-4 的结果上，
  fixture 评审 F4 判验收项 4 未满足）。实现为
  `path.parent.resolve(strict=False) / path.name`，`except (OSError, RuntimeError): return path`。
  `≤3.12` 环路抛 errno-less `RuntimeError` 被吞、返回 raw path；3.13+ 不抛、返回折叠值。
  三个调用点（`path_modes.py::_config_path_preserve_final_component_for_mode`、
  `::_config_path_relative_to_preserve_final_for_mode`、以及 `::_confined_path_for_mode` 的兜底臂）
  的产物进 preflight 路径，在 `scheduler_runtime_roots.py:304` 被 `lstat`。
  **按从句 1 正当，不是 fail-open。** 初稿在此多写了一条「跨解释器 blocker 码分歧」的残留，
  **实测证伪**（4 形状 × 3 字段 × 3.11.14/3.13.3，blocker code 全同）：让两条产物分叉的
  充要条件就是父段仍有环存活，故 `lstat` 两边都得 `ELOOP`、同映射为 `UNSAFE_PATH`。
  **该臂在解引用点判据中性，到此为止**——不等于「无残留」：该 preflight 腿确有跨解释器的
  blocker 码与证据载荷差异，归因于它自己的 `Path.resolve(strict=False)`，由 **#2453** 跟踪。
  初稿在此写「该臂无残留」，是把一份 agent 报告放大成了结论；口径以
  `openspec/specs/slurm-array-runner-integration/spec.md` 与 ADR 0009 为准，本节不复述。
- **8 个核心 root 字段经 `_resolve_config_path_for_mode`**：全部有内核级探针
  （`scheduler_preflight.py:634` 的 `.exists()/.is_dir()`，或 `scheduler_runtime_roots.py:304`
  的 `lstat`），但每一条都挂在默认关闭的开关上（`slurm_execution_enabled` /
  `require_runtime_roots`）。这**不**构成错位：`lock_path` 与 `evidence_dir` 在使用点真的落到内核
  （`scheduler_lease.py:252` `os.open`、`:271` `os.stat(follow_symlinks=False)`；
  `scheduler_evidence.py:912` `open_evidence_directory`），phantom 值在那里是响亮的 ELOOP。
  前置探针是**更早、更干净**的裁决，不是**唯一**的裁决——开关关掉的代价是
  「配置错误表现为运行期 ELOOP 而不是 preflight blocker」，属**可诊断性**降级，
  而那正是 `require_runtime_roots` 存在的意义。ADR 记为观察，不记为缺陷。

## 证据映射

| 主张 | 证据 |
|---|---|
| 权威 19 成员、形状无关、取键法闭合 | 实现任务交付的 AST 扫描 + 守卫测试；fixture 评审独立复算逐行一致 |
| 零错位站点 | D2 表逐行从句归属 + file:line；D6 的两处内核解引用 |
| 立场 C 实为 B | `scheduler_state_failure.py:1401` + `job-retry-mechanism/spec.md:1585-1589` |
| 原则早已成文 | `archive/2026-08-16-runtime-root-safety-symlink-loop/design.md:41-43`、`archive/2026-08-10-symlink-loop-errno-detection/design.md:216-218` |
| 守卫今天为绿且能变红 | 实现任务须给出红证据：新增一个只写非严格 realpath 的桩函数 → 守卫失败；删桩 → 恢复绿 |
| tracker 指针双边闭合 | A-2 的全仓 `#1627` grep 结果随 PR body 交付 |

> **本节的行锚已全部改为符号锚。** 初稿写死的 `:576`/`:578`/`:611`/`:617` 四处，
> 被本 commit 自己在 `journal_scope_census.py` 插入的 18 行 docstring 整体下移到
> `:594`/`:596`/`:629`/`:635`——由交叉审查实测发现。这是 ADR 已知限制 1 教训 (b)
> 「同文件内的行锚会被本次写入自己作废」的第五次复发，也是它第一次发生在
> **专门警告这件事的那份文档自己的 fixture 里**。
