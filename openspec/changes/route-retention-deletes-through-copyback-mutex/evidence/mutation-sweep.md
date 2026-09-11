# Evidence Floor mutation sweep — archived record

Three sweeps were run over `tests/test_retention_copyback_mutex.py`'s Evidence
Floor. This file is the auditable record. Its predecessor lived under
`.workplans/`, which `.gitignore` ignores, so `tasks.md` rested the Evidence
Floor on a file that did not ship; the distinction this move fixes is
gitignored-versus-in-tree, not recorded-versus-unrecorded.

## Frame

Sweeps A and B ran at commit `3854b596`. Sweep C ran at this change's head.

Every mutant that alters removal behaviour targets
`services/orchestrator/retention.py`, and that module is AST-identical between
`3854b596` and the head: `ast.dump(..., include_attributes=False)` over both
parses, with every Module/Class/Function docstring node stripped, compares
EQUAL. Three rows of sweep B target other files instead, and each is
reconstructable for its own reason:

- the two call-site rows target `services/orchestrator/scheduler_runtime.py` and
  `services/orchestrator/cli.py`, neither of which appears in
  `git diff --stat 3854b596 <head>` at all;
- the broad-rule row targets `scripts/select_ci_tests.py` and
  `tests/test_select_ci_tests.py`, which changed comment-only over that range
  (test-count comments, `21 tests in 3.04s` → `25 tests in 5.27s`). The rule's
  member list is unchanged, so the mutant applies as written.

