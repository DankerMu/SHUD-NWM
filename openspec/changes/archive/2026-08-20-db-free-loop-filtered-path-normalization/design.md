# Design: db-free-loop-filtered-path-normalization

> **坐标取景**：本文件所有 `:NNNN` 均为**规划期坐标**，量于实现前基线（`origin/master`，
> 2026-08-20），**有意不随实现漂移**——它们记录「当时在哪里决定的什么」，不是终态位置。
> 定位终态代码请按**符号名** grep。
> **本豁免不适用于生产代码与测试中的注释坐标**——那些必须是终态值。

## 风险分级

**Fixture level: expanded**（`symlink` 与 `path` 两个强制触发器同时命中）。

选中的 risk pack：`filesystem-boundary`（收容判据 fail-open）、`cross-version-behavior`
（两条 CPython 臂）、`oracle-integrity`（需翻转一条既有 pin）。
未选中：`concurrency`（无共享状态）、`data-migration`（无持久化改动）、
`auth-boundary`（不涉权限面）。

## 实测基线（本地 3.14.2，`origin/master`）

tmp base 一律先 `os.path.realpath` —— macOS `/var → /private/var`，等值断言不先规范化必然误判。

| 输入 | 站点 | 现状产出 | 判别力 |
|---|---|---|---|
| `<clean-root>/never-created/state.json` | B1 | `None`（admitted） | **必须保持**（ENOENT 回退是刚需，见 D3）|
| 同上，值等价 | B1 | `resolve(strict=False)` == `realpath` 非 strict → `True` | 字节兼容锚 |
| `<clean-root>/loop_a/state.json` | B1 | `None` ← **缺陷** | 有 |
| `<clean-root>/loop_a`（直接环） | B1 | `None` ← **缺陷** | 有 |
| `<tmp>/never-created/../loop_a`（phantom） | A1 | `roots=(loop_a,)`, `rejected=[]` ← **缺陷** | 有（**唯一**载体）|
| `<tmp>/loop_a`（直接环） | A1 | `roots=()`, 一条 `db_free_allowed_root_unresolvable` | **无**（ELOOP 臂已治，见 D7）|

原语层（三解释器，引 #1427 证据 3 / #1400 证据 3，本地只能跑 3.14 臂）：

```
direct  realpath strict=True  -> OSError ELOOP(62)     3.11 / 3.12 / 3.14 一致
phantom realpath strict=True  -> OSError ENOENT(2)     3.11 / 3.12 / 3.14 一致
phantom realpath 非 strict    -> <tmp>/loop_a（不抛）   3.11 / 3.12 / 3.14 一致
Path.resolve(strict=False) 环路 -> ≤3.12 抛无 errno RuntimeError；3.13+ 不抛、原样返回
```

---

## D0 — 机制：strict realpath + errno 分流 + loop-filtered admit，任何形态的 `Path.resolve()` 都不用

**裁定**：逐字复用 `_local_runtime_root_safety`（`services/orchestrator/retry.py:1533`，
PR #1426 落地）已定型的范式。

**依据（机械，非偏好）**：`Path.resolve()` 两种形态都不是可用的环判据——非 strict 形态在
3.13+ 不抛（GH-113838）、strict 形态在 ≤3.12 抛**无 errno 的 `RuntimeError`**，
而 `os.path.realpath(..., strict=True)` 在 3.11/3.12/3.14 上一致抛 `OSError` 且带 errno
（上表原语层）。errno 才是本范式读的东西，异常**类型**逐版本不同。

**否决的备选**：只在 `except` 元组补 `RuntimeError`（#1400「备选」原文）——一行修好 ≤3.12 臂，
但 3.13+ 臂仍是死码 + 词法放行，且与已统一的 errno 范式分裂成两套判据。#1400 自己标注「不推荐」。

**否决的备选 2**：`os.path.normpath` 词法折叠后再 strict 复查——normpath **抹掉 symlink 重定向**，
可能凭空造出运维从未批准过的根。#1348 家族 design D2 已否决过同一路子（#1427「备选」原文）。

## D1 — A2（`scheduler_config._db_free_allowed_roots_and_blockers`）出局，且家族口径另行路由

**裁定**：`scheduler_config.py:1097` 与 A1 是**同形缺陷**（ENOENT 臂 `:1123`
`Path(os.path.realpath(expanded))` 直接采信，无二次复查），本 change **不改**。

