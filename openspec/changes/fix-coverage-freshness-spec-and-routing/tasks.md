# Tasks — close #2472 + #2473 (coverage freshness spec truncation + operator routing)

Fixture level: expanded · Repair intensity: medium · `design.md` exempt (decisions carried here)

**Directive for this run (user, verbatim):** 「把 #2472 #2473 一起改了，中间遇到的问题不再defer路由，直接一并修复」.
Every defect in this lane surfaced while doing this work is **FIX_NOW in this PR**. DEFER and issue-routing are not
available dispositions — for implementers, reviewers and verifiers alike. A non-goal below is a recorded design
boundary with its reason, never a parked defect: after this change no sentence in scope may remain false.

## Change surface

- `openspec/changes/fix-coverage-freshness-spec-and-routing/specs/display-coverage-freshness/spec.md` (three MODIFIED requirements)
- `docs/runbooks/current-production-ops.md` — §10.8 (one sentence), §11.2 (exit-2 and exit-3 rows + their note), §11.3 (branch C's §-pointer; the last entry's scope; one new entry), §11.5 (import check + conditional sync; the governance paragraph)
- `scripts/node27_resource_governance.py` — `collect_systemd`'s `systemctl show -p` property list **only**
- `tests/test_node27_coverage_freshness_alert.py`, `tests/test_node27_resource_governance.py`
- `scripts/select_ci_tests.py` — the `docs/runbooks/current-production-ops.md` `PathTestRule` (around `:2788` on `origin/master`) gains `tests/test_node27_coverage_freshness_alert.py`, because T4 makes that test a reader of the runbook
- `tests/test_select_ci_tests.py` — the exact expected lists for that path (around `:2691` and `:5532`) and its count (around `:5988`, `"3"` → `"4"`)
- `openspec/changes/archive/2026-09-18-node27-coverage-freshness-alert/design.md` (appended dated note)

## Must preserve

