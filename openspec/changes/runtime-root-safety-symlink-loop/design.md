# Design: runtime-root-safety-symlink-loop (#1401)

## D0 — 现场与裁决（探针先行，task 0）

缺陷点唯一：`services/orchestrator/retry.py:1456-1464`
`_local_runtime_root_safety`。消费链两条腿共用：

- DB 腿：`_resolve_runtime_root_candidate:1399`（唯一直接调用点，grep 复
  证）→ `_resolve_retry_runtime_roots:679` → `_submit_retry_job:653`（在
  gateway try `:659` 之外）→ `attempt_manual_retry` 宽 `except
  Exception`（`:551-559`）。
- db-free 日志腿：`file_orchestration_journal.py:89` import
  `_resolve_runtime_root_candidate`（调用面
  `_resolve_file_retry_runtime_roots:7272` 区域，issue 快照行号 :7044 已
  漂移），同形宽 except。

unsafe 接线已存在且零改动：`safety[0] is None → unsafe_rejected=True +
rejection(reason=safety[1])`（`:1400-1403`）→
`_RetryRuntimeRootResolutionError(RETRY_RUNTIME_ROOTS_UNSAFE, evidence)`
（`:767-772`）→ `_retry_submission_error_code` 取 `.code`（`:1167-1173`）
→ `details["runtime_root_resolution"]` 证据附着。**本 design 只改判据函数
本体，不触碰任何接线。**

**具名边界 rider（`:1457` expanduser）**：`Path(value).expanduser()` 对
`~<未知用户>/...` 两臂抛 RuntimeError（#1424 同族抛点；#1402 round-1
A2/#1424 已实测该原语）。该行在 issue in-scope 行区间 `:1456-1464` 内。
处置：换 `os.path.expanduser(value)`（不抛，未知用户原样返回）——
`~zz/x` 展开失败后非绝对路径 → 既有 `relative_local_root` fail-closed 出
口。与 PR #1422 root 侧同款处置；若不改，本 change 的 spec「规范化故障不
以异常逃逸」断言将重蹈 #1402 A2 的假穷尽覆辙（helper 内留一个已知抛
点）。记入 PR 偏离记录。out-of-scope 的同族 expanduser 抛点：
`retry.py:1524`（allowed-roots lane）——**不改**，路由至 #1424 评论补
面。

**phantom 形态裁决（fixture review P1-1，与 #1402/preflight 家族先例
显式分歧）**：`"<不存在>/../<loop>"` 形态 root 的 strict 解析先命中缺失
分量（errno=ENOENT）。若照搬 #1402/`scheduler_preflight.py:539-543` 的
「残留记录不修」裁决，本 lane 会出现 **≤3.12 生产臂 fail-closed →
fail-open 退化**：旧行为该形态抛 RuntimeError（宽 except 挡下提交），新
行为 admit 带环值进 `comparable_local_roots` 与提交 manifest（fixture
review 实测：3.11 OLD RAISED / NEW ok）。artifact-guard lane 的残留后果
面是 verdict 路由口味；本 lane 是 manifest fail-open——后果面不同，裁决
不同。**本 design 消除该残留**：ENOENT 回退结果再做一次 strict realpath
复查，仅 ENOENT 可再 admit（loop-filtered admit，见 D1）；phantom 两臂
均拒。家族分歧具名入 PR 偏离记录。

## D1 — 判据函数改造

```python
def _local_runtime_root_safety(value: str) -> tuple[str | None, str]:
    try:
        expanded = os.path.expanduser(value)
    except ValueError:
        return None, "unresolvable_local_root"
    path = Path(expanded)
    if not path.is_absolute():
        reason = "parent_traversal_local_root" if ".." in path.parts else "relative_local_root"
        return None, reason
    try:
        return os.path.realpath(expanded, strict=True), "ok"
    except ValueError:
        return None, "unresolvable_local_root"
    except OSError as exc:
        if getattr(exc, "errno", None) != ENOENT:
            return None, "unresolvable_local_root"
    try:
        fallback = os.path.realpath(expanded)
    except (ValueError, OSError):
        return None, "unresolvable_local_root"
    try:
        os.path.realpath(fallback, strict=True)
    except ValueError:
        return None, "unresolvable_local_root"
    except OSError as exc:
        if getattr(exc, "errno", None) != ENOENT:
            return None, "unresolvable_local_root"
    return fallback, "ok"
```

