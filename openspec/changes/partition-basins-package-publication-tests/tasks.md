## 1. Baseline Contract

- [x] 1.1 Persist baseline SHA/source/guard/selector digests, exactly 88 unique nodes,
  baseline full-node digest
  `f609688b6b6df4e870ef1add8afa56f009a06ef3b819257b19018731f186c138`, stable
  baseline/post suffix digest
  `f8d203d5d6637541201300d0d0be3b5863c670904a556e18fd12e94801ed6787`, all 80
  normalized function/decorator/parameter fingerprints, helper definitions/importers and
  the existing model-registry owner selection.
- [x] 1.2 Run the baseline monolith and record pass/skip count; record source imports, monkeypatch target literals, current validation commands and the exact sole sibling helper importer.

## 2. Six-Suite Physical Partition

- [x] 2.1 Retain core lines 1–847 in `tests/test_basins_package_publication.py`; mechanically move refusal 848–1495, failures 1496–2007, TOCTOU 2008–2611, migration 2612–3241 and forcing-identity 3420–3582 into the six frozen owners.
- [x] 2.2 Move shared imports/constants and baseline helpers 3242–3417 into non-collectible `tests/basins_package_helpers.py`; import support at module scope from every consumer and update `tests/test_basins_package.py` to the single helper owner.
- [x] 2.3 Compare pre/post suffix sets and normalized definitions; require exactly 88 unique identical suffixes and 80 one-to-one fingerprints with no assertion, decorator, parameter ID, skip, fixture or monkeypatch-target drift.

## 3. Selector, Guard and Current Docs

- [x] 3.1 Add one six-partition selector authority; extend `workers/model_registry/**` so production-owner changes select all six plus every prior target.
- [x] 3.2 Route helper-only changes through `SUPPORT_MODULE_TEST_RULES` to exactly six partitions + `tests/test_basins_package.py` (+ existing selector meta rider); add exact-set/tree-derived closure tests.
- [x] 3.3 Run per-edge constructed-RED mutations for all six production-owner routes—including retained core, which is not same-name-derived from `basins_package.py`—and all seven helper consumers; restore each edge, then prove the real tables GREEN.
- [x] 3.4 Update every live publication command (M9 closeout, #148 regression and `NHMS_RUN_BASINS_SMOKE`) to list all six suites; prove the moved real-smoke node still runs, and keep historical M9 result bullets plus archived evidence byte-identical.
- [x] 3.5 Move the baseline heading-bounded M10 family—from line 172
  `## M10 #147 Production Slurm Closure` through the blank immediately before line 842
  `## M19 Production Readiness Proof` (670 lines)—into
  `docs/validation/production-closure.md`; preserve the block byte-for-byte except the
  task-3.4 six-file command expansion and moved self-lint paths, leave all six original
  heading texts/slugs as linked root stubs, and mark `docs/validation/**` current in
  `DOC_STATUS.md`.
- [x] 3.6 Record root/child post-split line counts (both `<1000`), prove six root heading texts and generated anchor slugs byte-identical to baseline, resolve every stub link to the child heading, and confirm no other M10 evidence/prose or historical M9 result bullet changed.
- [x] 3.7 Prove every changed/new text source `<1000` lines, `.large-file-guard.json` byte-identical (`5c06fad8ba8f488d8bfc836e747cd7af642232a880bec25ae132e1bd17ab87ad` baseline digest), no replacement exclusion, Markdown lint success and ordinary hook acceptance.

## 4. Evidence Floor

- [x] 4.1 Run explicit six-file collect-only and pytest; expect 88 unique identical suffixes and baseline pass/skip semantics, helper zero collection and `tests/test_basins_package.py` import success.
- [x] 4.2 Run `uv run pytest -q tests/test_select_ci_tests.py` plus selector exact-set/mutation proofs; expect all assertions GREEN after each mutation is restored.
- [x] 4.3 Run affected Basins/object-store/scheduler consumers and `uv run pytest -q`; expect no regression or production-source drift.
- [x] 4.4 Run ruff for every changed/new Python file, entropy report/hard structural checks, strict single/all OpenSpec, Markdown lint and `git diff --check`; expect zero new violations.
- [ ] 4.5 On node-27, run frozen-SHA six-suite focused backend receipt and compare baseline/frozen collection/pass/skip; no DB/display mutation is required. Node-22 is not applicable.
- [x] 4.6 Confirm final diff contains only test layout/helper, sibling import, selector metadata/meta-tests, root/M10 current validation ownership, DOC_STATUS routing and this OpenSpec change; explicitly exclude #1903, registry corpus, database filter and production behavior.
