# Design

## Risk triage

- Fixture level: **compact**. One predicate in a governance script plus its
  tests. No runtime, no DB, no API, no production data path.
- Divergence from #1707's `预估规模: S/M`: none.
- Risk packs selected: **oracle integrity** (governing, D4) and **governance
  gate semantics** (a detector is being narrowed, i.e. recall is deliberately
  traded away and must be shown to be traded only where it was noise).
- Risk packs not selected, with reason: geospatial/CRS, hydro-met windows, SHUD
  numerics, PostGIS/Timescale, Slurm lifecycle, provider snapshots,
  run-manifest provenance, display identity — the diff touches a static text
  scanner and its tests and performs no I/O against any of those surfaces.

## Must-preserve behavior

- The three positive branches of
  `_topology_line_has_node22_local_postgres_or_mirror_drift` above the fallback
  stay byte-identical: the port branch (`:1913-1914`), the local-postgres-token
  branch (`:1915-1927`), and the DSN-token branch (`:1928-1934`).
- `_topology_line_mentions_mirror` (6 call sites) and
  `_topology_mentions_database` (3 other callers) keep their current meaning.
- Every other `check_id` in the hard gate keeps its current finding set.
- The four flagged source files are not edited.

## Seams under test

- `_topology_line_has_node22_local_postgres_or_mirror_drift(line)` — driven
  directly with literal lines, including the four real ones from the incident.
- The whole-repo audit output, as the incident-level before/after.

## Decisions

### D1 — Narrow the trigger; measure the database signal after removing DB-absence claims

The fallback becomes: the existing two-term conjunction, and then either
rollback wording is present, or a database token survives the removal of every
explicit "there is no database here" assertion on that line.

The removal-then-measure shape matters, and a plain boolean exclusion would be
wrong. Consider a single physical line that both claims an active primary
database mirror and, in a later clause about an unrelated subsystem, says
"DB-free" — this repository hard-wraps prose and routinely fuses several clauses
onto one line. A `db_token AND NOT db_free` rule would exempt that line
entirely. Removing only the DB-absence phrases and then asking whether a
database token remains keeps it flagged, because the active-primary token
survives the removal.

`#1707`'s own recommendation — require `_topology_mentions_database` on the line
— is **not sufficient on its own**, and this is measurable rather than
theoretical. That predicate matches a bare `db` word, and the runbook line
`#1707` calls "该 checker 当前误判性质的最短证明" contains `DB-free`. So the
recommendation as written returns True there and leaves the headline example
flagged. Recorded as a deviation; `#1707`'s acceptance criteria are unchanged
and are all met.

Worked through before implementation. Must stop firing (all four are the real
findings, quoted with their file and line):

```text
# check_id under discussion: production-topology-node22-local-postgres
# docs/runbooks/current-production-ops.md:1560 -> db token only inside "DB-free",
#   no rollback wording -> after removal there is no database signal -> not reported
# openspec/specs/production-scheduler-orchestration/spec.md:103 -> no db token at
#   all, "mirrored" is an ordinary verb -> not reported
# scripts/node22_clone_direct_grid_cutover_states.py:25 -> no db token -> not reported
# scripts/node22_clone_direct_grid_cutover_states.py:28 -> no db token -> not reported
```

Must keep firing:

```text
# check_id under discussion: production-topology-node22-local-postgres
# ":55433" on the line -> port branch, untouched by this change
# "node-22 local postgresql" -> local-postgres-token branch, untouched
# "n22_dsn" or "node22-dsn-file" plus mirror -> DSN-token branch, untouched
# bare "node-22 rollback mirror" -> the new rollback leg of the fallback
# "node-22 hosts the active primary postgresql mirror ... that subsystem is DB-free"
#   -> the postgresql token survives DB-absence removal -> still reported
#   (meta line for production-topology-node22-db-writer, whose context window is
#    0 before / 2 after, so its token must sit within two lines of the quote)
```

### D2 — Reject the alternative (grow the exemption list)

`#1707` offers, as a fallback, adding a state-index entry to the mirror branch of
`_topology_local_postgres_context_is_allowed`. Rejected for the reason `#1707`
itself gives: an exemption list is maintained word by word, so the next non-DB
mirror concept collides again, and every added exemption phrase is one more way
for genuine drift to slip past by echoing it. D1 fixes the trigger instead of
apologizing for it afterwards.

### D3 — DB-absence removal never applies to the rollback leg

Rollback wording keeps firing even when the same line also claims DB-freedom. A
line asserting both is self-contradictory, and a governance gate should surface
that rather than swallow it.

### D4 — Oracle integrity is the governing risk

This change **narrows a detector** and takes a permanently-red assertion
(`tests/test_entropy_audit_script.py:214`) green. That is the literal shape of
switching off an inconvenient check. Three things distinguish it:

1. Every eliminated finding is shown to be a false positive on its own text —
   two of them state on the flagged line that the compute node holds no
   database.
2. New must-still-flag tests pin real drift wording, including the bare rollback
   phrase, which the narrowed fallback is the only branch to catch, and the
   fused-clause line from D1 that a naive boolean exclusion would have exempted.
3. A revert receipt: undo the narrowing, and the new must-not-flag tests go red.
   Without that, "the tests pass" proves nothing.

### D5 — The spec finding is adjudicated, not silently swept

`#1707` lists `openspec/specs/production-scheduler-orchestration/spec.md:103` as
out of scope because it belongs to `#1662`, and asks the implementer to rule on
whether it is a true positive. Measured verdict: **false positive, same class**
— `_topology_mentions_database` is False on that line and it carries no drift
token; it is flagged only because "mirrored verbatim" contains the substring the
predicate looks for. D1 therefore removes it too. The file is not edited and
`#1662` is not closed by this change; the receipt is posted to `#1662` with a
recommendation, and the call stays a human one.

### D6 — Baseline restated: 4, not 7

`#1707` recorded 7 findings on the pre-merge `#1697` branch. On master there are
4: archiving that change moved two of the evidence files under
`openspec/changes/archive/`, which `_topology_path_is_archive_or_generated`
skips (`audit_repo_entropy.py:1396`) — verified by calling the predicate
directly, not inferred. The runbook finding also moved from `:1341` to `:1560`.
The target is therefore **4 -> 0**, not 7 -> 1.

### D7 — This change's own fixture must not trip the check it is fixing

Discovered in fixture review, measured rather than argued: with the first draft
of this fixture present, the check reported **28** findings, 24 of them from
this change's own `proposal.md`/`design.md`/`tasks.md`/`spec.md`. OpenSpec
changes are scanned as active surfaces until the post-merge archive commit
moves them (D6), and a document *about* this check unavoidably quotes the
wording the check looks for.

The repository already has the mechanism for this: the meta-context allowlist
`_topology_context_is_guardrail_or_test_meta` (`:2471`) recognises, among other
tokens, the literal check id. Its window is the surrounding non-blank block
(6 lines before, 10 after, stopping at blank lines — `:1673`, `:1681`). So the
rule this fixture follows is: **any block that quotes drift wording also names
the check id inside the same block**. That is why the blocks above are written
as fenced text with a check-id comment on the first line. No new exemption is
introduced and no code is changed for this; it is a writing constraint on this
change's own prose.

The window is also **not the same for every check**. The sibling check
`production-topology-node22-db-writer` builds its context with
`_topology_node22_writer_claim_context` (0 lines before, 2 after,
`audit_repo_entropy.py:1667`), far tighter than the 6/10 block window above. A
quote that satisfies the block rule can still trip the writer check, which is
how this fixture shipped one such finding into the implementer's baseline; the
fix is a meta token within two lines of the quote, not six.

The constraint reaches the change's own slug: the first draft was named after
the compute node and the mirror concept, so every line quoting that slug — task
E6, the archive path, any future reference — became a finding. The slug is
therefore `topology-mirror-fallback-db-signal`, which names neither.

The general fact — that authoring a governance change about this check requires
knowing all of this — is a real usability edge in the gate, and is recorded as a
known limit rather than fixed here.

Verified after rewriting: with this fixture present the check reports exactly
the four real findings and nothing from this change's own directory.

### D8 — Known limit of removal-then-measure: double negation

Surfaced in fixture review. Removal is literal, so a doubly-negated construction
whose only database token is textually inside the removed phrase produces a
false negative — e.g. a line saying a service is *not* "postgresql-free" and
then naming a mirror. Stripping the literal phrase takes the only database token
with it.

Not handled, deliberately. Detecting negations of negations in mixed-language
prose costs far more than it buys, and the construction does not occur in this
repository's writing — its Chinese technical prose is declarative rather than
double-negated around an English suffix. Recorded so that a future reader finds
the decision instead of rediscovering the hole.

## Evidence mapping

| Acceptance criterion (#1707) | Evidence |
|---|---|
| the state-index false positives are gone from the hard-gate output | before/after whole-repo audit, cwd stated in the command |
| the four evidence files are unedited | `git diff --stat origin/master...HEAD` |
| tests pin both directions | must-not-flag (four real lines verbatim) and must-still-flag cases |
| `uv run pytest -q tests/test_entropy_audit_script.py` recorded | full transcript |
| `uv run ruff check .` | clean |

## Non-goals

- Any other `check_id` in the hard gate.
- The three positive branches above the fallback.
- Editing any of the four flagged files, or `#1697`'s text and flag names.
- Closing `#1662`.
- Fixing the authoring edge described in D7.
