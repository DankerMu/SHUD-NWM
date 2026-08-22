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
  stay byte-identical: the port branch (`:1914-1915`), the local-postgres-token
  branch (`:1916-1928`), and the DSN-token branch (`:1929-1935`) — each range
  includes its own `return True`.
- `_topology_line_mentions_mirror` (6 call sites) and
  `_topology_mentions_database` (3 other callers) keep their current meaning —
  including under D9, which adds vocabulary at the fallback's call site only.
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

Round-1 cross-review added three more, in the vocabulary band D9 covers. Each is
quoted on its own line under a meta comment naming both sibling check ids,
because after D9 these lines become live triggers:

```text
# check_id production-topology-node22-local-postgres / production-topology-node22-db-writer
# "node-22 本地库通过 mirror 实时同步给 node-27，生产查询直接读取该镜像"
```

```text
# check_id production-topology-node22-local-postgres / production-topology-node22-db-writer
# "node-22 hosts the warm standby that mirrors production writes from node-27"
```

```text
# check_id production-topology-node22-local-postgres / production-topology-node22-db-writer
# "node-22's local instance mirrors node-27 and is queried when the primary is busy"
```

Round-2 cross-review added the inflected rollback forms and the standard
replication nouns, same one-quote-per-block discipline:

```text
# check_id production-topology-node22-local-postgres / production-topology-node22-db-writer
# "Operators roll back via the node-22 mirror on demand"
```

```text
# check_id production-topology-node22-local-postgres / production-topology-node22-db-writer
# "State was rolled back from the node-22 mirror last cycle"
```

```text
# check_id production-topology-node22-local-postgres / production-topology-node22-db-writer
# "node-27 ingest 前先从 node-22 mirror 回退"
```

```text
# check_id production-topology-node22-local-postgres / production-topology-node22-db-writer
# "node-22 hosts a read replica that mirrors production writes from node-27"
```

```text
# check_id production-topology-node22-local-postgres / production-topology-node22-db-writer
# "node-22 的从库通过 mirror 对外提供读服务"
```