**依据**：#1427 边界原文逐字列它为「不修的兄弟副本」，并给出理由——数处兄弟（
`scheduler_runtime_roots.py:509-512`、`scheduler_preflight.py:539-542`）的注释把
「回退产物包含 `<missing>/../<loop>` 形状」写成**有意为之**，与 PR #1426 采纳的
loop-filtered admit 是**两套相反教条**。在本 change 里顺手改一个兄弟，等于用沉默替代
那次显式裁决。

**残留处置（已办，非待办）**：本 PR 合并即关闭 #1427，那条家族级教条冲突（约 8 处兄弟站点）
随之失去 open tracker。故该家族级裁决 issue 已**提前到合并前**由 issue-scribe 开出——
**#1627**——而不是留到 Phase 8（tasks 5.2）。两条 live spec 的指针已改指它。

**兄弟站点坐标（fixture review P2-6 更正后，已独立复量于 base）**——这批将成为 5.2 路由
issue 的载荷，故必须准确，不得沿用 issue 原文坐标：

| 站点 | strict 调用实际位置 | 备注 |
|---|---|---|
| `scheduler_runtime_roots.py` | `:506`、`:572`、`:597` | `:509-511` 的「有意为之」注释坐标正确 |
| `scheduler_preflight.py` | `:541`、`:604` | 「有意为之」注释实为 `:544-547`（非 `:539-542`）|
| `scheduler_config.py` | `:1120`（A2；`:1094` 只是委托 return） | |
| `scheduler_state_failure.py` | `:1443`（全文件唯一 strict 站点） | issue 原文 `:1114-1117` 实为 object-store prefix 的 docstring |
| `workers/model_registry/basins_package.py` | `:2770`（`_resolve_package_path` `:2764-2775`） | |
| `workers/model_registry/basins_discovery.py` | `:664`（`_safe_resolve_under_root` `:652-670`） | issue 原文 `:519-538` 实为 `_count_csv_files` |

另有一个**不同子族**的兄弟（P3-1 发现，非 ENOENT-无复查族，而是 `Path.resolve` 族）：
`scheduler_config.py:934` `_safe_preserve_final_component` 的 `path.parent.resolve(strict=False)`，
db-free 下经 `_confined_path_for_mode` 的回退臂（`:990-996`）可达。一并列入 5.2 载荷。

**同源旁证，及初稿在此处的一个错判**：
`openspec/specs/runtime-evidence-and-operations/spec.md:176-177` 把本 phantom 几何写作
「the **already-tracked** #1427 adjacency and is documented, not changed, here」。

初稿据此裁定「该句在本 change 后仍然为真，不需更正」。**该裁定错了**（round-1 lens C P3-1，
verifier V5 CONFIRMED）——它只审了句子的**几何**半边（preflight 腿确实没动，这半边为真），
漏了句子真正的承重词是 **already-tracked**：本 PR 关闭 #1427，合并瞬间该几何就无人跟踪了。

同类错判**在另一个 spec 文件里还有一处，而初稿连查都没查**：
`openspec/specs/slurm-array-runner-integration/spec.md:106-109` 写着
`_safe_preserve_final_component` 的 ≤3.12 残留「belongs to **issue #1400**」——本 PR 同样
关闭 #1400 且同样不修该臂。初稿只 grep 了 `#1427` 的孪生指针，没 grep `#1400` 的。

**处置**：家族级裁决 issue **#1627 已在合并前开出**（不再等 Phase 8），两条 live spec 均已
改指它，并各留一句过去式历史陈述。教训记此：审「某句是否仍为真」时，必须逐个从句审，
且必须对**本 PR 关闭的每一个 issue 号**做全仓指针扫描，而不只是当下正在讨论的那一个。

## D2 — path 级（B1/B2）的 ENOENT 回退**也**做 loop-filtered 复查

**这是本 change 里唯一没有现成先例的裁定，故展开。**

**张力**：仓库里并存两套已成文的相反教条——

- `openspec/specs/job-retry-mechanism/spec.md:1566`（runtime roots，PR #1426）：
  ENOENT 回退**必须**二次 strict 复查，phantom 形态**拒收**。
- `openspec/specs/job-retry-mechanism/spec.md:1484`（artifact 腿，issue #1402 / PR #1422）：
  phantom 根**保持 admitted**，明文记作「a known, recorded residual」。

