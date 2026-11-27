# Tasks: compute-role narration sweep

A group that is not atomic lands as several PRs, first slice first (the last line of each group says
which). Every PR carries in its body the counter output of `design.md` §3 before and after
(`uv run --no-sync python -I openspec/changes/sweep-compute-role-narration/narration_count.py`, and
`--tokens` for the name tokens) and the hard-gate finding counts before and after. Every PR ticks
its tasks here in its final push.

## Must preserve (all groups)

- The hard gate passes on the real repository with the same finding multiset, or each difference is
  listed and judged in the PR.
- Every test that reads a rewritten page by structure stays green; a test that pins a rewritten
  sentence follows it in the same PR with a red run on the old sentence.
- A `name` token count of the counter moves only for a deleted file or a removed pointer to one, and
  the PR lists it.
- Commands that remain in a live runbook are the ones the page had, minus the literal `ssh` to the
  retired host; `uv` discipline lines for the compute hosts stay as they are.
- History, persisted values and the gate's existing vocabulary are not edited (`design.md` §3).

## 1. Gate vocabulary

- [ ] 1.1 The role and host-set terms of `design.md` §2 are a subject for both legs of the topology
  family (DB writer, local PostgreSQL), with the token boundaries of the host names.
- [ ] 1.2 Synthetic cases: each term × each leg × asserted / denied; boundaries (`compute_control`, a
  path, an identifier, upper case); mixed runs ("the compute role (w3 / w2)").
- [ ] 1.3 Real repository: finding multiset before and after; every new or lost finding listed and
  judged; mutation: one term removed, its case goes red.
- [ ] 1.4 Spec delta in the slice's own change: `production-topology-contract`, "Static guardrails
  block topology drift" (the gate's subjects).

Suggested fixture level: expanded - changes what a hard gate that runs on every PR matches.
Minimal mergeable slice: atomic - a term without its synthetic cases and the real-repository comparison cannot be judged; they are one verification path.

## 2. Terms

- [ ] 2.1 `openspec/glossary.md`: the seven entries of `design.md` §1, the retired spellings, and the
  difference from the runtime role `compute_control`.
- [ ] 2.2 `instructions/agents/shared.md` uses the terms consistently; `CLAUDE.md` / `AGENTS.md`
  regenerated (byte-exact test); the retired-host line keeps the terms its test requires.
- [ ] 2.3 The scoped `AGENTS.md` files that narrate where compute runs (`appendix-inventory.md` §8,
  slice 2).

Suggested fixture level: none - documentation and generated instruction files; no runtime behaviour.
Minimal mergeable slice: atomic - the glossary entry and the instruction lines that use it are one statement of the vocabulary; landing one without the other leaves the two sources disagreeing, which is what the group removes.

## 3. Production-ops pages

Each task rewrites the bodies with the glossary, removes the page's literal `ssh` lines
(`design.md` §3, commands) and its banner pointer, and keeps dated observations.

- [ ] 3.1 `operating-scope.md`, `frontier-stall-alert.md`, `parse-failure-residency-alert.md` (no
  `ssh` line, no pointer, no pinned sentence).
- [ ] 3.2 `artifacts-and-storage.md`, `recalibration-and-archive.md`.
- [ ] 3.3 `service-bringup.md`, `known-issues-state-index.md` (structure readers named in
  `appendix-inventory.md` §2b stay green).
- [ ] 3.4 `stuck-detection.md`, `known-issues-pipeline.md`: the heading §8.5 with its five inbound
  anchors.
- [ ] 3.5 `gateway-and-services.md`, `file-provider-refresh.md`, `overview.md`: the two pins of
  `tests/test_node22_entrypoint_invariant_python_scan.py` follow their sentences.
- [ ] 3.6 `current-production-ops.md` and its banner; a static test that the production-ops pages and
  this page hold no `ssh` command addressed to the retired host (red run: one line restored); spec
  deltas in the slice's own change: `production-ops-readiness` (the banner scenario goes),
  `production-topology-contract` (the banner scenario no longer names this page). The test backs the
  scenario of this change's requirement.

Suggested fixture level: none - documentation; 3.6 adds one static test over documents and a spec delta, still no runtime behaviour.
Minimal mergeable slice: 3.1 - three pages no test pins and no other page links by anchor; 3.2 to 3.5 are independent of it and of each other; 3.6 depends on 3.2 to 3.5, because its test is green only when the last `ssh` line is gone.

## 4. Other live docs

