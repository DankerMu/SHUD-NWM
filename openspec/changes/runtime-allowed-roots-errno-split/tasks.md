# Tasks: runtime-allowed-roots-errno-split (#1348)

Fixture level: expanded（issue 无 Suggested fixture level 字段；预估规模 M——
三站点范式搬运是 S，跨版本臂 + 双证据面一致性测试撑到 M；同族单站点先例
#1345 用 compact，本 issue 三站点 + 证据面锚点，上调为 expanded 并记录此分
歧）· Repair intensity: high · Seams under test：
`_scheduler_allowed_roots_and_blockers` / `_scheduler_allowed_roots`（站点 1
单元 seam）、两条 preflight 臂 payload（`_scheduler_lock_evidence_root_
preflight` / `_scheduler_runtime_root_preflight` 的 status/blockers/checks）、
`_db_free_allowed_roots`（站点 2）、retry.py db-free selector allowed-roots
lane 的 rejection 列表（站点 3）、双证据面一致性（slurm preflight ×
runtime-root preflight）——由 issue 复核命令
`-k "preflight or allowed_root"` 声明；无新 seam。

## Risk packs (considered)

- File IO / path safety / overwrite: **selected** — symlink 环/ENOTDIR/EACCES
  根的规范化判据本体。
- Error handling / rollback / partial outputs: **selected** — 裸 raise →
  结构化 blocker 契约升级；死码 rejection lane 复活；fail-open→fail-closed。
- Schema / columns / units / field names: **selected** — blocker code 家族
  （`SCHEDULER_ROOT_ALLOWED_ROOTS_<REASON>`）、preflight payload
  `allowed_roots`/`allowed_roots_policy` 证据形状、path 掩码纪律。
- Legacy compatibility / examples: **selected** — ENOENT admitted 语义、#831
  词法容忍臂、去重保序、既有 122 条 preflight/allowed_root 测试零回归。
- Concurrency / shared state / ordering: not selected — 纯函数判据，无共享态。
- Public API / CLI / script entry: not selected — module-private 符号 +
  facade 名称再导出不变。
- Auth / permissions / secrets: not selected — 无凭据面（EACCES 只是 errno 分
  流输入）。
- Config / project setup: not selected — 不新增配置；#1347 已修配置构造层。
- Resource limits / large input / discovery: not selected — 每根一次 realpath。
- Release / packaging / dependency compatibility: **selected** — 跨 CPython
  版本（3.11/3.12 生产臂 vs 3.13+）语义一致性是本病根，测试须版本不敏感
  （errno 判据 + 真实故障形状，不 mock 版本）。
- Documentation / migration notes: **selected**（fixture-review round 1 重
  选，范围窄）— runbook 无 allowed-roots 专段（核实过），但
  `docs/governance/SCHEDULER_COMPATIBILITY_INVENTORY.md:127/:156` 强制要求新
  runtime-root helper 经 `scheduler.py` 暴露时登记；D2 裁定新配对函数走
  facade 惯例，故 inventory 必须补一行。
- Domain packs: not selected — 无数值/地理/求解器面。

## Tasks

- [ ] 1. 站点 1：新增 `_scheduler_allowed_roots_and_blockers`（D2/D3 三臂裁
  决，含掩码纪律），`_scheduler_allowed_roots` 降级为 `[0]` 读取面；两条
  preflight 臂解包并 extend blockers（顺序：unsafe 先于 policy MISSING）；
  `:457` 裸 raise 删除；全文件 allowed-roots 用途零 `Path.resolve()`。
- [ ] 2. 站点 2：改为配对函数 `_db_free_allowed_roots_and_blockers`（D4
  round-2），旧名 = `[0]` 读取面，唯一调用点 `scheduler_config.py:738` 解包
  并 extend blockers；ENOENT → 非 strict realpath 纳入；非 ENOENT ×
  db-free → #831 词法臂逐字保留；非 ENOENT × 数据库态 repair-authority
  lane → 剔除 + `_db_free_blocker("db_free_allowed_root_unsafe", ...)`。
- [ ] 3. 站点 3：retry.py `:1529-1535` strict realpath + errno 分流，
  `db_free_allowed_root_unresolvable` 死码复活。
- [ ] 4. facade 注册与消费方封闭性：新配对函数登记
  `scheduler_candidate_runtime.py` FORWARDER_NAMES/赋值区/EXPORTS 三处 +
  `docs/governance/SCHEDULER_COMPATIBILITY_INVENTORY.md` 补行（D2）；
  `grep -rn "_scheduler_allowed_roots\b"` 全仓核对旧符号调用点无漏改。
- [ ] 4b. B8 tripwire 翻转（D6，必做交接）：
  `test_scheduler_root_preflight_still_raises_on_preexisting_loop_allowed_root`
  按其 docstring 契约改为修后结构化断言（不抛、status=not_required、
  `allowed_roots` 不含环根），移除 `skipif(>=3.13)`，改名去掉
  "still_raises"；同步更新 B1 测试 docstring 的 tripwire 交叉引用
  （`tests/test_production_scheduler.py:31754-31756`）。
