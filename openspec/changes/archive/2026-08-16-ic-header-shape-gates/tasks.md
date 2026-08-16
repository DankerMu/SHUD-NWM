# Tasks: ic-header-shape-gates (#1197)

## Fixture triage

- Issue 无 upstream `Suggested fixture level`（issue-scribe 产出，非
  stage-change-pipeline）；orchestrator 定 **expanded**（1 shared helper +
  4 消费点 + 3 spec 域 + 生产 OOM 事故根因），记录在案。
- Minimal mergeable slice = 全部四门 + helper（拆开任何一门都留下
  fail-open 面；SHUD 侧 sanity 上界与 state_cli 同型漏洞已 out-of-scope）。

## Tasks

- [x] 0. 运行时探针（先于实现，结果记入 PR 偏离记录）：
  - (a) 复证 issue 证据链：构造 `23106\t6` 头部 fixture 直调
    `cfg_ic_header_minute_index(["23106","6"])` → 1（列数被当 minute），
    `_shift_cfg_ic_time` 实测覆写第二 token。
  - (b) 复证 `_header_counts(["23106","6"])` 返回非 None
    （mesh=23106, river=0, lake=0）——设计 D1「旁路新增不改语义」的前提。
  - (c) 复证勘察结论：`direct_grid_variant_registration.py` 全文无 IC 字
    节读取；`provision_direct_grid_scheduler_registry.py:354` 是唯一把 IC
    字节**喂入 dg 变体打包**（`build_direct_grid_variant`）的点；
    `node22_clone_direct_grid_cutover_states.py:183` 亦读 IC 字节但仅作
    G10 指纹输入、不打包，具名 out-of-scope（fixture review P2-3 口径）。
  - (d) 复证 `.sp.mesh` 首行格式 `<n_elements>\t<n_cols>`（fixture
    `tests/fixtures/mapping_builder/keliya_minimal/keliya.sp.mesh`）。
  - (e) 复证限定门分域现状：生产门 `scheduler_generation_gate.py:205-244`
    与审计镜像两探针的三态、tier-a/tier-b 分派
    （`scheduler_generation.py:437-441`）、
    `PACKAGED_IC_UNREADABLE`/`PACKAGED_IC_UNQUALIFIED` 分域走向
    （scheduler_generation.py:449-560 token 词表）。
  - (f) **实机 mesh 先验探针**（fixture review P2-4，mesh 交叉校验零真实
    数据先验）：node-27 `/home/ghdc/nwm/Basins` 13 个 baseline + node-22
    NFS `/ghdc/data/nwm/object-store/models/**` 51 个打包 package（含
    dg 变体共 103 个 IC 文件），逐个
    `head -1 *.cfg.ic`（token 数 + 首 token）对 `head -1 *.sp.mesh` 首
    token，全表记入 PR 证据；**发现任何在跑 model 不匹配 → 停下报告重
    裁**（never-break-userspace）。
  - 任一探针与 design 断言不符 → 停下报告重裁。
- [x] 1. `packages/common/state_qc.py`：`CfgIcHeaderShape` +
  `cfg_ic_header_shape`（design D1）；沿用 `_as_float`；不改既有函数。
- [x] 2. 注册门接线（design D2 行 1）：`basins_discovery.py` **每个**
  cfg_ic 匹配文件首行有界读取 + `.sp.mesh` 首行轻量解析 + 新 key
  `invalid_required_files`（参与 `status` 计算，不污染
  `missing_required_files`）fail-closed 拒绝（reason 含路径 + 实际 token
  数）；匹配到但读不出 → 第三态拒绝（独立 reason）；`sp_mesh` 多匹配 →
  ambiguous 拒绝（独立 reason）；`invalid_required_files` 进
  publish/import 终态错误 details（closure F-D 三处：
  `publish_scheduler_file_registry.py:670-676`/`:762-770`/`:890-898`）；
  不 abort 整个 discovery。
- [x] 3. dg provision 门接线（design D2 行 2）：
  `provision_direct_grid_scheduler_registry.py` 喂 bytes 前形状 + mesh 校
  验，fail-closed 拒绝。
- [x] 4. 限定门接线（design D2 行 3 分层 seam）：tier-b——
  `PackagedIcObjectProbe` 形状字段 + 生产门/审计镜像两探针填充 +
  `classify_packaged_initial_condition` 消费（classify tier 分派不动，
  `test_scheduler_generation.py:3035-3044` tier-a 永不探针锁保持绿）；
  tier-a——audit 自有层内容探针 + 同一 helper + receipt 覆写 + 新
  `ic_qualification_source` 值（`:713` 正常 inventory 锁保持绿）+
  schema 同步（limits 新键 + source 词表 + note 文案）；新 token
  `packaged_initial_condition_header_shape_invalid` 归 UNQUALIFIED 分
  域；UNREADABLE 分域零改动；生产门 tier-a metadata-only 具名限制。
- [x] 5. 注入器 fail-closed（design D2 行 4 + D3 表）：文件存在且非空且
  <3 数字 token → 不写 + `SHUDRuntimeError`；缺失/空文件 noop 不变；≥3
  行为逐字节不变；`:2796`/`:2844` 两调用点零改动自然冒泡；`:1576` warm
  路径调用方捕获转 corrupted-state 降级阶梯。
- [x] 6. 测试（design D4 seams 1-6 + D5 重判）：helper 矩阵、四门用例、
  回归夹具 `23106\t6` 三处拦截、byte-compat oracle、重判
  `test_shift_header_without_minute_time_pair_is_noop`（表外零编辑）。
- [x] 7. 红证五组（design D6 R1-R5）+ mutation 还原 + `git stash list`
  空核验。
- [x] 8. 回归：`uv run pytest -q tests/test_state_qc.py
  tests/test_basins_discovery.py tests/test_runtime_ic_header.py
  tests/test_first_cycle_initial_state_audit.py` 全绿；
  `uv run pytest -q tests/test_shud_runtime.py` 全量全绿；
  `uv run ruff check .`；`openspec validate ic-header-shape-gates --strict
  --no-interactive`。
- [x] 9. AC 对照自审：issue #1197 六条 AC 逐条映射（AC-1 通知半边 =
  issue 评论已登记用户侧待办 + 0.0 占位显式记录，PR 不关闭 issue）；具名
  偏离（dg 门落点替换、state_cli 同型漏洞路由、注入器 ≥5 宽准入不对称、
  1-token 重判、生产门 tier-a metadata-only 限制、warm 路径降级转译）写
  入 PR body；`state_cli.py` follow-up issue（覆盖 :262 与 :317 两处）在
  Phase 8 前立单；`_checksums_for_required_files:316-323` 静默
  `except OSError: continue`（fail-open）记 out-of-scope 观察一并路由。

## Required evidence (maps every selected pack)

- oracle-integrity：task 0 探针 + task 7 红证 + byte-compat oracle。
- spec-compliance：三 spec delta scenario ↔ D4 seams 映射 + task 9 AC 对照。
- terminal-state-semantics：D2 失败通道逐门测试 + D3 注入器行为表逐行。

## Non-goals

见 design "Non-goals" / proposal "Out of scope"。
