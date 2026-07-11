# Scan Dimensions

Detailed methods for each entropy axis scan. Used during Phase 2 of repo-entropy-audit.

For theoretical background, see [Six Entropy Axes](../../control-plane-auditor/references/methodology/six-entropy-axes.md).

**Agent-era calibration:** Traditional proxy metrics (file line count, function length, nesting depth) have been replaced or recalibrated. We measure the actual underlying problems (mixed responsibilities, implicit dependencies, uncovered branches) rather than human cognitive proxies. See `docs/decisions/agent-era-metric-recalibration.md` (repo-level doc; not shipped with standalone skill installs).

---

## Language adaptation

Every command below is a **TypeScript/JavaScript-flavored example**. The six axes and their thresholds are language-independent — before scanning, substitute your project's file extensions and import syntax:

| Language | File glob | Import syntax to match |
|----------|-----------|------------------------|
| TypeScript/JS | `*.ts`, `*.tsx`, `*.js`, `*.mjs` | `import ... from`, `require(` |
| Python | `*.py` | `^import `, `^from ... import` |
| Go | `*.go` | `import (`, `import "` |
| Rust | `*.rs` | `^use ` |
| Java | `*.java` | `^import ` |

Swap the `--include`/`-name` globs and the import-matching patterns to fit; the scoring bands stay the same.

---

## Structure

### Dependency direction analysis

Build a module-level import graph and check for layering violations.

```bash
# List all imports grouped by source module (TypeScript/JavaScript).
# Scan the module roots detected in Phase 1b: apps/ packages/ for monorepos,
# src/ for single-app layouts (2>/dev/null skips roots that do not exist).
grep -rn "^import\|^} from\|require(" --include='*.ts' --include='*.tsx' --include='*.js' --include='*.mjs' \
  apps/ packages/ src/ 2>/dev/null | grep -v node_modules | grep -v '.test.' | grep -v '.spec.'
```

Group imports by source module → target module. Flag edges that go "upward" (e.g., packages/db importing from apps/api).

### Cycle detection

Detect import cycles. Catching every strongly connected component (SCC) needs a real graph tool; without one, catch the common pairwise (2-node) cycles directly and escalate to a tool for longer chains.

Pairwise approach (always available): for each module pair (A, B), check if A imports B and B imports A. This finds 2-cycles but not longer ones. For full SCC detection, use `madge --circular` or `dependency-cruiser` when installed; otherwise traverse the import graph manually.

```bash
# Quick circular check with madge (if installed) — point it at the module
# roots detected in Phase 1b (apps/ packages/ for monorepos, src/ otherwise)
npx madge --circular --extensions ts,tsx src/
```

### Utility directory analysis

Measure the growth and coupling of utility/shared directories.

```bash
# Count files in utility-like directories (grouped so the -o branches stay
# inside the type/name filter, and node_modules is pruned)
find . -path ./node_modules -prune -o -type f -name '*.ts' \
  \( -path '*/util/*' -o -path '*/common/*' -o -path '*/helpers/*' -o -path '*/shared/*' \) -print | wc -l

# Check what imports from these directories
grep -rn "from.*['\"].*\/util\|from.*['\"].*\/common\|from.*['\"].*\/helpers" --include='*.ts' | grep -v node_modules | wc -l
```

Flag: utility directories with > 20 files, or utility directories importing from business-logic modules.

### Responsibility mixing (replaces file size as primary metric)

File line count alone is a poor signal in agent-first development — agents read large files without difficulty. The real problem is mixed responsibilities: a file that imports from many unrelated modules is doing too many things, regardless of its length.

```bash
# Per-file import source diversity: count distinct top-level module sources
for f in $(find . -name '*.ts' -not -path '*/node_modules/*' -not -path '*/dist/*' -not -name '*.test.*'); do
  sources=$(grep "from ['\"]" "$f" 2>/dev/null | grep -Eo "from ['\"][^'\"]+" | sed 's|from ['\''"]||' | grep -Eo '^[^/]+(/[^/]+)?' | sort -u | wc -l)
  [ "$sources" -gt 5 ] && echo "$f imports from $sources distinct modules"
done
```

Flag files importing from > 5 distinct top-level modules. These files likely mix multiple responsibilities.

File line count is retained as a **secondary, informational signal** (not a red flag):

```bash
# Informational: largest files (may indicate responsibility mixing, investigate further)
find . -name '*.ts' -not -path '*/node_modules/*' -not -path '*/dist/*' -not -name '*.test.*' -not -name '*.spec.*' \
  -exec wc -l {} + | sort -rn | head -20
```

### Implicit dependencies

Functions relying on global state, mutable singletons, or undeclared side effects are harder for agents to reason about than long-but-pure functions. Agents have no cross-session memory — they cannot carry "watch out for that global variable" knowledge.

