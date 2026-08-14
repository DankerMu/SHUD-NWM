# Design: retention-archive-gate-disabled-mode（#1369）

Fixture level: expanded
Repair intensity: high（触发词：production delete / rollback / evidence chain（receipt schema）/ production config（env + 首次 enable timer））
Project profile: NHMS/NWM（node-27 生产 DB 常设删除面）

## Change surface

- `scripts/node27_timeseries_retention.py`：`RetentionConfig`（新字段 `archive_gate`；`completeness_receipt_path`/`drill_receipt_path` 转 `Path | None`，`:206-207`）、`config_from_args`（env/CLI 解析 + 条件必填）、`_parser()`（`--archive-gate` choice）、`run_retention()`（相位 1a/1b/2b/1c/2c 模式路由 + 两个非 gate 消费点，见 D2）、`build_receipt()`（archive_gate 对象）、模块 docstring `:50`。
- `schemas/timeseries_retention_receipt.schema.json`：`schema_version` const `1.0`→`1.1`；顶层必填 `archive_gate`。
- `schemas/examples/timeseries_retention_receipt.example.json`：同步 1.1 + archive_gate（不改则 `tests/test_timeseries_storage_schemas.py:48-60` 直接红）。
- `tests/test_node27_timeseries_retention.py`（含 `:4676-4700` 两个 format-checker 负例修复，见 legacy 包）、`tests/test_timeseries_storage_schemas.py`。
- 文档：runbook `docs/runbooks/tier-node27-timeseries-storage.md` §7 头/§8 开篇/§8.1/§8.2 注记/§8.4/§8.5+§8.7 读法/Live-state notes（`:308`）/**Timer cadence order 表与 D6 新鲜度段（`:310-334`）**；`infra/env/node27-timeseries-retention.example`；receipts README。
- **不动**：`infra/systemd/*.timer|*.service`（OnCalendar 05:15 用户裁定沿用）、wrapper（env 透传）、`WIRE_CODES` 四面同一性、gate 判定函数本体、redaction chokepoint、H3/H4/H5 drop 语义、`packages/common/storage.py`。

## D1 模式解析（fail-closed enum，非 truthiness）

env `NODE27_TIMESERIES_RETENTION_ARCHIVE_GATE`：未设 → `enabled`；设值 strip+lower 后必须 ∈ {`enabled`,`disabled`}，否则（含空串、`disable`、`true` 等）→ `RETENTION_CONFIG_INVALID`，exit 2，不写 receipt（`:1590-1603` 语义）。不采用 `--enforce` truthiness 先例：三值风险开关 typo 必须炸。CLI `--archive-gate` 为 argparse `choices`，优先于 env（沿 `--enforce` 先例）。

## D2 gate 跳过的完整边界（两 gate + 两个非 gate 消费点）

`disabled` 短路四处 completeness/drill 消费：

1. `load_completeness_receipt`/`check_completeness_gate`（相位 1a/1b/2b）与 `load_drill_receipt`/`check_drill_gate`（相位 1c/2c）——不调用。
2. **`_partition_by_completeness_bounds`（`:1489-1496`，gate 之外）**：enabled 下把超出 completeness bounds 的 boundary-partial chunk 移入 `deferred_remainder`；disabled 下无 completeness 对象，裁定 `covered_eligible = eligible`——**boundary-partial chunk 在 disabled 下会被删**。这是记录在案的删除面语义变化（无归档数据时"部分覆盖"概念本身消失），非疏漏；runbook §8.5 的 deferred 语义描述加 disabled 注记。
3. **`derive_salvage_backed_windows`（`:1568`）**：enforced receipt 的 schema 必填字段 `salvage_backed_windows` 由 completeness 派生；disabled 下恒为 `[]`（= 无归档背书的显式记录），§8.5/§8.7 读法注记。

与 dry-run/enforce 正交（第三轴）。runner 自有 refusal（`RETENTION_CONCURRENT_INVOCATION`/`RETENTION_DROP_FAILED`/`RETENTION_UNCAUGHT_ERROR`）在 disabled 下依旧可达；**13 个归档族 code（`COMPLETENESS_*`×5 + `DRILL_*`×8）**在 disabled 下不可达（钉法见 Seams 3）。

## D3 receipt 的 archive_gate 对象（schema 1.1）

```json
"archive_gate": {
  "type": "object", "additionalProperties": false, "required": ["mode"],
  "properties": {
    "mode": {"enum": ["enabled", "disabled"]},
    "adr_reference": {"const": "docs/adr/0002-node27-timeseries-hot-cold-tiering.md Revision 2026-08-11"}
  },
  "oneOf": [
    {"properties": {"mode": {"const": "enabled"}}, "not": {"required": ["adr_reference"]}},
    {"properties": {"mode": {"const": "disabled"}}, "required": ["adr_reference"]}
  ]
}
```

顶层 `required` 增补 `archive_gate`（三个 outcome 分支通吃——refused receipt 也必须自述当时 gate 模式）；既有三分支 `not` 禁用列表不涉新字段。`schema_version` bump `1.1`：新增必填字段即格式版本变更；历史 receipt 不回写（既有 requirement），README 注记双版本读法。`adr_reference` const 钉死引用（#1338 出处教训）。

## D4 条件必填的两个路径 env

completeness/drill 路径必填校验仅 `archive_gate=enabled` 时生效；disabled 时二者（连同 max-age env）可缺省、给了也不读（config 存 `None`）。**实现陷阱**：`_resolve_path` 的 `required=False` 分支是 raise（`:296-297`）——disabled 侧的正确表述是**不调用该解析**，而非传 `required=False`。方向双钉：enabled+缺路径 → `RETENTION_CONFIG_INVALID`（不变）；disabled+缺路径 → 正常运行。

## 实现陷阱（reviewer 核出，实现必读）

- `RetentionConfig.completeness_receipt_path/drill_receipt_path` 类型改 `Path | None`。
- runbook 修订文本中**禁止出现裸 `RETENTION_ARCHIVE_GATE` token**——`tests/test_node27_timeseries_retention.py:432-448` 反向 token walk 会将其判为 orphan wire code；一律写全 `NODE27_TIMESERIES_RETENTION_ARCHIVE_GATE`。
- `:4676-4700` 两个 format-checker 负例手写 `"schema_version": "1.0"` 且无 archive_gate——bump 后它们会因缺字段而红转"假绿"（不再检验 date-time checker）；必须补 `1.1` + 合法 archive_gate 使其只因 date-time 违规而红。

## Seams under test

1. `config_from_args`（模式解析全表 + CLI 优先 + D4 双向）。
2. `run_retention`（disabled 跳 gate + boundary-partial 语义变化 + salvage `[]`；enabled 回归：除 schema_version/示例同步与 `:4676-4700` 两个 format 负例修复外既有测试零修改通过）。
3. **不可达性钉法**：monkeypatch `load_completeness_receipt`/`load_drill_receipt`/`check_completeness_gate`/`check_drill_gate` 为 `raise AssertionError`，断言 disabled 下 dry-run 与 enforce 均正常出 receipt；矩阵断言：disabled 下所有 refused receipt 的 `refusal_reason` 前缀 ∉ 13 个归档族 code。
4. `build_receipt` + `_validate_receipt` + schema 文件（三分支 × 两模式；负例见 tasks 2.5）。
5. 文档字节钉：env example key 元组扩展（`test_env_example_lists_all_h13_keys`，`:4360`）；runbook §8.4 ADR 引用逐字锚。

## Invariant Matrix

- Governing invariant: 每张 retention receipt 自述其生效 gate 模式，disabled 模式必携 ADR 0002 Revision 2026-08-11 的 const 引用；env 未设时 runner 行为与 master 逐字节一致。
- Source-of-truth identity/contract: `schemas/timeseries_retention_receipt.schema.json` 1.1 的 `archive_gate` 对象（mode enum + const adr_reference）。
- Producers: `build_receipt()`/`run_retention()`（`scripts/node27_timeseries_retention.py`）。
- Validators/preflight: `_validate_receipt()`（jsonschema + date-time format checker）；`config_from_args` fail-closed enum。
- Storage/cache/query: receipt 原子写 0600（不变）；无 DB 存储面。
- Public routes/entrypoints: 无 HTTP 面；入口 = CLI/systemd timer（oneshot wrapper）。
- Frontend/downstream consumers: 无 runtime 消费者（探明：receipt 仅 schema-fixture 测试与文档引用）；文档面 = runbook §8.5/§8.7 + receipts README。
- Failure paths/rollback/stale state: 非法模式 → exit 2 无 receipt；refused receipt 亦带 archive_gate；drop 失败 fail-closed 不变；**已删 chunk 不可逆（无归档兜底）**——回滚仅指停止后续删除（移除 env + disable timer）。
- Evidence/audit/readiness: node-27 dry-run receipt + enforce receipt + `list-timers` 取证（tasks 3.4）。
- Regression rows:
  - env 未设 + 归档 receipt 齐备 → 与 master 判定逐字节一致（既有套件除同步处外零修改绿）。
  - disabled + 两路径全缺 + enforce → 删 ≤per-tick bound；receipt mode=disabled + const 引用 + `salvage_backed_windows=[]`。
  - enabled + 缺任一路径 env → `RETENTION_CONFIG_INVALID`（enabled 侧未放松）。
  - disabled + enabled 下会被 bounds-defer 的 boundary-partial chunk → 进 candidate（记录的语义变化行）。
  - 未触碰兄弟面：`check_*_gate` 单测 + wire-code 四面同一性 + timer OnCalendar 钉全绿零改动。

## Boundary-surface checklist

- Write/delete surface：`drop_chunk`（H3/H4/H5 语义零触碰；变的只是"谁有资格进 candidate"）。
- Evidence surface：receipt schema 1.1 + 历史 receipt 不回写；示例文档同步。
- Production config surface：env example 新变量（强警示）+ 首次 enable timer（用户裁定 2026-08-14）。
- Unchanged downstream：gate 函数本体、WIRE_CODES 四面同一性、wrapper、redaction。

## Risk packs considered（core）

- Public API / CLI / script entry: **selected** — 生产脚本入口新增 env+CLI 开关；解析错误面 = exit 2 无 receipt。
- Config / project setup: **selected** — D4 条件必填反转是最易错处（双向钉）；env example 同步。
- Schema / columns / units / field names: **selected** — receipt schema 1.1 新必填字段 + 内嵌 oneOf（负例钉）+ 示例文档 + 两个 format 负例修复。
- Error handling / rollback / partial outputs: **selected** — 非法值 fail-closed exit 2；runner 自有 refusal 可达 + 13 归档族 code 不可达双钉；**删除不可逆性显式记录**。
- Legacy compatibility / examples: **selected** — 默认行为逐字节不变；既有套件**除 schema_version/示例同步与 `:4676-4700` 负例修复外零修改**全绿为硬证据；历史 receipt 不回写。
- File IO / path safety / overwrite: not selected — receipt 原子写/0600/锁路径未触碰；新增的是"少读两个文件"。
- Auth / permissions / secrets: not selected — 无凭据面变化，redaction 零触碰。
- Concurrency / shared state / ordering: not selected — flock 未动；timer 启用后的并发由既有 `RETENTION_CONCURRENT_INVOCATION` 覆盖（已有测试）。
- Resource limits / large input / discovery: not selected — per-tick bound/超时不变。
- Release / packaging / dependency compatibility: not selected — 零依赖变化。
- Documentation / migration notes: **selected** — runbook 七处（含 cadence/D6 段）+ env example + receipts README + 示例文档；ADR 本体只引用不改写。

## Domain risk packs（openspec/project-profile.md）

- PostGIS / TimescaleDB domain behavior: **selected** — `drop_chunks` 生产删除面；证据 = 3.4.2 node-27 实机 enforce receipt（真 hypertable 真 chunk）。
- Hydro-met time series / forcing windows: not selected — watermark/保留窗语义零触碰（Non-Goals）。
- Published NHMS artifacts / display identity: not selected — display carve-out 明文不变（ADR revision 原文）。
- Slurm / compute scheduling: not selected — node-27 systemd timer，非 Slurm 面。
- Geospatial / CRS / basin geometry: not selected — 无几何面。
- SHUD numerical runtime: not selected — 无 SHUD runtime 面。
- External hydro-met providers / snapshot reproducibility: not selected — 无 provider snapshot 面。
- Run manifest / QC provenance: not selected — 无 run manifest 面（receipt 是 retention 自有证据，非 run QC 链）。

## Review focus

1. 默认路径零漂移：env 未设时与 master 行为逐字节一致（含 config 错误面）。
2. D4 反转没有放松 enabled 侧；`_resolve_path` 陷阱是否按"不调用"实现。
3. archive_gate 在全部三个 outcome 分支必填且形状正确；refused-in-disabled 可审计；`salvage_backed_windows=[]`。
4. 13 个归档族 code 不可达的钉法（monkeypatch raise + 前缀矩阵）是否真钉住。
5. boundary-partial chunk 在 disabled 下会被删——语义变化是否在代码注释、runbook §8.5、receipt 读法三处一致陈述。
6. runbook 措辞与 ADR revision 一致（deliberate and auditable, not a silent bypass）；无裸 token 触发反向 walk。
