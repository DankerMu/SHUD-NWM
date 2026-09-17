# Design —— ENOENT 非严格兜底的家族级裁定

## 风险分诊

| 轴 | 取值 | 依据 |
|---|---|---|
| Fixture 级别 | **compact** | 零运行时行为改动。issue 自估 M 建立在「需对齐 9 处站点」之上，而逐行普查得出零错位站点（ADR 0009「普查」），该前提不成立；「家族层从未裁定过」那半句是对的。 |
| 必须保持的行为 | 全部 19 个权威成员的现有裁决逐字不变 | 本 change 不碰实现；`.py` 改动只有注释与 tracker 指针 |
| 被测接缝 | 「调用 `os.path.realpath` 的函数集合」 | D3 / ADR 0009「守卫」；形状无关，故重写站点不会让守卫漂移 |
| 选中的 risk pack | spec/doc 一致性、闭合集合完整性 | 本 change 通篇是规范文本与引用，两者正是它唯一能出的错 |
| 未选 | 并发、性能、安全边界、迁移 | 无运行时行为改动 |

### 继承自 PR #1626 复盘的三条硬约束（本 change 与它同一家族，且同样通篇是文本）

1. **对 `os.path.realpath` 只写有界断言，永不写全称。** #1626 两次把全称断言写进
   `openspec/specs/`，两次被推翻（嵌 NUL 值 → `ValueError`；相对路径 + cwd 已删除 →
   `FileNotFoundError`）。本 change 的 delta spec 因此写成有界形式：
   无豁免从句的 "Every function … SHALL" 在合并当天的仓里就有三个反例。
2. **任何 `#N` / 归档路径 / file:line 落笔前必须现证。** 一位之差的锚解析得掉、
   读起来也合理：承重引用 `scheduler_state_failure.py:1401` 与 `:1399`
   （`if not allowed:`）只差两行，只有逐条打开才判得了。
   核对结果落在 EF-6 的机械回执，不写成「已逐行核对」这类自述。
3. **孤儿指针审计必须双边。** #1626 审了 #1427 那条指针却没 grep #1400，合并即假。
   本 change 的 tracker 面除两份 spec 外，全仓另有 5 处 tracker 语义的活引用，
   全部并入 A-2；终态一律指向 **ADR**，不指向任何 issue（该 change 合并后 #1627 关闭）。

## D1 —— 裁定：家族从来只有一条原则，它只是写在没人会去找的地方

issue 的前提是「三套相反教条并存，从未在家族层裁定过一次」，**后半句是对的**。

完整论证、两处归档出处的原文、唯一口径的裁定文本、fail-open 的两种形状、以及三条从句，
**只写在 `docs/adr/0009-path-canonicalization-dereference-doctrine.md`**（「背景」「决策」两节）。
**本 design 不保留任何一段的副本**——同一段论证存两份，两份必然各自漂移，
而漂移后两份都还是绿的。

本节只留本 change 自己的决定：

- 裁定**在家族层作出**，写进 ADR 0009（长期件），不写进本 change 目录（会被归档）。
- 立场 C 的措辞按验收项「保留或改写，二选一并说明」**保留**，并在 ADR 记明已实核为立场 B 的实例。

## D2 —— 普查：19 个权威成员

**权威取键法（「调用了 `os.path.realpath` 的 (模块, 函数) 对」）、19 行处置表、
wrapper 消费者三行表、以及「权威集合是守卫的嗅探边界、不是裁定的覆盖边界」的论证，
只写在 ADR 0009「普查」一节。** 本 design 不保留副本：一张声称「零错位」的处置表，
存两份就会自己先错位。

### 权威怎么取（这一步是本 change 最贵的一课）

方法论结论与它的三处反例（AST 形状匹配器的误判）同样写在 ADR「普查」一节。
本节只记不在 ADR 里的过程事实：

- issue 自带的表（9 处未复查 + 2 处已复查）在四周内就烂了：文件搬家两处
  （`scheduler_config.py` 拆成包、`basins_package.py` 改名 `basins_package_source_io.py`）、
  归因错误一处、未覆盖站点若干。**手抄的普查一定漂移**是 PR #2440 烧掉三轮审查买回的不变量。
- 取键法的闭合性**不靠散文断言，由守卫的取键法闭合断言强制**
  （`tests/test_path_canonicalization_family_guard.py::test_no_call_shape_defeats_the_attribute_name_key`）；它覆盖哪三种形状、
  以及为什么 `import posixpath` 不在其中，写在 ADR 0009「普查」，本节不复述。

## D3 —— 防复发锚：断言违规者集合为空，不是断言成员清单

本 change 的决定：**不做**一张 19 行的处置注册表 + 双向 `stale == []` 钉，
改为「违规者集合为空」形的守卫断言——两条管成员，一条管取键法本身的前提。

