# Tasks: scheduler-refusal-source-and-repaired-arm-oracles

> **唯一写入面是 `tests/test_production_scheduler.py`。** 任何对
> `services/orchestrator/**` 的改动都是越界——本 change 交付后编排者以 sha256 校验
> 四个被断言模块逐字节不变（design D5）。
>
> **所有坐标以本 change 的 base commit 为准**（两条 issue body 里的行号来自更早的 HEAD，
> 已整体漂移：#1418 的目标测试从 `:20160` 漂到 `:26690`，#1451 的谓词从
> `scheduler_state_rows.py:643` 漂到 `:860`、`chain_source_cycle.py:503` 漂到 `:512`）。

## 1. #1418 结构守卫——钉死「模块级常量 → 消费函数」映射

- [x] 1.1 在 `tests/test_production_scheduler.py` 新增模块级辅助函数，对给定模块
      `ast.parse(Path(module.__file__).read_text(encoding="utf-8"))`，返回
      `dict[str, frozenset[str]]`：键 = 每个**模块级常量赋值**的目标名，
      值 = 在其函数体内引用该名的**函数名**集合（嵌套函数按其自身名计）。
- [x] 1.2 **`ast.Assign` 与 `ast.AnnAssign` 都必须收**（design D2）。本模块的
      `_REMEDY_NON_CAUSAL_CLASSIFIER_TABLE`(`:218`) / `_REMEDY_NON_CAUSAL_CODE_TABLE`(`:222`)
      正是注解赋值；只处理 `ast.Assign` 会让这两条**整个不在盘点里**，M4 变异随即存活。
- [x] 1.3 **不得按值形状筛选主体**（不得要求"值是 set/frozenset 字面量"或
      `"frozenset" in ast.dump(value)`）。上面那两张表的值是 dict-of-Name，
      `frozenset` 只出现在**注解**里，任何值形状筛法都漏。主体 = 全部模块级常量。
- [x] 1.3b **主体必须 fail-closed，且必须以 accept-set 的形式写**（fixture 评审 F1 + round-2 P1-1）：
      遍历模块 body 时，**显式枚举放行集** `Import` / `ImportFrom` / `FunctionDef` /
      `AsyncFunctionDef` + 唯一放行的 `ClassDef`（见下），单 `Name` 目标的 `Assign`/`AnnAssign`
      入主体，**其余一律 catch-all `else: raise`**。
      **不得只写"拒绝这几种"**：只钉 refuse-list 的实现在 HEAD 上同样绿，而 `match`-case 体、
      PEP695 `type` alias、`AugAssign`、`global` 安装会**全部逃逸**（round-2 实测，且已用
      `match`-case 做出活体复发：结构守卫绿、映射逐字节相同、`_remedy_permits_permanent_failure`
      的裁决从 `True` 翻成 `False`）。catch-all 才能让**未来新增的语句形式**按构造被拒。
      **`Expr` 的裁定必须写明**：HEAD 该模块模块级 `Expr` 数为 **0**（无 module docstring），
      所以不放行 `Expr` 今天可用、且能顺带拦住 `global` 安装的那句裸调用——但那是撞上的不是设计的，
      谁补一句 docstring 就假红。**按本 change 的选择：放行 `Expr`，`global`/`setattr` 一类
      交给 1.3c 的运行时交叉核对兜底**；实现须在注释里写明这个取舍。
      被拒的形式（非穷举，catch-all 覆盖其余）：`Tuple`/`List` target（元组解包）、`NamedExpr`、
      `If`/`Try`/`For`/`While`/`With`/`Match` 体内的赋值、`AugAssign`、`TypeAlias`、
      以及未显式放行的 `ClassDef`。
      **`ImportFrom` 的 `*` 必须显式拒**：星号导入下 AST 不知道绑了哪些名字，这是 1.3c 的
      名字比对关不掉、只能靠拒绝关掉的唯一支路。
      **静默跳过 = 逃逸口**：实测这四种写法产出的映射与基线逐字节相同，其中元组解包那一支
      已被做成**活体复发**（生产语义真的翻了，两条守卫全绿）。
      `ClassDef` 今天只有 `_ForcingSidecarProvenance`（`services/orchestrator/scheduler_state_failure.py:947`），
      **按类名显式放行**并在注释里写明理由；放行的是这一个名字，不是"所有类"。
      合成源码用例**至少两条**（round-2 P1-1）：一条喂元组解包（refuse-list 上的形式），
      **另一条必须喂一种不在 refuse-list 上的形式**（如 `match`-case 体或 PEP695 `type` alias），
      两条都断言抛错。只测前者钉的是枚举、不是 catch-all；后者才是 accept-set 边界唯一的 oracle。
