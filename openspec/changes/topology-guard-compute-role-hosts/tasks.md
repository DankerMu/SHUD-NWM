# Tasks: topology guard recognises the compute hosts (#3358)

Fixture level: standard. Risk packs: governance guard (a guard that looks green but pins nothing; a guard that turns true statements red). No production runtime code.

Notation. This file is inside the guard's scan set, so example sentences use `<h>` for a host name. The host names are the two lower-case tokens listed in the spec delta's requirement text. `<pair>` is both of them joined by a slash, with or without spaces around it.

## Design decisions (orchestrator)

- D1. One source for the host names: one function that rewrites host-name tokens in a raw line to the codename (`node-22`), before any lower-casing. A run of host names joined only by `/`, `,`, `、`, `and`, `or`, `和` and spaces is rewritten to one codename, so `<pair>` behaves like a single name. The codename is emitted with a space on each side: the existing codename regex uses `\b`, which does not see a boundary between a CJK character and a letter, and the existing normalisation collapses the extra spaces. The database-writer family runs on the rewritten lines: the claim start, the standalone anchor, the drift clauses, the claim context window and every allowance rule. No host literal inside the existing predicates and phrase tables is edited.
- D2. Token rule, on the raw text, case-sensitive: a host name in lower case, not preceded or followed by an ASCII letter, digit or underscore, not preceded by a dot, and not followed by a dot or a hyphen plus an ASCII letter or digit. A host name at the end of a sentence, in parentheses, in backticks, after `nwm-`, or next to a CJK character is a host name. The upper-case spelling, `www.<h>.org`, `<h>.internal`, `host.<h>`, `<h>c`, `<h>_dir` and `show30s` are not, and neither is the first part of a hyphenated longer name (`<h>-scheduler-journal`, the runbook file name that starts with both host names): such names are identifiers, and rewriting them would also cut the line into clauses at the hyphens.
- D3. A finding keeps the path and line number of the original line. The claim de-duplication key is built from the original lines, so a codename line and a host-name line with otherwise equal text stay two findings.
- D4. Compute-host local PostgreSQL: a new leg of `production-topology-node22-local-postgres`, evaluated for every line independently of the retired-host leg (the retired-host leg's early `continue` must not skip it), with at most one finding per check id per line. The existing relation-clause split lower-cases its input, so host tokens are marked on the raw line first (the D1 function with a distinct lower-case marker in place of the codename), and the marked line is split. A clause that has the marker and one of the existing local-PostgreSQL tokens is a finding, unless that clause (marker replaced by the codename) carries the existing negative writer boundary, or one of the absence forms `no local postgres|postgresql|pg`, `without (a) local postgres|postgresql|pg`, `never` followed by one of `connect|connects|use|uses|read|reads|query|queries|had|has|have|run|runs|host|hosts`, `从不`, `没有本地`, `没有本机`, `无本地`, `无本机`, `不存在`. Negation in another clause of the line does not count. The context-window allowances of the retired-host leg (historical / archived / stopped / rollback / structured boundary) are not consulted.
- D5. Slurm accounting: a database-writer drift clause that contains `slurmdbd`, `slurm accounting`, `mariadb` or `mysql`, and none of `postgres`, `postgresql`, `pg` (as a word), `primary`, `主库`, `nhms`, `55432`, `55433`, `active`, `current`, `当前`, `production`, is not a drift clause. The test sits where drift clauses are selected, so detection and allowance agree. It applies to every name of the role; this is the one deliberate change for text that names only the codename (such a clause was a finding and is not one now).
- D6. Not extended: the mirror legs, the port leg and the `DATABASE_URL` scan leg keep reading the original lines.
- D7. Finding title, description and recommendation of the two checks name the role as "compute role (node-22 / w3 / w2)". Check ids, function names and module names keep `node22`.

## 1. Code

- [x] 1.1 `topology_predicates.py`: the host-name function of D1/D2 (one regex, one function), and the Slurm-accounting exclusion of D5 inside the drift-clause selection.
- [x] 1.2 `check_topology.py`: run the database-writer family on the rewritten lines (D1) with the D3 key; add the compute-host local-PostgreSQL leg (D4); update the finding texts (D7). The retired-host legs keep reading the loop's original line.
- [x] 1.3 `topology_context_rules.py`: only what D4 needs, if anything. No phrase table gains a host-name variant.
- [x] 1.4 Module docstrings say that the family covers the compute role by codename and by host name.

## 2. Tests

In `tests/test_entropy_audit_topology_db_boundary.py`, using its helpers that write lines into a scanned runbook and run the check. Each row is run for both host names unless it says otherwise.

- [x] 2.1 Flagged as `production-topology-node22-db-writer`:
  - `Current NHMS production says <h> is the active database writer.`
  - `Current NHMS production says <h> hosts the active primary PostgreSQL.`
  - `当前生产 <h> 是主库，写入数据库。`
  - `当前生产<h>是主库，写入数据库。` (CJK neighbours, no spaces)
  - `nwm-<h> is the active database writer.`
  - `<pair> own the active primary database.`
  - `slurmdbd on <h> is the active primary PostgreSQL writer.` (D5 does not launder)
  - `<h> runs slurmdbd and is the active database writer.` (D5 does not launder)
  - Two lines in one file, equal except that one uses the codename and the other `<h>`: two findings, on their own line numbers (D3).
- [x] 2.2 Flagged as `production-topology-node22-local-postgres`:
  - `Operators should connect to <h> local PostgreSQL for active DB checks.`
  - `<h> local PostgreSQL is historical, do-not-connect for current NHMS production state, archived, and stopped rollback-only.` (the retired-host boundary wording does not apply)
  - `使用 <h> 本地 PostgreSQL 做检查。`
  - `Connect to <h> local PostgreSQL for current checks; do not use node-27.` (negation in another clause)
  - `<h> local PostgreSQL never goes down.` and `<h> 本地 PostgreSQL 没有密码。` (a bare negative word is not an absence form)
  - One line that carries the complete retired-host boundary wording for the codename's own PostgreSQL and, after a semicolon, `connect to <h> local PostgreSQL`: exactly one finding of this check id.
- [x] 2.3 Not flagged:
  - The allowed sentences of the database-writer check, with the codename replaced by `<h>` and by `<pair>`. Sources: the `_write` body of `test_entropy_audit_topology_guardrails_allow_explicit_negative_node22_db_access` and the parameter list of `test_entropy_audit_topology_guardrails_allow_coordinated_negative_node22_db_access`. Lift the sentences into module constants so both the existing tests and the new ones read them; the existing assertions do not change. Replace with the pattern `node[-_ ]?22`. Do not include sentences about the retired host's own PostgreSQL or mirror: D4 reports their host-name versions by design.
  - `without database mutation or <pair> access.`
  - `Reviewer W2: the writer test misses the database mutation path.` (literal upper case; once)
  - `Reviewer W2: the local PostgreSQL fixture is missing.` (literal upper case; once; pins the D4 leg)
  - `See https://www.<h>.org/TR/ for the database writer vocabulary.`
  - `The <h>c validator is the active database writer of that report.`
  - `show30s is the active database writer flag.` (once)
  - `The <h>-scheduler-journal mirror is not the active database writer.` and a sentence that names the runbook file `w2-w3-compute-topology.md` next to `describes the active DB writer` (hyphenated longer names)
  - `slurmdbd on <h> writes Slurm accounting to its local database.`
  - `<h> hosts slurmdbd with a local MariaDB database for Slurm accounting.`
  - `<pair> run no local PostgreSQL.`
  - `Do not connect to a <h> local PostgreSQL; there is none.`
  - `<h> 没有本地 PostgreSQL。`
  - `<pair> never connect to node-27 local PostgreSQL.`
- [x] 2.4 Unit rows for the host-name function: every example of D2 in both directions, `<pair>` in its three spellings (one codename out), a line with none (returned equal), start and end of line, backticks, `<h>:`.
- [x] 2.5 The existing tests of the five files in the issue's Verification stay green with no assertion edited. If one must change, stop and report.

## 3. Evidence (`.workplans/3358/`)

- [x] 3.1 Red first: write 2.1-2.4, run against the unmodified source, save to `red-runs.log` (which cases fail). No-flag rows that already pass before the change are listed as such.
- [x] 3.2 `head-scan.log`: `uv run --no-sync python scripts/governance/audit_repo_entropy.py --mode hard-gate --format json` on the tree with the change, this change directory included: the total (247 on master) and the number of `production-topology-*` findings (0) are unchanged. List every scanned line that the host-name function rewrites at HEAD (path and line number), so a reviewer can see what the guard now reads as a host. The orchestrator repeats the scan after `openspec archive`.
- [x] 3.3 Mutations, each applied by hand, checked, recorded, restored (verify with `cmp`):
  - M1: host-name regex made case-insensitive -> both `Reviewer W2` rows red. M1b: the D4 leg tests the host token after the clause split instead of marking the raw line -> the second `Reviewer W2` row red.
  - M2: drop the leading-dot rule -> the `host.<h>` unit row red; drop the trailing-dot rule -> the `<h>.internal` unit row red. (The domain row is covered by both rules and needs both gone.) M2c: drop the hyphen-suffix rule -> the runbook-file-name row of 2.3 red. M2b: emit the codename without the surrounding spaces -> the CJK-neighbour row of 2.1 red.
  - M3: recognise the host names inside `_topology_mentions_node22` instead of rewriting -> host-name versions of the allowed sentences red.
  - M4: remove the Slurm-accounting exclusion -> the slurmdbd no-flag row red; make it ignore the PostgreSQL/primary/active tokens -> the two slurmdbd flag rows of 2.1 red.
  - M5: let the compute-host local-PostgreSQL leg consult the retired-host allowance, fed with the rewritten line -> the boundary-wording row of 2.2 red.
  - M6: look for negation in the whole line instead of the clause -> the `do not use node-27` row of 2.2 red.
  - M7: put the new leg after the retired-host leg's `continue` -> the one-finding row of 2.2 red.
  - M8: rewrite each host of a pair separately -> the `<pair> access` row of 2.3 red.
- [x] 3.4 Local: the five test files of the issue's Verification, `uv run --no-sync ruff check --no-cache .`, `openspec validate topology-guard-compute-role-hosts --strict --no-interactive`.
- [ ] 3.5 node-27: the same five test files, zero unexpected skips.

## Must preserve

- Every existing finding and non-finding for text that names only the codename, D5 aside: the existing tests are the oracle and their assertions are not edited.
- Check ids, the scan set, the skip rules for archive and receipt paths.
- The hard gate on the current tree: no new finding.
- `_topology_mentions_database`, `_topology_mentions_writer` and the phrase tables are not edited.

## Known limits (stated in the PR)

- A host named only by address, or in upper case, or as the first part of a hyphenated word (`<h>-only`), is not recognised.
- The mirror, port and `DATABASE_URL` legs do not know the host names.
- Clause splitting is unchanged: a sentence that names node-27 as the DB writer and, after `and`, says what a compute host runs is a finding, as its codename form is today. Write such sentences with a semicolon.
- The D4 absence forms are a short list. A true negative statement in other words is reported; reword it with one of the forms.
- A clause that names a compute host and another host's local PostgreSQL without negation is reported by the new leg. That is intended: compute hosts connect to no live database.
