# Entropy-Aware AGENTS.md Specification

Standard constraint dimensions that instruction files should contain to effectively reduce agent-introduced entropy. This spec applies to any instruction file format (AGENTS.md, CLAUDE.md, .cursorrules, etc.) — the dimensions are platform-agnostic.

---

## Why This Matters

Instruction files are the primary mechanism through which a repository communicates constraints to agents. When they only contain workflow guidance ("how to run tests") but lack semantic, structural, and behavioral constraints, agents operate in a high-freedom space and naturally introduce entropy: new naming variants, inconsistent error handling, layer violations, stale docs.

The constraint dimensions below are the minimum set needed to meaningfully reduce agent-introduced entropy across the six axes.

---

## Dimension 1: Glossary

**What:** A list of canonical terms for key business concepts, with prohibited aliases.

**Why:** Without a glossary, agents will invent new names for existing concepts every session. This is the single largest driver of semantic entropy.

**What to include:**
- Canonical name for each core domain concept
- Prohibited aliases (names that have been used historically but should not appear in new code)
- Scope: which bounded context each term belongs to

**Example (good):**
```markdown
## Glossary
- **Member**: a registered user of the platform. Do NOT use: `user`, `account`, `profile`, `customer`.
- **Space**: a collaborative workspace owned by an organization. Do NOT use: `team`, `group`, `tenant`.
- **Journey**: a structured learning path. Do NOT use: `course`, `track`, `program`.
```

**Example (bad):**
```markdown
## Terms
We use standard terminology. See the wiki for details.
```
The bad example provides no machine-actionable constraint. An agent reading it learns nothing.

**Minimum viable version:** 5–10 core terms with prohibited aliases. This covers 80% of naming drift.

---

## Dimension 2: Dependency Rules

**What:** Which modules/packages/layers are allowed to import from which, and which directions are prohibited.

**Why:** Without explicit rules, agents will create the shortest-path import, regardless of architectural intent. This is the primary driver of structural entropy.

**What to include:**
- Allowed dependency directions (e.g., `apps/api → packages/domain → packages/db`)
- Prohibited imports (e.g., "apps/ must not import directly from packages/db; go through domain")
- Shared packages: which are truly shared utilities vs. which contain domain logic that should be scoped

