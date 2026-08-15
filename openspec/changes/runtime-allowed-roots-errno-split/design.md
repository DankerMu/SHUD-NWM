# Design: runtime-allowed-roots-errno-split (#1348)

Fixture level: expanded. Repair intensity: high (fail-open→fail-closed 契约面 +
证据链一致性 + 跨版本语义)。Project profile: NHMS.

## Context (verified anchors, master 2026-08-15, post PR #1349)

- Site 1: `services/orchestrator/scheduler_runtime_roots.py:448-463`
  `_scheduler_allowed_roots` — `resolve(strict=False)` + `except (OSError,
  RuntimeError)`；db-free 走词法回退，数据库态 `:457` 裸 `raise`。
  消费方：`:21`/`:79`（两条 preflight 臂）→
  `_scheduler_allowed_roots_policy_check`（`:425-445`，`not allowed_roots` 才出
  MISSING blocker）+ `_scheduler_root_check`（require_approved_root 容器判
  定）；`:168`/`:184-188`（payload/not_required 的 `allowed_roots` 证据字段）。
- Site 2: `services/orchestrator/scheduler_config.py:1058-1071`
  `_db_free_allowed_roots` — 同病，db-free 专用，消费方
  `_db_free_path_check`（`:738-762` 一带）。
- Site 3: `services/orchestrator/retry.py:1529-1535` — `except OSError` 接不住
  ≤3.12 的 RuntimeError、3.13+ 不抛，`db_free_allowed_root_unresolvable` 两端死码。
- 范式来源：`scheduler_preflight.py:516-560`（PR #1346）＋
  `scheduler_runtime_roots.py:500-523` `_optional_config_path`（PR #1349，config
  层"任何 OSError → 非 strict realpath 下放、分类归 preflight"）。
- Blocker 家族：`_scheduler_root_blocker`（`:375-386`，code =
  `SCHEDULER_ROOT_<FIELD>_<REASON>`）；errno→reason 映射
  `_scheduler_root_os_error_reason`（`:388-394`：ELOOP/ENOTDIR→UNSAFE_PATH、
  EACCES/EPERM→NOT_WRITABLE、其余→UNAVAILABLE）。
- 掩码纪律：`evidence_safe_paths = db_free_required or repair_missing_forcing`
  （`:17-20`/`:76-79`）；既有 root blocker 传 `evidence_path or str(path)`。
- 依赖状态：#1347 已修（PR #1349 merged），站点 1 的 ≤3.12 数据库态臂现实可达。

## Decisions

### D1 — 判据与家族逐字同范式，两种 `Path.resolve()` 均禁用

唯一 strict 解析形态：`os.path.realpath(expanded, strict=True)`；异常面只接
`OSError`，errno 分流。非 strict `Path.resolve()` 在 3.13+ 对环不抛（判据死亡），
strict `Path.resolve()` 在 ≤3.12 抛无 errno 的 RuntimeError（无法分流）——与
#1344/#1346/#1349 的 D1 一致，不再重复论证。改造后三站点文件内不得再出现任何
形式的 `Path.resolve()` 用于 allowed-roots 规范化（`_config_path_preserve_*`
等 parent 级用法属 D5 评估面，不在此禁令内）。

### D2 — 站点 1 返回形状：新增配对函数，保留旧签名做纯读取面

`_scheduler_allowed_roots(config)` 的 4 个调用点分两类：两条 preflight 臂需要
blocker；payload/not_required 只要 roots 列表。裁定：

- 新增 `_scheduler_allowed_roots_and_blockers(config) ->
  (tuple[Path, ...], list[dict])`，携带全部裁决逻辑。
- `_scheduler_allowed_roots(config)` 保留签名，实现为
  `_scheduler_allowed_roots_and_blockers(config)[0]` —— facade 再导出与既有测
  试的符号面零破坏。
- 两条 preflight 臂（`:21`/`:79`）改调配对函数，将 unsafe blockers `extend`
  进各自 blocker 列表（在 policy check 之前产出，顺序：unsafe blockers 先于
  policy MISSING blocker，与"根为何被丢"→"丢完后没根了"的因果一致）。
- payload（`:184-188`）与 not_required（`:168`）继续调旧签名——它们展示的
  `allowed_roots` 与 preflight 臂消费的是同一裁决结果（同一函数产物），证据面
  天然一致。

