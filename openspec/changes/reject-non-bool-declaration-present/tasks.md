# Tasks: reject-non-bool-declaration-present

Fixture level: compact (one-field contract fix in a shared normalizer +
tests + spec alignment; issue is S-size with recorded triage decision)
Repair intensity: normal

Risk packs considered (core):
- Data shape / contract: selected - the whole issue is a contract
  disambiguation; the reject rule must not change the missing-key default
- Error handling / rollback / partial outputs: selected - new raise must
  reuse SCHEDULER_REGISTRY_CUTOVER_AUDIT_INVALID with a details payload
  consistent with the sibling arms
- Auth / permissions / secrets: not selected - no privilege surface
- Config / project setup: not selected
- Documentation / migration notes: not selected - spec delta carries the
  contract; no runbook mentions declaration_present coercion

## 1. Normalizer + tests + spec

- [x] 1.1 `packages/scheduler/registry_audit.py:80`: replace
  `declaration_present = bool(cutover_gate.get("declaration_present"))`
  with a fail-closed read:
  ```python
  declaration_present = cutover_gate.get("declaration_present")
  if declaration_present is None:
      declaration_present = False
  elif not isinstance(declaration_present, bool):
      raise SchedulerRegistryPublishError(
          "SCHEDULER_REGISTRY_CUTOVER_AUDIT_INVALID",
          "cutover_gate.declaration_present must be a boolean.",
          details={"provided_type": type(declaration_present).__name__},
      )
  ```
  (exact message/details style mirrors the `declaration_env` arm at
  `:74-79`). Missing key and explicit None both stay `False` — the absent
  case's observable output is unchanged. Update the module/function
  docstring sentence describing the three-field shape.
  Evidence floor: `git diff` confined to this field's handling + docstring.
- [x] 1.2 `tests/test_registry_audit.py`: add
  `test_normalizer_rejects_non_bool_declaration_present` parametrized over
  at least `"no"`, `1`, `0`, `1.0`, `[]` (mix of truthy and falsy
  non-bools — the falsy ones prove reject-not-coerce in BOTH directions).
  FIXTURE BLOCK SHAPE IS PINNED (fixture-review P2-1): each call must use
  a FULL valid block except the field under test —
  `{"mode": "enforced", "declaration_env": "E", "declaration_present": <param>}`
  — NOT the `{"mode": mode}` shape of the `:32` model test: a bare
  `{"declaration_present": v}` block raises at the mode arm (`:67`) with
  the SAME error code pre-change, making the test a false green. Assert
  `SchedulerRegistryPublishError` with error_code
  `SCHEDULER_REGISTRY_CUTOVER_AUDIT_INVALID` AND
  `to_payload()`/details `provided_type` matching the input's type name
  (mirrors `:43-51`, distinguishes this arm from the mode arm). Plus
  `test_normalizer_defaults_missing_declaration_present_to_false` pinning
  {mode, declaration_env} without the key → `declaration_present is False`
  (and same for explicit `None`).
  Evidence floor: RED-PROOF mandatory — against the pre-change normalizer
  the rejection test must FAIL with "DID NOT RAISE" (values are currently
  coerced, e.g. `"no"` → True), NOT with an assertion mismatch on some
  other field; the missing-key test must pass pre AND post (default
  unchanged). Record outputs verbatim.
- [x] 1.3 Spec alignment (change `unify-cutover-gate-audit-normalizer` is
  archived → this change carries a MODIFIED requirement in
  `specs/scheduler-registry-refresh/spec.md` restating the normalizer
  requirement with the `declaration_present` rejection scenario added).
  The merged spec source to align with:
  `openspec/specs/scheduler-registry-refresh/spec.md:6-45` (requirement
  at :6, sentence at :8). Keep every existing scenario of that
  requirement verbatim; add one rejection scenario.
  Evidence floor: `openspec validate reject-non-bool-declaration-present
  --strict --no-interactive` PASS.
- [x] 1.35 Channel-level pin (fixture-review P2-2, route (a)): add one
  `pytest.param({"mode": "enforced", "declaration_env": "E",
  "declaration_present": "no"}, id="non_bool_declaration_present")` to
  `_MALFORMED_CUTOVER_GATES` (`tests/test_publish_scheduler_file_registry.py:1494-1502`)
  so the manifest channel's fail-closed behavior on the new arm is pinned
  end-to-end; spec delta scenario 1's WHEN already lists non-boolean
  `declaration_present`.
  Evidence floor: `uv run pytest -q tests/test_publish_scheduler_file_registry.py
  -k malformed_cutover_gate` green including the new param.
- [x] 1.4 Caller sweep (read-only, record in PR body): confirm the only
  in-tree producers pass real bools —
  `scripts/publish_scheduler_file_registry.py:1206/:1216` and
  `scripts/scheduler_file_provider_refresh.py:811-817` — and no other
  call site constructs a cutover_gate mapping. If any passes a non-bool,
  STOP and report (would falsify the no-production-trigger premise).

## 2. Change-level verification floor

- [x] 2.1 `uv run pytest -q tests/test_registry_audit.py
  tests/test_publish_scheduler_file_registry.py
  tests/test_scheduler_file_provider_refresh.py` green.
- [x] 2.2 `uv run ruff check .` clean.
- [x] 2.3 `openspec validate reject-non-bool-declaration-present --strict
  --no-interactive` PASS.
- [x] 2.4 Scope check: `git diff --name-only origin/master...HEAD` =
  registry_audit.py, test_registry_audit.py,
  test_publish_scheduler_file_registry.py (one pytest.param), this
  fixture. Nothing else.
