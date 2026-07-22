from __future__ import annotations

import re
from typing import Any, Callable, Mapping, Sequence

import httpx

from services.orchestrator.chain_types import OrchestratorError

__all__ = ("HttpSlurmGatewayClient",)


def _coerce_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return dict(model_dump(mode="json"))
    if hasattr(value, "__dict__"):
        return dict(value.__dict__)
    raise TypeError(f"Expected mapping-like Slurm payload, got {type(value).__name__}")


def _response_json_or_text(response: httpx.Response) -> dict[str, Any] | str:
    try:
        payload = response.json()
    except ValueError:
        return response.text
    return dict(payload) if isinstance(payload, Mapping) else str(payload)


def _error_code_from_response(details: dict[str, Any] | str) -> str:
    if isinstance(details, Mapping):
        value = details.get("error_code") or details.get("code")
        if value not in (None, ""):
            return str(value)
        for key in ("error", "detail"):
            nested = details.get(key)
            if isinstance(nested, Mapping):
                nested_code = _error_code_from_response(dict(nested))
                if nested_code != "SLURM_GATEWAY_ERROR":
                    return nested_code
    return "SLURM_GATEWAY_ERROR"


_PROVEN_PRE_ACCEPTANCE_REJECTION_CODES = frozenset(
    {
        "MANIFEST_VALIDATION_ERROR",
        "TEMPLATE_NOT_FOUND",
        "TEMPLATE_SECURITY_ERROR",
        "VALIDATION_ERROR",
    }
)
_SUBMIT_JOB_ID_RE = re.compile(r"^(?:\d+|mock_\d+)$")


class HttpSlurmGatewayClient:
    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 30.0,
        error_cls: type[Exception] = OrchestratorError,
        coerce_mapping: Callable[[Any], dict[str, Any]] = _coerce_mapping,
        response_json_or_text: Callable[[httpx.Response], dict[str, Any] | str] = _response_json_or_text,
        error_code_from_response: Callable[[dict[str, Any] | str], str] = _error_code_from_response,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._error_cls = error_cls
        self._coerce_mapping = coerce_mapping
        self._response_json_or_text = response_json_or_text
        self._error_code_from_response = error_code_from_response

    def submit_job(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = self._request("POST", "/api/v1/slurm/jobs", json=payload, expected=(200, 201))
        return self._validated_submit_response(response)

    def submit_job_array(
        self,
        job_type: str | Mapping[str, Any],
        cycle_id: str | None = None,
        stage_name: str | None = None,
        tasks: Sequence[Mapping[str, Any]] | None = None,
        manifest: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if isinstance(job_type, Mapping):
            payload = dict(job_type)
        else:
            payload = {"job_type": job_type}
        if cycle_id is not None:
            payload["cycle_id"] = cycle_id
        if stage_name is not None:
            payload["stage_name"] = stage_name
        if tasks is not None:
            payload["tasks"] = [dict(task) for task in tasks]
        if manifest is not None:
            payload["manifest"] = dict(manifest)
        response = self._request("POST", "/api/v1/slurm/job-arrays", json=payload, expected=(200, 201))
        return self._validated_submit_response(response)

    def get_job_status(self, job_id: str) -> dict[str, Any]:
        return self._request("GET", f"/api/v1/slurm/jobs/{job_id}", expected=(200,))

    def get_array_task_results(self, job_id: str) -> list[dict[str, Any]]:
        response = self._request("GET", f"/api/v1/slurm/jobs/{job_id}/array-tasks", expected=(200,))
        if isinstance(response, list):
            return [dict(item) for item in response]
        tasks = response.get("tasks") if isinstance(response, Mapping) else None
        if isinstance(tasks, Sequence) and not isinstance(tasks, str | bytes):
            return [dict(self._coerce_mapping(item)) for item in tasks]
        raise self._error(
            "SLURM_GATEWAY_INVALID_RESPONSE",
            "Slurm Gateway returned an invalid array task response.",
            {"response": response},
        )

    def fetch_logs(self, job_id: str) -> dict[str, Any]:
        return self._request("GET", f"/api/v1/slurm/jobs/{job_id}/logs", expected=(200,))

    def cancel_job(self, job_id: str) -> dict[str, Any]:
        return self._request("DELETE", f"/api/v1/slurm/jobs/{job_id}", expected=(200,))

    def _request(
        self,
        method: str,
        path: str,
        *,
        expected: tuple[int, ...],
        json: dict[str, Any] | None = None,
    ) -> Any:
        try:
            with httpx.Client(base_url=self.base_url, timeout=self.timeout) as client:
                response = client.request(method, path, json=json)
        except httpx.HTTPError as error:
            raise self._error(
                "SLURM_GATEWAY_UNAVAILABLE",
                f"Slurm Gateway request failed: {error}",
                submit_disposition="ambiguous" if method == "POST" else None,
            ) from error
        if response.status_code not in expected:
            details = self._response_json_or_text(response)
            code = self._error_code_from_response(details)
            disposition = None
            if method == "POST":
                disposition = "rejected" if code in _PROVEN_PRE_ACCEPTANCE_REJECTION_CODES else "ambiguous"
            raise self._error(
                code,
                f"Slurm Gateway returned HTTP {response.status_code}.",
                {"response": details},
                submit_disposition=disposition,
            )
        try:
            return response.json()
        except ValueError as error:
            raise self._error(
                "SLURM_GATEWAY_INVALID_RESPONSE",
                "Slurm Gateway returned a non-JSON success response.",
                submit_disposition="ambiguous" if method == "POST" else None,
            ) from error

    def _validated_submit_response(self, value: Any) -> dict[str, Any]:
        try:
            response = self._coerce_mapping(value)
        except (TypeError, ValueError) as error:
            raise self._error(
                "SLURM_GATEWAY_INVALID_RESPONSE",
                "Slurm Gateway returned an invalid submit response.",
                submit_disposition="ambiguous",
            ) from error
        job_id = response.get("job_id")
        if not isinstance(job_id, str) or _SUBMIT_JOB_ID_RE.fullmatch(job_id) is None:
            raise self._error(
                "SLURM_GATEWAY_INVALID_RESPONSE",
                "Slurm Gateway submit response did not contain a valid master job id.",
                {"response_fields": sorted(str(key) for key in response)},
                submit_disposition="ambiguous",
            )
        return response

    def _error(
        self,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
        *,
        submit_disposition: str | None = None,
    ) -> Exception:
        error = self._error_cls(code, message, details or {})
        if submit_disposition is not None:
            setattr(error, "submit_disposition", submit_disposition)
        return error
