# Evidence Floor mutation sweep — archived record

Two independent sweeps were run over `tests/test_retention_copyback_mutex.py`'s
Evidence Floor. This file is the auditable record; before it existed, the only
account was a sentence in `tasks.md` and a commit message — an inspection-only
claim about not trusting inspection.

## Frame

Both sweeps were run at commit `3854b596`. At this change's head,
`services/orchestrator/retention.py` — the module every mutant targets — is
AST-identical to that commit: `ast.dump(..., include_attributes=False)` over
both parses, with every Module/Class/Function docstring node stripped, compares
EQUAL. So each mutant below is reconstructable at the head as written.

The suite itself is **not** byte-identical to `3854b596`: one test was added
after the sweep (`test_ef11_a_long_uncontended_hold_is_not_charged_against_the_pass_budget`,
EF-11's third case) and the EF-5 banner comment was rewritten. The baseline
counts recorded below are therefore one test short of the head, and the failure
sets were not re-measured at the head.

## Sweep A — implementer, at head `3854b596`

22 mutants over EF-1..EF-16, reported as "each one reds the clause that names
it". **The enumeration was not preserved** and cannot be re-audited from this
repository. It is recorded here as an unverifiable claim, not as evidence.

## Sweep B — round-4 test-evidence reviewer, at head `3854b596`, independent

18 mutants, constructed without reference to sweep A, via an in-memory
source-mutation pytest plugin (no repository file modified). Baseline for the
four-file batch at that SHA: 117 passed / 3 skipped.

| mutant (target) | result vs baseline |
|---|---|
| acquire after `rmtree` | 9 failed, incl. EF-1, EF-2 |
| lock every extra root | 6 failed, incl. EF-3 |
| drop the `extra_roots` membership check | **0 failed — survives** |
| …the same, plus a pass-level acquire (double mutant) | 9 failed, incl. EF-5 and EF-6-overlap |
| blank root falls back to the first extra root | 4 failed, incl. all 3 EF-7 params |
| hold the lock across the whole pass | 2 failed, incl. EF-8 |
| `CopybackLockError` out of the `except` tuple | 4 failed, incl. EF-9 |
| catch `CopybackLockTimeout` only | 2 failed, incl. EF-10 |
| zero-write pass acquires anyway | 2 failed (dry-run, disabled) |
| the mutex changes selection | 9 failed, incl. EF-13 |
| widen the enumeration and drop the `is_dir` filter (double) | EF-14 reds on the lock file's key |
| drop `copyback_root=` at the scheduler call site | 1 failed — exactly the scheduler case |
| drop `copyback_root=` at the CLI call site | 1 failed — exactly the CLI case |
| drop the suite from the broad `services/orchestrator/**` rule | 8 failed in `tests/test_select_ci_tests.py`, incl. the frozen-literal pin |
| `release_copyback_batch_lock` out of its `finally` | 2 failed — both params of the round-3 T1 pin |
| `timeout_seconds=budget_seconds` per acquire | 1 failed — the charged-wait case |
| charge the budget in `except` instead of `finally` | 1 failed — the charged-wait case |
| drop the `remaining_seconds <= 0` refusal | 1 failed — EF-11's acquisition-count assertion |

Two behaviours `tasks.md` records as caveats were confirmed honest by this
sweep: the membership check is a redundant second line of defence (single
mutation survives, double mutant reds), and EF-14 admits no removal mutation
because the lock file is excluded from the walk by construction.

The reviewer also ran the suite 42 times under 28-way CPU oversubscription
(load average to 117): 42/42 green, no timing fragility.

## What a reader can check

Sweep B's mutants are reconstructable from the target column against a
`retention.py` that has not changed since; sweep A's are not. Where `tasks.md`
states that the floor is mutation-backed, sweep B is the evidence it rests on.
