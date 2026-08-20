# Design: artifact-probe-file-kind-and-key-framing

> Fixture review round 1（`.workplans/batch-p1/review/fixture-review.md`，REVISE）后修订。
> 该轮 **F1 实证推翻了本文件初稿登记的一条"缺陷"**，见 D3；坐标更正见文末「坐标勘误」。

## 裁定（实现期不得静默改判）

### D1 —— 种类判定放在探针层，`_object_manifest_is_missing` 签名与语义逐字不变

issue #1394 建议「在探针层修，别动共享 store API」。`:1196-1198` 的注释声称
`_object_manifest_is_missing`「since #1393 … has exactly one caller left」——**该断言只对调用点成立，
调用点不是契约**。它同时被 compat shim 再导出（`scheduler_state_compat.py:113`）、被
`scheduler.py:174` / `scheduler_state.py:21` 导入，并在测试里当**可 monkeypatch 的接缝**用
（全仓 monkeypatch 站点恰好 4 处 + 2 处真实 store 断言，评审已复核穷尽）：

| 站点 | 用法 |
|---|---|
| `tests/test_production_scheduler.py:4684`、`:24435` | patch **facade**（`scheduler_module`），靠 compat 传播 |
| `:12140` | patch **`scheduler_state_failure_module` 本身**，"被调用即 fail" |
| `:12187` | patch **`scheduler_state_failure_module` 本身**，抛 `ObjectStoreError` |
| `:25902`、`:25941` | 真实 store 上直接断言 `is True` / `is False` |

故 `(candidate, uri) -> bool` 是**被测试钉死的接缝**，不重构。

**做法：新增 sibling 探针**，四条硬约束：

1. **仅当 `missing is False` 时才咨询**——这让 `:12140`（根门 `:1188` 就已抢先）与 `:12187`
   （在 `ObjectStoreError` 处理臂里就已 return）两处几何**自动成立**，无需改动。
2. **只许"加一个正向裁决"，不许改动任何既有裁决**（round-1 F5 加严）：sibling 只能把
   *确实存在且非常规* 的目标翻成 `artifact_target_not_a_file`；**absent、不可解析、以及 sibling 的
   任何其他结局，都必须让 `(False, None)` 逐字不变**。
   - 初稿只禁了 absent→missing，**不够**：若 sibling 在探针 `:1200-1201` 的 `try` 内调
     `resolve_path`，一个来自 `normalize_key`/`validate_object_path` 的 `ValueError` 会落到
     `:1218`，把 present 悄悄翻成 `(True, None)`——四态规则管不住这条。
   - 故 store 侧用**四态种类分类器**（`"file" | "directory" | "other" | "absent"`，**纯新增方法**，
     `exists` 不动），**不用**布尔 `is_regular_file`（布尔对 absent 返回 False，正是会翻掉
     `:24435` 几何的混淆）；且 sibling 自身的失败**一律回落"不翻"**。
3. **错误容器与 `exists` 逐字一致**：`SafeFilesystemError` → `ObjectStoreError`，
   由探针既有的 `except ObjectStoreError`（`:1202`）接住 → `artifact_probe_error`。
   sibling **不得自建错误 reason 词表**。
4. **两个 helper，不是一个**（round-1 F4/F6）：
   - `_object_store_prefix_for(candidate)`：**store-free 全函数**，用防御式
     `getattr` + `isinstance(Mapping)` + `str()`（对齐 `_object_store_root_configured`
     `:1244-1245` 的姿态，而**不是** `_object_manifest_is_missing` `scheduler_state_common.py:165`
     的裸 `.get`），**永不抛**；分类器与下面那个 helper 共用它。
   - `_local_object_store_for(candidate)`：**构造 store 的工厂**，只给探针腿用，**分类器禁用**（D4）。
   - **两者都放 `services/orchestrator/scheduler_state_common.py`**，紧挨 `_object_manifest_is_missing`：
     `scheduler_state_failure.py:19` 是 `from scheduler_state_common import ...` 的方向，
     反向调用会成环。`_object_manifest_is_missing` 变薄壳委托工厂（签名/可 patch 性不变；
     测试 patch 的是名字，不是函数体）。