三条断言的正文、它们与被否决的那张注册表的区别、两处收紧（`tokenize.COMMENT` 判定、
同限定名多次绑定判红）、三项具名白名单及其理由、以及守卫抓得住/抓不住什么，
**只写在 ADR 0009「守卫」一节**，本节不复述。

只记本节自己的两件事：

- 断言二存在的理由：delta spec 的「recorded at the site」若只是一条散文 SHALL，
  就没有任何东西能让它变红（论证见 ADR 0009「守卫」）。
- 具名白名单**恰好三项**，B-3b 的红证据证明三项**每一项都承重**——从白名单删掉
  `db_free.py::_db_free_path_identity`，守卫即红。

## D4 —— errno 分流是正交问题，不在本裁定内

issue 把「裸 `except OSError` vs errno 分流」和「兜底要不要复查」混在一处。它们正交。
`scheduler_runtime_roots.py::_optional_config_path` 的注释（#1423 design D2 的反论
「errno 分流一无所获」，今天在 `:679-683`）只回答前者。

裸写法各条臂的从句归属，以及对裸写法本身的评价，写在 ADR 0009 的普查表与「子族定性」两节，
本节不复述：**本节自认与对齐问题正交，把定性挂在这里等于没回答。**

## D5 —— report-only（按仓规：报告，不修）

1. **`.resolve()` 面**：基数、它为什么需要自己的判据、以及本守卫不能靠扩展闭合它，
   写在 ADR 0009「已知限制」2，本节不复述。本 change 的处置：**未逐一核过、不下判断、立单路由**。
2. `journal_scope_census::_require_output_outside_root` 的**判据/动作分离**及其 TOCTOU 敞口：
   试破结果、从句 2 的量化范围、以及它所依的并发前提，
   **只写在 ADR 0009「已知限制」3**，本节不复述。
   本节只记 report-only 的路由决定：本 change 不改该站点，前提失效时它须改依从句 1 或改为复查。
3. 同一函数内那次 `os.path.realpath(target)` 无保护——两个逃逸输入类写在
   ADR 0009「已知限制」3 的末句，本节不复述。本 change 的处置：**既存问题，零运行时改动，立单。**

## D6 —— 验收项要求「单独定性」的两个子族

- **`_safe_preserve_final_component`（`path_modes.py::_safe_preserve_final_component`）**
  ——**定性已在本 fixture 内完成，不作为实现阶段的条件项下派**。实现形状、两个解释器上的行为、
  三个调用点、以及「按从句 1 正当，背书范围只到解引用点判据中性为止」的全部论证，
  写在 ADR 0009「子族定性」，本节不复述。本节只记路由：该 preflight 腿确有的
  跨解释器差异归因于它自己的 `Path.resolve(strict=False)`，已立单 **#2453**；
  口径以 `openspec/specs/slurm-array-runner-integration/spec.md` 与 ADR 0009 为准。
- **8 个核心 root 字段经 `_resolve_config_path_for_mode`**：探针、开关、代价与结论
  全部写在 ADR 0009「已知限制」4，本节不复述。本 change 的处置只有一句：
  **记为观察，不记为缺陷**，不改行为。

## 证据映射

| 主张 | 证据 |
|---|---|
| 权威 19 成员、形状无关、取键法闭合 | AST 扫描 + 守卫测试（表在 ADR 0009「普查」） |
| 零错位站点 | ADR 0009「普查」表逐行从句归属 + file:line；ADR 0009「已知限制」4 的两处内核解引用 |
| 立场 C 实为 B | `services/orchestrator/scheduler_state_failure.py:1401` + `openspec/specs/job-retry-mechanism/spec.md:1585-1589` |
| 原则早已成文 | `archive/2026-08-16-runtime-root-safety-symlink-loop/design.md:41-43`、`archive/2026-08-10-symlink-loop-errno-detection/design.md:216-218` |
| 守卫今天为绿且能变红 | 红证据两向实跑：新增一个只写非严格 realpath 的桩函数 → 守卫失败；删桩 → 恢复绿 |
| tracker 指针双边闭合 | A-2 的全仓 `#1627` grep 结果随 PR body 交付 |

> 锚的形式按脆弱程度选，**不按一条全称规则选**：本表里指向本 change 自己改动区附近的
> 位置用 `模块::函数` 符号锚（同文件内的行锚会被写入它的那次编辑自己顶走，
> 见 ADR 0009「已知限制」1(b)）；指向已冻结归档件、或指向被改动文件里**远离改动区**的
> 稳定位置时留行号。哪条锚今天成立，**以 `cite-check-receipt.txt` 的逐条解析为准**，
> 不由任何一句关于锚形式的散文断言担保——本表就有一条留行号的锚落在被本 change
> 改动过的文件里（`scheduler_state_failure.py:1401`，改动点在其后数百行），它成立，
> 但它成立是因为回执解析得到，不是因为某条规则保证。