**裁定：B1/B2 走 recheck 教条（`:1566` 侧）。**

**依据（机械）**：B1 **消费** A1 的产出——`_db_free_selector_path_rejection` 的
`allowed_roots` 形参就是 `_db_free_selector_allowed_roots` 的返回值。#1427 要求 A1 装
recheck；若 B1 的回退臂不装，则 A1 拒掉的那类环路会在**下一级**以 path 身份原样通过，
即在同一条腿上、同一个 PR 内**复刻本 PR 正在修的缺陷**。两级教条必须一致，否则收容判据
在腿内自相矛盾。

`:1484` 的 admitted 姿态是 artifact 腿的**局部**裁定（其 spec 文本本身把后续裁定链
逐条写死在同一段里），与本腿无消费关系，故不构成反例。本 change 不触碰该腿。

**#1400 的 AC 未禁止本裁定**：其 AC-1 只要求环路 selector path 产出
`db_free_selector_path_unresolvable`「或 errno 归类等价 reason」的 rejection。

## D3 — ENOENT 回退臂本身是刚需，不能取消

**裁定**：B1/B2 换成 strict realpath 后，**必须**保留非 strict 回退，否则「尚未创建的路径」
会开始被拒。

**依据（实测）**：`_db_free_selector_path_rejection("state_path", …, "<clean-root>/never-created/state.json")`
当前返回 `None`（admitted）。#1400 原文亦确认「本函数与其直接调用方**无任何 existence 复核**
（已逐行确认）」——提交期不做存在性检查是**设计意图**。B2 一侧更直接：它在
`scheduler_config.py:1219-1223` 另有 `db_free_required_path_parent_missing` blocker，
说明「最终分量缺失」在该站点是 admitted-by-design，缺失判定归下游而非归解析步。

**字节兼容锚（实测）**：clean-prefix / missing-suffix 形态下
`path.resolve(strict=False)` 与 `os.path.realpath(path)` 产出**逐字相等**（上表第 2 行）。
仓库内已有同一断言的成文依据：`scheduler_config.py:939` non-db-free 臂的注释
（该注释与其 `design D1` 引用由 `ac80e341` = **issue #1423 / PR #1522** 写下；
其 design D1 又转采 #1347 为**另一模块的另一个函数**所立的范式——round-2 lens C P2-2
更正，初稿此处只写「#1347 change design D1」是错的）写明非 strict `os.path.realpath()`「reproduces the product of the
old non-strict `Path.resolve()` verbatim -- POSIX order, symlinks first and `..` afterwards」。
即便如此仍**独立钉一条等价测试**（tasks 3.4），不靠注释背书。

## D4 — `ValueError` 折进既有出口，不新增 token

**裁定**：A1 的 strict 调用当前只 `except OSError`（`retry.py:1651`），B1 的
`except OSError`（`:1684`）同理——嵌入 NUL 的值抛 `ValueError` 会**逃逸**。本 change
按 `_local_runtime_root_safety` 的形状把 `ValueError` 一并折进既有的
`db_free_allowed_root_unresolvable` / `db_free_selector_path_unresolvable` 出口。

**性质**：这是**对齐先例**带来的附赠，不是偷运的行为变更——故在此显式登记，并由一条
NUL 用例钉住（**tasks 3.7**）。不登记的话 round-1 correctness 透镜会把它当未声明变更开单。

**B2 同折**（fixture review P2-4）：`_db_free_path_check` 今天对嵌 NUL 的值**直接抛
`ValueError`**（实测：`lstat: embedded null character in path`）。本 change 的 spec delta
明文要求该类落同一个 unsafe blocker，故 B2 的 `except` 元组必须一并补 `ValueError`
（tasks 2.2），否则交付即违反自己新增的 scenario。

**证据形状的残留**（fixture review P3-6，登记不修）：A1/B1 的 rejection 记录会把带 NUL 的
原始值写进 `value` 字段；`_bounded_redacted_text`（`retry.py:1837-1842`）只做脱敏与截断，
不剥控制字符，而 `json.dumps` 产出的 `\x00` 会被 PostgreSQL 的 `jsonb` 拒收。
先例 `_local_runtime_root_safety` 根本不回显值（返回 `(None, reason)`）故无此形状。
本 change **不改证据形状**（改了就超出两条 issue 的边界），tasks 3.7 只钉「无异常逃逸」；
该残留随 5.2 一并路由。