**compat 传播实测**（`scheduler_state_compat.py:184-219`；`scheduler.py:231` 安装）：facade 上的
私有函数被 `compat_wrapper` 包了一层，调用时进 `compat_bindings()`，把 facade 上被 monkeypatch
的名字**传播进所有 `services.orchestrator.scheduler_state*` 模块**——所以 `:4684`/`:24435` 打在
`scheduler_module` 上的 patch **确实生效**，`:4680` 的注释准确。

实测方法与结果（`uv run python`，patch facade 属性后经任一 compat-wrapped facade 函数入场，
在 wrapper 执行期间回看 `scheduler_state_failure` 的同名全局；评审独立复现同一结论）：

```text
ssf sees patch BEFORE facade call : False
ssf sees patch DURING facade call : True
ssf sees patch AFTER  facade call : False (restored)
```

**由此得出一条实现期硬约束**：传播的作用域**仅限 facade 调用期间**，调用结束即还原。
本 change 的新测试都是**直调** `scheduler_state_failure_module._artifact_uri_missing_status`，
因此**必须直接 patch `scheduler_state_failure_module._object_manifest_is_missing`**
（如既有 `:12140`/`:12187` 那样），**不得**改为 patch facade——后者拿不到 patch，测试会静默
退化成"探针跑了真实 store"的另一个几何，失去本用例要钉的东西。

sibling **不入** compat re-export 名单（`SCHEDULER_STATE_COMPAT_REEXPORT_NAMES` 里
`_artifact_uri_missing_status` / `_needs_package_manifest_witness` 本来也都不在）：
约束 2 已保证 `:24435` 几何（patch 成 present + 物理不存在）走"absent → 不翻"，无涟漪。

### D2 —— 目录取**新** reason `artifact_target_not_a_file`，且是**非空**（=修复渠道拒绝）

**非空**：对一个被目录占位的 file key，rebuild **清不掉**它（往该路径写文件会
`IsADirectoryError`），所以 `scheduler_candidates.py:1617-1621` 的拒绝是**正确路由**，
与 `artifact_probe_error` 同一教义。该处按 `unsafe_reason not in (None, "")` 判断，**与 token 无关**，
故新增 token 不改变任何既有路由（评审已复核）。

**为何是新 token 而不是复用 `artifact_probe_error`**（round-1 F13 修正了初稿的论证）：
现行 spec `openspec/specs/job-retry-mechanism/spec.md:1181-1183` 那句的**规范性动词是"异常收容"**
——「SHALL be contained fail-closed and SHALL never escape … as an exception」。而
`stat_no_follow`（`packages/common/safe_fs.py:270-288`）**只拒 `S_ISLNK`**（`:277-278`），
目录/FIFO/socket/设备**从不抛**。所以那句括号里的「non-regular」**自始就是对实现的描述性误述**，
对一个什么都不抛的目标**没有任何规范性内容**——本次是**更正**它，不是推翻一条既有裁定。

（初稿在这里把**场景**的三例举证当成了**那句话本身**的列举，评审 F13 指出是误引；结论不变，前提改诚实。）

新 token 的成本≈0：`unsafe_reason` 在 `schemas/`/`apps/`/`packages/`/`openapi/`/`db/`/`workers/`
中**无任何枚举或 schema 约束**（评审 grep 复核，仅 `scripts/governance/audit_repo_entropy.py:2526`
有一个无关同名字符串）；`safe_fs.py:418` 本就有把目录单独措辞的先例。
`artifact_target_not_a_file` 是 **issue #1394 自己提的 token**，非本 fixture 发明。

### D3 —— symlink 口径**不对齐**；初稿登记的那条"缺陷"经实证**不存在**，已撤

两条腿在**目录**上对齐（同一 reason）。symlink-to-file 上仍分歧，**本 change 不对齐**。

**初稿写的理由是错的，已撤回**（round-1 F1）：初稿称
`_local_artifact_path_is_allowed` 量的是"链接自身路径"、`path.exists()` 量的是"链接目标"，
两者错位。实证否定：`_local_artifact_path_is_allowed`（`:1322`）调 `_realpath_or_none`，
后者是 `os.path.realpath(expanded, strict=True)`（`:1303-1305`），**连最后一段一起解析**，
收容因此量在**完全解析后的目标**上——与 `Path.exists()` 回答的是同一个对象。评审另造了
"允许根内的 symlink 指向根外"实测，得到
`(True, 'local_artifact_path_outside_allowed_roots')`，**逃逸被拒**，不存在错位。
→ **删除该缺陷登记，不开 issue**（初稿的 task 4.2 已删）。

