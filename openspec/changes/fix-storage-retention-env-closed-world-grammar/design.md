# Design: fix-storage-retention-env-closed-world-grammar (#1230)

## D1 — Grammar and refusal point

In `_scan_env_assignment` (`packages/common/storage.py:328-342`), the
per-line dispatch changes from open-world to closed-world:

- A line is LEGAL iff it is (after `strip()`): empty, a full-line `#`
  comment, or a fullmatch of `_ENV_ASSIGNMENT_PATTERN`
  (`[export ]KEY=VALUE`, ANY variable name).
- `matched is None` on a non-empty non-comment line raises
  `ArchiveConfigurationError` IMMEDIATELY (first offending line in file
  order — deterministic), replacing today's silent `continue`. Message
  carries the path AND the offending line: e.g.
  `"retention env line is not a supported assignment in {path}:
  {candidate!r} — every line must be blank, a full-line # comment, or a
  [export ]KEY=VALUE assignment; refusing instead of guessing what the
  shell would export"`.
- Raising inside the scan follows the existing precedent (non-newline
  line breaks `:317-323`, unquoted leading whitespace `:344-348`).

Coverage check against the issue's 8 differential shapes: `VAR+=21`
(`+` breaks the name class → no fullmatch), `VAR=14`+`VAR+=7`,
`: ${VAR:=21}`, `. other.env`, `source other.env`, `printf -v VAR 21`,
`read VAR <<< 21`, `eval 'VAR'=21` — none fullmatch; all refuse. Nested
source is caught precisely because the GRAMMAR is judged, not the
variable name (alternative (a), bare-token detection, cannot see it —
rejected per issue).

Monotonicity invariant (the must-preserve): the acceptance set STRICTLY
SHRINKS. Grammar refusal only fires on lines that today hit `continue`
(mention-refusal lines that fullmatch keep their path; non-matching
lines that today feed nothing now refuse). No input flips
refuse→accept; every previously-refused input refuses with an
equally-or-more-specific message. The differential oracle's fail-closed
early-return arm (`tests/test_storage.py:1196-1197`) makes added
refusals free.

## D2 — Mention layer kept, message localized

