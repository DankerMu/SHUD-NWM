# Tasks — lockfile 身份守卫 + 探针姿态补测（#1609 + #1610）

风险包选择与未选理由见 `design.md`。本单**合批两条 issue**：A/B 段是 #1609（生产改动），
D 段是 #1610（纯证据面）。本稿已吸收 round-0 审的 2×P1 + 1×P2 + 3×P3。

## A. 实现（#1609）

- [x] A.1 `packages/common/state_manager.py` 的 safe_fs import 块里**只需补** `directory_identity_no_follow`
      —— `stat_no_follow` 已在 `:31` 导入（round-0 iteration-2 核实）。必须导入到**自身命名空间**
      （patch 点必须就是产线调用点，否则 B.2 的支 A 红证作废）。
- [x] A.2 新增私有判据函数（本文件内，不导出），实现 design 的两支：
      - 支 A：两侧 `index 文件父目录`身份相等 **且** 两侧 `provider_lock_path(...).name` 相等；
      - 支 B：**两侧 lockfile 都存在**时，`stat_no_follow` 的 `(st_dev, st_ino)` 相等。
      **`FileNotFoundError` 一律使该支「不适用」，两支皆然、两侧皆然**（不存在的路径没有 inode，
      不可能与已存在者互为别名）。`except FileNotFoundError` 必须**先于** `except OSError`。
      任一支成立即判「同一 lockfile」。
- [x] A.3 在 `merge_state_snapshot_index_copyback`（`:1867`）函数体**最顶端**调用该判据，
      早于 `:1894` 的 `with provider_destination_lock(source_path, ...)`。
- [x] A.4 **两个 reason 名（写死，实现不得自造）**，沿用既有 `state_snapshot_index_*` 命名族：
      - 拒绝：`state_snapshot_index_copyback_lock_identical`
      - 探针失败包裹：`state_snapshot_index_copyback_lock_identity_unavailable`
      **必须用 `_state_index_error(reason, field=..., evidence={...})`**（`state_manager.py:3141`）构造 ——
      它会设 `.reason` / `.field` / `.evidence` 且**不设 `.phase`**。
      **禁止**裸 `StateManagerError("...路径...")`：那样 `.reason` 为空，
      `run_tree_copyback.py:141` 拿到 `error_reason=None`，replay 会合成
      `merge_unexpected_exception:StateManagerError`。
- [x] A.5 **路径进 `evidence`，不进 message**（message 就是 reason）。裸路径塞进 message 会经 `str(error)`
      泄进 `RunTreeCopybackError.details["error"]` 与 replay stderr，绕过该模块的脱敏约定。
      **脱敏必须显式做**：`_state_index_error`（`:3141-3146`）只 `dict(evidence or {})`，
      **不会**自动调用 `_state_index_evidence_safe`（`:3090`）—— 实现要自己把 evidence 过一遍它。
- [x] A.6 **新 raise 点不得带 `phase`**（`_state_index_error` 天然不设，别手动加）。
      它在取锁之前，是 `run_tree_copyback.py:117-136` 那条已审计不变量的新成员，
      必须落 `OBJECT_STORE_COPYBACK_STATE_INDEX_FAILED`（fail-closed）桶。
- [x] A.7 **replay 侧分类必须同步修**（round-0 P1）：`scripts/scheduler_state_index_copyback_replay.py`
      按 **reason 白名单**分类（`:405` `error_reason in MERGE_PRE_COMMIT_REFUSAL_REASONS`），**不看 phase**；
      白名单自己的 docstring（`:183-190`）写明「a reason added to the merge later」默认归 commit-uncertain。
      故 **A.4 的两个 reason 都必须加进 `_PRE_COMMIT_INDEX_REASONS`（`:152`）**。
      不加 = 把一个「什么都没碰」的前置拒绝报成 exit 3 + `merge_commit_state: "uncertain"` + 跑
      `_verify_committed_destination` + 写 receipt。
