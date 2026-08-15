# Design: artifact-guard-symlink-loop-roots (#1402)

## Risk packs

- Selected: oracle-integrity（跨版本臂红证 + 突变敏感性——同族 #1348 的
  核心风险轴）· spec-compliance（新 reason 必须落 #1365 doctrine 与
  runbook 路由）· terminal-state-semantics（fail-closed 出口形状与授权修
  复通道拒收语义）。
- Not selected: concurrency（单线程判据函数）· performance（每候选 ≤7 个
  root 的一次性规范化）· data-migration（无持久化变更）· security（不引
  入新输入面）。

## D0 — 现状清单（master 9fd13667 实测行号）

Lane 内三处 symlink-loop-unsafe 解析，全部在
`services/orchestrator/scheduler_state_failure.py`：

| 位置 | 函数 | 形式 | 后果 |
|---|---|---|---|
| `:1091` | `_local_artifact_path_is_allowed` | `path.resolve(strict=False)` | path 侧崩溃/收编 |
| `:1120` | `_local_artifact_allowed_roots` | `Path(text).expanduser().resolve(strict=False)`，`except (OSError, ValueError)` 接不住 RuntimeError | root 侧崩溃/收编（缺陷主点） |
| `:1133` | `_path_is_relative_to` | 双侧 `resolve(strict=False)` | **必须一并治（fixture review P2-5 更正理由）**：D1 之后普通环 case 两实参已是 `_realpath_or_none` 产物走不到此；真实残留是 **ENOENT 回退臂**——形如 `"<不存在>/../<loop>"` 的 root 走 ENOENT 臂后仍是带环路径（非 strict realpath 不抛、原样返回），`:1133` 的 `root.resolve(strict=False)` 在 ≤3.12 照旧抛无 errno RuntimeError，`except ValueError` 接不住——lane 内真实残留逃逸点（`scheduler_preflight.py:539-543` 注释记的同一残留）。**须在 PR body 记具名边界偏离**（issue「受影响面」句有歧义，此处 in-scope 且必要） |

关键事实：`_path_is_relative_to` 为**模块私有，本模块内唯一消费者
`:1093`**（全仓另有 8 份同名副本，无跨模块 import，
`tests/test_two_node_e2e_evidence.py` 的反射打的是 production_closure 那
份——均 out of scope）。lane 自洽可整体改造，无外溢。消费链：
`_artifact_uri_missing_status` local 腿（`:1055-1061`）→
`_local_artifact_path_is_allowed`（`:1090-1093`）→
`_local_artifact_allowed_roots`（`:1096-1129`）。三条消费腿（forcing
sidecar `:519` / forcing journal-direct `:599` / copyback `:643`）共用，
修一处覆盖三腿。版本矩阵（issue 证据 2，task 0 复证）：≤3.12 非 strict
resolve 遇环抛无 errno RuntimeError；3.13+ 静默收编。
`os.path.realpath(strict=True)` 两臂均抛 `OSError(ELOOP)`——范式可用。

## D1 — 规范化判据（单一 helper，lane 级）

新增模块级 helper（形状照搬 `scheduler_preflight.py:516-560` 已合入范
式）：

```python
# 注：scheduler_state_failure.py 现未 import errno；照 scheduler_preflight.py
# 用 `from errno import ENOENT`（fixture review P3-4/NEW-1，防 ruff F821）。
def _realpath_or_none(text: str) -> Path | None:
    """strict realpath；ENOENT 回退非 strict；其余 errno（ELOOP/EACCES/…）返回 None。"""
    expanded = os.path.expanduser(text)
    try:
        return Path(os.path.realpath(expanded, strict=True))
    except OSError as exc:
        if getattr(exc, "errno", None) == ENOENT:  # 合入范式同款（preflight）
            return Path(os.path.realpath(expanded))  # 根尚未创建/NFS 未挂载：既有 admitted 语义
        return None
```

裁决：

- **ENOENT 回退臂必须保留**——现网 root 可以指向尚未创建的目录（object
  store 首启、NFS 挂载竞态），现行 `strict=False` 对不存在路径不抛、词法
  规范化后照常入 roots；丢掉该臂是行为收紧越权（且 #1399 同族已裁过同
  款）。非 strict `os.path.realpath()` 对 symlink 环**不抛不循环**（内部
  有环检测，返回部分解析路径）——task 0 探针须复证该断言，两臂均不得再
  抛。
- **`Path.resolve()` 两种形式全禁**（AC-1）：lane 内规范化仅经
  `_realpath_or_none`。
