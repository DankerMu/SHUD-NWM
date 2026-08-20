# Tasks: artifact-probe-file-kind-and-key-framing

> 已按 fixture review round 1（`.workplans/batch-p1/review/fixture-review.md`）修订：
> 坐标勘误、新增 sidecar / raw-manifest / 文档三族任务，删除基于误判的原 4.2。

## 1. store 层（`packages/common/object_store.py`，只许纯新增 + 一处委托）

- [x] 1.1 抽出模块级**纯字符串**归一化函数（prefix 路径段剥离 + `s3://` 支的 unquote），
      `normalize_key`（`:183-196`）改为**委托**给它。
      **不得顺手改语义**：unquote 只发生在 `s3://` 支（`_normalize_s3_uri` `:204-223`），
      裸 key 支（`:190-191`）**不 unquote**，本次不动。
      （委托 vs 复制是**代码评审属性、不是测试属性**——3.8 能证的只是"扰动纯函数时
      `normalize_key` 随之变动"，见 design.md 变异节 M5。）
- [x] 1.2 新增**四态**种类分类器方法（`file|directory|other|absent`），
      复用 `resolve_path`（`:169-181`）+ `stat_no_follow`，用 `stat.S_ISREG`/`S_ISDIR` 判定；
      错误容器与 `exists`（`:68-78`）**逐字一致**：`SafeFilesystemError` → `ObjectStoreError`，
      `FileNotFoundError` → `absent`。
- [x] 1.3 `exists` 本体**零改动**（diff 中不得出现该方法的任何行）。

## 2. 探针层与消费腿（`services/orchestrator/`）

- [x] 2.1 在 **`scheduler_state_common.py`**（不是 `scheduler_state_failure.py`——后者 `:19`
      单向 import 前者，反向会成环）新增两个 helper：
      `_object_store_prefix_for(candidate)`（**store-free 全函数**，防御式 `getattr` +
      `isinstance(Mapping)` + `str()`，**永不抛**）与 `_local_object_store_for(candidate)`（**store 工厂**）；
      `_object_manifest_is_missing`（`:164-169`）改为薄壳委托工厂，签名与返回类型不变。
- [x] 2.2 新增 sibling 探针，**仅当 `_object_manifest_is_missing` 返回 False 时**被咨询（`:1201`）；
      **只许加一个正向裁决**——absent、不可解析、以及 sibling 的任何其他结局，
      都必须让 `(False, None)` **逐字不变**（design.md D1 约束 2 的加严版）。
      sibling 不入 compat re-export 名单。
- [x] 2.3 local 腿 `:1231` 在 `path.exists()` 之上加**同一 reason** 的种类判定；symlink 口径不动（D3）。
- [x] 2.4 `_needs_package_manifest_witness` 改签名为 `(candidate, value)`，**先归一化再问 validator**；
      prefix 经 2.1 的 store-free helper 读取（**不得**照抄 `scheduler_state_common.py:165` 的裸 `.get`）；
      `ValueError` 折进既有 `:1087-1088` 腿；**不得构造 store**（D4）。生产调用点 `:713` 穿参。
- [x] 2.5 sidecar 腿（`:644-700`）**不改代码**，但确认新 token 落在 `:684` 且 `unsafe_reason`
      随 `:695` 带出（design.md D6 的实现期确认项；若实测不符则回到 D6 重裁）。
- [x] 2.6 扩 `tests/test_production_scheduler.py:25522` 的 `_RAW_MANIFEST_ABSTENTION_REASONS`
      至三项，并改其 `:25519-25521` 注释（"The **two** unsafe reasons …" 将不再成立）。
- [x] 2.7 改准四处失准注释/文档：
      `:1064-1067`（"validator 自己的 urlparse 已剥掉 prefix" —— 只对裸桶成立）、
      `:1196-1198`（"exactly one caller left"）、
      `:811-816`（自称 deeper-directory fail-open "unchanged here … routed as a separate follow-up"
      ——**本次正是修它**）、
      以及 `docs/runbooks/current-production-ops.md`：
      `:1756-1762` 的 operator reason 路由表**补一行**（`artifact_target_not_a_file` →
      "rebuild 修不了；先把占位目录清掉"）；
      `:1742-1744` 那句**严格说并不假**（它是只针对
      `object_store_root_unconfigured` / `artifact_probe_error` 两个 token 的条件句），
      但读者会取的推论"非空 reason ⇒ 根本没探"对新 token 不成立，须补一句限定；
      `:1749-1754` 关于 sidecar 层"read fault … 带 **null** `unsafe_reason`"的指引
      需注明新 token 在该层是**非空**（design.md D6）。

## 3. 测试（`tests/test_production_scheduler.py`）

- [x] 3.1 object 腿目录用例：URI **深于 pattern 段数** + root **已配置** + 目录连**整条父链**建
      （`mkdir -p`，否则 O8 抢先）+ root **先 realpath**（否则 macOS `/var` 使 O4/O9 抢先），
      断言 `(True, "artifact_target_not_a_file")`。
