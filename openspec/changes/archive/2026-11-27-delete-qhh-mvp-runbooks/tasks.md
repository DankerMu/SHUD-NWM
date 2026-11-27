# Tasks: delete the four QHH MVP evidence runbooks (#3405)

Must preserve: the section-marker test still fails when the production section of its remaining
carrier uses `--plan`, and when the page stops reserving `--plan` for the dry-run / no-mutation
path. History files are not edited. Files owned by later slices are edited only where they name one
of the four files.

## 1. Deletions and followers (orchestrator)

- [x] 1.1 `git rm` the four runbooks.
- [x] 1.2 `docs/governance/API_CONTRACT_RETIREMENT_INVENTORY.md`: the three places that name
  `qhh-mvp-smoke-evidence.md` or the checklist.
- [x] 1.3 `docs/governance/LEGACY_DEAD_CODE_INVENTORY.md`: the four places that name
  `qhh-continuous.md` or `qhh-mvp-smoke-evidence.md`; what the remaining files and tests record
  stays.
- [x] 1.4 `docs/runbooks/qhh-backend-smoke.md`: the pointer sentence to `qhh-mvp-smoke-evidence.md`.
- [x] 1.5 Spec delta: `evidence-boundary-hardening`, one requirement removed.

## 2. Test (implementer)

- [x] 2.1 `test_docs_reserve_plan_for_no_mutation_and_use_submit_for_production_submission`: the two
  deleted carriers leave the list; no assertion is weakened.
- [x] 2.2 Two red runs on the remaining carrier, each restored: `--plan` put into the production
  section of `docs/VALIDATION.md`; the words "dry-run/no-mutation" taken out of the page.

## 3. Evidence

- [x] 3.1 A search of the repository for each of the four file names returns only history (listed
  by path) and this change's and the umbrella change's own files.
- [x] 3.2 No link in a remaining document points at a deleted file.
- [x] 3.3 Hard gate: finding multiset identical before and after, or each difference listed.
- [x] 3.4 `narration_count.py` plain and `--tokens` before and after; moved name tokens listed.

## Evidence Floor

- `uv run pytest -q tests/test_production_scheduler.py -k
  docs_reserve_plan_for_no_mutation` and the entropy-audit files that read `docs/` locally and on
  node-27; `uv run ruff check --no-cache .`; markdownlint on the edited docs;
  `openspec validate delete-qhh-mvp-runbooks --strict --no-interactive` before the archive;
  `openspec validate evidence-boundary-hardening --type spec --strict --no-interactive` and
  `openspec validate sweep-compute-role-narration --strict --no-interactive` after; CI, every shard.
