## ADDED Requirements

### Requirement: DB tile cache writes are gated by a privilege probe, not by a failed write

On PostgreSQL, the MVT DB cache write path SHALL determine per engine (no further probe after the first one completes; concurrent first probes may each run), with a read-only catalog query that cannot raise for a missing table, whether the current role may INSERT and UPDATE `map.tile_cache` and `map.tile_layer`. When it may not, a cache miss SHALL send no INSERT or UPDATE to either table and SHALL fall back to the file cache with `cache_status` `miss`. When it may, the DB cache write SHALL behave as before. A probe that raises, returns no row or returns a non-boolean SHALL be treated as not writable for that engine. The role's privileges SHALL NOT be changed.

#### Scenario: Read-only display role
- **WHEN** the probe reports no write privilege and several cache misses are built
- **THEN** no INSERT or UPDATE statement targets `map.tile_layer` or `map.tile_cache`, each tile is written to the file cache, and on a cacheable engine the probe is not repeated after the first one completed

#### Scenario: Writable role
- **WHEN** the probe reports write privilege
- **THEN** the tile layer upsert and tile cache upsert are issued as before