- [x] 3.2 local 腿目录用例：目录坐在**允许收容根之内**（绕开 L5 抢先），断言同一元组。
- [x] 3.3 seam 契约用例（D1 约束 2 的守门人）：monkeypatch
      **`scheduler_state_failure_module._object_manifest_is_missing`**（**不是 facade**——
      compat 传播只在 facade 调用期间生效，见 design.md D1 实测）→ False，
      真实 store 里**无任何物理对象**，断言探针仍返回 `(False, None)`。
      **须参数化覆盖两类 key**：validator 可收的与不可收的（后者证明 sibling 的
      `resolve_path` 失败不会把 present 翻成 `(True, None)`）。
      注：本用例**单独存在时是空腿**（sibling 没接进探针也照样绿），其判别力来自与 3.1/3.2 成对。
- [x] 3.4 #1397 回归：`OBJECT_STORE_PREFIX` **带路径段**、forcing FILE key **物理存在**，
      断言分类器答 False 且探针答 `(False, None)`（今日会答 `artifact_probe_error`）。
- [x] 3.5 裸桶前缀 + 无 percent-encoding 下，分类器答案与改动前**逐字一致**（must-preserve）。
- [x] 3.6 桶不匹配残留（`s3://other-bucket/...`）：路由与今日一致（D4 末段）。
- [x] 3.7 symlink-to-file 在 local 腿仍为 present（D3 分歧钉），注释引 **design.md D3**（**无 issue 号**）。
- [x] 3.8 扰动纯归一化函数时 `normalize_key` **随之变动**的等价性用例（M5 的真实 oracle）。
- [x] 3.9 #1365 既有目录形状 witness 推导在带路径段 prefix 下仍成立。
- [x] 3.10 **sidecar 层终局（#1394 AC-2，初稿遗漏）**：派生见证 key
      （`forcing/<source>/<cycle>/<basin>/<model>/forcing_package.json`）落在**目录**上时，
      该层不再"无 blocker"，而是发 `missing_forcing_package_uri` 且
      `unsafe_reason == "artifact_target_not_a_file"`，并被修复渠道拒绝（design.md D6）。
- [x] 3.11 两条 raw-manifest 腿（`:1519`/`:1587`）在新 token 下**abstain**
      （`:1526`/`:1594`），与 2.6 的三项元组一致。
- [x] 3.12 `normalize_key` 裸 key 支**不 unquote** 的守门用例（1.1 的 must-preserve）。
- [x] 3.13 把新的 local 腿种类 helper 加进
      `tests/test_production_scheduler.py:15288-15297` 的 `_ARTIFACT_GUARD_LANE_FUNCTIONS`
      meta-guard 元组（该元组枚举"local artifact guard 归一化流经的每个函数"并禁用 `Path.resolve()`）。
- [x] 3.14 M1-M5 逐条实测：**用精确源文本匹配施加变异并断言 `count == 1`**（不用行号——
      陈旧坐标会产出假的"变异体存活"），记录每条的转红 oracle 与形态；
      **M1 与 M3 按 design.md 记为预期连坐**（M1 一删则 3.1/3.10/3.11 同红；M3 连坐
      `:24435` helper 家族），二者均**不主张独占证死**。

## 4. spec 与登记

- [x] 4.1 `specs/job-retry-mechanism/spec.md` delta：拆分"会抛的 probe fault"与"存在但非常规文件"、
      新增 key 取景要求、local 腿 symlink carve-out、sidecar 层一句、
      以及"positive-determination-only"的场景。
      **显式记录**：另两条新 SHALL——「分类器不得构造 store / 不得抛」与「归一化须是单一共享推导」
      ——**有意只作 prose，不配场景**：前者是"永不发生"的否定式（场景只能写成同义反复，
      其真实 oracle 是 3.6 与代码评审），后者是**代码评审属性而非测试属性**（design.md M5 已记）。
- [x] 4.2 `openspec validate artifact-probe-file-kind-and-key-framing --strict --no-interactive` 通过。

> 原 4.2「为 local 腿收容/存在性错位开 issue」**已删**：该缺陷经 fixture review F1 实证**不存在**
> （`_realpath_or_none` 用 `realpath(strict=True)`，连最后一段一起解析，收容与存在性量在同一对象上）。

## Evidence Floor

实现前基线实测于 `aafb50f9`（本分支起点），下表的"只增不减"以此为准。

| 命令 | 期望 |
|---|---|
| `uv run pytest -q tests/test_production_scheduler.py tests/test_object_store_roots.py tests/test_state_manager.py` | 全绿；**基线 1811 passed**，只增不减 |
| `uv run pytest -q -k missing_forcing tests/test_production_scheduler.py` | 全绿；**基线 44 passed / 1619 deselected**（即该文件 1663 例），只增不减 |
| `tests/test_object_store_roots.py`、`tests/test_state_manager.py` | 上面第一行已含；它们是仓库里覆盖 `normalize_key` / `exists` 的现有套件，充当语义未变的守门 |
| `uv run ruff check .` | All checks passed |
| `openspec validate artifact-probe-file-kind-and-key-framing --strict --no-interactive` | valid |
| M1-M5 变异 | 逐条转红并由 3.1-3.11 中指定的 oracle 证死；**M1 与 M3 允许预期连坐**（M1→3.10/3.11，M3→`:24435` helper 家族；design.md 已记） |

db-free 纯文件态逻辑，本地 pytest 闭环；不涉 node-27 真实 DB，不涉 node-22 实机。
