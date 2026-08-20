# Tasks — 路径展开/解析抛型面家族收口（#1547 #1549 #1544 #1546 #1545）

> **全程按函数名锚定，不按行号。** master 坐标已与全部五个 issue 正文漂移
> （例：#1549 写 `:572`，master 实际 `:595`）；#1545 正文亦明文警告。

## A. 实现

### A.1 #1547 — `packages/common/safe_fs.py` `_expand_path`

- [ ] A.1(a) 只把 `Path(path).expanduser()` 那一行包进 `try`，`except RuntimeError` →
      抛 `SafeFilesystemError(..., kind="unsafe")`。**cwd 锚定那一行逐字不动。**
- [ ] A.1(b) **不新增 `kind`**，复用 `unsafe`（design D0.1）。
- [ ] A.1(c) docstring 写明失败语义，并点明方向性：`SafeFilesystemError` **是** `RuntimeError` 的子类，
      反向不成立，所以 `except SafeFilesystemError` 的调用方零改动即接住。
- [ ] A.1(d) **不改任何调用方的 `except` 元组**（纯收窄，Non-Goal）。

### A.2 #1549 — 三处 config-construction 裸 expanduser（**不抛**）

- [ ] A.2(a) `_optional_config_path`
- [ ] A.2(b) `_config_path_relative_to_preserve_final`
- [ ] A.2(c) **`_config_path_preserve_final_component`** —— #1549 正文漏数的第三处，
      经 `scheduler_config.py:273`（`workspace_root_preflight_path`）在 db-backed 臂活跃可达。
- [ ] A.2(d) 三处一律**收敛到 db-free 臂的产物**（构造成功、分类留给 preflight），
      **不是**抛结构化错误 —— 与 A.1 方向相反，见 design D0.2。
- [ ] A.2(e) `_optional_config_path` 的注释补一句 tilde 口径；
      **已定稿的 realpath 范式论证（design D2 段）逐字不动**。

### A.3 #1544 — `_require_safe_directory_final_component` 的 S_ISLNK 臂

- [ ] A.3(a) 裸 `path.resolve(strict=False)` → `os.path.realpath(strict=True)` 优先。
- [ ] A.3(b) **ELOOP（errno 62）→ 结构化 `ValueError`**，两个解释器同类型同文案。
- [ ] A.3(c) **ENOENT → 非 strict `os.path.realpath` 兜底**，继续走 containment 复查与 `is_dir()` 裁决。
      **这条是 load-bearing 的**：strict realpath 对**悬空 symlink** 抛 ENOENT，
      而今天悬空 symlink 是**放行**的。做成 strict-only 会把放行变成拒绝 —— 静默打断 userspace。
- [ ] A.3(d) 两臂判定步数一致：≤3.12 不再因
      `scheduler_config.py` `_require_safe_directory_final_component_for_mode` 的
      `except (OSError, RuntimeError, ValueError)` 吞掉 `RuntimeError` 而跳过后续两步。

### A.4 #1545 — 拒绝文案

- [ ] A.4(a) `WORKSPACE_ROOT` 环的拒绝消息**含出错路径**（脱敏口径与 `_scheduler_root_blocker` 一致）
      且能定位到 `workspace_root` 旋钮（点名字段或显式标注派生关系）。
- [ ] A.4(b) 类型仍 `ValueError`；`lock_path` 那条**逐字不变**；非环输入的全部拒绝文案不变。
- [ ] A.4(c) **与 A.3(b) 共用同一套消息格式**（design D1）—— 先设计一份两臂共用，
      否则无法同时满足 #1545「两臂逐字一致」与 #1544「跨解释器一致」。

### A.5 #1546 — 兼容面那一对

- [ ] A.5(a) `_resolve_optional_config_path`：裸 `value.resolve()` → strict-then-non-strict realpath 范式。
- [ ] A.5(b) `_optional_config_path_relative_to`：**两族交叉，两条都要治** ——
      裸 `expanduser()` 走 A.2 的不抛口径，裸 `path.resolve()` 走本条的范式。
      **本单最容易只改一半的地方。**
- [ ] A.5(c) 既有契约零回归：`None`/`""` 早退返回 `None`；相对值仍按 `base` 拼接；
      ENOENT 仍返回非 strict 规范化结果而不抛。
- [ ] A.5(d) `docs/governance/SCHEDULER_COMPATIBILITY_INVENTORY.md` 的
      `scheduler-runtime-roots-forwarders` 行与实现保持一致。

### 全 PR 改动面 allowlist（可机械核验）

- [ ] A.6 非 `tests/**`、非 `openspec/**` 的改动**只允许**出现在：
      `packages/common/safe_fs.py`、`services/orchestrator/scheduler_runtime_roots.py`、
      `docs/governance/SCHEDULER_COMPATIBILITY_INVENTORY.md`（A.5(d) 一行）。
