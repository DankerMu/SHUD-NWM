# Design: artifact-guard-path-expanduser (#1424)

## Risk triage

- Fixture level：**compact**（issue 为 issue-scribe 产出、无上游
  `Suggested fixture level`；orchestrator 定级依据：S 规模单 seam、
  #1348 家族第 3 面、先例 #1402/#1401 均 compact——偏离方向记录在案）。
- Risk packs selected：oracle-integrity（红证 + 双触发面实测 +
  byte-compat oracle）、terminal-state-semantics（逃逸 → fail-closed 终
  态改判的确定性）。未选 spec-compliance 全量包：单 requirement delta，
  scenario↔seam 映射在 D3 内联即可覆盖，理由记录。

## D1 — 修法（推荐臂，issue 解决思路照抄并钉细节）

`_local_artifact_path` 两处：

| 行 | 旧 | 新 |
|---|---|---|
| `:1088`（file:// 臂，理论面） | `Path(unquote(parsed.path)).expanduser()` | `Path(os.path.expanduser(unquote(parsed.path)))` |
| `:1089`（裸路径臂，实证触发点） | `Path(value).expanduser()` | `Path(os.path.expanduser(value))` |

- 与 root 侧 `_realpath_or_none:1112` 首行逐字同款原语。
- `os.path.expanduser` 语义：成功臂与 `Path.expanduser` 返回等价字符
  串；失败臂（未知用户 / 无家目录）**原样返回**，不抛。
- must-preserve：可解析 `~` 与非 `~` 输入的返回值逐字节不变；
  `except (OSError, ValueError)` 臂、全部 reason 码、
  `_looks_like_local_uri_or_path` 准入判据、三条腿调用点零改动。

## D2 — 终态表（新旧对照；未展开串是**相对路径，终态是进程 cwd 的函数**——fixture review F2 实测钉定，用例必须 `monkeypatch.chdir` 锚定 cwd）

| 输入形态 | 旧行为 | 新行为 |
|---|---|---|
| `~<unknown-user>/...`（裸），**cwd 不在任何 allowed root 下** | `RuntimeError` 逃逸 → 整趟 pass 崩、零 evidence | 原样串 → 相对路径按 cwd 锚定 → containment 拒 → `(True, "local_artifact_path_outside_allowed_roots")`（fixture review 已实测证实；task 0(c) 复证） |
| 同上，**cwd 在某 allowed root 之下** | 同上逃逸 | `(True, None)`：cwd 锚定后被收容放行，按「不存在」落 null-reason（路由 rebuild/repair 通道）。具名接受：null-reason 语义 = 「被收容且探测过不存在」，与本 lane 既有 doctrine 一致 |
| 同上 + cwd 下恰有字面同名目录 | 同上逃逸 | `(False, None)` fail-open（守卫为该 uri 背书存在）。**具名残余**：需 operator 写 `~unknown` uri + cwd 在 root 下 + 字面同名目录三者并发，几乎不可达；报而不改，记 PR body 残余 |
| `~/...` 且 HOME 未设 + getpwuid KeyError（cwd 不在 root 下） | 同上逃逸 | 同行 1 fail-closed |
| `file://` 带 `~` netloc（`file://~user/o.json`） | 不抛（urlparse 把 `~user` 判 netloc，走不到展开）→ `(True, outside_allowed_roots)` | 不变（fixture review 19 形态实测：`parsed.path` 恒不以 `~` 开头，含 `file:///~user/...` → `/~user/...` 与 `%7E` 解码——`/~` 非 leading `~`，expanduser 恒 no-op） |
| 可解析 `~/...`（正常家目录） | 展开后走 containment | 逐字节同旧 |
| 非 `~` 输入（`/abs`、`file:///abs`） | 走 containment | 逐字节同旧 |
| root 与 path 喂同一 `~unknown` 串 | root 侧不抛（#1422 已修）、path 侧逃逸 | 两侧均不抛；cwd 锚定后两侧相等 → 收容放行 → 终态 `(True, None)`（**非** containment 拒——fixture review 实测；对称的含义 = 同规则同锚点，不是同 reason 码） |

## D3 — seams under test（scenario ↔ 用例映射）

1. 裸 `~nosuchuser_zz/output/summary.json` 经
   `_artifact_uri_missing_status`：不抛 + D2 行 1 终态（AC-1/AC-2a；
   Scenario 1）。**用例必须 `monkeypatch.chdir(tmp_path)` 锚定 cwd 于
   allowed roots 之外**，否则断言随执行目录漂移（F2）。
2. `HOME` 删除 + `pwd.getpwuid` monkeypatch 抛 `KeyError` 下普通
   `~/output/summary.json`：不抛 + D2 行 4 终态（AC-2b；Scenario 2）。
   同样 chdir 锚定。
3. byte-compat oracle：可解析 `~` 与非 `~` 输入在新旧实现下返回值等价
   （旧实现独立转写对照，非自比；Scenario 3）。oracle 输入域**显式排除
   `HOME=''`**（`~//x` 在旧 `/x` 新 `//x` 的 POSIX 双斜杠语义差，落在可
   达输入域外——fixture review 审点 4 实测）。
4. 钉测扩面：receiver 判别式（见 proposal——lane 内 `expanduser` 仅
   `os.path.expanduser` receiver 合法；`_realpath_or_none:1112` 在判据
   下合法，post-fix `_local_artifact_path` 两处同样合法，任何
   `<Path>.expanduser()` 红）（AC-3）。
5. 对称断言：root 侧（resource_profile roots）与 path 侧喂同一
   `~unknown` 串，两侧均不抛且终态 = D2 行 8 `(True, None)`（AC-4；
   Scenario 4；chdir 锚定）。
6. 回归：`test_local_artifact_guard_lane_contains_no_path_resolve_call`
   与 #1402 `_realpath_or_none` 矩阵保持绿（AC-5）。

## D4 — 红证

- R1：核心两行回退为 `Path(...).expanduser()` → seam 1/2/4/5 用例红
  （钉测 + 双触发面 + 对称）。
- R2：except 臂扩 `RuntimeError`（备选臂冒充修复）→ seam 1 期望 reason
  断言红（塌缩成 `local_artifact_path_unresolvable`，与 D2 终态表不
  符）——证明测试判别的是推荐臂而非「不抛」本身。

## Task 0 探针（实现前）

- (a) 复证 issue 证据 2：两触发面在本机 CPython 直调生产函数逃逸。
- (b) 复证 `_realpath_or_none:1112` 首行原语与修法一致。
- (c) 复证 D2 行 1/2/8 三格终态（fixture review 已实测一轮；实现前在
  chdir 锚定下重跑一遍确认）——任一格不符停下重裁。
- (d) 复证钉测 `_resolve_call_names` 结构（:12633-12638）以构造
  receiver 判别式变体（**非同构复制**——attr 名全禁在正确修复码上红，
  F1）。
- 任一探针与 design 断言不符 → 停下报告重裁。

## Non-goals

见 proposal Out of scope。
