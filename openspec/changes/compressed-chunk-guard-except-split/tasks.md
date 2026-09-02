## Risk Triage

```text
Issue type: diagnostics bugfix (error-code contract)
Project profile: NHMS (openspec/project-profile.md)
Blast radius: low-medium (exception routing in four modules; no data path change)
Fixture level: expanded
Repair intensity: medium
Upstream suggested level: absent (hand-written issue; expanded because the code contract spans two DB columns, two CLIs and the runbook route table)
Why:
- operator-facing wire codes across four surfaces must stay byte-consistent
- order constraint between three except arms; a wrong order silently reverts the fix
OpenSpec change: compressed-chunk-guard-except-split
Evidence floor:
- uv run pytest -q tests/test_forcing_producer.py tests/test_forcing_producer_cli.py tests/test_output_parser.py tests/test_output_parser_cli.py tests/test_timescale_write_guard.py
- uv run ruff check .
- grep -n "except CompressedChunkWriteError\|except CompressedChunkGuardError" over the four modules (8 + 8 code arms, subclass first; comment at producer.py:812 reworded)
- grep -n GUARD_FAILED docs/runbooks/tier-node27-timeseries-storage.md
- grep -n "therefore \*\*ambiguous\*\*\|hint, not a verdict\|#1785" docs/runbooks/tier-node27-timeseries-storage.md -> empty (pre-existing unrelated hits of bare "ambiguous" at :626 and :3353 must survive; the pattern is anchored on §4.3.1's own strings)
- grep -n "#1785" openspec/changes/tier-node27-timeseries-storage/design.md -> the dated supersede bullet
- openspec validate compressed-chunk-guard-except-split --strict --no-interactive
```

## Risk Packs

| Pack | 选择 | 理由 |
|---|---|---|
| Public API / CLI / script entry | selected | two CLIs × two legs; stderr prefixes |
| Config / project setup | not selected | none |
| File IO / path safety / overwrite | not selected | none |
| Schema / columns / units / field names | selected | new `error_code` values in two columns (free text, no constraint) |
| Auth / permissions / secrets | not selected | none |
| Concurrency / shared state / ordering | selected | except-arm ordering |
| Resource limits / large input / discovery | not selected | none |
| Legacy compatibility / examples | selected | `_BLOCKED` codes byte-stable; dashboards keep working |
| Error handling / rollback / partial outputs | selected | re-raise semantics and generic wrapper unchanged |
| Release / packaging / dependency compatibility | not selected | none |
| Documentation / migration notes | selected | §4.3.1 route table; design.md correction |
| PostGIS / TimescaleDB 域行为 | selected | which guard outcome means "compressed chunk" |
| 水文气象时序 / forcing 窗口 | not selected | the producer's domain, but no forcing window, alignment or value changes — only exception routing after the guard |
| 地理空间 / CRS / basin 几何 | not selected | untouched |
| SHUD 数值运行时 | not selected | untouched |
| Slurm 生产生命周期 | not selected | untouched |
| 外部气象 provider | not selected | untouched |
| run manifest / QC 溯源 | not selected | untouched |
| 已发布 NHMS 制品 / display 身份 | not selected | error codes are operator-facing, not display identity |

## Tasks

- [x] T1 Split the eight arms (subclass first, base second, generic last) with the new inline codes/prefixes; rewrite the routing comments at `producer.py:812-816` and `parser.py:276-278`.
- [x] T2 16 paired cases in the four suites: producer/parser fixtures (4), forcing CLI `_click_main` + `_argparse_main` (4), output-parser CLI both subcommands (`["parse", "--run-id", …]` for the second) × `_click_main` + `_argparse_main` (8); order-mutation check recorded in the PR body.
- [x] T3 Runbook §4.3.1 per D3's line-by-line list (count, intro, rows, HANDOFF generalisation, caveat deletion, triage paragraph); `tier-node27-timeseries-storage/design.md` dated supersede bullet covering `:1713-1715`, `:1780-1781`, `:1792-1794`.
- [x] T4 Post the explicit pytest + grep outputs in the PR (CI does not select these suites, #1656).

## Non-goals (explicit)

Guard hierarchy, terminal states, apply layer, backfill catches.