**真正的、且成立的分歧**是另一件事：object 腿的 `stat_no_follow` 把**任何 symlink 都当收容问题
硬拒**（`safe_fs.py:277-278`）→ `artifact_probe_error`；local 腿的收容是**在解析后的目标上**算的，
因而**接受**一个收容之内的 symlink-to-file。两侧对 symlink 的处置源于**不同机制**（安全收容 vs
解析后收容），不是种类判定的不一致。#1394 只问目录，把 local 腿改成拒 symlink 会让**当下可读的**
产物开始被拒——本 issue 之外的 fail-close 风险，故不动。
本 change 内：一条测试钉住 symlink-to-file 在 local 腿仍为 present，注释引本 D3（**无 issue 号**）。

### D4 —— 分类器**绝不构造 `LocalObjectStore`**（issue 首选方案是陷阱）

#1397 正文首列的方案「复用 `LocalObjectStore.normalize_key`」会踩坑：
`LocalObjectStore.__post_init__` 跑 `ensure_directory_no_follow(root)`（`object_store.py:63`）
——一次**真实文件系统触碰**，抛的是 `ObjectStoreError`（`RuntimeError` 子类，`:57-65`），
分类器的 `except ValueError` **接不住**；而分类器自己的 docstring（`:1069-1079`）明写它跑在
**探针容器之外**，一次逃逸会中止整个 scheduler pass 上所有剩余 candidate。那正是 #1365 round-1
那条故障族，被"修复"本身重新引入。

**取第二方案**：把纯字符串归一化（prefix 剥离 + `s3://` 支的 unquote）抽成
`object_store.py` 的**模块级纯函数**，`normalize_key`（`:183-196`）**委托**给它，分类器调纯函数。

**分类器允许的异常面只有一个**：来自纯归一化函数的 `ValueError`，折进既有
`except ValueError: return False`（`:1087-1088`）。prefix 读取走 D1.4 的 store-free 全函数
`_object_store_prefix_for`，**不得**照抄 `scheduler_state_common.py:165` 的裸
`candidate.resource_profile.get(...)`——那会在容器外抛 `AttributeError`/`TypeError`，
既不是 `ValueError`，`:1087-1088` 也折不住（round-1 F6）。

**桶不匹配的残留**（`s3://other-bucket/...`）：纯归一化抛 → 分类器 False → 探针自己的
`ValueError` 腿（`:1218-1223`）→ 路由与今日一致。一行钉住即可。

### D5 —— 签名

`_needs_package_manifest_witness(candidate, value)`。生产调用点 1 处（`:713`），
测试直调 2 处（`tests/test_production_scheduler.py:12278`、`:12425`）须穿参。
该名**不在** compat re-export 名单，无 compat 面影响。

### D6 —— sidecar 层：新 token **故意**落到 `:684` 的既有 blocker，不进 `:650` 特判（round-1 F3）

`:650` 是对 `_ARTIFACT_PROBE_ERROR_REASON` 的**精确相等**判断，新 token 会掉进 `:684` 的
`if sidecar_missing:`。这是**正确落点，但必须是裁定而非意外**：

- `:650` 那条特判的依据是 **#1203 round-2 V5-C2**：「an unreadable probe object is *cannot
  determine*, NOT *package determined absent*」——它管辖的是**读不出**的故障，故落
  `forcing_version_row_absent` 并**保持 repair-eligible**（`:664`），靠 `tier_status` 提示
  "rebuild 无效"。
- 目录**不是"读不出"，而是一次确定的判定**：我们确知目标不是文件。故它不属 `:650` 的管辖前提。
- 落 `:684` 的后果经复核是对的：该分支**会把 `unsafe_reason` 带进 blocker**（`:695`），
  于是 `scheduler_candidates.py:1617-1621` 按非空 reason **拒绝修复**——正是 D2 要的路由，
  且与 journal/direct 两层的形状**完全一致**。

→ 不改 `:650`，不加 `tier_status`；用一条测试把 sidecar 层的这个终局钉死，spec 补一句。