- [x] 1.3c **再加一条运行时交叉核对**（编排者自查，已实测）：AST 盘点出的名字集合必须等于
      导入后模块对象上实际存在的模块级常量名集合（`vars(module)` 去掉 dunder、去掉本模块 AST
      里的 import 名与 `def`/`class` 名、去掉模块对象与本模块定义的可调用对象）。
      纯语法主体对 **`global` 安装式绑定完全失明**——变异后 body 只多出 `FunctionDef` 与
      `Expr`(调用)，两者都是已识别形式，既不入主体也不触发拒绝，且消费映射不变。
      实测：HEAD 上两侧均为 18、双向差集为空（无假阳性）；`global` 变异下运行时 19 vs AST 18，
      指名多出 `_DOWNSTREAM_EXTRA_REFUSAL_CODES` → 红。
- [x] 1.4 改写 `test_scheduler_state_failure_holds_no_second_permanent_code_refusal_list`
      （base `tests/test_production_scheduler.py:26690`）：**删除**其中
      `for retired_literal in (...)` / `assert retired_literal not in source` /
      `assert source.count("_REMEDY_NON_CAUSAL_CODES = ") == 1` 三段字符串扫描
      （AC-6：`assert <literal> not in source` 形态清零），换成对 1.1 产物与钉死期望映射的
      **整体相等**断言。期望映射的权威值见 design D1 的 18 行表（照抄，不要另行推导）。
- [x] 1.5 **保留不动**该测试自 `assert scheduler_state_failure_module._REMEDY_NON_CAUSAL_CODE_TABLE == {`
      起的四条常量取值断言（#1313 round-1 V1-C1 加入，是真值断言不是源码扫描）。
      **1.4 删掉 `source.count(...) == 1` 之所以安全，唯一理由是这一条保留**：同名重复赋值
      对结构守卫完全隐形（键与消费映射逐字不变），只有取值断言能抓（import 时后赋值胜出）。
      见 design D2 盲区表最后一条。
- [x] 1.6 测试注释写明这条守卫钉的是什么不变量（"被咨询的 refusal 判据源只有一个"）、
      以及它**有意**带来的摩擦（正当新增模块级常量须同步更新期望映射），并写明它的两条
      盲区（函数体内联清单、跨模块清单）各自由谁接（前者 → §2 行为守卫；后者 → 不在 #1313
      AC-1 的命题域内）。

## 2. #1418 行为守卫——`code_recorded=True` 域上裁决与 `reason_code` 无关

- [x] 2.1 新增 `@pytest.mark.parametrize` 用例，直接调
      `scheduler_state_failure_module._downstream_failure_restartable`。
- [x] 2.2 码轴**必须包含**三条已退役的黑名单码 `INVALID_MANIFEST`、
      `MANIFEST_SCHEMA_INVALID`、`MALFORMED_INPUT`（复发时会被写回清单的正是这三个），
      外加至少一条 transient 码与一条 unknown-default 码作对照。
      **每行同时设 `error_code` 与 `reason_code` 两个键同值**（design D3）：本模块读码走
      键链，只设 `reason_code` 的用例对写成读 `error_code` 的内联复发整片失明。
