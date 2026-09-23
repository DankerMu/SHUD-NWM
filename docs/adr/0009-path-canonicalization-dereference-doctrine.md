# ADR 0009 — 路径归一化：ENOENT 非严格兜底的容忍以「解引用」为条件

- 状态：Accepted
- 日期：2026-09-17
- 关联：issue #1627（家族级裁定单，本 ADR 即其交付物）

## 背景

仓内有一条反复出现的代码形状：

```python
try:
    return Path(os.path.realpath(value, strict=True))
except OSError as error:
    if getattr(error, "errno", None) == ENOENT:
        return Path(os.path.realpath(value))      # 非严格兜底
    ...
```

非严格 `os.path.realpath` 对 `<不存在>/../<环路>` 这一输入类不抛异常，而是返回一个
**部分解析**的产物——环路被词法折叠进了结果。于是「兜底产物要不要再做一次严格复查」
成了每个站点各自的选择，而仓内并存三种写了理由的相反做法：

- **立场 A（复查）**：`services/orchestrator/retry.py::_local_runtime_root_safety` 等。
- **立场 B（有意容忍）**：`services/orchestrator/scheduler_preflight.py::_preflight_allowed_roots` 等。
- **立场 C（已裁定并接受为残留）**：`services/orchestrator/scheduler_state_failure.py::_realpath_or_none`。

issue #1627 据此判断「家族层从未裁定过一次」，要求补一次裁定。

### 前提对了一半：原则写过两次，家族级裁定确实从未作出

issue 说「从未在家族层裁定过一次」——**这句是对的**。
原则本身被写下过两次，但**每一次都只裁自己那条 lane，并且把与邻接 lane 的分歧记录下来、
而不是解决它**。其中一处出处的小标题就明写「与 #1402/preflight 家族先例**显式分歧**」，
段末是「家族分歧具名入 PR **偏离记录**」：

> `openspec/changes/archive/2026-08-16-runtime-root-safety-symlink-loop/design.md:41-43`
> 「artifact-guard lane 的残留后果面是 verdict 路由口味；本 lane 是 manifest fail-open
> ——**后果面不同，裁决不同**。」

另一半在更早的归档里，它说明了立场 B 的正当性**从来不是**「环路无所谓」：

> `openspec/changes/archive/2026-08-10-symlink-loop-errno-detection/design.md:216-218`
> 「…and nothing unsafe is admitted (dangling entries fail `is_dir()`/existence checks downstream)」

即：容忍之所以成立，是因为**下游会挡**。立场 A 不是推翻这条原则，而是发现自己那条腿
（提交 manifest）**没有下游**，于是同一条原则给出了相反结论。三个立场是一条原则的三个实例。

**所以本 ADR 做的是两件事，不是一件**：把这条已经存在的原则搬到新站点作者会看见的位置；
以及**首次在家族层把它裁定下来**——给出可判定的判据、对全部在管站点逐一定性、
并留一条机械守卫防止复发。前一件是转述，后一件此前没有人做过，
两次写下原则的那两份 design 都明确地把它推给了偏离记录。

## 决策

> **被裁决的那条路径，只要在任何据其归一化产物作出的判断被提交之前，
> 会被对内核解引用一次，ENOENT 非严格兜底就可以容忍；否则必须 loop-filtered 复查。**

fail-open 有**两种**形状：

> **形状一**：判据建立在归一化字符串上，而它代表的对象**从未**被解引用。
> **形状二**：对象被解引用过，但裁决在动作发生前已经失效（check-then-act 竞态）。

形状一正是 #1401 的 manifest 腿——把归一化路径写进提交 manifest、据此宣告「产物在此」，全程不 stat。
形状二不由本裁定消除，而是由从句 2 的**前提**排除在外（见已知限制 3）：
援引从句 2 的站点必须写明「无并发写者」，前提失效则该站点必须改依从句 1 或改为复查。

### 三条从句（析取，任一成立即可容忍）

1. **下游解引用。** 被裁决的路径在任何据其归一化产物作出的判断被提交之前，
   被探测（`exists()` / `lstat` / `open`），且那里的失败会改变裁决。
   探测点可以是**原值**而非归一化产物——内核解析使二者在探测能成功的输入上等价。
   实例：`scheduler_preflight.py:634` 探 `path`、`:635` 的包含判定用 `resolved`，
   而 `visible=False` 确实改变裁决（`:652-661` 出 `SLURM_PREFLIGHT_<FIELD>_NOT_VISIBLE`）。