每个 preflight 臂各自调用一次配对函数（现状已是每臂独立调 `_scheduler_
allowed_roots`，无新增调用成本量级；payload 内的第三次调用维持现状——KISS，
不引入跨函数缓存）。

Facade 注册（fixture-review 建议 4 的裁定，round-2 修正论据）：本文件兄弟函
数的既有惯例是经 `_scheduler.*` forwarder 调用（`:21`/`:79` 即如此调旧符
号），且 `docs/governance/SCHEDULER_COMPATIBILITY_INVENTORY.md:127/:156` 明文
要求新 runtime-root helper 经 `scheduler.py` 暴露时同步登记。裁定：**遵循惯
例**——新配对函数登记进 `scheduler_candidate_runtime.py` 的 FORWARDER_NAMES/
赋值区/EXPORTS 名单，preflight 臂经
`_scheduler._scheduler_allowed_roots_and_blockers(config)` 调用，并补
inventory 一行。理由是惯例一致性 + inventory 治理义务本身（不是 monkeypatch
面——grep 证实无测试 patch 旧符号，且臂改调新符号后 patch 旧符号本就触不到
臂，无论是否注册 facade）。

### D3 — 站点 1 三臂裁决

对每个配置根 `expanded = Path(value).expanduser()`：

1. strict realpath 成功 → 纳入（去重保序，现状语义）。
2. `OSError.errno == ENOENT` → `Path(os.path.realpath(expanded))` 非 strict 回
   退后纳入——保留"根尚未创建/NFS 未挂载"的历史 admitted 语义，两种运行态一
   致，无 blocker（与 #1346 ENOENT 臂逐字对齐；非 strict realpath 在 3.11-3.14
   均不抛，`<missing>/../<loop>` 组合由它吸收，见 #1332 round-1）。
3. 其余 errno：
   - db-free（`db_free_required=True`）→ #831 词法回退臂逐字保留（expanduser +
     绝对化），无 blocker。
   - 数据库态 → 根**剔除** + `_scheduler_root_blocker("allowed_roots",
     _scheduler_root_os_error_reason(error), <path>)`；path 按掩码纪律
     （evidence_safe_paths → `"[local-path]"`，否则 `str(expanded)`）。
     裸 `raise`（`:457`）删除。

blocker code 由此落 `SCHEDULER_ROOT_ALLOWED_ROOTS_UNSAFE_PATH` /
`_NOT_WRITABLE` / `_UNAVAILABLE` —— 与 issue 验收"沿用/对齐
SLURM_PREFLIGHT_ALLOWED_STORAGE_ROOTS_UNSAFE_PATH 家族"的对齐方式是**同判据、
同 errno 分流、层内原生命名**（跨层复用 SLURM_PREFLIGHT_* 前缀反而伪装成
slurm preflight 的产物，污染证据面归属）。

剔除后全部根丢失时：roots 为空 → 既有 policy check 照常追加 MISSING blocker
（两个 blocker 并存，因果链完整）；`enforce_approved_roots` keyed off policy
blocker 的现状逻辑不变——status 已 blocked，不存在 fail-open 窗口。

### D6 — `require_runtime_roots=False`（db-backed 默认态）的显式裁定（fixture-review round 1）

两条 preflight 臂在 `not config.require_runtime_roots` 时 early-exit 到
`_scheduler_root_preflight_not_required`（`:14-15`/`:72-73` → `:162-169`），该
payload **没有 blocker 通道**，只展示 `allowed_roots` 列表——今天这条路径在
≤3.12 上正是裸 RuntimeError 的逃逸面（#1346 留下的 B8 tripwire
`tests/test_production_scheduler.py:32033-32065` 钉住的就是它）。裁定：

- not_required 面**共享同一裁决产物**（`_scheduler_allowed_roots` = 配对函数
  `[0]`）：不可解析根被剔除出展示列表，**无 blocker**（通道不存在，且
  **就 `_scheduler_allowed_roots` 的消费面而言**该运行态不做 runtime-root 容
  器判定——`:21`/`:79` 被 early-exit 跳过，仅 `:168` 展示面运行）。**注意**：
  `require_runtime_roots=False` 不等于全局无 containment 判定——数据库态
  repair 运行的 `db_free_runtime_preflight` lane 独立于该开关执行，由修订后
  的 D4（站点 2 数据库态臂 drop + blocker）负责封口。**永不以未捕获异常逃逸**。