```bash
# Heuristic scan for global/mutable state
grep -rn "global\|singleton\|Singleton" --include='*.ts' | grep -v node_modules | grep -v '.test.'

# Top-level let declarations (potential mutable module state)
grep -rn "^let \|^export let " --include='*.ts' | grep -v node_modules | grep -v '.test.'

# Side-effect imports (import without binding)
grep -rn "^import ['\"]" --include='*.ts' | grep -v node_modules
```

Flag: modules with > 3 mutable state declarations or side-effect imports.

---

## Semantics

### Naming diversity analysis

For core domain concepts, count how many distinct identifier names are used. The [Six Entropy Axes](../../control-plane-auditor/references/methodology/six-entropy-axes.md) framework phrases this as "cluster all exported identifiers by embedding similarity" — but a single agent session has no embedding-clustering step. The practical method is the grep-based variant below: enumerate candidate identifiers and group them by hand against the glossary.

```bash
# Example: find all identifiers containing "user" or "member" (adjust per project glossary)
grep -rn "User\|user\|Member\|member\|Account\|account" --include='*.ts' -l | head -30
```

Approach:
1. Identify the project's core domain concepts (from glossary if available, or from the most frequent nouns in type names)
2. For each concept, search for all identifier variants
3. Count unique variants per concept