2. **可证重合。** 归一化产物与后续动作实际作用的路径，在**任何该动作能够成功的输入**上重合。
   **本从句只对输入量化，不覆盖 check-then-act 竞态**——援引它必须写明
   「单次操作者 CLI、无并发写者」这个前提（见「已知限制」3）。
3. **仅作包含基底 / 比较操作数。** 归一化产物只被用作包含判定的基底或身份比对的操作数，
   自身从不承载「该路径存在或可用」的断言；而**被判定的那一方在裁决提交前已被解引用**
   ——安全性到此为止就够了，不需要第二条性质。

   **本从句的安全性只依赖「被判定的那一方被解引用」，不要再给它加一条兜底性质。**
   有一条听起来可检查的性质专门在这里勾人——「真实对象的严格解析产物不可能以一个
   含缺失分量或环路的字符串为前缀」——**它对一半输入为假**：`<base>/missing/../real`
   的非严格兜底折回 `<base>/real`，而那是一个**真实目录**；`..` 把缺失分量消掉了，
   被 admit 的基底已不再含缺失分量，`<base>/real/obj` 的严格产物确实以它为前缀。
   该性质只在**折叠后仍留有环路或缺失分量**时成立；`..` 折掉缺失分量的那一半里它是空真，
   而那一半的基底本就是一条真实路径，包含判定退化为普通包含判定。

本裁定需要第三条从句，才判得了自己的普查表：只作包含基底 / 比较操作数的站点既不落在
从句 1 上（产物自身从不被探），也不落在从句 2 上（它不与任何后续动作的作用路径重合）。
一个判不了自己普查表的「唯一口径」，新站点作者照样读不出该照哪套写。

### 新站点作者该怎么用（本 ADR 的主要用途）

写下一个归一化站点时，按顺序回答四个问题：

1. 我的 `os.path.realpath` 调用里有没有 `strict=True`？**没有就得进具名白名单并写明理由**——
   非严格形不能当环路谓词，`Path.resolve()` 同样不能（见「权衡」）。
2. 被裁决的路径，在被据以作出任何判断之前，会被 `exists()` / `lstat` / `open` 碰一次吗？
   **会 → 从句 1，容忍合法**，把这句理由写进注释。
3. 不会，那么归一化产物与我后续真正动手的那个路径，在动作能成功的输入上重合吗？
   **重合 → 从句 2**，把重合的论证**和并发前提**一起写进注释。
4. 都不是，那么它是不是只当包含基底 / 比较操作数，而被判定的那一方被解引用了？
   **是 → 从句 3。**
   **四条都不成立 → 必须 loop-filtered 复查**：兜底产物再做一次严格解析，
   只有再次严格解析仍 ENOENT 或干净解析才保留 admit。

## 普查

权威取「**调用了 `os.path.realpath` 的 (模块, 函数) 对**」，覆盖 `services/`、`workers/`、
`packages/`、`apps/` 四棵树。选它而不选代码形状，是因为形状匹配器编码的是作者猜的形状：
「`try` 内 strict + handler 内非 strict」这一形漏掉 handler 之后才复查的写法；
放宽之后又会把同一函数另一条臂的 strict 调用误判成复查，连 issue 自己列为立场 A
头号站点的 `retry.py::_local_runtime_root_safety` 都漏在集合外。按调用点取集合则形状无关：站点被重写、
errno 分流被增删、兜底被挪进 helper，集合都不动，只有**新增或删除站点**才动。

该集合当前 **19** 个成员。逐行处置如下——这是**本 SHA 的快照，供读者对照**；
承重的是下面那条守卫与各站点自己的从句标记，不是这张表。
表内联于此、而不是留在 change 目录里由 ADR 指过去：ADR 是长期件，change 目录会被归档，
指过去就是一条要等归档才存在、且依赖日期 slug 恰好正确的死指针。

