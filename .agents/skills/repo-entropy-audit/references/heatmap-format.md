# Heatmap Output Format

Use this format for the Phase 3 module-level entropy heatmap.

---

## Table Format

```markdown
## Entropy Heatmap

| Module | Structure | Semantics | Behavior | Context | Protocol | Control | Priority |
|--------|-----------|-----------|----------|---------|----------|---------|----------|
| apps/api | 🟢 | 🟡 | 🔴 | 🟢 | 🟢 | 🟡 | ⚠️ |
| apps/web | 🟢 | 🟢 | 🟡 | 🟡 | 🟢 | 🟢 | — |
| packages/domain | 🟢 | 🟡 | 🟢 | 🟢 | 🟢 | 🟢 | — |
| packages/db | 🟢 | 🟢 | 🟡 | 🟡 | 🟢 | 🟢 | — |
| packages/ui | 🟡 | 🟢 | 🟢 | ❌ | 🟡 | 🟡 | ⚠️ |
| tools/ | 🟢 | 🟢 | 🟢 | 🟡 | 🟢 | 🟢 | — |
```

## Score Definitions

| Score | Label | Meaning |
|-------|-------|---------|
| 🟢 | Low | Healthy; no significant entropy signals on this axis |
| 🟡 | Medium | Some signals present; not urgent but worth monitoring |
| 🔴 | High | Significant entropy; recommend prioritized attention |
| ❌ | Critical | Layer/dimension essentially absent; immediate action recommended |

## Priority Column

Derived from the row's worst score:
- **❌ Critical** — any cell is ❌
- **⚠️ Attention** — any cell is 🔴 or two+ cells are 🟡
- **—** None — all cells 🟢 or at most one 🟡

## Scoring Guidelines

### Structure
- 🟢: clear layering, no circular deps, util directories bounded
- 🟡: minor violations (< 3), some large files (> 500 lines)
- 🔴: significant SCC, util absorbing business logic, frequent layer violations

### Semantics
- 🟢: naming consistent, state models centralized, glossary terms dominant
- 🟡: 1-2 naming variants per concept, minor state scatter
- 🔴: > 3 naming variants for core concepts, state definitions in > 3 files

### Behavior
- 🟢: unified error model, consistent boundary handling
- 🟡: minor inconsistencies in non-critical paths
- 🔴: multiple error envelopes, inconsistent null/empty handling across the module

### Context
- 🟢: instruction file present with substantive content, docs fresh
- 🟡: instruction file exists but thin, or some docs stale
- 🔴: no instruction file, or docs significantly stale (> 6 months behind code)
- ❌: no instruction file and no documentation for a critical module

### Protocol
- 🟢: follows project template, verification commands consistent
- 🟡: minor deviations from template
- 🔴: significantly different structure from peer modules

### Control
- 🟢: rules enforced by automation, cleanup tracked
- 🟡: some rules documented but not enforced
- 🔴: significant gap between documented rules and enforcement
