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

### 这个前提是错的

裁定做过，论证也写得很清楚，只是写进了某个具体 change 的归档 design 里，
新站点作者不会去那里找：

> `openspec/changes/archive/2026-08-16-runtime-root-safety-symlink-loop/design.md:42`
> 「artifact-guard lane 的残留后果面是 verdict 路由口味；本 lane 是 manifest fail-open
> ——**后果面不同，裁决不同**。」

另一半在更早的归档里，它说明了立场 B 的正当性**从来不是**「环路无所谓」：

> `openspec/changes/archive/2026-08-10-symlink-loop-errno-detection/design.md:216-217`
> 「…and nothing unsafe is admitted (dangling entries fail `is_dir()`/existence checks downstream)」

即：容忍之所以成立，是因为**下游会挡**。立场 A 不是推翻这条原则，而是发现自己那条腿
（提交 manifest）**没有下游**，于是同一条原则给出了相反结论。三个立场是一条原则的三个实例。

本 ADR 不发明新规则，它把这条已经存在的规则搬到一个**新站点作者会看见**的位置，
给出可判定的判据，并留一条机械守卫防止复发。

## 决策

> **被裁决的那条路径，只要在任何据其归一化产物作出的判断被提交之前，
> 会被对内核解引用一次，ENOENT 非严格兜底就可以容忍；否则必须 loop-filtered 复查。**

fail-open 形状**有且只有一个**：

> 判据建立在归一化字符串上，而它代表的对象从未被解引用。

这正是 #1401 的 manifest 腿——把归一化路径写进提交 manifest、据此宣告「产物在此」，全程不 stat。

### 三条从句（析取，任一成立即可容忍）

1. **下游解引用。** 被裁决的路径在任何据其归一化产物作出的判断被提交之前，
   被探测（`exists()` / `lstat` / `open`），且那里的失败会改变裁决。
   探测点可以是**原值**而非归一化产物——内核解析使二者在探测能成功的输入上等价。
   实例：`scheduler_preflight.py:627` 探 `path`、`:628` 的包含判定用 `resolved`，
   而 `visible=False` 确实改变裁决（`:645-655` 出 `SLURM_PREFLIGHT_<FIELD>_NOT_VISIBLE`）。
2. **可证重合。** 归一化产物与后续动作实际作用的路径，在**任何该动作能够成功的输入**上重合。
   **本从句只对输入量化，不覆盖 check-then-act 竞态**——援引它必须写明
   「单次操作者 CLI、无并发写者」这个前提（见「已知限制」3）。
3. **仅作包含基底 / 比较操作数。** 归一化产物只被用作包含判定的基底或身份比对的操作数，
   自身从不承载「该路径存在或可用」的断言；而被判定的那一方在裁决提交前已被解引用。
   安全性由一条可检查的性质兜底：**真实对象的严格解析产物不可能以一个含缺失分量或
   环路的字符串为前缀**，故 phantom 基底放不进任何真实对象。这正是 #1332 那句话的机制。

初稿只写了前两条。是 fixture 评审指出：一个自称「唯一口径」的裁定，如果判不了自己的
19 行普查表（表里实际用了四类理由，三类不落在任何从句上），新站点作者照样读不出该照哪套写
——正是 #1627 要治的病，发在裁定本身上。第三条从句是被这条批评逼出来的。

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
   只有第二次仍 ENOENT 或干净解析才保留 admit。

## 普查

权威取「**调用了 `os.path.realpath` 的 (模块, 函数) 对**」，覆盖 `services/`、`workers/`、
`packages/`、`apps/` 四棵树。选它而不选代码形状，是因为形状匹配器编码的是作者猜的形状：
本轮先写的那个（`try` 内 strict + handler 内非 strict）漏掉了 handler 之后才复查的写法，
放宽后又把同一函数另一条臂的 strict 调用误判成复查，并且**漏掉了 issue 自己列为立场 A
头号站点的 `retry.py::_local_runtime_root_safety`**。按调用点取集合则形状无关：站点被重写、
errno 分流被增删、兜底被挪进 helper，集合都不动，只有**新增或删除站点**才动。