- [ ] A.7 **`services/orchestrator/scheduler_config.py` 零改动** ——
      `_expanduser_for_mode` 的「故意 re-raise」是 #1423/#1520 已裁定的设计决策（Non-Goal）。
      机械核验：`git diff <base>..HEAD -- services/orchestrator/scheduler_config.py` 为空。
- [ ] A.8 **不改 `scheduler_runtime_roots.py:271` / `:332`** —— 两处已双接
      （`except OSError` + `except RuntimeError` / `except (OSError, RuntimeError, ValueError)`），
      家族范式已到位，动它们属扩面。

## B. 测试

### B.1 #1547（放进**既有** `tests/test_safe_fs.py`，见 design D4）

- [ ] B.1(a) 写侧 `ensure_directory_no_follow`、读侧 `read_bytes_limited_no_follow`、
      删侧 `rmtree_no_follow` 三个入口，输入 `~<不存在用户>/…`，
      断言抛 `SafeFilesystemError` **且 `kind == "unsafe"`**。
- [ ] B.1(b) **副作用断言**：cwd 下**未留下任何字面 `~…` 目录/文件**。
      这条是防 verbatim-keep 备选方案回潮的判别器，不可省。
- [ ] B.1(c) 逃逸面 A：`scripts/scheduler_file_provider_refresh.py::_apply_environment_file`
      输入同形路径 → 断言 `RefreshError("configuration_invalid")`，不是裸 `RuntimeError`。
- [ ] B.1(d) 逃逸面 B 抽检：`services/production_closure/met_validation.py` 的 evidence-root 准备
      在同输入下产出 `PRODUCTION_MET_EVIDENCE_*` 结构化码。
- [ ] B.1(e) 全部新用例 `tmp_path` 隔离、不留进程级状态
      （`tests/test_safe_fs.py` 是 #1613 顺序依赖的触发文件，见 design D4）。

### B.2 #1549 —— parity 断言，不是「不抛」

- [ ] B.2(a) `allowed_storage_roots=("~nosuchuser_zz/roots",)`、`log_root="~nosuchuser_zz/logs"`、
      `workspace_root="~nosuchuser_zz/workspace"` 三个字段，在 **db-backed 臂**构造**成功**。
- [ ] B.2(b) **两臂产物逐字相等**（db-backed == db-free）。只断言「没抛」对
      「抛型改了但产物错了」恒绿。
- [ ] B.2(c) 同输入下 preflight 产出**结构化结果**（`status`/`blockers`）而非抛栈；
      具体 reason 以实现期探针实测为准（沿 #1424/#1548 记录法）。

### B.3 #1544 —— 五条语义栅栏

- [ ] B.3(a) workspace **内部**末段环 × db-free/db-backed 两臂 → 同一结构化 `ValueError`。
- [ ] B.3(b) 健康目录 symlink → 通过。
- [ ] B.3(c) symlink 指向文件 → `must be a directory`。
- [ ] B.3(d) symlink 逃出 workspace → `must be under workspace_root`。
- [ ] B.3(e) 末段不存在（`FileNotFoundError`）→ 直接 return。
- [ ] B.3(f) **悬空 symlink（指向不存在目标）→ 放行**。这条是 A.3(c) 兜底存在的理由，
      strict-only 实现会把它从放行变成拒绝，必须有独立用例逮住。

### B.4 #1545

- [ ] B.4(a) 更新 `tests/test_production_scheduler.py` 里
      `test_resolve_residue_config_final_segment_loop_converges_to_structured_refusal` 的
      `WORKSPACE_ROOT` 那一行 parametrize 期望值（**授权改写，见 D2**）。
- [ ] B.4(b) 同 parametrize 上方注释里「declared non-goal, pinned rather than corrected」
      的措辞**删除或改写** —— 否则注释与新断言自相矛盾。
- [ ] B.4(c) `NHMS_SCHEDULER_LOCK_ROOT` 那一行**逐字保留**。
- [ ] B.4(d) 新增断言：消息**含路径**、**可定位到 workspace_root**。

### B.5 #1546

- [ ] B.5(a) 环路 × 两个函数 × 相对/绝对入参，断言两臂同一规范化结果。
- [ ] B.5(b) `None`/`""` 早退、相对拼接、ENOENT 不抛，三条契约各一条用例。

### B.6 判别力自证（每条修法一个 mutant，逐条必红）

