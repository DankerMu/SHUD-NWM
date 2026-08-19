# Tasks — copyback root inode 身份守卫（#1192）

风险包选择与未选理由见 `design.md` 的「风险三角」表，此处不复述。
本稿已吸收 round-0 fixture 审的 2×P1 + 2×P2 + 4×P3。

## A. 实现

- [ ] A.1 在 `packages/common/safe_fs.py` 新增 `directory_identity_no_follow(path: Path) -> tuple[int, int]`：
      复用 `_open_directory_no_follow`（`:654`）拿 fd → `os.fstat(fd)` → `finally: os.close(fd)` → 返回
      `(info.st_dev, info.st_ino)`。**禁止**新增任何 follow 型 `os.stat(path)`；**禁止**返回 `os.stat_result`。
      异常按该模块既有风格向上抛 `OSError` / `SafeFilesystemError`，不在 helper 内吞。
      docstring 必须写明：**只接受已 resolve 的路径**，任意 symlink 分量（含末段）都会被 no-follow walk 拒绝。
- [ ] A.2 `scripts/scheduler_state_index_copyback_replay.py:766` `_refuse_root_conflicts`：
      判等改为身份相等 → **原样**抛 `roots_identical`（payload 字段不变）。
      身份探针失败 → 复用既有 `root_unavailable`（带出错的 `field`），**不新增 error code**。
      `_paths_overlap`（`:779`）与其后的 `roots_overlap` 分支**一字不动**；identity 先于 overlap 的顺序不变。
- [ ] A.3 `services/orchestrator/run_tree_copyback.py:46`：`object_root == target_root` 改为身份相等 →
      走**同一个** skip 分支（含其中 `_validate_run_tree` 循环与返回 dict，一字不动）。
      身份探针失败 → 复用既有 `OBJECT_STORE_COPYBACK_ROOT_UNAVAILABLE`。
      `:44` 的 `ensure_directory_no_follow(...).resolve()` 创建语义不变（先建后 stat）。
- [ ] A.4 `services/tile_publisher/publisher.py:737`（`_copyback_run_products`）与 `:875`（`_copyback_qdown_products`）：
      `copyback_root == object_store_root` 改为身份相等 → 走**同一个** skip 分支（返回 dict 一字不动）。
      探针失败 → 复用**各方法内**既有的
      `PublishError("OBJECT_STORE_COPYBACK_FAILED", "Object-store staging root is unsafe for copyback.", ...)`
      （`_copyback_run_products` 的 `:718-728`、`_copyback_qdown_products` 的 `:845-855`）。
      注：`:731` 与 `:858` 是**两个方法里对同一个 `_prepare_copyback_root` 的两次独立调用**，不是同一次；
      但两处的 `copyback_root`（经 `:1104` 的 `.resolve()`）与 `object_store_root`（经各方法 `:719`/`:845` 区的
      `verify_directory_no_follow(...).resolve()`）都已 resolve，A.7 的硬约束在 publisher 侧成立。
      `_prepare_copyback_root` 里的两处 overlap（`:1095`/`:1116`）**一字不动**，其「overlap 在 same-root 之前」的
      既有顺序**保持例外，不得为统一顺序而调整**（理由见 design must-preserve #2）。
- [ ] A.5 `services/tile_publisher/forcing_copyback_backfill.py:397` `_reject_same_copyback_root` **改签名**，
      接收已算好的身份：`(*, copyback_root, object_store_root, copyback_identity, object_store_identity)`，
      身份相等 → **原样**抛 `COPYBACK_ROOT_SAME_AS_OBJECT_STORE_ROOT`（`details.reason` 不变）。
      **`object_store_identity` 的出处（单点）**：在 `_verify_object_store_root`（`:309`，今天唯一的 object-store
      校验点，严格抛 `OBJECT_STORE_ROOT_UNSAFE`）内**一次算出**，改为随 `Path` 一并返回，由 `run_backfill:130` 持有并
      向下透传到 `:176` / `_validate_copyback_root_boundary:327` / `_dry_run_copyback_root:369`。
      探针失败沿用既有 `OBJECT_STORE_ROOT_UNSAFE`。
      **禁止**把 object-store 侧探针塞进 `_dry_run_copyback_root` 的 try（`:373-378`）或
      `_reject_existing_same_copyback_root` 的 try（`:360-365`）—— 那会把 object-store root 的故障误报成
      `COPYBACK_ROOT_UNSAFE` 且 `details.copyback_root` 指向错的 root，正是本单要防的误诊。
      四个调用点**只**负责各自 copyback 侧的探针，姿态逐点照 design 表实现：
      - `:176`（apply）→ 探针必须 try 包住，失败 raise `COPYBACK_ROOT_UNSAFE`；**不得**让 `OSError` 裸逃出
        `run_backfill` 被 CLI `:221` 兜成通用 `BACKFILL_FAILED`（`:224`）；
      - `:359`（raw 串相等短路）→ **不探针**，两侧传同一个 `object_store_identity`；
      - `:366` → 探针**移进** `:360-365` 那个既有 try（或自带 try → `return`），保持宽松姿态；
      - `:384`（dry-run）→ 探针失败 raise 既有 `COPYBACK_ROOT_UNSAFE`。
