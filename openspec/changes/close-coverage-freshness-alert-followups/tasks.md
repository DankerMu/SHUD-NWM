# Tasks — close #2465 + #2466 (coverage freshness lane follow-ups)

Fixture level: compact · Repair intensity: medium · `design.md` exempt at this level

## Change surface

- `scripts/node27_resource_governance.py` (`DEFAULT_SERVICES` only)
- `tests/test_node27_resource_governance.py`
- `tests/test_node27_coverage_freshness_alert.py`
- `docs/runbooks/current-production-ops.md` (§11.2, §11.3, §11.5)
- `scripts/node27_coverage_freshness_alert.py` — **comment text only**, the D3 journal-budget comment at `:106-110`
- `openspec/changes/archive/2026-09-18-node27-coverage-freshness-alert/{design.md,tasks.md}` (dated correction notes)

## Must preserve

- `scripts/node27_coverage_freshness_alert.py` — every **executable** line unchanged. Its exit-2-on-import-failure behaviour is the deliberate one (`config_from_env`'s docstring records why); #2465 is documentation drift, not a code defect. The single permitted edit is the D3 journal-budget comment at `:106-110` (see T5); `git diff -- scripts/node27_coverage_freshness_alert.py` must touch comment lines only.
- `scripts/node27_resource_governance.py` beyond `DEFAULT_SERVICES` — unchanged; `_recommendations` does not read the `systemd` section, so the audit's exit code must not move.
- Every existing test in both test files; all existing `DEFAULT_SERVICES` entries and their alphabetic order.
- The archived change's record of what was decided: correct by appending a dated note, never by rewriting history in place.

## Seams under test

- `main(argv, *, now=, observe=, env=)` for the coverage lane's exit code (the same public boundary the existing 26 tests use).
- `governance.DEFAULT_SERVICES` and `governance.collect_systemd(...)` for the registration, mirroring the #1368 pair exactly.

## Risk packs

- Config / project setup: **selected** — a production monitoring-inventory constant grows by two entries.
- Documentation / migration notes: **selected** — three runbook sections plus two archived-artifact corrections; this is most of the change.
- Public API / CLI / script entry: **not selected** — no entrypoint, argument, exit code or output shape changes; the exit-code *semantics* are only being documented, not altered.
- File IO / path safety / overwrite: **not selected** — nothing reads or writes a path.
- Schema / columns / units / field names: **not selected** — no schema, payload or field changes; the governance receipt gains two keys under an existing map, which is what its own collector test asserts.
- Auth / permissions / secrets: **not selected** — no credential surface is touched.
- Concurrency / shared state / ordering: **not selected** — both lanes remain stateless w.r.t. this change.
- Resource limits / large input / discovery: **not selected** — two more `systemctl show` calls per daily governance tick.
- Legacy compatibility / examples: **not selected** — every existing `DEFAULT_SERVICES` consumer uses `issubset`/membership, so nothing breaks.
- Error handling / rollback / partial outputs: **not selected** — no failure path changes.
- Release / packaging / dependency compatibility: **not selected** — no dependency movement.
- Domain — Operator alerting lanes / observer-observed predicate parity: **selected** — #2466 is precisely the liveness-of-the-observer half of that pack.

## Implementation tasks

- [x] T1 `DEFAULT_SERVICES` gains `nhms-node27-coverage-freshness-alert.service` and `.timer` in alphabetic position (between `nhms-node27-autopipe.timer` and `nhms-node27-download.service`).
- [x] T2 `tests/test_node27_resource_governance.py`: `test_default_services_includes_coverage_freshness_alert_units` mirroring `test_default_services_includes_frontier_alert_units`, plus the collector twin `test_collect_systemd_receipt_includes_coverage_freshness_alert_units` mirroring the frontier one (mocked `_run_command`).
- [x] T3 `tests/test_node27_coverage_freshness_alert.py`: a test that makes `lookback_days` raise `ImportError` and asserts exit 2, `CODE_CONFIG_INVALID` on the structured stderr line, and that the injected observation provider was never called. **The injected env MUST set `DATABASE_URL`**, because `config_from_env` runs `_required_env(env, "DATABASE_URL")` *before* `lookback = lookback_days()` — without it the test passes on the missing-DSN path and never reaches the import at all. To make the assertion discriminating, also assert the structured `reason` starts with `ImportError:` (`main`'s generic config handler formats `f"{type(error).__name__}: …"`). This pins the behaviour the docs are being corrected to describe, so the drift cannot silently return.
- [x] T4 Runbook §11.2: the exit-2 row names the import-time display-module / missing-`PYTHONPATH` case with its own first step (pointing at §11.5's install/`PYTHONPATH` block, not §11.4's threshold knob); the exit-3 row's 「展示模块报错」 is qualified as *观测期*.
- [x] T5 Runbook §11.2, same paragraph, two measured corrections carried in because they are in the text being edited.
  - (a) The truncation guarantee is **bounded**, not absolute: with a 3-line failing `VERDICT:` block the table holds 19 rows, so more than 19 breaching sources do lose table rows — the header count, the omission line and `VERDICT: breaching=… +N more` keep the numbers honest.
  - (b) The journal-budget figure must be stated **by category, not as one lump**: how many lines systemd itself frames a failing tick with, and whether this lane adds a structured stderr line on that exit path. The prior draft's single "5" was an attribution error — `scripts/node27_coverage_freshness_alert.py:106-110` enumerates *four* systemd framing lines plus *one* lane stderr line, whereas the node-27 measurement on the **exit-1** path shows five systemd framing lines (`Starting…`, `Main process exited…`, `Failed with result…`, `Failed to start…`, `Triggering OnFailure= dependencies.`) and **no** stderr line, because the alert path calls `_emit` without `structured=`. Both compositions total 5, so `24 + 5 = 29 ≤ 30` holds either way — but only one of them is true.
  - Therefore the runbook sentence **and** the `:106-110` comment must both be rewritten to whatever E7 measures, with the two categories named separately and the exit path (1 vs 2/3) stated. Writing the corrected figure in only one of the two places is not acceptable: it would move the drift from doc↔doc to code↔doc. If E7 cannot be run, T5(b) is dropped whole — do not ship an unmeasured number in either place.
- [x] T6 Runbook §11.3: a third branch for "A 和 B 都查空" — coverage rows populated with `segment_count > 0` but the catalog still refuses the cycle (window columns inconsistent, or the hourly grid off the 3 h phase). Give the check (inspect `river_valid_time_start/end`, `min/max_lead_time_hours`, `river_sample_count` against `segment_count * lead_count`, the `(end - start) = (lead_count - 1) * 3600` span, and the `(start - cycle) % 3600` phase for the source's newest cycles) and the remediation (re-run `scripts/node27_refresh_coverage.py` with `--force` to rewrite the window columns). Note the class has live writers so it is reachable, not hypothetical. Two cross-references in the same edited text go stale on this task and must move with it: §11.3's own heading 「处置：两个真分支 + 一个"看不见"分支」 and §11.2's 「走 §11.3 两个分支」.
- [x] T7 Runbook §11.5: the governance-registration sentence, mirroring §10.8's.
- [x] T8 Append a dated `## 修正 2026-09-18（#2465）` note to the archived `design.md` naming the D6 row that was wrong and what is true, and a one-line note next to the archived `tasks.md` T3's "exactly as design D6" wording. **Both notes carry the literal marker `修正 2026-09-18`** (E6d greps for it in both files). Do not rewrite the original rows — the archive is the record of what was decided.

## Required evidence

- [x] E1 `uv run pytest -q tests/test_node27_resource_governance.py` — new tuple pin and collector twin pass; every pre-existing test still passes.
- [x] E2 `uv run pytest -q tests/test_node27_coverage_freshness_alert.py` — the new import-failure test passes and the pre-existing 26 still pass.
- [x] E3 Red proof, stated honestly per test. **T2's tuple pin and collector twin are genuinely red pre-change** — run them against the unregistered `DEFAULT_SERVICES` and paste the failure. **T3 is a characterization pin, not new behaviour**: the code already exits 2 on this path, so its red proof is an assertion-level mutation (flip the asserted exit code, or drop the `DATABASE_URL` from the injected env and show the `reason` assertion going red because the run never reached the import). Paste both. Do not present T3's red as if it proved new behaviour.
- [x] E4 `uv run ruff check .` → zero findings.
- [x] E5 `openspec validate close-coverage-freshness-alert-followups --strict --no-interactive` → strict-valid.
- [x] E6 **Live surface (the only surface the new spec clause governs).** `grep -rn "展示模块报错\|display-module error" docs/` → **every** hit is stage-qualified, i.e. each line names 观测期 or 导入期 (or the English equivalent) next to the phrase. A count is not the criterion — the number of hits may grow with T4 — an unqualified hit is the miss.
- [x] E6a **Archive surface (exempt from the clause, checked for containment).** `grep -rn "展示模块报错\|display-module error" openspec/changes/archive/2026-09-18-node27-coverage-freshness-alert/` → hits occur only on the original `design.md:190` row or inside a `修正 2026-09-18` note. No archived line is rewritten in place.
  - The fixture's own files under `openspec/changes/close-coverage-freshness-alert-followups/` quote the phrase while describing the drift and are outside both greps by construction — which is exactly why E6 is scoped to `docs/` rather than repo-wide.
- [x] E6b T7: `grep -n "DEFAULT_SERVICES" docs/runbooks/current-production-ops.md` → exactly two hits (§10.8's existing sentence + §11.5's new one).
- [x] E6c T6: the new §11.3 branch names its entry condition ("A 和 B 都查空") and the `--force` remediation; `grep -n -- "--force" docs/runbooks/current-production-ops.md` shows it inside §11.3.
- [x] E6d T8: `grep -rn "修正 2026-09-18" openspec/changes/archive/2026-09-18-node27-coverage-freshness-alert/` → one hit in `design.md` and one in `tasks.md`.
- [x] E7 (orchestrator, node-27) the governance audit run over `DEFAULT_SERVICES` returns a receipt carrying both new units with their real systemd state, and its exit code is unchanged from before the registration. **Same session re-measures T5(b)**: drive one failing tick through §11.5's scratch drop-in flow (`EnvironmentFile=` reset + throwaway DSN, production env untouched) and count, **separately**, (i) the systemd framing lines and (ii) this lane's structured stderr lines inside `journalctl --user -u nhms-node27-coverage-freshness-alert.service -n 30`, recording which exit code the tick produced. The real `OnFailure=` handler fires, so **one operator mail to `mumzy1995@163.com` is an expected artifact of this receipt**, not an incident — say so in the receipt. Both the runbook sentence and `scripts/node27_coverage_freshness_alert.py:106-110` must equal what this returns. Remove the drop-in and confirm the unit is back on its production `EnvironmentFile=` before closing the receipt.
  - **Recorded deviation, satisfied differently.** The retained journal already holds a failing tick of the real unit (`Sep 18 09:00:45`, exit 1), driven through exactly that scratch drop-in flow during the #2080 L4 receipt. Reading that tail measures the same thing on the same unit and costs no second operator mail, so no new failure was triggered. Result: 5 systemd framing lines, 0 lane stderr lines. Receipt: `.workplans/pr-2465-2466/node27-e7-receipt.md`.

## Verification matrix rows consumed

| Surface | Command | Expected evidence |
|---|---|---|
| Python script + tests | `uv run pytest -q tests/test_node27_resource_governance.py tests/test_node27_coverage_freshness_alert.py`; `uv run ruff check .` | E1-E4 pass, zero lint findings |
| OpenSpec contract | `openspec validate close-coverage-freshness-alert-followups --strict --no-interactive` | strict-valid change |
| node-27 alerting/governance lane | E7 on the real host | receipt carries both units, audit exit code unchanged |

## Recorded deviations from the two issues' own acceptance wording

- **#2465 asks for design D6's table row to be made consistent.** This change appends a dated correction note instead of rewriting the row. Reason: the change is archived, and an archive is the record of what was decided — rewriting it in place would erase the fact that the drift existed. The live operator surface (runbook §11.2) is corrected in place, and the new spec requirement is scoped to live documentation with the archive explicitly exempt.
- **#2465 notes the spec requirement text is unaffected.** True of the *existing* requirement, and this change does not touch it. But `openspec validate` hard-requires at least one delta, so this change ships two **ADDED** requirements (no MODIFY, no collision with the five existing ones at `openspec/specs/display-coverage-freshness/spec.md:6/:22/:32/:62/:77`), which archive into `openspec/specs/display-coverage-freshness/spec.md` in the normal way.
- **T5 is carried by neither issue.** The §11.2 paragraph is already under the pen for T4, and its current 「不会被截掉」 is contradicted by `build_report`'s own bounded guarantee; leaving a false absolute inside the routing table being fixed for routing accuracy would be incoherent. Recorded here and in the PR body as an addition, not smuggled in. T5(b) additionally widens the change surface to one comment block in `scripts/node27_coverage_freshness_alert.py` — the Non-goal below forbids changing that script's *behaviour*, and a comment-only edit does not; correcting the runbook while leaving the code comment stating a different composition would only relocate the drift.

## Non-goals

- Changing any **executable** line of `scripts/node27_coverage_freshness_alert.py`, including remapping the import failure to exit 3 (the alternative #2465 allows). The code's stage split is correct; only the documents — including one of its own comments — were wrong.
- Fixing #2464 (the display-source allowlist) — it is a code change with its own reachability argument and stays in its own issue.
- Registering the other units that are also absent from `DEFAULT_SERVICES` (`nhms-node27-mvt-cache-retention.*`, `nhms-node27-resource-governance.*`, `nhms-node27-timeseries-compression-replay.service`); each needs its own adjudication and #2466 explicitly scoped them out.
