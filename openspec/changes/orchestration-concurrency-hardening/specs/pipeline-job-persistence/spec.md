## ADDED Requirements

### Requirement: Journal read caches are safe under concurrent orchestration threads sharing one repository instance

The file journal repository's read-side caches SHALL tolerate concurrent
readers and writers on a single shared repository instance without
raising or corrupting cached values, because the production scheduler
hands one repository instance to every per-cohort orchestrator and fans
them out across a thread pool: cache population, lookup, and eviction
are mutually exclusive critical sections, and taking the cache mutex
never acquires the journal write mutex inside it (single lock order).
Cache semantics are unchanged: identical keys observe identical values
and the eviction policy is untouched — only mutual exclusion is added.

#### Scenario: concurrent cohorts hammer the shared caches

WHEN multiple orchestration threads concurrently read cycle rows and
journal files through one repository instance while cache eviction is
continuously triggered
THEN no thread observes a runtime iteration error or a torn cache entry,
and every lookup returns a value equal to what a single-threaded run
would have produced

#### Scenario: single-threaded behavior is unchanged

WHEN the repository is used from a single thread
THEN cache hits, misses, and evictions behave byte-for-byte as before
the change