| 成员 | 立场 | 依据从句 | 解引用/基底证据 |
|---|---|---|---|
| `retry.py::_db_free_selector_allowed_roots` | A 已复查 | — | #1400/#1626 |
| `retry.py::_db_free_selector_path_rejection` | A 已复查 | — | #1400/#1626 |
| `retry.py::_local_runtime_root_safety` | A 已复查 | — | #1401：manifest 腿无下游 |
| `scheduler_config/db_free.py::_db_free_loop_filtered_realpath` | A 已复查 | — | #1626 |
| `scheduler_config/db_free.py::_db_free_allowed_roots_and_blockers` | B | 1 | 下游 blocker 阶梯 |
| `scheduler_config/db_free.py::_db_free_path_identity` | B | 1 | **守卫白名单第 3 项**（无 strict 臂）。**config 提供的那一侧**操作数以 `canonical_root_blocker is None` / `raw_root_blocker is None` 为前提（`services/orchestrator/scheduler_config/config.py:792`/`:806`/`:818`），blocker 来自 `_db_free_path_check` 的真探针（`_db_free_path_check` 内的 `parent.lstat()`、`path.exists()`、`is_symlink()/is_dir()` 四处真探针）。另一侧是模块常量（`services/orchestrator/scheduler_config/config.py:788-790` 无门调用），**从未被探测** |
| `scheduler_config/path_modes.py::_resolve_config_path_for_mode` | B | 1 | 8 个 root 字段在使用点被 `open()`/`lstat()` 解引用（见「已知限制」4） |
| `scheduler_preflight.py::_preflight_allowed_roots` | B | **3** | 仅作包含基底；被判定路径在 `:634` 被 `exists()/is_dir()` 探 |
| `scheduler_preflight.py::_storage_root_check` | B | 1 | `:634` `.exists() and .is_dir()`，`:652-661` 据此出 blocker |
| `scheduler_runtime_roots.py::_scheduler_allowed_roots_and_blockers` | B | **3** | 同 `_preflight_allowed_roots` |
| `scheduler_runtime_roots.py::_canonical_parent` | B（裸 `except OSError`） | 1 | 产物进 preflight 路径，`_scheduler_root_check` 的 `path.lstat()` |
| `scheduler_runtime_roots.py::_canonical_path` | B（裸 `except OSError`） | 1 | 同上 |
| `scheduler_runtime_roots.py::_optional_config_path` | B（裸 `except OSError`） | **3** | 产物经 `services/orchestrator/scheduler_config/path_modes.py::_optional_config_path_for_mode` 变成 `allowed_storage_roots`，**只作包含基底**、自身从不被探；被判定的那条路径才被解引用——`_scheduler_root_check` lstat 的是 `path`，**不是 `allowed_roots`**，是另一个值 |
| `scheduler_runtime_roots.py::_require_safe_directory_final_component` | B | 1 | 构造期硬守卫，产物随即被 open |
| `scheduler_state_failure.py::_realpath_or_none` | C（**该 lane 被他处描述为残留，非本函数自述**） | 1 | **实核为 B**：`:1401` `path.exists()`；`openspec/specs/job-retry-mechanism/spec.md:1585-1589` 已写成 SHALL |
| `basins_discovery.py::_safe_resolve_under_root` | B | 1 | 下游 containment / `relative_to` |
| `basins_package_source_io.py::_resolve_package_path` | B | 1 | 其余 errno 抛 `BASINS_PACKAGE_PATH_UNRESOLVABLE` |
| `journal_scope_census.py::_require_output_outside_root` | B | **2** | **守卫白名单第 1 项**（无 strict 臂）。见「已知限制」3；非 ENOENT-兜底形状，issue 表未收 |
| `shud_preflight.py::check_shud_executable` | B | 1 | **守卫白名单第 2 项**（无 strict 臂）。`_is_stub_basename(real)`（`shud_preflight.py:164`）判 stub 后，该可执行文件随即真的被执行 |

