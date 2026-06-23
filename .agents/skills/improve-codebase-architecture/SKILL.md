---
name: improve-codebase-architecture
description: >
  基于“深模块”哲学寻找架构改进点：扫描 codebase，对照 openspec/glossary.md 的领域术语与 docs/adr/ 的决策，
  找出 shallow modules 并提出 deepening 重构（把浅模块变深，提升 testability 与 AI 可导航性），
  产出可视化 HTML 评审，再进入 grill 对话逐个落地。
  触发词："improve architecture"、"找重构机会"、"合并紧耦合模块"、"让代码更可测"、"架构评审"。
  不用于纯需求澄清（用 clarify）或纯方向选型（用 future-aware-architecture）。
invocation_posture: hybrid
version: 0.1.0
---

# Improve Codebase Architecture

暴露 architecture friction，并提出 **deepening opportunities**——把 shallow modules 变成 deep modules 的 refactors。目标是 testability 和 AI-navigability。

**Invocation posture:** `hybrid`。优先显式调用；自动触发限于明确的"架构改进 / 找重构机会 / 让代码更可测"意图。

## Glossary

每个建议都精确使用这套 architecture 术语。语言一致性就是重点，不要漂移到 "component"、"service"、"API" 或 "boundary"。完整定义见 [LANGUAGE.md](LANGUAGE.md)。

- **Module** — 任何有 interface 和 implementation 的东西（function、class、package、slice）。
- **Interface** — caller 为正确使用 module 必须知道的一切：types、invariants、error modes、ordering、config。不只是 type signature。
- **Implementation** — 内部代码。
- **Depth** — interface 上的 leverage：小 interface 后面有大量 behaviour。**Deep** = 高 leverage；**Shallow** = interface 几乎和 implementation 一样复杂。
- **Seam** — interface 所在的位置；可以不原地编辑就改变 behaviour 的地方。（用这个词，不用 "boundary"。）
- **Adapter** — 在 seam 处满足 interface 的具体东西。
- **Leverage** — callers 从 depth 获得的东西。
- **Locality** — maintainers 从 depth 获得的东西：change、bugs、knowledge 集中在一个地方。

关键原则（完整列表见 [LANGUAGE.md](LANGUAGE.md)）：

- **Deletion test**：想象删除这个 module。复杂性消失，它只是 pass-through；复杂性在 N 个 callers 中重新出现，它就在发挥价值。
- **The interface is the test surface.**
- **One adapter = hypothetical seam. Two adapters = real seam.**

本 skill 参考项目的领域模型：**领域术语**来自 `openspec/glossary.md`（为好的 seam 命名）；**决策**来自 `docs/adr/`（记录 skill 不应重新争论的事）。两者都是 `grill-with-docs` 维护的资产。

## When Not To Use

- 纯需求澄清（把模糊需求变 actionable scope）→ `clarify`
- 纯架构方向 / 技术选型 → `future-aware-architecture`
- 全仓库的乱与冗余清理（广度扫描）→ `repo-entropy-audit` / `entropy-review`；本 skill 专攻模块深浅这条轴

## Process

### 1. Explore

先读项目的领域术语表 `openspec/glossary.md` 和你将触碰区域的相关 `docs/adr/`。

然后用编排器的原生 **`explorer` subagent**（Claude Code Task subagent 或 Codex subagent）遍历 codebase。不要死套启发式；自然探索并记录 friction：

- 理解一个概念是否需要在许多小 modules 之间来回跳？
- 哪些 modules 是 **shallow**，interface 几乎和 implementation 一样复杂？
- 哪些 pure functions 只是为 testability 抽出，但真正的 bug 藏在调用方式里（没有 **locality**）？
- 哪些 tightly-coupled modules 跨 seams 泄漏？
- 哪些部分未测试，或很难通过当前 interface 测试？

对任何疑似 shallow 的东西应用 **deletion test**：删除它会集中复杂性，还是只是移动复杂性？"会集中"就是信号。

### 2. Present candidates as an HTML report

