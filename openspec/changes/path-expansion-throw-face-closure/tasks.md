# Tasks — 路径展开/解析抛型面家族收口（#1547 #1549 #1544 #1546 #1545）

> **全程按函数名锚定，不按行号。** master 坐标已与全部五个 issue 正文漂移
> （例：#1549 写 `:572`，master 实际 `:595`）；#1545 正文亦明文警告。

## A. 实现

### A.1 #1547 — `packages/common/safe_fs.py` `_expand_path`

- [x] A.1(a) 只把 `Path(path).expanduser()` 那一行包进 `try`，`except RuntimeError` →
      抛 `SafeFilesystemError(..., kind="unsafe")`。**cwd 锚定那一行逐字不动。**
- [x] A.1(b) **不新增 `kind`**，复用 `unsafe`（design D0.1）。
- [x] A.1(c) docstring 写明失败语义，并点明方向性：`SafeFilesystemError` **是** `RuntimeError` 的子类，
      反向不成立，所以 `except SafeFilesystemError` 的调用方零改动即接住。
- [x] A.1(d) **不改任何调用方的 `except` 元组**（纯收窄，Non-Goal）。

### A.2 #1549 — 三处 config-construction 裸 expanduser（**不抛**）

- [x] A.2(a) `_optional_config_path`
- [x] A.2(b) `_config_path_relative_to_preserve_final`
- [x] A.2(c) `_config_path_preserve_final_component` —— **兼容面 / 家族账目项，不是活崩溃路径**。
      初稿说它「db-backed 臂活跃可达」是错的（fixture 审 P0-2 实测推翻）：
      `scheduler_config.py:269` 的 `_raw_config_path_preserve_components` 排在 `:273` 之前，
      任何 tilde 输入先在 `_expanduser_for_mode` re-raise，走不到这里。
      仍修（同款裸副本，值得结清），但**验收只能按直接调该函数写**（见 B.2(d)）。
- [x] A.2(d) 三处一律**收敛到 db-free 臂的产物**（构造成功、分类留给 preflight），
      **不是**抛结构化错误 —— 与 A.1 方向相反，见 design D0.2。
- [x] A.2(e) `_optional_config_path` 的注释补一句 tilde 口径；
      **已定稿的 realpath 范式论证（design D2 段）逐字不动**。

### A.3 #1544 — `_require_safe_directory_final_component` 的 S_ISLNK 臂

- [x] A.3(a) 裸 `path.resolve(strict=False)` → `os.path.realpath(strict=True)` 优先。
- [x] A.3(b) **`errno.ELOOP` → 结构化 `ValueError`**，两个**解释器臂**同类型同文案。
      **必须写 `errno.ELOOP`，不得硬编码 62** —— Darwin 是 62、**Linux 是 40**，
      硬编码会做出一条本机绿、CI 与 node-27 上永远走不到的死分支（本模块 `:411` 已 import `ELOOP`）。
      判据是在既有 `except OSError` 臂内**按 errno 分流**，**不得整臂改写**（护栏见 D.6）。
      **两个代码臂的分流形状不同（round-1 裁定，见 D.10）**：
      - **lstat 臂**（`except OSError` on `path.lstat()`）：`ELOOP` → 加料文案；**其余 errno 保持
        `must be a safe directory` 逐字不变**（`:30460`/`:30591` 两条 pin 就在这条臂上）。
      - **S_ISLNK 臂**（strict realpath）：`ELOOP` → 结构化拒绝；**其余 errno 一律落非 strict 兜底**，
        **不是**「保持既有拒绝」—— 那条臂上**根本不存在**非环的既有拒绝（master 走
        `path.resolve(strict=False)`，3.13+ 压根不抛）。
- [x] A.3(c) **S_ISLNK 臂上，除 `ELOOP` 外的每一个 `OSError` → 非 strict `os.path.realpath` 兜底**，
      继续走 containment 复查与 `is_dir()` 裁决。
      **这条是 load-bearing 的，且兜底面必须宽于 ENOENT**（round-1 裁定，见 D.10）：
      strict realpath 在这条臂上至少抛三种 errno，而**三种几何 master 今天都放行**——
      orchestrator 独立实测（3.14.2）：悬空 symlink → `ENOENT(2)` ACCEPT；
      symlink 指向 0600 父目录下的项 → `EACCES(13)` ACCEPT；
      symlink 路径穿过普通文件 → `ENOTDIR(20)` ACCEPT。
      只兜 ENOENT（或字面执行「其余保持既有拒绝」）会把后两种从**放行翻成拒绝** —— 静默打断 userspace。