该集合当前 **19** 个成员，逐行处置（含各自所依从句）见
`openspec/changes/archive/2026-09-17-enoent-fallback-family-ruling/design.md` 的 D2 表
（那是**本 SHA 的快照，供读者对照**；承重的是下面那条守卫，不是那张表）。
四棵树外 `scripts/`/`db/`/`infra/` 的真调用点为 0、`tests/` 不发布。
**取键法的闭合性由守卫自己强制，不靠散文断言**：按 attribute 名取键只在
`realpath` 无法经裸名到达时闭合，故守卫第二条断言「击穿取键法的构造集合为空」，
覆盖裸名 import、非调用位置的属性引用、`getattr` 动态查找三种形状。
`import posixpath` 与各类 `os`/`os.path` 别名**不在**其中——它们产生的仍是
attr 为 `realpath` 的 `ast.Attribute`，扩面而不藏事。
（本 ADR 初稿曾把「全仓无 `import posixpath`」当成前提写进 design，
而 `workers/forcing_producer/file_store.py:6` 今天就有一个；见已知限制 1。）

### 权威集合是守卫的嗅探边界，不是裁定的覆盖边界

权威按**生产者**取键，而判据问的是**消费者**。经 helper 归一化的消费者永远进不了这个集合。
今天有三个，它们同样受本裁定约束，必须在此具名：

| 消费者 | 经由 | 依据从句 |
|---|---|---|
| `scheduler_config/db_free.py:285 _db_free_path_check` | `_db_free_loop_filtered_realpath`（立场 A，已复查） | — |
| `scheduler_state_failure.py:1561 _local_artifact_path_is_allowed` | `_realpath_or_none` | 1 |
| `scheduler_state_failure.py:1612 _local_artifact_allowed_roots` | `_realpath_or_none` | 1 |

其中 `_db_free_path_check` 是前序 PR 明文派给 #1627 的义务
（`openspec/changes/archive/2026-09-02-journal-root-realpath-and-job-id-scope-census/design.md:152-155`
指向 `openspec/specs/runtime-evidence-and-operations/spec.md:233` 的 requirement）。
该义务实质满足——它经由的 helper 本身就是立场 A——但初稿从裁定文本里**追溯不到**它，
而 #1627 就要关了。列在这里就是为了可追溯。

**结论：零错位站点。** 三个立场在今天的 master 上全部落在三条从句之一上，
没有要对齐的对象，故本次裁定不含任何运行时行为改动。

立场 C 的措辞按 issue 验收项「保留或改写，二选一并说明」**保留**，但须记明：
它已被实核为**立场 B / 从句 1 的一个实例**——`scheduler_state_failure.py:1401`
的 `path.exists()` 紧随 admitted 之后，且 `openspec/specs/job-retry-mechanism/spec.md:1585-1589`
已把这条写成 SHALL（null-reason absent verdict 只在实际探过存在性之后产生）。
「已裁定并接受为残留」这个自述在今天偏严。

## 守卫

> 权威集合中每个成员的 `os.path.realpath` 调用里，至少有一处带 `strict=True`；
> 违规者集合为空。

**具名白名单恰好三项**，每项带所依从句与理由：

| 豁免 | 从句 | 理由 |
|---|---|---|
| `journal_scope_census::_require_output_outside_root` | 2 | 见「已知限制」3 |
| `shud_preflight::check_shud_executable` | 1 | `_is_stub_basename(real)`（`shud_preflight.py:160`）判 stub 后，该可执行文件随即真的被执行 |
| `scheduler_config/db_free.py::_db_free_path_identity` | 1 | 只比较自身产物；两操作数均以 `_db_free_path_check` 真探针出的 blocker 为前提（`services/orchestrator/scheduler_config/config.py:792`/`:806`/`:818`）；`db_free.py:164-167` 自述无拒绝通道 |

白名单三项以外的任何增长都要过评审。守卫抓得住「有人加了第 20 个站点、只写非严格形」。
它**抓不住**「作者忘了下游裁决」——后者是设计评审的事，上面那四个问题就是那份评审清单。
这个分工是刻意的，不是遗漏。

## 子族定性

