# Design: ic-header-shape-gates (#1197)

## D0 — 风险裁量与具名偏离（先读）

- 风险轴：畸形数据穿三关 + 运行期 183 GB OOM（生产已炸过一次），
  priority:high。fixture 级 **expanded**（多面接入：1 helper + 4 消费点 +
  3 spec 域；无 upstream suggested level，orchestrator 定级记录在案）。
- **偏离 1（issue 文本纠偏，dg 打包门落点）**：issue In-scope 2 指向
  `workers/model_registry/direct_grid_variant_registration.py`，勘察证实该
  文件是纯 DB 行插入（`core.model_instance` INSERT + `met.met_station`
  upsert），**不接触 IC 字节**。direct-grid 流程中 IC 字节唯一读取点是
  `scripts/provision_direct_grid_scheduler_registry.py:354`
  （`state_schema_bytes=_required_single(baseline_root, "*.cfg.ic").read_bytes()`
  → `build_direct_grid_variant`）。dg 打包门落在该读取点。issue 措辞
  「与/或 baseline 注册」允许该替换；PR 偏离记录具名。
- **偏离 2（第三条同型漏洞 out-of-scope）**：
  `packages/common/state_cli.py` 内**两处**共享同一条 2-token 歧义
  （fixture review N4 扩面）：`_normalized_checkpoint_ic_file`（:247-282，
  归一化覆写）与 `_checkpoint_header_minute`（:307-323，把最后数字 token
  当 minute 读出喂 `_checkpoint_with_header_time`——2-token 头部会让
  checkpoint valid_time 被列数反推，危害更大）。checkpoint 头部由本仓
  state-save 流程自产（3-token native），暴露面远小于上游交付物；**报告
  不修**，Phase 8 前立 follow-up issue（覆盖 :262 与 :317 两处）并在 PR
  body 路由。
- **相邻门澄清（勿混同）**：`state_clone.py:297-306` 的空字节拒绝属 G10
  hydrologic-core 指纹相等门（fingerprint-gated-state-clone 域），不是形
  状门；`basins_package.py` `_validated_canonical_required_source_files` 是
  path-safety/checksum 门。两者均不因本 change 改动。
- **时序事实（限定门价值论证）**：`prepare_workspace`（runtime.py:552-554）
  在 `_consume_packaged_initial_state` 之后仍会经
  `_stage_standard_shud_forcing:1058 → _shift_cfg_ic_time:2844` 对同一
  `<project>.cfg.ic` 二次写头——packaged IC 的「timeless」docstring
  （:1334-1341）只约束自身函数体不调用 shift，不阻止后续路径。故注入器
  fail-closed 是最后一道网，注册/限定门是把故障左移到消费前。

## D1 — 共享 helper（`packages/common/state_qc.py`）

```python
@dataclass(frozen=True)
class CfgIcHeaderShape:
    numeric_token_count: int
    mesh_count: int | None      # 首个数字 token 的整数值；非整数/缺失为 None
    valid: bool
    reason: str | None          # 自然语句风格（本模块惯例）；valid 时 None


def cfg_ic_header_shape(
    header_tokens: Sequence[str],
    *,
    expected_mesh_count: int | None = None,
) -> CfgIcHeaderShape: ...
```

- 数字 token 判定沿用模块内 `_as_float`（与 `cfg_ic_header_minute_index`
  同口径，避免两套「什么算数字」规则）。
- **合法形状 = 数字 token 数 ∈ {3, 4}**（native
  `<mesh> <mesh-state-columns> <minute-time>` / 兼容
  `<mesh> <river> <lake> <minute-time>`）。0/1/2 个拒绝（`23106\t6` 即 2
  个）；**≥5 个拒绝**（未知布局，门上 fail-closed——注入器另有更宽准
  入，见 D3 行为对照）。
- mesh 交叉校验：`expected_mesh_count` 非 None 时，首个数字 token 必须为
  整数且等于之，否则 `valid=False`；两种合法布局首 token 均为 mesh 计
  数，无歧义。None 时跳过（qualification 门无 `.sp.mesh` 场景）。
- reason 必须含实际数字 token 数（AC-3 可定位性）；helper 不做 IO——头
  部行由调用方读好传入（模块 `expected_*_count` 由调用方供给的既有惯例）。
