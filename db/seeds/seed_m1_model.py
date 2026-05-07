"""Bootstrap the M1 demo model registry assets.

This script reuses the deterministic demo seed implementation and is intentionally
idempotent. It is split out so M1 model registration can be invoked without
remembering the broader seed module name.
"""

from __future__ import annotations

from db.seeds.seed_demo import main

if __name__ == "__main__":
    main()
