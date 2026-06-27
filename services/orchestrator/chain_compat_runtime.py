# ruff: noqa: E402,E501,F401,F821,I001
from __future__ import annotations

from services.orchestrator import chain as _chain

globals().update({name: value for name, value in _chain.__dict__.items() if name.startswith("_CHAIN_")})


def install_chain_runtime_compat() -> dict[str, object]:
    _CHAIN_STAGE_EXECUTION_COMPAT_FACADE_MISSING = tuple(
        (
            name
            for name in _CHAIN_STAGE_EXECUTION_COMPAT_FORWARDER_NAMES
            if not callable(getattr(_chain.ForecastOrchestrator, name, None))
        )
    )
    if _CHAIN_STAGE_EXECUTION_COMPAT_FACADE_MISSING:
        raise RuntimeError(
            f"chain stage execution compatibility forwarders missing from facade: {', '.join(_CHAIN_STAGE_EXECUTION_COMPAT_FACADE_MISSING)}"
        )
    _CHAIN_RESERVATION_COMPAT_METHOD_FACADE_MISSING = tuple(
        (
            name
            for name in _CHAIN_RESERVATION_COMPAT_METHOD_FORWARDER_NAMES
            if not callable(getattr(_chain.ForecastOrchestrator, name, None))
        )
    )
    if _CHAIN_RESERVATION_COMPAT_METHOD_FACADE_MISSING:
        raise RuntimeError(
            f"chain reservation compatibility methods missing from facade: {', '.join(_CHAIN_RESERVATION_COMPAT_METHOD_FACADE_MISSING)}"
        )
    _CHAIN_RESERVATION_COMPAT_OWNER_METHOD_FORWARDERS = _chain.MappingProxyType(
        {
            facade_name: getattr(_chain.reservation, owner_name)
            for facade_name, owner_name in zip(
                _CHAIN_RESERVATION_COMPAT_OWNER_METHOD_FORWARDER_NAMES,
                _CHAIN_RESERVATION_COMPAT_OWNER_METHOD_OWNER_FUNCTION_NAMES,
                strict=True,
            )
        }
    )
    _chain_retry_init_code = _chain.ForecastOrchestrator.__init__.__code__
    _chain_retry_init_param_names = _chain_retry_init_code.co_varnames[
        : _chain_retry_init_code.co_argcount + _chain_retry_init_code.co_kwonlyargcount
    ]
    _CHAIN_RETRY_COMPAT_CONSTRUCTOR_PARAM_MISSING = tuple(
        (name for name in _CHAIN_RETRY_COMPAT_CONSTRUCTOR_PARAM_NAMES if name not in _chain_retry_init_param_names)
    )
    _CHAIN_RETRY_COMPAT_CONSTRUCTOR_CONFIG_DRIFT = tuple(
        (name for name in _CHAIN_RETRY_COMPAT_INSTANCE_CONFIG_NAMES if name not in _chain_retry_init_code.co_names)
    )
    if _CHAIN_RETRY_COMPAT_CONSTRUCTOR_PARAM_MISSING:
        raise RuntimeError(
            f"chain retry constructor parameters missing from facade: {', '.join(_CHAIN_RETRY_COMPAT_CONSTRUCTOR_PARAM_MISSING)}"
        )
    if _CHAIN_RETRY_COMPAT_CONSTRUCTOR_CONFIG_DRIFT:
        raise RuntimeError(
            f"chain retry constructor config binding drifted from facade: {', '.join(_CHAIN_RETRY_COMPAT_CONSTRUCTOR_CONFIG_DRIFT)}"
        )
    _CHAIN_RETRY_COMPAT_LOCAL_METHOD_MISSING = tuple(
        (
            name
            for name in _CHAIN_RETRY_COMPAT_LOCAL_METHOD_NAMES
            if not callable(getattr(_chain.ForecastOrchestrator, name, None))
        )
    )
    if _CHAIN_RETRY_COMPAT_LOCAL_METHOD_MISSING:
        raise RuntimeError(
            f"chain retry local bridge methods missing from facade: {', '.join(_CHAIN_RETRY_COMPAT_LOCAL_METHOD_MISSING)}"
        )
    _CHAIN_RETRY_COMPAT_CHAIN_LOCAL_METHODS = _chain.MappingProxyType(
        {name: getattr(_chain.ForecastOrchestrator, name) for name in _CHAIN_RETRY_COMPAT_LOCAL_METHOD_NAMES}
    )
    del _chain_retry_init_code, _chain_retry_init_param_names
    AnalysisOrchestrator = _chain.importlib.import_module(
        "services.orchestrator.chain_analysis_orchestrator"
    ).AnalysisOrchestrator
    AnalysisOrchestrator.__module__ = _chain.__name__
    _chain.__dict__["AnalysisOrchestrator"] = AnalysisOrchestrator
    _CHAIN_MANIFEST_COMPAT_FORECAST_METHOD_FACADE_MISSING = tuple(
        (
            name
            for name in _CHAIN_MANIFEST_COMPAT_FORECAST_METHOD_FORWARDER_NAMES
            if not callable(getattr(_chain.ForecastOrchestrator, name, None))
        )
    )
    if _CHAIN_MANIFEST_COMPAT_FORECAST_METHOD_FACADE_MISSING:
        raise RuntimeError(
            f"chain manifest compatibility forecast methods missing from facade: {', '.join(_CHAIN_MANIFEST_COMPAT_FORECAST_METHOD_FACADE_MISSING)}"
        )
    _CHAIN_MANIFEST_COMPAT_ANALYSIS_METHOD_FACADE_MISSING = tuple(
        (
            name
            for name in _CHAIN_MANIFEST_COMPAT_ANALYSIS_METHOD_FORWARDER_NAMES
            if not callable(getattr(AnalysisOrchestrator, name, None))
        )
    )
    if _CHAIN_MANIFEST_COMPAT_ANALYSIS_METHOD_FACADE_MISSING:
        raise RuntimeError(
            f"chain manifest compatibility analysis methods missing from facade: {', '.join(_CHAIN_MANIFEST_COMPAT_ANALYSIS_METHOD_FACADE_MISSING)}"
        )
    _CHAIN_MANIFEST_COMPAT_FORECAST_METHOD_FORWARDERS = _chain.MappingProxyType(
        {
            facade_name: getattr(_chain.chain_manifests, owner_name)
            for facade_name, owner_name in zip(
                _CHAIN_MANIFEST_COMPAT_FORECAST_METHOD_FORWARDER_NAMES,
                _CHAIN_MANIFEST_COMPAT_FORECAST_METHOD_OWNER_FUNCTION_NAMES,
                strict=True,
            )
        }
    )
    _CHAIN_MANIFEST_COMPAT_ANALYSIS_METHOD_FORWARDERS = _chain.MappingProxyType(
        {
            facade_name: getattr(_chain.chain_manifests, owner_name)
            for facade_name, owner_name in zip(
                _CHAIN_MANIFEST_COMPAT_ANALYSIS_METHOD_FORWARDER_NAMES,
                _CHAIN_MANIFEST_COMPAT_ANALYSIS_METHOD_OWNER_FUNCTION_NAMES,
                strict=True,
            )
        }
    )
    _CHAIN_MANIFEST_COMPAT_DEPENDENCY_BINDING_DRIFT = tuple(
        (
            f"{binding_owner}.{field}"
            for binding_owner, bindings in _CHAIN_MANIFEST_COMPAT_DEPENDENCY_BINDINGS.items()
            for field, facade_name in bindings
            if not callable(_chain.__dict__.get(facade_name))
        )
    )
    if _CHAIN_MANIFEST_COMPAT_DEPENDENCY_BINDING_DRIFT:
        raise RuntimeError(
            f"chain manifest compatibility dependency bindings missing from facade: {', '.join(_CHAIN_MANIFEST_COMPAT_DEPENDENCY_BINDING_DRIFT)}"
        )
    from services.orchestrator.chain_repository import PsycopgOrchestratorRepository

    PsycopgOrchestratorRepository.__module__ = _chain.__name__
    _chain.__dict__["PsycopgOrchestratorRepository"] = PsycopgOrchestratorRepository
    _CHAIN_PERSISTENCE_REPOSITORY_COMPAT_LOCAL_REPOSITORY_MISSING = tuple(
        (
            name
            for name in _CHAIN_PERSISTENCE_REPOSITORY_COMPAT_LOCAL_REPOSITORY_NAMES
            if not isinstance(_chain.__dict__.get(name), type)
        )
    )
    if _CHAIN_PERSISTENCE_REPOSITORY_COMPAT_LOCAL_REPOSITORY_MISSING:
        raise RuntimeError(
            f"chain persistence repository local classes missing from facade: {', '.join(_CHAIN_PERSISTENCE_REPOSITORY_COMPAT_LOCAL_REPOSITORY_MISSING)}"
        )
    _CHAIN_PERSISTENCE_REPOSITORY_COMPAT_FORWARDER_CLASSIFICATION_DRIFT = tuple(
        (
            name
            for name, classification in _CHAIN_PERSISTENCE_REPOSITORY_COMPAT_CHAIN_LOCAL_CLASSIFICATIONS.items()
            if "forwarder" in classification or classification.startswith("owner-")
        )
    )
    if _CHAIN_PERSISTENCE_REPOSITORY_COMPAT_FORWARDER_CLASSIFICATION_DRIFT:
        raise RuntimeError(
            f"chain persistence repository classifications must remain chain-local, not pure owner forwarders: {', '.join(_CHAIN_PERSISTENCE_REPOSITORY_COMPAT_FORWARDER_CLASSIFICATION_DRIFT)}"
        )
    _CHAIN_PERSISTENCE_REPOSITORY_COMPAT_LOCAL_REPOSITORIES = _chain.MappingProxyType(
        {name: _chain.__dict__[name] for name in _CHAIN_PERSISTENCE_REPOSITORY_COMPAT_LOCAL_REPOSITORY_NAMES}
    )
    _CHAIN_PERSISTENCE_REPOSITORY_COMPAT_LOCAL_REPOSITORY_OWNER_DRIFT = tuple(
        (
            name
            for name, repository_type in _CHAIN_PERSISTENCE_REPOSITORY_COMPAT_LOCAL_REPOSITORIES.items()
            if getattr(repository_type, "__module__", None) != _chain.__name__
        )
    )
    if _CHAIN_PERSISTENCE_REPOSITORY_COMPAT_LOCAL_REPOSITORY_OWNER_DRIFT:
        raise RuntimeError(
            f"chain persistence repository classes must be defined in the chain facade until extraction: {', '.join(_CHAIN_PERSISTENCE_REPOSITORY_COMPAT_LOCAL_REPOSITORY_OWNER_DRIFT)}"
        )
    _CHAIN_PERSISTENCE_REPOSITORY_COMPAT_PROTOCOL_METHOD_MISSING = tuple(
        (
            name
            for name in _CHAIN_PERSISTENCE_REPOSITORY_COMPAT_PROTOCOL_METHOD_NAMES
            if not callable(getattr(_chain.OrchestratorRepository, name, None))
        )
    )
    if _CHAIN_PERSISTENCE_REPOSITORY_COMPAT_PROTOCOL_METHOD_MISSING:
        raise RuntimeError(
            f"chain persistence repository protocol methods missing from local protocol: {', '.join(_CHAIN_PERSISTENCE_REPOSITORY_COMPAT_PROTOCOL_METHOD_MISSING)}"
        )
    _CHAIN_PERSISTENCE_REPOSITORY_COMPAT_IMPLEMENTATION_METHOD_MISSING = tuple(
        (
            name
            for name in _CHAIN_PERSISTENCE_REPOSITORY_COMPAT_IMPLEMENTATION_METHOD_NAMES
            if not callable(getattr(PsycopgOrchestratorRepository, name, None))
        )
    )
    if _CHAIN_PERSISTENCE_REPOSITORY_COMPAT_IMPLEMENTATION_METHOD_MISSING:
        raise RuntimeError(
            f"chain persistence repository implementation methods missing from local implementation: {', '.join(_CHAIN_PERSISTENCE_REPOSITORY_COMPAT_IMPLEMENTATION_METHOD_MISSING)}"
        )
    _CHAIN_PERSISTENCE_REPOSITORY_COMPAT_PROTOCOL_METHODS = _chain.MappingProxyType(
        {
            name: getattr(_chain.OrchestratorRepository, name)
            for name in _CHAIN_PERSISTENCE_REPOSITORY_COMPAT_PROTOCOL_METHOD_NAMES
        }
    )
    _CHAIN_PERSISTENCE_REPOSITORY_COMPAT_IMPLEMENTATION_METHODS = _chain.MappingProxyType(
        {
            name: getattr(PsycopgOrchestratorRepository, name)
            for name in _CHAIN_PERSISTENCE_REPOSITORY_COMPAT_IMPLEMENTATION_METHOD_NAMES
        }
    )

    def _utcnow() -> _chain.datetime:
        return _chain.datetime.now(_chain.UTC)

    def _retry_service_from_env() -> _chain.RetryService | None:
        database_url = _chain.os.getenv("DATABASE_URL", "").strip()
        if not database_url:
            return None
        from sqlalchemy import create_engine
        from sqlalchemy.orm import Session

        engine = create_engine(database_url, future=True)
        session = Session(engine)
        store = _chain.PipelineStore(session)
        return _chain.RetryService(store, _chain.RetryConfig.from_settings(_chain.SlurmGatewaySettings()))

    _chain.__dict__["_utcnow"] = _utcnow
    _chain.__dict__["_retry_service_from_env"] = _retry_service_from_env
    _CHAIN_RETRY_COMPAT_LOCAL_FACTORY_MISSING = tuple(
        (name for name in _CHAIN_RETRY_COMPAT_LOCAL_FACTORY_NAMES if not callable(_chain.__dict__.get(name)))
    )
    if _CHAIN_RETRY_COMPAT_LOCAL_FACTORY_MISSING:
        raise RuntimeError(
            f"chain retry local factories missing from facade: {', '.join(_CHAIN_RETRY_COMPAT_LOCAL_FACTORY_MISSING)}"
        )
    _CHAIN_RETRY_COMPAT_CHAIN_LOCAL_FACTORIES = _chain.MappingProxyType(
        {name: _chain.__dict__[name] for name in _CHAIN_RETRY_COMPAT_LOCAL_FACTORY_NAMES}
    )
    _CHAIN_WORKSPACE_LOG_COMPAT_METHOD_FACADE_MISSING = tuple(
        (
            name
            for name in _CHAIN_WORKSPACE_LOG_COMPAT_METHOD_FORWARDER_NAMES
            if not callable(getattr(_chain.ForecastOrchestrator, name, None))
        )
    )
    _CHAIN_WORKSPACE_LOG_COMPAT_TOP_LEVEL_FACADE_MISSING = tuple(
        (
            name
            for name in _CHAIN_WORKSPACE_LOG_COMPAT_TOP_LEVEL_FORWARDER_NAMES
            if not callable(_chain.__dict__.get(name))
        )
    )
    if _CHAIN_WORKSPACE_LOG_COMPAT_METHOD_FACADE_MISSING:
        raise RuntimeError(
            f"chain workspace/log compatibility methods missing from facade: {', '.join(_CHAIN_WORKSPACE_LOG_COMPAT_METHOD_FACADE_MISSING)}"
        )
    if _CHAIN_WORKSPACE_LOG_COMPAT_TOP_LEVEL_FACADE_MISSING:
        raise RuntimeError(
            f"chain workspace/log compatibility top-level helpers missing from facade: {', '.join(_CHAIN_WORKSPACE_LOG_COMPAT_TOP_LEVEL_FACADE_MISSING)}"
        )
    _CHAIN_WORKSPACE_LOG_COMPAT_METHOD_FORWARDERS = _chain.MappingProxyType(
        {
            facade_name: getattr(_chain.chain_workspace, owner_name)
            for facade_name, owner_name in zip(
                _CHAIN_WORKSPACE_LOG_COMPAT_METHOD_FORWARDER_NAMES,
                _CHAIN_WORKSPACE_LOG_COMPAT_METHOD_OWNER_FUNCTION_NAMES,
                strict=True,
            )
        }
    )
    _CHAIN_WORKSPACE_LOG_COMPAT_TOP_LEVEL_FORWARDERS = _chain.MappingProxyType(
        {
            facade_name: getattr(_chain.chain_workspace, owner_name)
            for facade_name, owner_name in zip(
                _CHAIN_WORKSPACE_LOG_COMPAT_TOP_LEVEL_FORWARDER_NAMES,
                _CHAIN_WORKSPACE_LOG_COMPAT_TOP_LEVEL_OWNER_FUNCTION_NAMES,
                strict=True,
            )
        }
    )
    _CHAIN_ARRAY_ACCOUNTING_COMPAT_TOP_LEVEL_FACADE_MISSING = tuple(
        (
            name
            for name in (
                *_CHAIN_ARRAY_ACCOUNTING_COMPAT_TOP_LEVEL_FORWARDER_NAMES,
                *_CHAIN_ARRAY_ACCOUNTING_COMPAT_LOCAL_BINDING_NAMES,
            )
            if not callable(_chain.__dict__.get(name))
        )
    )
    if _CHAIN_ARRAY_ACCOUNTING_COMPAT_TOP_LEVEL_FACADE_MISSING:
        raise RuntimeError(
            f"chain array accounting compatibility names missing from facade: {', '.join(_CHAIN_ARRAY_ACCOUNTING_COMPAT_TOP_LEVEL_FACADE_MISSING)}"
        )
    _CHAIN_ARRAY_ACCOUNTING_COMPAT_METHOD_FACADE_MISSING = tuple(
        (
            name
            for name in _CHAIN_ARRAY_ACCOUNTING_COMPAT_METHOD_FORWARDER_NAMES
            if not callable(getattr(_chain.ForecastOrchestrator, name, None))
        )
    )
    if _CHAIN_ARRAY_ACCOUNTING_COMPAT_METHOD_FACADE_MISSING:
        raise RuntimeError(
            f"chain array accounting compatibility methods missing from facade: {', '.join(_CHAIN_ARRAY_ACCOUNTING_COMPAT_METHOD_FACADE_MISSING)}"
        )
    _CHAIN_ARRAY_ACCOUNTING_COMPAT_TOP_LEVEL_FORWARDERS = _chain.MappingProxyType(
        {
            facade_name: getattr(_chain.chain_array_accounting, owner_name)
            for facade_name, owner_name in zip(
                _CHAIN_ARRAY_ACCOUNTING_COMPAT_TOP_LEVEL_FORWARDER_NAMES,
                _CHAIN_ARRAY_ACCOUNTING_COMPAT_TOP_LEVEL_OWNER_FUNCTION_NAMES,
                strict=True,
            )
        }
    )
    _CHAIN_ARRAY_ACCOUNTING_COMPAT_METHOD_FORWARDERS = _chain.MappingProxyType(
        {
            facade_name: getattr(_chain.chain_array_accounting, owner_name)
            for facade_name, owner_name in zip(
                _CHAIN_ARRAY_ACCOUNTING_COMPAT_METHOD_FORWARDER_NAMES,
                _CHAIN_ARRAY_ACCOUNTING_COMPAT_METHOD_OWNER_FUNCTION_NAMES,
                strict=True,
            )
        }
    )
    _chain_array_accounting_deps = _chain._array_accounting_dependencies()
    _CHAIN_ARRAY_ACCOUNTING_COMPAT_DEPENDENCY_BINDING_DRIFT = tuple(
        (
            field
            for field, facade_name in _CHAIN_ARRAY_ACCOUNTING_COMPAT_DEPENDENCY_BINDINGS
            if getattr(_chain_array_accounting_deps, field) is not _chain.__dict__[facade_name]
        )
    )
    if _CHAIN_ARRAY_ACCOUNTING_COMPAT_DEPENDENCY_BINDING_DRIFT:
        raise RuntimeError(
            f"chain array accounting dependency bindings drifted from legacy facade: {', '.join(_CHAIN_ARRAY_ACCOUNTING_COMPAT_DEPENDENCY_BINDING_DRIFT)}"
        )
    del _chain_array_accounting_deps
    _CHAIN_WORKER_ADAPTER_COMPAT_CANONICAL_ALIAS_NAMES = ("evaluate_canonical_readiness", "expected_converter_version")
    _CHAIN_WORKER_ADAPTER_COMPAT_CYCLE_ALIAS_NAMES = ("parse_cycle_time", "format_cycle_time", "cycle_id_for")
    _CHAIN_WORKER_ADAPTER_COMPAT_ALIAS_NAMES = (
        *_CHAIN_WORKER_ADAPTER_COMPAT_CANONICAL_ALIAS_NAMES,
        *_CHAIN_WORKER_ADAPTER_COMPAT_CYCLE_ALIAS_NAMES,
    )
    _CHAIN_WORKER_ADAPTER_COMPAT_TIME_ALIAS_NAMES = ("_check_three_way_time_consistency",)
    _CHAIN_WORKER_ADAPTER_COMPAT_TIME_ALIAS_OWNER_NAMES = _chain.MappingProxyType(
        {"_check_three_way_time_consistency": "check_three_way_time_consistency"}
    )
    _CHAIN_WORKER_ADAPTER_COMPAT_LOCAL_HELPER_NAMES = (
        "scenario_for_source",
        "_auto_trigger_source_policy_identity",
        "_auto_trigger_source_object_identity",
        "_auto_trigger_source_identity_adapter",
    )
    _CHAIN_WORKER_ADAPTER_COMPAT_CHAIN_LOCAL_CLASSIFICATIONS = _chain.MappingProxyType(
        {
            "scenario_for_source": "chain-local-source-scenario-glue",
            "_auto_trigger_source_policy_identity": "chain-local-source-identity-glue",
            "_auto_trigger_source_object_identity": "chain-local-source-identity-glue",
            "_auto_trigger_source_identity_adapter": "chain-local-dynamic-adapter-glue",
        }
    )
    _CHAIN_WORKER_ADAPTER_COMPAT_DYNAMIC_ADAPTERS = _chain.MappingProxyType(
        {
            "gfs": ("workers.data_adapters.gfs_adapter", "GFSAdapter", "GFSAdapterConfig"),
            "IFS": ("workers.data_adapters.ifs_adapter", "IFSAdapter", "IFSAdapterConfig"),
        }
    )
    _CHAIN_WORKER_ADAPTER_COMPAT_CANONICAL_ALIAS_OWNER_MISSING = tuple(
        (
            name
            for name in _CHAIN_WORKER_ADAPTER_COMPAT_CANONICAL_ALIAS_NAMES
            if not hasattr(_chain._canonical_converter_module, name)
        )
    )
    _CHAIN_WORKER_ADAPTER_COMPAT_CYCLE_ALIAS_OWNER_MISSING = tuple(
        (
            name
            for name in _CHAIN_WORKER_ADAPTER_COMPAT_CYCLE_ALIAS_NAMES
            if not hasattr(_chain._data_adapters_base_module, name)
        )
    )
    _CHAIN_WORKER_ADAPTER_COMPAT_ALIAS_FACADE_MISSING = tuple(
        (name for name in _CHAIN_WORKER_ADAPTER_COMPAT_ALIAS_NAMES if name not in _chain.__dict__)
    )
    _CHAIN_WORKER_ADAPTER_COMPAT_TIME_ALIAS_OWNER_MISSING = tuple(
        (
            facade_name
            for facade_name, owner_name in _CHAIN_WORKER_ADAPTER_COMPAT_TIME_ALIAS_OWNER_NAMES.items()
            if not hasattr(_chain._time_consistency_module, owner_name)
        )
    )
    _CHAIN_WORKER_ADAPTER_COMPAT_TIME_ALIAS_FACADE_MISSING = tuple(
        (name for name in _CHAIN_WORKER_ADAPTER_COMPAT_TIME_ALIAS_NAMES if name not in _chain.__dict__)
    )
    _CHAIN_WORKER_ADAPTER_COMPAT_LOCAL_HELPER_MISSING = tuple(
        (name for name in _CHAIN_WORKER_ADAPTER_COMPAT_LOCAL_HELPER_NAMES if not callable(_chain.__dict__.get(name)))
    )
    if _CHAIN_WORKER_ADAPTER_COMPAT_CANONICAL_ALIAS_OWNER_MISSING:
        raise RuntimeError(
            f"chain worker canonical compatibility aliases missing from owner module: {', '.join(_CHAIN_WORKER_ADAPTER_COMPAT_CANONICAL_ALIAS_OWNER_MISSING)}"
        )
    if _CHAIN_WORKER_ADAPTER_COMPAT_CYCLE_ALIAS_OWNER_MISSING:
        raise RuntimeError(
            f"chain worker cycle compatibility aliases missing from owner module: {', '.join(_CHAIN_WORKER_ADAPTER_COMPAT_CYCLE_ALIAS_OWNER_MISSING)}"
        )
    if _CHAIN_WORKER_ADAPTER_COMPAT_ALIAS_FACADE_MISSING:
        raise RuntimeError(
            f"chain worker compatibility aliases missing from facade: {', '.join(_CHAIN_WORKER_ADAPTER_COMPAT_ALIAS_FACADE_MISSING)}"
        )
    if _CHAIN_WORKER_ADAPTER_COMPAT_TIME_ALIAS_OWNER_MISSING:
        raise RuntimeError(
            f"chain worker time-consistency aliases missing from owner module: {', '.join(_CHAIN_WORKER_ADAPTER_COMPAT_TIME_ALIAS_OWNER_MISSING)}"
        )
    if _CHAIN_WORKER_ADAPTER_COMPAT_TIME_ALIAS_FACADE_MISSING:
        raise RuntimeError(
            f"chain worker time-consistency aliases missing from facade: {', '.join(_CHAIN_WORKER_ADAPTER_COMPAT_TIME_ALIAS_FACADE_MISSING)}"
        )
    if _CHAIN_WORKER_ADAPTER_COMPAT_LOCAL_HELPER_MISSING:
        raise RuntimeError(
            f"chain worker/source identity local helpers missing from facade: {', '.join(_CHAIN_WORKER_ADAPTER_COMPAT_LOCAL_HELPER_MISSING)}"
        )
    _CHAIN_WORKER_ADAPTER_COMPAT_OWNER_ALIASES = _chain.MappingProxyType(
        {
            **{
                name: getattr(_chain._canonical_converter_module, name)
                for name in _CHAIN_WORKER_ADAPTER_COMPAT_CANONICAL_ALIAS_NAMES
            },
            **{
                name: getattr(_chain._data_adapters_base_module, name)
                for name in _CHAIN_WORKER_ADAPTER_COMPAT_CYCLE_ALIAS_NAMES
            },
        }
    )
    _CHAIN_WORKER_ADAPTER_COMPAT_FACADE_ALIASES = _chain.MappingProxyType(
        {name: _chain.__dict__[name] for name in _CHAIN_WORKER_ADAPTER_COMPAT_ALIAS_NAMES}
    )
    _CHAIN_WORKER_ADAPTER_COMPAT_TIME_OWNER_ALIASES = _chain.MappingProxyType(
        {
            facade_name: getattr(_chain._time_consistency_module, owner_name)
            for facade_name, owner_name in _CHAIN_WORKER_ADAPTER_COMPAT_TIME_ALIAS_OWNER_NAMES.items()
        }
    )
    _CHAIN_WORKER_ADAPTER_COMPAT_TIME_FACADE_ALIASES = _chain.MappingProxyType(
        {name: _chain.__dict__[name] for name in _CHAIN_WORKER_ADAPTER_COMPAT_TIME_ALIAS_NAMES}
    )
    _CHAIN_WORKER_ADAPTER_COMPAT_CHAIN_LOCAL_HELPERS = _chain.MappingProxyType(
        {name: _chain.__dict__[name] for name in _CHAIN_WORKER_ADAPTER_COMPAT_LOCAL_HELPER_NAMES}
    )
    _CHAIN_WORKER_ADAPTER_COMPAT_ALIAS_DRIFT = tuple(
        (
            name
            for name in _CHAIN_WORKER_ADAPTER_COMPAT_ALIAS_NAMES
            if _CHAIN_WORKER_ADAPTER_COMPAT_OWNER_ALIASES[name] is not _CHAIN_WORKER_ADAPTER_COMPAT_FACADE_ALIASES[name]
        )
    )
    _CHAIN_WORKER_ADAPTER_COMPAT_TIME_ALIAS_DRIFT = tuple(
        (
            name
            for name in _CHAIN_WORKER_ADAPTER_COMPAT_TIME_ALIAS_NAMES
            if _CHAIN_WORKER_ADAPTER_COMPAT_TIME_OWNER_ALIASES[name]
            is not _CHAIN_WORKER_ADAPTER_COMPAT_TIME_FACADE_ALIASES[name]
        )
    )
    if _CHAIN_WORKER_ADAPTER_COMPAT_ALIAS_DRIFT:
        raise RuntimeError(
            f"chain worker adapter direct alias drifted from owner module: {', '.join(_CHAIN_WORKER_ADAPTER_COMPAT_ALIAS_DRIFT)}"
        )
    if _CHAIN_WORKER_ADAPTER_COMPAT_TIME_ALIAS_DRIFT:
        raise RuntimeError(
            f"chain worker time-consistency alias drifted from owner module: {', '.join(_CHAIN_WORKER_ADAPTER_COMPAT_TIME_ALIAS_DRIFT)}"
        )
    _CHAIN_RESERVATION_COMPAT_LOCAL_BINDING_MISSING = tuple(
        (name for name in _CHAIN_RESERVATION_COMPAT_LOCAL_BINDING_NAMES if not callable(_chain.__dict__.get(name)))
    )
    if _CHAIN_RESERVATION_COMPAT_LOCAL_BINDING_MISSING:
        raise RuntimeError(
            f"chain reservation compatibility local bindings missing from facade: {', '.join(_CHAIN_RESERVATION_COMPAT_LOCAL_BINDING_MISSING)}"
        )
    _chain_stage_execution_deps = _chain.ForecastOrchestrator._chain_stage_execution_dependencies()
    _CHAIN_TILE_PUBLISHER_COMPAT_STAGE_EXECUTION_DEPENDENCY_BINDING_DRIFT = tuple(
        (
            field
            for field, facade_name in _CHAIN_TILE_PUBLISHER_COMPAT_STAGE_EXECUTION_DEPENDENCY_BINDINGS
            if getattr(_chain_stage_execution_deps, field) is not _chain.__dict__[facade_name]
        )
    )
    if _CHAIN_TILE_PUBLISHER_COMPAT_STAGE_EXECUTION_DEPENDENCY_BINDING_DRIFT:
        raise RuntimeError(
            f"chain tile-publisher stage execution dependency bindings drifted from legacy facade: {', '.join(_CHAIN_TILE_PUBLISHER_COMPAT_STAGE_EXECUTION_DEPENDENCY_BINDING_DRIFT)}"
        )
    _CHAIN_RESERVATION_COMPAT_STAGE_EXECUTION_DEPENDENCY_BINDING_DRIFT = tuple(
        (
            field
            for field, facade_name in _CHAIN_RESERVATION_COMPAT_STAGE_EXECUTION_DEPENDENCY_BINDINGS
            if getattr(_chain_stage_execution_deps, field) is not _chain.__dict__[facade_name]
        )
    )
    if _CHAIN_RESERVATION_COMPAT_STAGE_EXECUTION_DEPENDENCY_BINDING_DRIFT:
        raise RuntimeError(
            f"chain reservation stage execution dependency bindings drifted from legacy facade: {', '.join(_CHAIN_RESERVATION_COMPAT_STAGE_EXECUTION_DEPENDENCY_BINDING_DRIFT)}"
        )
    del _chain_stage_execution_deps
    export_names = (
        "AnalysisOrchestrator",
        "PsycopgOrchestratorRepository",
        "_CHAIN_ARRAY_ACCOUNTING_COMPAT_DEPENDENCY_BINDING_DRIFT",
        "_CHAIN_ARRAY_ACCOUNTING_COMPAT_METHOD_FACADE_MISSING",
        "_CHAIN_ARRAY_ACCOUNTING_COMPAT_METHOD_FORWARDERS",
        "_CHAIN_ARRAY_ACCOUNTING_COMPAT_TOP_LEVEL_FACADE_MISSING",
        "_CHAIN_ARRAY_ACCOUNTING_COMPAT_TOP_LEVEL_FORWARDERS",
        "_CHAIN_MANIFEST_COMPAT_ANALYSIS_METHOD_FACADE_MISSING",
        "_CHAIN_MANIFEST_COMPAT_ANALYSIS_METHOD_FORWARDERS",
        "_CHAIN_MANIFEST_COMPAT_DEPENDENCY_BINDING_DRIFT",
        "_CHAIN_MANIFEST_COMPAT_FORECAST_METHOD_FACADE_MISSING",
        "_CHAIN_MANIFEST_COMPAT_FORECAST_METHOD_FORWARDERS",
        "_CHAIN_PERSISTENCE_REPOSITORY_COMPAT_FORWARDER_CLASSIFICATION_DRIFT",
        "_CHAIN_PERSISTENCE_REPOSITORY_COMPAT_IMPLEMENTATION_METHODS",
        "_CHAIN_PERSISTENCE_REPOSITORY_COMPAT_IMPLEMENTATION_METHOD_MISSING",
        "_CHAIN_PERSISTENCE_REPOSITORY_COMPAT_LOCAL_REPOSITORIES",
        "_CHAIN_PERSISTENCE_REPOSITORY_COMPAT_LOCAL_REPOSITORY_MISSING",
        "_CHAIN_PERSISTENCE_REPOSITORY_COMPAT_LOCAL_REPOSITORY_OWNER_DRIFT",
        "_CHAIN_PERSISTENCE_REPOSITORY_COMPAT_PROTOCOL_METHODS",
        "_CHAIN_PERSISTENCE_REPOSITORY_COMPAT_PROTOCOL_METHOD_MISSING",
        "_CHAIN_RESERVATION_COMPAT_LOCAL_BINDING_MISSING",
        "_CHAIN_RESERVATION_COMPAT_METHOD_FACADE_MISSING",
        "_CHAIN_RESERVATION_COMPAT_OWNER_METHOD_FORWARDERS",
        "_CHAIN_RESERVATION_COMPAT_STAGE_EXECUTION_DEPENDENCY_BINDING_DRIFT",
        "_CHAIN_RETRY_COMPAT_CHAIN_LOCAL_FACTORIES",
        "_CHAIN_RETRY_COMPAT_CHAIN_LOCAL_METHODS",
        "_CHAIN_RETRY_COMPAT_CONSTRUCTOR_CONFIG_DRIFT",
        "_CHAIN_RETRY_COMPAT_CONSTRUCTOR_PARAM_MISSING",
        "_CHAIN_RETRY_COMPAT_LOCAL_FACTORY_MISSING",
        "_CHAIN_RETRY_COMPAT_LOCAL_METHOD_MISSING",
        "_CHAIN_STAGE_EXECUTION_COMPAT_FACADE_MISSING",
        "_CHAIN_TILE_PUBLISHER_COMPAT_STAGE_EXECUTION_DEPENDENCY_BINDING_DRIFT",
        "_CHAIN_WORKER_ADAPTER_COMPAT_ALIAS_DRIFT",
        "_CHAIN_WORKER_ADAPTER_COMPAT_ALIAS_FACADE_MISSING",
        "_CHAIN_WORKER_ADAPTER_COMPAT_ALIAS_NAMES",
        "_CHAIN_WORKER_ADAPTER_COMPAT_CANONICAL_ALIAS_NAMES",
        "_CHAIN_WORKER_ADAPTER_COMPAT_CANONICAL_ALIAS_OWNER_MISSING",
        "_CHAIN_WORKER_ADAPTER_COMPAT_CHAIN_LOCAL_CLASSIFICATIONS",
        "_CHAIN_WORKER_ADAPTER_COMPAT_CHAIN_LOCAL_HELPERS",
        "_CHAIN_WORKER_ADAPTER_COMPAT_CYCLE_ALIAS_NAMES",
        "_CHAIN_WORKER_ADAPTER_COMPAT_CYCLE_ALIAS_OWNER_MISSING",
        "_CHAIN_WORKER_ADAPTER_COMPAT_DYNAMIC_ADAPTERS",
        "_CHAIN_WORKER_ADAPTER_COMPAT_FACADE_ALIASES",
        "_CHAIN_WORKER_ADAPTER_COMPAT_LOCAL_HELPER_MISSING",
        "_CHAIN_WORKER_ADAPTER_COMPAT_LOCAL_HELPER_NAMES",
        "_CHAIN_WORKER_ADAPTER_COMPAT_OWNER_ALIASES",
        "_CHAIN_WORKER_ADAPTER_COMPAT_TIME_ALIAS_DRIFT",
        "_CHAIN_WORKER_ADAPTER_COMPAT_TIME_ALIAS_FACADE_MISSING",
        "_CHAIN_WORKER_ADAPTER_COMPAT_TIME_ALIAS_NAMES",
        "_CHAIN_WORKER_ADAPTER_COMPAT_TIME_ALIAS_OWNER_MISSING",
        "_CHAIN_WORKER_ADAPTER_COMPAT_TIME_ALIAS_OWNER_NAMES",
        "_CHAIN_WORKER_ADAPTER_COMPAT_TIME_FACADE_ALIASES",
        "_CHAIN_WORKER_ADAPTER_COMPAT_TIME_OWNER_ALIASES",
        "_CHAIN_WORKSPACE_LOG_COMPAT_METHOD_FACADE_MISSING",
        "_CHAIN_WORKSPACE_LOG_COMPAT_METHOD_FORWARDERS",
        "_CHAIN_WORKSPACE_LOG_COMPAT_TOP_LEVEL_FACADE_MISSING",
        "_CHAIN_WORKSPACE_LOG_COMPAT_TOP_LEVEL_FORWARDERS",
        "_retry_service_from_env",
        "_utcnow",
    )
    return {name: value for name, value in locals().items() if name in export_names}