## D5 — B3 `_db_free_path_identity`（#1400 evaluate-only）：**改**

**裁定**：`scheduler_config.py:1143` 改为 `Path(os.path.realpath(path))`（去掉
`try/except` 与 `resolve`），**不改签名、不改消费方**。

**依据**：

1. **本身无 fail-open 后果**（#1400 D5 已裁）——消费方是 topology identity **比较**
   （`scheduler_config.py:777-810`），比较两侧由同一函数产出，自洽。
2. **但有版本分歧**：现状在 ≤3.12 对环路返回**未解析**的原始 path、在 3.13+ 返回**折叠**形。
   于是「环路 path 与其折叠等价物」这一对，在 3.13+ 判等、在 ≤3.12 判不等——
   同一份配置在两个生产解释器（3.11.15 / 3.12.7）与本地（3.14.2）之间得到**不同的 topology 裁定**。
   换成非 strict realpath 后两臂同产物，分歧消失。
3. **让守卫的 allowlist 只剩一项**：4.4 的守卫（round-2 后已改为「断言违规者」形状）
   要求列出模块内所有仍调用 `.resolve()` 的函数。B3 若保留 `.resolve()`，
   allowlist 就得从一项变两项；改掉它，`scheduler_config` 的 allowlist 精确等于
   `{"_safe_preserve_final_component"}`——**去掉一个特例，而不是新增一个特例**。
   **措辞纪律（fixture review round-1 P3-1）**：allowlist 里那一项是**故意保留**的，
   不是遗漏——
   `_safe_preserve_final_component`（`scheduler_config.py:932-936`）在 `:934` 另有一处
   live 的 `path.parent.resolve(strict=False)`，db-free 下经 `_confined_path_for_mode`
   回退臂（`:990-996`）可达。它属 5.2 载荷里的另一个子族，本 change **不动**——
   故它必须出现在 allowlist 里；漏掉它守卫落地即红。

**代价与两处更正（fixture review P2-5；round-1 lens A 追加第二处）**：初稿写
「非 strict realpath 永不抛」，**这是假的**，且假在两个方向上：

1. **`ValueError`（P2-5 已捉）**：实测（3.14.2）对嵌 NUL 的路径，
   `Path.resolve(strict=False)`、`os.path.realpath` 的 strict 与非 strict 形态
   **三者都抛 `ValueError`**。
2. **`OSError`（round-1 lens A）**：对**相对**路径，非 strict `os.path.realpath` 在 cwd
   不可用（cwd 目录被删）时抛 `OSError`——实测 `FileNotFoundError`，errno `ENOENT`。
   即「非 strict 形态不抛 `OSError`」这句只在本 lane 判别的输入类（symlink 环、缺失分量）
   上成立，**不是无条件真**。

准确表述是「非 strict realpath 对本 lane 判别的输入类不抛 `OSError` / `RuntimeError`，
但对不可表示的路径串抛 `ValueError`、对 cwd 不可用时的相对路径抛 `OSError`」。

两处更正**都不改变 D5 的结论**，但改变其论证：删掉 `except (OSError, RuntimeError)` 之后，

- `ValueError` 仍然逃逸——**而它今天也一样逃逸**（现有 except 元组同样接不住 `ValueError`），
  即 B3 **保留这条既有逃逸**，本 change 不新增也不消除它；
- `OSError`（相对路径 + cwd 不可用）则是**本 change 新放开的一条**——旧 handler 会接住它
  并原样返回入参。该类**登记而不重新加守卫**：B3 与 B4 不同，**没有 db-backed 臂可供取齐**
  （其消费方只比较它自己的产物），故这一半是**纯粹的守卫删除**；且**当下可达性为零**——
  现有全部调用点传入的都是绝对值。等价的相对值在 B4 那一侧根本到不了该 helper：
  `_optional_config_path_for_mode` 先用 `Path.cwd() / path` 绝对化，
  cwd 不可用时**在那一步就已抛**，改前改后同样。

此处显式记录，避免把一句假的规范性陈述写进 `openspec/specs/`（spec delta 已同步改写）。

## D6 — 翻转 `:17686` 那条 pin 是 oracle **增强**，不是削弱

