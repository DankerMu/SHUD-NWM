# Design — 路径展开/解析抛型面家族收口

## D-1. 术语：「臂」有三种，本文档一律写全称

初稿把「两臂」同时用于三件不同的事，fixture 审判定这**正是 P0-3 的直接成因**。此后一律写全称：

| 术语 | 指 |
|---|---|
| **解释器臂** | CPython 3.11/3.12 vs 3.13+ |
| **database 臂** | `db_free_required=True` vs `False` |
| **代码臂** | 同一函数内的 `S_ISLNK` 分支 vs `except OSError` 分支 |

**跨解释器臂一致**是本单的目标，五处皆然。跨 database 臂要分成两件事，**不可一概而论**：

- **跨 database 臂的「构造产物」一致**（#1549，B.2(b)/B.2(d)）—— 是本单目标，且**可实现**
  （fixture 审已模拟三处修法与今天 db-free 臂产物逐字相等）。
- **跨 database 臂的「拒绝判定」一致**（#1544 那条 lane）—— 在本单 allowlist 下**不可实现**：
  db-free 包裹层 `scheduler_config.py:1020-1024` 一揽子吞掉**全部**拒绝，而该文件 A.7 钉死零改动。

（初稿这里写成一句无条件的「跨 database 臂一致不可实现」，与 D0.2 / B.2(b) / spec 的 parity 锚点冲突；
fixture 审 round-1 P2-A 更正。）

## D0. 四种语义，禁止统一

五个站点长得像（都是「裸展开/解析 + 抛型不匹配」），**目标语义各不相同**。
把一种修法复制五遍会同时打断三处 userspace。逐条口径：

### D0.1 #1547 `safe_fs._expand_path` —— 抛

```python
def _expand_path(path: Path) -> Path:
    expanded = Path(path).expanduser()          # <-- 唯一要包的一行
    return expanded if expanded.is_absolute() else Path.cwd() / expanded
```

`safe_fs` 对外的契约就是「所有失败以 `SafeFilesystemError` + `kind` 出面」。这条前奏是
**全部 16 个 public 入口**的共享路径，它漏抛裸 `RuntimeError` 就是整条契约在这一输入形态上失效。

**修法**：只把 `expanduser()` 那一行包进 `try/except RuntimeError`，抛
`SafeFilesystemError(..., kind="unsafe")`；**cwd 锚定那一行逐字不动**。

**为什么不能照抄家族的 verbatim-keep 原语**（`Path(os.path.expanduser(...))` 原样保留 tilde 段）：
本站点的下游是**写侧原语**。原样保留的 `~nonexistent_user` 段被 cwd 锚定后会进入
`ensure_directory_no_follow` 的逐段 mkdir 循环，**真的在 cwd 下建出一个字面名为 `~nonexistent_user` 的目录**；
`rmtree_no_follow` / `unlink_no_follow` 则会去动一条与用户意图完全不同的路径。
即「静默走既有臂」在这里不是无害降级，而是**错误的文件系统副作用**。这与 #1441 同形，故走收窄抛型。

**为什么 `except RuntimeError` 是安全的**：这一行只调 `Path(path).expanduser()`，
不可能抛 `SafeFilesystemError`；不存在「把自己的结构化错误重新包一层」的风险。
方向性提醒仍要写进 docstring：`SafeFilesystemError ⊂ RuntimeError`，反向不成立。

**`kind` 选 `unsafe` 不新增**：新增 `kind` 会让所有 `error.kind == "io"` 的二分判据出现未覆盖分支。

### D0.2 #1549 三处 config-construction 裸 expanduser —— **不抛**

这三处的正确出口与 D0.1 **相反**。理由在代码自己的注释里：
`_optional_config_path` 的 `:596-613` 明写「classification belongs to the storage preflight,
not to config construction」。对 `~` 输入，这条设计意图恰恰被 `expanduser()` 排在 `try` 之外抵消了 ——
分类还没轮到 preflight，**构造期就崩了**，一条结构化 blocker 都产不出来。

**修法**：把 expanduser 纳入既有 try 的语义边界，失败时走与 **db-free 臂逐字相同**的产物
（`_expanduser_for_mode(..., db_free_required=True)` 已定稿的「吞下、原样 cwd/base 锚定」），
让构造**成功**，把分类留给 preflight。

**验收锚点是 parity 断言，不是「不抛」**：同一输入下 db-backed 臂与 db-free 臂的产物必须逐字相等。
只断言「没抛」会对「抛型改了但产物错了」恒绿。

