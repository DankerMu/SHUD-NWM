# Design: scheduler-refusal-source-and-repaired-arm-oracles

## 风险分级

- **fixture 级别**：`expanded`（两条 S 级 issue 合批；纯测试面，零生产改动）。
- **风险轴**：**oracle 有效性**是本 change 的**唯一**风险轴——交付物本身就是判别力。
  一个"写完是绿的但杀不死变异"的守卫，比不写更坏（它让验收线看上去有人看守）。
  因此每条新守卫都必须附**实测的红/绿矩阵**，不接受"看起来能拦住"。
- **不在风险面上**：生产行为（零改动）、跨解释器差异（纯 Python 数据结构断言）、
  DB/网络/文件系统（无）。
- **must-preserve**：`tests/test_production_scheduler.py` 全量绿；该测试既有的四条常量
  取值断言逐字保留；两个被断言模块 sha256 不变。

## 实测基线

编排者在**隔离副本**内实测（`git archive HEAD | tar -x`，并已证明 import 解析到副本而非
本仓 editable 安装；两个副本互不并发改同一棵树）。

### B-1 #1418 现行守卫的判别力（`-k no_second_permanent_code_refusal_list`）

复发变异 = 把旧黑名单以某种写法加回 `scheduler_state_failure.py` 并接进判据。

| 变异 | 结果 |
|---|---|
| M0 未变异 | 绿（1 passed） |
| N1 单行加回（换名常量，消费于判据函数内） | **绿（未抓）** |
| N2 多行、4 空格缩进加回 | **绿（未抓）** |
| N3 多行、8 空格缩进、原字面顺序 | 红（抓到） |
| N4 多行、8 空格、打乱一个相邻对（`MALFORMED_INPUT` 提前） | 红（抓到） |
| N6 多行、8 空格、**两个相邻对全打断**（`MALFORMED_INPUT` 居中） | **绿（未抓）** |
| N5 换名清单 `_DOWNSTREAM_EXTRA_REFUSAL_CODES` 消费在第三个函数 | **绿（未抓）** |

**精确刻画**（比 issue #1418 表格更窄，issue 说"改元素顺序 = 唯一被抓的一种"，实测是
*保留相邻对的*排列才被抓）：现行扫描只在**同时**满足「多行写法 + 恰好 8 空格元素缩进 +
两个钉死相邻对 `INVALID_MANIFEST`→`MANIFEST_SCHEMA_INVALID` 或
`MANIFEST_SCHEMA_INVALID`→`MALFORMED_INPUT` 至少留一个逐字完整」时才红。

### B-2 #1451 `active_blocker is False` 臂的判别力（全文件）

| 变异 | 结果 |
|---|---|
| M0 未变异 | 1731 passed / 144s |
| M1 删掉 `or job.get("active_blocker") is False` | **1731 passed**（全绿，变异存活）/ 187s |
| M2 改 truthy（`or not job.get("active_blocker")`） | **319 failed, 1412 passed** / 248s |

**M2 的结果证伪了 issue #1451 的一半前提。** issue 写的是「将来任何人把这一臂删掉、
**或把 `is False` 写成 truthy 判断**……CI 与本地全量都不会亮红」。实测：删臂确实全绿
（0 判别力，issue 的动态证据——9023 次调用 0 次判别性调用——刻画的正是**删臂**变异的判别域），
但 truthy 变异**打红 319 条**。原因是两种变异的判别域不同：删臂的判别域是
「`active_blocker=False` 且**无** `repair_status`」的行（仓内 fixture 里一条都没有），
truthy 的判别域是「**缺**该字段」的普通失败行（仓内 fixture 里遍地都是）。

**因此 AC-2 的买点必须重述**：矩阵第 4 行买的**不是**「从无覆盖到有覆盖」，而是
「把 319 条远端偶然失败换成一条**指名的、就在谓词旁边的**失败」。定位成本与意图表达是
真实收益，判别力增量是零。这条必须在 PR body 里如实写，不得沿用 issue 的措辞。

**对变异验收的直接后果**（tasks §5）：M2 在基线上就红，所以「改后 M2 红」**不证明任何事**。
M2 的指名 oracle 只能是**矩阵第 4 行本身出现在失败清单里**。M1 相反：基线全绿，
「改后 M1 红」是真实购买，其指名 oracle 是矩阵第 2 行出现在失败清单里。