**裁定**：`tests/test_production_scheduler.py:17686`
`test_tilde_residue_change_leaves_the_issue_1400_resolve_line_in_place`
断言 `_resolve_call_names(...) == ["resolve"]`，即**钉住 B1 恰好保留一个 `.resolve()`**。
本 change 把它翻成 `== []`（并入新 lane 元组）。

**为何这不是削弱 oracle**：它不是行为断言，是 #1436 那个 change 立的**范围栅栏**。
其注释原文：「Scope pin in the other direction: **#1400 owns**
`_db_free_selector_path_rejection`'s `path.resolve(strict=False)` and its `except OSError`.
This change must not have "helpfully" migrated it.」——它**自带退休条件**，且点名 #1400
为被授权的拆除者。本 change 正是 #1400。

**连带清单（fixture review P2-1 补全——初稿只列了 2 处，实为 4 处）**：
本 change 会让下列**每一条陈述**变成假话。断言本身全部存活，失效的是理由/范围声明，
故这是**注释与 docstring 的同步义务**，不是断言改动：

| 位置 | 现有陈述 | 失效原因 |
|---|---|---|
| `tests:17664-17670` | 把 retry 腿排除在 `_resolve_call_names` 之外，理由是「B1 keeps a `path.resolve(strict=False)` line that belongs to #1400」 | 该行被本 change 删除 |
| `tests:17686-17692` | 断言 B1 恰好保留一个 `.resolve()`（#1436 的范围栅栏，点名 #1400 为拆除者）| 本 change 即 #1400 |
| `tests:17436-17439` | 「The neighbouring `path.resolve(strict=False)` line is **#1400's territory and is deliberately untouched**」 | 同上（断言本身靠相对路径臂预占，存活）|
| `tests:16527-16533` | `..._keeps_graceful_degradation_for_symlink_loop`：「Control for the arm #1423 declares out of scope (**#1400 territory**)…**the fix must not drift it**」——**这条讲的正是 B4** | 本 change 按 AC-6 就是要 drift 它 |
| `tests:39652-39660` | 「B5: the db-free arm is a **declared non-goal and must not drift**…PR #831 lexical tolerance keeps producing the root itself」 | B4 落地后 db-free 臂与 db-backed 臂取齐 |

**留痕要求**：本裁定与上表必须出现在 PR body 的「计划偏离 / oracle 完整性」段，
供 Phase 7 oracle-integrity 复核直接对照——测试文本的删除/改写面不得静默。
遗漏其中任何一条，都会在测试文件里留下一句被本 change 悄悄证伪的范围声明。

## D7 — 判别几何与预占分析（写死，实施者不得自选用例）

**预占者完整清单**（fixture review P2-2 / Q3 补全；写用例前必须逐条绕开）：

*B1（`_db_free_selector_path_rejection`），按代码顺序：*

1. `not allowed_roots` 门（`retry.py:1672`）先答 `db_free_allowed_roots_missing`。
   **故「环路 path 配环路 root」永远不是本 change 的 oracle**——A1 修好后所有 root 被拒，
   答案被这道门预占。
2. `_URI_STYLE_RE`（`:1674`）→ `db_free_selector_path_uri`。
3. 非绝对臂（`:1680`）→ `db_free_selector_path_relative`。
4. containment 门（`:1687`）**位于 resolve 步之后**——注意本 change **反转了这对的先后**
   （round-2 P2-1）：改前 resolve 不抛，环路值一路走到这道门，越界环路答
   `outside_allowed_roots`；改后 strict 解析先答 `unresolvable`。
   **推论一**：只有**干净**的越界值仍走 containment 门；越界**环路**的 reason 变了（tasks 3.13b）。
   **推论二**：写 B1 用例时环路仍须**词法落在**已配置 root 之下——不是因为 containment 会预占
   （改后不会了），而是为了让用例证明的是「环被拒」而非「越界被拒」。

*A1（`_db_free_selector_allowed_roots`）：* `secret_manifest_value_reason`（`:1625`）、
`_URI_STYLE_RE`（`:1629`）、非绝对臂（`:1639`）均在 strict 解析之前。

*B2（`_db_free_path_check`），这条腿的门最多，也最容易踩空：*

