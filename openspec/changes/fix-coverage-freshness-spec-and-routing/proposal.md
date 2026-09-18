## Why
Two follow-ups from PR #2468 (node-27 coverage-freshness lane), both pre-existing defects that PR made visible. The user's directive for this run, verbatim: **「把 #2472 #2473 一起改了，中间遇到的问题不再defer路由，直接一并修复」** — every defect in this lane surfaced while doing this work is FIX_NOW here; DEFER / issue-routing is not an available disposition.

- **#2472** — the published spec (`openspec/specs/display-coverage-freshness/spec.md`, the truncation scenario of the coverage-freshness-observer requirement) asserts absolutely that truncation keeps breaching sources. `build_report`'s docstring, its arithmetic, `tests/test_node27_coverage_freshness_alert.py::test_more_breaching_sources_than_the_table_holds_is_reported_honestly` and runbook §11.2 all say the guarantee is bounded. Only the spec lies.
- **#2473** — runbook §11.2's exit-3 row sends five failure classes to a §11.3 subsection that handles two; the exit-2 row names a cause (「venv 里没有展示栈」) its destination §11.5 has no step for; and none of the three structured `COVERAGE_FRESHNESS_*` codes the mail carries appears anywhere in §11.

Defects surfaced mid-way (live-measured on node-27 before this fixture was written, `.workplans/pr-2472-2473/node27-receipt.md`), all fixed here per the directive:

- **§11.5's list-timers claim is false.** PR #2468 wrote that a disabled timer 「在表里仍然出现、只是没有 NEXT」. Measured with a throwaway unit: `disable --now` → **0 rows** in `list-timers --all`, identical to never-installed. `ActiveState/SubState` are also identical (`inactive/dead`). Only `LoadState` (`loaded` vs `not-found`) and `UnitFileState` (`disabled` vs empty) separate the two, and `scripts/node27_resource_governance.py` `collect_systemd` collects neither — so the receipt cannot make the distinction §11.5 promises, and the governance requirement PR #2468 added says it can.
- **§10.8 carries the same automaticity overclaim** for the sibling frontier lane (「timer 被人 disable 掉时，靠治理面发现」).
- **#2473's own remedy was incomplete.** It proposed routing `ProgrammingError` to the read-only role's grants. Measured: a DSN pointing at a database without the lane's relations also reads `ProgrammingError: (psycopg2.errors.UndefinedTable) …`. The base `psycopg2.OperationalError` covers refusal, password failure and a missing database, told apart only by the text after the class (R2, R5). A DSN that does not parse never reaches the driver at all (`ValueError: …`, no `(psycopg2.` in the reason). So the structural discriminator is whether the reason carries `(psycopg2.`, then the driver class, then — for the base class — the text.
- **#2473's remedy jumped to rebuilding.** It suggested pointing DB-unreachable straight at the container-recreation procedure. The first step for an alerting mail is diagnosis — container state, and a probe through the lane's own venv, env file and role that also reports which database it reached and whether the lane's relations exist there. Recreation stays one hop further, behind §5.1's existing pointer (which leads to `tier-node27-timeseries-storage.md` §4.3.3 for general container recreation; that document forbids §4.3.3 only for the cold bind).
- **§11.3 branch C cites 「见 §2」** for the #1446 refusal guard; it is documented in §3.1.
- **The archived #2080 design.md** still states 「roughly four framing lines」 and 「breaching sources kept」 (D3 and its test list).

## What Changes
- Spec: three **MODIFIED** requirements, each restated in full by verbatim extraction from the live spec with only the named text changed — the truncation scenario becomes bounded with its three honesty compensations (no hard-coded line counts); the failure-stage requirement gains the obligation that every structured code is documented and multi-cause codes are routed by the discriminator the mail carries; the governance-registration requirement names the load/unit-file state that makes "disabled" distinguishable, and states registration is visibility, not an alert.
- Runbook §11.2: exit-2 row names `COVERAGE_FRESHNESS_CONFIG_INVALID`; exit-3 row routes on `code`, then on whether the reason carries a driver class, then on that class, with every leaf given a first step. §11.3's last entry states which two causes it owns; a new short entry gives measured first steps for unreachable / timeout / wrong database. §11.3 branch C's §2 → §3.1. §11.5 gains an import check and, only if it fails, the dependency sync; its governance paragraph states the measured truth. §10.8's sentence is brought to the same truth.
- `scripts/node27_resource_governance.py` `collect_systemd`: also collect `LoadState` and `UnitFileState` for every unit.
- `scripts/select_ci_tests.py`: the runbook's path rule gains `tests/test_node27_coverage_freshness_alert.py`, which now reads the runbook — the reader-coverage defect class `ci-contract-baseline` names — with `tests/test_select_ci_tests.py`'s exact lists and count updated.
- Tests: a routing-completeness test (every `COVERAGE_FRESHNESS_*` code the script defines appears in runbook §11); a collector test pinning the two new properties; a characterization pin that an observation failure's reason keeps the `<SQLAlchemy class>: (<driver class>) …` shape the runbook now routes on.
- The archived #2080 design.md gets a dated correction note; its original rows are untouched.

## Capabilities
### New Capabilities
None.
### Modified Capabilities
- `display-coverage-freshness`: three requirements modified as above.

## Impact
`docs/runbooks/current-production-ops.md` (§10.8, §11.2, §11.3, §11.5), `scripts/node27_resource_governance.py` (`collect_systemd`'s property list only), `scripts/select_ci_tests.py` (one path rule), three test files, the archived #2080 `design.md` (appended note), one spec delta. `scripts/node27_coverage_freshness_alert.py` is **not touched** — its failure-line format is what the new routing reads, and it is pinned, not changed. The governance audit's exit code and recommendations do not move (`_recommendations` never reads the `systemd` section); the receipt gains two keys per unit under an existing unconstrained map (no JSON schema covers it — `grep -rl resource_governance schemas/` is empty).

Issue type: bugfix (spec + operator documentation, one monitoring-inventory collector)
Fixture level: expanded
Upstream suggested level: absent (issue-scribe follow-ups, no stage-change-pipeline contract fields)
Repair intensity: medium
Blast radius: operator routing for one alerting lane, one sibling-lane sentence, the governance receipt's per-unit property set (additive)
Selected risk packs: Documentation / migration notes; Config / project setup; Schema / columns / field names; domain: Operator alerting lanes / observer-observed predicate parity
Evidence floor: targeted pytest on both test files with genuine red proofs, `uv run ruff check .`, `openspec validate fix-coverage-freshness-spec-and-routing --strict --no-interactive`, a verbatim-restatement diff for each MODIFIED requirement, the #2473 routing-completeness greps, and node-27 live receipts: every command added to the runbook executed first, and the governance audit at the branch head carrying the new properties with its exit code unchanged.
