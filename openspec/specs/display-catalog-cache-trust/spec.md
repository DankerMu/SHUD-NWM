# display-catalog-cache-trust Specification

## Purpose

Define who may force the display_readonly catalog cache (`apps/api/display_cache.py::display_catalog_cached`) to skip its store lookup and recompute: only the in-process warmer and a caller holding the configured token. Every other request, whatever headers it carries, is served by the TTL / stale-while-revalidate rules.

## Requirements

### Requirement: Forced refresh is granted only to the in-process warmer or a caller holding the configured token

`display_catalog_cached` SHALL bypass the store lookup and recompute the value only when `_force_refresh(request)` is true. `_force_refresh` SHALL return true when, and only when, either (a) the ASGI scope carries `scope["state"]["nhms_display_cache_warm"] is True`, which only the in-process warmer's `_replay_targets` sets by wrapping the application before handing it to `httpx.ASGITransport`, or (b) the runtime configuration holds a non-empty `display_cache_warm_token` (from `NHMS_DISPLAY_CACHE_WARM_TOKEN`, stripped; blank means unset) AND the request carries an `x-nhms-cache-warm` header whose value equals the token under `hmac.compare_digest` applied to the `encode("utf-8", "surrogateescape")` encodings of both strings (never to `str` objects, and with `surrogateescape` so a token that reached `os.environ` through an invalid UTF-8 byte cannot raise either: Starlette decodes header values as latin-1 and `compare_digest` raises `TypeError` on non-ASCII `str`). The runtime configuration is read from `request.app.state.runtime_config` with the same defensive attribute access as `_display_readonly`; a request object lacking `app`, `scope` or `headers`, a non-`str` header value, or a blank token SHALL yield false, and `_force_refresh` SHALL never raise. The literal value `refresh` SHALL NOT be privileged. When the token is unset, no header value SHALL force a refresh. The non-display-role passthrough (`loader()` called directly) SHALL be unchanged. `RuntimeConfig.public_dict()` and `repr(RuntimeConfig)` SHALL NOT include the token.

#### Scenario: An external request carrying the legacy header value hits the cache

- **WHEN** the display_readonly role has a cached value for key `k`, the token is unset, and a request carries `x-nhms-cache-warm: refresh`
- **THEN** `display_catalog_cached` returns the cached value and the loader is not called

#### Scenario: The configured token forces a recompute and the result is stored

- **WHEN** the runtime config on `request.app.state` holds `display_cache_warm_token == "abc"`, key `k` is cached, and a request carries `x-nhms-cache-warm: abc`
- **THEN** the loader is called, its value is stored, and a following plain request for `k` returns the new value
- **AND** a request carrying any other value (or no header) for `k` returns the cached value without calling the loader

#### Scenario: A non-ASCII header value is a cache hit, never an error

- **WHEN** the runtime config holds token `abc`, key `k` is cached, and a request carries `x-nhms-cache-warm` with the value `"ab\u00e9"` (a non-ASCII `str`, as Starlette produces for a byte ≥ 0x80)
- **THEN** `display_catalog_cached` returns the cached value without raising and the loader is not called

#### Scenario: The in-process warmer still refreshes without any token

- **WHEN** the token is unset, key `k` is cached from a plain request, and `_replay_targets(app, [path])` replays that path
- **THEN** the loader is called again and the store holds the replayed value

#### Scenario: The token is never exposed through the runtime config surface

- **WHEN** `load_runtime_config` is given `NHMS_DISPLAY_CACHE_WARM_TOKEN=" abc "`
- **THEN** `config.display_cache_warm_token == "abc"`, `config.public_dict()` has no key whose value is `"abc"`, and `repr(config)` does not contain `"abc"`; a missing or blank variable yields `None`

### Requirement: The node-27 prewarm sends the token when configured and degrades honestly when it is not

`scripts/node27_mvt_prewarm.py::fetch_json` SHALL send `x-nhms-cache-warm: <token>` when `NHMS_DISPLAY_CACHE_WARM_TOKEN` is set to a non-blank value in the process environment. When it is unset or blank, `fetch_json` SHALL NOT send the header, SHALL write exactly one warning line to stderr per process (`prewarm: NHMS_DISPLAY_CACHE_WARM_TOKEN unset; discovery may see up to 45 s stale catalog`), and SHALL otherwise behave unchanged (same URL, same `Accept`, same JSON parsing, same summary schema).

#### Scenario: Token present

- **WHEN** the environment holds `NHMS_DISPLAY_CACHE_WARM_TOKEN=abc` and `fetch_json` issues a discovery request
- **THEN** the request carries `x-nhms-cache-warm: abc`

#### Scenario: Token absent

- **WHEN** the variable is unset and `fetch_json` issues two discovery requests
- **THEN** neither request carries `x-nhms-cache-warm`, stderr holds exactly one warning line, and both responses are parsed normally

