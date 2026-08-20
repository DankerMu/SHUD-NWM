# 路径展开/解析抛型面家族收口（#1547 + #1549 + #1544 + #1546 + #1545）

## Why

`#1332 → #1423 → #1424/PR #1435 → #1436/#1441（PR #1548）→ #1520（PR #1541）` 这条家族链
逐轮收敛「裸 `Path.expanduser()` / 裸 `Path.resolve()` 的抛型面」。五个残留单同域、同证据链、
同两条文件，合批交付。

共同的双重失效预设是：**「`expanduser()` / 非 strict `resolve()` 失败时抛 `OSError`」**。两条都不成立：

- `Path.expanduser()` 在家目录不可确定时（`~<不存在用户>/…`，或无 passwd 条目且 `HOME` 未设）
  抛的是**无 `errno` 的裸 `RuntimeError`**（`"Could not determine home directory."`）。
- 非 strict `Path.resolve()` 遇 symlink 环，**≤3.12 抛无 errno 的 `RuntimeError`**，
  **3.13+（GH-113838）根本不抛**、原样收编环路。

方向性陷阱（两条链共用）：`SafeFilesystemError` **是** `RuntimeError` 的子类
（`packages/common/safe_fs.py:10`），反向不成立 —— 所以 `except SafeFilesystemError` 一律接不住裸抛，
而宽接 `RuntimeError` 的调用方读 `error.kind` 会 `AttributeError`。

## What Changes

**五个站点是四种不同的目标语义，不是一种修法的五次复制。** 这是本单最大的实现风险，
逐条口径见 design「四种语义，禁止统一」。

| 单 | 站点（**按函数名锚定，行号会漂**） | 目标语义 |
|---|---|---|
| #1547 | `packages/common/safe_fs.py` `_expand_path` | **抛**：`SafeFilesystemError(kind="unsafe")` |
| #1549 | `scheduler_runtime_roots.py` `_optional_config_path`（活，`allowed_storage_roots`）、`_config_path_relative_to_preserve_final`（活，`log_root`）、`_config_path_preserve_final_component`（兼容面，见下） | **不抛**：db-backed 臂收敛到 db-free 臂，构造成功、产物逐字相等，分类仍留给 preflight |
| #1544 | 同文件 `_require_safe_directory_final_component` 的 **S_ISLNK 臂** | strict-realpath 范式：ELOOP → 两个解释器同一结构化 `ValueError`；ENOENT → 非 strict 兜底，**保住今天的悬空 symlink 放行** |
| #1546 | 同文件 `_resolve_optional_config_path`、`_optional_config_path_relative_to` | 两个**解释器臂**收敛为同一规范化 `Path` 返回（同文件 `_canonical_parent` 已有的定型范式） |
| #1545 | 同文件 `_require_safe_directory_final_component` 的拒绝文案 | **只改消息内容**：类型仍 `ValueError`，`lock_path` 那条逐字不变 |

### 站点可达性口径（round-0 fixture 审实测所得；其中两条**推翻了我自己的初稿**）

**只有两个生产字段真的走到本模块的裸 expanduser**：`allowed_storage_roots`（→ `_optional_config_path`）
与 `log_root`（→ `_config_path_relative_to_preserve_final`）。其余九个 root 字段在更早的
`_expanduser_for_mode` 上崩掉——那是 #1423/#1520 已裁定的「故意 re-raise」，本单 Non-Goal。

1. **初稿写「#1549 漏数一处、`_config_path_preserve_final_component` 在 db-backed 臂活跃可达」——错的。**
   `scheduler_config.py:269` 的 `_raw_config_path_preserve_components` 排在 `:273` **之前**，
   对任何 tilde 输入都先经 `_expanduser_for_mode` re-raise。实测栈：
   `__post_init__:269 → _raw_config_path_preserve_components:861 → _expanduser_for_mode:853 → RuntimeError`。
   即 `workspace_root` 根本走不到 `:273`。**该站点仍修**（它确实是同款裸副本，值得结清），
   但定性改为**兼容面 / 家族账目**项，与 #1546 那一对同级，**不是**「#1549 漏数」。
2. **#1546 的「仓内零调用方」成立，但要说准。** `_resolve_optional_config_path` 在 `scheduler_config.py`
   有 7 处形似调用，但那些走的是 `_resolve_optional_config_path_for_mode` → `_resolve_config_path_for_mode`
   （`scheduler_config.py:939-961`），两个 database 臂各自都安全，但**理由不同**：
   db-free 臂有 `except (OSError, RuntimeError)`；**db-backed 臂没有**，它用的是
   `os.path.realpath`，压根不产生无 errno 的 `RuntimeError`，所以不需要该 handler。
   （初稿写「两臂各自已有 `except (OSError, RuntimeError)`」是**修复时新引入的假陈述**，
   fixture 审 round-1 P2-B 更正；结论不变，理由不同。这句排在 D.2 要进 PR body 的清单里，
   不改就会把假陈述写进永久记录 —— 与 round-0 P1-2 同类。）
   **不经过** `_scheduler._resolve_optional_config_path`。这一对只经
   `scheduler_candidate_runtime.py:557-558` 的 forwarder 对外暴露，属兼容面，今天不是活的崩溃路径。