1. URI 臂（`:1172-1184`）在 expanduser 之前。
2. 非绝对臂（`:1187`）。
3. `_db_free_local_path_component_reason`（`:1190`）——它拒收任何**字面** `..` 分量
   （reason `traversal`）。实测
   `<clean>/never-created/../loop_a → ('db_free_required_path_unsafe','traversal')`。
   **初稿由此推出「phantom 形态在 B2 根本不可达」，该推论过宽，已由 round-2 P2-6 推翻**：
   这道门只看**配置值自身的字面分量**，而一个**目标文本**携带 phantom 形状的 symlink
   （`<clean>/indirect -> "never-created/../ring-a"`）字面上没有 `..`，门不响，
   strict 解析吃 ENOENT、非 strict 回退折到 `<clean>/ring-a`、二次 strict 撞 ELOOP。
   **这是 B2 复查臂唯一可达的几何**（tasks 3.8b）——初稿的「B2 不要写 phantom 用例」
   恰好把实施者从唯一能证明该臂有效的用例身边支开了，删掉 B2 的复查臂将无人报警。
4. containment 门（`:1210-1218`）**位于 resolve 步之后**，与 B1 同样被本 change 反转
   （round-2 P2-1）：越界**环路**从 `('db_free_required_path_outside_boundary','outside_boundary')`
   变为 `('db_free_required_path_unsafe', <errno 归类>)`；干净的越界值不变。见 tasks 3.13b。
5. **父目录 `lstat` 门（`:1220-1231`）——初稿完全没列，正是它让「显而易见的几何」失明**：
   `<loop>/child` 今天就已经是 `('db_free_required_path_unsafe','unsafe')`，由这道门产出。
   在该几何上只断言 blocker **code** 的测试，改前改后都绿，什么也钉不住。
6. **命名陷阱**：既有的 B2 环路测试
   `test_db_free_required_path_symlink_loop_blocks_without_crash`（`tests:34521`）
   **压根到不了 resolve 步**——它的环路目录名叫 `secret-token-loop`，
   `_DB_FREE_CREDENTIAL_WORDS`（`scheduler_config.py:43-56`）先答 `credential_component`。
   新用例必须避开该表里的每一个词。

**隔离矩阵**：

| 要钉的站点 | 几何 | 传参方式 / 断言 |
|---|---|---|
| A1（#1427） | phantom root `<missing>/../<loop>` + 干净 path | 直接调 `_db_free_selector_allowed_roots` |
| A1 无回归① | 良性 phantom `<missing>/../<realdir>` | 对照 `tests:40499` 既有裁定 |
| A1 无回归②（**M2 的真 oracle**） | **纯未创建根** `<tmp>/not-yet/roots`（第二次 ENOENT 臂）| admitted、零 rejection |
| B1（#1400） | 环路 path 在**干净** root 之下 | `allowed_roots=` 直接传元组，绕开 A1 |
| B1 无回归 | `<clean-root>/never-created/state.json` | 断言仍 `None` 且产物逐字相等 |
| **B2** | **直接环**（`path` 就是环本身；名字避开 credential 词表）| **断言 `(code, reason)` 二元组**，不只 code：`('db_free_required_path_not_found','not_found')` → `('db_free_required_path_unsafe','unsafe_path')` |
| 级联 | 全部 root 被拒 → path 侧 `db_free_allowed_roots_missing` | 对照 `tests:40523` 同形断言 |

**tmp base 一律先 `os.path.realpath`**（macOS `/var` 陷阱，上文已述）。

## D8 — B4 不移植 errno 分流：与同函数的 non-db-free 臂取齐

**裁定**：`_resolve_config_path_for_mode`（`scheduler_config.py:939`）的 db-free 臂改成
与其 non-db-free 臂**同形**（`realpath(strict=True)` → `except OSError` → 非 strict 回退），
**不**加 rejection/blocker 分流。

**依据**：该函数**没有拒绝通道**——签名返回 `Path`。分类归下游，这一点是该函数
non-db-free 臂注释自己写的（引 #1347 design D1）：「classification belongs to the storage
preflight, not to config construction」「Splitting on errno would buy nothing, because both
would-be lanes converge on this same product」。#1400 AC-6 括号里的「strict realpath + errno
分流」是**过度指定**；AC-6 的实质要求是「两解释器臂产出同一规范形」，取齐即达成。

**行为差的准确口径与其不可本地观测性（fixture review P1-1 更正初稿）**：