- **不改** `cfg_ic_header_minute_index` / `_header_counts` /
  `cfg_ic_header_minute_time` 的任何行为（全仓 7 个调用点零波及）。

## D2 — 四个消费点接线

| # | 门 | 位置 | 读取 | 失败通道 | expected_mesh_count |
|---|---|---|---|---|---|
| 1 | baseline 注册 | `basins_discovery.py` `_inventory_for_model`（`_match_required_files` 之后） | **每个** `cfg_ic` 匹配文件首行有界读取（≤4 KiB 首行，多匹配逐一校验、任一畸形即拒——N1 统一口径；`sp_mesh` 多匹配 → **拒绝为 ambiguous**（fail-closed，独立 reason），单一匹配才做 mesh 交叉——task 0(f) 实机探针若发现在跑 model 有多 `.sp.mesh` 即触发 stop 重裁）；匹配到但首行**读不出**（mode 000 / NFS EIO 等）→ **第三态 fail-closed 拒绝**，reason 与形状违规**不同**但同样拦注册（fixture review P1-2：仓内不存在「既有 unreadable 通道」，`_checksums_for_required_files:316-323` 的 `except OSError: continue` 是 fail-open，不得依赖） | 该 model 拒绝注册：新增 key `invalid_required_files`（不污染 `missing_required_files`——其 set-相等消费方 `publish_scheduler_file_registry.py:934` / `node27_autopipeline.py:822` 只认 glob 模式，N3），同样参与 `status` 计算（`basins_discovery.py:243`）使 `status != valid` → 既有硬拒通道（`publish_scheduler_file_registry.py:663-676` / `basins_registry_import.py:218-224`）生效；`invalid_required_files` 同时进入终态错误 details（closure F-D：`publish_scheduler_file_registry.py:670-676`/`:762-770`/`:890-898` 现只带 `missing_required_files`，只因形状被拒的 model 否则呈现 `status=partial + missing=[]` 零线索；「形状非法 + 只缺 tsd.rl」组合在 `node27_autopipeline.py:816-823` 仍判 repairable 后同样需要可定位理由）；reason 含路径 + 实际 token 数；**不 abort 整个 discovery** | 读 `sp_mesh` 匹配文件首行首 token（轻量本地解析，不引 `workers.mapping_builder` 重依赖——分层约束）；`.sp.mesh` 首行不可解析 → 同通道拒绝（fail-closed），reason 指明 sp.mesh |
| 2 | dg-variant provision | `provision_direct_grid_scheduler_registry.py:354` 一带，`read_bytes()` 后喂 `build_direct_grid_variant` 前 | 已在内存的 bytes 首行 | 脚本既有错误风格 fail-closed 拒绝 provision（退出码非 0 + 可定位消息） | baseline root 的 `.sp.mesh` 首行（同上轻量解析）；缺失/不可解析 → 拒绝 |
| 3 | packaged-IC 限定（fixture review P1-1 更正落点；判别 seam 见 closure F-A） | **tier-b（探针路径）**：`PackagedIcObjectProbe`（`scheduler_generation.py:365-378`）新增 header-shape 字段，生产门 `scheduler_generation_gate.py:205-244` `_canonical_packaged_ic_probe` 与审计镜像 `audit_first_cycle_initial_state.py:318-390` 两实现填充，判定在 `classify_packaged_initial_condition` 消费该字段——**classify 的 tier 分派（:437-441）不动**，tier-a 在 classify 内**永不发探针**（既有锁 `test_scheduler_generation.py:3035-3044` 保持绿，生产门每 pass 零对象 IO 承诺保持）。**tier-a（审计离线扫，audit 自有层）**：audit 对 inventory 形行在 classify 之后**自行**发起内容探针（`canonical_packaged_ic_object_uri` 对 tier-a 行同样可算，audit:449-457 已有 resource_profile 入口），用**同一个** `cfg_ic_header_shape` helper 判形状，畸形则在 audit 层把该行 receipt 判定覆写为 unqualified——形状规则单一来源在 helper，分层判定各自消费 | 探针内首行有界读取；存量 37 包经审计离线扫获得左移 | `ic_qualified=False`，新增 snake_case token `packaged_initial_condition_header_shape_invalid`（归 `PACKAGED_IC_UNQUALIFIED` 内容判定分域）；tier-a 审计覆写行的 `ic_qualification_source` 用**新值**（如 `inventory_content_probe`）——既有锁 `test_first_cycle_initial_state_audit.py:713`（正常 inventory 行 == "inventory"）保持绿；**receipt 契约同步（closure F-B）**：`schemas/first_cycle_initial_state_audit_receipt.schema.json` 为 limits 新增键（如 `inventory_tier_ic_header_probed`，`additionalProperties:false` 必须改 schema）+ source 字段词表扩值 + `:720-734` note 文案更新（内容探针只读首行不 re-hash，`inventory_tier_package_objects_rehashed=False` 的 const 仍真）；**探针不可读维持既有 UNREADABLE 分域**——「读不到」与「读到了但形状不合法」两域不混同（AC-4）。**具名限制**：生产调度门 tier-a 保持 metadata-only；补偿 = 审计离线扫 + 注入器最后一道网 + 新交付走注册门 | None（package 内不保证 `.sp.mesh` 在手；只做 token 形状——具名限定，mesh 交叉校验只在 1/2 两门） |
| 4 | 注入器 | `runtime.py:3215-3229` `_shift_cfg_ic_time` | 既有读取 | 数字 token < 3（文件存在且非空时）：**不写文件** + `raise SHUDRuntimeError`（新 error code，命名贴 `PACKAGED_IC_CONSUMPTION_FAILED` 风格，如 `IC_TIME_SHIFT_HEADER_INVALID`）。**三个调用点**（fixture review P2-1 补全）：`:2796`/`:2844` 不捕获自然冒泡至 `prepare_workspace` 调用方——既有可见错误通道；**`:1576`（`_materialize_ic_to_project_name`，warm-start 路径）由调用方捕获该 error code 并转入 corrupted-state 拒绝通道**（`_mark_init_state_corrupted` → `_next_usable_state` → cold-start 回退）——保住降级阶梯，畸形快照不整 run 失败。**可达性前提（closure F-C）**：warm 路径 `:1574` 先跑 `_verify_ic_time_consistency`，2-token 头部的最后数字 token 可读出（=6.0），快照带 `valid_time` 时会**先**在 `:1626-1633` 抛 `WARM_START_TIME_MISMATCH` 整 run 失败（**既有行为，本 change 不动**，具名记录）；新守卫的 warm 可达形态 = 0/1 数字 token 快照或无 `valid_time` 快照，D4 seam 5 回归夹具**必须**用这两种形态。「捕获转译」是本 change 新增行为，仓内**无**既有转译先例（closure 复核纠偏：`WARM_START_TIME_MISMATCH` 本身就是整 run 失败通道，不是转译先例，勿照抄） | 不做（注入器只管 token 数，mesh 校验在门上） |