- `ENOENT` 用 `services/orchestrator/retry.py:8` **既有** import（#1399
  落地）——不新增 import（fixture review note：重复 import 触发 F811）。
- 返回值形状 `(str | None, str)`、既有 reason 三元组不变；"ok" 臂返回值
  恒为 strict 成功结果或非 strict `fallback`——后者与旧
  `str(Path.resolve(strict=False))` byte-compat（三臂已证），复查臂只做
  errno 过滤**不改返回值**，byte-compat 由构造保证。
- **loop-filtered admit（P1-1 修法 a）**：ENOENT 回退结果 `fallback` 再
  过一次 strict realpath，仅 ENOENT（root 确实尚未创建）再 admit；
  ELOOP/EACCES 等 → 拒。`"<missing>/../<real>"` 形态：fallback =
  `<real>`（存在）→ 复查 strict 成功 → admit，值与旧行为相同。
- **结构穷尽（P2-3，closure check P2-A 更正；implementer 偏离 #5 补
  正——非 strict 回退调用同样套 try，NUL+ENOENT 复合形态
  `/srv/…/object\x00store` 实测在裸调用形下逃逸）**：每个 realpath 调用
  （含非 strict 回退）都在自己的 `except (ValueError, OSError)` 覆盖内；`os.path.expanduser` 对
  `~<NUL>` 前缀形态会抛 ValueError（closure check 实测反例
  `'~\x00zz/x'`——「不抛」论断为假），故 expanduser 也套自己的
  `except ValueError`；`Path()` 构造与 `is_absolute()` 纯词法——「helper
  内无抛点」由代码形直接支撑，非论证穷尽。
- `except ValueError`：NUL-byte 输入 realpath 抛 ValueError；旧代码同样
  接不住、逃逸宽 except。fail-closed 硬化；输入域备注：root 值来自 DB
  row/env，PG jsonb 与 execve 均拒 NUL——实际不可达，纯防御。具名入 PR
  偏离记录。

## D2 — 出口语义（完整枚举）

| # | 输入形态 | 旧 3.13+ | 旧 ≤3.12 | 新（两臂同判决） |
|---|---|---|---|---|
| 1 | 相对路径 | `(None, "relative_local_root")` | 同 | 不变 |
| 2 | 相对含 `..` | `(None, "parent_traversal_local_root")` | 同 | 不变 |
| 3 | 绝对正常路径 | `(resolved, "ok")` | 同 | `(realpath, "ok")` byte-compat |
| 4 | 绝对 ENOENT（root 尚未创建/NFS 未挂载） | `(词法 resolved, "ok")` | 同 | `(非 strict realpath, "ok")` byte-compat（admitted 语义保留，复查臂 ENOENT 再 admit） |
| 4b | `"<missing>/../<real>"`（缺失分量先于存在目标） | `(<real>, "ok")` | 同 | `(<real>, "ok")`（复查 strict 成功，byte-compat） |
| 5 | 绝对 symlink 环 | `(环路原样, "ok")` **fail-open 进 manifest** | RuntimeError 逃逸 | `(None, "unresolvable_local_root")` |
| 6 | 绝对非 ENOENT errno（EACCES/ENOTDIR/ESTALE/EPERM） | **静默收编 `(路径, "ok")`** | **同——两臂都收编**（fixture review P2-1 实测更正：≤3.12 `Path.resolve(strict=False)` 仅 ELOOP 经 check_eloop 转 RuntimeError，其余 OSError 被内部 `p.stat()` except 吞掉，「部分抛型逃逸」不存在） | `(None, "unresolvable_local_root")`——**全版本 admit→reject 扩面**（非「3.13+ 补齐」）；issue 推荐臂授权「其余 errno」，且与同族 `scheduler_runtime_roots.py:410-416`（EACCES/EPERM→NOT_WRITABLE、ELOOP/ENOTDIR→UNSAFE_PATH 均 blocker）一致；具名入 PR 偏离记录 |
| 7 | `~<未知用户>/...`（rider） | RuntimeError 逃逸 | 同 | `(None, "relative_local_root")`（展开失败非绝对，fail-closed） |
| 8 | 含 NUL 字节（含 `~<NUL>` 前缀形态，closure check P2-A） | ValueError 逃逸 | 同 | `(None, "unresolvable_local_root")`（防御性，输入域外） |
| 9 | phantom `"<missing>/../<loop>"` | `(带环值, "ok")` 收编 | **RuntimeError 逃逸（fail-closed 挡下提交）** | `(None, "unresolvable_local_root")`（loop-filtered admit 复查拒收；消除 D0 所述 ≤3.12 fail-open 退化） |
| 9b | `"<missing>/../<非环故障>"`（复查非 ENOENT 非环，如 EACCES） | `(路径, "ok")` 收编 | 同 | `(None, "unresolvable_local_root")`（复查臂统一拒收非 ENOENT，closure check 实测存在） |