三处（**按函数名锚定**）：
- `_optional_config_path` —— **活**，db-backed `allowed_storage_roots` 实测可达。
- `_config_path_relative_to_preserve_final` —— **活**，db-backed `log_root` 实测可达。
- `_config_path_preserve_final_component` —— **非活**（初稿说它「db-backed 臂活跃可达」是错的，
  fixture 审 P0-2 推翻）：`scheduler_config.py:269` 的 `_raw_config_path_preserve_components`
  排在 `:273` 之前，任何 tilde 输入都先在 `_expanduser_for_mode` re-raise，走不到这里。
  仍修，但定性是**兼容面 / 家族账目**项（同 #1546 那一对），验收也只能按**直接调该函数**来写，
  不能写成「`workspace_root` 构造成功」——那条在本单 allowlist 下不可实现。

### D0.3 #1544 S_ISLNK 臂裸 `resolve(strict=False)` —— strict-realpath 范式

```python
if stat.S_ISLNK(path_stat.st_mode):
    resolved = path.resolve(strict=False)       # <-- 本单
    _scheduler._require_under_workspace(resolved, workspace_root, field_name)
    if resolved.exists() and not resolved.is_dir():
        raise ValueError(f"production scheduler {field_name} must be a directory")
    return
```

几何是 **workspace 内部**的末段环（父段健康、containment 已过），是 #1520 矩阵的**补集** ——
#1520 的用例把环造在 workspace 之外，父段 containment（master `:734`）先拒，这一臂永不执行。

**修法**：`os.path.realpath(strict=True)` 优先。
- **`errno.ELOOP` → 结构化 `ValueError` 拒绝**，两个**解释器臂**同类型同文案。
  **必须写 `errno.ELOOP`，不得硬编码数字** —— 它在 **Darwin 是 62、Linux 是 40**。
  照抄 62 会做出一条本机绿、在 CI 与 node-27 上**永远走不到**的死分支
  （本模块 `:411` 已 import 了 `ELOOP`，照用）。
- **ENOENT → 落非 strict `os.path.realpath` 兜底**，继续走 containment 复查与 `is_dir()` 裁决。
- **其余 real-path 失败保持既有拒绝** —— 同一 `except OSError` 代码臂上还挂着两条**非环**几何的
  既有精确等值 pin（见 D2 护栏），它们必须逐字保持绿，所以判据必须是 `errno == errno.ELOOP` 的**分流**，
  不是把整个 `except OSError` 臂换掉。

**这条 ENOENT 兜底是 load-bearing 的**：strict realpath 对**悬空 symlink**（指向不存在目标）
抛 ENOENT，而今天的代码对悬空 symlink 是**放行**的（`resolved.exists()` 为 False ⇒ 不报
`must be a directory` ⇒ return）。天真地换成 strict-only 会把悬空 symlink 从「放行」变成「拒绝」——
**静默打断 userspace**，且既有用例未必逮得到。

**六条既有语义必须逐条锁成 spec scenario**（回归栅栏，对应 B.3(b)–B.3(g)）：
健康目录 symlink → 通过；symlink 指向文件 → `must be a directory`；
symlink 逃出 workspace → `must be under workspace_root`；末段不存在（`FileNotFoundError`）→ 直接 return；
**悬空 symlink 且目标在 workspace 内 → 放行**（今天的行为，上面那条 ENOENT 兜底存在的理由）；
**悬空 symlink 且目标在 workspace 外 → 仍报 `must be under workspace_root`**
（`:743` 的 containment 复查先于 `exists()` 闸触发 —— 少了这条限定，头号判别器 B.6(c) 会因为别的原因红）。

**判定步数一致只能按解释器臂表述（初稿写错了，fixture 审 P0-3 推翻）**：
初稿说「≤3.12 靠包裹层吞掉 `RuntimeError` 而跳过后续两步，3.13+ 跑完，修完两臂要一致」——
这把**吞异常的条件**说反了。`scheduler_config.py:1020-1024` 的
`except (OSError, RuntimeError, ValueError): if not db_free_required: raise`
是**按 database 臂**分流、**与解释器无关**：db-free 臂今天就把**全部**拒绝吞掉。
fixture 审的 8 几何 × 2 database 臂实测：symlink 指向文件 / 逃出 workspace / 悬空且指向 workspace 外
三种几何上，db-backed 报 `ValueError`、**db-free 一律 ACCEPTED**。
⇒ 修完之后 db-free 臂**依然**会吞掉新的环路拒绝，**跨 database 臂一致在本单 allowlist 下不可实现**。
故：一致性目标一律按**解释器臂**表述（3.11/3.12 vs 3.13+，都在 db-backed 臂上，或直接测被守卫函数本身）；
db-free 包裹层的一揽子吞异常是独立缺陷，proposal 已列为 Non-Goal。