## D3 — 注入器新旧行为对照（完整枚举）

| 头部数字 token 数 | 旧行为 | 新行为 |
|---|---|---|
| 文件不存在 | 早退 noop（`:3216-3217`） | **不变**（同族 `_shift_tsd_time_axis:3174-3177` 统一容忍模式；`*.cfg.ic` 未物化的 cold-start/诊断 manifest 合法形态，fixture review P2-2） |
| 文件为空 | 早退 noop（`:3218-3220`） | **不变**（同上） |
| 0/1（文件非空） | `minute_index=None` → 静默 noop（文件保留） | **raise + 文件字节不变**（需求重判：畸形头部必须可见，见 D5） |
| 2（`23106\t6`） | `minute_index=1` → **覆写列数**（本次事故） | **raise + 文件字节不变** |
| 3（native/兼容 3-token） | 覆写最后 token | 不变（逐字节同） |
| 4（兼容布局） | 覆写最后 token | 不变（逐字节同） |
| ≥5 | 覆写最后 token | **不变**（保守：注入器准入 ≥3 即 shift，宽于门上 {3,4}——避免对未知既有布局的静默行为翻转；门上已拦，具名不对称，PR 偏离记录） |

## D4 — Seams under test

1. helper 单元矩阵：3-token 通过 / 4-token 通过 / 2-token 拒绝 /
   0、1-token 拒绝 / ≥5 拒绝 / mesh 计数不匹配拒绝 / mesh 匹配通过 /
   非数字混排 token 计数正确 / reason 含实际 token 数。