- **`_safe_preserve_final_component`（`services/orchestrator/scheduler_config/path_modes.py::_safe_preserve_final_component`，
  幸存的 `Path.resolve` 子族）**：实现为 `path.parent.resolve(strict=False) / path.name`，
  `except (OSError, RuntimeError): return path`。`≤3.12` 环路抛 errno-less `RuntimeError`
  被吞、返回 raw path；3.13+ 不抛、返回折叠值。四个调用点
  （同文件的 `_config_path_preserve_final_component_for_mode`、
  `_config_path_relative_to_preserve_final_for_mode`、以及 `_confined_path_for_mode`
  的兜底臂——本 PR 触及该文件，故按本 ADR 已知限制 1(b) 用符号锚而非行号）的产物进 preflight 路径，
  在 `scheduler_runtime_roots.py:304` 被 `lstat`。**按从句 1 正当，不是 fail-open。**

  初稿在这里多写了一句残留：「两个解释器上被 `lstat` 的是不同字符串，可能得到不同 blocker 码」。
  **产物确实分叉，但分叉在解引用点是判据中性的**——本人实测（非转述）：
  环藏在 symlink 父段之后时，3.13 折叠、3.11 返回 raw，两条拼法逐字不同；
  而 `lstat` 对两种拼法、在两个解释器上**一律返回 `ELOOP`**（四格全同）。
  结构性原因就是从句 1 本身：让两条产物分叉的**充要条件**是父段里仍有环存活
  ——非严格解析越不过 cycle，只能把已解析部分与残段拼回。

  **这不等于该 preflight 腿没有跨解释器差异。** 它有：blocker code 与 evidence 载荷
  两端都不同（实测 `..._LOCK_ROOT_UNSAFE_PATH` vs `..._LOCK_ROOT_SYMLINK`）。
  但反事实探针把成因钉在**那条腿自己的** `Path.resolve(strict=False)`
  （`scheduler_runtime_roots.py:271` 与其下 `except RuntimeError` 早退臂）上，
  **不是本 helper**：交叉喂入两种拼法，键集仍随解释器走而不随输入串走。已立单 **#2453**。
  本 ADR 只为本 helper 的这条臂背书，且背书范围**只到「解引用点判据中性」为止**。
- **裸 `except OSError` 写法**（`scheduler_runtime_roots.py::_canonical_parent` / `_canonical_path` /
  `_optional_config_path`，`path_modes.py::_resolve_config_path_for_mode` 两条臂）：
  三者均依从句 1（产物进 preflight 路径、`:304` `lstat`）。至于裸写法本身，
  在同一条解引用保证下它**更宽而不更险**（ESTALE / EACCES 也被折叠进兜底）。
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
（「从句 1，下游裁决在 `scheduler_preflight.py:627`」）。一个只检查「每个函数都有一行」的钉，
验的是行的**存在**，不是所引裁决**存在、可达、独立**。行文照样烂，而钉照样绿。
**绿在漂移上的钉比没有钉更坏。** 守卫因此只断言家族唯一的**机械**不变量，
且按「断言违规者集合，不要断言成员清单」的写法。

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

1. **本 ADR 的三条自定硬约束，作者在本 PR 内全部违反；未现证的陈述共十三处。**
   继承自 PR #1626 复盘的三条是：只写有界断言、引用必须现证、孤儿指针审计必须双边。
   初稿写了无豁免从句的全称 SHALL；只审两条 spec 指针而漏掉全仓另外 5 处 tracker 活引用；
   以及十三处未经现证就落笔的陈述：

   - 承重引用 `scheduler_state_failure.py:1401` 写成 `:1399`，**且在同一节自称
     「每条引用均已逐行打开核对」——那句自述本身是假的**；
   - 两处归档 design 的行范围过宽；一处 `production_closure` 计数两种数法都不可复现；
   - `shud_preflight` 的判 stub 记在 realpath 调用那一行，实际在 `_is_stub_basename(real)`；
   - `_db_free_path_check` 三个探针行号各差 1-2；
   - 一条**写下即腐烂**的同文件锚（我要求在某 docstring 里写同文件下方的行号，
     而插入该 docstring 本身就会让它们失效）；
   - 四处行号在本 PR **自己的 diff** 内被顶走而失效；
   - 把「全仓无 `import posixpath`」当前提写进 design，而 `workers/forcing_producer/file_store.py:6`
     今天就有一个——**那句话是从本 PR 自己的评审席报告里誊来的，我没有自己测**；
   - `_safe_preserve_final_component` 写成「三个包装器」，实为四个调用点；
   - 「`Path.resolve()` 非严格形在 3.13+ 不再抛」漏了后半句——**≤3.12 上它同样抛**；
   - 把另一处站点的 `resolve(strict=False)` 记在它上面那行的 `try:` 上；
   - 以及最重的一条：断言该臂留有「跨解释器 blocker 码分歧」残留，
     **并把这句话写进了一份 live spec**；
   - 然后在撤回它时**又把一份 agent 报告的结论放大成了规范文本**——写下「该臂不携带任何残留、
     每个 blocker code 都相同」，而另一个 agent 的端到端实测给出
     `..._UNSAFE_PATH` vs `..._SYMLINK`。两份报告并不矛盾（分歧源是另一处站点），
     **是我把其中一份放大了**。最终由我自己动手实测才定案。

   **十三处没有一处是 `openspec validate --strict` 抓到的**——它只做结构校验，
   看不见一份规格与它自己想要的行为相反，更数不清行号。抓到它们的是四种机制：
   fixture 评审席（人）三处；实现者三处，其中一处是它**拒绝执行我的指令**；
   一个当场写的机械 file:line 校验器两处（其一覆盖四个站点），
   且第一处只在实现者改完同批文件后才暴露——**校验器自己也是在第十一次错误之后才被写出来的**；
   以及三处来自被派去立单的 agent——**其中一个没有立单，它把我要它跟踪的那条残留测掉了**。
   最后这一条是本 ADR 最该被记住的方法论实例：**被要求为某个断言建档时，
   正确的第一步是去测它是否成立，而不是替它写一份档案。**

   给下一个作者的三条情报：
   **(a) 本家族的失效模式是文本，不是代码**——结构校验器看不见规格与行为相反。
   **(b) 同文件内、以及本 PR diff 内的 file:line 锚会在写下它的那次编辑里腐烂**；脆弱处一律用符号锚。
   **(c) 转述一份报告里的闭合性断言，和手抄一张清单是同一件事。** 要么自己跑，要么别写。
   本 PR 还给这条加了一个推论：**在撤回一条转述错误时，最容易犯的就是再转述一次**
   ——我撤回那条被证伪的残留断言时，照另一份报告又写了一句同样未自测的话，依旧落在规范文本里。
   两份报告冲突时，唯一正确的动作是自己动手测，而不是挑一份信。