- [ ] 5. 测试（`tests/test_production_scheduler.py`）：
  - 站点 1 单元 seam：环根（真实 symlink 环）/ENOTDIR/ENOENT/
    `<missing>/../<loop>` 四形状 × 数据库态/db-free 两运行态的
    `(roots, blockers)` 裁决表；
  - preflight 臂 seam：数据库态环根 → 两臂 status=blocked、
    `SCHEDULER_ROOT_ALLOWED_ROOTS_UNSAFE_PATH`、payload `allowed_roots` 不含
    环根；全根剔除 → UNSAFE_PATH + MISSING 并存；
  - **双证据面一致性（主锚点）**：同 config 同环根（config 必须显式
    `require_runtime_roots=True`，否则 runtime 臂 early-exit 空断言），一个
    测试内实调 `_preflight_allowed_roots` 与 runtime-root preflight，断言两
    个 `allowed_roots` 证据面同为丢根 + 各自 blocker，不矛盾；
  - not_required 态（D6）：db-backed 默认 config + 环根 → 两条臂均不抛、
    payload `allowed_roots` 不含环根、无 blocker；
  - 掩码：数据库态 + repair_missing_forcing → blocker path `[local-path]`；
  - 站点 2：db-free 环根走词法臂（3.13+ 上与"解析成功"可区分）、ENOENT 走非
    strict 规范形；**数据库态 repair-authority lane**（repair_missing_forcing
    config，参照 `tests/test_production_scheduler.py:11033-11047` 既有
    helper）+ 环根 → `db_free_allowed_root_unsafe` blocker 且 copyback/raw
    path containment 不以环根为基准；
  - 站点 3：环根/ENOTDIR → rejection 真实触发（附修前死码红证：判据回退后该
    测试必红）；ENOENT → 纳入；
  - 版本不敏感性：全部用真实故障形状 + errno 断言，无版本分支 mock。
- [ ] 6. 既有测试零回归：`-k "preflight or allowed_root"` 全绿且断言不弱化
  ——**唯一授权例外是 B8 tripwire（任务 4b）**，其翻转是该测试自身 docstring
  写明的交接义务，非回归；受 D2 签名影响的直接调用测试只允许解包适配。

## Required evidence (maps every selected pack)

- 环根 × 数据库态 → 剔除 + UNSAFE_PATH blocker + status=blocked。[File IO,
  Error handling, Schema]
- 环根 × db-free → 词法臂纳入、无 blocker。[Legacy, File IO]
- ENOTDIR 根（无 symlink，全版本可达）→ 与环根同分流——判据 "not ENOENT"。
  [File IO, Release/跨版本]
- ENOENT 根 × 两运行态 → 纳入、无 blocker（历史 admitted 语义）。[Legacy]
- `<missing>/../<loop>` → ENOENT 臂吸收纳入。[File IO, Release/跨版本]
- 双证据面一致性：slurm preflight 与 runtime-root preflight 对同一环根同判。
  [Schema, Error handling]（issue 主锚点）
- 掩码：repair_missing_forcing 数据库态 blocker path=`[local-path]`。[Schema]
- 全根剔除 → UNSAFE_PATH + MISSING 并存。[Error handling, Schema]
- 站点 3 rejection 死码复活（红证：回退判据必红）。[Error handling]
- 站点 2 db-free 两臂重新可区分。[Legacy, Release/跨版本]
- 站点 2 数据库态 repair lane 环根 → 剔除 + `db_free_allowed_root_unsafe`
  blocker（phantom base 消灭，与 slurm 平面一致）。[Error handling, Schema]
- not_required 态环根 → 不抛 + `allowed_roots` 剔除 + 无 blocker；B8
  tripwire 翻转为此断言（D6）。[Error handling, Legacy, Release/跨版本]
- inventory 登记行存在（`SCHEDULER_COMPATIBILITY_INVENTORY.md`）。
  [Documentation]
- 既有 preflight/allowed_root 测试（issue 撰写时 122 条）零回归（B8
  tripwire 除外，按 D6 翻转）。[Legacy]
- Commands: `uv run pytest -q tests/test_production_scheduler.py -k
  "preflight or allowed_root"`、`uv run pytest -q
  tests/test_production_scheduler.py`、`uv run ruff check .`、
  `openspec validate runtime-allowed-roots-errno-split --strict
  --no-interactive`。

## Non-goals

- `_optional_config_path`（#1347/PR #1349 已修，不触碰）；
  `_preflight_allowed_roots`/`_storage_root_check`（PR #1346 已修）；D5 三处
  evaluate-only 面（`_db_free_path_identity`、
  `_db_free_selector_path_rejection`、parent 级 preserve-final 三处）全部
  DEFER——`_db_free_selector_path_rejection` 合并前经 issue-scribe 立项路由。