- [x] 2.3 断言：`code_recorded=True` **且无 `limit_exhausted`** 时裁决 == `not failure.get("permanent")`；
      `limit_exhausted is True` 时恒为 False（该短路**先行**）。两条从句的域**不得重叠**——
      初稿两句都写成无界，在 2.3b(b) 强制要求的那一行（`permanent` 假 × `limit_exhausted=True`）上
      互相矛盾（HEAD 返回 `False`，而第一句要求 `True`），照字面写出的测试**在 HEAD 上就是红的**，
      而让它变绿最省事的做法正是删掉那一行——那是 M9 唯一的杀手（fixture 评审 round-2 P1-2）。
- [x] 2.3b **矩阵必须含 `permanent` 取假的行，否则 M8/M9 在完全合规的实现下双双存活**
      （fixture 评审 F2，20 行真值表实测：12 行 `permanent=True` 对两条变异零判别力）。
      硬性两条：
      (a) **每一个**轴上码 × `permanent` 假 × 无 `limit_exhausted` —— 杀 M8（内联码清单）；
      (b) 至少一行 `limit_exhausted=True` × **`permanent` 假** × `code_recorded=True`
          —— 杀 M9（删短路）。**在 `code_recorded=True` 域内**实测差异域只有这一格：
          `limit_exhausted=True, permanent=True` 两版同为 `False`，变异存活。
          **注意限定词**：放到全函数域上，M9 与 HEAD 共 7 个差异组合，其中 3 个落在
          `code_recorded=False`（任何 `limit_exhausted=True` 且 classifier 不在 placeholder 清单里的行）。
          所以 2.4 允许写的 `code_recorded=False` 正向用例**可能顺带**杀掉 M9；
          但本条要求的那一行仍是 `code_recorded=True` 域内唯一的杀手，不得据此省略（round-2 P2-1）。
- [x] 2.4 **不要**把 `code_recorded=False` 分支卷进"无二次拒绝"的断言域
      （design D3）：该分支的 `_DOWNSTREAM_PLACEHOLDER_REFUSAL_CLASSIFIERS` 按 #1313 D4
      是**正当的**唯一清单，卷进去会直接红且与 #1313 D4 打架。该分支可以另写正向用例，
      但不得断言"与 classifier 无关"。

## 3. #1451 `active_blocker` 臂矩阵

- [x] 3.1 在 `tests/test_production_scheduler.py` 的 import 段新增
      `from services.orchestrator import chain_source_cycle as chain_source_cycle_module`
      （该文件今天没有这个别名；这是本 change 对 import 段的唯一改动）。
- [x] 3.2 新增参数化矩阵，轴一 = 谓词实现
      （`scheduler_state_rows_module._pipeline_job_is_repaired_stage_evidence` 与
      `chain_source_cycle_module._pipeline_job_is_repaired_stage_evidence`，两处逐字相同的
      独立函数对象），轴二 = design D4 的五行行形状。
- [x] 3.3 五行**逐行**断言谓词取值；其中第 2 行（`active_blocker: False` 且**无**
      `repair_status`）与第 4 行（两字段全缺）是杀 M1 / M2 的指名 oracle，**缺一不可**
      （design D4 已论证：单留任一行都会留下一个存活变异）。
      **两行的买点不同，注释里要如实分开写**（design B-2 实测）：第 2 行是**判别力从 0 到 1**
      （删臂变异在基线上 1731 全绿存活）；第 4 行**不增判别力**——truthy 变异在基线上就打红
      319 条——它买的是把远端偶然失败换成谓词旁的指名失败（定位成本 + 意图表达）。
      不得把第 4 行写成"补上了缺失的覆盖"。
- [x] 3.4 对 `scheduler_state_rows` 那一份，同时断言下游
      `scheduler_state_manual_retry_module._job_row_is_live_failure` 的取值（第 2 行 False、
      第 4 行 True）。`chain_source_cycle` 那份**不**接这条下游腿
      （`_job_row_is_live_failure` 从 `scheduler_state_rows` 导入谓词，
      base `services/orchestrator/scheduler_state_manual_retry.py:17-25`），
      不要给它编一个不存在的下游断言。
- [x] 3.5 用例注释写明：这一臂在**仓内生产写入方**路径上恒被第一臂遮蔽
      （两个 annotate 写入方同时写 `repair_status="repaired"` 与 `active_blocker=False`），
      它的判别力只对**持久化 state 回读的外部/历史形状**成立——即为什么这组用例只能是
      直接谓词单测而不是端到端用例。