**这条同时补上 #1394 的 AC-2**（"forcing 腿：派生见证 key 落在目录上时不再返回'存在'"）：
初稿 §3 只有 object key（3.1）与 local（3.2），**AC-2 无测试**——实测
`_artifact_uri_missing_status(cand, "forcing/…/model_a/forcing_package.json")` 在该 key 是目录时
今日返回 `(False, None)`，即该层今天**根本不发 blocker**。AC-2 不是可选范围。

## 抢先分析（fixture 期写死，不留给 round-1 发现）

Batch J 的教训：一条负向腿能否证死自己那道守卫，取决于**上游是否有别的门抢先**。
下表由评审**从源码逐门走出**（非从初稿复制），初稿只列了其中两门。

### object 腿（`:1183-1223`，执行序）

| # | 门 | 站点 | 抢先后的结局 | 初稿列了吗 |
|---|---|---|---|---|
| O1 | `strip()` 为空 | `:1184-1186` | `(True, None)` | 否（无害） |
| O2 | 分支选择器 | `:1187` | 落到 local 腿 | 否（无害） |
| O3 | `_object_store_root_configured` | `:1188`（实现 `:1236-1246`） | `(True,"object_store_root_unconfigured")` | **是** |
| O4 | `LocalObjectStore.__post_init__` 触碰文件系统 | `object_store.py:47-66` → `:1202` | `(True,"artifact_probe_error")` | **否** |
| O5 | `normalize_key` 抛 `ValueError`（空 key、`..`、桶不匹配、越前缀） | `object_store.py:183-196`、`:204-223` → `:1218` | `(True, None)` | 部分（仅作 D4 注） |
| O6 | `validate_object_path` 段数不足 | `storage.py:114` → `:1218` | `(True, None)` | **是** |
| O7 | `relative_to(root)` 逃逸 | `object_store.py:177-180` | `(True, None)` | 否（O5 之后不可达） |
| O8 | key 链上**祖先目录缺失** → `FileNotFoundError` | `safe_fs.py:646-649` → `exists` | `(True, None)` | **否** |
| O9 | 祖先是非目录/symlink → `SafeFilesystemError` | `safe_fs.py:650-653` → `:1202` | `(True,"artifact_probe_error")` | **否** |
| O10 | 叶子是 symlink | `safe_fs.py:277-278` → `:1202` | `(True,"artifact_probe_error")` | 隐含（D3） |
| O11 | **新** 种类判定 | 待加 | `(True,"artifact_target_not_a_file")` | 目标 |

- **O8 是测试作者最先踩的坑**：目录必须**连同整条父链**建（`mkdir -p`），否则结局是 `(True, None)`。
- **O4/O9 可复现，非理论**：评审第一次探针用了裸 `tempfile.mkdtemp()`，其 `/var -> /private/var`
  分量使**每一个** object 腿用例都返回 `(True,'artifact_probe_error')`。pytest `tmp_path` 是
  realpath 安全的，但**任何手搭的 root 必须先 realpath**。（macOS 特有的测试作者陷阱，非生产问题。）

### local 腿（`:1224-1233`）

| # | 门 | 站点 | 抢先后的结局 | 初稿列了吗 |
|---|---|---|---|---|
| L1 | 分支选择器（`file://`/`/`/`~`） | `:1249-1250` | 走 object 腿 | 否 |
| L2 | `_local_artifact_path` 返回 None | `:1225-1227`、`:1275-1278` | `(True,"invalid_local_artifact_path")` | 否 |
| L3 | `_realpath_or_none` 非 ENOENT errno | `:1322`、`:1283-1309` | `local_artifact_path_unresolvable` | 否 |
| L4 | **任一**配置根不可解析（根故障优先） | `:1332-1338`、`:1341-1381` | `local_artifact_root_unresolvable` | 否 |
| L5 | 根集为空 **或** path 在根外 | `:1324-1339` | `local_artifact_path_outside_allowed_roots` | **是**（初稿只列了这一门） |
| L6 | `path.exists()` 为 False | `:1231` | `(True, None)` | 否 |
| L7 | **新** 种类判定 | 待加 | `(True,"artifact_target_not_a_file")` | 目标 |