下游语义（零代码改动，测试钉住）：行 5/6/8/9 → `unsafe_rejected` →
`RETRY_RUNTIME_ROOTS_UNSAFE` + `details.runtime_root_resolution` 携
rejection（field/source/reason/value 形状同既有 parent-traversal 锁
`tests/test_retry.py:1229-1232`）；该 root 不进
`comparable_local_roots`、不进该候选的 `resolved`/manifest_fields。

**候选级语义限定（fixture review P2-2）**：unsafe rejection 是**候选级**
的。`_resolve_retry_runtime_roots` 多候选时，含坏 root 的候选被跳过
（`retry.py:724-730` 守卫 + continue），若后续存在完整候选则提交**照常成功**
（`:748` return），rejection 仍记入 evidence 的 `rejected` 列表；
`RETRY_RUNTIME_ROOTS_UNSAFE` 终态仅在**无完整候选可用**时出现
（`:767-772`）。spec 场景与 D4 测试均按此口径表述，不写绝对句。

## D3 — 逃逸链收口

修复后 helper 内无抛点，且为**结构穷尽**（D1：每个 realpath 调用各有
`except (ValueError, OSError)` 覆盖；`os.path.expanduser` 不抛；
`is_absolute` 纯词法）。两条腿的宽 `except Exception` 对这些输入不再到
达；结构化出口 `RETRY_RUNTIME_ROOTS_UNSAFE` 与 evidence 附着
（`:669-671` 臂）恢复执行。
**不**在调用方加兜底 except——判据函数自身收口即够，宽兜底会把未来新缺
陷静默降级（#1365 doctrine 同理）。

## D4 — 测试面（tests/test_retry.py）

**既有锁（必须全绿，64 个既有用例零重判——`def test_` 计 62，`:105` 三
参 parametrize 实收 64）**：`:1229` 是 UNSAFE 出口的
parent-traversal 形态锁；`:929`/`:974`/`:1270` 锁的是
`resolves_to_workspace_dir` 重叠守卫（fixture review note 更正——此前误
标为 parent/相对形态锁）；全仓无 `relative_local_root` 既有锁。最受本改
动影响的是重叠守卫锁：`<tmp>/alias/../workspace` 形态经 ENOENT 回退臂词
法 pop 后重叠等式保持（review 仿真已证，实现后以实跑复证）。

新增：

1. 单元矩阵（D2 行 1-8 + 4b 逐行，真实 tmp_path symlink 环；EACCES 用
   `chmod 0o000` 父目录 + `geteuid()==0` skip，先例
   `tests/test_first_cycle_initial_state_audit.py:495`；finally 复原权
   限）。
2. phantom 拒收钉（D2 行 9：`(None, "unresolvable_local_root")` 两臂同
   判决——loop-filtered admit 的判别用例，兼防 ≤3.12 fail-open 退化回
   潮）。