### D0.4 #1546 那一对 —— 收敛为规范化返回

```python
def _resolve_optional_config_path(value: Path | None) -> Path | None:
    if value is None: return None
    return value.resolve()                      # <-- 本单

def _optional_config_path_relative_to(value: Path | str | None, base: Path) -> Path | None:
    if value in (None, ""): return None
    path = Path(value).expanduser()             # <-- 顺带：这一行也是 D0.2 那一族
    if not path.is_absolute(): path = base / path
    return path.resolve()                       # <-- 本单
```

**修法**：换成同文件 `_canonical_parent` / `_optional_config_path` 已经定型的
「`os.path.realpath(strict=True)` + 单次非 strict `os.path.realpath` 兜底」范式，
两个解释器同一规范化 `Path` 产物。

**注意 `_optional_config_path_relative_to` 是跨 lane 站点**：它同时有 D0.2 的裸 expanduser
**和** D0.4 的裸 resolve，两条都要治，且治法不同（expanduser 走 D0.2 的不抛口径）。
这是本单唯一一处两族交叉的函数，实现时最容易只改一半。

**既有语义零回归**：`None` / `""` 仍早退返回 `None`；相对路径仍按 `base` 拼接；
ENOENT（路径不存在）仍返回非 strict 规范化结果而**不抛**。

### D0.5 #1545 —— 只改消息内容

类型仍 `ValueError`；`NHMS_SCHEDULER_LOCK_ROOT` 环路那条
（`production scheduler lock_path must be under workspace_root`）**逐字不变**；
非环输入的全部拒绝文案不变。

改的是：`WORKSPACE_ROOT` 为环时，现在报
`production scheduler evidence_dir must be a safe directory` —— 字段名指向用户**没配**的旋钮
（`evidence_dir` 是从 `workspace_root` 派生的默认值 `<workspace>/scheduler/evidence`，
`NHMS_SCHEDULER_EVIDENCE_ROOT` 在该几何下根本没被设置），消息**不含路径也不含 env var**，
运维排查方向直接被引偏。成因是 `workspace_root` 自身没有任何 loop/safety 校验，
环只能被派生字段的守卫顺带发现。

## D1. #1544 与 #1545 共用一套消息格式（先设计，后实现）

#1545 的加料消息（含出错路径 + 指明 `workspace_root` 派生关系；
**路径呈现口径按本守卫的兄弟拒绝来，不是 `_scheduler_root_blocker`** —— 初稿写「脱敏口径与
`_scheduler_root_blocker` 一致」是错的，fixture 审 P1-3 推翻：那个函数**根本不做脱敏**，
`[local-path]` 是调用侧按 `evidence_safe_paths` 标志决定的，而
`_require_safe_directory_final_component(path, workspace_root, field_name)` 既没有该标志也拿不到 config
句柄，复现不了那套口径；真去脱成 `[local-path]` 还会让「消息含路径」这条验收自相矛盾。
故：直接带 `str(path)`，与本函数 `:740/:745/:748` 的兄弟拒绝一致，它们都不脱敏）
与 #1544 新增的 ELOOP 拒绝，**落在同一个函数的相邻两个代码臂**。

**初稿的理由是非因果的（fixture 审 P2-2），已更正。** 两条要求都只约束**解释器臂**之间一致，
**没有**任何一条要求两个代码臂文案逐字相同。而且强行统一是**有害的**：S_ISLNK 代码臂上的环
确实就在运维自己配的 `NHMS_SCHEDULER_EVIDENCE_ROOT` 里，把它的消息也指向 `workspace_root`
就是**反方向重新制造归因错误**。

降级为**推荐**：抽一个共用的消息构造 helper（同一句式、同一路径呈现），
但**字段归因按各自代码臂各报各的** —— S_ISLNK 臂报运维实际配的那个字段，
`except OSError` 臂的 workspace-root 环报 `workspace_root` 的派生关系。

## D2. #1545 是**经授权的 oracle 改写**（评审前置声明）

`tests/test_production_scheduler.py` 的
`test_resolve_residue_config_final_segment_loop_converges_to_structured_refusal` 里，
PR #1541 用 parametrize **精确等值**把错误的字段归因钉成了全版本期望：

- 旧期望：`"production scheduler evidence_dir must be a safe directory"`
- 新期望：本单 D1 定稿的格式（含路径 + 可定位到 `workspace_root`）