- [ ] B.6(a) A.1：去掉 `try/except` → B.1(a) 必红，且红因是裸 `RuntimeError` 逃逸。
- [ ] B.6(b) A.2：把三处之一改回 try 之外 → B.2 对应字段必红（**parity 断言**红，不是「抛了」红）。
- [ ] B.6(c) A.3(c)：把 ENOENT 兜底删掉（strict-only）→ **B.3(f) 悬空 symlink 用例必红**。
      这条是本单最重要的判别器 —— 它证明「打断 userspace」这条被真的钉住了。
- [ ] B.6(d) A.4：把消息改回旧文案 → B.4(d) 必红。
- [ ] B.6(e) A.5(b)：只改 resolve 不改 expanduser（或反之）→ 对应用例必红，证明两族交叉都被覆盖。
- [ ] 每条 mutant 必须 `git checkout -- <file>` 还原并以 `git diff HEAD` 为空自证。

## C. 验证（Evidence Floor）

- [ ] C.1 `uv run pytest -q tests/test_safe_fs.py tests/test_production_scheduler.py tests/test_scheduler_file_provider_refresh.py tests/test_production_met_validation.py`
- [ ] C.2 `uv run ruff check $(git ls-files '*.py')`（**不是** `ruff check .` —— 后者会命中
      未跟踪的本地 `skills/` 工具，报与本 PR 无关的 E501）
- [ ] C.3 **版本臂 before/after 矩阵**（判别力所在）：一个独立 3.11 venv，建一次复用 ——
      `UV_PROJECT_ENVIRONMENT=<scratchpad>/venv311-batchE uv run --python 3.11 …`。
      **绝不裸跑 `uv run --python 3.11`**（会重建项目 `.venv`）。
      矩阵覆盖 #1544 / #1545 / #1546 三组几何，before 与 after 均贴 PR body。
- [ ] C.4 `openspec validate path-expansion-throw-face-closure --strict --no-interactive`
- [ ] C.5 merge 后 node-27 全量 receipt，`umask 022`，独立 detached worktree，
      **不碰 `/home/nwm/NWM` 主树**。判读口径「相对 master 基线**不新增红**」，
      master 已知红：`test_entropy_audit_script.py::…hard_gate…`、
      `test_scheduler_file_provider_refresh.py::test_provider_snapshot_rejects_replacement_between_metadata_and_read`、
      以及 #1613 那条顺序依赖。**node-27 是 3.11.15，该 receipt 顺带就是 ≤3.12 的实机 oracle。**
- [ ] C.6 node-22 全程不跑任何东西（本单纯逻辑，无 Slurm/SHUD 面）。

## D. 记账与承接（必须进 PR body）

- [ ] D.1 **口径更正 1**：#1549 漏数第三处 `_config_path_preserve_final_component`（proposal 已记）。
- [ ] D.2 **口径更正 2**：#1546 的「仓内零调用方」成立，但要说准 ——
      `scheduler_config.py` 的 7 处形似调用走的是 `_resolve_config_path_for_mode`，
      **不经过** `_scheduler._resolve_optional_config_path`。
- [ ] D.3 **口径更正 3（#1547 前提 stale）**：`tests/test_safe_fs.py` 已存在，
      新测试并入该文件；「CI selector 路由核查」那条验收项随之不适用（不是跳过）。
- [ ] D.4 **`chain_runtime_utils.py` `_absolute_configured_path` 立单承接** ——
      与 `_expand_path` 逐字同形，跨 lane、语义可能不同，**本单不修**。
      必须开 issue 并把编号回填此处：`#____`。
      （#1547 验收标准明文要求，防重演 #1423「仅登记不修、无单承接」。）
- [ ] D.5 **归档口径不改写**（design D3）：`runtime-roots-resolve-residue` Non-Goal 2 与
      PR #1548 `proposal.md:25-33` 的可达性机制归因（真实机制是 `_optional_config_path`
      这一族，不是 `_expanduser_for_mode`），承接关系记在本 change 的 proposal
      与关闭 issue 时的评论里，**不重写 archive 文件**。
- [ ] D.6 **oracle 改写预声明**（design D2）：B.4(a) 改的是 PR #1541 精确等值 pin 的
      `WORKSPACE_ROOT` 那一行，授权来源是 #1545 本身。PR body 必须列出
      旧期望串 → 新期望串，否则会撞合并硬门的「不得削弱 oracle」条款。
- [ ] D.7 家族账目结清：PR body 附机械 grep 表 ——
      `scheduler_runtime_roots.py` 内每一处 `resolve(` / `expanduser(` 及其处置
      （已治 / 本单治 / 已双接不动），声明 `#1332→#1423→#1520→#1544→#1546` 这条链上
      本文件**再无**未收敛裸站点，或列出保留者与理由。
- [ ] D.8 诚实记账：判别力只在 ≤3.12 臂；#1546 今天无活调用方，改它买的是家族账目结清
      而非当下崩溃修复；#1547 触发条件少见但影响面是共享底座。
