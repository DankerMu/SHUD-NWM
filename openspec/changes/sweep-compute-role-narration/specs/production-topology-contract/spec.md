## ADDED Requirements

### Requirement: Live text names the compute role and keeps the codename for names

Live documentation, capability specs, agent instructions, env templates, unit files and the
human-read text of code SHALL say where compute work happens with the terms that
`openspec/glossary.md` defines: the compute role, the compute control host, the compute hosts, the
compute node, or a host name (`w3`, `w2`) where the particular host matters. The codename (`node22`,
`node-22`, also written `node 22`) and its shorthand (`22 节点`, `22-node`, `22 compute-control`)
SHALL NOT stand for the place where compute work happens. The codename SHALL remain only:

- as part of a name;
- as a persisted or compared value, and in the comment text of an applied migration;
- in a sentence that defines the codename;
- in a statement about the retired host `210.77.77.22`, and in a dated observation of that host;
- in the topology gate's match vocabulary and in a test's own input;
- in history records.

#### Scenario: The glossary is the source of the terms

- **WHEN** `openspec/glossary.md` is read
- **THEN** it defines the compute role, the compute control host, the compute hosts, the compute
  node, the host names `w3` and `w2`, the codename, and the retired node-22 host
- **AND** it states that the runtime service role `compute_control` is an identifier and is not the
  compute role

#### Scenario: Live text does not use the codename as a place

- **WHEN** a live runbook, a capability spec, an agent instruction file, an env template, a unit
  file, or a message, help text, docstring or comment in code says who does compute work or where it
  runs
- **THEN** it uses a glossary term
- **AND** a line where the codename or its shorthand stands alone belongs to one of the classes the
  requirement lists

#### Scenario: Names keep the codename

- **WHEN** a file, script, unit, module, function, env key, schema id, capability name, CLI flag,
  marker, check id or path contains the codename
- **THEN** it keeps its name
- **AND** running prose that mentions it writes the name in code font

#### Scenario: A requirement title is not a name

- **WHEN** a requirement title uses the codename for the compute role or for something the role owns
- **THEN** the title uses a glossary term instead
- **AND** a title about the retired host or its database keeps the codename

#### Scenario: The production-ops pages carry no command addressed to the retired host

- **WHEN** `docs/runbooks/current-production-ops.md` or a page under
  `docs/runbooks/production-ops/` gives a command
- **THEN** no command is a literal `ssh` to `210.77.77.22`
- **AND** a step that runs on the compute control host says so, and the page links the section of
  `docs/runbooks/w2-w3-compute-topology.md` that says how the compute hosts are reached

#### Scenario: History is not rewritten

- **WHEN** an archived change, a receipt, a plan, an ADR, a review log or `progress.md` uses the
  codename
- **THEN** its text is left as written