- [ ] A.6 四个消费模块（replay / run_tree_copyback / publisher / forcing_copyback_backfill）**必须**以
      `from packages.common.safe_fs import directory_identity_no_follow` 导入到自身命名空间。
      这是 B 段红证的 patch 点约束：patch 点必须就是产线调用点，否则整层证据作废。
- [ ] A.7 探针**只对已 `.resolve()` 的路径**调用（五处今天都已 resolve；不得改成探 raw 路径）。
- [ ] A.8 `packages/common/state_manager.py`、`packages/common/provider_atomic.py` **一字不动**
      （不采纳 `blocking=False` 备选；lockfile 级身份判定另开 follow-up）。
- [ ] A.9 把 `tests/test_safe_fs.py` 挂进 `scripts/select_ci_tests.py` 对 `packages/common/safe_fs.py` 的路由
      （今天 safe_fs-only 改动只选到 journal 系列，选不到 helper 自有套件，正是 #1487 要治的漂移）；
      同步更新 `tests/test_select_ci_tests.py` 的期望集。
      **必须新增而非替换**：改后 safe_fs-only 改动的选择结果必须是「今日 journal 闭包 ∪ `tests/test_safe_fs.py`」。
      `packages/common/safe_fs.py` 今天只出现在 `FILE_JOURNAL_READ_STATE_PATH_PATTERNS`（`:187`）这个 gate 列表里、
      没有专属 `PathTestRule`；新加一条带 `stop_on_match` 的专属规则会把 journal 闭包整组挤掉
      （`:141-143` 有在案警告「selection must not move」）。若结构上不适合挂载，**写明理由**，不得默默跳过。

## B. 测试（三层证据链，缺一层即假绿）

### B.1 L1 — helper 是身份语义而非字符串语义（真实 FS，可移植，新建 `tests/test_safe_fs.py`）

- [ ] B.1(a) **同一目录、不同输入字符串** → 返回**相同** tuple。用 round-0 已实测可行的两种构造之一或全部：
      monkeypatch `HOME` 后的 `~/real` vs 绝对路径（`_expand_path` 会 `expanduser()`）；`os.chdir` 后的相对路径 vs 绝对路径。
      **不得**用 symlink 构造（no-follow walk 直接拒绝末段 symlink，round-0 已实测）。
- [ ] B.1(a2) **`os.rename` 前后身份不变**：把目录改名到另一个 realpath，断言身份 tuple 不变。
      **这一条是 L1 判别力的核心** —— 只有它能杀掉纯字符串实现（`return (0, hash(str(_expand_path(path))))`
      在 B.1(a) 下照样全绿）。round-0 实测该构造成立：`same identity across two genuinely different realpaths: True`。
- [ ] B.1(a3) **oracle 相等**：`directory_identity_no_follow(p) == (os.stat(p).st_dev, os.stat(p).st_ino)`。
      钉死「返回的确实是内核给的那对数」，杀掉任何自造数值的实现。
- [ ] B.1(b) 两个真实不同目录 → 返回**不同** tuple。
- [ ] B.1(c) 目标不存在 / 路径分量是 symlink → 抛出异常。**只断言异常类型，不断言文案**：
      同一 symlink 分量 macOS 得 `Path component is not a directory:`（ENOTDIR，`safe_fs.py:627`）、
      Linux 得 `Path component must not be a symlink:`（ELOOP，`:630`），断文案会跨平台红。