**全部 19 个成员**都在自己的函数体内写有处置标记：15 个走 admit 的写
`ADR 0009 clause N`，4 个立场 A（表中 `A 已复查`，本就做 loop-filtered 复查、不走非严格 admit）
写 `ADR 0009 loop-filtered`。**立场 A 成员同样要写标记**——义务是「记录处置」而不是
「记录从句」，因为「谁在 admit」是对 handler 形状的判断，而本家族的全部教训就是
形状推断编码的是作者猜的形状，守卫不该去猜。**立场 A 不是豁免**：一个写了 loop-filtered
站点却不写标记的新作者，会在合并门上变红，而这正是守卫断言二想要的行为。
四棵树外 `scripts/`/`db/`/`infra/` 的真调用点为 0、`tests/` 不发布。
（一句 naive grep 会在这里打脸：`scripts/node27_timeseries_budget_preflight.py` 有三行
`os.path.realpath(...)`，但它们整段落在 `_IMPORT_ORIGIN_PROBE` 这个字符串字面量里，
AST 上不是调用点。这条反证写在这里，是因为没有它，下一个作者会以为这句断言是假的。）
**取键法的闭合性由守卫自己强制，不靠散文断言**：按 attribute 名取键只在
`realpath` 无法经裸名到达时闭合，故守卫的**取键法闭合断言**（`test_no_call_shape_defeats_the_attribute_name_key`）
把「击穿取键法的构造集合」也断言为空，覆盖裸名 import、非调用位置的属性引用、
`getattr` 动态查找三种形状。它与下面的断言一、断言二并列，是第三条家族级断言。
`import posixpath` 与各类 `os`/`os.path` 别名**不在**其中——它们产生的仍是
attr 为 `realpath` 的 `ast.Attribute`，扩面而不藏事。
「全仓无 `import posixpath`」**不是**这里的前提，也不成立
（`workers/forcing_producer/file_store.py:6` 今天就有一个）——闭合性不靠这个前提。

### 权威集合是守卫的嗅探边界，不是裁定的覆盖边界

权威按**生产者**取键，而判据问的是**消费者**。经 helper 归一化的消费者永远进不了这个集合。
今天有三个，它们同样受本裁定约束，必须在此具名：

| 消费者 | 经由 | 依据从句 |
|---|---|---|
| `scheduler_config/db_free.py::_db_free_path_check` | `_db_free_loop_filtered_realpath`（立场 A，已复查） | — |
| `scheduler_state_failure.py::_local_artifact_path_is_allowed` | `_realpath_or_none` | 1 |
| `scheduler_state_failure.py::_local_artifact_allowed_roots` | `_realpath_or_none` | 1 |

其中 `_db_free_path_check` 是前序 PR 明文派给 #1627 的义务
（`openspec/changes/archive/2026-09-02-journal-root-realpath-and-job-id-scope-census/design.md:152-155`
指向 `openspec/specs/runtime-evidence-and-operations/spec.md` 的 requirement
「DB-free scheduler config path adjudication survives symlink loops」）。
该义务实质满足——它经由的 helper 本身就是立场 A——列在这里是为了从裁定文本里**追溯得到**它。

**结论：零错位站点。** 三个立场在今天的 master 上全部落在三条从句之一上，
没有要对齐的对象，故本次裁定不含任何运行时行为改动。

立场 C 的措辞按 issue 验收项「保留或改写，二选一并说明」**保留**，但须记明：
它已被实核为**立场 B / 从句 1 的一个实例**——`scheduler_state_failure.py:1401`
的 `path.exists()` 紧随 admitted 之后，且 `openspec/specs/job-retry-mechanism/spec.md:1585-1589`
已把这条写成 SHALL（null-reason absent verdict 只在实际探过存在性之后产生）。
「已裁定并接受为残留」这个自述在今天偏严。

## 守卫

守卫有**三条**家族级断言，都是「违规者集合为空」形，都不断言成员清单。
前两条管成员，第三条管取键法本身的前提——它们**刻意分开**：树里出现一个裸名调用时，
断言一照样绿（那正是洞），闭合断言变红（那正是抓）：

> **断言一**：权威集合中每个成员的 `os.path.realpath` 调用里，至少有一处带 `strict=True`
> （或进具名白名单）；违规者集合为空。
> **断言二**：**权威集合中的每个成员**，函数体内写有处置标记——admit 者
> `ADR 0009 clause N`，复查者 `ADR 0009 loop-filtered`；无标记者集合为空。
> **断言三（取键法闭合）**：击穿 attribute 名取键的构造——裸名 import、非调用位置的
> 属性引用、`getattr` 动态查找——集合为空。它守的是上面「普查」一节那条前提，
> 使之不再是一句散文。