初稿要求「刻意钉住 ≤3.12 的行为差：改前返回未解析原始 path，改后返回折叠形」。
**该要求在本地不可满足**——在 3.13+ 上 `Path.resolve(strict=False)` **就是**
`os.path.realpath(p, strict=False)`，故改前改后对每一种几何都产出同一个值（实测五种形状
`cur == prop` 全为 `True`）。任何为它写的本地测试都是**盲 oracle**。

两条初稿未言明的补充：

- 即便在 ≤3.12 上，该差异也需要一个**可折叠的前缀**才显形——裸的 `<realdir>/loop_a`
  两臂都返回 `<realdir>/loop_a`；几何必须是 `<symdir>/<loop>` 或 `<dir>/../<loop>`
  （#1400 自己的措辞）。
- D9 的论证性结清链原本只 scope 到 AC-3（retry 腿），**未覆盖 B4**。

**故改钉「双臂同产物」**——那才是 AC-6 的实质要求（原文：「两解释器臂产出同一规范形」）：

```
_resolve_config_path_for_mode(v, db_free_required=True)
    == _resolve_config_path_for_mode(v, db_free_required=False)   # v 取 <symdir>/<loop> 形
```

**该断言在本地恒真，包括改前和 M9 变异下**（round-2 P2-3 实测：基线、改后、M9 三者
`<symdir>/<loop>`、`<dir>/../<loop>`、裸 `<realdir>/loop` 全部 `EQUAL=True`）——
因为 3.13+ 上 `Path.resolve(strict=False)` **就是**非 strict realpath，两臂在任何变体下都同值。
故**不得把 3.9 记作本地 oracle**：它的判别力只在 **CI 3.11 臂**（那里 M9 会让 db-free 臂
撞 `RuntimeError` 而返回未解析值，db-backed 臂返回折叠形，两者不等）。
M9 在本地的证死由 **4.4 的违规者守卫独力承担**，6.6 已据此改写。
≤3.12 那一半由 D9 的链结清（见下）。

## D9 — ≤3.12 侧的**论证性**结清链（覆盖 #1400 AC-3 **与 D8/B4**）

本地解释器是 3.14.2，**跑不到** ≤3.12 那条 `RuntimeError` 逃逸臂，也观测不到 B4 的规范形差异
（D8）。故这两处均由三步构造性论证结清，而非由不可执行的测试结清
（fixture review P1-1：初稿把本链只 scope 到 AC-3，漏了 B4）：

1. **AST 层**：lane meta-guard（tasks 4.3/4.4）断言点名函数内**零** `.resolve()` 调用 →
   产生 `RuntimeError` 的唯一形态已从代码里消失（`Path.resolve` 是该异常的唯一来源）。
   对 B4 同理：db-free 臂一旦不再有 `.resolve()`，两臂就只能走同一个 realpath 原语，
   「同一规范形」由构造保证而非由观测保证。
2. **原语层**：`os.path.realpath(..., strict=True)` 在 3.11/3.12/3.14 上一致抛
   `OSError`（带 errno），见上表与 #1427 证据 3、#1400 证据 3 的三解释器矩阵。
3. **CI 层**：合并后 master run 在 **CI 3.11** 上执行同一批用例，另一条臂免费取得执行证据。

**此链必须写进 tasks 与 PR body**，否则评审会要求一条本地不可能跑的测试。

## 变异清单（M1-M12）

每条**按精确源文本匹配**施加、断言 `count == 1`；**严禁按行号定位**（坐标会漂）。

| # | 站点 | 变异 | 指名 oracle |
|---|---|---|---|
| M1 | A1 | 删掉 ENOENT 臂的二次 strict 复查 | tasks 3.1 A1 phantom-环路用例 |
| M2 | A1 | 二次复查的 errno 判据 `!= ENOENT` 改成恒真 | **tasks 3.3b 纯未创建根用例**（见下）|
| M3 | B1 | `realpath(…, strict=True)` 改回 `resolve(strict=False)` | tasks 3.5 B1 环路 path 用例 |
| M4 | B1 | 删掉 ENOENT 非 strict 回退（直接拒） | tasks 3.4 B1 `never-created` admitted 用例 |
| M5 | B1 | 回退臂去掉 loop-filtered 复查 | tasks 3.6 B1 phantom-环路 path 用例 |
| M6 | A1/B1/B2 | `except` 元组去掉 `ValueError` | tasks 3.7 NUL 用例（三站点各一） |
| M7 | B2 | 环路出口 reason 改回 `not_found` 类 | tasks 3.8 B2 `(code, reason)` 二元组用例 |
| M8 | 守卫 | 从守卫里摘掉任一函数 | **tasks 4.4 违规者式守卫**——反转后无元组可摘，结构性死亡（见下）|
| M9 | B4 | db-free 臂改回 `path.resolve(strict=False)` | **仅** tasks 4.4 守卫（本地）；3.9 在 CI 3.11 臂 |
| M10 | B3 | 改回 `path.resolve(strict=False)` + `try/except` | **仅** tasks 4.4 守卫（已登记弱证死点）|
| M11 | B2 | 删掉 ENOENT 非 strict 回退（直接拒） | tasks 3.4b B2 未创建路径无回归用例 |
| M12 | B2 | 回退臂去掉 loop-filtered 复查 | tasks 3.8b 间接 symlink phantom 用例（**唯一**能走到该臂的几何）|

