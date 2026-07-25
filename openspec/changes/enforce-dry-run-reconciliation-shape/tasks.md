# Tasks: enforce-dry-run-reconciliation-shape

Fixture level: compact (single-function validator hardening + negative
tests; issue is implementation-ready with probe-verified gap and explicit
legal-constraint boundary)
Repair intensity: normal

Risk packs considered (core):
- Error handling / rollback / partial outputs: selected - the validator is
  tamper-resistance depth; every new raise must use the existing
  `receipt_classification_invalid` token, and honest receipts must not be
  rejected
- Data shape / contract: selected - the exact set of constraints valid in
  id-only mode is the crux (removed==0 yes; full previous equality NO)
- Auth / permissions / secrets: not selected - no privilege surface
- Config / project setup: not selected - no config change
- Documentation / migration notes: not selected - validator-internal; spec
  delta carries the contract

## 1. Validator + tests

- [ ] 1.1 `scripts/scheduler_file_provider_refresh.py` dry_run branch
  (`:1977-1987` — `if outcome == "dry_run":` at :1977, early `return` at
  :1987; NOTE :1988-1992 is the post-return non-dry_run section and must
  stay untouched), inside
  `_enforce_registry_classification_reconciliation`: BEFORE the early
  `return`, add:
  (a) `if removed_total != 0: raise ValueError("receipt_classification_invalid")`
  — dry_run never evaluates removals (writer comment `:2523-2524`; removal
  loop `:2542-2544` sits after the dry_run return `:2525`).
  (b) read `previous_count = classification.get("previous_model_count")`
  and `previous_sha = classification.get("previous_registry_sha256")`
  locally in the branch; if `previous_sha is None`: require
  `previous_count is None`; else require `previous_count` is a non-bool
  int >= 0 AND `unchanged_total <= previous_count`.
  (c) `if classification.get("new_registry_sha256") is not None: raise ...`
  — writer pins it to None in dry_run (`:2485`); only non-null 64-hex
  format is checked today (`:1834-1840`).
  Do NOT add `unchanged + package_changed + removed == previous_count` to
  this branch — it is false in dry_run (removals uncomputed; issue
  acceptance criterion 4). Keep every existing dry_run check byte-identical.
  Mirror the non-bootstrap branch's isinstance/bool/negative guards for
  `previous_count` (`:2007-2012` ONLY — line `:2013` is exactly the
  forbidden equality, do not copy it).
  Update the function docstring's dry_run bullet to state the new shape
  constraints.
  Evidence floor: `git diff` confined to the dry_run branch + docstring;
  the post-`return` sections untouched.
- [ ] 1.2 Negative tests (append to
  `tests/test_scheduler_file_provider_refresh.py`). CALL SHAPE IS PINNED
  (fixture-review P2-2): call the validator DIRECTLY —
  `refresh._enforce_registry_classification_reconciliation(classification,
  outcome="dry_run", reason=<honest dry_run reason>)` — do NOT reuse the
  `_validate_receipt`-based recipe of `:3833` as-is: for
  `outcome="dry_run"` `_validate_receipt` requires the full provider
  triple (`:1729-1733`) and `providers=[]` false-reds with
  `receipt_provider_invalid`. (Alternative only if you need receipt-level
  coverage: build the full provider triple per the `:3876` pattern.)
  Each tampered classification MUST pass every PRE-EXISTING dry_run check
  (package_changed/refused/declared totals 0; added+unchanged ==
  prospective_count) so the pre-change validator ACCEPTS it and the red
  reason is exactly "DID NOT RAISE", not an adjacent-check false red:
  (i) bootstrap dry_run tamper: previous_sha=null, previous_count=null,
  removed.total=1 (with matching item), added+unchanged == prospective →
  must raise `receipt_classification_invalid`.
  (ii) non-bootstrap dry_run tamper: previous_sha=<64-hex>,
  previous_count=5, removed.total=1, unchanged<=5 → must raise.
  (iii) contradictory shape: previous_sha=null but previous_count=7,
  removed=0 → must raise.
  (iv) upper-bound: previous_sha=<64-hex>, previous_count=2,
  unchanged.total=3 (added+unchanged == prospective kept consistent) →
  must raise.
  (v) forged publish sha: dry_run + `new_registry_sha256=<64-hex>`, all
  other fields honest → must raise (pins 1.1(c)).
  Optional pin of the 1.1(b) isinstance guard (previous_count=True under
  non-null sha): meaningful ONLY via the direct call — on the
  `_validate_receipt` path `_validate_registry_classification_field`
  (`:1841-1848`) already rejects bool counts for every outcome, so a
  receipt-level version is a vacuous always-green (fixture-review P2-3).
  Evidence floor: RED-PROOF mandatory — run the five new negative tests
  against the PRE-change validator (git stash the validator hunk or use
  `git show`): all five must FAIL with "DID NOT RAISE" (the tampered
  classifications are currently ACCEPTED). No `receipt_provider_invalid`
  may appear in the red output. Record outputs verbatim. Green after 1.1.
- [ ] 1.3 Honest dry_run happy-path regression (one test, SAME direct-call
  shape as 1.2): classification shaped exactly as the writer produces for
  "prospective has additions AND previous has models absent from
  prospective" — e.g. previous_sha=<64-hex>, previous_count=3,
  unchanged.total=2, added.total=2, removed.total=0,
  new_registry_sha256=None, prospective_model_count=4 (note unchanged <
  previous_count, sum equality intentionally NOT satisfied) → validator
  must NOT raise. This pins acceptance criterion 4 (the full equality must
  not creep into dry_run) — fixture review probe-confirmed this shape is
  ACCEPTED by the dry_run branch and REJECTED by the published branch, so
  it genuinely discriminates.
  Also confirm existing dry_run happy-path tests stay green unmodified.
  Evidence floor: test passes pre-change AND post-change (it guards against
  over-tightening, not the gap itself) — record both runs.
- [ ] 1.4 Sibling-surface check (read-only, record result in PR body): no
  other caller constructs dry_run receipts that would newly fail — grep
  for `_enforce_registry_classification_reconciliation` call sites and any
  fixture receipts with `outcome="dry_run"` in tests; adjust NONE of them
  (honest shapes must already satisfy the new constraints; if one does
  not, STOP and report — that would falsify the writer-honesty premise).

## 2. Change-level verification floor

- [ ] 2.1 `uv run pytest -q tests/test_scheduler_file_provider_refresh.py`
  green (full suite).
- [ ] 2.2 `uv run ruff check .` clean.
- [ ] 2.3 `openspec validate enforce-dry-run-reconciliation-shape --strict
  --no-interactive` PASS.
- [ ] 2.4 Scope check: `git diff --name-only origin/master...HEAD` = the
  validator script, the tests file, this fixture. Nothing else.