- [x] A.3(d) **判定一致按解释器臂表述**（初稿按 database 臂写，fixture 审 P0-3 实测推翻）：
      3.11/3.12 与 3.13+ 在 db-backed 臂上（或直接调被守卫函数）得到同一结构化拒绝。
      **不要求跨 database 臂一致** —— `scheduler_config.py:1020-1024` 的 db-free 包裹层
      今天就把**全部**拒绝一揽子吞掉（实测：symlink 指向文件 / 逃出 workspace / 悬空且指向 workspace 外，
      三种几何 db-backed 报 `ValueError`、db-free 一律 ACCEPTED），
      而该文件在 A.7 下零改动，所以跨 database 臂一致**不可实现**。

### A.4 #1545 — 拒绝文案

- [x] A.4(a) `WORKSPACE_ROOT` 环的拒绝消息**含出错路径**（直接带 `str(path)`，
      与本函数 `:740/:745/:748` 的兄弟拒绝一致 —— 初稿写「脱敏口径与 `_scheduler_root_blocker` 一致」
      是错的，fixture 审 P1-3 推翻：那个函数不做脱敏，`[local-path]` 由调用侧的 `evidence_safe_paths`
      标志决定，本守卫既无该标志也拿不到 config 句柄；真脱成 `[local-path]` 还会与 B.4(d) 自相矛盾），
      且能定位到 `workspace_root` 旋钮（点名字段或显式标注派生关系）。
- [x] A.4(b) 类型仍 `ValueError`；`lock_path` 那条**逐字不变**；非环输入的全部拒绝文案不变。
- [x] A.4(c) **推荐**抽一个共用的消息构造 helper（同一句式、同一路径呈现），
      但**字段归因按各自代码臂各报各的**（design D1）。初稿写「必须两臂共用同一文案，
      否则两条要求无法同时满足」是**非因果**的（fixture 审 P2-2）：两条要求都只约束**解释器臂**一致；
      强行统一反而会让 S_ISLNK 臂（环真在运维配的 `NHMS_SCHEDULER_EVIDENCE_ROOT` 里）
      也去指 `workspace_root`，**反方向重新制造归因错误**。

### A.5 #1546 — 兼容面那一对

- [x] A.5(a) `_resolve_optional_config_path`：裸 `value.resolve()` → strict-then-non-strict realpath 范式。
- [x] A.5(b) `_optional_config_path_relative_to`：**两族交叉，两条都要治** ——
      裸 `expanduser()` 走 A.2 的不抛口径，裸 `path.resolve()` 走本条的范式。
      **本单最容易只改一半的地方。**
- [x] A.5(c) 既有契约零回归：`None`/`""` 早退返回 `None`；相对值仍按 `base` 拼接；
      ENOENT 仍返回非 strict 规范化结果而不抛。
- [x] A.5(d) 核对 `docs/governance/SCHEDULER_COMPATIBILITY_INVENTORY.md` 的
      `scheduler-runtime-roots-forwarders` 行。**默认预期是「无需改动」** —— fixture 审已核实
      该行只是符号清单 + 保留理由，**不描述 resolve/expanduser 语义**，而本单不删符号、不改签名。
      若实现期发现确需改，在 PR body 写明改了哪句、为什么；否则在 PR body 明确记「已核对，无需改动」。
      **不得为了「有个交代」而随手改一行**。

### 全 PR 改动面 allowlist（可机械核验）

- [x] A.6 非 `tests/**`、非 `openspec/**` 的改动**只允许**出现在：
      `packages/common/safe_fs.py`、`services/orchestrator/scheduler_runtime_roots.py`、
      `docs/governance/SCHEDULER_COMPATIBILITY_INVENTORY.md`（A.5(d)；**默认无需改动**）。
- [x] A.7 **`services/orchestrator/scheduler_config.py` 零改动**，两条各自的理由：
      (a) `_expanduser_for_mode`（`:850-857`）的「故意 re-raise」是 #1423/#1520 已裁定的设计决策；
      (b) `_require_safe_directory_final_component_for_mode`（`:1020-1024`）的 db-free 一揽子吞异常
      是独立缺陷，本单不治（它使跨 database 臂一致不可实现，见 A.3(d)）。
      机械核验：`git diff <base>..HEAD -- services/orchestrator/scheduler_config.py` 为空。
