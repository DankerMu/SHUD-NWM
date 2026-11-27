# Topology guard: recognise the compute role by its role terms (#3403)

```text
Issue type: feature
Fixture level: expanded
Upstream suggested level: expanded (agree)
Blast radius: a hard gate that runs on every PR; a wrong pattern turns true statements red repository-wide, or lets a writer claim through
Selected risk packs: Legacy compatibility / examples; Documentation / migration notes
Evidence floor: the two topology test files, the real-repository hard gate before and after, node-27 run of the same files, CI
```

## Why

The narration sweep (#3345, `openspec/changes/sweep-compute-role-narration/`) rewrites sentences that
say "node-22" into sentences that name the role. The Production Topology Hard Gate recognises the
role by its codename and by the two host names (#3358). It does not recognise the role terms, so a
rewritten sentence leaves the gate on the day it lands. This slice comes first for that reason.

This file, `design.md` and `tasks.md` are inside the gate's scan set. Example sentences use `<r>`
for a role term and `<h>` for a host name.

## What Changes

- The two checks that read the host names today read the role terms as well (the writer check, and
  the leg of the local-database check that #3358 added for the two hosts). The terms: "compute
  role", "compute control host", "compute host", "compute node" (singular and plural, a space or a
  hyphen between the words, any letter case), and 计算角色, 计算控制主机, 计算主机, 计算节点.
- An English term is a whole token: not inside a longer identifier, a dotted name or a
  hyphenated longer name (`compute_control`, `compute-role-x`, `compute_node_count`). A path
  segment that is exactly a term is read as the term, as one that is exactly a host name is.
- A term is matched inside one line. A term and host names joined only by the separators that join
  host names today are one name.
- A table row about node-27 is not a claim about the role: in a row whose first cell names node-27,
  the leg #3358 added reads each cell on its own. This holds for host names as well.
- The contract's own sentences about the checks are worded so the gate reads them as statements
  about the gate, and a scenario title of a spec that says what is flagged is not a claim.
- `production-topology-contract`: requirement titles leave the list of identifiers that keep the
  codename (the sweep renames 14 of them later).

## Not in this change

- The mirror legs and the port leg belong to the retired host and keep reading the original lines.
  The `DATABASE_URL` scan leg is not changed either: it anchors on the compute runtime's paths and
  on two narration phrases, and its narration anchors are reported as a follow-up (`design.md`).
- Rewording any document to make a finding go away, except the gate's own contract text, which this
  change rewrites anyway, and one line of the umbrella design that describes the gate.
- Finding ids, function names, module names.
- The glossary and the instructions (#3404).

## Impact

- Code: `scripts/governance/entropy_audit/topology_predicates.py`; `check_topology.py` for the
  scenario-title rule.
- Tests: `tests/test_entropy_audit_topology_db_boundary.py`.
- Spec: `production-topology-contract`, two requirements.
- No production runtime code.