写一个 self-contained HTML file 到 OS temp directory，**不要让任何内容落进 repo**。Temp dir 从 `$TMPDIR` 解析、fallback 到 `/tmp`（Windows 用 `%TEMP%`），写入 `<tmpdir>/architecture-review-<timestamp>.html`，每次运行新文件。为用户打开：Linux `xdg-open <path>`、macOS `open <path>`、Windows `start <path>`，并告知绝对路径。

Report 用 **Tailwind via CDN** 做 layout，**Mermaid via CDN** 处理 graph/flow/sequence diagrams，并与手写 CSS/SVG 混用（graph-shaped 用 Mermaid，editorial 效果用 hand-built div/SVG）。每个 candidate 都要有 **before/after visualisation**。

每个 candidate 渲染成 card：**Files** / **Problem**（一句话）/ **Solution**（一句话）/ **Wins**（用 locality、leverage 命名收益）/ **Before-After diagram** / **Recommendation strength** badge（`Strong` / `Worth exploring` / `Speculative`）。最后加 **Top recommendation** section。

**领域词汇用 `openspec/glossary.md`，architecture 词汇用 [LANGUAGE.md](LANGUAGE.md)。** 如果 glossary 定义了 "Order"，就说 "Order intake module"，不要说 "FooBarHandler" 或 "Order service"。

**ADR conflicts**：candidate 与现有 ADR 冲突时，只有 friction 真实到值得重开 ADR 才提出，并在 card 中明确标记（warning callout：_"contradicts ADR-0007 — but worth reopening because…"_）。不要列出 ADR 理论上禁止的每个 refactor。

完整 HTML scaffold、diagram patterns 和 styling 见 [HTML-REPORT.md](HTML-REPORT.md)。

文件写好后，先问用户："你想探索哪一个？" 不要还没问就提出 interfaces。

### 3. Grilling loop

用户选中 candidate 后，进入 grilling conversation，和他们走完整个 design tree：constraints、dependencies、deepened module 的形状、seam 后面是什么、哪些 tests 能经受变化（依赖分类与 seam 纪律见 [DEEPENING.md](DEEPENING.md)）。

决策成形时内联产生 side effects，纪律同 `grill-with-docs`：

- **用 `openspec/glossary.md` 中没有的概念命名 deepened module？** 把 term 加进 `openspec/glossary.md`（格式见 [../grill-with-docs/GLOSSARY-FORMAT.md](../grill-with-docs/GLOSSARY-FORMAT.md)）；文件不存在就懒创建。
- **对话中收紧了模糊 term？** 立刻更新 `openspec/glossary.md`。
- **用户用有分量的理由拒绝 candidate？** 提议落 ADR 到 `docs/adr/`（格式与三门槛见 [../grill-with-docs/ADR-FORMAT.md](../grill-with-docs/ADR-FORMAT.md)）：_"要我把这记录成 ADR，避免未来 architecture review 再次建议它吗？"_ 只有当未来的 explorer 真的需要这个理由来避免重复建议时才提议；短期理由和显而易见的理由跳过。
- **想探索 deepened module 的替代 interfaces？** 见 [INTERFACE-DESIGN.md](INTERFACE-DESIGN.md)。

## 与本仓库其它 skill 的关系

- `grill-with-docs`：本 skill 复用其 `openspec/glossary.md` / `docs/adr/` 落点与术语/ADR 纪律；grilling loop 与它同源。
- `future-aware-architecture`：定架构方向与可逆性；本 skill 在既定方向内找模块深化机会。
- `repo-entropy-audit` / `entropy-review`：治全仓库的乱与冗余（广度）；本 skill 深化模块、提升可测试性（深度）。
- `explorer` subagent：第 1 步 codebase 遍历的执行者。

---

改编自 [`mattpocock/skills`](https://github.com/mattpocock/skills) 的 `improve-codebase-architecture`（中文参考 [`vinvcn/mattpocock-skills-zh-CN`](https://github.com/vinvcn/mattpocock-skills-zh-CN)）。沉淀落点由上游的 `CONTEXT.md`/`docs/adr/` 本地化为本仓库的 `openspec/glossary.md`/`docs/adr/`，并复用 `grill-with-docs` 的格式与纪律。深模块理念出自 Ousterhout《A Philosophy of Software Design》与 Michael Feathers 的 seam 概念。