- [x] A.8 探针的其它失败（非 `FileNotFoundError` 的 `OSError`/`SafeFilesystemError`）→
      用 A.4 的第二个 reason 包成 `_state_index_error`、fail-closed。
      **不得吞、不得「判不出就放行」**——放行等于把死锁留在原地。
- [x] A.9 `packages/common/provider_atomic.py` **一字不动**。
- [x] A.10 `merge_state_snapshot_index_copyback` / `_merge_state_snapshot_index_copyback_locked`
      **签名不变**；两个生产调用点的**调用形式**不改（replay 工具**只允许**改 A.7 那个白名单常量，
      不得改其分类逻辑、退出码或 receipt 结构）。
- [x] A.11 #1192 交付的**五处 root 级守卫一字不动**（`git diff` 核验）。

### 全 PR 改动 allowlist（替代原 D.3，可机械核验）

非 `tests/**` 与 `openspec/**` 的改动**只允许**出现在这三个文件：
`packages/common/state_manager.py`、`scripts/scheduler_state_index_copyback_replay.py`（仅白名单常量）、
`docs/runbooks/current-production-ops.md`（仅 A.12 那一行）。其余任何生产文件出现在 diff 里即为越界。

- [x] A.12 **同步 runbook**（round-0 iteration-2 P2）：`docs/runbooks/current-production-ops.md:1979`
      的 exit-code 解码表写着「allowlist 之外（含未来新增 reason）工具已归为 commit-uncertain，走 exit 3
      …**不会**出现在 exit 2 里」。本单新增两个 reason 后该括注**变成假的**——把两个新 reason 补进该行的
      allowlist 举例，并把括注改成「allowlist 之外的 reason 归 commit-uncertain」（去掉「含未来新增」的绝对化措辞）。

## B. 测试（#1609）

### B.1 真红证（支 B，真实 FS，免 root，可移植）

- [x] B.1(a) 两个真实不同目录各放一个 index 文件，用 `os.link` 把两者的 `.<name>.lock` **硬链到同一 inode**
      → 断言 `state_snapshot_index_copyback_lock_identical`、**两个 index 都未被修改**、**未取到任何 provider 锁**。
- [x] B.1(b) **umask/权限必须钉死（round-0 P2）**：两侧 lockfile 父目录都要 `umask 077` + `chmod 0o700`。
      否则 `provider_atomic.py:209-210` 的 `provider_lock_parent_unsafe`
      （`st_uid != geteuid()` 或 `S_IMODE & 0o022`）会在**取源锁时**先抛，mutant 就变成「因为别的原因红」。
      既有套件已有先例：`tests/test_run_tree_copyback.py:212` 显式 `os.umask(0o077)`。
      **round-1 P3 更正——「钉死」必须覆盖 fixture 构造，否则整条钉子是空的**：
      `ensure_directory_no_follow` 用裸 `os.mkdir`（`packages/common/safe_fs.py:68`）建 lock 父目录，
      模式即 `0o777 & ~umask`，所以判定完全由**环境 umask**决定：
      `022 → 0755`（`S_IMODE & 0o022 == 0`，闸不响，chmod 是 no-op）；`002 → 0775` / `000 → 0777`（闸响）；
      `077 → 0700`（闸不响）。而 `chmod` 与 `os.umask(0o077)` 原本都排在 `_CopybackRoots(...)` / `_write_run` /
      replay `Fixture(...)` **之后**——闸真要响时，是在**构造里**先响的，钉子还没执行。
      结论：原措辞把 mutant 证据的成立**归因错了**（真正兜底的是 oracle 环境的 ambient `umask 022`）。
      修法：`previous_umask = os.umask(0o077)` 上移到构造**之前**，且构造本身放进同一个 `try`
      （否则构造中途抛异常会把 077 泄漏给后续整个 pytest 进程），`finally` 沿用既有还原。
      三处均已修：`tests/test_state_manager.py:3670-3712`、`tests/test_run_tree_copyback.py:1334-1375`、
      `tests/test_scheduler_state_index_copyback_replay.py:1157-1176`（replay 侧新增专用 fixture
      `private_umask_fixture`，**不动**共享 `fixture_factory`——动它会顺带遮掉 #1513 的既有红，污染基线计数）。
      实测：`umask 002` 下三条硬链用例由「2 failed + 1 error（`provider_lock_parent_unsafe`）」转 `3 passed in 0.29s`；
      `umask 022` 下 `186 passed` 不变。三文件在 `umask 002` 下剩余 `39 failed + 27 errors` 属 **#1513** 既有面，
      本单未触碰（改前 `41 failed + 28 errors`，差值恰为这三条）。
