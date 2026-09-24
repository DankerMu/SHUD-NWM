"""Runtime response models for the JSON routes in ``apps/api/routes/`` (#2348).

One module per route module plus :mod:`.envelope`. Each model is typed by the
Python objects its handler returns today, so adding it changes validation, not
the parsed response (``tests/test_response_model_preservation.py``):

* ``datetime`` where the handler returns ``datetime`` objects (registry rows,
  pipeline ORM rows); ``str`` only where the value is already stringified
  (``_json_ready`` / ``_format_time``); ``datetime | str`` where a raw driver
  value is passed through untouched;
* ``int | float`` wherever either can occur (a bare ``float`` would print
  ``3`` as ``3.0``), ``list[Any]`` for GeoJSON coordinates and forecast
  ``points``, ``Any`` / ``dict[str, Any]`` for open JSON passthrough;
* every model passes unknown keys through (``extra="allow"``) -- the one
  filtering model is :class:`.forecast.HydroRun` (#2222);
* routes whose handlers omit optional keys declare
  ``response_model_exclude_unset=True`` so an absent key stays absent.

Class ``__name__``s are the published OpenAPI component names. Where a
hand-written schema is published for a route
(``apps/api/openapi_restored_schemas.py``, ``openapi_patching_*``), the hand
schema stays the published component and
``tests/test_response_model_schema_parity.py`` binds it to the model.
"""
