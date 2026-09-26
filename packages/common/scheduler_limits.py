"""Scheduler hard limits that ``packages/common`` must also enforce.

``services.orchestrator.scheduler`` imports ``packages.common.state_manager``, so
the state-index retention floor (#2548) cannot import the scheduler module back.
The limit lives here and the scheduler re-exports it under its historical name.
"""

from __future__ import annotations

#: Deepest discovery / replay lookback any production scheduler lane may be
#: configured with (``scheduler_config`` rejects anything larger).
MAX_LOOKBACK_HOURS = 336