- 该配置下 blocker 责任面仍在 slurm storage preflight（`_slurm_preflight` 独
  立于 `require_runtime_roots` 运行，#1346 已修）：运维仍然收到
  `SLURM_PREFLIGHT_ALLOWED_STORAGE_ROOTS_UNSAFE_PATH`。**前提限定（round-1
  EC-2 修正）**：此兜底只在 `slurm_execution_enabled=True` 的配置成立；两开关
  同时关闭（均默认 False 的 db-backed dev/dry-run 形态，所有已文档化部署均开
  slurm）下无任何平面记录该处置——该态没有 containment 消费者、payload 在
  status != not_required 之外不进 evidence，属接受的残余而非契约违反。证据一致性在该态的形
  式：not_required payload 的 `allowed_roots` 与 slurm 平面的有效根集合同源
  同值，不再有"一边丢根一边展示环根"的矛盾。
- **B8 tripwire 必须翻转**：其 docstring 明文 "When #1348 lands this test
  MUST go red; flipping it to the fixed structured assertion is part of that
  change"。翻转后的断言：`_scheduler_lock_evidence_root_preflight(config)`
  不抛、status=not_required、`allowed_roots` 不含环根；`skipif(>=3.13)` 移除
  （修后行为全版本一致）。这是本 PR 的**必做交接**，不是回归。

### D4 — 站点 2 / 站点 3 裁决（fixture-review round 2 重写站点 2）

- 站点 2 `_db_free_allowed_roots`：**并非 db-free 专用**（round-2 复审纠正的
  错误前提）——`db_free_runtime_preflight` 在数据库态 `repair_missing_forcing`
  运行上同样执行（`scheduler_config.py:687` `repair_authority_required` 跳过
  not_required 早退；`:738` 调用在 `scheduler_db_free_required` guard 之外；
  `:1141` 对 copyback/raw-manifest root 做真实 containment 判定并产
  `db_free_required_path_outside_boundary`）。裁定：
  - 改为配对函数 `_db_free_allowed_roots_and_blockers(config) ->
    (tuple[Path, ...], list[dict])`；旧名保留为 `[0]` 读取面（当前唯一调用点
    `:738` 改为解包并 `blockers.extend`）。该符号 module-private 且无 facade
    再导出（grep 核实仅 def + `:738` 两处），无 inventory 义务。
  - ENOENT → 非 strict realpath 纳入（两模式一致，无 blocker）。
  - 非 ENOENT + `scheduler_db_free_required=True` → #831 词法臂逐字保留，无
    blocker（AC 4）。
  - 非 ENOENT + `scheduler_db_free_required=False`（数据库态 repair-authority
    lane）→ 根**剔除** + 本层既有 blocker 家族
    `_db_free_blocker("db_free_allowed_root_unsafe",
    "NHMS_SCHEDULER_ALLOWED_ROOTS", <reason>, path=...)`（reason 沿用
    `_scheduler_root_os_error_reason` 词汇小写化，path 传法从本函数既有
    blocker 惯例）——phantom containment base 在该 lane 同样被消灭，证据面
    与 slurm preflight 一致。"config 层不裁决"（#1349 裁定）不适用于此：
    `db_free_runtime_preflight` 本身就是 preflight，有 blocker 通道。
  3.13+ 上 db-free 两臂重新可区分（环根走词法臂而非"解析成功"）。
- 站点 3 retry.py `:1529-1535`：`os.path.realpath(text_expanded, strict=True)`
  + `except OSError`：ENOENT → 非 strict realpath 纳入；非 ENOENT →
  `db_free_allowed_root_unresolvable` rejection（既有 code，死码复活）。该
  lane 在 db-free selector 语境里 rejection 即结构化产物，无需 blocker。