- [ ] B.1(d) 用例注释写明 L1 的**诚实边界**：L1 证的是「helper 消费 inode 身份」，**不**证「两条**同时并存**的不同
      realpath 会给出同一身份」—— 那个形状（bind mount）无可移植免 root 构造；B.1(a2) 的 rename 对是**时序先后**的，不是并存的。

### B.2 L2 — 守卫接线，注入别名身份

- [ ] B.2(a) replay：monkeypatch `scheduler_state_index_copyback_replay.directory_identity_no_follow`，
      让两个**真实不同**的 root 返回相同身份 → 断言结构化 `roots_identical` + 非零退出 +
      **destination index 文件不存在**（不写 index、不留半成品 receipt）。
      搭台复用 `tests/test_scheduler_state_index_copyback_replay.py::test_replay_refuses_identical_and_overlapping_roots`（`:739`）。
- [ ] B.2(b) `copyback_run_trees`：同样注入 → 断言返回既有 skip dict（`reason == "copyback_root_matches_object_store_root"`），
      **且 `merge_state_snapshot_index_copyback` 从未被调用**（打桩计数或 patch 断言）。
      **该断言必须传入含 `STATE_INDEX_OBJECT_KEY` 的 `extra_object_keys`**，否则 merge 本就不会被调用
      （`services/orchestrator/run_tree_copyback.py:94-97`），断言自动成立、零判别力。
      注意 `tests/test_run_tree_copyback.py` 现有 20 个用例**没有**任何一条覆盖 `copyback_root_matches_object_store_root`
      （grep 为空）—— 这是全新地基，不是复用既有搭台。
- [ ] B.2(c) publisher `_copyback_run_products` 与 `_copyback_qdown_products` 各一条：同样注入 →
      断言返回既有 skip dict（`reason == "copyback_root_matches_object_store_root"`）且未复制任何对象。
- [ ] B.2(d) backfill：同样注入 → 断言 `COPYBACK_ROOT_SAME_AS_OBJECT_STORE_ROOT` +
      `details.reason == "copyback_root_matches_object_store_root"` + 未复制任何对象。
      另加一条：`:176` apply 路径上探针失败 → `COPYBACK_ROOT_UNSAFE`（**不是** `BACKFILL_FAILED`）。
- [ ] B.2(e) **判别力自证（必做，不是可选）**：B.2(a)~(d) 中**断言身份判等的那些别名注入用例**必须在
      「把判等改回 resolve 字符串比较」的 mutant 下**全红**。
      **posture-failure 类用例明确排除在这道门之外**（B.2(d) 那条 `:176` 探针失败 → `COPYBACK_ROOT_UNSAFE`
      与判等谓词无关，mutant 下必然仍绿；把它算进「全红」会让这道门按字面不可满足）。
      在 PR body 贴出 mutant 前后的实测 failed 计数。修前全绿的守门测试等于装饰品（#1491 刚吃过两次这个亏）。

### B.3 L2 附加 — 「不 hang」断言

- [ ] B.3 B.2(a) 与 B.2(b) 各自把被测调用放进 **`threading.Thread(..., daemon=True)`**，`thread.join(5.0)`；
      `if thread.is_alive(): pytest.fail(...)`。
      **`daemon=True` 不可省** —— round-0 实测非 daemon 线程会让解释器退出时再等 30.05s（sleep 全长），
      而真实回归是 `fcntl.flock` 永久阻塞（`provider_atomic.py:219`），非 daemon 即永久挂死，与本条想避免的后果相同。
      用例注释注明：挂住的 daemon 线程会在本次 session 剩余时间继续持有 lockfile fd。
      **禁止裸调用。**

### B.4 L3 — 不回归，真实 FS

- [ ] B.4(a) symlink 别名 root 仍被拒 —— 走的是 `.resolve()` 折叠这条路，**不是** helper（helper 不接受末段 symlink）。
      用例注释写明这一点，别让后人以为 helper 能吃 symlink。