L5 的根集不是任意的：resource-profile 的 `object_store_root`、`object_store_copyback_root`、
`copyback_root`、`published_artifact_root`，加环境 `OBJECT_STORE_ROOT`、
`NHMS_OBJECT_STORE_COPYBACK_ROOT`、`NHMS_PUBLISHED_ARTIFACT_ROOT`（`:1350-1364`）；
含 `://` 的值被静默跳过（`:1370-1371`）；**不读** `NHMS_SCHEDULER_ALLOWED_ROOTS`。

### 「抢先」的正确表述（round-1 F1 后修正）

初稿说被抢先的腿"钉不住任何东西"。**不确切**：本 fixture 强制断言**整个元组**，而上表每一个抢先门
都产出**互不相同**的元组，所以被抢先的用例在**编写期就会响亮地红**，不是静默空腿。

**本 change 真正的空腿风险在 task 3.3**：它断言 `(False, None)`，因此**即使 sibling 根本没接进探针
也照样绿**。3.3 在同一接缝上**没有正向对照**——必须与 3.1/3.2 成对存在才有判别力。

## 变异配方（逐守卫具名，实现期须逐条实测转红）

| # | 变异 | 应转红的 oracle | 期望形态 |
|---|---|---|---|
| M1 | 删 object 腿的种类判定 | 3.1 object-leg 目录用例 | `(True,"artifact_target_not_a_file")` → `(False,None)` |
| M2 | 删 local 腿的种类判定 | 3.2 local-leg 目录用例 | 同上 |
| M3 | sibling 改成"absent 也翻" | 3.3 seam 契约用例 | `(False,None)` → `(True,…)`；**预期连坐**见下 |
| M4 | 分类器回退到 raw 取景 | 3.4 带路径段 prefix 用例 | 复现伪造 witness 的 `artifact_probe_error` |
| M5 | 扰动纯归一化函数 | 3.8 委托等价性用例 | `normalize_key` 随之变动 |
| M6 | 删 sidecar 腿的种类判定路径 | 3.11 sidecar 用例 | 该层今日的 `(False,None)`「不发 blocker」复现 |

两条配方按 round-1 F8 改写：

- **M3 不是独占的**：`:24435` 的 helper 以 `manifest_missing=False` + 空 store 调用，该变异会让探针
  返回非空 reason，于是两条 raw-manifest 腿一起 abstain（`:1526`/`:1594`），
  **该 helper 家族的既有用例会与 3.3 一同转红**。这是**预期连坐**，Evidence Floor 不对 M3 主张独占。
- **M5 初稿写的是"复制而非委托"，不成立**：逐字复制能通过任何等价性测试；而扰动
  `normalize_key` 那份副本又会连带 `tests/test_object_store_roots.py` 转红（无判别力）。
  正确形态是**扰动纯函数、断言 `normalize_key` 随之变动**——这才是 3.8 能证的东西。
  **委托 vs 复制是代码评审属性，不是测试属性**；tasks.md 1.1 的相应措辞已改。

## 风险三元组