**Example (good):**
```markdown
## Dependency Rules
- `apps/*` → `packages/domain` → `packages/db` → `packages/types`
- `apps/*` must NOT import directly from `packages/db`
- `packages/contracts` is generated; import types from it but do not hand-edit
- `packages/ui` may only import from `packages/types` and `packages/config`
```

**Minimum viable version:** A top-level dependency direction diagram with 2–3 explicit prohibitions.

---

## Dimension 3: Error Model

**What:** The standard error envelope, error code taxonomy, and boundary behavior conventions.

**Why:** Without a defined error model, each module invents its own error shape. This fragments behavioral entropy across the entire system and makes unified monitoring, logging, and client handling impossible.

**What to include:**
- Standard error response shape (envelope structure)
- Error code ranges or categories
- Boundary behavior: what to return for empty results, missing resources, auth failures
- What NOT to do (e.g., "do not return 200 with `{success: false}`")

**Example (good):**
```markdown
## Error Model
All API errors use this envelope:
  `{ code: string, message: string, details?: Record<string, unknown> }`

Error code prefixes:
- `AUTH_*`: authentication and authorization errors (HTTP 401/403)
- `VALIDATION_*`: input validation errors (HTTP 400)
- `NOT_FOUND_*`: resource not found (HTTP 404)
- `INTERNAL_*`: unexpected server errors (HTTP 500)

Boundary conventions:
- Empty collections: return `[]`, never `null`
- Missing optional fields: omit the key, do not include `null`
- Auth failures: always return 401 with `AUTH_EXPIRED` or `AUTH_INSUFFICIENT`, never 200
```

**Minimum viable version:** The error envelope shape + 3 boundary conventions.

---

## Dimension 4: Naming Conventions

**What:** Rules for how code identifiers are named — functions, types, events, API operations, database columns.

**Why:** Naming convention drift (camelCase vs snake_case mixed in the same layer, inconsistent verb prefixes) acts as a noise amplifier that makes all other entropy axes harder to read and maintain.

**What to include:**
- Case convention per layer (e.g., camelCase for TypeScript, snake_case for DB columns)
- Verb conventions for functions (e.g., `get*` for retrieval, `create*` for creation, `handle*` for event handlers)
- File naming rules (e.g., `kebab-case.ts`, `PascalCase.tsx` for React components)
- Event naming (e.g., `domain.entity.action` format)

**Example (good):**
```markdown
## Naming Conventions
- TypeScript identifiers: camelCase for variables/functions, PascalCase for types/classes
- Database columns: snake_case
- API operation IDs: `verbNoun` (e.g., `getMember`, `createSpace`)
- Event names: `domain.entity.past_tense` (e.g., `member.profile.updated`)
- Files: `kebab-case.ts` for modules, `PascalCase.tsx` for React components
```

**Minimum viable version:** Case conventions per layer + file naming rules.

---

## Dimension 5: Doc Freshness Rules

**What:** Which documents must be updated when corresponding code changes, and how freshness is verified.

**Why:** Without explicit freshness rules, documentation silently rots. Agents update code but skip docs because no rule tells them the doc exists or that it must stay current.

**What to include:**
- Which docs are tied to which code paths
- The rule: "if you change X, you must also update Y"
- How freshness is checked (manual review, automated drift detection, CI gate)

**Example (good):**
```markdown
## Doc Freshness Rules
- Changing `packages/contracts/` → must update `product/specs/` if the contract shape changes
- Changing API routes in `apps/api/` → must update the corresponding runbook in `docs/runbooks/`
- Changing state machine logic → must update `docs/design/state-machines.md`
- `pnpm sync:check` verifies source-generated freshness in CI
```

**Minimum viable version:** 3–5 explicit "if X changes, update Y" rules for the most critical paths.

---

## Dimension 6: State Model References

**What:** Pointers to where key entity state machines and lifecycle models are defined.

**Why:** When state logic is scattered across handlers, services, and jobs with no central definition, agents have no way to know what states are valid or what transitions are allowed. They add new states or transitions locally, fragmenting the semantic model.

**What to include:**
- Which entities have explicit state machines
- Where the canonical state definition lives (file path)
- What the valid states and transitions are (or a pointer to where they are defined)

**Example (good):**
```markdown
## State Models
- **Order lifecycle**: defined in `packages/domain/order/state-machine.ts`
  States: draft → submitted → confirmed → fulfilled → closed
  Only `domain/order/` may define new transitions.
- **Member status**: defined in `packages/domain/member/status.ts`
  States: pending → active → suspended → deleted
```

**Minimum viable version:** List the top 3–5 entities that have state/lifecycle behavior, with file paths to their canonical definitions.

---

## Dimension 7: Implicit Dependencies (Agent-Era Addition)

**What:** A declaration of global state, environment variables, mutable singletons, and side effects that exist in the module.

**Why:** Agents have no cross-session memory. A human engineer can carry "watch out for that global cache variable" knowledge between tasks; an agent cannot. Undeclared implicit dependencies are the single largest source of agent-introduced bugs that pass all explicit tests but break in production due to state interactions.

**What to include:**
- Global or module-level mutable state (singletons, caches, registries)
- Environment variables the module depends on
- Side effects of importing or initializing the module
- External service dependencies that are not obvious from type signatures

**Example (good):**
```markdown
## Implicit Dependencies
- `packages/auth/` uses a module-level token cache (`tokenCache` in `cache.ts`).
  This cache is shared across all requests in the same process.
  Do NOT create a second cache; always import from `cache.ts`.
- `apps/api/` reads `DATABASE_URL`, `REDIS_URL`, `JWT_SECRET` from environment at startup.
  Missing env vars cause silent fallback to defaults in dev; hard failure in production.
- Importing `packages/observability/` has a side effect: it registers global trace handlers.
  Import it exactly once at the application entry point.
```

**Example (bad):**
```markdown
## Dependencies
This module depends on the database and Redis.
```
The bad example tells the agent nothing it couldn't infer from the imports.

**Minimum viable version:** List the top 3 implicit dependencies that have caused bugs or confusion in the past.

---

## Audit Checklist

When auditing an AGENTS.md (or equivalent instruction file) for entropy-awareness, check:

| Dimension | Present? | Quality |
|-----------|----------|---------|
| Glossary | Yes / Partial / No | Has prohibited aliases? Covers core concepts? |
| Dependency Rules | Yes / Partial / No | Has explicit prohibitions? Covers critical paths? |
| Error Model | Yes / Partial / No | Has envelope shape? Has boundary conventions? |
| Naming Conventions | Yes / Partial / No | Covers all layers? Has file naming? |
| Doc Freshness Rules | Yes / Partial / No | Has specific "if X then update Y" rules? |
| State Model References | Yes / Partial / No | Points to canonical definitions? |
| Implicit Dependencies | Yes / Partial / No | Declares global state, env vars, side effects? |

A score of "Partial" on all seven is better than "Yes" on two and "No" on five. Breadth of coverage matters more than depth in the early stages.