- `_path_is_relative_to` 改纯词法（`relative_to` 直接比较，不再自行
  resolve）——两个实参在调用点均已是 `_realpath_or_none` 产物。签名与
  异常语义（`except ValueError: return False`）不变。

## D2 — tri-state 出口分流（AC-2/3/4 的裁决表）

`_local_artifact_allowed_roots` 返回形改为
`tuple[tuple[Path, ...], bool]`（`(resolved_roots, any_root_unresolvable)`；
内部私有函数，无外部签名约束——grep 确认全仓仅 `:1092` 一个调用点）。
`_local_artifact_path_is_allowed` 同步改造并把 tri-state 传给消费者。
`_artifact_uri_missing_status` local 腿出口表（**完整枚举**，实现与测试
逐行对齐）：

| # | path 规范化 | roots 状态 | 收容判定 | 出口 | 语义 |
|---|---|---|---|---|---|
| 1 | OK | 全部可解析，非空 | 在某 root 下 | `(not path.exists(), None)` | 现行为不变 |
| 2 | OK | 全部可解析，非空 | 不在任何 root 下 | `(True, "local_artifact_path_outside_allowed_roots")` | 现 reason 收严为「根可解析且确在外」（AC-4） |
| 3 | OK | 存在不可解析 root | 在某**可解析** root 下 | `(not path.exists(), None)` | 可解析 root 的收容裁决不受坏 root 污染 |
| 4 | OK | 存在不可解析 root | 不在任何可解析 root 下 | `(True, "local_artifact_root_unresolvable")` | **新 reason**（AC-3）：非空 → 授权修复通道拒收（#1365 doctrine），运维路由「查 root 本身（环/权限），非查产物摆放」 |
| 5a | OK | 配置非空但全部不可解析 | — | `(True, "local_artifact_root_unresolvable")` | #4 的极端形（`any_root_unresolvable` 为真） |
| 5b | OK | root 列表本来为空（全未配置/全空串） | — | `(True, "local_artifact_path_outside_allowed_roots")` | 现行 `bool(roots)==False` 行为**不改**（现网常见路径，回归钉） |
| 6 | path 自身不可规范化（ELOOP 等） | **无 root 故障（含 roots 为空）** | — | `(True, "local_artifact_path_unresolvable")` | 既有 reason 保留「path 问题」语义（root 问题走 #4，两类可区分） |

**判序限定（round-1 review B2，round-2 C3 更正）**：实现判序为可解析
root 收容成功 → root 故障 → path 故障 → outside/空 roots；「roots 为空 × path 自身不可规范化」落行 #6
（`local_artifact_path_unresolvable`），**不**落行 5b——行 5b 仅在 path
规范化成功时到达。这是相对 master 的行为变化：master 该组合在 ≤3.12 整
链抛 RuntimeError，3.13+ 报 `outside_allowed_roots`；HEAD 报 path 口味
reason（两者均非空、修复通道同拒收，仅路由文案差异），参数化行钉住。

**优先级规则（fixture review P1-1）**：`any_root_unresolvable` 为真时
root reason 优先——同一输入可能同时命中 root 故障与 path 故障（产物在
环路 root 之下时 path 侧 strict realpath 同样 ELOOP → None），此时出
`local_artifact_root_unresolvable`；path 侧 `None` 仅在无 root 故障时出
`local_artifact_path_unresolvable`。spec 场景 2（环下/环外均出 root
reason）与场景 4（GIVEN 限定 roots 全可解析）与此一致。

裁决理由（#4 vs 静默丢弃）：issue 推荐臂明确「丢弃该 root 并产出可区分
reason」——若只丢弃，产物落在坏 root 之外时会退化成
`outside_allowed_roots`，运维被路由去查摆放（issue 证据 4 的误路由原
样保留）。tri-state 是消除两种误路由的最小结构。

已知残留（与 `scheduler_preflight.py:539-543` 同一裁决，记录不修）：
`"<不存在>/../<loop>"` 形态 root 经 ENOENT 臂非 strict realpath admit 后
成为**仍带环的** phantom containment base；该 root 本身永不触发
root-fault reason（`any_root_unresolvable` 不置位），此后 verdict 由产物
自身规范化结果决定，三出口均可达：产物路径同走 ENOENT 臂落 base 之下 →
行 #1/#3（null reason）；产物直接穿环（自身 strict ELOOP → None）→ 行
#6 `local_artifact_path_unresolvable`；产物在外 → 行 #2
`local_artifact_path_outside_allowed_roots`——spec delta 同款 carve-out
（round-1 A1，round-2 C1 三出口更正）。

