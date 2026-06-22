## 1. Evaluation

- [x] 1.1 Review current disk-only station-series contract and legacy DB helper boundary.
- [x] 1.2 Decide whether long-term history belongs in the current route or a separate surface.
- [x] 1.3 Record freshness, retention, provenance, and error-code semantics.

## 2. Documentation

- [x] 2.1 Add ADR for the station forcing history API boundary.
- [x] 2.2 Add OpenSpec delta for future history/archive semantics.
- [x] 2.3 Update object-store station-series runbook follow-ups and references.

## 3. Verification

- [x] 3.1 `openspec validate evaluate-forcing-series-history-api --strict --no-interactive` PASS.
- [x] 3.2 `openspec validate --all --strict --no-interactive` PASS, 178 passed.
- [x] 3.3 `corepack pnpm dlx markdownlint-cli2 "docs/**/*.md"` PASS.
- [x] 3.4 `git diff --check` PASS.
