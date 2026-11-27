# Tasks: compute-role narration sweep

A group that is not atomic lands as several PRs, first slice first (the last line of each group says
which). The order between tasks of different groups is the table of `design.md` §6; a task not
named there depends only on groups 1 and 2. Every PR carries in its body the counter output of
`design.md` §3 before and after
(`uv run --no-sync python -I openspec/changes/sweep-compute-role-narration/narration_count.py`, and
`--tokens` for the name tokens) and the hard-gate finding multiset (check id, path, matched line)
before and after. Every PR ticks
its tasks here in its final push.

## Must preserve (all groups)

- The hard gate passes on the real repository with the same finding multiset, or each difference is
  listed and judged in the PR.
- Every test that reads a rewritten page by structure stays green; a test that pins a rewritten
  sentence follows it in the same PR with a red run on the old sentence. A test that pins a dated
  observation stays as it is, with its sentence.
- A `name` token count of the counter moves only for a deleted file or a removed pointer to one, and
  the PR lists it.
- Commands that remain in a live runbook are the ones the page had, minus the literal `ssh` to the
  retired host; `uv` discipline lines for the compute hosts stay as they are.
- A renamed heading takes every inbound link with it, and a deleted file every reference to it. The
  PR shows a repository-wide search for the old slug or file name returning nothing outside history,
  outside this change's directory and apart from tests' own synthetic inputs (each listed in the PR).
- No rewritten line ends inside a role term (`design.md` §3): a search of the PR's files for lines
  that end with "compute" or "compute control" finds none that continues the term on the next line.
- No rewritten line joins a role term to another word with a hyphen (`design.md` §3, compounds).
- A rewritten denial sits in the clause of the name it denies (`design.md` §3, denials); the PR
  lists each rewritten line that carries a denial about a database.
- History, persisted values and the gate's existing vocabulary are not edited (`design.md` §3).

## 1. Gate vocabulary

- [x] 1.1 The role and host-set terms of `design.md` §2 are a subject for both legs of the topology
  family (DB writer, local PostgreSQL), with the token boundaries of the host names.
- [x] 1.2 Synthetic cases: each term × each leg × asserted / denied; boundaries (`compute_control`, a
  path, an identifier, upper case); mixed runs ("the compute role (w3 / w2)").
- [x] 1.3 Real repository: finding multiset before and after; every new or lost finding listed and
  judged; mutation: one term removed, its case goes red.
- [x] 1.4 Spec deltas in the slice's own change, both in `production-topology-contract`: "Static
  guardrails block topology drift" (the gate's subjects); "Current production topology is a governed
  contract" (the sentence that lists the identifiers keeping the codename no longer lists requirement
  names). Found by this slice: a scenario cannot be retitled or dropped by a MODIFIED block
  (`design.md` §3, "Scenario titles").

Suggested fixture level: expanded - changes what a hard gate that runs on every PR matches.
Minimal mergeable slice: atomic - a term without its synthetic cases and the real-repository comparison cannot be judged; they are one verification path.

## 2. Terms

- [x] 2.1 `openspec/glossary.md`: the seven entries of `design.md` §1, the retired spellings, and the
  difference from the runtime role `compute_control`; its existing line that uses the codename as a
  place.
- [x] 2.2 `instructions/agents/shared.md` uses the terms consistently; `CLAUDE.md` / `AGENTS.md`
  regenerated (byte-exact test); the retired-host line keeps the terms its test requires; the host
  statement (`w3` and `w2`, the retired host, the link to the topology runbook) stays.
- [x] 2.3 The scoped `AGENTS.md` files that narrate where compute runs (`appendix-inventory.md` §8,
  slice 2).

Suggested fixture level: none - documentation and generated instruction files; no runtime behaviour.
Minimal mergeable slice: atomic - the glossary entry and the instruction lines that use it are one statement of the vocabulary; landing one without the other leaves the two sources disagreeing, which is what the group removes.

## 3. Production-ops pages

Each task rewrites the bodies with the glossary, removes the page's literal `ssh` lines
(`design.md` §3, commands) and its banner pointer, and keeps dated observations with their pins.

- [ ] 3.1 `operating-scope.md`, `frontier-stall-alert.md`, `parse-failure-residency-alert.md` (no
  `ssh` line, no pointer, no pinned sentence, no heading that carries the codename).
- [ ] 3.2 `artifacts-and-storage.md`, `recalibration-and-archive.md`: the heading §5.8 with its inbound
  link in `current-production-ops.md`.
- [ ] 3.3 `service-bringup.md`, `known-issues-state-index.md` (structure readers named in
  `appendix-inventory.md` §2b stay green).