- [x] A.8 **不改四处已核实的非 scope 站点**（proposal 有表）：
      `:271`（已双接 `except OSError` + `except RuntimeError`）、
      `:332`（包在 `except (OSError, RuntimeError, ValueError)` 里）、
      `:504`（`_scheduler_allowed_roots_and_blockers`，裸但 tilde 非活 —— `allowed_storage_roots`
      已在 `scheduler_config.py:418-423` 归一为绝对路径）、
      `:578`（`_confined_path`，裸但 tilde 非活 —— 全部生产路径之前都有 `_raw_config_path_*` 先 re-raise）。
      后两处是 fixture 审补列的，属**文档缺口不是 scope 缺口**。

## B. 测试

### B.1 #1547（放进**既有** `tests/test_safe_fs.py`，见 design D4）

- [x] B.1(a) 写侧 `ensure_directory_no_follow`、读侧 `read_bytes_limited_no_follow`、
      删侧 `rmtree_no_follow` 三个入口，输入 `~<不存在用户>/…`，
      断言抛 `SafeFilesystemError` **且 `kind == "unsafe"`**。
- [x] B.1(b) **副作用断言**：用 `monkeypatch.chdir(tmp_path)` 把 cwd 钉到 `tmp_path`（自动还原，
      与 B.1(e) 不冲突），断言该目录下**未留下任何字面 `~…` 目录/文件**。
      **必须显式 chdir**：`_expand_path` 锚的是 `Path.cwd()`，不 chdir 的话
      verbatim-keep mutant 会把 `~nosuchuser_zz` 建进**仓库工作树**里（fixture 审 P2-5）。
      这条是防 verbatim-keep 备选方案回潮的判别器，不可省。
- [x] B.1(c) 逃逸面 A：`scripts/scheduler_file_provider_refresh.py::_apply_environment_file`
      输入同形路径 → 断言 `RefreshError("configuration_invalid")`，不是裸 `RuntimeError`。
- [x] B.1(d) 逃逸面 B 抽检：`services/production_closure/met_validation.py` 的 evidence-root 准备
      在同输入下产出 `PRODUCTION_MET_EVIDENCE_*` 结构化码。
- [x] B.1(e) 全部新用例 `tmp_path` 隔离、不留进程级状态
      （`tests/test_safe_fs.py` 是 #1613 顺序依赖的触发文件，见 design D4）。

### B.2 #1549 —— parity 断言，不是「不抛」

- [x] B.2(a) `allowed_storage_roots=("~nosuchuser_zz/roots",)` 与 `log_root="~nosuchuser_zz/logs"`
      **两个**字段，在 **db-backed 臂**构造**成功**。
      **`workspace_root` 不在此列**（初稿写了，fixture 审 P0-1 实测推翻）：它在 `scheduler_config.py:269`
      的 `_expanduser_for_mode` 上更早 re-raise，而那是 A.7 钉死零改动的文件里的 Non-Goal。
- [x] B.2(b) **跨 database 臂的构造产物逐字相等**（db-backed == db-free）。只断言「没抛」对
      「抛型改了但产物错了」恒绿。（fixture 审已模拟确认三处修法都能达成 parity，非纸上要求。）
- [x] B.2(c) 同输入下 preflight 产出**结构化结果**（`status`/`blockers`）而非抛栈；
      具体 reason 以实现期探针实测为准（沿 #1424/#1548 记录法）。
- [x] B.2(d) A.2(c) 的兼容面站点：**直接调** `_config_path_preserve_final_component("~nosuchuser_zz/workspace")`，
      断言不抛裸 `RuntimeError` 且产物与 db-free 臂一致。不得写成「构造 `workspace_root` 成功」。

### B.3 #1544 —— 五条语义栅栏

- [x] B.3(a) workspace **内部**末段环 → 结构化 `ValueError`，**db-backed 臂**上跨解释器臂一致
      （或直接测 `_require_safe_directory_final_component` 本身）。
      **不要写成 db-free/db-backed 两臂一致** —— 不可实现，见 A.3(d)。