### 断言二：为什么标记不是被否决的那种「分离注册表」

规范正文要求所依从句「recorded **at the site**」。**一条只以散文形式存在的 SHALL，
没有任何东西能让它变红**——它会在交付它的那次提交里就被大面积违反而无人察觉，
那是本 ADR「权衡」一节谴责的「绿在漂移上的钉」的对偶形态。断言二就是这条 SHALL 的强制形式：
写下一条 SHALL 时，同一次提交里就要给它一条能变红的断言。

被否决的备选是一张 19 行的处置注册表加双向 `stale == []` 钉；区别在**标记住在哪**：注册表是一张与代码分离的表，
站点被重写、改名、挪走，表都不动，于是它验证的是「行存在」而非「裁决仍然成立」；
而从句标记写在函数体内，**随函数移动、随函数删除**，无法与它所标注的站点漂移。
「at the site」本来就是一个存在性要求，用存在性检查强制它是同义的，不是降格。

**标记断言的是「具名了从句」，不是「从句选对了」。** 后者仍然是上面四个问题那份设计评审，
守卫对它无话可说——这一点写进了规范正文，不留含混。

断言二有两条收紧：标记必须是**真正的注释**
（按 `tokenize.COMMENT` 判定，docstring 或字符串字面量里提到这个 token 不算——
纯文本正则实测可被一句 docstring 散文绕过）；以及**同一限定名被绑定多次时直接判红**，
因为 `(模块, 限定函数)` 这个键分不开它们，合并会让第一个 def 同时逃过两条断言
（`ruff --select F811` 对该形状不报，没有第二道拦）。两条今天在活树上都是零实例。

**具名白名单恰好三项**，每项带所依从句与理由：

| 豁免 | 从句 | 理由 |
|---|---|---|
| `journal_scope_census::_require_output_outside_root` | 2 | 见「已知限制」3 |
| `shud_preflight::check_shud_executable` | 1 | `_is_stub_basename(real)`（`shud_preflight.py:164`）判 stub 后，该可执行文件随即真的被执行 |
| `scheduler_config/db_free.py::_db_free_path_identity` | 1 | 只比较自身产物；**config 提供的那一侧**操作数以 `_db_free_path_check` 真探针出的 blocker 为前提（`services/orchestrator/scheduler_config/config.py:792`/`:806`/`:818`）。另一侧是模块常量 `NODE22_CANONICAL_NFS_RAW_AUTHORITY_ROOT`（`services/orchestrator/scheduler_config/config.py:788-790`），**从未被探测**（故「两操作数均以真探针为前提」是错的写法）；风险低（常量路径），但断言必须准。`db_free.py:174-177` 自述无拒绝通道 |

白名单三项以外的任何增长都要过评审。

**守卫抓得住什么、抓不住什么，说准：**

- 抓得住：**新的 (模块, 函数) 对**只写非严格形——即一个成员**完全**不做严格解析。
- **抓不住（一）**：在**已有成员内部**新增一处非严格调用。断言按成员归并
  （该成员另有一处 strict 即过关），所以这类新增是隐形的。最锋利的例子（实测）：
  删掉 `db_free.py::_db_free_loop_filtered_realpath` 那次 **loop-filtered 复查**的 `strict=True`
  ——那次复查正是本 ADR 普查表把该函数列为「立场 A 已复查」的**全部理由**——守卫仍然是绿的，
  因为该函数开头那次严格解析已经满足 `any()`。
  这是**声明的限制**，不是隐形缺口：要机械化地堵它，只能钉「每成员的 strict 调用计数不得减少」，
  而那是成员清单形的断言，与本节「断言违规者集合、不断言成员清单」的论证直接冲突。
- **抓不住（二）**：「作者忘了下游裁决」——那是设计评审的事，上面四个问题就是那份评审清单。
- **抓不住（三）**：标记写了、但从句选错。断言二是存在性检查，见上。

三条抓不住都是刻意的取舍，不是遗漏。