- [ ] 4.1 `docs/governance/ROLE_BOUNDARY.md` and `openspec/project-profile.md`, with their three
  pinned sentences and the required-term set in `tests/test_role_boundary_static.py`.
- [ ] 4.2 `docs/spec/*`, `docs/modules/*`, the two forcing notes under `docs/`,
  `scripts/diagnostic/qhh/README.md` (its pin follows).
- [ ] 4.3 `infra/env/README.md`: the body, the authority heading with its two tests and three inbound
  anchors; the comments of `infra/env/*.example`.
- [ ] 4.4 Live runbooks without a banner: `failed-basin-retry.md`,
  `scheduler-dbfree-typed-reasons.md`, `slurm-gateway-preflight.md`,
  `canonical-precip-mirror-backfill.md`, `held-forcing-absence-exit.md`, `ci-test-routing.md`,
  `node-27-bringup-checklist.md`, `tier-node27-timeseries-storage.md`,
  `display-readonly-live-mvt.md`, `forcing-copyback-backfill.md` (header corrected: the lane is not
  current practice, the CLI and its spec are live).
- [ ] 4.5 The two remaining banner pages, `node22-control-plane-manual-recovery.md` and
  `two-node-deployment-overview.md` (the bare "22" shorthand; the heading and sentence pinned by
  `tests/test_node22_runbook_section_markers.py` and the sentence pinned by
  `tests/test_role_boundary_static.py`); spec delta in the slice's own change:
  `production-topology-contract` loses the banner scenario.

Suggested fixture level: none - documentation and template comments; pinned sentences are followed, no runtime behaviour.
Minimal mergeable slice: 4.2 - design notes and one README with a single pin, no anchor and no banner; 4.1, 4.3 and 4.4 are independent of it and of each other; 4.5 depends on 3.6, which narrows the same scenario first.

## 5. Historical runbooks with shared followers