- [x] B.3(b) 健康目录 symlink → 通过。
- [x] B.3(c) symlink 指向文件 → `must be a directory`。
- [x] B.3(d) symlink 逃出 workspace → `must be under workspace_root`。
- [x] B.3(e) 末段不存在（`FileNotFoundError`）→ 直接 return。
- [x] B.3(f) **悬空 symlink 且目标在 workspace 内 → 放行**。这条是 A.3(c) 兜底存在的理由，
      strict-only 实现会把它从放行变成拒绝，必须有独立用例逮住。
      **`GIVEN` 必须写明「目标在 workspace 内」**（fixture 审 P2-1）：目标在 workspace **外**时，
      `:743` 的 containment 复查先于 `exists()` 闸触发，今天就报 `must be under workspace_root`；
      用例若随手挑了个 workspace 外的目标，会**因为别的原因红**，而 B.6(c) 恰恰拿它当头号判别器。
- [x] B.3(g) **悬空 symlink 且目标在 workspace 外 → 仍报 `must be under workspace_root`**（今天的行为）。

### B.4 #1545

- [x] B.4(a) 更新 `tests/test_production_scheduler.py` 里
      `test_resolve_residue_config_final_segment_loop_converges_to_structured_refusal` 的
      `WORKSPACE_ROOT` 那一行 parametrize 期望值（**授权改写，见 D2**）。
- [x] B.4(b) 同 parametrize 上方注释里「declared non-goal, pinned rather than corrected」
      的措辞**删除或改写** —— 否则注释与新断言自相矛盾。
- [x] B.4(c) `NHMS_SCHEDULER_LOCK_ROOT` 那一行**逐字保留**。
- [x] B.4(d) 新增断言：消息**含路径**（`str(path)`）、**可定位到 workspace_root**。
- [x] B.4(e) **护栏**：同一条旧文案在 `tests/test_production_scheduler.py:30460` 与 `:30591`
      还有另外两处 pin，那两处是**非环**几何（`workspace_root` 是普通文件 / mode 0600 不可遍历），
      经同一个 `except OSError` 代码臂，**必须逐字保持绿**。
      ⇒ 实现只能在该臂内按 `errno == errno.ELOOP` 分流，不得整臂改写。

### B.5 #1546

- [x] B.5(a) 环路 × 两个函数，断言跨**解释器臂**同一规范化结果。
      相对入参只对 `_optional_config_path_relative_to` 适用 ——
      `_resolve_optional_config_path(value: Path | None)` 不收 base、没有相对臂（fixture 审 P3）。
- [x] B.5(b) `None`/`""` 早退、相对拼接、ENOENT 不抛，三条契约各一条用例。

### B.6 判别力自证（每条修法一个 mutant，逐条必红）

- [x] B.6(a) A.1：去掉 `try/except` → B.1(a) 必红，且红因是裸 `RuntimeError` 逃逸。
- [x] B.6(b) A.2 需要**两个** mutant（初稿只写一个，且把它的红形描述错了 —— fixture 审 P3）：
      (i) 把某处改回 try 之外 → 对应用例红，红形是**构造抛异常**（不是 parity 不等）；
      (ii) **产物锚错**（例如把 `log_root` 从 base 锚定改成 cwd 锚定）→ **parity 断言**红。
      只有 (ii) 才真正证明 B.2(b) 的 parity 断言有判别力。
- [x] B.6(c) A.3(c)：把 ENOENT 兜底删掉（strict-only）→ **B.3(f) 悬空 symlink 用例必红**。
      这条是本单最重要的判别器 —— 它证明「打断 userspace」这条被真的钉住了。
- [x] B.6(d) A.4：把消息改回旧文案 → B.4(d) 必红。
- [x] B.6(e) A.5(b)：只改 resolve 不改 expanduser（或反之）→ 对应用例必红，证明两族交叉都被覆盖。
- [x] 每条 mutant 必须 `git checkout -- <file>` 还原并以 `git diff HEAD` 为空自证。

## C. 验证（Evidence Floor）

- [x] C.1 `uv run pytest -q tests/test_safe_fs.py tests/test_production_scheduler.py tests/test_scheduler_file_provider_refresh.py tests/test_production_met_validation.py`
- [x] C.2 `uv run ruff check $(git ls-files '*.py')`（**不是** `ruff check .` —— 后者会命中
      未跟踪的本地 `skills/` 工具，报与本 PR 无关的 E501）