```text
# check_id production-topology-node22-local-postgres / production-topology-node22-db-writer
# "node-22 的备库通过 mirror 同步给 node-27"
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
skips (defined at `audit_repo_entropy.py:1554`, called from the scan at
`:1397`) — verified by calling the predicate
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
`_topology_context_is_guardrail_or_test_meta` (`:2522` at this change's final HEAD) recognises, among other
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
prose costs far more than it buys, and the construction does not occur on any
surface this check scans. It does occur once in the repository —
`services/orchestrator/retention.py:414` reads "that is not db-free" — but
`services/` is outside `_production_topology_scan_files`'s roots, so that line
never reaches this check. Recorded so that a future reader finds the decision
instead of rediscovering the hole.

### D9 — The fallback carries its own database vocabulary

Round-1 cross-review measured a real recall hole in D1 as first implemented:
after narrowing, the only surviving database signal is
`_topology_mentions_database`, whose token set predates this change and is
narrower than the phrasings real drift uses. Four constructed drift lines were
checked against both revisions; all four are True on master and False at the
first HEAD — the loss is created by this diff, not inherited, so it is fixed
here rather than routed to a follow-up.

The fix adds vocabulary **at the fallback's own call site**, not to
`_topology_mentions_database`. That helper has three other callers and is pinned
must-preserve above; widening it would change checks this change has no mandate
over. The fallback already applies its own call-site rule (DB-absence removal),
so a call-site token list is the established shape here.

Tokens added, applied to the same DB-absence-stripped text the existing leg
measures:

- `本地库` and `本机库` — compound only. A bare `库` is unusable: `仓库`,
  `代码库`, `图库` are ordinary words in this repository.
- `standby` and `instance` — bare is acceptable here. They are reached only
  after the `node-22` and mirror terms have both matched, and a whole-repo run
  with them present produced zero new findings.

Receipt, measured before implementation on the widened predicate: whole-repo
audit stays at **0** topology findings and **753** total, identical to the
un-widened HEAD, while three of the four constructed lines come back. None of
the four false positives this change removes contains any added token, so the
widening cannot resurrect them, and the must-not-flag tests guard that direction
automatically.

The fourth constructed line — a compute node whose written output reaches the
production node through a file mirror, phrased without any database word — is
**deliberately not pinned in either direction**. It is near-verbatim the
legitimate state-index file-mirror phrasing this change exists to stop flagging,
so pinning it either way would encode a debatable adjudication as a test. It is
the boundary this vocabulary approach cannot decide, and belongs beside D8 as a
known limit rather than as an assertion.

### D10 — The vocabulary is bounded by decision, and master is not the baseline

Two consecutive review rounds produced findings in the same class: a constructed
line that the pre-change predicate returned True on and the narrowed one returns
False on. Round 3 would produce more. That is not a sign the token list is a few
words short — it is a sign the class is unbounded and needs a declared boundary,
which is the same mechanism D8 and D9 already use. Evidence that the mechanism
works: the round-2 reviewer explicitly declined to report the compute-instance
phrasing family, because D9 had recorded it.

**Master is not a normative recall baseline.** Its fallback was the bare
`node-22 ∧ mirror` conjunction, so it returned True on every line naming the
compute node that happened to contain the word — precision 0/4 on real
repository content, and, as the verifier demonstrated, True on a line about a
cat photo gallery. A `master=True, head=False` measurement therefore compares
against a detector with no discriminating power. Matching master's recall means
restoring master's four false positives, because they are the same behavior. Any
predicate that discriminates loses to master on some constructible phrasing,
permanently.

So `master flagged it and head does not` is **not** on its own sufficient to
establish a regression in this predicate. A finding must additionally show the
phrasing is one a *discriminating* detector should catch — in practice, that it
falls inside something this fixture commits to. Both round-2 findings did, which
is why both were fixed: D3 commits to rollback wording as a concept, and D9's
recorded limit is explicitly and only the line carrying no database word at all.

The boundary this change ships with:

- **In scope, and pinned by tests**: rollback wording in any inflection of the
  lexeme, plus the database-role nouns enumerated in D9 as amended.
- **Amended in round 3**: `主库` was proposed for the fallback tuple and then
  removed, because it is already a member of `_topology_mentions_database`'s own
  token list. The tuple is only reached after that helper has returned False, so
  the entry could never be the deciding token — dead code wearing the costume of
  a widening, and it made the "every token here was measured free" claim above
  literally untrue for one entry. The behavior is still pinned by a
  must-still-flag test, deliberately at the behavior level rather than the leg
  level, so that removing the token from the shared helper later cannot silently
  drop it. Nine tokens remain. Six of them — `本地库`, `standby`, `instance`,
  `replica`, `从库`, `备库` — are the deciding leg for a pinned must-still-flag
  line. The other three, `本机库`, `secondary` and `生产库`, are not separately
  pinned: they are near-synonyms carried on the same evidence as their pinned
  counterparts, and like every token here they were measured to add zero findings
  repo-wide. Stated plainly so the claim above is not read as stronger than it
  is.
- **Out of scope, known limit**: any other synonym for the same relationship.
  No token set here has demonstrable completeness, and the widenings that landed
  are justified specifically because they were *free* — each was measured to add
  zero findings repo-wide and to resurrect none of the four false positives.
- A future candidate arguing for tokens that **do** change the repo-wide finding
  count is a different proposition and must be adjudicated on its own evidence,
  not folded in as more vocabulary.

Round 3 also closed an **over-match** on the rollback lexeme, which is a
different axis from the synonym question this decision bounds: the pattern had
no left word boundary, so `scrollback` — a realistic CI/terminal term — matched
inside a longer token. That matters because the rollback leg deliberately
bypasses DB-absence stripping (D3), so a "this host has no database" disclaimer
on the same line cannot suppress it. Fixed with a left boundary only; `rollbacks`
must keep matching, so no right boundary. Pinned by a must-not-flag test.

Recorded rather than fixed, from the same round: `_topology_strip_db_absence_claims`
matches the `no database handle` shape but not `no database at all`, so a line
using the latter still reaches the database leg with its token intact. That is a
precision gap in the opposite direction, master had it too, and it is out of
this change's scope.

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
