## REMOVED Requirements

### Requirement: Compatibility guard remains authoritative

**Reason**: F2 (#3272, #3273) deleted the programmatic installers and every `_*_COMPAT_*` table; both facades now import each name explicitly and `tests/test_facade_name_resolution.py` fails before merge if any name an owner reads or a test patches does not resolve. The compatibility inventories and the entropy-audit facade guard only pinned the deleted surface, so F3 (#3274) deletes them (#3227).
**Migration**: None. A new facade name is an explicit import or alias line in `scheduler.py` / `chain.py`, covered by the facade name-resolution test (spec `scheduler-facade-deepening`, `chain-facade-deepening`).
