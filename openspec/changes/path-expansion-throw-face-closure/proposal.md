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
| #1549 | `scheduler_runtime_roots.py` `_optional_config_path`、`_config_path_relative_to_preserve_final`、**`_config_path_preserve_final_component`** | **不抛**：db-backed 臂收敛到 db-free 臂，构造成功、产物逐字相等，分类仍留给 preflight |
| #1544 | 同文件 `_require_safe_directory_final_component` 的 **S_ISLNK 臂** | strict-realpath 范式：ELOOP → 两个解释器同一结构化 `ValueError`；ENOENT → 非 strict 兜底，**保住今天的悬空 symlink 放行** |
| #1546 | 同文件 `_resolve_optional_config_path`、`_optional_config_path_relative_to` | 两臂收敛为规范化 `Path` 返回（同文件 `_canonical_parent` 已有的定型范式） |
| #1545 | 同文件 `_require_safe_directory_final_component` 的拒绝文案 | **只改消息内容**：类型仍 `ValueError`，`lock_path` 那条逐字不变 |

### 对 issue 正文的两处口径更正（round-0 scope 前提核实所得）

1. **#1549 漏数一处。** 它点名 `_optional_config_path` 与 `_config_path_relative_to_preserve_final`
   两处，但同 lane 还有第三处同款裸 expanduser：`_config_path_preserve_final_component`，
   经 `scheduler_config.py:273`（`workspace_root_preflight_path`）在 **db-backed 臂活跃可达**
   （`_config_path_preserve_final_component_for_mode:896` 的 `if not db_free_required:` 直接转发）。
   本单一并修；不修它会让「db-backed 臂对 `~` 的失败语义收敛」这条验收本意在 `workspace_root` 上落空。
2. **#1546 的「仓内零调用方」成立，但要说准。** `_resolve_optional_config_path` 在 `scheduler_config.py`
   有 7 处形似调用，但那些走的是 `_resolve_optional_config_path_for_mode` → `_resolve_config_path_for_mode`
   （`scheduler_config.py:938-963`，两臂各自已有 `except (OSError, RuntimeError)`），
   **不经过** `_scheduler._resolve_optional_config_path`。这一对只经
   `scheduler_candidate_runtime.py` 的 forwarder 对外暴露，属兼容面，今天不是活的崩溃路径。

### 另两处已核实**不在** scope 的站点（避免评审重复挖）

- `scheduler_runtime_roots.py:271` 的 `path.resolve(strict=False)`：**已双接**
  （`except OSError` + `except RuntimeError`，`:284`），家族范式已到位。
- `:332` 的 `Path(workspace_root).expanduser().resolve(strict=False)`：包在
  `except (OSError, RuntimeError, ValueError)` 里，同上。

## Non-Goals

- **不改 `_expanduser_for_mode`（`scheduler_config.py:842`）的「故意 re-raise」口径** ——
  那是 #1423/#1520 已裁定的设计决策，9 个字段依赖它，本单不动。
- **不改 safe_fs 任何调用方的 `except` 元组**。#1547 走的是**纯收窄**：
  抛型从裸 `RuntimeError` 变成其子类 `SafeFilesystemError`，26 处 `except SafeFilesystemError` 零改动即接住，
  宽接 `RuntimeError` 的也不回退。
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