## D1 — issue #1418 提议的 AST 不变量按原文写在 HEAD 上会**直接红**，须改主体

issue 的建议是「断言每个 frozenset 常量的引用点只出现在 `_remedy_permits_permanent_failure`
或 `_downstream_failure_restartable` 的 placeholder 分支内」。实测该模块的模块级常量盘点：

| 坐标 | 常量 | 消费函数 |
|---|---|---|
| `:65` | `_COPYBACK_REQUIRED_RESTART_STAGES` | `_missing_upstream_forecast_artifact_evidence` |
| `:73` | `_ARTIFACT_PROBE_ERROR_REASON` | `_artifact_uri_missing_status`, `_missing_upstream_forecast_artifact_evidence` |
| `:80` | `_ARTIFACT_TARGET_NOT_A_FILE_REASON` | `_artifact_uri_missing_status` |
| `:84` | `_NON_REGULAR_OBJECT_KINDS` | `_object_artifact_target_is_not_a_file` |
| `:177` | `_REMEDY_NON_CAUSAL_CLASSIFIERS` | `_remedy_permits_permanent_failure` |
| `:200` | `_REMEDY_NON_CAUSAL_CODES` | `_remedy_permits_permanent_failure` |
| `:214` | `_CHANGED_MODEL_PACKAGE_NON_CAUSAL_CLASSIFIERS` | `frozenset()`（无函数消费者，仅经 `:218` 表在**模块级**间接） |
| `:215` | `_CHANGED_MODEL_PACKAGE_NON_CAUSAL_CODES` | `frozenset()`（无函数消费者，仅经 `:222` 表在**模块级**间接） |
| `:218` | `_REMEDY_NON_CAUSAL_CLASSIFIER_TABLE` | `_remedy_permits_permanent_failure` |
| `:222` | `_REMEDY_NON_CAUSAL_CODE_TABLE` | `_remedy_permits_permanent_failure` |
| `:312` | `_RECORDED_FAILURE_CODE_KEYS` | `_downstream_recorded_error_code` |
| `:320` | `_HYDRO_RUN_CODE_CLEARING_STATUSES` | `_downstream_recorded_error_code` |
| `:520` | `_DOWNSTREAM_FORECAST_OUTPUT_DEPENDENT_STAGES` | `_missing_forecast_output_recompute_evidence` |
| `:521` | `_MISSING_FORECAST_OUTPUT_RECOMPUTE_CODES` | `_missing_forecast_output_recompute_evidence` |
| `:934` | `_FORCING_SIDECAR_FILENAME` | `_forcing_sidecar_provenance` |
| `:935` | `_FORCING_PACKAGE_MANIFEST_FILENAME` | `_package_manifest_probe_uri` |
| `:943` | `_FORCING_SIDECAR_MAX_BYTES` | `_forcing_sidecar_provenance` |
| `:1570` | `_DOWNSTREAM_PLACEHOLDER_REFUSAL_CLASSIFIERS` | `_downstream_failure_restartable` |

18 个常量里有 **11 个**被判据函数之外的函数正当消费（stage 集合、object kind、
sidecar 文件名、recompute 码表……），另 2 个（`:214`/`:215`）根本没有函数消费者。
按 issue 原文断言在 HEAD 上必红，具体条数随主体读法而异，三种计法都 > 0：

- 按 D1 采用的判据（**任意**模块级常量、被判据函数之外的函数消费）：**11 条**。
- 按 issue 字面的主体（"收集所有 **frozenset 字面量**常量"，实测该模块只有 7 个值是
  `frozenset(...)` 调用）：以**函数体**引用计 **2 条**（`_NON_REGULAR_OBJECT_KINDS`、
  `_HYDRO_RUN_CODE_CLEARING_STATUSES`）；若把**模块级**引用也算进"引用点"，再加
  **两条**——`_REMEDY_NON_CAUSAL_CODES`(`:200`) 与
  `_CHANGED_MODEL_PACKAGE_NON_CAUSAL_CODES`(`:215`)，二者都被 `_REMEDY_NON_CAUSAL_CODE_TABLE`
  (`:222`) 在模块级引用，而 issue 的白名单**只写了** `_REMEDY_NON_CAUSAL_CLASSIFIER_TABLE`
  这层间接、**没写** code 表 —— 合计 **4 条**。（一稿这里写的是 3，漏了 `:200`，
  是修"12→11"时新引入的错数，round-2 P2-2 抓出。）