- `scripts/node27_coverage_freshness_alert.py` — **byte-identical**. Its failure-line format (`reason = f"{type(error).__name__}: …"`) is exactly what the new routing reads; it is pinned by a test here, not changed. `MAX_REPORT_LINES` in particular does not move (#2472 is a spec-text correction, not a behaviour change).
- `scripts/node27_resource_governance.py` beyond `collect_systemd`'s property list — unchanged. `_recommendations` never reads the `systemd` section, so the audit's exit code and recommendation set must not move.
- Every existing test in both files; every existing `DEFAULT_SERVICES` entry and its order.
- Archived records: `openspec/changes/archive/**` original lines untouched. The archived #2080 **spec delta** copy (its truncation scenario) is left entirely as is — it is the snapshot of what #2080 shipped, and the correction lives in the live spec this change modifies. This is a reasoned choice, not a deferral: an archived delta is not read as current contract by anything in the repo.
- Unmodified requirements in `openspec/specs/display-coverage-freshness/spec.md`, and every non-designated sentence and scenario of the three modified ones (see E5).

## Seams under test

- `alerter.main(argv, *, now=, observe=, env=)` — an injected `observe` that raises a real SQLAlchemy-wrapped psycopg2 error.
- `governance.collect_systemd(...)` with `governance._run_command` monkeypatched, mirroring the existing collector twins.
- The runbook itself, read as a file: §11 is delimited by the `## 11.` and `## 12.` headings.

## Risk packs

- Documentation / migration notes: **selected** — most of the change; two prior rounds on this lane proved operator prose is where the defects live.
- Config / project setup: **selected** — the governance collector's property list changes.
- Schema / columns / units / field names: **selected** — the governance receipt gains `LoadState` and `UnitFileState` under `systemd.services[*].properties`. Additive, under an unconstrained map; no JSON schema covers the receipt (`grep -rl resource_governance schemas/` empty). E13 checks no reader breaks.
- Domain — Operator alerting lanes / observer-observed predicate parity: **selected** — the lane's routing must read the discriminator the mail actually carries; the governance receipt must actually distinguish what the docs say it does.
- Public API / CLI / script entry: **not selected** — no argument, exit code or output format changes (the alert script is byte-identical).
- File IO / path safety: **not selected** — nothing new is read or written by code.
- Auth / permissions / secrets: **not selected** — no credential surface changes; the runbook's new probe reuses the lane's existing env file and read-only role, and the superuser `psql` form already appears elsewhere in the runbooks.
- Concurrency / shared state: **not selected**.
- Resource limits: **not selected** — two more property names on an existing `systemctl show` call.
- Legacy compatibility / examples: **not selected** — every receipt consumer reads keys by name.
- Error handling / rollback: **not selected** — no failure path changes in code.
- Release / packaging / dependency: **not selected** — no dependency movement (the runbook's `uv sync` is operator guidance, conditional on a failed import check; nothing is synced by this change).

## Implementation tasks

- [ ] T1 Spec delta — already authored by verbatim extraction; implementers must not hand-edit it. Three MODIFIED requirements: (a) the coverage-freshness-observer requirement, only its truncation scenario rewritten as a bounded guarantee with its three honesty compensations and **no** hard-coded line counts; (b) the failure-stage requirement, gaining the obligation that every structured failure code is documented and multi-cause codes are routed by the reason's exception-class prefix and the driver class that follows it, plus two scenarios; (c) the governance-registration requirement, naming load state and unit-file state as what makes "disabled" distinguishable, stating registration is visibility and not an alert, plus one scenario.
- [ ] T2 `scripts/node27_resource_governance.py` `collect_systemd`: add `-p LoadState` and `-p UnitFileState` to the `systemctl show` argument list. Nothing else in that file.
- [ ] T3 `tests/test_node27_resource_governance.py`: (a) a collector test, in the style of the existing collector twins, asserting the `systemctl show` args for every unit include `LoadState` and `UnitFileState`, and that those keys parse into `properties`; (b) a test pinning the spec scenario "Registration is visibility, not an alert": the same receipt with the coverage timer's `systemd` properties set to enabled/active and to disabled/inactive yields identical recommendations and an identical audit exit code. Use the audit's real entry points for recommendations and exit code; do not restate their logic in the test.
- [ ] T4 `tests/test_node27_coverage_freshness_alert.py`: a routing-completeness test — the set of `COVERAGE_FRESHNESS_[A-Z_]+` string constants defined in `scripts/node27_coverage_freshness_alert.py` (read from source, never restated in the test) must each appear in `docs/runbooks/current-production-ops.md` between the `## 11.` and `## 12.` headings. Fail with the list of missing codes.
- [ ] T5 `tests/test_node27_coverage_freshness_alert.py`: a characterization pin — inject an `observe` that raises a real `sqlalchemy.exc.OperationalError` wrapping a real `psycopg2.OperationalError`, and one raising `sqlalchemy.exc.ProgrammingError` wrapping `psycopg2.errors.UndefinedTable`; assert exit 3, `COVERAGE_FRESHNESS_OBSERVATION_FAILED`, and that the structured `reason` starts with `OperationalError: (psycopg2.OperationalError)` / `ProgrammingError: (psycopg2.errors.UndefinedTable)` respectively — the exact shape the runbook routes on, and the shape measured live (receipt R2). If constructing those exceptions needs care (SQLAlchemy's `DBAPIError(statement, params, orig)` signature), follow the library, not a hand-made string.
- [ ] T6 Runbook §11.2 exit-2 row: name `COVERAGE_FRESHNESS_CONFIG_INVALID`. Keep the existing import-stage vs config-stage split (verified correct, receipt R3).
- [ ] T7 Runbook §11.2 exit-3 row + a routing note under the table. First read `code`: `COVERAGE_FRESHNESS_NO_SOURCES` → §11.3's last entry. `COVERAGE_FRESHNESS_OBSERVATION_FAILED` → route on the reason, in this order (named classes win over the catch-alls):
  - `OperationalError: (psycopg2.errors.QueryCanceled)` → statement timeout → the new DB entry's `pg_stat_activity` step.
  - `ProgrammingError: (psycopg2.errors.InsufficientPrivilege)` → the read-only role's grants (its own step, see T8).
  - `ProgrammingError: (psycopg2.errors.Undefined…)` (`UndefinedTable`, `UndefinedSchema`, …) → the lane reached a database without its relations, or the schema drifted → the new DB entry's probe (which reports the database and whether `hydro.hydro_run` exists there).
  - **any other reason carrying `(psycopg2.`** — the base `psycopg2.OperationalError`, `psycopg2.errors.AdminShutdown` (container restarted mid-observation), `InterfaceError`, deadlock / lock errors, … → the new DB entry. For the base `psycopg2.OperationalError` the text after the class tells refusal (`Connection refused`), password failure (`password authentication failed`) and a missing database (`database "…" does not exist`) apart; say so, and note the lane redacts the role name (`user "***"`).
  - **no `(psycopg2.` in the reason** → it never reached the driver or was not a DB error. Read the `VERDICT: FAIL` line's lead: `observation failed:` with `ValueError` / `ArgumentError` → **run the probe from T8(c) first**: the display module can raise `ValueError` during observation too, so the exception class alone must not send anyone to the env file. If the probe fails with the same exception, the DSN in the env file does not parse → fix `DATABASE_URL` in the env file §11.4 names (measured: R6, the probe reproduces the lane's `ValueError` exactly); if the probe succeeds, it came from the display module → T8(d). `observation failed:` with any other class → the display module raised during observation; `observation unusable:` → `evaluate()` rejected the observation. Both of the last two get a first step (T8d).
  Mark which forms were **observed live** (refused, wrong database, password failure, missing database, unparsable DSN — receipts R2, R5) and which are **derived** from the driver's class hierarchy or the code (timeout, grants, `AdminShutdown`, `InterfaceError`, the two display/evaluate legs). Quote one real redacted reason as the example.
- [ ] T8 Runbook §11.3:
  - (a) branch C's 「见 §2」 → 「见 §3.1」.
  - (b) the 退出 3 entry opens by stating it owns `COVERAGE_FRESHNESS_NO_SOURCES` only, and where every other exit-3 cause goes. **Remove its 「再查只读角色权限」 step from the NO_SOURCES path**: a missing grant raises `InsufficientPrivilege` (an `OBSERVATION_FAILED` reason), it never returns zero rows — there is no row-level security under `db/`. NO_SOURCES's second step becomes: confirm which database the lane actually reads — `systemctl --user show -p EnvironmentFiles nhms-node27-coverage-freshness-alert.service` (a scratch drop-in left behind from §11.5 points it at a database with tables but no data) plus the probe from (c).
  - (c) one new short entry for the database-side `OBSERVATION_FAILED` causes, using **only** node-27-verified commands (R4, R5): `docker ps -a --filter name=^nhms-db$ --format '{{.Names}} {{.Status}} {{.Ports}}'`; the probe through the lane's own venv + env file reporting `current_database()`, `current_user` and `to_regclass('hydro.hydro_run') is not null`; and the `pg_stat_activity` query for timeouts. Grants (`InsufficientPrivilege`) get their own line here: `nhms_display_ro` needs SELECT on `hydro.hydro_run`, `hydro.run_display_coverage`, `core.model_instance`. Recreation is **not** a first step — say "diagnose before rebuilding" and point at §5.1, which carries the recreation pointer. `pg_isready` must not appear (R4).
  - (d) a first step for the two non-driver legs: reproduce by hand with §11.5's manual block (same env file, so the same line comes back), check recent changes to the display module (`git log -5 --oneline -- services/tiles/mvt.py`), and treat it as a code defect in the display module or in this lane's contract with it — record the reason line when reporting it.
- [ ] T9 Runbook §11.5:
  - (a) the exit-2 import-stage case: first the import check `PYTHONPATH=/home/nwm/NWM .venv/bin/python -c 'import services.tiles.mvt'`. **If it fails**, the venv lacks the display stack → `export PATH=$HOME/.local/bin:$PATH && uv sync --all-extras --dev`, and say why that is not a default (R4: on this host it installs packages into the shared production venv). **If it passes** — the check sets `PYTHONPATH` itself, so it cannot see a unit that lacks it — run `systemctl --user show -p Environment nhms-node27-coverage-freshness-alert.service`; no `PYTHONPATH=` there → re-run the install block above.
  - (b) rewrite the governance paragraph to the measured truth: the receipt carries each unit's `LoadState` / `UnitFileState` (after T2); a disabled timer reads `loaded` + `disabled`, a never-installed one `not-found` + empty; the timer is normally `enabled`, while the **service is `static`** (it has no `[Install]` section — R5), which is not a fault; `list-timers --all` shows neither a disabled nor an absent timer (R1); and none of this is an alert.
- [ ] T10 Runbook §10.8: bring the sibling sentence to the same truth — the receipt makes a disabled timer identifiable on periodic reading; it is not discovered automatically.
- [ ] T11 Append `## 修正 2026-09-18（#2472）` to the archived `openspec/changes/archive/2026-09-18-node27-coverage-freshness-alert/design.md`, naming D3's 「roughly four framing lines」 (measured: five on the exit-1 path, no lane stderr line there) and the test-list line 「breaching sources kept」 (bounded, not absolute), pointing at the live spec for the corrected contract, and stating the archived spec delta is left as the #2080 snapshot. Original lines untouched. Must contain the literal marker `修正 2026-09-18（#2472）`.

## Required evidence

Every item is a predicate over the tree, not a count that the change itself can perturb.

- [ ] E1 `uv run pytest -q tests/test_node27_resource_governance.py tests/test_node27_coverage_freshness_alert.py tests/test_select_ci_tests.py` — all pass, every pre-existing test included.
- [ ] E2 `uv run ruff check .` — zero findings.
- [ ] E3 `openspec validate fix-coverage-freshness-spec-and-routing --strict --no-interactive` — valid.
- [ ] E4 Red proofs, labelled honestly: T3 and T4 are **genuinely red on master's tree** (master's collector lacks both properties; master's §11 names none of the three codes) — run each against `origin/master`'s version of the file under test and paste the failure. T3(b) is also a characterization pin (the audit already ignores the `systemd` section) — its red proof is a scratch mutation making `_recommendations` read the timer's state. T5 is a **characterization pin** (the code already behaves so): its red proof is a mutation of the reason format in a scratch copy (e.g. drop the class-name prefix) showing the pin goes red; do not present it as new behaviour.
- [ ] E5 Verbatim restatement: for each of the three MODIFIED requirements, extract the requirement block from `openspec/specs/display-coverage-freshness/spec.md` (`git show origin/master:…`) and from the delta and `diff` them. Predicate: every differing line belongs to the designated text in T1 (the truncation THEN/AND lines; the added obligation sentence and two scenarios; the load/unit-file-state clause, the rewritten THEN and one scenario). Paste the three diffs.
- [ ] E6 `grep -c "breaching sources are kept" openspec/changes/fix-coverage-freshness-spec-and-routing/specs/display-coverage-freshness/spec.md` = 0, and the rewritten truncation scenario contains none of the literals `19`, `20`, `24`.
- [ ] E7 Routing completeness (#2473's own ACs, as predicates): for every code in `grep -o 'COVERAGE_FRESHNESS_[A-Z_]*' scripts/node27_coverage_freshness_alert.py | sort -u`, `sed -n '/^## 11\./,/^## 12\./p' docs/runbooks/current-production-ops.md | grep -q "$code"` succeeds. (T4 turns this into a standing test.)
- [ ] E8 In §11, `uv sync` appears, and the import check appears **before** it in §11.5's text.
- [ ] E9 The sentence naming `DISPLAY_COVERAGE_REFRESH_REFUSED` in §11.3 cites §3.1, and the `## 3.` section (up to `## 4.`) contains `DISPLAY_COVERAGE_REFRESH_REFUSED`.
- [ ] E10 `git diff origin/master -- scripts/node27_coverage_freshness_alert.py` is empty.
- [ ] E11 `git diff origin/master -- openspec/changes/archive/` contains only `+` lines, all inside the new `修正 2026-09-18（#2472）` note; the archived spec delta is absent from that diff.
- [ ] E12 No false claim survives in the rewritten sentences: in §10.8 and §11.5, no sentence asserts that `list-timers` distinguishes disabled from absent, and none describes the governance receipt as an alert or as automatic discovery. `pg_isready` does not appear in §11. Checked by reading, with the greps pasted.
- [ ] E13 Receipt readers: `git grep -n '"systemd"\]\|get("systemd")' -- '*.py' '*.sh'` across the whole repo — every hit is the writer or a test; no reader depends on the exact property set.
- [ ] E14 (orchestrator, node-27) Every shell command added to §11 by this change was executed on node-27 before landing, rc recorded — receipts R4 and R5 cover `docker ps`, the extended probe, `pg_stat_activity`, the import check, `uv sync --dry-run`, `systemctl --user show -p Environment` / `-p EnvironmentFiles`; the implementer must list every other command it writes so the orchestrator runs it before the PR is opened, and every quoted example reason must match a receipt verbatim (modulo redaction).
- [ ] E15 (orchestrator, node-27) Governance audit at the branch head (isolated worktree, production checkout untouched), invoking the python script directly — not the `_once.sh` wrapper — with `NODE27_GOVERNANCE_SUMMARY_PATH` unset and an explicit `--summary-path` under `/home/nwm/tmp/`, so no production summary is written: every unit in the receipt carries `LoadState` and `UnitFileState`; the coverage timer reads `loaded` / `enabled` and the coverage service `loaded` / `static`; the audit's exit code and recommendation count equal a run of `origin/master`'s script in the same session under the same conditions.

## Recorded deviations from the two issues' own wording

- **#2472 AC 「`git diff --stat -- scripts/` 为空」** cannot hold: `scripts/node27_resource_governance.py` changes, because §11.5's claim about distinguishing a disabled timer turned out false and the receipt could not support it (receipt R1) — surfaced mid-way, fixed per the directive. The AC's intent (zero behaviour change in the alert lane, #2472 is spec text only) is kept as E10: the alert script is byte-identical.
- **#2473's `ProgrammingError → grants` routing** is replaced by class-plus-driver-class routing, because a wrong-database DSN also reads `ProgrammingError` (R2).
- **#2473's pointer to container recreation** is not used as a first step: an alerting mail's first step is diagnosis, and recreation stays one hop further behind §5.1's existing pointer. (The runbook must not describe §4.3.3 as forbidden in general — `tier-node27-timeseries-storage.md` forbids it only for the cold bind.)
- **#2473 proposes `uv sync` as the remedy**; it becomes conditional on a failed import check, because on node-27 it would mutate the shared production venv (R4).
- **Scope beyond both issues**, all surfaced while doing them and fixed per the directive: the CI selector's runbook rule missing the test that now reads the runbook; the NO_SOURCES path's wrong 「再查只读角色权限」 step; §11.5's false list-timers claim and the governance collector gap behind it; the governance requirement's scenario that overclaimed; §10.8's automaticity overclaim; §11.3 branch C's §2 → §3.1; the archived design.md's two stale claims.

## Non-goals (design boundaries, each with its reason — none is a parked defect)

- **Making the governance audit alert on a disabled timer.** That would change the audit's exit semantics for every inventoried unit of every lane, which #2466 explicitly held fixed; it is a policy decision for the governance lane, not a correction. After this change no document claims the audit alerts — the defect (a false claim) is removed, and the boundary is stated in the spec (T1c).
- **Syncing node-27's production venv.** R4 found it does not match `uv sync --all-extras --dev` (three packages absent) while the lane imports fine. That is host state, not a defect in this lane, and changing a shared production venv is an operator action outside these issues. Reported to the user.
- **Changing the alert script** in any way, including its reason format or `MAX_REPORT_LINES`.