2. 守卫只覆盖 `os.path.realpath` 面。`.resolve()` 面在同四棵树上有 152 个 (模块, 函数) 对
   / 223 个调用点，本次未逐一对照三条从句归类，已立单 **#2452**（分诊，不是「替换全部 `.resolve()`」）。
   该单实测出一个**与本 ADR 守卫直接冲突的倒置**，记在这里免得有人提「把 `.resolve()` 加进守卫」：
   本 ADR 的守卫判据是「至少一处 `strict=True`」，而在 `.resolve()` 面上，
   仓内 7 个 `strict=True` 站点里有 4 个的 handler 不捕 `RuntimeError`
   ——**在 3.11 生产 pin 上它们是死代码**。也就是说，在那个面上，
   「能通过本守卫的 `strict=True`」恰恰是生产 pin 上不安全的那一形。
   `.resolve()` 没有可充当安全性质的 `strict=True`，故它需要自己的判据，
   不能靠扩展本守卫闭合。
3. `journal_scope_census::_require_output_outside_root` 依从句 2。静态输入上无逃逸
   （悬空 symlink、中间组件缺失 + `..`、中间组件 ELOOP、父目录含 symlink + 末组件缺失，
   四类均方向正确或响亮失败）。**但从句 2 只对输入量化**：`_require_output_outside_root` 的返回值
   在 `_census_command_result` 里做完判定、才走到 `target.write_text(...)`，
   中间隔着一次自述在 node-22 上「takes minutes」的 census。
   本 ADR 对该站点的背书**以「单次操作者 CLI、无并发写者」为前提**；
   若将来有并发写者进入该路径，从句 2 不再适用，该站点须改依从句 1 或改为复查。
   同一函数里那次未加保护的 `os.path.realpath(target)` 另有问题（相对路径 + cwd 已删除、
   内嵌 NUL），属既存问题，另行立单。
4. 8 个核心 root 字段的前置探针（`scheduler_preflight.py:627`、`scheduler_runtime_roots.py:304`）
   挂在默认关闭的开关上（`slurm_execution_enabled` / `require_runtime_roots`）。
   这**不**构成错位——`lock_path` 与 `evidence_dir` 在使用点真的落到内核
   （`scheduler_lease.py:252` `os.open`、`scheduler_evidence.py:912` `open_evidence_directory`），
   phantom 值在那里是响亮的 ELOOP。关掉开关的代价是「配置错误表现为运行期 ELOOP
   而不是 preflight blocker」，属**可诊断性**降级，而那正是该开关存在的意义。