3. 三 field 覆盖：`workspace_dir`/`object_store_root`/
   `published_artifact_root` 环 root 各自 `(None,
   "unresolvable_local_root")`（参数化）。
4. DB 腿 e2e：`attempt_manual_retry` + 环 `workspace_dir` →
   `retry.error_code == "RETRY_RUNTIME_ROOTS_UNSAFE"` 且
   `details["runtime_root_resolution"]` 非空、rejection reason ==
   `unresolvable_local_root`（模式照抄既有 `:1229` 用例）。
5. db-free 日志腿同形断言一条——放
   `tests/test_file_orchestration_journal.py`（fixture review P1-2 更
   正：`tests/test_retry.py` 五处 UNSAFE 全是 DB 腿；journal 腿测试供体
   是该文件 `:3802`/`:3985` 的 `RETRY_RUNTIME_ROOTS_UNRESOLVED` 形状，
   照搬结构、错误码换 UNSAFE、输入换环 root）。
6. manifest/比较基准排除：环 root 不出现在
   `resolution.resolved`/`comparable_local_roots`（单元级，调
   `_resolve_runtime_root_candidate`）。
7. rider：`~<未知用户>` → `(None, "relative_local_root")` 不抛。

## D5 — seams under test

1. 环 root 不抛（≤3.12 逃逸臂红转绿）→ D4 #1/#4。
2. 环 root 拒进 manifest/比较基准（3.13+ fail-open 臂红转绿）→ D4 #6。
3. `unresolvable_local_root` 出口首次可达（死码复活）→ D4 #1/#3。
4. ENOENT admitted 语义回归钉 → D4 #1 行 4。
5. 非 ENOENT errno 扩面（EACCES 实证）→ D4 #1 行 6。
6. 结构化错误码 + evidence 恢复（DB 腿）→ D4 #4。
7. db-free 日志腿同判决 → D4 #5。
8. rider expanduser fail-closed → D4 #7。
9. phantom 拒收（loop-filtered admit 判别）→ D4 #2。

## D6 — 红证

- R1：helper 回退为 `str(path.resolve(strict=False))` + `except OSError`
  → **两臂**红（3.13+：seam 2/3/5 收编面；≤3.12：seam 1/6 逃逸面，
  RuntimeError）。双臂 receipt 留存（本机 3.14 + `uv run --python 3.11`
  scratch venv）。
- R2：删 ENOENT 回退臂（全部 OSError → None）→ seam 4 红（admitted 语义
  锁真锁）。
- R3：rider 回退 `Path(value).expanduser()` → seam 8 红（RuntimeError）。
- R4：删 loop-filtered 复查臂（ENOENT 直接 admit fallback）→ seam 9 红
  （phantom 拒收钉是复查臂唯一判别器）。
- 全程 `git stash list` 空、mutation 还原自证。

## Non-goals

- `retry.py:1562` 区域 db-free selector path 判据（#1400）；
  `retry.py:1524` allowed-roots lane 的 expanduser 抛点（路由 #1424 补
  面）；parent 级三处（两次裁定 scope out）；重叠守卫 inode 语义
  （#1192）；`_scheduler_root_os_error_reason` 细化 reason（issue 备选，
  KISS 不采——单一 reason 已满足 AC 与路由需求）。
- evidence schema、journal 结构、`_retry_submission_manifest` 不动。

## Evidence mapping（Required evidence per pack）

- oracle-integrity：task 0 双臂探针 + D6 三组红证 + mutation 还原自证。
- spec-compliance：spec delta 五场景 ↔ seams（场景 1↔seam 1/6/7/8（GIVEN
  含 tilde 形态）、场景 2↔seam 2/3、场景 3↔seam 4 含 4b、场景 4
  phantom↔seam 9、场景 5 权限↔seam 5）+ AC 对照。
- terminal-state-semantics：D2 出口表逐行测试 + 错误码/evidence 终态断言
  （seam 6）。
