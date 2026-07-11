# Heatmap Output Format

Use this format for the Phase 3 module-level entropy heatmap.

---

## Table Format

```markdown
## Entropy Heatmap

| Module | Structure | Semantics | Behavior | Context | Protocol | Control | Priority |
|--------|-----------|-----------|----------|---------|----------|---------|----------|
| apps/api | 🟢 | 🟡 | 🔴 | 🟢 | 🟢 | 🟡 | ⚠️ |
| apps/web | 🟢 | 🟢 | 🟡 | 🟡 | 🟢 | 🟢 | ⚠️ |
| packages/domain | 🟢 | 🟡 | 🟢 | 🟢 | 🟢 | 🟢 | — |
| packages/db | 🟢 | 🟢 | 🟡 | 🟡 | 🟢 | 🟢 | ⚠️ |
| packages/ui | 🟡 | 🟢 | 🟢 | ❌ | 🟡 | 🟡 | ❌ |
| tools/ | 🟢 | 🟢 | 🟢 | 🟡 | 🟢 | 🟢 | — |
```

## Score Definitions

| Score | Label | Meaning |
|-------|-------|---------|
| 🟢 | Low | Healthy; no significant entropy signals on this axis |
| 🟡 | Medium | Some signals present; not urgent but worth monitoring |
| 🔴 | High | Significant entropy; recommend prioritized attention |
| ❌ | Critical | Axis unmeasurable (a prerequisite is missing) or a critical breach makes the dimension effectively absent; immediate action recommended |

**When `❌` applies (general rule, all axes):** score `❌` when the axis cannot be measured because a prerequisite is missing — no instruction file to read, imports that will not parse, no error handling to sample — **or** when a critical breach makes the dimension effectively absent (a mandatory layer, error model, or enforcement mechanism entirely gone). Treat `❌` as "restore the prerequisite, then rescore." Each axis below states its own `❌` trigger.

## Priority Column

Derived from the row's worst score:
- **❌ Critical** — any cell is ❌
- **⚠️ Attention** — any cell is 🔴 or two+ cells are 🟡
- **—** None — all cells 🟢 or at most one 🟡

## Scoring Guidelines

Thresholds mirror the detection numbers in [Scan Dimensions](scan-dimensions.md); each axis links to its method section. Bands are contiguous (no gaps): a value that just crosses the 🟡 ceiling is 🔴.

### Structure
See [Scan Dimensions § Structure](scan-dimensions.md#structure).
- 🟢: clear layering, no import cycles, util/shared directories ≤ 20 files
- 🟡: 1-2 layer violations, a util directory approaching the 20-file limit, or 1-2 files importing from > 5 distinct top-level modules
- 🔴: ≥ 1 import cycle, a util directory > 20 files or absorbing business logic, or ≥ 3 files importing from > 5 distinct top-level modules
- ❌: import graph cannot be built (imports unparseable / build broken), or a mandatory architectural layer is entirely missing

### Semantics
See [Scan Dimensions § Semantics](scan-dimensions.md#semantics).
- 🟢: naming consistent, state models centralized, glossary terms dominant
- 🟡: 1-2 naming variants per concept, or one entity's state defined in 2 files
- 🔴: ≥ 3 naming variants for a core concept, or an entity's state defined in ≥ 3 files
- ❌: no glossary and type names too divergent to identify canonical concepts (semantics cannot be assessed)

### Behavior
See [Scan Dimensions § Behavior](scan-dimensions.md#behavior).
- 🟢: unified error model, consistent boundary handling, `any` under 5% of annotations
- 🟡: 2 error-handling patterns coexisting in a layer, or minor inconsistencies in non-critical paths
- 🔴: > 2 error-handling patterns in one layer, multiple error-envelope shapes, or `any` above 5% of type annotations
- ❌: a layer that must handle errors has none, or all errors are silently swallowed (behavior unobservable)

### Context
See [Scan Dimensions § Context](scan-dimensions.md#context).
- 🟢: instruction file present with substantive content, docs fresh
- 🟡: instruction file exists but thin, or some docs stale
- 🔴: no instruction file, or docs > 6 months behind code (instruction files use the stricter > 3-month threshold)
- ❌: no instruction file and no documentation for a critical module

### Protocol
See [Scan Dimensions § Protocol](scan-dimensions.md#protocol).
- 🟢: follows the project template, verification commands consistent with peers
- 🟡: minor deviations from the dominant template
- 🔴: structure significantly diverges from peer modules, or verification gates peers enforce are missing
- ❌: no instruction file exists to compare against the template (protocol cannot be assessed)

### Control
See [Scan Dimensions § Control](scan-dimensions.md#control).
- 🟢: rules enforced by automation, cleanup tracked
- 🟡: some rules documented but not enforced
- 🔴: fewer than half of documented rules are enforced, or a known backlog has no tracking
- ❌: documented rules with zero enforcement mechanism (no CI, lint, or tests) for the module