**issue 的验收标准（AC-1..AC-6）全部成立且不变，被否的只是它的"解决思路"那一段。**

三条被否的备选主体，各自的证伪：

- **按名字前后缀选主体**（只看 `*_CODES` / `*_CLASSIFIERS`）：AC-3 明写要抓
  **换名**的第二份清单，按名字选主体等于把 AC-3 的攻击面排除在主体之外。自相矛盾。
- **按内容选主体**（元素形如大写 reason code）：`_MISSING_FORECAST_OUTPUT_RECOMPUTE_CODES`
  （`:521`）就是一组大写 reason code，却被 recompute 通道正当消费——内容启发式今天就有
  假阳性，须再挂白名单，白名单又把 AC-3 打回原形。
  （附注，避免与上面的条数混读：这个证人的运行时类型是 `set` 而**非** `frozenset`，
  所以它落在 issue **字面**主体之外、却落在**内容**主体之内——这恰好是两种读法条数不同的
  原因之一，也说明"按值的容器类型选主体"同样不可靠。）
- **只用行为守卫**：见 D3，行为守卫只覆盖 downstream 这一条腿，抓不到换名清单接到别的通道上。

## D2 — 结构守卫：钉死**全量模块级常量 → 消费函数**映射

主体 = `scheduler_state_failure.py` 的**每一个**模块级常量赋值，**`ast.Assign` 与
`ast.AnnAssign` 都算**；产物 = `{常量名: frozenset(消费它的函数名)}`；断言 = 与钉死的
期望映射**整体相等**。

**主体必须 fail-closed（fixture 评审 F1，已实测）。** 只认「单 `Name` 目标的
`Assign`/`AnnAssign`」而对其余绑定形式**静默放行**，会留下一串已实测的逃逸口——
下列写法在 HEAD 上产出的映射与基线**逐字节相同**（守卫绿）：

| 逃逸写法 | 为什么逃逸 |
|---|---|
| 元组解包 `_X, _S = frozenset({...}), None` | `ast.Assign` 的 target 是 `Tuple` 不是 `Name` |
| `if True:` 块内赋值 | 节点不在 `tree.body` 顶层 |
| `try:/except:` 块内赋值 | 同上 |
| `class _C: CODES = frozenset({...})` + `_C.CODES` 消费 | 类体不在模块级常量主体内 |

元组解包那一支评审者做成了**活体复发**并实测：把码清单查询插进
`_remedy_permits_permanent_failure` **体首**，
`_remedy_permits_permanent_failure({"error_code": "INVALID_MANIFEST", …}, remedy="raw_input_reingestion")`
从 `True` 翻成 `False`——**生产语义真的变了**，而映射 diff 为空、现行字符串守卫 `1 passed`。
行为守卫（D3）只覆盖 downstream 腿，对这一支同样失明：**两条合取守卫全漏**。

### fail-closed 必须写成 accept-set + catch-all，不能只写 refuse-list（round-2 P1-1）

"遇到任何未识别形式就抛错"这句**字面不可实现**：`Import`/`ImportFrom`/`FunctionDef`
本身就是模块级绑定形式，字面实现在 HEAD 第一行就拒
（HEAD body：`ImportFrom×18, Import×4, Assign×16, AnnAssign×2, FunctionDef×59, ClassDef×1`）。
实施者**被迫**发明一个放行集——而初稿对这个放行集一个字没写，于是两种读法都"合规"：

- **Impl-A**：显式 accept-set + catch-all `else: raise`；
- **Impl-B**：只拒枚举的那几种、其余静默跳过。

**两者在 HEAD 上都绿、映射同为 18 键**，但 Impl-B 漏掉 `match`-case 体、PEP695 `type` alias、
`AugAssign`、`global` 安装——round-2 已用 **`match`-case 体**做出活体复发：
结构守卫绿、映射逐字节相同，而 `_remedy_permits_permanent_failure` 的裁决 `True → False`。
这是 round-1 F1 的原形在一个满足其修复文本的实现下重现。

