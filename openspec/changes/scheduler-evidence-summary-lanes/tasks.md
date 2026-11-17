# Tasks — scheduler-evidence-summary-lanes (#3170)

- [ ] 1.1 Triage: reconstruct the 132-candidate pass as design.md describes, rank lanes by indent-2 bytes after tiers 3–4,
  and record the table plus the script path in design.md.
- [ ] 1.2 Tier 4b: compact retry of the candidate-list summary.
- [ ] 1.3 Tier 5: lane projection per design.md, starting with `model_run_evidence`. Record the per-field keep/drop
  decision with reader evidence. `summarized_fields` must be honest.
- [ ] 1.4 Fallback `limit.lane_bytes` attribution, bounded and shed before the existing floor fields.
- [ ] 1.5 Tests per E3: red on the old code for new behaviour, plus the golden byte-identity for tiers 1–4a. Route new
  test files in the selector.
- [ ] 1.6 Update the docs that describe the tiers, if any: `docs/runbooks/production-ops/stuck-detection.md` and
  `docs/runbooks/node22-*` mentions of the size fallback.
