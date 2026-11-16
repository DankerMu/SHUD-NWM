## ADDED Requirements

### Requirement: High-entropy directories SHALL have scoped agent instructions

Directories with high structural or behavioral entropy SHALL have local
`AGENTS.md` files that state ownership and verification rules without expanding
the generated root instruction file.

#### Scenario: orchestrator scoped instructions exist

- **WHEN** an agent works under `services/orchestrator/`
- **THEN** a scoped instruction file SHALL define dependency direction,
  scheduler/chain facade compatibility rules, state ownership, mutation fences,
  and focused verification commands.

#### Scenario: production-closure scoped instructions exist

- **WHEN** an agent works under `services/production_closure/`
- **THEN** a scoped instruction file SHALL define lane ownership, evidence
  schema/redaction/path-safety rules, readonly boundary invariants, and focused
  verification commands.

#### Scenario: API and frontend scoped instructions exist

- **WHEN** an agent works under `apps/api/` or `apps/frontend/`
- **THEN** scoped instruction files SHALL define bootstrap/routing boundaries,
  role guard expectations, frontend live-vs-mocked evidence rules, and focused
  verification commands.

### Requirement: Glossary SHALL centralize entropy-governance language

The repository SHALL provide `openspec/glossary.md` as the canonical glossary
for recurring governance terms used by OpenSpec changes, issues, and scoped
instructions.

#### Scenario: glossary is created

- **WHEN** the glossary is introduced
- **THEN** it SHALL define at least `active entrypoint`, `legacy redirect
  alias`, `retired active-tree path`, `compatibility facade`, `lane`,
  `budget-counted finding`, `gate-eligible finding`, `current authority`, and
  `historical evidence`.

#### Scenario: scoped instructions use glossary terms

- **WHEN** scoped instruction files refer to governance concepts
- **THEN** they SHALL use glossary terms or link to the glossary instead of
  introducing local synonyms.

### Requirement: Scoped context SHALL remain fresh

Scoped agent context MUST NOT become another stale authority layer.

#### Scenario: scoped instruction coverage is checked

- **WHEN** the entropy or control-plane audit checks scoped instruction files
- **THEN** it SHALL report whether high-entropy directories have local
  instructions and whether those instructions reference current specs, runbooks,
  and verification commands.