## 4. 非目标校验

- [x] 4.1 `git diff --stat` 只含 `tests/test_production_scheduler.py` 与
      `openspec/changes/**`；`services/` 零改动。
- [x] 4.2 不动 `_live_failure_closure_row_field_reads`
      （base `tests/test_production_scheduler.py:8720`，`inspect.getsource` + 正则字段扫描）
      ——同族的源码扫描型断言，但 #1418 边界明写"同文件其它测试" out of scope。**只报不修**。

## 5. 变异验收（Evidence Floor）

> 全部在**隔离副本**内执行（`git archive HEAD | tar -x -C <scratch>`），并先证明 import 解析到
> 副本；**绝不**把变异写回本仓工作树（本工作树被多个会话共享）。每条变异按**唯一源码文本
> 匹配**应用并断言 `count == 1`，**绝不按行号**。每条跑完 sha256 校验还原。

- [x] 5.1 M1 / M2 / M3 各自由指名用例打红。**判据是"指名的那一行出现在失败清单里"，
      不是"套件变红"**——M2 在基线上就已经打红 319 条（design B-2），单看变红什么也证明不了。
      每条变异记录必须贴出 `-k` 到指名用例后的红/绿，而不是只贴总数。
- [x] 5.2 M4（**AnnAssign** 形式的第二清单）、M5、M6、M7 各自由结构守卫打红。
- [x] 5.2b **M11 元组解包形式的第二清单**（fixture 评审 F1 的活体复发形状）由 fail-closed
      主体打红——这一条在 1.3b 未实现时必绿，是 F1 的回归证据。
- [x] 5.2c **M12 `global` 安装式的第二清单**由 1.3c 的运行时交叉核对打红——这一条在
      1.3c 未实现时必绿（fail-closed 语法主体也拦不住），是编排者自查那条发现的回归证据。
- [x] 5.3 M8（**函数内联**码清单）、M9 各自由行为守卫打红。
- [x] 5.4 M10（`ruff format` 语义不变）**保持绿**——AC-4 的反向用例。
- [x] 5.5 四个被断言模块 sha256 与 base 逐字节相同。
- [x] 5.6 `uv run pytest -q tests/test_production_scheduler.py` 全绿
      （base 基线：**1731 passed**）。
- [x] 5.7 `uv run ruff check .` 通过。
- [x] 5.8 `openspec validate scheduler-refusal-source-and-repaired-arm-oracles --strict --no-interactive` 通过。

## 6. 留痕与路由

- [x] 6.1 PR body 写明：#1418 的"解决思路"段被**实测否掉**——按 D1 判据在 HEAD 上红 **11** 条，
      按 issue 字面的 frozenset-only 主体红 **2** 条（只计函数体引用）或 **4** 条（把模块级引用
      也算上：`_NON_REGULAR_OBJECT_KINDS`、`_HYDRO_RUN_CODE_CLEARING_STATUSES`、
      `_REMEDY_NON_CAUSAL_CODES`、`_CHANGED_MODEL_PACKAGE_NON_CAUSAL_CODES`——后两者都被
      issue **未放行**的 `_REMEDY_NON_CAUSAL_CODE_TABLE` 在模块级引用；issue 原文只放行了
      classifier 表）。**三种计法都 > 0**（design D1）。被采纳的是它的验收标准 AC-1..AC-6 与
      "两条合取"那句。**数字口径注意**：初稿写过"12 条"（错）、一稿改成"3 条"（也错，漏了
      `_REMEDY_NON_CAUSAL_CODES`）——PR body 只许写上面这组已实证的数。
- [x] 6.2 PR body 记录 B-1 表（现行守卫判别力的精确刻画，比 issue 表格窄）与 B-2 表
      （M1 删臂后 1731 passed 的实测坐实）。
- [x] 6.3 §4.2 的同族源码扫描断言：报，不修，不另立单（#1418 边界已把它排除在外）。
