# ci-contract-baseline delta

## ADDED Requirements

### Requirement: Meta-guard tree derivations MUST stay content-faithful under parse caching

The selector meta-guard suite's parse layer (`_parse_tracked`) SHALL
memoize parse results keyed by resolved file identity — the resolved
absolute path plus stat identity (mtime_ns and size) — so that
reusing parses within one suite run cannot change derivation
semantics: a working-directory change SHALL NOT alias a test-fixture
path onto a same-named repository file's cached parse, and a rewrite
of a previously parsed file SHALL be observed by subsequent parses.
(Recorded boundaries: a rewrite preserving resolved path, mtime_ns
and size is outside the cache's discrimination — unreachable under
the suite's no-tracked-mutation probe discipline; the `filename=`
argument to `ast.parse` affects only parse-time SyntaxError messages
and is not carried by the returned tree, so cache reuse cannot alter
any derivation through it.)

#### Scenario: cwd changes cannot alias fixture paths onto repository parses

- **WHEN** a test parses a tracked repository file via its
  repo-relative spelling, then chdirs into a temporary directory
  containing a different file at the same relative spelling and
  parses that spelling again
- **THEN** the second parse reflects the temporary file's content,
  not the cached repository parse

#### Scenario: rewrites of a parsed file are observed

- **WHEN** a file is parsed, rewritten with different content and a
  distinct stat identity, and parsed again
- **THEN** the second parse reflects the rewritten content