一处**已具名的缺口**：`basins_discovery.py::_safe_resolve_under_root` 所在模块的行数
超过仓库行数守卫的上限，而该守卫判的是绝对行数而非增量，故任何把它写入索引的变更都被拦。
按钩子自述的补救办法，该模块已加入豁免，从而得以在站点写下标记；拆分由 **#2460** 跟踪，
其完成判据就是移除该豁免。**标记本身照常写在站点上——守卫断言二不为它开洞。**

## 子族定性

- **`_safe_preserve_final_component`（`services/orchestrator/scheduler_config/path_modes.py::_safe_preserve_final_component`，
  幸存的 `Path.resolve` 子族）**：实现为 `path.parent.resolve(strict=False) / path.name`，
  `except (OSError, RuntimeError): return path`。`≤3.12` 环路抛 errno-less `RuntimeError`
  被吞、返回 raw path；3.13+ 不抛、返回折叠值。三个调用点
  （同文件的 `_config_path_preserve_final_component_for_mode`、
  `_config_path_relative_to_preserve_final_for_mode`、以及 `_confined_path_for_mode`
  的兜底臂——按本 ADR 已知限制 1(b) 用符号锚而非行号）的产物进 preflight 路径，
  在 `scheduler_runtime_roots.py::_scheduler_root_check` 的 `path.lstat()` 被探。**按从句 1 正当，不是 fail-open。**

  **产物在两个解释器上确实分叉，但分叉在解引用点是判据中性的**（实测）：
  环藏在 symlink 父段之后时，3.13 折叠、3.11 返回 raw，两条拼法逐字不同；
  而 `lstat` 对两种拼法、在两个解释器上**一律返回 `ELOOP`**（四格全同）。
  结构性原因就是从句 1 本身：让两条产物分叉的**充要条件**是父段里仍有环存活
  ——非严格解析越不过 cycle，只能把已解析部分与残段拼回。

  **这不等于该 preflight 腿没有跨解释器差异。** 它有：blocker code 与 evidence 载荷
  两端都不同（实测 `..._LOCK_ROOT_UNSAFE_PATH` vs `..._LOCK_ROOT_SYMLINK`）。
  但反事实探针把成因钉在**那条腿自己的** `Path.resolve(strict=False)`
  （#2453 修复前 `scheduler_runtime_roots.py::_scheduler_root_check` 里那次调用与其下 `except RuntimeError` 早退臂）上，
  **不是本 helper**：交叉喂入两种拼法，键集仍随解释器走而不随输入串走。已立单 **#2453**，
  **已闭合**：`_scheduler_root_check` 改走 `_canonical_path`（本表从句 1 成员）、删掉早退臂，
  环路输入与非环路输入同一条装配线，由 `lstat` 定 `SYMLINK` / `UNSAFE_PATH` / `NOT_FOUND`，
  3.11 与 3.14 上 code 与键集一致（`tests/test_scheduler_root_check_loop_convergence.py`）。
  本 ADR 只为本 helper 的这条臂背书，且背书范围**只到「解引用点判据中性」为止**。
- **裸 `except OSError` 写法**（`scheduler_runtime_roots.py::_canonical_parent` / `_canonical_path` /
  `_optional_config_path`，`path_modes.py::_resolve_config_path_for_mode` 两条臂）：
  **写法相同不等于从句相同。** `_canonical_parent`、`_canonical_path` 与 `path_modes.py`
  两条臂依**从句 1**（产物进 preflight 路径、`scheduler_runtime_roots.py::_scheduler_root_check` 的 `path.lstat()`）；
  `_optional_config_path` 依**从句 3**——它的产物变成 `allowed_storage_roots`，
  只作包含基底、自身从不被探，被探的是 `path`，是另一个值。
  **把它们按写法合并成一条从句是错的**——守卫断言二只管「具名了从句」，
  不管「从句选对了」，选对是设计评审的事。
  至于裸写法本身，在同一条解引用保证下它**更宽而不更险**（ESTALE / EACCES 也被折叠进兜底）。
  值得一句注释，不值得一次改动。是否按 errno 分流是**正交**问题，本 ADR 不裁定。
- **无拒绝通道的函数**（`_resolve_config_path_for_mode` 返回裸 `Path`，`path_modes.py::_resolve_config_path_for_mode` 的注释
  自述「classification stays with the storage preflight」）：这不是第四种立场，而是
  **为什么这些函数不能就地复查**的原因。本裁定把它们的正当性挂在消费者的解引用上，
  不要求它们自己产出一个无处上报的裁决。

