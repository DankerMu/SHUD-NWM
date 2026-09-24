"""The ``_ok()`` success envelope and the pass-through base model (#2348)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class OpenModel(BaseModel):
    """A response object whose undeclared keys pass through unchanged.

    ``extra="allow"`` keeps every key the handler returns, so a model that
    declares fewer keys than the handler writes cannot silently drop one; the
    declared fields add the runtime shape check.
    """

    model_config = ConfigDict(extra="allow")


class OkEnvelope(OpenModel):
    """``{request_id, status, data}`` as built by each route module's ``_ok``.

    Subclasses declare ``data``. Conditional siblings (``auth_policy_decisions``
    from ``models._ok`` / ``pipeline._ok``) pass through as extras, so they stay
    absent when the handler omits them. The published 200 body is
    ``allOf[SuccessEnvelope, {data}]`` (``openapi_patching_response_models``);
    these envelope classes never reach the published components.
    """

    request_id: str
    status: str