- [x] C.3 **版本臂 before/after 矩阵**（判别力所在）：一个独立 3.11 venv，建一次复用 ——
      `UV_PROJECT_ENVIRONMENT=<scratchpad>/venv311-batchE uv run --python 3.11 …`。
      **绝不裸跑 `uv run --python 3.11`**（会重建项目 `.venv`）。
      矩阵覆盖 #1544 / #1545 / #1546 三组几何，before 与 after 均贴 PR body。
- [x] C.4 `openspec validate path-expansion-throw-face-closure --strict --no-interactive`
- [ ] C.5 merge 后 node-27 全量 receipt，`umask 022`，独立 detached worktree，
      **不碰 `/home/nwm/NWM` 主树**。判读口径「相对 master 基线**不新增红**」，
      master 已知红：`test_entropy_audit_script.py::…hard_gate…`、
      `test_scheduler_file_provider_refresh.py::test_provider_snapshot_rejects_replacement_between_metadata_and_read`、
      以及 #1613 那条顺序依赖。**node-27 是 3.11.15，该 receipt 顺带就是 ≤3.12 的实机 oracle。**
- [ ] C.6 node-22 全程不跑任何东西（本单纯逻辑，无 Slurm/SHUD 面）。

## D. 记账与承接（必须进 PR body）

- [ ] D.1 **口径更正 1（我的初稿被 fixture 审推翻）**：初稿声称「#1549 漏数第三处、
      `_config_path_preserve_final_component` 在 db-backed 臂活跃可达」——**假的**。
      `scheduler_config.py:269` 更早 re-raise，该站点 tilde 非活，属兼容面。
      真正走到本模块裸 expanduser 的生产字段**只有两个**：`allowed_storage_roots`、`log_root`。
- [ ] D.2 **口径更正 2**：#1546 的「仓内零调用方」成立，但要说准 ——
      `scheduler_config.py` 的 7 处形似调用走的是 `_resolve_config_path_for_mode`，
      **不经过** `_scheduler._resolve_optional_config_path`。
- [ ] D.3 **口径更正 3（#1547 前提 stale）**：`tests/test_safe_fs.py` 已存在，
      新测试并入该文件；「CI selector 路由核查」那条验收项随之不适用（不是跳过）。
- [ ] D.4 **`chain_runtime_utils.py` `_absolute_configured_path` 立单承接** ——
      与 `_expand_path` 逐字同形，跨 lane、语义可能不同，**本单不修**。
      站点是 `services/orchestrator/chain_runtime_utils.py:487-489`
      （#1547 正文写 `:438-440`，master 已漂；fixture 审实测确认与 `_expand_path` **逐字同形**）。
      已开 **#1621** 承接。
      （#1547 验收标准明文要求，防重演 #1423「仅登记不修、无单承接」。）
- [ ] D.5 **归档口径不改写**（design D3）：承接关系记在本 change 的 proposal 与关闭 issue 时的评论里，
      **不重写 archive 文件**。
      **且这条「更正」本身要限定范围**（初稿写成了一刀切，fixture 审 P1-2 推翻）：
      归档的 `2026-08-18-expanduser-throw-face-residue/proposal.md:27-32` **已经按字段分开写对了** ——
      多数 root 字段确实崩在 `_expanduser_for_mode`。需要改引本单编号的**只有
      `allowed_storage_roots` 与 `log_root` 这两条链路**；其余九个字段的
      `_expanduser_for_mode` 归因**仍然正确**，不得在 PR body / issue 评论里写成一刀切
      —— 那会把一条假陈述写进永久记录。
- [ ] D.6 **oracle 改写预声明**（design D2）：B.4(a) 改的是 PR #1541 精确等值 pin 的
      `WORKSPACE_ROOT` 那一行，授权来源是 #1545 本身（`runtime-roots-resolve-residue` 的 Non-Goal 2
      明文承诺立单承接，归档 `proposal.md:34-35` 已回填 `#1545`）。PR body 必须列出
      旧期望串 → 新期望串，否则会撞合并硬门的「不得削弱 oracle」条款。
      **同时必须写明护栏**：同一条旧文案在 `tests/test_production_scheduler.py:30460` 与 `:30591`
      还有另外两处 pin，那两处是**非环**几何（普通文件 / mode 0600），经同一个 `except OSError` 代码臂，
      **逐字保持绿** —— 这就是 A.3(b) 要求「按 `errno.ELOOP` 分流而非整臂改写」的原因。