非 ELOOP 的其余 errno（EACCES/ESTALE/ENOTDIR）同样落 root-unresolvable
臂——超出 symlink 环的 fail-closed 行为变化，issue 推荐臂已授权
（「其余 errno（ELOOP/EACCES/…）」），runbook 条目须写明（NFS ESTALE 期
间运维看到新 reason 时的排查路径）。

## D3 — 逃逸链收口（AC-6）

修复后 lane 的**规范化面**无抛点（`_realpath_or_none` 吞 OSError；
`_path_is_relative_to` 纯词法只可能 ValueError 且已接），issue 证据 3
的整链逃逸（`run_once` 中止、evidence 不落盘、`run_continuous` 退出）在
本 issue 的触发原语（`Path.resolve`）上根除。已知残留抛点（既存缺陷，
不在本 diff 内，round-1 review 发现并已立单 **#1424**）：
`scheduler_state_failure.py:1089` `_local_artifact_path` 的
`Path(value).expanduser()` 对 `~<不存在用户>/...` 形态裸路径值两臂抛
RuntimeError 穿透 `:1062`（`file://` 臂经 urlparse 剥离 netloc 不抛）。
**不**在 `_artifact_uri_missing_status` local 腿加宽兜底
except（备选臂的止血形）——宽兜底会把未来新缺陷静默降级为
`local_artifact_path_unresolvable`，与 #1365「可区分证据」doctrine 相
悖；本 design 以「规范化面无抛点 + 残留抛点具名立单」承接 AC，e2e 测试
以真实环 root 跑 `run_once` 级路径证明 pass 存续。

## D4 — 测试面

**既有回归锁（fixture review P2-1 更正——「零测试锁」不成立）**：三条既
有用例逐字钉住本守卫出口
（`tests/test_production_scheduler.py:12330`/`:12520`/`:12563`，断言
`unsafe_reason == "local_artifact_path_outside_allowed_roots"`，构造均为
可解析 roots + 产物在外 = D2 行 #2）——改造后**必须保持绿**（AC-4 收严
的既有语义锁），task 7 全量复跑覆盖。

新增测试（`tests/test_production_scheduler.py`，命名对齐既有
`symlink_loop` 用例族）：

1. **单元矩阵**（`_local_artifact_allowed_roots` / `_realpath_or_none`）：
   真实 symlink 环 root（`a -> b -> a`）→ 不抛、标记 unresolvable；
   ENOENT root → 词法规范化入 roots（既有语义钉住）；正常 root → 入
   roots；混合（好+坏）→ 好 root 保留 + 标记为真。
2. **出口表逐行**（D2 #1-#6）：`_artifact_uri_missing_status` local 腿以
   构造 candidate（resource_profile 注入 root）逐行断言 `(missing,
   unsafe_reason)`。
3. **e2e 腿**（AC-6，copyback 腿）：真实环 root 作
   `NHMS_OBJECT_STORE_COPYBACK_ROOT` + 越界 copyback source →
   `run_once` 级决策路径不抛、pass 继续、决策为 blocked 且 reason 属
   D2 表（非 traceback）。同趟第二候选正常调度（「单候选坏 root 不再连
   坐整趟」的判别断言）。
4. **授权修复通道拒收（fixture review P2-2 更正——必须用 forcing
   腿）**：拒收点 `scheduler_candidates.py:1617` 的前置门
   `_decision_is_stable_missing_forcing_blocker`（`:1460-1480`）要求
   reason ∈ {missing_forcing_package_uri, forcing_version_row_absent} +
   `artifact_type == "forcing_package_uri"` + `restart_stage ==
   "forecast"`——copyback 腿的 `missing_copyback_source` 永远走不到
   `:1617`（在 `:1615` 以 `missing_forcing_blocker_contract_invalid` 被
   拒，非本 AC 要证的拒收）。用例用 **forcing 腿本地 uri**（journal/
   direct tier，落 `missing_forcing_package_uri` 携新 unsafe_reason），
   断言 rejection == `forcing_artifact_reference_unsafe` 且
   `unsafe_reason == "local_artifact_root_unresolvable"`。「零代码改动即
   被拒收」判断成立（`:1617` 只看 `not in (None, "")`）。
5. **跨版本双臂（fixture review P2-3 更正）**：本 worktree venv 为
   3.14（3.13+ 收编臂），≤3.12 崩溃臂（= 全部生产解释器）本地默认跑不
   到。测试仅用两臂行为一致的原语（realpath strict=True 环 → OSError
   ELOOP 四臂同型，fixture review 探针已证），不写版本分支断言；
   **task 0/6/7 显式加 ≤3.12 臂命令** `uv run --python 3.11 pytest -q
   tests/test_production_scheduler.py -k "artifact or symlink"`（3.11 可
   装：onnxruntime==1.19.2 有 cp311 wheel），**两臂 receipt 同时留存**作
   为 AC-2「两版同结论」证据。