例外记录（fixture-review 建议 5，防 cross-review 重开）：站点 2 与站点 3 同为
db-free 但对非 ENOENT 根的裁决不同——站点 2 词法容忍纳入（#831 语义、AC 4 强
制保留），站点 3 rejection 剔除（AC 5 强制复活死码；roots 全空时
`retry.py:1599-1603` 级联为 `db_free_allowed_roots_missing`，fail-closed）。
两者输入源不同（config env vs retry manifest 的 selector 面），issue 边界的
"三者一致裁决"指的是与 #1346 范式的**判据一致**（strict realpath + errno 分
流 + ENOENT 豁免），不是出口形态一致；出口形态由各 lane 的既有契约（词法臂
vs rejection lane）分别决定。

### D5 — 顺带评估面（issue 授权 evaluate-only）：全部 DEFER，理由记录

- `_db_free_path_identity`（scheduler_config.py:1074-1081）：非 strict resolve
  失败→返回未解析 path，消费方是 topology identity **比较**（`:764-797`）——
  两边同函数产出，比较自洽；环路 path 在 3.13+ 被 resolve 原样返回不影响相等
  性判断的自反性。无 fail-open 后果升级，DEFER（与站点级修复非同病害等级）。
- `_db_free_path_check` 内部的 path 级 `resolve(strict=False)`
  （scheduler_config.py:1131 一带，值域是被检查的 path 而非 root）：≤3.12 上
  raise → `db_free_required_path_unsafe` fail-closed；3.13+ 环路 path 原样返
  回后仍过 containment 判定——root 基准已被本 PR 修好，残余是 path 级词法放
  行，与下条同类。DEFER，并入同一 follow-up issue 路由。
- `_db_free_selector_path_rejection`（retry.py:1555-1558）：path 级，同病
  （`except OSError` 接不住 ≤3.12 RuntimeError；3.13+ 不抛）。但其下一行
  containment 判定以**本 PR 修好的 allowed_roots** 为基准：环路 path resolve
  原样返回后仍要过 `_path_is_relative_to(resolved, root)`，phantom base 已被
  上游消灭，残余后果收窄为"环路 selector path 被词法 containment 放行/拒绝"
  ——fail-open 面显著小于根级。DEFER 并路由 issue（合并前经 issue-scribe）。
- parent 级 `resolve()` 三处（`_confined_path` `:491`、
  `_config_path_preserve_final_component` `:530`、
  `_config_path_relative_to_preserve_final` `:537`——行号为 post-#1349 实测，
  fixture-review 更正了 pre-#1349 的 `:512`/`:519` 残留编号）：非 strict、对
  parent 而非 root，语义是"尽力规范化展示路径"，#1332 已裁定 scope out 一
  次。维持 DEFER，不重开。

## Invariant Matrix

Governing invariant：一条配置根要么以内核可走通的规范形态进入 approved
containment base 集合，要么（ENOENT）以历史 admitted 语义进入，要么被结构化
证据（blocker / rejection / 词法臂）显式处置——任何运行态、任何受支持 CPython
版本上，环路/不可解析根都不得静默成为 containment 基准，也不得以未捕获异常
逃逸；slurm preflight 与 runtime-root preflight 两个证据面对同一输入给出一致
裁决。

Source-of-truth identity/contract：`_scheduler_allowed_roots_and_blockers` 的
`(roots, blockers)`；errno→reason 映射 `_scheduler_root_os_error_reason`；
blocker code `SCHEDULER_ROOT_ALLOWED_ROOTS_<REASON>`。

Surfaces:
- Producers: 配置构造层 `_optional_config_path`（#1349，不动——任何 OSError
  下放非 strict 规范形，本 PR 的 strict 判据在其产物上重新裁决）。
- Validators/preflight: 站点 1 + 两条 preflight 臂（改）；
  `_preflight_allowed_roots`（不动，#1346）；policy check `:425-445`（不动，
  语义由输入变化间接增强）。
- Storage/cache/query: 无。
- Public routes/entrypoints: preflight payload `allowed_roots` 字段、
  `allowed_roots_policy` check（形状不变，取值收紧）；db-free selector
  rejection 列表（新增可达 rejection）。
- Frontend/downstream consumers: `_scheduler_root_check` 容器判定（消费收紧后
  的 roots）；`_db_free_path_check`（消费站点 2 产物）；facade
  `scheduler_candidate_runtime.py` 符号再导出（名称不变）。