- [ ] D.7 家族账目结清：PR body 附机械 grep 表 ——
      `scheduler_runtime_roots.py` 内**每一处** `resolve(` / `expanduser(` / `realpath(` 及其处置
      （已治 / 本单治 / 已双接不动 / **裸但 tilde 非活**），四处非 scope 站点（`:271 :332 :504 :578`）
      必须逐条带理由出现在表里；`packages/common/safe_fs.py` 同样列（该文件只有 `_expand_path` 一处）。
      声明 `#1332→#1423→#1520→#1544→#1546` 这条链上两个文件**再无**未收敛裸站点，或列出保留者与理由。
- [ ] D.10 **round-1 裁定：S_ISLNK 臂是两分岔，不是三分岔**（implementer D-1，orchestrator 实测采纳）。
      fixture 初稿的「其余 real-path 失败保持既有拒绝」是**按 lstat 臂**写的，被错误地一并套到了
      S_ISLNK 臂上；后者不存在非环的既有拒绝。实测证据（master，3.14.2）：
      `EACCES(13)` 与 `ENOTDIR(20)` 两种几何**今天都 ACCEPT**，字面三分岔会把它们翻成拒绝。
      裁定：S_ISLNK 臂 `ELOOP → 拒绝 / 其余 OSError → 非 strict 兜底`；lstat 臂维持 errno 分流 +
      非环文案逐字不变。PR body 必须记这条裁定与实测。
- [ ] D.11 **B.1(d) 的可达性前提是假的**（implementer D-3，已实测）：`EvidenceWriter.prepare()`
      在自己的 containment 闸（`resolved_lane.relative_to(self.evidence_root)`）上就拒了，
      **走不到任何 safe_fs 原语**；而经 `from_env` / `validate_met` 的那条路，裸 `RuntimeError`
      来自 `services/production_closure/met_validation.py:1926` **自己的** `expanduser()`，在本单 allowlist 之外。
      spec scenario 按字面仍成立（该 lane 确实产结构化码），但**不是**靠 A.1 的修复成立的。
      处置：保留该用例并在测试里写明 HONEST LIMIT，spec 已补限定句，`:1926` 已开 **#1622** 承接。
- [ ] D.12 **另两条只报不修**（implementer 观察 2/3）：
      (a) 守卫 `resolved.exists()` 闸上的 EACCES 跨版本分歧（3.11 抛 `PermissionError`、3.12+ 吞掉）——
      #1544 家族在另一行的残留，五个 issue 都没覆盖，本单未触碰：已开 **#1623** 承接（旁系同型 #1554）。
      (b) `scheduler_runtime_roots.py:242` / `:255` 两处裸 expanduser，tilde 非活
      （输入是构造期已归一的绝对值，探针跑通全 preflight 无抛）——与 `:504`/`:578` 同类，
      属 D.7 表的文档面，已补入表中，不另立单。
- [ ] D.13 **对 implementer brief 的事实更正**（implementer D-5）：`:30460` / `:30591` 两条护栏 pin 是
      `pytest.raises(ValueError, match=...)` **正则匹配**，不是精确等值。结论不变（非环文案逐字保留、两条均绿），
      但 PR body 不得把它们描述成精确等值 pin。
- [ ] D.9 **新 capability 的 Purpose**：`safe-filesystem-primitive-contract` 归档后会带
      `Purpose: TBD - created by archiving change …` —— 正是 D5 拿来否掉
      `data-integrity-storage-contract` 的那条毛病。仓内无先例在 delta 里写 `## Purpose`
      （归档才物化 `openspec/specs/<cap>/spec.md`），故**归档 chore PR 里补一句真 Purpose**，
      别让它以 TBD 落地。
