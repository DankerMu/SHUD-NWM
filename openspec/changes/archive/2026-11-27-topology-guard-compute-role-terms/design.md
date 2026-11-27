# Design

Change surface: `_topology_rewrite_compute_hosts` and the pattern it uses
(`scripts/governance/entropy_audit/topology_predicates.py`); the leg #3358 added for the two hosts,
`_topology_line_has_compute_host_local_postgres_drift`; the two places in `check_topology.py` that
turn a line into a writer or local-database finding, only for the scenario-title rule (D5).

Must preserve: every existing test of the two topology test files, unedited, with one exception:
`test_topology_rewrite_compute_hosts_leaves_lines_without_a_host_unchanged` has the input "no
compute host is named here", whose premise this change reverses. That input is replaced by one that
names no host and no term, and the old sentence becomes a positive rewrite case. The PR records it
as a deviation. Host-name rules of #3358 (lower case only, token boundaries, runs, the `nwm-`
alias). The strictness of the leg #3358 added (only prohibitions and absence forms excuse the
claim). The mirror, port and `DATABASE_URL` legs read original lines. A finding keeps the path and
line of the original line; the de-duplication key is built from original lines.

Must add/change:

- D1. One pattern for every name of the role that is not the codename: the host names and the role
  terms. The rewrite turns each name, or run of names, into the codename before the writer
  predicates run, and into the marker for the leg #3358 added. No predicate gains a per-term
  variant.
- D2. English terms: `compute`, one space or one hyphen, then `role` | `control host(s)` |
  `host(s)` | `node(s)` (inside "control host" also one space or one hyphen), any letter case, with
  the boundaries of the host names: not next to an ASCII letter, digit or underscore, not after a
  dot or hyphen, not before a dot or hyphen plus a letter or digit. So `non-compute host`,
  `compute_control` and `compute-role-x` are not names. `/` is a separator of names (D3), as it
  is for the host names, so a path segment that is exactly a term is read as the term: the gate
  errs toward a finding, and no such path exists in the repository today. Chinese terms are matched as
  written (Chinese has no token boundary; a longer word that starts with one, such as "计算节点数",
  matches). A term is matched inside one physical line: the gate reads lines, and a term broken
  across a line end is not a name. The umbrella change gains the rule that rewritten prose does not
  break a term across a line end. A hyphenated compound of a term (`<r>-local`) is not a name in
  any leg, as `<h>-local` is not, while the compound of the codename is one: the umbrella change
  gains the rule that such a compound is rewritten to an open phrase.
- D3. Runs are the runs of #3358, over all names: names joined only by spaces, `/`, `、`, `和`,
  `and`, `or` are one name ("`<r>` `<h>`"). Brackets and `+` do not join: "`<r>` (`<h>` / `<h>`)"
  is rewritten to two names, which changes no verdict of a claim line.
- D4. Table rows. Measured on master with the vocabulary added: the node-27 row of the hosts table
  in the instructions (one cell says that node-27 reads the artifacts of `<r>`, the next cell names
  node-27's own database) becomes a finding of that leg, three times (the source and the two
  generated roots). The row is about node-27. Rule, for the leg #3358 added: a line whose first
  non-blank character is `|` is a table row; when its first cell (asterisks and code font
  aside) is exactly `node-27`, the leg evaluates each cell on its own; a cell ends at a `|` that
  is not escaped. Any other line is read as before, also a row whose first cell is a longer name
  that contains `node-27` (a file name) or names the role as well. The rule holds for host
  names too, which changes #3358 for exactly this shape of row.
- D5. The gate's own contract. With the vocabulary added, three lines of
  `production-topology-contract` ("Static guardrails block topology drift") that describe the
  checks become findings: one sentence of the requirement body and two scenario titles.
  - The body sentence is rewritten in this change's delta so that no line of it carries a name of
    the role together with writer and database words.
  - A MODIFIED block cannot retitle a scenario (the validator refuses to drop one), so the two
    titles stay. Rule: a line whose text starts with `#### Scenario:` and contains "is flagged"
    or "are flagged" (any letter case) after it is a statement about the gate. A title that says
    something "is not flagged" is an ordinary line. It is decided on the shape
    of the line alone, in any scanned file. It applies to the writer check, for every name of the
    role including the codename, and to the leg #3358 added. It excuses no other line. (The
    guard-meta phrases of the context rules read a claim's window as before: a title whose words
    are guard meta can still cover an unterminated claim on the line above it, under any name.
    Unchanged by this change.) No phrase is added to the guard-meta vocabulary, and that leg gets
    no other allowance. For a title of this shape under the codename or a host name that no
    guard-meta phrase covered, the verdict changes from a finding to none: the PR records it.
- D6. One line of `openspec/changes/sweep-compute-role-narration/design.md` (§2, the sentence that
  describes the leg #3358 added) used the words that leg matches. It is already reworded in this
  branch.

Governing invariant: a sentence on one line means the same to the gate whichever name of the
compute role it uses.

Sibling surfaces: the slurm-accounting exclusion (applies to every name: positive and negative
rows with a term); the negative-boundary lists of the writer family (they read the rewritten line:
the allowed negative sentences of the codename are allowed with a term); the standalone anchor rule
(`_topology_line_is_standalone_node22_anchor`: a line that is only a term behaves as a line that is
only the codename); `topology_context_rules.py` phrase tables (unchanged; they see the codename);
the de-duplication key; the mirror and port legs (they do not read the terms; one row);
the `DATABASE_URL` scan leg (unchanged: it anchors on the compute runtime's paths and env file and
on two narration phrases, "node-22 compute" and "计算控制面"; it is not a leg of the retired host.
Its narration anchors go stale as the sweep rewrites those phrases. That is reported, not changed
here); finding texts (unchanged here; #3422 rewrites them).

Seams under test: `_check_production_topology_drift` through the helpers of
`tests/test_entropy_audit_topology_db_boundary.py` that write lines into a scanned file (the
highest seam: pattern, rewrite, legs and context rules together); `_topology_rewrite_compute_hosts`
directly for the pattern, boundaries and runs, as the #3358 tests do; the real-repository hard gate
(`tests/test_entropy_audit_report_contract.py` and the JSON report).

Required evidence: the rows of `tasks.md` §2, each for every term unless it says otherwise; the
mutations of 2.7; the real-repository evidence of §3.

Non-goals: see the proposal.

Review focus:

1. A term cannot be laundered: no wording that is a finding with the codename passes with a term.
2. Token boundaries: `compute_control` and other identifiers are not names; a path segment that
   is exactly a term is one.
3. D4 and D5 do not open a hole: a row whose first cell is the role, or is neither node-27 nor the
   role, still yields its finding; a claim next to a scenario title still yields its finding.
4. The new spec text is true of the code, scenario by scenario.