因此规范必须是：**显式枚举 accept-set**（`Import` / `ImportFrom` / `FunctionDef` /
`AsyncFunctionDef` + 唯一按名放行的 `ClassDef`），单 `Name` 目标的 `Assign`/`AnnAssign` 入主体，
**其余 catch-all `else: raise`**——让 `Match`/`TypeAlias`/`AugAssign` **以及未来新增的语句形式**
按构造被拒，而不是靠枚举追。`ClassDef` 放行的是 `_ForcingSidecarProvenance`
（`:947`，只有字段声明、无清单常量）**这一个名字**，不是"所有类"。
`ImportFrom` 的 `*` 必须显式拒：星号导入下 AST 不知道绑了哪些名字。

编排者按 Impl-A 实现了参考主体，对 **10 种**写法逐一实测：普通赋值（对照）与海象
`_ = (_X := ...)` 由**映射变化**打红；元组解包、`if` 块、`try` 块、`for` 绑定、`with` 块、
新增类的类属性、`setattr(sys.modules[__name__], ...)`（目标是 `Attribute` 非 `Name`）
七种由**拒绝**打红。round-2 另在 Impl-A 上验了 `match` / `TypeAlias` / `AugAssign` 三种，
同样被 catch-all 拒。

**`Expr` 的裁定**：HEAD 该模块模块级 `Expr` 数为 **0**（无 module docstring），
所以不放行 `Expr` 今天能顺带拦住 `global` 安装的那句裸调用——但那是**撞上的不是设计的**，
补一句 docstring 即假红。**本 change 选择放行 `Expr`**，`global`/`setattr` 一类交给下面的
运行时交叉核对兜底；实现须在注释里写明这个取舍。`ruff format` 不新增 `Expr`，M10 不受影响。

### fail-closed 仍不够：`global` 安装是唯一漏网，须加运行时交叉核对

```python
def _install_extra_refusal() -> None:
    global _DOWNSTREAM_EXTRA_REFUSAL_CODES
    _DOWNSTREAM_EXTRA_REFUSAL_CODES = frozenset({"INVALID_MANIFEST"})


_install_extra_refusal()
```

实测：变异后模块 body 只多出 `FunctionDef` 与 `Expr`(调用)，**两个都是已识别形式**，
所以既不入主体、**也不触发拒绝**；且因该名字不在主体里，它在判据函数里被消费时
消费者映射同样不变——纯语法主体对它**完全失明**。

注意措辞层面的陷阱：规格句写的是「**模块级绑定形式**未被识别时须拒绝」，而 `global`
赋值在语法上**不是**模块级绑定形式，是函数体语句在运行时绑定模块级名字。
所以那句话**没有被字面证伪，被绕过的是它的目的**——本役第三次遇到"标题为真、意图为假"。

**修法（已实测）**：在语法主体之外加一条**运行时交叉核对**——AST 盘点出的名字集合，
必须等于**导入后模块对象上实际存在的模块级常量名**集合（`vars(module)` 去掉 dunder、
去掉本模块 AST 里的 import 名与 `def`/`class` 名、去掉模块对象与本模块定义的可调用对象）。

| 情形 | AST 盘点 | 运行时 | 结论 |
|---|---|---|---|
| HEAD 未变异 | 18 | 18 | 双向差集为空 → 绿（无假阳性） |
| `global` 安装变异 | 18 | **19** | 运行时多出 `_DOWNSTREAM_EXTRA_REFUSAL_CODES` → **红** |

round-2 独立复核把这条的射程测全（同一形状、四种造名通道，均在隔离副本内）：

| 运行时造名通道 | 交叉核对结果 |
|---|---|
| `global` 安装 | STRAY `_DOWNSTREAM_EXTRA_REFUSAL_CODES` → 红 |
| `globals()["_X"] = …` | STRAY 同上 → 红 |
| `setattr(sys.modules[__name__], …)` | STRAY 同上 → 红 |
| 装饰器 mutate `globals()` | STRAY 同上 → 红 |
| star-import | **名字比对关不掉**——须由显式拒 `*` 关闭 |

两个实现要点（否则这条会误报）：AST 侧的"已绑定名字"集合**必须**把
`Import`/`ImportFrom`/`FunctionDef`/`ClassDef` 绑定的名字也算进去，否则会吐出 11 个
假 stray（`ACTIVE_PIPELINE_STATUSES`、`ENOENT`、`UTC`、`annotations` 之类全是 import 进来的）；
加上之后 `运行时 − 已绑定` 在 HEAD 上恰为空。第二，`ImportFrom` 的 `*` 必须**显式拒**，
那是本条靠名字比对关不掉的唯一支路。

