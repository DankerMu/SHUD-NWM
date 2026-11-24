"""The one lookup through which scheduler owners observe a patch on the facade.

``services.orchestrator.scheduler`` is a monkeypatch surface.  The owner modules
that call their own functions directly (``scheduler_state_decision``,
``scheduler_state_failure``, ``scheduler_lease``) observe a patch on it only for
the names registered here, and only through :func:`facade_seam`.  A patch on any
other facade name is not observed inside these owners.

Rule (spec ``scheduler-facade-deepening``):

* Patch detection: "patched" is decided by identity against the function
  recorded by :func:`facade_seam_original` where the seam is defined.  That
  reference does not move with a patch on the facade or on any re-exporting
  module.
* Precedence: a facade binding that is callable and is not the recorded
  original is a patch and wins.  While the facade is unpatched or not loaded,
  ``fallback`` (the binding of the module that holds the call site) is used, so
  a patch on that module is observed.
* Not observed: a patch on any other module.  That covers a module that merely
  re-exports the name (the ``scheduler_state`` aggregate) and the defining
  module when it is not the call-site module.
* Unregistered names: a lookup for a name that was never registered raises
  ``LookupError`` instead of falling back.

The facade is read from ``sys.modules`` and never imported here.  This module
imports only the standard library, so any owner can import it.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from typing import Any, TypeVar

_FACADE_MODULE = "services.orchestrator.scheduler"

_F = TypeVar("_F", bound=Callable[..., Any])

_ORIGINALS: dict[str, Callable[..., Any]] = {}


def facade_seam_original(function: _F) -> _F:
    """Record ``function`` as the unpatched reference of the seam ``function.__name__``.

    Returns ``function`` unchanged.  A later registration of the same name (a
    module reload) replaces the earlier one.
    """

    _ORIGINALS[function.__name__] = function
    return function


def facade_seam(name: str, fallback: Callable[..., Any]) -> Callable[..., Any]:
    """Return the facade's patch of the seam ``name`` at call time, else ``fallback``."""

    try:
        original = _ORIGINALS[name]
    except KeyError:
        raise LookupError(f"{name!r} is not a registered scheduler facade seam") from None
    facade = sys.modules.get(_FACADE_MODULE)
    value = getattr(facade, name, None) if facade is not None else None
    if callable(value) and value is not original:
        return value
    return fallback
