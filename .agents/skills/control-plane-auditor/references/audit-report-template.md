# Control Plane Audit Report Template

Use this template for Phase 3 output.

---

```markdown
# Control Plane Audit Report

**Repository:** <name>
**Date:** <YYYY-MM-DD>
**Scope:** <full repo | specific modules>

## Summary

| Layer | Status | Key Finding |
|-------|--------|-------------|
| Memory | ✅ / ⚠️ / ❌ | <one-line summary> |
| Invariant | ✅ / ⚠️ / ❌ | <one-line summary> |
| Protocol | ✅ / ⚠️ / ❌ | <one-line summary> |
| Permission | ✅ / ⚠️ / ❌ | <one-line summary> |
| Sensorium | ✅ / ⚠️ / ❌ | <one-line summary> |
| Evaluation / GC | ✅ / ⚠️ / ❌ | <one-line summary> |
| Governance | ℹ️ | <one-line note> |

## Layer Details

### Memory Layer
**Status:** ✅ / ⚠️ / ❌

**What exists:**
- <concrete findings with file paths>

**What's missing:**
- <concrete gaps>

**Recommendations:**
1. <specific, actionable recommendation>

### Invariant Layer
(same structure)

### Protocol Layer
(same structure)

### Permission Layer
(same structure)

### Sensorium Layer
(same structure)

### Evaluation / GC Layer
(same structure)

### Governance Layer
**Status:** ℹ️ (informational only)
**Notes:** <observations about current autonomy posture>

## AGENTS.md Constraint Dimensions

| Dimension | Present? | Quality | Location |
|-----------|----------|---------|----------|
| Glossary | Yes / Partial / No | <notes> | <file path or "missing"> |
| Dependency Rules | Yes / Partial / No | <notes> | <file path or "missing"> |
| Error Model | Yes / Partial / No | <notes> | <file path or "missing"> |
| Naming Conventions | Yes / Partial / No | <notes> | <file path or "missing"> |
| Doc Freshness Rules | Yes / Partial / No | <notes> | <file path or "missing"> |
| State Model References | Yes / Partial / No | <notes> | <file path or "missing"> |

## Priority Actions

Ordered by impact on future agent correct-change cost:

1. **<action>** — <why this matters most>
2. **<action>** — <why>
3. **<action>** — <why>

## Recommended Tools

Based on findings, consider using:
- `entropy-review` — for PR-level consistency checks (after adding constraint dimensions)
- `repo-entropy-audit` — for full-repo entropy scan and baseline
- `project-documentation` — for docs/ tree governance
- `dependency-audit` — for dependency health
- `review` — for general code review with broader scope
```