把主体从"语法形状枚举"升级成"语法与运行时一致性"之后，整个"运行时造名"类一次关闭。

**`AnnAssign` 不是可选项。** 本模块最要害的两份清单载体
`_REMEDY_NON_CAUSAL_CLASSIFIER_TABLE` / `_REMEDY_NON_CAUSAL_CODE_TABLE`（`:218` / `:222`）
正是注解赋值（`: dict[str, frozenset[str]] = {...}`）。编排者盘点脚本第一版只处理
`ast.Assign`，这两条**整个不在盘点里**——一份写成注解赋值的第二清单会完全隐形。
实施者若只实现 `ast.Assign`，M4 变异会存活。

**同理，值形状不得用作筛选条件**：那两张表的值是 dict-of-Name，`ast.dump` 里**没有**
`frozenset` 字样（`frozenset` 只出现在**注解**里）。任何"值里含 frozenset / 是 set 字面量"
的筛法都会漏掉它们。主体就是"全部模块级常量"，不做形状筛选。

这条守卫杀死的变异形状：

1. 新增任何模块级常量（单行 / 多行 / 任意缩进 / 任意元素顺序 / 任意名字）→ 多一个键。
2. 既有清单多出**第二个消费函数**（例：`_REMEDY_NON_CAUSAL_CODES` 同时被
   `_downstream_failure_restartable` 咨询）→ 该键的值集合变化。**这一条才是 AC-1 的真不变量**
   ——"只有一个被咨询的判据源"。
3. 换名清单 + 新判据函数（N5）→ 同时命中 1 与 2。

**声明的摩擦**：任何**正当**新增模块级常量的改动都要同步更新期望映射。这是**有意**的：
AC-3 要求"新增一份换名清单要变红"，而"变红"与"正当新增也变红"在结构上是同一件事，
不可能只要前者。18 条的映射是可读的，更新成本是一行。

**声明的盲区**（本 change 不关闭，见 D3 与 Non-Goals）：

- 写在**函数体内联**的拒绝集合（`if code in {"MANIFEST_SCHEMA_INVALID", ...}`）没有模块级
  常量，结构守卫看不见。→ 由 D3 的行为守卫在 downstream 腿上覆盖。
- 写在**别的模块**里的第二份清单。#1313 AC-1 的原文就是模块内命题，不扩。
- 写成**类属性**的清单。本模块今天有且只有一个模块级 `ClassDef`
  （`_ForcingSidecarProvenance`，`:947`，六个字段声明，无清单）。上面的 fail-closed 主体
  把它**按名显式放行**，所以「在这个类里加清单」仍然是盲区；但**新增**任何其它类会直接
  打红（未在放行名单上），这正是 fail-closed 与静默放行的差别。
  实测依据：模块 body 节点只有 `Import`/`ImportFrom` ×22、`Assign` ×16、`AnnAssign` ×2、
  `FunctionDef` ×59、`ClassDef` ×1——**无**模块级 `if`/`try`/`for` 块、无 `AugAssign`、
  无元组解包目标，故 16+2=18 就是**今天** HEAD 上模块级常量的全集，不是抽样；
  但"今天没有"不等于"守卫可以假设没有"，这就是 F1 的教训。
- **模块级引用不计入消费者**（fixture 评审 F10）：值集合只统计**函数体内**的引用，
  所以形如 `_TABLE["x"] = _SOME_CODES` 的模块级消费既不产生新键也不改任何值集合。
  今天 `:214`/`:215` 正是这种形状（只经两张表在模块级间接）。这条盲区不扩主体来治：
  真要治得把模块级引用也归到一个伪函数名下，那会让期望映射多出一层不好读的结构，
  而它能挡的形状（把清单挂到模块级表里再由已有消费者读）已经被"表本身的消费者集合变化"
  与 1.5 的取值断言双重覆盖。
