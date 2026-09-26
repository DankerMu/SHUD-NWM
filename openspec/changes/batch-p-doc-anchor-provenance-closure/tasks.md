## Risk packs

- **Documentation:** every rewritten citation resolves to the thing it claims, read back with `sed -n`; history is pinned to a SHA, never moved.
- **Legacy compatibility:** fact-baseline tables, SHA-prefixed coordinates, receipts and archive evidence stay byte-identical, except for the two explicitly scoped archive edits (D3 and the K3 receipt addition).
- **Spec ledger:** archiving #2326 must not import unimplemented clauses (`/flood-alerts`, basin-detail).
- **Provenance:** the #2365 numbers come only from the captured read-only data; nothing is fabricated or recomputed from memory.

## Must-preserve

- In the #2406 archived `design.md`, only two lines change (D3): the `_event_matches_candidate_rows` line and the "见 #2423" line. The rest of the section, including the fact table and the existing re-point narrative, has a zero diff.
- No runtime behaviour changes. Edits to `.py` files are docstrings and comments only, plus the new tests.
- The K3 receipt's existing text is unchanged; the change only adds to it.

## 1. #2085 mvt.py anchors (implementer A)

- [x] 1.1 Run `grep -rn 'mvt\.py' docs/ openspec/` and classify every hit using the D2 partition. Record the table (fixed / excluded + reason) for the PR.
- [x] 1.2 Fix the in-scope hits by symbol per D1. Re-run the `active_flag` census (`grep -n active_flag services/tiles/mvt.py`) and rewrite both census copies so they agree. Add `_national_discharge_coverage_rows` to the glossary.
- [x] 1.3 Pin the ADR 0002 measurement (the #1338 pre-drop baseline Q1/Q8 text, cited by content) at `af49ced22`. Pin other dated plans and ADRs likewise (D2): find each SHA with `git log -S '<cited line text>' -- <doc>` on the doc line that introduced the citation, then read it back pinned. Mark `feat-reach-geom-oq-findings.md` historical and neutralise its deep links (D2).
- [x] 1.4 Read-back evidence for the PR: for each current-truth `mvt.py:N`, `sed -n` at HEAD shows the claimed symbol. For each SHA-pinned one, `git show <sha>:services/tiles/mvt.py | sed -n` shows it.

## 2. #2423 archived design coordinates (implementer A)

- [x] 2.1 Fix the one stale `_event_matches_candidate_rows` coordinate by symbol at `2ee1f53ed` (def `:13706`, `forecast_cycle` branch `:13719-13729`, with the SHA prefix).
- [x] 2.2 Turn the "检测工具与后续清理见 #2423" line into a closure note (detection script declined).
- [x] 2.3 Prove with `git diff` that no other line of the archived design changed.
- [x] 2.4 Read-back table for the PR: `git show 2ee1f53ed:<path> | sed -n <N>p` for all 13 coordinates.

## 3. #2263 references and retention tests (implementer B)

- [x] 3.1 `publisher.py` `_copyback_batch_mutex`: remove the `` `:207-210` `` reference and replace it with a symbol reference. `grep -nE '`:[0-9]+' services/tile_publisher/publisher.py` must return nothing.
- [x] 3.2 Fix the stale references by symbol (D4):
  - `scheduler_discovery.py` → `scheduler_generation_gate.strict_warm_start_evidence`'s `COLD_NEW_MODEL` / `COLD_DECLARED_CUTOVER` branches;
  - `scheduler_state_failure.py` → `retry.failure_classifier`'s `"policy_blocked"` arm.
- [x] 3.3 Add the two `_resolve_runs_only_roots` tests in `tests/test_retention_extra_roots.py` per D4, and reword the docstring and comment. Red proof: temporarily make blank values produce a skip entry, confirm test 1 fails, and record the output. The PR names the test file and both test names, and records `grep -n "db-free" services/orchestrator/retention.py`.
- [x] 3.4 PR records: the census counts (`\.py:[0-9]+` over `services/ packages/ apps/api`), the decision not to add a gate, and the pattern's known blind spots. The orchestrator files the follow-up issue for the gate through issue-scribe, after a dedup check.

## 4. #2365 republish identity receipt (implementer B)

- [x] 4.1 Write `docs/runbooks/receipts/2026-08-24-issue-1816-republish-identity.md` per D5, using the captured files in the orchestrator scratchpad `p-evidence/`. Include:
  - the 16-row map;
  - the check commands **and their output**;
  - the per-id first-admitted-run and clone-row evidence;
  - the clone-provenance source statement;
  - the hetianhe and shj_2shj observations.
- [x] 4.2 Add a §5.7.1 pointer in `docs/runbooks/production-ops/recalibration-and-archive.md`: M1′ was superseded by #1816, see the receipt.
- [x] 4.3 `grep -rn 'dg_5bd9935f3c32ac5b2936f68588489575\|dg_67210bfe424cdcdc69aaa7469485a382' docs/` hits the receipt, and the receipt names the predecessors `dg_281ff8c7…` / `dg_03b3cd97…`.

## 5. K3 receipt completion (implementer B)

- [x] 5.1 Append to the K3 receipt the **first** statement of `k3-catalog.sql` (the `== FK dependents` `pg_constraint` query) and its 20 rows from `.workplans/k3/k3-catalog.out`, verbatim. Capture context: node-27, `BEGIN READ ONLY`. The output file was last written 2026-09-24T09:17:33Z, which is an upper bound on the capture time and falls before the 12:30–12:42Z delete window. Do not claim it ran inside that window.

## 6. #2326 m11-popup rebase and archive (implementer C)

- [x] 6.1 Rebase the four delta specs per D6, with a code-evidence table (file + symbol + current line) for each retained clause.
- [x] 6.2 Dry archive in a temporary copy succeeds, then run the real `openspec archive m11-popup-station-overlay-usability -y`.
- [x] 6.3 Run the diff-level check in D6 over the four capability specs. Record the `openspec validate --all --strict` failure counts for master and for HEAD; HEAD must not be higher.

## 7. Verification (local)

- [x] 7.1 `uv run pytest -q tests/test_retention_extra_roots.py`, and `uv run ruff check .`.
- [x] 7.2 `openspec validate batch-p-doc-anchor-provenance-closure --strict --no-interactive`; `openspec validate --specs` counts as in 6.3.
- [x] 7.3 Markdown lint on the changed `docs/**` files (CI lints `docs/**/*.md`).

## Evidence Floor

1. **#2085:** the classification table; symbol-based anchors with read-back output at HEAD, and at the pinned SHA for historical ones; the census copies agree, and the glossary entry is added; ADR 0002 is SHA-pinned; the survey is marked historical; the AC6 deviation is recorded.
2. **#2423:** the stale coordinate is fixed by symbol, and the closure note is added; a read-back table for all 13 coordinates at `2ee1f53ed`; a zero diff elsewhere in the section.
3. **#2263:** zero backtick line references in `publisher.py`; both stale references fixed by symbol; two passing tests in `tests/test_retention_extra_roots.py`, with red proof; the `db-free` grep recorded; the no-gate decision recorded, and the follow-up filed.
4. **#2365:** a receipt that is greppable for the new ids and names their predecessors; the 16-row map with check commands and their output; first-admitted-run and clone-row evidence, with its source stated; a §5.7.1 pointer in `recalibration-and-archive.md`.
5. **#2326:** the change is archived, the D6 diff-level check passes, and there is code evidence (file + symbol + current line) per retained clause.
6. **K3:** the receipt contains the query and the 20 rows.
7. ruff, pytest, openspec and markdownlint pass.