- [ ] D.14 **round-1 交叉评审 + 独立裁决 + 修复轮记账**（必须进 PR body）：
      lens A 无 P0/P1/P2；lens B 3×P2 + 1×P3；两批独立 verifier 共 8 条裁决 ——
      **5 条 CONFIRMED/FIX_NOW 已修，2 条 DISCARD，1 条 DEFER**。
      - **F1（P2）**：`_canonical_path` 换成恒等函数，`1692 passed` **全绿存活**（两个解释器臂皆然）。
        三条 compat-surface 用例都先把入参 canonical 化再断言 `f(x) == x`，恒等函数照样满足；
        spec R4 卖的「normalized」从没被检查过。**比 PR 已声明的限制严格更强**——已声明的判别器
        （回退 `value.resolve()`）只在 3.11 红，恒等 mutant 两臂全绿。已补
        `_resolve_optional_config_path(loop/"y"/".."/"z") == loop/"z"`，两臂验证。
      - **F2（P3）**：`_expanduser_or_verbatim` 退化成「永不展开」，`1692 passed` + Evidence Floor 其余
        `310 passed` 全绿存活。原因：文件里每条 tilde 用例都用**故意不可展开**的 tilde。
        master 用的是 stdlib `.expanduser()`（无需栅栏），本单换成手写两分支包装却只钉了异常分支，
        **每个真实运维配置都走的 happy 分支无人看守**。已补展开断言。
      - **F3（P2）**：EACCES 用例的裸 `except PermissionError` 把本单**自己负责**的 strict-realpath 步骤
        也一并吞了——strict-only mutant 下另外三条红、**它自己绿**。已按解释器版本设闸：
        3.12+ 裸调断言，`< 3.12` 才保留容忍（那半是 #1623 的面）。
      - **F4（P3）**：ELOOP 文案硬编码「the final component is a symlink loop」，但 strict realpath
        对**解析链上任何位置**的环都抛 ELOOP。实测 `via_midloop -> midloop/tail` 几何下末段是健康 symlink，
        文案却把运维指向错的链接——正是 #1545 要消除的那类归因错误。已改为按 `error.filename` 归因
        （verifier 更正了 reviewer 的数据：filename 在两臂上都是**成环的那个组件**，故跨臂逐字一致）。
      - **F5（P3）**：spec R2 场景 1 写的是「构造路径」，九条用例却全在直调守卫。已补构造路径用例。
      - **DISCARD 1**：root-UID 下 EACCES 栅栏空转 —— 任何 oracle 都不以 root 跑
        （CI `runs-on: ubuntu-latest` 无 job 级 `container:`；node-27 是 `nwm`；本机 euid 501），
        且 `:30889-30899` 的既有金丝雀在 root 下会**响亮失败**而非静默通过。
      - **DISCARD 2**：「verbatim 只按子串强制」—— 那条 `raise` 在 diff 里是 **context 行**、与 master 逐字相同，
        只有 ELOOP 在它之前被分流走，所以行为上本来就是 verbatim，无输入可造出非 verbatim 消息。
      - **DEFER 2（Note，终审新增）**：另一条几何 —— workspace **内部**的 symlink，其目标穿过一个位于
        workspace **外部**的环 —— 是 refuse→refuse（master 报 `must be under workspace_root`，
        HEAD 报环路拒绝），无放行回归；但按 `error.filename` 归因会把一条 **workspace 外的绝对路径**
        原样写进消息。`_symlink_loop_refusal` 的 docstring 预声明的「不脱敏」只覆盖 `{path}`
        （恒为 config 派生、workspace 内），`error.filename` 是新的无界来源。影响极小
        （那是运维自己放的 symlink 的目标），且环路归因比它替掉的 containment 判定更可操作。
        已在 PR body 的修后几何表补一行披露。
      - **DEFER 1（P4）**：`via_midloop` 几何在 3.14 上 master ACCEPT、HEAD REFUSE 是 accept→refuse 翻转。
        它在 #1544 的**要求层**意图之内（「环不得被静默收编」，且 3.11 上 master 本来就抛），
        缺的只是**场景/表格层**的披露 —— 已在 PR body 的修后几何表补一行。
      - **对我 brief 的一处更正（fix pass deviation 1）**：我要求顺带补的「相对拼接半边无断言」是**假前提**——
        把 `_optional_config_path_relative_to` 的 `base` 换成 `Path.cwd()`，**既有用例已经红**
        （用例不 chdir，cwd 是仓库根、base 是 tmp_path）。未补，避免冗余。
      - **fix pass deviation 4（保留）**：`error.filename or path` 的 `or path` 半边是防御性未测表达式；
        实测每种几何 filename 都有值，保留只为避免打印 `at None`。
      - **fix pass deviation 7（记过）**：首个 mutant 改写误用了裸 `python3`（项目规则是 uv-only），
        后续全部改回 `uv run python`；无状态影响，还原经 sha256 核验。
- [ ] D.8 诚实记账：判别力只在 ≤3.12 臂；#1546 今天无活调用方，改它买的是家族账目结清
      而非当下崩溃修复；#1547 触发条件少见但影响面是共享底座。