- [ ] 3.4 `stuck-detection.md`, `known-issues-pipeline.md`: the heading §8.5 with its five inbound
  links (in `current-production-ops.md`, `node22-control-plane-manual-recovery.md`,
  `file-provider-refresh.md`, `gateway-and-services.md`, `stuck-detection.md`).
- [ ] 3.5 `gateway-and-services.md`, `file-provider-refresh.md`, `overview.md`: the pin of
  `tests/test_node22_entrypoint_invariant_python_scan.py` on the key-entry row of `overview.md`
  follows its row; its pin on the dated 2026-06-22 line of `gateway-and-services.md` stays.
- [ ] 3.6 `current-production-ops.md`: the body, with the host statement (`w3` and `w2`, the retired
  host, the link to the topology runbook) moved out of the banner; the banner goes. A static test in
  `tests/test_node22_entrypoint_invariant.py`: these pages hold no `ssh` command addressed to the
  retired host (red run: one line restored), and the index page keeps the host statement (red run:
  the statement removed). Spec deltas in the slice's own change: `production-ops-readiness`, "Current
  production ops runbook reflects verified 27-centric topology" (the banner scenario goes; the
  scenarios that say what the runbook calls the compute side follow the page);
  `production-topology-contract` (the banner scenario no longer names this page).

Suggested fixture level: none - documentation; 3.6 adds one static test over documents and a spec delta, still no runtime behaviour.
Minimal mergeable slice: 3.1 - three pages with no pinned sentence, no command to remove and no heading that changes; 3.2 and 3.3 are independent of it; 3.4, 3.5 and 3.6 follow the order of `design.md` §6.

## 4. Other live docs

- [ ] 4.1 `docs/governance/ROLE_BOUNDARY.md` and `openspec/project-profile.md`, with their three
  pinned sentences and the required-term set in `tests/test_role_boundary_static.py`; the host
  statement of `ROLE_BOUNDARY.md` stays.
- [ ] 4.2 `docs/spec/*`, `docs/modules/*`, the two forcing notes under `docs/`, the host line of
  `docs/VALIDATION.md`, the two narration lines of `docs/governance/LEGACY_DEAD_CODE_INVENTORY.md`,
  `scripts/diagnostic/qhh/README.md` (the tokens its test pins stay).
- [ ] 4.3 `infra/env/README.md`: the body, the authority heading with its two tests and three inbound
  links; the comments of `infra/env/*.example`.
- [ ] 4.4 Live runbooks without a banner: `failed-basin-retry.md`,
  `scheduler-dbfree-typed-reasons.md`, `slurm-gateway-preflight.md`,
  `canonical-precip-mirror-backfill.md`, `held-forcing-absence-exit.md`, `ci-test-routing.md`,
  `node-27-bringup-checklist.md`, `tier-node27-timeseries-storage.md`,
  `display-readonly-live-mvt.md`.