- Failure paths/rollback/stale state: ≤3.12 数据库态环根：RuntimeError 逃逸 →
  结构化 blocker（契约升级，披露于 spec delta）；3.13+ 环根：静默纳入 →
  剔除 + blocker（fail-closed 收紧）。
- Evidence/audit/readiness: 两证据面一致性（主锚点回归测试）；blocker path
  掩码纪律。

Regression rows:
- 环路根（真实 symlink 环，全版本 ELOOP）：数据库态 → 根剔除 +
  `SCHEDULER_ROOT_ALLOWED_ROOTS_UNSAFE_PATH`，两条 preflight 臂 status=blocked，
  payload `allowed_roots` 不含环根；db-free → 词法臂纳入、无 blocker。
- ENOTDIR（普通文件作中间组件，无需 symlink、全版本可达）：同上分流——判据
  是 "not ENOENT" 而非 "only ELOOP"。
- ENOENT 根：两种运行态均纳入（非 strict 规范形），无 blocker——历史语义钉死。
- `<missing>/../<loop>` 组合：strict 抛 ENOENT → 非 strict realpath 吸收，
  纳入，无 blocker（#1332 round-1 组合路径）。**限定（round-1 FT-1 修正）**：
  该行只在 seam 输入面成立（站点 3 的 raw env 面端到端可达）；真实
  `ProductionSchedulerConfig` 在构造期经 `_optional_config_path` 把该形态折叠
  为裸 loop，db-backed 落 UNSAFE_PATH + 剔根（终态由裸 ELOOP db-backed 行钉
  住），db-free 走 #831 词法臂。
- 证据面一致性（主锚点）：同 config 同环根，slurm preflight 丢根+blocker 且
  runtime-root preflight 丢根+blocker，两个 `allowed_roots` 证据字段一致。
- 掩码：数据库态 + repair_missing_forcing → UNSAFE_PATH blocker 的 path 为
  `[local-path]`。
- 站点 3：环根/ENOTDIR 根 → `db_free_allowed_root_unresolvable` rejection
  真实触发（死码复活锚点）；ENOENT 根 → 纳入无 rejection。
- 全根剔除：UNSAFE_PATH blocker + MISSING blocker 并存，status=blocked。
- not_required（db-backed 默认态）：环根 → payload `allowed_roots` 不含环
  根、无 blocker、不抛（D6）；B8 tripwire 翻转为该结构化断言且移除
  `skipif(>=3.13)`。
- 数据库态 repair-authority lane（`repair_missing_forcing=True`、
  `scheduler_db_free_required=False`、`require_runtime_roots` 任意）+ 环根 →
  站点 2 剔除该根 + `db_free_allowed_root_unsafe` blocker；copyback/raw-
  manifest path 的 containment 不再以环根为基准（D4 round-2 臂）。
- db-free 运行 + 环根 → 站点 2 词法臂纳入、无 blocker（#831 逐字，AC 4）。
- EACCES/EPERM 行为变更披露（fixture-review 建议 6）：不可 traverse 祖先的
  根今天在两运行态均被静默放行，改后 db-backed 剔除 +
  `SCHEDULER_ROOT_ALLOWED_ROOTS_NOT_WRITABLE`、db-free selector 落
  rejection——判据是 "not ENOENT"，此收紧作用于全部受支持版本。容器内不可移
  植构造 EACCES（root 用户绕过 chmod），该分支由 ENOTDIR 行代表性覆盖 + 此处
  具名披露，不做专属测试。
- 双证据面一致性测试必须显式 `require_runtime_roots=True`（否则 runtime 臂
  early-exit 到 not_required，锚点空断言——fixture-review AC2 提示）。
- 既有绿测：`-k "preflight or allowed_root"`（issue 撰写时 122 条）零回归
  （**唯一例外：B8 tripwire 按其自身 docstring 契约翻转**），断言不弱化。

## Review focus

1. 三站点判据与 #1346 范式逐字一致；文件内不残留 allowed-roots 用途的
   `Path.resolve()`。
2. 证据面一致性测试是双平面实调（非各自单测拼装）。
3. #831 词法臂在站点 1/2 的非 ENOENT 路径上逐字保留。
4. 站点 3 rejection lane 的测试证明"修前死码、修后可达"（红证）。
5. D5 的三处 DEFER 是否遗漏了会被本 PR 改动激活的新后果。
