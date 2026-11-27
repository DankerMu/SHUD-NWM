# Design

Change surface: `_topology_rewrite_compute_hosts` and the pattern it uses
(`scripts/governance/entropy_audit/topology_predicates.py`); the compute-host leg
`_topology_line_has_compute_host_local_postgres_drift`. Callers in `check_topology.py` are
unchanged unless D4 needs the line's table structure.

Must preserve: every existing test of the two topology test files, unedited. Host-name rules of
#3358 (lower case only, token boundaries, runs, the `nwm-` alias). The strictness of the leg #3358
added for the two hosts (only prohibitions and absence forms excuse the claim). The mirror, port
and `DATABASE_URL` legs read original lines. A finding keeps the path and line of the original
line; the de-duplication key is built from original lines.

Must add/change:

- D1. One pattern for every name of the role that is not the codename: the host names and the role
  terms. The rewrite turns each name, or run of names, into the codename before the database-writer
  predicates run, and into the marker for the compute-host leg. No predicate gains a per-term
  variant.
- D2. English terms: `compute` + space or hyphen + `role` | `control host(s)` | `host(s)` |
  `node(s)`, any letter case (a sentence may start with one), with the boundaries of the host names:
  not next to an ASCII letter, digit or underscore, not after a dot or hyphen, not before a dot or
  hyphen plus a letter or digit. Chinese terms are matched as written; Chinese has no token
  boundary.
- D3. A run joins names by the separators of #3358 and also by brackets and `+`, so "`<r>`
  (`<h>` / `<h>`)" and "`<r>` `<h>`（控制）+ `<h>`" are one subject. Words between two names other
  than these separators end the run, except the short gloss "（控制）" / "(control)" after a host
  name.
- D4. Table rows. Measured on master with the vocabulary added: the node-27 row of the hosts table
  in the instructions (one cell says that node-27 reads the artifacts of `<r>`, the next cell names
  node-27's own database) becomes a finding of that leg, three times (the source and the two
  generated roots). The row is about node-27. Rule: in a table row whose first cell names node-27, a name of the compute role in one
  cell and a local PostgreSQL in a different cell are not one claim. A row whose first cell is a
  name of the compute role stays a claim, and so does a term and a local PostgreSQL in the same
  cell of any row.
- D5. The gate's own contract. With the vocabulary added, three lines of
  `production-topology-contract` ("Static guardrails block topology drift") that describe the
  checks become findings: one sentence of the requirement body and two scenario titles.
  - The body sentence is rewritten in this change's delta with the wording the gate already reads
    as guard meta ("the guard reports").
  - A MODIFIED block cannot retitle a scenario (the validator refuses to drop one), so the two
    titles stay. Rule: a line that is a scenario title of a spec (`#### Scenario:` at the start of
    the line) and says that something is "flagged" is a statement about the gate, for both checks.
    The same words without the title prefix stay a finding. No phrase is added to the guard-meta
    vocabulary, and the leg #3358 added gets no other allowance.
- D6. One line of `openspec/changes/sweep-compute-role-narration/design.md` (§2) describes the
  compute-host leg in the words the leg matches. It is reworded here (orchestrator, documentation).

Governing invariant: a sentence means the same to the gate whichever name of the compute role it
uses.

Sibling surfaces: the slurm-accounting exclusion (applies to every name: one positive, one
negative row with a term); the negative-boundary lists of the database-writer family (read the
rewritten line: denials with a term are allowed exactly as with the codename); the standalone
anchor rule (`_topology_line_is_standalone_node22_anchor`: a line that is only a term, e.g. a table
cell or a heading "`<r>`:" followed by writer text on the next lines, behaves as the codename
does); `topology_context_rules.py` phrase tables (unchanged; they see the codename); the
de-duplication key; finding texts (unchanged here; #3422 rewrites them).

Seams under test: `_check_production_topology_drift` through the helpers of
`tests/test_entropy_audit_topology_db_boundary.py` that write lines into a scanned file (the
highest seam: pattern, rewrite, legs and context rules together); the real-repository hard gate
(`tests/test_entropy_audit_report_contract.py` and the JSON report).

Required evidence: the rows of `tasks.md` §2, each for every term unless it says otherwise; one
mutation (a term removed from the pattern turns its rows red); the real-repository finding multiset
before and after, with the seven measured differences resolved as D4, D5, D6 say and any other
difference listed and judged.

Non-goals: see the proposal.

Review focus:

1. A term cannot be laundered: no wording that is a finding with the codename passes with a term.
2. Token boundaries: `compute_control`, paths and identifiers are not subjects.
3. D4 and D5 do not open a hole: a row whose first cell is the role still yields its finding when
   another cell names a local database, and the scenario-title rule needs the title prefix.
4. The new spec text is true of the code, scenario by scenario.