Score: concepts with ≥ 3 naming variants are high semantic entropy (🔴 in the heatmap). Concepts with a single canonical name are low; 1-2 variants are medium (🟡). See [Heatmap Format § Semantics](heatmap-format.md#semantics).

### State definition scatter

```bash
# Find all status/state/phase enum-like definitions
grep -rn "enum.*Status\|enum.*State\|enum.*Phase\|type.*Status\|type.*State" --include='*.ts' \
  | grep -v node_modules | grep -v '.test.'
```

Count how many files define status/state enums for each domain entity. If the same entity's state is defined in > 1 file, that is a scatter signal.

### Glossary coverage

If a glossary exists (canonically `openspec/glossary.md`, otherwise AGENTS.md or another dedicated file):
- List all glossary terms
- For each, search the codebase for the canonical name vs prohibited aliases
- Coverage = (terms where canonical name is dominant) / (total terms)

### Pattern contagion risk (agent-era metric)

Agents mechanically replicate patterns they find in the repository. A bad pattern that appears in 5 files is far more dangerous than one that appears once, because it will be treated as "the project standard" by future agent sessions.

Approach:
1. Identify known-bad patterns (from cleanup backlog, TODO/FIXME markers, deprecated annotations, or quality docs)
2. For each bad pattern, count occurrences across the codebase
3. Check recent commits: is this pattern still being replicated?
4. Contagion risk: rank by occurrence count. Break ties and raise priority using recency — flag any pattern replicated within the last N commits (e.g., the last 30 days) as "actively spreading". Recency is a flag/tiebreaker, not a multiplier.

```bash
# Find deprecated/flagged patterns and count occurrences
grep -rn "@deprecated\|DEPRECATED\|LEGACY\|HACK" --include='*.ts' | grep -v node_modules

# Check whether flagged patterns were replicated recently (the "actively
# spreading" recency window — adjust the last-N-days value as needed)
git log --since="30 days ago" --oneline -- $(grep -rl "@deprecated" --include='*.ts' | head -10)
```

Score: patterns with > 3 occurrences are high contagion risk (ranked by count). Those also replicated within the recency window (last ~30 days / last N commits) are flagged "actively spreading" and handled first, even at equal occurrence counts.

---

## Behavior

### Error handling pattern diversity

```bash
# Classify error handling approaches
grep -rn "catch[[:space:]]*(" --include='*.ts' | wc -l  # try-catch count (POSIX class; portable on BSD/macOS + GNU)
grep -rn "\.catch(" --include='*.ts' | wc -l            # promise catch count
grep -rn "Result<\|Either<\|Ok(\|Err(" --include='*.ts' | wc -l  # Result type count
grep -rn "return null\|return undefined" --include='*.ts' | wc -l # null return count
```

Score by diversity: if > 2 distinct error handling patterns coexist in the same layer, behavioral entropy is elevated.

### Error envelope analysis

For API layers specifically:

```bash
# Find error response shapes
grep -rn "status\|statusCode\|HttpStatus\|BadRequest\|NotFound\|Unauthorized" --include='*.ts' \
  -l | grep -i "controller\|handler\|route\|resolver"
```

Read a sample of error responses. Count distinct envelope shapes.

### Boundary behavior consistency

Sample 5-10 functions that handle "no data" or "invalid input" cases. Check whether they consistently return `null`, `[]`, throw, or use a Result type. Mixed approaches = high behavioral entropy.

### Type coverage (elevated weight in agent-era)

Type signatures are the primary channel through which agents understand function contracts. `any`, `unknown`, and untyped parameters are not just "lazy" — they actively hide constraints from agents.

```bash
# Count any/unknown usage
grep -rnE "(: any|: unknown|as any)([^[:alnum:]_]|$)" --include='*.ts' | grep -v node_modules | grep -v '.test.' | wc -l  # -E + POSIX class replaces GNU \b/\|

# Count total type annotations (rough proxy for type coverage)
grep -rn ": [A-Z]" --include='*.ts' | grep -v node_modules | wc -l
```

Score: if `any` usage exceeds 5% of type annotations, behavioral entropy is elevated. Type coverage should be weighted higher than in human-era assessments because it directly affects agent comprehension.

---

## Context

### Instruction file coverage

```bash
# Count critical directories (containing > 5 source files)
find . -type d -not -path '*/node_modules/*' -not -path '*/dist/*' -not -path '*/.git/*' \
  -exec sh -c 'count=$(find "$1" -maxdepth 1 -name "*.ts" -o -name "*.tsx" | wc -l); [ "$count" -gt 5 ] && echo "$1 ($count files)"' _ {} \;

# Count which have instruction files
find . -name "AGENTS.md" -o -name "CLAUDE.md" | sort
```

Coverage = (critical directories with instruction files) / (total critical directories).

### Documentation freshness (with instruction file priority)

Instruction files (AGENTS.md, CLAUDE.md) have a much higher impact on agent behavior than general documentation. A stale AGENTS.md will actively mislead agents into following outdated rules. Weigh instruction file freshness 2-3x higher than general doc freshness.

```bash
# Instruction file freshness (HIGH PRIORITY)
for f in $(find . -name "AGENTS.md" -o -name "CLAUDE.md" | grep -v node_modules); do
  file_date=$(git log -1 --format=%ci "$f" 2>/dev/null)
  dir=$(dirname "$f")
  code_date=$(git log -1 --format=%ci -- "$dir" 2>/dev/null)
  echo "INSTRUCTION: $f  doc: $file_date  code: $code_date"
done

# General doc freshness (NORMAL PRIORITY)
for doc in $(find docs/ -name '*.md' 2>/dev/null); do
  doc_date=$(git log -1 --format=%ci "$doc" 2>/dev/null)
  echo "DOC: $doc  last: $doc_date"
done
```

Flag: instruction files not modified in > 3 months while corresponding code has changed (stricter than the 6-month threshold for general docs).

### Dead reference detection

Resolve each relative Markdown link against the directory of the file that contains it (not the current working directory), and cover nested `AGENTS.md`/`CLAUDE.md` files, not just the repo root.

```bash
# Collect instruction files (nested included) + docs, then check each .md link
# relative to the containing file's own directory. Portable on BSD/macOS + GNU.
files="$(find . \( -name AGENTS.md -o -name CLAUDE.md \) -not -path '*/node_modules/*'; find docs -name '*.md' 2>/dev/null)"
for file in $files; do
  dir=$(dirname "$file")
  grep -Eo '\]\([^)]+\)' "$file" 2>/dev/null | sed -E 's/^\]\(//; s/\)$//; s/#.*$//' | while read -r link; do
    case "$link" in
      ''|/*|http://*|https://*) continue ;;   # skip empty, absolute, and URL links
      *.md) [ -f "$dir/$link" ] || echo "DEAD: $file -> $link" ;;
    esac
  done
done
```

---

## Protocol

### Instruction file template compliance

Compare all module-level instruction files for structural consistency:
- Do they use the same section headings?
- Do they cover the same topics?
- Are verification commands present and consistent?

Score: fraction of module AGENTS.md files that follow the dominant template pattern.

### CI gate consistency

```bash
# List all CI workflow files
ls .github/workflows/ 2>/dev/null

# For each, extract the main gate steps
grep -l "test\|lint\|typecheck\|build" .github/workflows/*.yml 2>/dev/null
```

Check whether all PR-facing workflows enforce the same gates (lint, typecheck, test, build).

---

## Control

### Rule enforcement ratio

1. List all documented rules (from instruction files, ADRs, architecture docs)
2. For each, check if there is a corresponding lint rule, test, guard, or CI check
3. Ratio = (enforced rules) / (documented rules)

### Cleanup backlog

```bash
# Count open cleanup-related items
grep -rn "TODO\|FIXME\|HACK\|@deprecated" --include='*.ts' | grep -v node_modules | wc -l
```

Also check for dedicated cleanup tracking (issues labeled tech-debt, cleanup backlog docs).

### Health check recency

Check if doctor/health-check scripts exist and when they last ran:

```bash
# Find health check scripts
find . -name 'doctor*' -o -name 'health*' -o -name 'validate*' | grep -v node_modules
```