## D5 — seams under test

1. root 环 → 不抛（≤3.12 崩溃臂红转绿）。
2. root 环 + 产物在环外 → `local_artifact_root_unresolvable`（3.13+ 误报
   `outside_allowed_roots` 臂红转绿）。
2b. 非 ENOENT errno root（EACCES 实证用例；ESTALE/ENOTDIR 走同一 errno
   分流臂）→ 同 root-fault 出口（round-1 B4；spec 场景 2 GIVEN
   「permission fault」的唯一见证）。
3. root 环 + 产物在环下 → 同 reason 非空（3.13+ `(True, None)` 喂
   rebuild 臂红转绿）。
4. ENOENT root 词法 admitted（既有语义回归钉）。
5. 好+坏混合 roots：好 root 收容不受污染（D2 #3）。
5b. 三类 root 共存（可解析 + ENOENT-admitted + 环）互不污染：good 下
   `(False, None)`、admitted 下 `(True, None)`（round-1 B3；spec 场景 3
   复合 GIVEN 的见证用例）。
6. 真「根可解析产物在外」仍 `outside_allowed_roots`（reason 收严后语义
   钉）。
7. path 自身环（roots 全可解析）→ `local_artifact_path_unresolvable`
   （path/root 两类可区分；roots 有故障时 root reason 优先，见 D2 优先
   级规则）。
8. e2e：pass 存续 + 邻座候选正常调度 + per-tick evidence 真实写出
   （`result.artifact_path` 非 None 且落盘——round-1 review B1：counts
   断言不蕴含写出，runtime `:1416-1428` 有 `artifact_path=None` 分支仍
   返回完整 counts）。
9. 修复通道拒收新 reason（forcing 腿，D4 #4）。
10. `_path_is_relative_to` 纯词法（fixture review P3-2 更正测试形）：
    **源码断言**（函数体内无 `resolve`）+ **调用者级钉**（含 `..` 逃逸
    的产物 uri 经 `_realpath_or_none` 归一后仍判
    `outside_allowed_roots`——纯词法版单看本函数更宽松，防线在调用点的
    先归一）。

## D6 — 红证

- R1：`_realpath_or_none` 回退为 `Path.resolve(strict=False)` →
  **3.13+ 臂（本机 venv）**：seam 2/3 红（静默收编）；**≤3.12 臂
  （`uv run --python 3.11`）**：seam 1 红（RuntimeError 逃逸）；e2e 腿两
  臂各红。两臂 receipt 均留存（fixture review P2-3）。
- R2：D2 #4 出口改回 `(True, None)` → seam 3/9 红。
- R3：`_path_is_relative_to` 恢复双侧 resolve → seam 10 源码断言红；
  ≤3.12 臂 `"<不存在>/../<loop>"` root 形态（D0 ENOENT 回退残留）逃逸
  红。
- 全程 `git stash list` 空。

## Non-goals

- `:1059` `return not path.exists(), None` 的目录 fail-open（#1394）——
  本 PR 不改该行的存在性判据（注意 `:1057` 的
  `_local_artifact_path_is_allowed` 调用行因签名改造**必然被动**，
  fixture review P2-4 更正行号）。
- `scheduler_runtime_roots.py` / `scheduler_config.py` / `retry.py` 的
  allowed-roots 级三处（#1348/PR #1399 已治）与 `_local_runtime_root_safety`
  （#1401）、db-free path 级两处（#1400）。
- 其他 8 文件的 `_path_is_relative_to` 同名副本（issue 已登记，独立曲
  面）。
- evidence schema、object 腿、`_object_manifest_is_missing` 不动。

## Evidence mapping

- oracle-integrity：D4 单元矩阵 + D6 三组红证 + e2e 判别断言。
- spec-compliance：spec delta 四场景 ↔ seams（场景 1↔seam 1/8、场景
  2↔seam 2/2b/3/9、场景 3↔seam 4/5/5b/6、场景 4↔seam 7）+ runbook 路由表条
  目（closure check 观察更正：场景 4 是 path-fault = seam 7，seam 9 归
  场景 2 末句）。
- terminal-state-semantics：D2 完整出口表逐行测试 + D3 规范化面无抛点
  （残留抛点 #1424 具名）承接 + seam 8 pass 存续。
