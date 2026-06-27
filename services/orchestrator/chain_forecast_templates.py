from __future__ import annotations

from pathlib import Path

from services.orchestrator import chain as _chain

AnalysisRunContext = _chain.AnalysisRunContext
ForecastRunContext = _chain.ForecastRunContext
OrchestratorError = _chain.OrchestratorError
StageDefinition = _chain.StageDefinition
_SAFE_AREA_RE = _chain._SAFE_AREA_RE
_SAFE_ID_RE = _chain._SAFE_ID_RE
__file__ = _chain.__file__
format_cycle_time = _chain.format_cycle_time


def _format_time(*args, **kwargs):
    return getattr(_chain, "_format_time")(*args, **kwargs)


def _template_export_lines(*args, **kwargs):
    return getattr(_chain, "_template_export_lines")(*args, **kwargs)


def render_stage_template(self, stage: StageDefinition, context: ForecastRunContext | AnalysisRunContext) -> str:
    template_path = Path(self.config.templates_dir) / stage.template_name
    if not template_path.exists():
        repo_template_path = Path(__file__).resolve().parents[2] / "infra" / "sbatch" / stage.template_name
        if repo_template_path.exists():
            template_path = repo_template_path
        else:
            raise OrchestratorError("SBATCH_TEMPLATE_MISSING", f"Missing sbatch template: {template_path.name}")
    for label, val in [
        ("source_id", context.source_id),
        ("model_id", context.model_id),
        ("run_id", context.run_id),
        ("basin_version_id", context.basin_version_id),
        ("river_network_version_id", context.river_network_version_id),
    ]:
        if not _SAFE_ID_RE.match(val):
            raise OrchestratorError("UNSAFE_TEMPLATE_PARAM", f"{label} contains unsafe characters: {val!r}")
    if context.basin_id and not _SAFE_ID_RE.match(context.basin_id):
        raise OrchestratorError("UNSAFE_TEMPLATE_PARAM", f"basin_id unsafe: {context.basin_id!r}")
    if hasattr(self.config, "era5_area") and not _SAFE_AREA_RE.match(self.config.era5_area):
        raise OrchestratorError("UNSAFE_TEMPLATE_PARAM", f"era5_area unsafe: {self.config.era5_area!r}")
    if isinstance(context, AnalysisRunContext):
        self._validate_analysis_template_context(context)
    run_manifest_path = self._workspace_path("runs", context.run_id, "input", "manifest.json")
    template_context = {
        "source_id": context.source_id,
        "source_id_lower": context.source_id.lower(),
        "cycle_time": format_cycle_time(context.cycle_time),
        "cycle_time_iso": _format_time(context.cycle_time),
        "model_id": context.model_id,
        "basin_id": context.basin_id or "",
        "basin_version_id": context.basin_version_id,
        "river_network_version_id": context.river_network_version_id,
        "run_id": context.run_id,
        "stage_name": stage.stage,
        "job_type": stage.job_type,
        "workspace_dir": str(Path(self.config.workspace_root)),
        "object_store_root": str(Path(self.config.object_store_root)),
        "object_store_prefix": self.config.object_store_prefix,
        "run_manifest_path": str(run_manifest_path),
        "run_type": getattr(
            context,
            "run_type",
            "analysis" if isinstance(context, AnalysisRunContext) else "forecast",
        ),
        "analysis_date": context.start_time.strftime("%Y-%m-%d"),
        "analysis_start_time": _format_time(context.start_time),
        "analysis_end_time": _format_time(context.end_time),
        "analysis_date_range": f"{_format_time(context.start_time)}/{_format_time(context.end_time)}",
        "era5_area": self.config.era5_area,
        "cycle_id": context.cycle_id,
        "partition": "compute",
        "nodes": 1,
        "ntasks": 1,
        "cpus_per_task": 1,
        "memory_gb": 1,
        "walltime": "01:00:00",
        "max_concurrent": 1,
        "shud_threads": 1,
        "manifest_index_path": "",
    }
    template_context["export_lines"] = _template_export_lines(template_context)
    template_text = template_path.read_text(encoding="utf-8")
    if "{{" in template_text or "{%" in template_text:
        from jinja2 import StrictUndefined
        from jinja2.sandbox import SandboxedEnvironment

        return (
            SandboxedEnvironment(undefined=StrictUndefined, autoescape=False)
            .from_string(template_text)
            .render(**template_context)
        )
    return template_text.format(**template_context)