## 权衡

**为什么不统一改成处处复查。** 这买到的是形式一致，而行为本来就已一致。代价是实的：
下列**断言容忍**的用例语义要翻转，而它们是本裁定的**成文形式**，不是待修的债——

- `tests/test_production_scheduler.py::test_preflight_allowed_roots_admits_loop_behind_missing_component_without_raising`
- `tests/test_production_scheduler.py::test_scheduler_allowed_roots_absorbs_loop_behind_missing_root_through_enoent_arm`
- `tests/test_production_scheduler.py::test_local_artifact_root_enoent_fallback_phantom_symlink_loop_residual`
- `tests/test_basins_discovery.py::test_symlink_loop_behind_missing_component_is_treated_as_nonexistence`

（这是一次 grep 取到的四个，不是穷举；本 ADR 对该计数只作有界断言。）
此外两个返回裸 `Path` 的函数必须先被加出拒绝通道才谈得上复查。
用真实的改动换一个不存在的问题。

**为什么不做一张 19 行的处置注册表 + 双向钉。** 那正是 PR #2440 用三轮审查买回来的教训的
**反面**：#2440 的处置表本身就是规格（每个键的运维可见行为），而这里的处置是**散文论证**
（「从句 1，下游裁决在 `scheduler_preflight.py:634`」）。一个只检查「每个函数都有一行」的钉，
验的是行的**存在**，不是所引裁决**存在、可达、独立**。行文照样烂，而钉照样绿。
**绿在漂移上的钉比没有钉更坏。** 守卫因此只断言家族唯一的**机械**不变量，
且按「断言违规者集合，不要断言成员清单」的写法——这条措辞是 **PR #1626 复盘的教训 6**，
本 ADR 是它的第二个用例，归因写在这里以免下一个作者以为它源于本 ADR。

**为什么 `Path.resolve()` 被逐出本家族。** 在本项目支持的解释器区间内，它的非严格形
在 CPython 3.13+ 对环路不再抛而原样收编，**而在 3.12 及更早连非严格形也抛**不带 errno 的
`RuntimeError`（实测 3.11.14：`resolve(strict=False)` → `RuntimeError errno=None`，
同一输入上非严格 `os.path.realpath` 折叠返回不抛）；严格形在 3.12 及更早同样抛
errno-less `RuntimeError`。即**两条臂在 ≤3.12 上都抛、在 3.13+ 上分道**，
没有哪一种形态在区间内说同一句真话。只有严格 `os.path.realpath`
在区间内一致地抛出带 errno 的 `OSError`。
顺带说明两个面的容忍剖面**正好相反**：`<不存在>/../<环路>` 这一 phantom 类，
非严格 `realpath` 四个版本全部容忍，而非严格 `.resolve()` 在 3.11（本仓生产 pin）直接中止。**该断言只覆盖本家族裁决的输入类**：
嵌 NUL 的值、以及 cwd 已被删除的相对路径，会让非严格 `os.path.realpath` 本身抛异常，
那些逃逸由别处治理。

## 已知限制

1. **本家族的失效模式是文本，不是代码，所以这里只留三条给下一个作者的情报。**

   **(a) 结构校验器看不见一份规格与它自己描述的行为相反。** `openspec validate --strict`
   校验的是结构，一条与实现相左的 SHALL、一个数不出来的计数、一处腐烂的行锚，它全都放行；
   能拦住这一类的只有人读与专门为该断言写的机械判据。
   **(b) 指向你正在编辑的那个文件、或指向你自己这次 diff 的 `file:line` 锚，
   会在写下它的那次编辑里腐烂。** 插入一段 docstring 就足以让它下方的所有行号失效，
   而腐烂后的锚往往仍落在一行看似合理的代码上——脆弱处一律用 `模块::函数` 符号锚。
   **(c) 转述别人报告里的闭合性断言，和手抄一张清单是同一件事。** 要么自己跑一遍，
   要么别写；两份报告冲突时唯一正确的动作是自己动手测，而不是挑一份信。