- [x] B.1(c) **「不 hang」断言，这次有判别力**：`threading.Thread(..., daemon=True)` + `join(5.0)` + `pytest.fail`。
      **禁止裸调用** —— 与 #1192 不同，**修前这条是真的会永久挂**（round-0 已 `subprocess timeout=25` 实测证实：
      `same lock inode: True` / `abspath keys differ: True` / `TIMEOUT -> deadlock confirmed`）。
      裸调用会挂死整个 pytest 进程。`daemon=True` 不可省。
- [x] B.1(d) **判别力自证**：去掉支 B 的 mutant 下，B.1(a) 必须**被 join-timeout tripwire 判红**，
      而不是「红了就算」——必须断言红因是 tripwire（挂起），不是 `provider_lock_parent_unsafe` 之类的旁路。
      贴出 mutant 前后实测。

### B.2 支 A 红证（探针注入）

- [x] B.2(a) monkeypatch `state_manager.directory_identity_no_follow`，让两个**真实不同**的父目录报同一身份
      → 断言同一个 reason，且**在 lockfile 尚不存在**时同样成立。
- [x] B.2(b) 判别力自证：去掉支 A 的 mutant 下 B.2(a) 必红。

### B.3 分类不变量（两个调用方都要）

- [x] B.3(a) 经 `copyback_run_trees` 触发拒绝 → 断言
      `OBJECT_STORE_COPYBACK_STATE_INDEX_FAILED`，**不是** `..._COMMIT_UNCERTAIN`。
- [x] B.3(b) **经 replay 工具触发拒绝** → 断言 **exit 2 / `status: "refused"` / `reason: "merge_failed"`**，
      **未跑** `_verify_committed_destination`、**未写** `merge_commit_state: "uncertain"` 的 receipt。
      这条直接钉 A.7；没有它，白名单漏加就是假绿（现有套件只逐条抽查个别 reason，
      `tests/test_scheduler_state_index_copyback_replay.py:564/648/691`，**无**白名单覆盖度测试）。

### B.4 不回归

- [x] B.4(a) 两侧 lockfile 确实不同（不同父目录、非硬链）→ merge 行为逐字不变，既有用例全绿。
- [x] B.4(b) **bootstrap 用例（round-0 P1，必做）**：destination index 的父目录
      （`<copyback_root>/scheduler/state-index/`）**尚不存在** → 正常取锁、正常合并、正常创建。
      既有 `tests/test_run_tree_copyback.py:198`
      `test_copyback_run_trees_copies_extra_state_index_object` 正是这个形状，**必须保持绿**。
- [x] B.4(c) `tests/test_state_manager.py -k copyback` 既有断言**不得修改**。

## C. 验证（Evidence Floor）

- [x] C.1 `uv run pytest -q tests/test_state_manager.py tests/test_run_tree_copyback.py tests/test_scheduler_state_index_copyback_replay.py`
- [x] C.2 `uv run pytest -q tests/test_forcing_copyback_backfill.py tests/test_tile_publisher.py tests/test_safe_fs.py`（#1610 落点）
- [x] C.3 `uv run ruff check $(git ls-files '*.py')`
- [x] C.4 `openspec validate state-index-lockfile-identity-guard --strict --no-interactive`
- [x] C.5 B.1(d) / B.2(b) / D.2 三组 mutant 判别力实测，计数贴 PR body
- [ ] C.6 merge 后 node-27 全量 receipt，`umask 022`，独立 detached worktree，**不碰 `/home/nwm/NWM` 主树**。
      **判读口径：相对 master 基线「不新增红」**，不是「零红」—— master 已知三条红：
      `test_entropy_audit_script.py::…hard_gate…`、
      `test_scheduler_file_provider_refresh.py::test_provider_snapshot_rejects_replacement_between_metadata_and_read`、
      以及 #1613 那条顺序依赖。