The suite is not byte-identical to `3854b596`: one test was added after sweeps A
and B (`test_ef11_a_long_uncontended_hold_is_not_charged_against_the_pass_budget`,
EF-11's third case) and the EF-5 banner comment was rewritten. Sweep B's
baseline count is therefore one test short of the head, and sweep B's failure
sets were never re-measured at the head. Sweep C closes that.

## Sweep A — implementer, at `3854b596`

22 mutants over EF-1..EF-16, reported as "each one reds the clause that names
it". **The enumeration was not preserved** and cannot be re-audited from this
repository. It is recorded here as an unverifiable claim, not as evidence.

## Sweep B — round-4 test-evidence reviewer, at `3854b596`, independent

18 mutants, constructed without reference to sweep A, via an in-memory
source-mutation pytest plugin (no repository file modified). Baseline at that
SHA for the four-file batch `tests/test_retention_copyback_mutex.py`,
`tests/test_copyback_guard.py`, `tests/test_retention_extra_roots.py`,
`tests/test_retention.py`: 117 passed / 3 skipped. (The same four at the head
are 118 passed / 3 skipped — exactly the one added test.)

| mutant (target) | result vs baseline |
|---|---|
| acquire after `rmtree` | 9 failed, incl. EF-1, EF-2 |
| lock every extra root | 6 failed, incl. EF-3 |
| drop the `extra_roots` membership check | **0 failed — survives** |
| …the same, plus a pass-level acquire (double mutant) | 9 failed, incl. EF-5 and EF-6-overlap |
| blank root falls back to the first extra root | 4 failed, incl. all 3 EF-7 params |
| hold the lock across the whole pass | 2 failed, incl. EF-8 — **target under-determined, see below** |
| `CopybackLockError` out of the `except` tuple | 4 failed, incl. EF-9 |
| catch `CopybackLockTimeout` only | 2 failed, incl. EF-10 |
| zero-write pass acquires anyway | 2 failed (dry-run, disabled) |
| the mutex changes selection | 9 failed, incl. EF-13 — **target under-determined, see below** |
| widen the enumeration and drop the `is_dir` filter (double) | EF-14 reds on the lock file's key |
| drop `copyback_root=` at the scheduler call site | 1 failed — exactly the scheduler case |
| drop `copyback_root=` at the CLI call site | 1 failed — exactly the CLI case |
| drop the suite from the broad `services/orchestrator/**` rule | 8 failed in `tests/test_select_ci_tests.py`, incl. the frozen-literal pin |
| `release_copyback_batch_lock` out of its `finally` | 2 failed — both params of the round-3 T1 pin |
| `timeout_seconds=budget_seconds` per acquire | 1 failed — the charged-wait case |
| charge the budget in `except` instead of `finally` | 1 failed — the charged-wait case |
| drop the `remaining_seconds <= 0` refusal | 1 failed — EF-11's acquisition-count assertion |

**Two rows do not reconstruct.** Sweep C rebuilt all 18 from the target column;
16 reproduce their recorded counts, and two do not, because the target text
admits several inequivalent source edits:

- "hold the lock across the whole pass" — caching the fd on the budget and
  reusing it across trees gives 6 failed; acquiring at pass start and releasing
  at pass end gives 9. Never 2, even after subtracting the one test added since.
- "the mutex changes selection" — this names the negation of an EF *clause*, not
  a source edit. The natural reading (skip entries on the copyback root) gives
  16 failed.

For these two rows the recorded counts stand as unverifiable, the same status as
sweep A. What they claim — that EF-8 and EF-13 are backed — is independently
re-established by sweep C.

## Sweep C — round-1 test-evidence reviewer, at this change's head, independent

27 mutants, in-memory as above, counted against the 25-test mutex suite unless
a row says otherwise (`git status` clean afterwards). The 27 comprise 18
rebuilds of sweep B's targets, 8 mutants new to sweep C, and the `< 0`
survivor below. The zero-write row in the table is a rebuild of sweep B's, not
a ninth new mutant; the identity and negative-control runs that validated the
harness are not counted.

**25 red. Two survive, both accounted for:**

- dropping the `extra_roots` membership check — sweep B's survivor, reproduced.
  The double mutant that also adds a pass-level acquire reds EF-5 and
  EF-6-overlap. Redundant second line of defence, not an uncovered clause.
- widening the budget refusal from `remaining_seconds <= 0` to `< 0` — an
  **undetected survivor, not an equivalent mutant**. An earlier draft of this
  line called it equivalent, reasoning that the budget is decremented by a
  strictly positive difference so exactly `0.0` is unreachable. That covers the
  decrement path and misses the seed: `run_retention` starts
  `remaining_seconds` at `float(copyback_lock_wait_budget_seconds)`, so a
  caller passing `0.0` is at exactly `0.0` before any charge. Measured on one
  fixture at that seed, the real code records the budget-exhausted error and
  the mutant records `copyback batch lock timeout must be positive` — raised
  inside `copyback_guard.resolve_copyback_lock_timeout_seconds`, so the mutant
  reaches the acquire the module's own contract says it must not. The
  divergence is confined to that seed: at `0.4` both builds behave identically,
  and in both the tree survives and lands in `failed[]`. No production caller
  passes the keyword, the default is 300 s, and the lowest value any test uses
  is `0.4`, so nothing shipped reaches it. Recorded rather than closed with a
  test, which would be scope this change does not own.

**No EF clause was found over-claimed.** Rows worth recording individually:

| mutant (target) | result |
|---|---|
| charge acquire and hold as ONE span | 1 failed — **exactly EF-11's third case**, its sole carrier |
| charge nothing (empty the `finally` body) | 2 failed — EF-11's first and second cases |
| release before `rmtree` | 4 failed — EF-2, EF-4, both EF-15 legs |
| acquire on `path.parent` instead of the copyback root | 11 failed, incl. EF-4 |
| zero-write acquire hung on the early-return path only | 2 failed — exactly EF-12's dry-run and disabled params |
| per-entry budget instead of pass-level | 2 failed — EF-11's first and second cases |
| swallow `CopybackLockError` silently (no `failed[]` entry) | 4 failed, incl. EF-9, EF-10 |
| count failed entries into `freed_bytes` | 4 failed in this suite, incl. EF-9, EF-10 (6 across the four-file batch) |
| drop the suite from the `cli.py` stop rule | 6 failed **in `tests/test_select_ci_tests.py`**, outside this sweep's suite, incl. the at-site ordering pin — EF-16's CLI leg |

EF-4 is redded by two of the rows above (four mutants across the full sweep);
EF-12's `extra_roots_enabled=False` parameter
is redded by the membership double mutant. Neither is named in sweep B's
non-exhaustive "incl." lists, which is why `tasks.md` now records where their
coverage comes from.

**Flake.** 15 suite runs with 28 CPU burners on 14 cores (load average peak
36.84): 15/15 green, 0 failures. The three timing-sensitive tests were stable
across all 15 (EF-11 third case 0.61–0.62 s, first case 0.51 s, second case
0.31–0.33 s). Uncontended acquire latency under 40 burners, n=300:
max 0.10 ms against EF-11's third case tolerance of 0.15 s — a margin of ~1490×.
Sweep B separately recorded 42 runs under 28-way oversubscription, also green.

**Routing, measured through the real selector** (API and CLI agree):

| changed module | suites selected | mutex suite included |
|---|---|---|
| `services/orchestrator/retention.py` | 49 | yes |
| `services/orchestrator/cli.py` | 30 | yes |
| `services/orchestrator/__init__.py` | 48 | yes |
| `tests/retention_test_helpers.py` | 6 | yes |
| `services/orchestrator/scheduler_runtime.py` | 22 | no |
| `packages/common/copyback_guard.py` | 9 | no |

The last two rows are the Known limits this change records, confirmed rather
than asserted.

## Independent re-measurement of this record

Sweep C first reached this file as a transcription of one reviewer's report,
which made its numbers second-hand to the document. A second reviewer, in a
later round and without access to the first, rebuilt all 27 mutants in-memory at
this head from the target descriptions above and re-ran the campaign. It
reproduced the headline (25 red, 2 survive), the sole-carrier result for EF-11's
third case, all six rows of the routing table, every Frame claim, and the four
structural legs `tasks.md` names. It also validated its harness in both
directions — an identity mutant reproducing the four-file baseline exactly, and
a negative control failing at collection when its pattern was absent.

Three discrepancies came out of that re-measurement, and all three are corrected
above: the `< 0` survivor's "equivalent mutant" label, the EF-4 count, and the
composition of the 27. A fourth was raised and rejected on the record's own
terms: the latency margin was re-measured under 28 burners against the recorded
40, which is a different load condition, not a failed reproduction.

## What a reader can check

Sweep C is reconstructable at the head as written. Sweep B's mutants are
reconstructable from the target column except for the two rows marked above.
Sweep A's are not. Where `tasks.md` states that the floor is mutation-backed,
sweep C is the current evidence and sweep B the corroborating earlier one.