**两条被 fixture review 证伪的初稿 oracle，及其更正**：

- **M2 的初稿 oracle（良性 phantom）是瞎的**：`<missing>/../<realdir>` 走的是二次复查
  **成功**的路径，那个 errno 谓词根本不被求值，变异不可见。实测：把谓词强制恒真后
  `-k "allowed_root or db_free"` 仍 **317 passed**（含被点名的 `tests:40499`）。
  真正的杀手是**纯未创建根**（第二次也是 ENOENT 的那条臂）——同一实测下
  `tests/test_retry.py` + journal **7 failed**。故新增 tasks 3.3b 承担该 oracle。
- **M8 的初稿 oracle 是重言式**：`lane` 由**同一个元组**过滤构建
  （`tests:15984-15987`、`:17655-17661`），删元组项时两边同步收缩，`sorted(lane) ==
  sorted(names)` 恒真。实测（在既有 artifact 腿上）：FULL=True，MUTATED=True。
  **精确口径**：该断言并非全然无用——它能抓「模块侧函数被删/改名而元组仍列它」；
  瞎的只是**元组侧掉名**这一向，也就是「把函数从守卫里摘掉以绕过 `.resolve()` 禁令」。
  （既有 artifact 腿守卫同形失明——该守卫源自 **issue #1402 / PR #1422**（`015318d2` 立
  `_ARTIFACT_GUARD_LANE_FUNCTIONS`），后经 **#1424 / PR #1435**（`72b3892e`）与 **#1618**
  （`8f386972` 加 `_local_artifact_target_is_not_a_file`）先后触及——**但只有 #1618 那次
  真正改了成员**（实测成员数 `015318d2` 6 / `72b3892e` 6 / `8f386972` 7；`72b3892e` 加的是
  断言而非成员。round-2 两个透镜独立发现：round-1 那次出处更正自己又写错了一处）；
  失明是其**自诞生起**
  的既有缺口，非 #1618 引入。**只报不修**，随 5.2 路由。）
- **round-1 给的补救（按命名谓词反查）经 round-2 实测不可写**：4.4 原定的那四个
  scheduler_config 函数**既无共同前缀也无共同后缀**——`_db_free_path*` 多圈一个
  `_db_free_path_evidence_scalar`，`*_for_mode` 多圈八个，两者并集仍不等于那四个。
  故守卫改为**断言违规者而非成员**（tasks 4.4）：枚举模块内所有仍调用 `.resolve()` 的函数，
  与一份显式 allowlist（`{"_safe_preserve_final_component"}`）比对。
  反转后**没有元组可供摘名**，M8 由构造死亡，而非靠一条更聪明的断言死亡。

**M9/M10 系 fixture review P3-3 补齐**：初稿 M1-M8 漏了 B3 与 B4 两个 in-scope 站点，
而 tasks 6.6 写的是「逐站点单删」。M10 的 oracle 只有 AST 守卫一条——B3 的行为在 3.14 上
改前改后**十种形状全等**（fixture review Q5 实测），无行为 oracle 可用，此为已知且已登记的
弱证死点，非疏漏。

## 坐标勘误

无。本文件坐标量于 `origin/master`（2026-08-20），与 issue 原文坐标的差异已在 proposal
表格中按符号名重新锚定（#1400 引 `retry.py:1565-1571`，实为 `:1665-1688`；
#1427 引 `:1568-1578`，实为 `:1649-1659`）。