- [x] C.7 **node-22 不跑任何东西**（#1192 的死锁探针禁令继续有效）。死锁红证只在本地/CI 的有界壳里跑。

## D. #1610 探针姿态补测（零生产改动）

- [x] D.1 逐条补 posture 测试，monkeypatch **各模块自身命名空间**里的
      `directory_identity_no_follow` 使其抛异常，断言该站点自己的 code：

      | 站点 | 期望 |
      |---|---|
      | replay `_root_identity`（`:765-777`） | `root_unavailable`（带 `field`） |
      | `run_tree_copyback._directory_identity`（`:207-220`） | `OBJECT_STORE_COPYBACK_ROOT_UNAVAILABLE`，**且消息按 field 区分操作数** |
      | `publisher._copyback_run_products`（`:743-753`） | `OBJECT_STORE_COPYBACK_FAILED` |
      | `publisher._copyback_qdown_products`（`:896-906`） | `OBJECT_STORE_COPYBACK_FAILED` |
      | backfill dry-run（`:439-448`） | `COPYBACK_ROOT_UNSAFE` |
      | backfill `_verify_object_store_root`（`:331-348`） | `OBJECT_STORE_ROOT_UNSAFE`，**details 指向 object-store root** |
      | backfill 宽松预检（`:401-408`） | **静默 `return`**，不 raise |

      apply 路径（`:185`）已有覆盖，**不重复造**。
- [x] D.2 **sentinel 复测**：把上述任一探针失败处理器换成 `raise AssertionError(...)` 必红；
      把探针移出其 try 块的变异必红。（#1610 的原始证据是这套 sentinel 下 `192 passed` 无一触发。）
- [x] D.3 D 段**不改任何生产姿态**（#1192 已逐点核实姿态表与设计一致）。
      机械核验落在上面那条「全 PR 改动 allowlist」上，不按段切分 diff
      （`tests/test_run_tree_copyback.py` 被 B.3 与 D.1 共用，按段归属不可判）。

## E. 诚实记账（必须进 PR body）

- [x] E.1 支 A 看不见 hardlink；支 B 只在两侧 lockfile 都已存在时可判。
      **「两个不同目录 + lockfile 尚不存在 + 未来才被硬链」无法预判**——物理上也无从判起。
- [x] E.2 `(st_dev, st_ino)` 仍只覆盖 same-superblock 别名（`nosharecache` 双挂载仍绕过），同 #1192。
- [x] E.3 **支 B 的红证是真的**（硬链 lockfile，免 root 可移植，round-0 已实测确认死锁），
      所以本单的「不 hang」断言**有判别力**——与 #1192 那条不同，那条已在 PR #1608 就地更正为通用「不返回」网。
      支 A 仍是探针注入（真实 bind mount 需 root，未实机验证）。
- [x] E.4 node-27 收据判读口径是「相对 master 基线不新增红」，master 已知三条红逐条列明。
- [x] E.5 **evidence 实际不外露**：两个调用方都只读 `["phase"]`
      （`run_tree_copyback.py:139`、`scheduler_file_provider_refresh.py:1094`），不外露 `.evidence`；
      操作员诊断实际只靠 reason 名，这是 reason 起成自解释名字的理由。
- [x] E.6 **round-0 推翻了我 design 的一句断言**：原写「replay 侧同理…符合既有形状」是错的 ——
      replay 按 reason 白名单分类、不看 phase，新 reason 默认掉进 commit-uncertain 尾。已改为 A.7 的显式白名单登记 + B.3(b) 的钉子。