Post-grammar, `mentioned_unaccepted` (`:334-335`) is reachable via TWO
conforming-line paths (fixture-review P3-1): a line whose VALUE embeds
`NAME=` (e.g. `X=NODE27_TIMESERIES_RETENTION_WINDOW_DAYS=21`), and a
line whose KEY merely ENDS with the name (suffix decoy, e.g.
`OLD_NODE27_TIMESERIES_RETENTION_WINDOW_DAYS=99` — conforming,
accepted as OLD_*'s assignment, still contains `NAME=`). Both refuse
(over-strict for the decoy, fail-closed). The shell would export only
the other variable, so the
helper's family-default answer could arguably be correct — but refusing
is fail-closed and removing the layer would be a semantic RELAXATION of
merged #1229 behavior; out of scope. Kept, with two changes:

- `_EnvAssignmentScan` gains `mentioned_line: str | None` (first
  offending candidate, file order).
- The caller's refusal message (`:260-264`) appends `{line!r}`, matching
  the malformed-value precedent (`:345-348`). Issue acceptance item 3.

## D3 — Test fallout (sized at HEAD)

`tests/test_storage.py`:

- NEW rows in `_UNSUPPORTED_SHAPE_ROWS` (8, issue table order), each
  `f"{_SIBLING}\n<shape>\n"` where the shape needs family context, match
  = the new grammar message fragment (e.g. "not a supported
  assignment"). They flow into `_differential_corpus()` (`:1136-1151`)
  with no extra wiring.
- EXISTING rows whose match string changes (grammar refusal now fires
  before the mention refusal): `readonly-prefix` (:1027),
  `declare-prefix` (:1028), `truncated-quoted-edit` (:1029),
  `mixed-plain-then-readonly` (:1034), `mixed-readonly-then-plain`
  (:1041), `mixed-plain-then-declare` (:1046) — update `match` to the
  grammar message; ids and bodies unchanged (the INPUTS still refuse;
  only the refusing layer moved).
- The mention refusal keeps direct coverage via TWO new rows
  (value-embedding `X={_WINDOW_VAR}=21`, and the KEY-suffix decoy
  `OLD_{_WINDOW_VAR}=99` — both conforming lines) asserting the
  mention message AND the offending line repr — without them the
  `cannot accept as an` branch loses its last test.
- Grammar-refusal message assertion (fixture-review P2-1): a dedicated
  test asserts `repr(offending_line)` appears in the message for at
  least two grammar-refused bodies (acceptance item 1; makes mutation
  probe (ii) killable — the shared shape-row assertion only checks the
  path).
- `multi-line-quoted-value-known-exception` (`:1128-1150`): the closing
  bare `"` line is grammar-refused, so the strict xfail would XPASS.
  Re-record: move `_MULTILINE_QUOTED_BODY` into
  `_UNSUPPORTED_SHAPE_ROWS` as a plain refusal row (grammar match) and
  delete the special xfail append in `_differential_corpus()` — AND add
  the D5(a2) all-conforming body (`{_WINDOW_VAR}=30\nOTHER="\n
  {_WINDOW_VAR}=7\nX=y"`) as a NEW strict xfail differential row (the
  class tripwire; fails the oracle today with helper=7 vs runner=30,
  XPASSes when unbalanced-quote tracking lands).
- NEW template-conformance test bound to BEHAVIOR (fixture-review
  P3-4): for every `infra/env/*.example` (assert >= 15 files), call
  `read_retention_window_days` and assert the outcome is either a
  positive integer or an ArchiveConfigurationError whose message does
  NOT contain the grammar-refusal fragment (archive + non-retention
  templates refuse as "does not look like the deployed retention env";
  the retention template returns 14) — zero grammar-class false
  refusals on shipped templates without re-implementing the grammar in
  the test.
- UNCHANGED locks that must stay green: shipped retention example
  accepted == 14 (`:1107-1112`), both archive templates refused
  (`:1082-1104`), wrong-file/pointer rows (`:1051-1065`).

Docstrings: `read_retention_window_days` (`:220-236` lexical-forms
paragraph) and `_scan_env_assignment` rewritten to state the
closed-world grammar; the "detectable substring" framing disappears
from code comments too.

## D4 — Live evidence (node-27, read-only)

This is pure local parsing logic — local pytest + ruff is the oracle
(issue Verification). One cheap read-only live receipt closes the loop
on the REAL deployed file (the `.example` scan cannot vouch for it):

- On node-27, from a scratch worktree of the PR branch, run the NEW
  `read_retention_window_days` against the deployed retention env path
  (pointer value from the deployed archive env) — assert it returns the
  positive integer actually assigned in the deployed file (read the
  file's own assignment as the expectation; do NOT hardcode 21, which
  is a 2026-08-01 runbook snapshot) and that no grammar refusal fires,
  proving the deployed bytes conform after the tightening.
- Mutation scope (fixture-review P3-4): no DB access, no receipt/lock
  paths, no env sourcing or editing; the scratch worktree itself is a
  git/filesystem write and is removed afterwards. Record the returned
  value + the deployed file's line count.
- If it REFUSES: stop, record verbatim — that is a real pre-deploy
  incompatibility finding, not an evidence failure; the fix would be an
  operator env edit (out of scope, #1228-style routing).

## D5 — Residuals

- (a) Multi-line-quoted-value class: only PARTIALLY closed, in BOTH
  directions (fixture-review P1-1 — the class must never be declared
  fail-open-free):
  - (a1) closing line breaks the grammar (bare `"`): grammar refuses —
    over-strict false refusal, fail-closed, safe;
  - (a2) every line happens to fullmatch (`OTHER="` → value `"`, inner
    `VAR=7`, closing `X=y"` → value `y"`): the grammar accepts, the
    helper reads the INNER line as the last window assignment while
    bash keeps it inside `OTHER`'s string — differentially reproduced
    `helper=7` vs runner `30` with an earlier plain `VAR=30`: STILL
    FAIL-OPEN. Not closable by line grammar; needs unbalanced-quote
    tracking (`_unquote_env_value('"')` returns `'"'` verbatim,
    `:376-380`) — explicitly out of scope, RECORDED in runbook + spec
    (quoted values MUST NOT span lines), and pinned by a NEW strict
    xfail differential row for the (a2) body (replacing the removed
    xfail as the class tripwire; a future unbalanced-quote fix shows
    up as XPASS).
- (b) The grammar accepts any `KEY=VALUE` fullmatch line — including
  values with `$VAR` / `$(cmd)` the shell would expand. Those are
  PRESENT values for the window variable itself (refused downstream as
  non-integers) but silently divergent for OTHER variables; the helper
  only ever answers for the window variable, so divergence elsewhere is
  out of its contract. Unchanged from #1229; documented, not widened.