- 常量被**重命名**但消费者不变：映射变化 → 红。这是假红（正当重构），按上面的摩擦声明处理。
- **同名重复赋值**：在模块后段再写一次
  `_REMEDY_NON_CAUSAL_CODES = frozenset({...旧黑名单...})`，键集合与消费映射**逐字不变**，
  结构守卫绿。堵这个洞的是 task 1.5 **保留**的四条常量取值断言——import 时后赋值胜出，
  模块属性的实际取值与钉死值不等即红。
  **两条精确化（fixture 评审 F8）**：(a) 这只覆盖被取值断言点名的**那 4 个**常量，
  另外 14 个（如 `_DOWNSTREAM_PLACEHOLDER_REFUSAL_CLASSIFIERS`）的重复赋值对两条守卫都隐形；
  (b) 真正抓住 `_REMEDY_NON_CAUSAL_CODES` 重复赋值的是那条**直接**取值断言，**不是**表断言
  ——表在 `:222` 求值时就绑定了旧 frozenset 对象，后段重赋值改不到表里那个引用。
  **因此 task 1.4 删掉 `assert source.count("_REMEDY_NON_CAUSAL_CODES = ") == 1` 之所以安全，
  唯一理由就是 1.5 保留了取值断言。** 这两条是绑定的：将来任何一轮若以"与结构守卫重复"
  为由裁掉取值断言，这个洞立刻重开。谁动 1.5，谁负责先补回一条重复定义守卫。

## D3 — 行为守卫：`_downstream_failure_restartable` 的裁决与 `reason_code` 无关

HEAD 形状（`scheduler_state_failure.py:1588-1592`，逐字；`def` 行在 `:1575`）：

```python
if failure.get("limit_exhausted") is True:
    return False
if not code_recorded:
    return str(failure.get("classifier") or "") not in _DOWNSTREAM_PLACEHOLDER_REFUSAL_CLASSIFIERS
return not failure.get("permanent")
```

`code_recorded=True` 时裁决 = `not failure.get("permanent")`（`limit_exhausted is True` 先行
短路），**完全不读任何码字段**。参数化断言这条恒等式，在码轴上必须**包含三条已退役的
黑名单码**（`INVALID_MANIFEST`、`MANIFEST_SCHEMA_INVALID`、`MALFORMED_INPUT`）——它们正是
复发时会被写回清单的那三个。

**每行必须同时设 `error_code` 与 `reason_code` 两个键。** 本模块读码走的是键链
（`_RECORDED_FAILURE_CODE_KEYS`，`:312`，由 `_downstream_recorded_error_code` 消费），
一条写成读 `error_code` 的内联复发，在只设 `reason_code` 的用例下会整片逃逸。
两个键同值是零成本的，换来的是"码字段的哪一种拼法都堵上"。

**判别力的边界是钉死的码轴本身**：一条只对**轴外**新码生效的依赖
（`if code == "SOME_NEW_CODE": return False`）不会被打红。这不是缺陷而是刻度——
要买更宽的判别力只能加宽码轴。规格 delta 的对应场景已按这个边界写，不写成全称。

这条守卫杀死结构守卫看不见的形状：任何按码清单的二次拒绝，**无论写成模块常量还是函数内联、
无论什么名字什么缩进**，只要接在这条腿上，就会让某个 `(码, permanent=False)` 行从 True 翻成
False → 红。

**声明的边界**：只覆盖 downstream 这一条腿。换名清单接到 raw-manifest 或 model-package
通道上，本守卫抓不到——那由 D2 的结构守卫接（那两条通道的清单都是模块级常量）。
两条守卫是**合取**，各自堵对方的盲区，这是采纳 issue「两条可以合取采用」的理由。

`code_recorded=False` 分支**不在**本守卫的断言域内：该分支按 #1313 D4 明确保留
pre-#1313 的 classifier 拒绝行为，`_DOWNSTREAM_PLACEHOLDER_REFUSAL_CLASSIFIERS` 是它
**正当的**唯一清单。把它卷进"无二次拒绝"的断言会直接红，且会与 #1313 D4 打架。

## D4 — #1451：三态矩阵 + 兄弟副本

谓词 HEAD 形状（两处**逐字相同**、独立函数对象）：
`services/orchestrator/scheduler_state_rows.py:860-861`、
`services/orchestrator/chain_source_cycle.py:512-513`。

```python
return job.get("repair_status") == "repaired" or job.get("active_blocker") is False
```