- [ ] B.4(b) 两个真实不同 root 仍正常放行、正常复制。
- [ ] B.4(c) 既有 `roots_identical` / `roots_overlap` 用例（`:739`，断言在 `:778-780`）继续绿，**且不得修改其断言**。
- [ ] B.4(d) 别名造成的**父子 overlap 仍检测不到** —— 这是声明的取舍，不写「期望被拦」的假测试；
      若加用例，只能是记录现状的 xfail/注释，不得伪装成能力。

### B.5 红证禁令（写死，防止后续有人「优化」进来）

- [ ] B.5 **禁止**在交付套件里出现：目录 hardlink（POSIX 不支持）、symlink 构造的 helper 身份对（no-follow walk 直接拒）、
      macOS 大小写折叠别名（Linux 上不成立，不可移植）、真实 `mount --bind`（需 root）。
      任何以这四者构造的「红证」都不接受。

## C. 验证（Evidence Floor）

- [ ] C.1 `uv run pytest -q tests/test_safe_fs.py tests/test_scheduler_state_index_copyback_replay.py tests/test_run_tree_copyback.py tests/test_forcing_copyback_backfill.py tests/test_tile_publisher.py`
- [ ] C.2 `uv run pytest -q tests/test_state_manager.py -k copyback`
- [ ] C.3 `uv run pytest -q tests/test_select_ci_tests.py`（A.9 改了 selector）
- [ ] C.4 `uv run ruff check .`
- [ ] C.5 `openspec validate copyback-root-inode-identity-guard --strict --no-interactive`
- [ ] C.6 B.2(e) 的 mutant 判别力实测（修前全绿 → 修后全红），计数贴 PR body。
- [ ] C.7 merge 后 node-27 全量 receipt，**`umask 022`**（默认 umask 会有 ~80 条 #1513 预置红淹掉真实回归），
      在独立 detached worktree 里跑，**不得触碰 `/home/nwm/NWM` 主树**（它被另一 session 的分支占用）。
- [ ] C.8 node-22 **只读**复核（唯一允许的远端动作）：
      `grep -E "ghdc|nwm" /proc/mounts` + `grep -hE "OBJECT_STORE_ROOT|COPYBACK_ROOT" /scratch/frd_muziyao/NWM/infra/env/compute.env`。
      **禁止再跑死锁探针**（issue 原文禁令，逐字继承）：不得在 22 上触发任何 copyback / merge / 锁获取。

## D. 诚实记账（必须进 PR body）

- [ ] D.1 `(st_dev, st_ino)` 只覆盖 **same-superblock** 别名。bind mount 天然同 superblock ⇒ 必拦；
      NFS 默认 `sharecache` 双挂载共享 superblock ⇒ 拦；显式 `nosharecache` 双挂载 fileid 相同但 `st_dev` 不同 ⇒ **仍绕过**。
      规格与 PR 一律只声称 same-superblock。
- [ ] D.2 真实 bind mount **未经实机验证**（需 root，CI/本地均不可构造）；L2 证明的是「守卫消费身份而非字符串」，
      不证明内核在真实 bind mount 上给出同 inode。
- [ ] D.3 别名造成的父子 overlap **仍检测不到**（overlap 按 issue 要求保持字符串判定）。
- [ ] D.4 issue 验收字面写的「hardlink 或同 inode 构造」对**目录**不可满足，已用 L1+L2+L3 分层证据链替代 —— 显式记为偏离。
- [ ] D.5 **范围扩张**：issue 点名三处，实际同形守卫五处（`publisher.py:737`/`:875` 由 round-0 审 grep 出），
      本单一并对齐 —— 显式记为偏离（理由：同一子系统，backfill 的 apply 路径本就调用 `publisher._prepare_copyback_root`）。
- [ ] D.6 **新增读权限要求**：`run_tree_copyback` 侧对 `object_root` 的每级祖先从「仅需 `+x`」变为「需可读」
      （`O_RDONLY|O_DIRECTORY`）。`0711` 祖先下今天正常的 copyback 会变成 `OBJECT_STORE_COPYBACK_ROOT_UNAVAILABLE`。
      「既有真不同 root 输出不变」这句必须限定为「祖先目录可读时不变」。
- [ ] D.7 **已知残余，另开 follow-up**：守卫只在 root 级比身份，root 之下的别名仍能在 **lockfile 级**自死锁
      （`state_manager.py:1894`/`:1954`）。本单 must-preserve 冻结 state_manager，故报告不修。