2. 守卫只覆盖 `os.path.realpath` 面；`.resolve()` 面由 **#2452 闭合**，靠两件东西，都不是扩展本守卫：
   - **一次性分诊**：四棵树上全部 `.resolve()` (模块, 函数) 对（当日 152 对 / 223 个调用点，AST 计数，
     与 #2452 同源）逐一对照三条从句归类，写在该 change 的 `design.md` §Triage——那是一次性分诊产物，
     **不是** shipped 注册表（理由同「权衡」节：钉行的存在不钉论证的成立）。结论：5 对落在三条从句之外
     （两组字符串审计 / 策略分类器：被判路径是别的节点上的日志 URI，或只与词法包含判定取交集的 URI 串，
     本机内核本就不是它们的权威），逐一写明理由接受，无一需要行为改动，生产调度路径上零 fail-open 站点；
     另把 3.11 上环路中止调用方的去向逐对记下（调用方已包 / 上游已拒 / 可接受的响亮中止 / 已改）。
   - **本面自己的机械判据** `tests/test_resolve_surface_guard.py`：**严格** `.resolve()`（`strict`
     在场且不是字面 `False`）若处于捕 `OSError` 本身的 `try` 体内，则该 `try` 或同函数内包住它的
     `try` 必须有捕 `RuntimeError`（或其基类）的 handler；违规者集合为空。
   本守卫的判据**构造性不可复用**：这里的判据建立在「严格 `os.path.realpath` 抛带 errno 的
   `OSError`」上，而 `.resolve()` 的严格形在 3.12 及更早对环路抛 errno-less `RuntimeError`——
   #2452 实测的 4 个「`strict=True` + handler 不捕 `RuntimeError`」站点在 3.11 生产 pin 上正是死代码，
   能过本守卫的那一形恰是 pin 上不安全的那一形；4 处已补 `RuntimeError` 臂、归到与 `OSError` 臂同一结果。
   **非严格形刻意不进新判据**：3.13+ 上它对环路折叠而不抛，给它加 `RuntimeError` 臂只会制造
   #2453 那种分歧（3.11 出 blocker、3.13 放行折叠值）；它们归分诊。新判据**判不了**
   「handler 是否归流到与不抛路径同一套装配」——那正是 #2453 的教训，仍是设计评审的事。
3. `journal_scope_census::_require_output_outside_root` 依从句 2。静态输入上无逃逸
   （悬空 symlink、中间组件缺失 + `..`、中间组件 ELOOP、父目录含 symlink + 末组件缺失，
   四类均方向正确或响亮失败）。**但从句 2 只对输入量化**：`_require_output_outside_root` 的返回值
   在 `_census_command_result` 里做完判定、才走到 `target.write_text(...)`，
   中间隔着一次自述在 node-22 上「takes minutes」的 census。
   本 ADR 对该站点的背书**以「单次操作者 CLI、无并发写者」为前提**；
   若将来有并发写者进入该路径，从句 2 不再适用，该站点须改依从句 1 或改为复查。
   同一函数里那两次 `os.path.realpath` 的抛型面（相对路径 + cwd 已删除 → `FileNotFoundError`、
   内嵌 NUL → `ValueError`）已由 **#2454 闭合**：包进 `except (OSError, ValueError)`，出 census 之前的
   typed code `CENSUS_OUTPUT_UNRESOLVABLE`；这是「调用会不会抛」，与从句 2 的「写落在哪」正交，
   守卫白名单里该项一字未动。
4. 8 个核心 root 字段的前置探针（`scheduler_preflight.py:634`、`scheduler_runtime_roots.py::_scheduler_root_check` 的 `path.lstat()`）
   挂在默认关闭的开关上（`slurm_execution_enabled` / `require_runtime_roots`）。
   这**不**构成错位——`lock_path` 与 `evidence_dir` 在使用点真的落到内核
   （`services/orchestrator/scheduler_lease.py:252` `os.open` 与 `:271`
   `os.stat(..., follow_symlinks=False)`、`services/orchestrator/scheduler_evidence.py:912`
   `open_evidence_directory`），
   phantom 值在那里是响亮的 ELOOP。关掉开关的代价是「配置错误表现为运行期 ELOOP
   而不是 preflight blocker」，属**可诊断性**降级，而那正是该开关存在的意义。