- [x] E.7 **round-1 又推翻了我一条归因（P3，已修）**：PR body 原写「旁路已被预先钉死：两侧 lock 父目录
      `chmod 0o700` + `os.umask(0o077)`」——**钉子排在 fixture 构造之后，任何 ambient umask 下都不起作用**。
      真正让 mutant 证据成立的是 oracle 环境的 ambient `umask 022`（+ tripwire 本身）。已按 B.1(b) 上移修正；
      口径与实测见 B.1(b)。**结论不变，证据链更强**：verifier 在钉子被证明为 no-op 的 `umask 022` régime 下
      独立复现，红因 100% 是 tripwire（`hang regression` ×4 / `provider_lock_parent_unsafe` ×0，
      `2 failed in 10.28s` vs 洁净 `2 passed in 0.45s`），faulthandler 栈定位阻塞点为
      `packages/common/provider_atomic.py:221` 的 `fcntl.flock`。
- [x] E.8 **本单让 #1608 的两条 `_call_without_hanging` docstring 变成假的（P2，已修）**：
      `tests/test_run_tree_copyback.py` / `tests/test_scheduler_state_index_copyback_replay.py` 的 helper
      docstring 原写「它**不可能**复现 `fcntl.flock` 自死锁……别名是在探针缝位注入的……绝不会以挂起形式红」——
      那是 #1192 时期「所有调用方都走探针缝位」的事实。本单新增的两个调用方用 `os.link` 造**真硬链**
      （同一 inode），helper 在那里**真的会挂**。已把 docstring 按调用方形状拆开：探针缝位段按用例名限定，
      硬链段点名两条新用例、写明 5s join 是真 tripwire 不得删除，并记明 `pyproject.toml` 无 `pytest-timeout`
      / 无 `addopts`——这个 join 是唯一的界，删了本地 pytest 会话会无限挂死、CI 烧满
      `.github/workflows/ci.yml:226` 的 `timeout-minutes: 35`。
- [x] E.9 **out-of-scope，只报不修**：三个测试文件在 `umask 002` 下仍有 `39 failed + 27 errors`，
      根因是 `safe_fs.ensure_directory_no_follow` 裸 `os.mkdir` 建 lock 父目录（`packages/common/safe_fs.py:68`）
      撞 `provider_lock_parent_unsafe` 闸——即既有 **#1513**（OPEN），不另开单，本单未触碰。
- [x] E.10 **Phase-7 终审 CLEAN，但揪出两条覆盖缺口（存活 mutant），已在本 PR 内补齐**（纯增测，生产零改动）：
      - **缺口 1（重要）**：`state_manager.py:1982` 的 `source_identity is not None` 无人钉——删掉它整套 190 前的
        186 条**全绿**。而它的失效形状是**误拒合法 merge**：两侧 lockfile 都不存在时两个探针都返回 `None`，
        `None == None` 成立 ⇒ 判成「同一 lockfile」拒绝。可达形状：source object store 被不带 dotfile 的
        rsync/restore 出来（没有 `.index-last.json.lock`），往新 copyback root 合并。既有支 A 用例覆盖不到——
        那里支 A 先抛，支 B 根本走不到。补 `test_state_index_copyback_merges_when_neither_lockfile_exists_yet`
        （`tests/test_state_manager.py:3907`）：父目录身份**实测不等**（支 A 证明不适用）+ 两侧 lockfile 均已 unlink，
        断言 merge **照常成功**（`merged_entry_count == 2`、state_ids、`requested_locks == [source, destination]`）。
        mutant 下必红，红因就是那条误拒。
      - **缺口 2**：支 B 探针失败路径（`_copyback_lockfile_identity` → `stat_no_follow`）、destination 侧
        `field="copyback_destination"` 操作数串、以及第二个 reason 的白名单成员资格，三者均无断言。已补三条：
        `tests/test_state_manager.py:3815` / `:3850` / `tests/test_scheduler_state_index_copyback_replay.py:1225`。
        白名单那条断在公开并集 `MERGE_PRE_COMMIT_REFUSAL_REASONS` 上——即 `:407` 分类器真正读的那个集合。
      四条 mutant 逐条判别力自证，红因均为目标断言，`git checkout --` 后 `git diff` 空。
      三文件 `umask 022` 由 `186 passed` 增至 **`190 passed`**，`150 passed` 不变，ruff 清洁。