### 已核实**不在** scope 的四处站点（避免评审重复挖）

| 站点 | 理由 |
|---|---|
| `:271` `path.resolve(strict=False)` | **已双接**：`except OSError`（`:272`）+ `except RuntimeError`（`:285`） |
| `:332` `Path(workspace_root).expanduser().resolve(strict=False)` | 包在 `except (OSError, RuntimeError, ValueError)`（`:334`）里 |
| `:504` `Path(value).expanduser()`（`_scheduler_allowed_roots_and_blockers`） | 裸，但 tilde **非活**：`config.allowed_storage_roots` 已在 `scheduler_config.py:418-423` 归一为绝对路径 |
| `:578` `Path(value).expanduser()`（`_confined_path`） | 裸，但 tilde **非活**：全部生产路径（`:545 :565 :596 :622`）之前都有 `_raw_config_path_*`，在 `:555 / :612 / :887` 先 re-raise（探针确认） |

后两处是 fixture 审补上的——初稿漏列，**属文档缺口不是 scope 缺口**（它们确实非活）。
D.7 的机械 grep 表必须把这四处连同处置一并列出。

## Non-Goals

- **`services/orchestrator/scheduler_config.py` 零改动。** 具体两条：
  - **不改 `_expanduser_for_mode`（`:850-857`）的「故意 re-raise」** —— #1423/#1520 已裁定，9 个字段依赖它。
  - **不改 db-free 包裹层 `_require_safe_directory_final_component_for_mode`（`:1020-1024`）的一揽子吞异常**
    （`except (OSError, RuntimeError, ValueError): if not db_free_required: raise`）。
    fixture 审实测：db-free 臂今天就把**全部**拒绝吞掉（symlink 指向文件 / 逃出 workspace /
    悬空且指向 workspace 外，三种几何 db-backed 报 `ValueError`、db-free 一律 ACCEPTED）。
    **因此「两个 database 臂判定一致」在本单 allowlist 下不可实现**，初稿把它写进验收是错的；
    已改成按**解释器臂**（3.11/3.12 vs 3.13+）表述。这个吞异常本身是独立缺陷，本单不治。
- **不改 safe_fs 任何调用方的 `except` 元组**。#1547 走的是**纯收窄**：
  抛型从裸 `RuntimeError` 变成其子类 `SafeFilesystemError`，既有
  **311 处 `except SafeFilesystemError`（46 个文件）** 零改动即接住，宽接 `RuntimeError` 的也不回退。
  （初稿写「26 处」，差约 12 倍，fixture 审实测更正；纯收窄的论证不受影响，但这个数是用来估爆炸半径的。）
- **不新增 `kind`**。复用 `kind="unsafe"`；新增会让所有 `error.kind == "io"` 的二分判据出现未覆盖分支。
- **不改 `_optional_config_path` 注释里已定稿的 realpath 范式论证**（design D2），只补 tilde 那一句。
- **不改 `scheduler_runtime_roots.py` 之外的同族副本**：`chain_runtime_utils.py` 的
  `_absolute_configured_path` 与 `_expand_path` 逐字同形，但跨 lane、语义可能不同 ——
  **另行立单承接，编号回填 tasks.md D.4**（#1547 验收标准明文要求，防重演 #1423「仅登记不修、无单承接」）。
- 不动 #1427 的 phantom-root 几何。

## Known Limits

- **判别力只在 ≤3.12 臂上。** 本机与 CI 主环境是 3.14.2，五处里有三处（#1544/#1545/#1546）的
  「修前红」只在 3.11/3.12 上成立；3.14 那一臂只能作**跨版本等价锁**。故证据要求含一个
  独立 3.11 venv 的 before/after 矩阵（tasks C.3），**绝不裸跑 `uv run --python 3.11`**（会重建项目 `.venv`）。
- **所有新增测试必须断言收敛后的行为、与解释器版本无关** —— 这样它们在 CI 的 3.11 与
  node-27 的 3.11.15 上都跑，node-27 receipt 顺带就是 ≤3.12 的实机 oracle。
- #1547 触发条件少见（家目录不可确定），且部分 lane 有前置 `is_absolute()` 门；
  取 low 风险的理由是「低概率 × 宽面」，不是「无影响」——已有一条 live 证实的逃逸（`--env-file`）。
- #1546 今天没有活调用方，改它买的是**家族账目结清**与兼容面一致性，不是当下崩溃修复。
