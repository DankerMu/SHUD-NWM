# OpenSpec Governance Glossary

This glossary is the canonical vocabulary for entropy-governance terms used by
OpenSpec changes, GitHub issues, scoped `AGENTS.md` files, and governance
reports. When a scoped instruction needs one of these concepts, link here or
reuse the term exactly instead of introducing a local synonym.

## Terms

| Term | Definition |
|---|---|
| active entrypoint | The current public route, CLI, module function, or operational command that callers should use. Historical names may mention it, but only the active entrypoint owns current behavior and verification. |
| legacy redirect alias | A compatibility surface that accepts an old route, name, or command and forwards to an active entrypoint. It is retained for caller continuity and must not be treated as a second product surface or owner. |
| retired active-tree path | A previously active tracked path that must not return to the live source tree unless a new issue explicitly reactivates it. Text mentions can remain as historical evidence when they are marked or allowlisted by governance rules. |
| compatibility facade | A module that keeps old import, monkeypatch, or call surfaces stable while delegating behavior to real owner modules. New facade surface requires inventory or guard evidence; local bug fixes that do not add ownership surface are not facade growth. |
| lane | A bounded validation or evidence responsibility inside a larger workflow, with an owner module, input contract, output/result shape, blocker or finding namespace, focused verification command, and retention condition. |
| budget-counted finding | An unallowlisted active entropy finding that consumes the current cleanup budget. It should map to a cleanup owner, a follow-up issue, or a deliberate accepted disposition. |
| gate-eligible finding | A budget-counted finding whose check ID is in the prepared hard-gate set and whose individual policy marks it eligible for explicit hard-gate failure. Gate eligibility is narrower than budget counting and does not enable CI failure by itself. |
| current authority | The source a reader or agent must consult before treating preserved text as actionable. It can be an active spec, runbook, inventory, source module, test, or documented decision that owns the present contract. |
| historical evidence | Preserved docs, archived specs, examples, logs, or work records kept for auditability, migration context, or regression proof. Historical evidence may explain why old terms exist, but it does not override current authority. |

## Usage Rules

- Prefer these exact terms in scoped `AGENTS.md` files and governance specs.
- Do not make a local synonym for a term in this file unless a new OpenSpec
  change updates this glossary first.
- Use `current authority` whenever archived or superseded text could otherwise
  look like live instructions.
- Keep `budget-counted finding` and `gate-eligible finding` separate in reports,
  PR evidence, and issue acceptance criteria.