- [ ] 5.1 Delete the QHH MVP family: `qhh-continuous.md`, `qhh-mvp-smoke-evidence.md`,
  `qhh-mvp-production-like-e2e-checklist.md`, `qhh-controlled-failure-retry-evidence.md`; the two
  section-marker entries of `tests/test_production_scheduler.py`; the requirement of
  `evidence-boundary-hardening` that demands the checklist (the slice's own change); the rows of the
  two governance inventories.
- [ ] 5.2 Delete `two-node-production-e2e-plan.md` and `node22-db-retirement-runbook.md`; successor
  for the 0600-then-source-then-validator snippet in `infra/env/README.md`, read by
  `tests/test_readonly_db_validation_routes.py` (red run with the order swapped); the scenario of
  `evidence-boundary-hardening` that names the plan (the slice's own change); the two tests whose
  subject is the retirement runbook; the three pointer sentences retargeted to
  `infra/env/display.example`, `ROLE_BOUNDARY.md` and the node-27 checklist; inventory rows.

Suggested fixture level: compact - deletions whose tests are re-homed, not dropped; no runtime behaviour.
Minimal mergeable slice: 5.1 - the family references only itself, two test entries and one requirement; 5.2 depends on it because both edit `evidence-boundary-hardening` and the two inventories.

## 6. Historical runbooks with a residue

- [ ] 6.1 `qhh-backend-smoke.md`: the local-lane lines move to `scripts/diagnostic/qhh/README.md`; the
  second recipe pin, the governance schema's freshness anchor and `apps/api/AGENTS.md` follow; delete.
- [ ] 6.2 `qhh-22-business-bringup.md`: script header comments and the authority-invariant test (a
  header names no deleted runbook); the three citations in the forcing note reworded; the pointers in
  the production-ops pages removed; delete.

Suggested fixture level: compact - a pinned recipe and a guard's anchor move to another file; no runtime behaviour.
Minimal mergeable slice: 6.1 - its followers are the README, one test and one anchor; 6.2 is independent of it and depends on 5.1, which deletes two files that link the bring-up runbook.

## 7. Human text in scripts, services, packages, workers

- [ ] 7.1 Services, packages, workers, `apps/` text outside the API contract, SQL comments (not the
  `COMMENT ON` text), the schema description: messages, help, docstrings, comments.
- [ ] 7.2 Scripts: comments, docstrings, CLI help, messages, the gate's finding texts; the message
  asserted by the two basin-retirement tests follows; no `file.py:NNN` token is added to a Python
  comment.
- [ ] 7.3 Not touched, and shown by the counter: forbidden-host sets, evidence keys, the receipt
  `source` default, the migration comment, the alert script's subject (group 9).

Suggested fixture level: compact - strings and comments in code; two messages are asserted by tests; no control flow changes.
Minimal mergeable slice: 7.1 - about 65 lines in packages no script imports text from; 7.2 is independent of it; 7.3 is a check both PRs carry.

## 8. API and UI text

- [ ] 8.1 Retry message and the OpenAPI description chain (patching code, `openapi/nhms.v1.yaml`,
  generated types); drift test and `check:api-types`; `ops-monitoring-api` delta in the slice's own
  change.
- [ ] 8.2 Monitoring hint, the C4 probe, the fake page, three tests; `display-readonly-ops-ui` delta in
  the slice's own change.
- [ ] 8.3 node-27, after 8.1 and 8.2 are merged. **Stop: needs the owner's go-ahead for the production
  action.** Display API restart, frontend rebuild and swap, C1 receipt, C4 live receipt taken with the
  new probe against the new page; receipt posted on the issue.

Suggested fixture level: expanded - public API description and generated types; a probe that runs against the production page.
Minimal mergeable slice: 8.1 - backend text and its generated chain, verified by the drift test; 8.2 is independent of it (frontend suite); 8.3 depends on both and is its own verification path (live receipts).

## 9. Units and alert lane

- [ ] 9.1 Alert mail subject and host fallback with their tests; `node22-unit-failure-alert` delta in
  the slice's own change. No installed file changes: the script runs from the production checkout.
- [ ] 9.2 14 `Description=` lines and the unit comments; `MANIFEST.json` shas. **Stop: needs the
  owner's go-ahead for the production action.** On the compute control host, before the PR's last
  push, at a pass boundary: drift probe before, install, `daemon-reload`, drift probe after with the
  PR's manifest, operator receipt in the PR. No test mail is sent; the subject is proven by the unit
  test and read from the next real alert.

Suggested fixture level: expanded - deployed unit bytes and the text of a production alert.
Minimal mergeable slice: 9.1 - script and tests only, deployed by the next checkout sync; 9.2 is independent of it and atomic, because the manifest, the tracked bytes and the installed copies have to agree when it merges.

## 10. Specs

Each task generates its deltas from the main text of that day with asserted substitutions, renames
the requirement titles that state a location, and checks every rewritten sentence that a test backs
against that test. `appendix-inventory.md` Appendix B is the work list.

- [ ] 10.1 The specs with one to three mentions (about 36).
- [ ] 10.2 The contract specs' remaining requirements (`production-topology-contract`,
  `production-ops-readiness`) and the specs named after the codename (`node22-db-retirement-cutover`,
  `node22-dbfree-runtime`, `node22-env-hygiene`, `node22-unit-tracking`,
  `slurm-gateway-node22-deployment`): statements about the retired host get the retired-host phrase.
- [ ] 10.3 The scheduler and pipeline specs with several mentions (`production-scheduler-orchestration`,
  `compute-scheduler-operationalization`, `scheduler-registry-refresh`, `job-retry-mechanism`,
  `node27-download-orchestration`, `scheduler-stall-alerting`, `variant-activation-cutover`,
  `canonical-precip-copyback`, `basin-retirement`, `forcing-domain-handoff`,
  `pipeline-job-persistence`).

Suggested fixture level: none - spec text; no code, no test expectation changes.
Minimal mergeable slice: 10.1 - one or two sentences per spec, no requirement shared with another task; 10.2 depends on 3.6 and 4.5 (the same two contract specs); 10.3 is independent of both.

## 11. Tests

- [ ] 11.1 Comments and docstrings (about 200 lines); literals left over from groups 2 to 10.
- [ ] 11.2 Final counter in the PR: every remaining `bare` line by the classes of `design.md` §3; the
  residue list is the input the owner needs for the guard question.
- [ ] 11.3 Archive this change in the PR's last push.

Suggested fixture level: none - comments and docstrings in tests; no assertion changes except leftover literals that follow already-merged text.
Minimal mergeable slice: atomic - comments have no dependants and one verification path (the suite's results do not change); a first cut would be an arbitrary directory split with no delivery value.

## Evidence Floor

- Per PR: touched and reading test files locally; `uv run ruff check --no-cache .`; markdownlint on
  edited docs; `openspec validate <the slice's change> --strict --no-interactive` where the slice has
  one, and `openspec validate sweep-compute-role-narration --strict --no-interactive` when this
  change's files are edited; hard gate; node-27 run of the same test files; CI.
- 8.3 and 9.2 additionally: the live receipts named in their tasks.