矩阵轴 = `active_blocker ∈ {False, True, 缺失}` × `repair_status ∈ {"repaired", 缺失}`，
行状态固定为 `permanently_failed`（`_job_row_is_live_failure` 域内的失败状态）：

| # | 行 | 谓词 | `_job_row_is_live_failure` | 钉住什么 |
|---|---|---|---|---|
| 1 | `{status, repair_status: repaired}` | True | False | 既有覆盖（不新增判别力） |
| 2 | `{status, active_blocker: False}`（**无** repair_status） | **True** | **False** | **删臂变异**（AC-1） |
| 3 | `{status, active_blocker: True}` | False | True | 臂的负向 |
| 4 | `{status}`（两字段都缺） | **False** | **True** | **truthy 变异**（AC-2）：`not job.get(...)` 会把它判成 True |
| 5 | `{status, repair_status: repaired, active_blocker: True}` | True | False | 第一臂遮蔽第二臂（仓内生产写入方形状之外的组合） |

第 2 行杀 M1（删臂），第 4 行杀 M2（truthy）。**两行缺一都留一个存活变异**：只有第 2 行时
truthy 变异仍绿（`not False` = True，与期望同值）；只有第 4 行时删臂变异仍绿。

兄弟副本：同一矩阵对 `chain_source_cycle._pipeline_job_is_repaired_stage_evidence` 再跑一遍
（参数化里带函数对象轴），顺带钉住两副本不漂移。
`tests/test_production_scheduler.py` 今天**没有** import `chain_source_cycle`，需新增
module 别名导入——这是本 change 对该测试文件 import 段的唯一改动。

## D5 — 零生产改动

两个模块只作为被断言对象。交付后编排者以 `sha256` 校验
`services/orchestrator/scheduler_state_failure.py`、`scheduler_state_rows.py`、
`chain_source_cycle.py`、`scheduler_state_manual_retry.py` 四个文件与 base 逐字节相同。

## 变异清单

实施者交付后，编排者/审查者在**隔离副本**内逐条执行；每条必须由**指名的**用例打红。

| # | 变异 | 指名 oracle | 必须 |
|---|---|---|---|
| M1 | 删 `or job.get("active_blocker") is False`（`scheduler_state_rows`） | #1451 矩阵第 2 行**出现在失败清单里**（基线全绿，真实购买） | 红 |
| M2 | 改 `or not job.get("active_blocker")` | #1451 矩阵第 4 行**出现在失败清单里**（基线已 319 红，"变红"本身不证明任何事） | 红 |
| M3 | 同 M1，但改 `chain_source_cycle` 那份副本 | #1451 矩阵（兄弟副本轴）第 2 行**出现在失败清单里** | 红 |
| M4 | 以 **`AnnAssign`** 形式新增第二份码清单（`_X: frozenset[str] = frozenset({...})`）并接进 `_remedy_permits_permanent_failure` | 结构守卫 | 红 |
| M5 | N1 形状（单行新增常量 + 判据函数内消费） | 结构守卫 | 红 |
| M6 | N5 形状（换名清单 + 第三个消费函数） | 结构守卫 | 红 |
| M7 | 既有 `_REMEDY_NON_CAUSAL_CODES` 追加第二个消费者（不新增常量） | 结构守卫（值集合变化） | 红 |
| M8 | **函数内联**码清单接进 `_downstream_failure_restartable` 的 `code_recorded=True` 腿 | 行为守卫的 **`permanent` 假 × 无 `limit_exhausted` × 码在钉死轴上** 那些行（`permanent=True` 的行对 M8/M9 零判别力） | 红 |
| M9 | `_downstream_failure_restartable` 的 `limit_exhausted` 短路删除 | 行为守卫的 **`limit_exhausted=True` × `permanent` 假 × `code_recorded=True`** 那一行——实测差异域**只有这一格**（fixture 评审 F2），光有 `limit_exhausted=True` 轴杀不掉它 | 红 |
| M10 | 对 `scheduler_state_failure.py` 跑 `ruff format`（**语义不变**） | 两条守卫 | **绿**（AC-4：无格式敏感假红） |

M10 是**反向**用例：它必须绿。现行字符串扫描在 M10 下是双向假信号来源
（一次换行调整就能让它无故红或无故绿），这正是 #1418 要治的第二个病。
