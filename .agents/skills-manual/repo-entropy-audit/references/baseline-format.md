# Baseline Snapshot Format

The baseline snapshot captures the entropy state at a point in time for trend comparison.

---

## File Location

Default: `.entropy-baseline/latest.json`

The skill creates or updates this file on each run. Previous baselines are archived as `.entropy-baseline/<timestamp>.json`, where `<timestamp>` is the `timestamp` field of the snapshot being replaced, with colons replaced by hyphens for filename safety (e.g. `2026-05-24T12-00-00Z.json`). Using the snapshot's own timestamp rather than the run date keeps same-day reruns from colliding.

If the user prefers a different location, store it where specified but use the same format.

## Baseline instance format

The block below is an **illustrative instance**, not a formal JSON Schema. The field names and shapes are the convention; `version` stays `1` until the format changes incompatibly.

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
    "naming_variant_total": 9,
    "error_envelope_variants_max_per_module": 3,
    "stale_docs_total": 1,
    "overall_trend": "stable"
  },
  "modules": {
    "apps/api": {
      "file_count": 85,
      "structure": {
        "score": "low",
        "cycle_count": 0,
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

## Summary metric aggregation

The trend table compares repo-wide numbers, so the `summary` object persists them (not just per-module scores). Each derived metric is computed as:

- `naming_variant_total` — **sum** over tracked core concepts of the distinct-variant count per concept (repo-wide per-concept rollup; the per-module `semantics.naming_variants` is a local view). Higher = more naming entropy.
- `error_envelope_variants_max_per_module` — **max** over modules of `behavior.error_envelope_variants`. Max (not sum) is the aggregation-valid choice: envelope shapes are often shared across modules, so summing double-counts; the worst single module is what matters.
- `stale_docs_total` — **count** of documents past their staleness threshold (instruction files > 3 months behind code, general docs > 6 months).
- `modules_with_high_entropy` — **count** of modules whose worst axis score is `high` or `critical`.
- `total_instruction_files` — **count** of substantive AGENTS.md/CLAUDE.md files (more is better; a drop is a regression).

## Regression rule

A run regresses versus its baseline if **either**:

1. **any module's axis score moves to a worse band** (`low`→`medium`, `medium`→`high`, or any axis → `critical`); or
2. **any tracked count worsens**: `modules_with_high_entropy`, `naming_variant_total`, `error_envelope_variants_max_per_module`, or `stale_docs_total` increases, or `total_instruction_files` decreases.

On a regression, flag the offending modules and metrics prominently in the trend comparison and set `summary.overall_trend` to `"worsening"`. A run with no band drops and no count worsening is `"stable"` (or `"improving"` if any tracked metric got better).

## Notes

- The `score` field uses strings: `"low"`, `"medium"`, `"high"`, `"critical"` (matching heatmap levels)
- Numeric fields (counts, variants) are best-effort approximations, not precise measurements
- The `high_spread_patterns` array captures the most concerning patterns sorted by spread risk
- Keep baselines version-controlled if the project wants historical trend tracking
