# Memoise the pure work of completion scoring

Issue: #3236 (part of #3217, follow-up of #3223). First stage; the issue stays
open for the journal read path.

## Why

An idle scheduler pass on the compute control node takes about 260 s of wall
time and 225 s of one core to conclude "nothing to do", every five minutes.
A sampling profile of one real idle pass (2026-11-25, `588faf2f7`, 27,804
samples, posted on the issue) puts 97.5 % of it in completion scoring, which
asks the file journal for the public candidate state of every (cycle, model):
8 cycles x 66 models. Each sample in exactly one row:

| where | share |
|---|---|
| public sanitising of the state | 43.7 % |
| reading and parsing the cycle's rows | 19.4 % |
| raw-manifest readiness, once per model | 14.9 % |
| containment fingerprint of the cycle's rows | 11.0 % |
| everything else | 11.0 % |

Two of these rows repeat pure work:

- **Sanitising.** 31.7 % is the text sanitiser on string values, 5.8 % the
  "is this key sensitive" test on field names, 2.7 % the path/URI test on
  scalars. All three are functions of one string and nothing else, and the
  same strings come back for every model of a cycle (cohort rows are
  attributed to every candidate) and twice per state (events are sanitised
  before the state is built and again with the whole state).
- **Raw-manifest readiness.** It depends on source and cycle only, verifies
  every raw file of the cycle, and is recomputed for each of the 66 models.

## What changes

1. `services/orchestrator/public_evidence.py` remembers, in bounded caches,
   the result of the text sanitiser and of the scalar path/URI-then-text
   sanitiser per input string, and the sensitive-key answer per field name.
   The output for every input is what it was.
2. `services/orchestrator/source_cycle_raw_manifest.py` gets a scope in which
   the readiness of a (source, cycle) is computed once; each caller inside the
   scope receives its own copy. `scheduler_discovery._cycle_completion_verdict`
   scores one cycle inside such a scope. Outside a scope nothing changes.

## What does not change, and why this shape

- **The state the ladder sees.** The issue's option 2 was a scoring-only state
  that skips the public wrappers. The wrappers are not only redaction: URIs
  and paths become placeholders, and the ladder and the init-state match rely
  on that ("withheld placeholder" mode: `terminal_init_state_match` skips a
  placeholder and compares a real URI; the artifact guard probes the object
  store for a real URI). A raw state could turn `complete` into `gap`.
  Memoising the same functions removes the cost and keeps every byte.
- **The journal read path** (`_cycle_rows`, its fingerprint, the #1567 and
  #1661 rules): not touched. One read per cycle (option 1) is the larger and
  riskier half; it is decided after this stage is measured in production.
- **No verdict is cached** (option 3, and #3223's non-goal), within or across
  passes.
- **The submitting path** computes readiness as before; it only benefits from
  the sanitiser caches, which cannot change its output.

## Decisions

- **Caches are process-wide and bounded** by entry count and by input length
  (longer strings are computed without caching). The module is also imported
  by the display API, a long-running process; the bounds cap its memory: an
  entry holds input and output, typically about 16 MB per text cache when
  full, more for pathological inputs (the code comment gives the formula).
  The constants are sized for one pass and confirmed by the cache statistics
  of a real pass before deployment (tasks, section 5).
- **The key cache wraps `is_sensitive_key` locally.** `packages/common/redaction.py`
  is shared by everything and is not edited.
- **The readiness scope is one cycle's scoring**, not the pass: the other
  readiness callers of a pass (source adapters, the predecessor gate, the file
  providers) keep reading the filesystem themselves.
- **A copy per caller.** The readiness mapping holds nested lists and
  mappings and ends up inside each candidate's state.
- **A failure is not remembered.** If the readiness call raises, the next
  caller in the scope calls again.
- **One scope per verdict.** The breaker re-scores the oldest available gap
  through the same function, in a scope of its own, so a cycle can cost two
  readiness computations in a pass that has gaps. The strict and successor
  providers called from the verdict do not read readiness; the only reader
  inside a verdict is the journal's candidate state.

## Expected effect

About 40 % of the pass from the sanitiser caches and about 15 % from the
readiness scope, less their own overhead: an idle pass of roughly 120-130 s.
`journal_read_attribution` is expected unchanged (no journal read is removed).

## Known limits

- Within one cycle's scoring the 66 states share one readiness snapshot. A raw
  file that changes during those seconds is seen by all or none of them; today
  it could be seen by some.
- The caches hold sanitiser inputs and outputs in memory for the life of the
  process. The inputs are values the process already holds; the outputs are
  the public form.
- About 30 % of the pass (cycle rows and fingerprint) remains.

## Impact

- Code: `services/orchestrator/public_evidence.py`,
  `services/orchestrator/source_cycle_raw_manifest.py`,
  `services/orchestrator/scheduler_discovery.py`.
- Tests: one new file for the caches, one for the readiness scope and its use
  in scoring; existing tests unedited.
- Spec: `production-scheduler-orchestration`, one added requirement.
- Deployment: compute control node, at a pass boundary, after a plan-only pass
  of the new code has been compared with production (tasks, section 5).
