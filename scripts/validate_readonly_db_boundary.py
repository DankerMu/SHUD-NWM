from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from services.production_closure.readonly_db_validation import main as validation_main

    return validation_main()


if __name__ == "__main__":
    raise SystemExit(main())
