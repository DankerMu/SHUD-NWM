# ci-contract-baseline delta

## MODIFIED Requirements

### Requirement: Meta-guard tree derivations MUST stay content-faithful under parse caching

The selector meta-guard suite's parse layer (`_parse_tracked`) SHALL
memoize parse results keyed by resolved file identity — the resolved
absolute path plus stat identity (mtime_ns and size) — so that
reusing parses within one suite run cannot change derivation
semantics: a working-directory change SHALL NOT alias a test-fixture
path onto a same-named repository file's cached parse, and a rewrite
of a previously parsed file SHALL be observed by subsequent parses.
Because cache hits hand every consumer the SAME `ast.Module`
instance, the suite SHALL mechanically guard its own source against
tree-mutation idioms — attribute stores and deletes in any
assignment form, subscript stores and deletes over an attribute
base, classes with a direct `NodeTransformer` base, calls to
`fix_missing_locations`/`copy_location`/`increment_lineno`,
bare-name `setattr`/`delattr` calls, and mutating list-method calls
on an attribute receiver — keeping the shared-instance premise a
standing assertion rather than a one-time audit, with the scan's
red and no-false-positive arms landed as standing tests.
(Recorded boundaries: a rewrite preserving resolved path, mtime_ns
and size is outside the cache's discrimination — unreachable under
the suite's no-tracked-mutation probe discipline; the `filename=`
argument to `ast.parse` affects only parse-time SyntaxError messages
and is not carried by the returned tree, so cache reuse cannot alter
any derivation through it; the mutation scan matches `setattr` only
as a bare name — `monkeypatch.setattr` is a legitimate attribute
callee in this module — so attribute-callee `setattr` aliases,
out-of-module helpers, and indirect `NodeTransformer` subclasses
evade it, a recorded tripwire limit.)

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

#### Scenario: tree-mutation idioms are mechanically barred

- **WHEN** a change to the meta-guard suite introduces an attribute
  store or delete (any assignment form), a subscript store or delete
  over an attribute base, a class with a direct `NodeTransformer`
  base, a call to
  `fix_missing_locations`/`copy_location`/`increment_lineno`, a
  bare-name `setattr`/`delattr` call, or a mutating list-method call
  on an attribute receiver
- **THEN** the shared-AST mutation guard fails, naming the offending
  construct and line
