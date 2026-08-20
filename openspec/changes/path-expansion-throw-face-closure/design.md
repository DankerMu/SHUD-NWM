# Design — 路径展开/解析抛型面家族收口

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

三处（**按函数名锚定**）：`_optional_config_path`、`_config_path_relative_to_preserve_final`、
`_config_path_preserve_final_component`。第三处是 #1549 正文漏数的，可达链见 proposal。

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
#1520 的用例把环造在 workspace 之外，`:731` 的 containment 先拒，这一臂永不执行。

**修法**：`os.path.realpath(strict=True)` 优先。
- **ELOOP（errno 62）→ 结构化 `ValueError` 拒绝**，两个解释器同一类型同一文案。
- **ENOENT → 落非 strict `os.path.realpath` 兜底**，继续走 containment 复查与 `is_dir()` 裁决。

**这条 ENOENT 兜底是 load-bearing 的**：strict realpath 对**悬空 symlink**（指向不存在目标）
抛 ENOENT，而今天的代码对悬空 symlink 是**放行**的（`resolved.exists()` 为 False ⇒ 不报
`must be a directory` ⇒ return）。天真地换成 strict-only 会把悬空 symlink 从「放行」变成「拒绝」——
**静默打断 userspace**，且既有用例未必逮得到。

**四条既有语义必须逐条锁成 spec scenario**（回归栅栏）：
健康目录 symlink → 通过；symlink 指向文件 → `must be a directory`；
symlink 逃出 workspace → `must be under workspace_root`；末段不存在（`FileNotFoundError`）→ 直接 return。
**外加第五条**：悬空 symlink → 放行（今天的行为，上面那条兜底存在的理由）。

**两臂判定步数必须一致**：≤3.12 今天靠 `scheduler_config.py:1002-1013`
`_require_safe_directory_final_component_for_mode` 的 `except (OSError, RuntimeError, ValueError)`
把 `RuntimeError` 吞掉，于是**连带跳过**了后面的 containment 复查与 `is_dir()` 裁决；3.13+ 则两步都跑完。
修完之后两臂必须执行同一批判定步骤。

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

#1545 的加料消息（含出错路径，脱敏口径与 `_scheduler_root_blocker` 一致 + 指明 `workspace_root` 派生关系）
与 #1544 新增的 ELOOP 拒绝，**落在同一个函数的相邻两臂**。
必须**先把格式设计成一份、两臂共用**，否则无法同时满足
#1545 的「两臂文案逐字一致」与 #1544 的「两个解释器同一文案」，会需要第二轮返工。

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
- #1547 的 safe_fs 共享底座契约 → `data-integrity-storage-contract`。
  **这是一个judgment call，请评审挑战**：仓内没有任何 capability 拥有 `safe_fs`
  （`grep -rl safe_fs openspec/specs/` 只命中 `hypertable-compression` 与 `prearm-error-model`，
  两者都是别的 lane 顺带提及）。`prearm-error-model` 谈的正是 `SafeFilesystemError` 语义，
  但它是 pre-arm nonmove 那条 lane 的错误模型，把共享原语契约塞进去是范畴错误。
  `data-integrity-storage-contract` 是仓内的存储契约 capability，共享文件系统原语的对外失败契约
  归它最不别扭。若评审认为应另立 capability，说明理由。

## D6. 证据面：版本臂的跑法

一个独立 3.11 venv，建一次、复用：

```
UV_PROJECT_ENVIRONMENT=<scratchpad>/venv311-batchE uv run --python 3.11 python - <<'PY' …
```

**绝不裸跑 `uv run --python 3.11`** —— 会重建项目 `.venv`（三个 issue 正文都点名警告）。
before 矩阵（全部坏形状）与 after 矩阵（收敛）用同一个 venv。

新增测试一律断言**收敛后**的行为，因而与解释器版本无关；它们在 CI 的 3.11 与 node-27 的 3.11.15 上
都会跑，**node-27 receipt 顺带就是 ≤3.12 的实机 oracle**，不需要额外的远端动作。