- [ ] 4.5 The two remaining banner pages, `node22-control-plane-manual-recovery.md` and
  `two-node-deployment-overview.md` (the bare "22" shorthand; the heading and sentence pinned by
  `tests/test_node22_runbook_section_markers.py`; the dated 2026-06-22 block and its pin in
  `tests/test_role_boundary_static.py` stay; the overview stays skipped by the gate, and the PR shows
  the gate's skip decision for it before and after); spec delta in the slice's own change:
  `production-topology-contract` loses the banner scenario.
- [ ] 4.6 `forcing-copyback-backfill.md`, the other page the gate skips (`design.md` §3): narration;
  one sentence added to its header that the CLI and its spec are live; the status words stay. The PR
  shows the gate's skip decision for the path before and after, and
  `tests/test_node22_entrypoint_invariant.py` green on the "historical" requirement.

Suggested fixture level: none - documentation and template comments; pinned sentences are followed, no runtime behaviour.
Minimal mergeable slice: 4.1 - two documents and one test function, shared with no other task; 4.2 to 4.5 follow the order of `design.md` §6; 4.6 is independent of the others.

## 5. Historical runbooks with shared followers

- [x] 5.1 Delete the QHH MVP family: `qhh-continuous.md`, `qhh-mvp-smoke-evidence.md`,
  `qhh-mvp-production-like-e2e-checklist.md`, `qhh-controlled-failure-retry-evidence.md`; the two
  section-marker entries of `tests/test_production_scheduler.py`; the requirement of
  `evidence-boundary-hardening` that demands the checklist (the slice's own change); the rows of the
  two governance inventories that name these four.
- [ ] 5.2 Delete `two-node-production-e2e-plan.md` and `node22-db-retirement-runbook.md`; the plan's
  snippet (0600 check, `source`, validator command with its evidence arguments) moves to
  `infra/env/README.md`, and both assertions of `tests/test_readonly_db_validation_routes.py` read it
  there (red runs: the order swapped; the run-id directory doubled); the scenario of
  `evidence-boundary-hardening` that names the plan (the slice's own change); the runbook leaves the
  tuple of the rollback-snippet test in `tests/test_role_boundary_static.py`, the receipt stays; the
  three pointer sentences retargeted to `infra/env/display.example`, `ROLE_BOUNDARY.md` and the
  node-27 checklist; the two links in `two-node-deployment-overview.md`; inventory rows.

Suggested fixture level: compact - deletions whose tests are re-homed, not dropped; no runtime behaviour.
Minimal mergeable slice: 5.1 - the family's followers are two test entries, one requirement and inventory rows that name only these four; 5.2 depends on it because both edit `evidence-boundary-hardening` and the two inventories.

## 6. Historical runbooks with a residue

- [ ] 6.1 `qhh-backend-smoke.md`: the local-lane lines move to `scripts/diagnostic/qhh/README.md`; the
  second recipe pin of `tests/test_qhh_scripts_static.py`, the governance schema's freshness anchor
  with the synthetic instruction files of `tests/test_entropy_audit_instruction_inventory.py` that
  mirror it, and `apps/api/AGENTS.md` follow; its citation in the forcing note; its rows in both
  governance inventories; delete.
- [ ] 6.2 `qhh-22-business-bringup.md`: script header comments and the authority-invariant test (a
  header names no deleted runbook); the front-matter test in
  `tests/test_node22_entrypoint_invariant.py` goes; the three citations in the forcing note reworded;
  the pointers in `current-production-ops.md` and `stuck-detection.md` and the link in
  `tests/fixtures/basins/qhh-sample/README.md` removed; its inventory row; delete.

Suggested fixture level: compact - a pinned recipe and a guard's anchor move to another file; no runtime behaviour.
Minimal mergeable slice: 6.2 - its followers are its own except one inventory row it shares with 5.1, which it follows; 6.1 is independent of 6.2 and depends on 5.1 and 5.2 (shared inventory bullets).

## 7. Human text in scripts, services, packages, workers

- [ ] 7.1 Services, packages, workers, `apps/` text outside the API contract and outside the frontend,
  SQL comments (not the `COMMENT ON` text), the schema description, the undated "measured on" comment
  in `config/calibration_overrides.yaml` (its date is added): messages, help, docstrings, comments.
- [ ] 7.2 Scripts: comments, docstrings, CLI help, messages, the gate's finding texts; the message
  asserted by the two basin-retirement tests follows; no `file.py:NNN` token is added to a Python
  comment. The alert script is group 9's.
- [ ] 7.3 Not touched, and shown by the counter: forbidden-host sets, evidence keys, the receipt
  `source` default, the migration comment.

Suggested fixture level: compact - strings and comments in code; one message is asserted by tests; no control flow changes.
Minimal mergeable slice: 7.1 - about 65 lines in packages no script imports text from; 7.2 is independent of it and follows 6.2; 7.3 is a check both PRs carry.

## 8. API and UI text

- [ ] 8.1 The retry message and both suggested actions of the control-plane refusal (the test that
  asserts one of them follows); the two
  OpenAPI description chains (patching code, `openapi/nhms.v1.yaml`, generated types); drift test and
  `check:api-types`. `ops-monitoring-api`, all of it: the sentence of the retry requirement in the
  slice's own change, the Purpose line edited at archive.
- [ ] 8.2 Monitoring hint (both components, both variants), the fake page, the probe's fallback text,
  three tests, the frontend comment lines; `display-readonly-ops-ui` (the guidance scenario and the
  two "22-node" lines) in the slice's own change.
- [ ] 8.3 node-27, after 8.1 and 8.2 are merged. **Stop: needs the owner's go-ahead for the production
  action.** Display API restart, frontend rebuild and swap, C1 receipt, C4 live receipt; receipt
  posted on the issue.

Suggested fixture level: expanded - public API description and generated types; a probe that runs against the production page.
Minimal mergeable slice: 8.1 - backend text and its generated chain, verified by the drift test; 8.2 is independent of it (frontend suite); 8.3 depends on both and is its own verification path (live receipts).

## 9. Units and alert lane

- [ ] 9.1 Alert mail subject and host fallback with their tests; the script's comments. No spec text
  states the subject, and no installed file changes: the script runs from the production checkout.
- [ ] 9.2 `Description=` of the 16 manifest units (`design.md` §5) and the unit comments (17 manifest
  files change: the 16, and `nhms-w3-journal-backup.service` by one comment);
  `MANIFEST.json` shas; the example unit `infra/systemd/nhms-slurm-gateway.service`. For the two units
  under `infra/systemd/node22/`: a new read-only receipt, the receipt rule of
  `tests/test_node22_unit_tracking.py` (the two paths are in the 2026-10-16 receipt with their old
  shas, so "no path in two receipts" becomes "the latest receipt that records a path holds its
  sha"; red run: the old sha restored), and `node22-unit-tracking` (which receipt records which
  file; the spec's own narration and title) in the slice's own change. **Stop: needs the owner's go-ahead
  for the production action.** On the compute control host, before the PR's last push, at a pass
  boundary: drift probe before, install, `daemon-reload` (no restart), drift probe after with the PR's
  manifest, operator receipt in the PR that lists every installed file. The tests that read the unit
  files are green before the host step. No test mail is sent.

Suggested fixture level: expanded - deployed unit bytes and the text of a production alert.
Minimal mergeable slice: 9.1 - script and tests only, deployed by the next checkout sync; 9.2 is independent of it and atomic, because the manifest, the tracked bytes, the receipts and the installed copies have to agree when it merges.

## 10. Specs

Each task generates its deltas from the main text of that day with asserted substitutions, renames
the titles `appendix-titles.md` assigns to it, edits Purpose lines directly at archive
(`design.md` §3), and checks every rewritten sentence that a test backs against that test.
`appendix-inventory.md` Appendix B is the work list, minus the specs another task owns:
`ops-monitoring-api` (8.1), `display-readonly-ops-ui` (8.2), `node22-unit-tracking` (9.2),
`evidence-boundary-hardening` (5.1, 5.2).

- [ ] 10.1 The specs with one to three mentions that 10.2 and 10.3 do not name (about 30), with
  `hydro-result-ingest-and-publish` and `multibasin-product-discovery`, which Appendix B misses.
- [ ] 10.2 The remaining narration of the contract specs (`production-topology-contract`,
  `production-ops-readiness`), including the other sentences of the requirements 1.4, 3.6 and 4.5
  touched, and the specs named after the codename (`node22-db-retirement-cutover`,
  `node22-dbfree-runtime`, `node22-env-hygiene`, `node22-unit-failure-alert`,
  `slurm-gateway-node22-deployment`): statements about the retired host get the retired-host phrase.
- [ ] 10.3 The scheduler and pipeline specs with several mentions (`production-scheduler-orchestration`,
  `compute-scheduler-operationalization`, `scheduler-registry-refresh`, `job-retry-mechanism`,
  `node27-download-orchestration`, `scheduler-stall-alerting`, `variant-activation-cutover`,
  `canonical-precip-copyback`, `basin-retirement`, `forcing-domain-handoff`,
  `pipeline-job-persistence`). **Stop: needs the owner's decision on the active change
  `fix-node22-keliya-forecast-output-recompute`** (`design.md` §6).

Suggested fixture level: none - spec text; no code, no test expectation changes.
Minimal mergeable slice: 10.1 - one or two sentences per spec, and no spec in it is owned by another task; 10.2 depends on 3.6 and 4.5 (the same two contract specs); 10.3 is independent of both.

## 11. Tests

- [ ] 11.1 Comments and docstrings (about 200 lines); literals left over from groups 2 to 10.
- [ ] 11.2 Final counter in the PR: every remaining `bare` line by the classes of `design.md` §3; the
  residue list is the input the owner needs for the guard question.
- [ ] 11.3 Archive this change in the PR's last push. Precondition: 8.3 and 9.2 are ticked, and the PR
  body links their receipts.

Suggested fixture level: none - comments and docstrings in tests; no assertion changes except leftover literals that follow already-merged text.
Minimal mergeable slice: atomic - comments have no dependants and one verification path (the suite's results do not change); a first cut would be an arbitrary directory split with no delivery value.

## Evidence Floor

- Per PR: touched and reading test files locally; `uv run ruff check --no-cache .`; markdownlint on
  edited docs; `openspec validate <the slice's change> --strict --no-interactive` where the slice has
  one, and `openspec validate sweep-compute-role-narration --strict --no-interactive` when this
  change's files are edited; hard gate; node-27 run of the same test files; CI.
- 8.3 and 9.2 additionally: the live receipts named in their tasks.