2. 注册门：畸形 `23106\t6` fixture → model 被拒（`invalid_required_files`
   进 `status` 计算 → 非 valid）且 reason 可定位（路径 + token 数）；合法
   3-token + mesh 匹配 → 注册照常；mesh 不匹配 → 拒；`.sp.mesh` 首行畸形
   → 拒；cfg_ic 匹配到但首行读不出 → **第三态拒绝**（reason 与形状违规不
   同，两态并测防混同）；多匹配任一畸形 → 拒；**20 个既有 discovery 用例
   全绿**。
3. provision 门：畸形 bytes → 脚本 fail-closed；合法 → 照常。
4. 限定门：畸形 IC → `ic_qualified=False` + 新 token（**生产门探针与审计
   镜像两实现并测**）；审计 tier-a 内容探针对 inventory 形 manifest 拦截
   畸形 baseline 包；探针不可读 → 既有 UNREADABLE 分域（两域并测防混
   同）；合法 IC → 既有判定不变；**18 个既有 audit 用例全绿**。
5. 注入器：2-token → raise + 文件前后字节一致；1-token（非空文件）→
   raise（重判用例）；文件缺失/空 → noop 不变；3-token native / 4-token
   兼容 → 输出逐字节与旧实现一致（byte-compat oracle 取改前表达式）；
   **warm-start 路径（`:1576`）畸形快照 → corrupted-state 降级阶梯接手
   （换下一可用快照/冷启动回退），不整 run 失败——夹具用 0/1 数字
   token 或无 `valid_time` 快照（2-token 带 valid_time 会先命中既有
   `WARM_START_TIME_MISMATCH` 整 run 失败，closure F-C 可达性前提）**；
   `test_packaged_ic_*` 既有全绿。
6. 回归夹具：`23106\t6` 三处拦截（注册/限定/注入器）各一条端到端形态用例。

## D5 — 既有绿测重判（表外零编辑）

- `tests/test_runtime_ic_header.py:57-63`
  `test_shift_header_without_minute_time_pair_is_noop`（1 数字 token →
  noop）：需求驱动重判为「raise + 文件不变」。理由：噪声静默保留正是本
  次事故的传播机制；issue In-scope 4 明文「少于 3 个数字 token 拒绝改
  写 + 上抛可见错误」。其余触及文件的既有用例**零改写**（清单：
  `test_state_qc.py` 31、`test_basins_discovery.py` 20、
  `test_first_cycle_initial_state_audit.py` 18、`test_shud_runtime.py`
  `test_packaged_ic_*` 14、`test_runtime_ic_header.py` 其余 4）。

## D6 — 红证（mutation + 还原自证）

- R1：注入器回退旧实现（删 <3 守卫）→ 2-token 覆写用例 + 字节一致断言
  必红。
- R2：helper 放行 2-token（`{2,3,4}`）→ 注册/限定/回归夹具用例必红。
- R3：限定门把形状失败误折进 UNREADABLE 分域 → 分域并测用例必红
  （AC-4 判别器）。
- R4：注册门去 mesh 交叉校验 → mesh 不匹配用例必红。
- R5：warm 路径调用点改为裸冒泡（去 corrupted-state 转译）→ 降级阶梯用
  例（畸形快照后仍产出可用 run）必红。
- 每组 mutation 后 `git stash list` 恒空 + sha256 还原自证。

## Evidence mapping（selected packs）

- oracle-integrity：D6 四组红证 + 注入器 byte-compat oracle（改前表达式）。
- spec-compliance：三 spec 域 delta scenario ↔ D4 seams 1-6 映射 + AC 对照。
- terminal-state-semantics：D2 失败通道列逐门测试（拒绝记录形状 / 退出
  码 / `ic_qualified` 三态 / SHUDRuntimeError code）+ D3 行为表逐行。

## Non-goals

见 proposal「Out of scope」；全部具名，其中 `state_cli.py` 同型漏洞立
follow-up issue（Phase 8 前），其余记录理由不立单。
