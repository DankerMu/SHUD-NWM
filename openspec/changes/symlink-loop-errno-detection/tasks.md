# Tasks: symlink-loop-errno-detection (#1332)

Issue: #1332 (S code / compact fixture). Branch:
`feat/issue-1332-symlink-loop-errno`, base master 9f8433bf.

## Evidence Floor

- `uv run pytest -q tests/test_basins_discovery.py
  tests/test_basins_package_publication.py`
- `uv run pytest -q tests/test_production_scheduler.py -k
  symlink_loop`
- `uv run ruff check .`
- `openspec validate symlink-loop-errno-detection --strict
  --no-interactive`
- Cross-version proof: the three A1-A3 tests green on BOTH local
  CPython 3.14.2 and a py3.11 interpreter (hand-built venv from
  the #1330 run, or CI py3.11 as the second leg).

## Deviations (recorded up front)

1. Issue line numbers are from `bccf92f5`-era heads; the three
   files have zero diff since, so cites remain valid on 9f8433bf
   (re-verified by reading the sites).

## 1. Fixture

- [x] 1.1 proposal/design/tasks + spec delta authored (this commit)
- [x] 1.2 Reviewer fixture review (read-only) until clean
  (two-iteration repair bound per workflow contract)
  - Round 3 CLEAN — approved for implementation (both round-2 P2
    repairs verified propagated; four notes verified self-
    consistent; reviewer additionally proved the D2.1
    prescription via monkeypatch: 88 passed, A3 restored by site
    1 alone, ENOTDIR tightening breaks nothing). One note closed
    same-round: D4/risk-triage "nil delta via earlier
    interceptor" had the call order backwards (`:159` resolve
    precedes `:162` interceptor) — rewritten as the
    hard-error→blocking-warning convergence the new spec scenario
    ratifies; A2 cite corrected to def `:29822`. Reminder kept:
    A4(b) OUT_OF_ROOT-priority pin is the P2-3 regression
    backstop and MUST land in implementation.
  - Round 1 NOT CLEAN (1 P1, 2 P2, 1 P3) — repaired (iteration
    1/2; reviewer probed 3.11/3.12/3.13/3.14 empirically): P1
    `Path.resolve(strict=True)` raises errno-less RuntimeError on
    ≤3.12 — a literal port crashes the production interpreters →
    predicate mandated to `os.path.realpath(strict=True)` only,
    RuntimeError-arm hardening rule added. P2-2 package-site
    attribution false (A3 is restored by the discovery fix alone,
    proven by monkeypatch; `BASINS_DIRECTORY_UNREADABLE` comes
    from basins_discovery.py:546) → trace duty removed, site 2
    rewritten to its real degradation (loop → SOURCE_NOT_FOUND vs
    py3.11 UNRESOLVABLE), own code preserved (never `_UNSAFE`),
    new anchor A6. P2-3 preflight ENOENT early-return would drop
    OUT_OF_ROOT priority (`:581` before `:591`) → continue-ladder
    semantics + new OUT_OF_ROOT pin in A4(b); code name corrected
    to `SLURM_PREFLIGHT_{FIELD}_NOT_VISIBLE`. P3 "≤3.12
    byte-identical" false for EACCES/ENOTDIR lanes + phantom
    "preflight spec" cite → must-preserve narrowed to loop+ENOENT
    lanes, cite corrected to slurm-array-runner-integration:38-42
    (blocker-level, no code granularity; "no delta needed"
    conclusion stands).
  - Round 2 NOT CLEAN (0 P1, 2 P2, 4 notes) — repaired (iteration
    2/2, the bound; all four round-1 repairs verified sound with
    fresh four-interpreter probes, A6 RED direction empirically
    confirmed at basins_package.py:639): P2-1 proposal Why item 2
    still carried the refuted round-1 attribution
    (DIRECTORY_UNREADABLE at site 2) → rewritten to site 2's real
    degradation (loop → SOURCE_NOT_FOUND :636-644 vs py3.11
    UNRESOLVABLE), A3's observed delta moved under site 1's
    downstream reach. P2-2 tasks 2.1 still ordered the deleted
    trace duty → replaced with the settled site-2 constraint.
    Notes folded same-round: no-delta rationale for site 2
    re-based on version parity (code unspecced, A6 first pin); D4
    records the package-site ENOTDIR drift (file/sub →
    UNRESOLVABLE on all versions, :1477 pin is ENOENT-lane-safe);
    A4(a) target-inside-root placement clause; evidence mapping
    made exact (A1→AC1/4, A2→AC3, A3→AC2, A4→AC5, AC6=floor,
    A5/A6 unmapped by design).
- [x] 1.3 `openspec validate symlink-loop-errno-detection --strict
  --no-interactive` green (re-run after every repair round)

## 2. Implementation (implementer subagent)

- [ ] 2.1 Three call-site edits per design D2 (strict
  `os.path.realpath(strict=True)` + ENOENT split; package site:
  non-ENOENT raises the helper's OWN
  `BASINS_PACKAGE_PATH_UNRESOLVABLE`, never re-coded to
  `_UNSAFE` — its py3.11 classification point is settled at
  `basins_package.py:2825-2830`, untouched; preflight: ENOENT
  continues the contained→visible ladder, non-ENOENT →
  `SLURM_PREFLIGHT_{FIELD}_UNSAFE_PATH`). Nothing else.
- [ ] 2.2 Anchors per design D3: A1-A3 existing tests go RED(3.14
  current)→GREEN with zero test edits; A4 ENOENT anchors per site
  (cite-or-add rule; A4(a) dangling entry at a resolved path like
  `forcing`; A4(b) adds the missing OUT_OF_ROOT-priority pin),
  RED-provable against a naive port without the ENOENT split
  (differential recorded); A5 publish-path test untouched and
  green; A6 package-source loop → `BASINS_PACKAGE_PATH_UNRESOLVABLE`
  (RED on current 3.14).
- [ ] 2.3 Evidence floor + ruff green on 3.14; py3.11 leg recorded;
  deviations reported explicitly ("no deviations" stated if none)

## 3. PR

- [ ] 3.1 Commit + push; PR with 变更摘要 / 偏离记录 / 测试证据 /
  Evidence-Floor 声明
- [ ] 3.2 CI green (targeted Unit Tests, py3.11 — the second
  matrix leg)

## 4. Review loop

- [ ] 4.1 Cross-review rounds per gate ledger; candidates → dedup →
  per-class verifier batches; findings verified before fix
- [ ] 4.2 Phase 7 final review clean on final head

## 5. Merge (pre-authorized) and closeout

- [ ] 5.0 Follow-ups routed with numbers (if any arise in review)
- [ ] 5.1 Chinese work summary + evidence posted; CI green on final
  head
- [ ] 5.2 Merge; archive change; loop-log line + audit; close issue
  #1332
