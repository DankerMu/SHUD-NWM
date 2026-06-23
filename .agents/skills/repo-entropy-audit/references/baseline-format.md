# Baseline Snapshot Format

The baseline snapshot captures the entropy state at a point in time for trend comparison.

---

## File Location

Default: `.entropy-baseline/latest.json`

The skill creates or updates this file on each run. Previous baselines are preserved as `.entropy-baseline/<date>.json`.

If the user prefers a different location, store it where specified but use the same format.

## JSON Schema

```json
{
  "version": 1,
  "timestamp": "2026-05-24T12:00:00Z",
  "repo": "heyi-co/heyi-next",
  "branch": "main",
  "commit": "abc1234",
  "summary": {
    "total_source_files": 829,
    "total_test_files": 242,
    "total_instruction_files": 14,
    "total_modules": 12,
    "modules_with_high_entropy": 2,
    "overall_trend": "stable"
  },
  "modules": {
    "apps/api": {
      "file_count": 85,
      "structure": {
        "score": "low",
        "scc_count": 0,
        "max_file_lines": 320,
        "layer_violations": 1
      },
      "semantics": {
        "score": "medium",
        "naming_variants": 3,
        "state_scatter_files": 2
      },
      "behavior": {
        "score": "high",
        "error_envelope_variants": 3,
        "boundary_inconsistencies": 5
      },
      "context": {
        "score": "low",
        "has_instruction_file": true,
        "stale_docs": 0
      },
      "protocol": {
        "score": "low",
        "template_compliance": true
      },
      "control": {
        "score": "medium",
        "enforced_rules": 4,
        "documented_rules": 6
      }
    }
  },
  "high_spread_patterns": [
    {
      "description": "Multiple error envelope shapes in API layer",
      "occurrences": 3,
      "files": ["apps/api/src/auth/handler.ts", "apps/api/src/user/handler.ts", "apps/api/src/order/handler.ts"],
      "axis": "behavior",
      "spread_risk": "high"
    }
  ],
  "cleanup_priorities": [
    {
      "target": "Unify error envelope in apps/api",
      "impact": "high",
      "effort": "medium",
      "axis": "behavior"
    }
  ]
}
```

## Trend Comparison

When a previous baseline exists, produce a comparison:

```markdown
## Trend vs Previous Baseline (2026-04-24)

| Metric | Previous | Current | Trend |
|--------|----------|---------|-------|
| Modules with high entropy | 3 | 2 | ⬇ Improving |
| Total instruction files | 12 | 14 | ⬆ Improving |
| Error envelope variants | 4 | 3 | ⬇ Improving |
| Naming variants (core concepts) | 8 | 9 | ⬆ Worsening |
| Stale docs (> 6mo) | 2 | 1 | ⬇ Improving |

### Module Score Changes
- `apps/api` behavior: 🔴 → 🟡 (improved after error model unification)
- `packages/ui` context: 🟡 → ❌ (AGENTS.md removed during refactor)
```

## Notes

- The `score` field uses strings: `"low"`, `"medium"`, `"high"`, `"critical"` (matching heatmap levels)
- Numeric fields (counts, variants) are best-effort approximations, not precise measurements
- The `high_spread_patterns` array captures the most concerning patterns sorted by spread risk
- Keep baselines version-controlled if the project wants historical trend tracking