**授权来源是 #1545 本身**，它正是 `runtime-roots-resolve-residue` 的 Non-Goal 2
两次书面承诺「另行立单承接」的落点。同时要求：该 parametrize 上方注释里
「declared non-goal, pinned rather than corrected」的措辞**随之删除或改写** ——
否则注释会与新断言自相矛盾。

**不改断言的另一半**：`NHMS_SCHEDULER_LOCK_ROOT` 那一行逐字保留。

**护栏（fixture 审补充，实现必须照做）**：同一条旧文案
`"production scheduler evidence_dir must be a safe directory"` 在
`tests/test_production_scheduler.py:30460` 与 `:30591` 还有**另外两处 pin**，
那两处是**非环**几何（`workspace_root` 是普通文件 / mode 0600 不可遍历），
它们经的是**同一个** `except OSError` 代码臂，且必须**逐字保持绿**。
⇒ 实现只能在该臂内按 `errno == errno.ELOOP` **分流**出新文案，不得整臂改写。
这同时也是 A.3(b) 那条「其余 real-path 失败保持既有拒绝」的落点。

这条**必须在 PR body 显式预声明**，否则会撞上「不得削弱 oracle」的合并硬门，
也会被评审当成偷改测试。

## D3. `runtime-roots-resolve-residue` 的 Non-Goal 2 回填口径

该 change 已归档（`openspec/changes/archive/2026-08-18-runtime-roots-resolve-residue/`）。
**不重写归档**。承接关系记在：(a) 本 change 的 proposal/tasks，(b) 关闭 #1545 时的 issue 评论。
同理 #1549 要求的「PR #1548 `proposal.md:25-33` 可达性口径改引本单编号」——
#1548 也已归档，同样落在本 change 的 proposal 更正段 + issue 关闭评论，不改归档文件。
**这条在 tasks.md 里定死，免得评审期重新扯一遍。**

## D4. `tests/test_safe_fs.py` 已存在 —— #1547 的两条前提是 stale

#1547 正文写「`ls tests | grep safe_fs` 为空」，并据此要求新建
`tests/test_safe_fs_expanduser.py` + 核查 CI selector 路由。**该前提为假**：
`tests/test_safe_fs.py` 存在（本仓多次以 `150 passed` 三件套跑过）。

故：#1547 的新测试**放进既有 `tests/test_safe_fs.py`**，CI selector 路由那条验收项随之**自动满足**
（同名 suite 已被 selector 覆盖），在 tasks 里记为「前提更正后不适用」，不是跳过。

**约束**：新测试全部 `tmp_path` 隔离、不留进程级状态 —— `tests/test_safe_fs.py`
是 #1613 顺序依赖的触发文件，往里加共享状态会加重那条尚未定位的三体效应。

## D5. Spec 归属

- scheduler 四单 → `slurm-array-runner-integration`，沿用 #1520/PR #1541 的先例（同文件、同家族）。
- #1547 的 safe_fs 共享底座契约 → **新建 capability `safe-filesystem-primitive-contract`**。
  初稿放 `data-integrity-storage-contract`、并标注「judgment call，请评审挑战」；
  fixture 审挑战成立（P2-4），已改。理由：那个 capability 只有三条 requirement
  （best-available 选择血缘 / 预报时效源校验 / S3 URI 前缀隔离），全是数据库与对象存储的行记录语义，
  **零文件系统原语内容**，且其 `## Purpose` 至今是字面 `TBD - created by archiving change …`。
  把共享文件系统原语的抛型契约塞进去，只会让一个本已不连贯的 capability 更不连贯。
  `prearm-error-model` 在种类上更近（它正文就在讨论 `SafeFilesystemError` 不是 `OSError`），
  但它是 pre-arm nonmove 那条 lane 的错误模型，仍是范畴错误。
  故另立一个窄 capability，只装「共享安全文件系统原语的对外失败契约」这一件事。

## D6. 证据面：版本臂的跑法

一个独立 3.11 venv，建一次、复用：

```
UV_PROJECT_ENVIRONMENT=<scratchpad>/venv311-batchE uv run --python 3.11 python - <<'PY' …
```

**绝不裸跑 `uv run --python 3.11`** —— 会重建项目 `.venv`（三个 issue 正文都点名警告）。
before 矩阵（全部坏形状）与 after 矩阵（收敛）用同一个 venv。

新增测试一律断言**收敛后**的行为，因而与解释器版本无关；它们在 CI 的 3.11 与 node-27 的 3.11.15 上
都会跑，**node-27 receipt 顺带就是 ≤3.12 的实机 oracle**，不需要额外的远端动作。