```text
Fixture level: expanded
Upstream suggested level: absent（两 issue 早于该字段；本次按触发器自评）
Project profile: Generic (openspec/project-profile.md)
Change surface:
- services/orchestrator/scheduler_state_failure.py（探针 :1183-1233、分类器 :1047-1088、sidecar 腿 :644-700）
- services/orchestrator/scheduler_state_common.py（两个共享 helper + _object_manifest_is_missing 薄壳化）
- packages/common/object_store.py（新增四态种类分类器方法 + 抽纯归一化函数 + normalize_key 委托）

Must preserve:
- LocalObjectStore.exists 语义逐字不变（20+ 构造点）
- normalize_key 的既有归一化语义逐字不变——特别是 unquote 只发生在 s3:// 支（:204-223），
  裸 key 支（:190-191）不 unquote，不得借本次"归一化对齐"顺手改
- _object_manifest_is_missing 的 (candidate,uri)->bool 签名与可 monkeypatch 性
- 收容阶梯顺序不变：root-unconfigured → 探针 → 新种类判定
- 分类器永不抛（探针容器之外），且其唯一允许异常面是纯归一化函数的 ValueError
- #1365 的目录形状 witness 推导在带路径段 prefix 下仍成立（#1397 的回归 AC）
- 所有 tracked 配置（裸桶前缀、无 percent-encoding）下分类器答案逐字不变
- sidecar 层 :650 的 artifact_probe_error 特判与 #1203 路由不变
- `-k missing_forcing` 用例数只增不减

Must add/change:
- 四态种类分类器（file|directory|other|absent），错误容器与 exists 一致
- 新 unsafe_reason: artifact_target_not_a_file（object / local / sidecar 三处终局）
- 分类器 normalize-first + 签名加 candidate + store-free prefix 读取
- 文档/注释更正：:1064-1067、:1196-1198、:811-816、tests :25519 注释与
  _RAW_MANIFEST_ABSTENTION_REASONS、docs/runbooks/current-production-ops.md:1742-1744 与 :1756-1762

Seams under test:
- _artifact_uri_missing_status(candidate, uri) -> (missing, reason)（探针唯一公共边界）
- _needs_package_manifest_witness(candidate, value) -> bool
- LocalObjectStore.normalize_key / 新纯归一化函数（委托等价性）
- sidecar / raw-manifest 两族消费腿的终局（新 reason 下的 blocker 形状与 abstain）

Selected risk packs:
- File IO / path safety / overwrite: 目录/symlink/收容三者交互；stat 不跟随；新种类判定不得放宽 exists
- Error handling / rollback / partial outputs: 分类器在容器外，任何逃逸都中止整个 pass（D4）
- Public API / CLI / script entry: LocalObjectStore 是 20+ 构造点的共享入口，只许纯新增 + 一处委托
- Legacy compatibility / examples: 裸桶前缀部署下行为必须逐字不变
- Documentation / migration notes: 一张 operator 路由表 + 三处代码注释 + spec 那句

Risk packs considered (core):
- Public API / CLI / script entry: selected - LocalObjectStore 共享入口
- Config / project setup: selected - OBJECT_STORE_PREFIX 的路径段形态正是 #1397 的触发条件
- File IO / path safety / overwrite: selected - 见上
- Schema / columns / units / field names: not selected - 无 schema/列/单位改动；unsafe_reason 无枚举约束（已 grep 复核）
- Auth / permissions / secrets: not selected - 不涉凭据或权限
- Concurrency / shared state / ordering: not selected - 纯读探针，无共享可变状态（stat 与后续读之间的 TOCTOU 本就存在且不由本 change 引入）
- Resource limits / large input / discovery: not selected - 每 candidate 至多多一次 stat
- Legacy compatibility / examples: selected - 见上
- Error handling / rollback / partial outputs: selected - 见上
- Release / packaging / dependency compatibility: not selected - 无依赖变更
- Documentation / migration notes: selected - 见上

Non-goals:
- 不对齐 symlink 口径（D3），不改 _local_artifact_path_is_allowed
- 不动 LocalObjectStore.exists 的既有语义，不动 normalize_key 的裸 key 支
- 不改 scheduler_candidates.py:1617-1621 的拒绝路由，不改 sidecar :650 的 #1203 特判

Review focus:
- sibling 是否真的只在 missing is False 时被咨询，且**任何**失败都不改动既有裁决（D1 约束 2 的加严版）
- 分类器是否真的不构造 store、且唯一异常面是 ValueError（D4 + F6）
- 两个 helper 是否落在 scheduler_state_common.py（否则成环，F4）
- 三条目录腿（object / local / sidecar）是否都绕开了各自的抢先门
- 裸桶前缀下的逐字不变、以及 normalize_key 裸 key 支不 unquote，是否有测试钉住
```

## 坐标勘误（round-1 F10；HEAD `aafb50f9` 实测）

| 初稿写的 | 实际 |
|---|---|
| `object_store.py:67-77`（`exists`） | `:68-78` |
| `object_store.py:157-186`（`normalize_key`） | `resolve_path` `:169-181`；`normalize_key` `:183-196`；`_normalize_s3_uri` `:204-223` |
| `open_regular_file_no_follow` | 函数名是 `open_file_no_follow`（`safe_fs.py:232`）；`:247`/`:257` 行号本身正确 |
| `safe_fs.py:417`（目录措辞先例） | 消息在 `:418`（`:417` 是 `S_ISDIR` 判断） |
| `safe_fs.py:270-284`（`stat_no_follow`） | `:270-288`；symlink-only 拒绝在 `:277-278` |
| `object_store_validation.py:2574-2585`（路径段前缀先例） | `_operational_prefix` 起于 `:2586`（`:2572` 是 percent-decode 轮次） |
