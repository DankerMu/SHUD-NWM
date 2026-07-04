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
| Implicit Dependencies | Yes / Partial / No | <notes> | <file path or "missing"> |

Canonical dimension definitions: references/methodology/agents-md-spec.md

## Priority Actions

Ordered by impact on future agent correct-change cost:

1. **<action>** — <why this matters most>
2. **<action>** — <why>
3. **<action>** — <why>

**Severity note:** These are repo-setup improvements, outside the P0/P1/P2/Note change-review scale. If a gap blocks an active change, raise it through `entropy-review` (E-grades) and fold it via the Severity Crosswalk in `risk-adaptive-cross-review`'s `finding-contract.md`.

## Recommended Tools

Based on findings, consider using:
- `entropy-review` — for PR-level consistency checks and dependency-direction drift in a change (after adding constraint dimensions)
- `repo-entropy-audit` — for full-repo entropy scan and baseline, including whole-repo structural dependency scans
- `project-documentation` — for docs/ tree governance
- `review` — for general code review with broader scope
```
