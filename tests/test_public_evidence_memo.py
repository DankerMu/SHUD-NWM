"""The public sanitiser remembers its results without changing them (#3236).

Seam: ``services.orchestrator.public_evidence`` -- ``_public_evidence`` and the
three cached entry points (``_sanitize_public_text``, ``_sanitize_public_scalar``
and ``_is_sensitive_key``).  The oracle is the uncached implementation of each
(``_sanitize_public_text_uncached``, ``_sanitize_public_scalar_uncached`` and
``packages.common.redaction.is_sensitive_key``) plus literals for the pairs the
cache key must keep apart.

Names the change introduces are reached through the module object inside each
test, so on a tree without the caches every case fails on its own line.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta, timezone

import pytest

from packages.common import redaction
from services.orchestrator import public_evidence as pe


@pytest.fixture
def cold_caches() -> Iterator[None]:
    pe._clear_sanitiser_caches()
    yield
    pe._clear_sanitiser_caches()


def _cache_sizes() -> tuple[int, int, int]:
    return (
        pe._public_text_cache.cache_info().currsize,
        pe._public_scalar_cache.cache_info().currsize,
        pe._sensitive_key_cache.cache_info().currsize,
    )


def _cache_infos() -> tuple[object, object, object]:
    return (
        pe._public_text_cache.cache_info(),
        pe._public_scalar_cache.cache_info(),
        pe._sensitive_key_cache.cache_info(),
    )


@contextmanager
def _uncached(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The three cached entry points replaced by their uncached implementations.

    Asserts that nothing inside touched a cache, so the oracle is cache-free.
    """

    before = _cache_infos()
    with monkeypatch.context() as patch:
        patch.setattr(pe, "_sanitize_public_text", pe._sanitize_public_text_uncached)
        patch.setattr(pe, "_sanitize_public_scalar", pe._sanitize_public_scalar_uncached)
        patch.setattr(pe, "_is_sensitive_key", redaction.is_sensitive_key)
        yield
    assert _cache_infos() == before


def _render_uncached(monkeypatch: pytest.MonkeyPatch, value: object) -> object:
    with _uncached(monkeypatch):
        return pe._public_evidence(value)


# Pairs whose members differ only by surrounding whitespace / letter case and
# must render differently: a cache keyed on a normalised string would hand the
# first member's rendering to the second.
_WHITESPACE_PAIR = ("plain note", " plain note ")
_CASE_PAIR = ("Ready Note", "ready note")
_TEXT_WHITESPACE_PAIR = ("/srv/nhms/ws", " /srv/nhms/ws")


def _corpus() -> list[str]:
    long_text = "lorem ipsum /srv/secret/path token=abc " * 40
    assert len(long_text) > 1024
    return [
        "Authorization: Bearer abcdef0123456789abcdef",
        "password=hunter2 and more",
        "https://user:pa55word@example.org/path?q=1",
        "/ghdc/data/nwm/workspace/runs/run_1/out.nc",
        "~/nwm/objects/run_1",
        "/home/nwm/nhms data/objects",
        "s3://nhms/published/run_1/manifest.json",
        "published://logs/GFS/2026050100/run_1/job_1.out",
        "https://example.org/api/v1/status",
        "upload failed, see s3://nhms/raw/gfs/2026050100/manifest.json for details",
        "s3:bucket/key",
        "output=/srv/nhms/runs/1",
        "'/srv/nhms/quoted'",
        "(/srv/nhms/bracketed)",
        "[s3://nhms/x],",
        "",
        "   ",
        "\t\n",
        *_WHITESPACE_PAIR,
        *_CASE_PAIR,
        *_TEXT_WHITESPACE_PAIR,
        "Ünïcödé text with ✓ and 雨量 /srv/数据/目录",
        "plain ascii prose without anything special",
        long_text,
    ]


def test_normalised_pairs_render_differently(cold_caches: None) -> None:
    """Fixture guard: each pair really renders differently without any cache."""

    for left, right in (_WHITESPACE_PAIR, _CASE_PAIR):
        assert pe._sanitize_public_scalar_uncached(left) != pe._sanitize_public_scalar_uncached(right)
        assert pe._sanitize_public_text_uncached(left) != pe._sanitize_public_text_uncached(right)
    assert pe._sanitize_public_text_uncached("/srv/nhms/ws") == "[local-path]"
    assert pe._sanitize_public_text_uncached(" /srv/nhms/ws") == " [local-path]"
    assert pe._sanitize_public_scalar_uncached("plain note") == "plain note"
    assert pe._sanitize_public_scalar_uncached(" plain note ") == " plain note "
    assert pe._sanitize_public_scalar_uncached("Ready Note") == "Ready Note"
    assert pe._sanitize_public_scalar_uncached("ready note") == "ready note"


# --- 3.1 corpus parity --------------------------------------------------------


def test_text_and_scalar_sanitisers_match_uncached_over_corpus(
    cold_caches: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    corpus = _corpus()
    with _uncached(monkeypatch):
        expected_text = [pe._sanitize_public_text(value) for value in corpus]
        expected_scalar = [pe._sanitize_public_scalar(value) for value in corpus]
    for _round in range(2):
        for value, text, scalar in zip(corpus, expected_text, expected_scalar, strict=True):
            assert pe._sanitize_public_text(value) == text, value
            assert pe._sanitize_public_scalar(value) == scalar, value
    # The corpus did exercise the caches (cold-to-warm), else parity is vacuous.
    assert pe._public_text_cache.cache_info().hits > 0
    assert pe._public_scalar_cache.cache_info().hits > 0
    # Spot literals: the uncached oracle itself is the shape rules.
    assert pe._sanitize_public_scalar("s3://nhms/published/run_1/manifest.json") == "[object-uri]"
    assert pe._sanitize_public_scalar("/home/nwm/nhms data/objects") == "[local-path]"
    assert "hunter2" not in pe._sanitize_public_text("password=hunter2 and more")


# --- 3.2 payload parity -------------------------------------------------------


def _payload() -> dict[str, object]:
    return {
        "scalars": [1, 1.0, 0, 0.0, True, False, "1", None],
        "nested": {
            "when": datetime(2026, 11, 20, 8, 30, tzinfo=timezone(timedelta(hours=8))),
            "items": ({"note": "see s3://nhms/x for details"}, ["/srv/nhms/a", "plain"]),
            "_file_journal_private": {"secret": "dropped"},
            "password": "hunter2",
            "api\\u005fkey": "escaped-secret",
            "message": "failed: token=abc123 at /srv/nhms/run",
            "stage_message": "upload to s3://nhms/y failed",
            "log_path": "/srv/nhms/logs/1.out",
            "object_store_path": {"value": "/srv/nhms/objects", "present": True},
            "artifact_uri": "s3://nhms/runs/1/manifest.json",
            "count": 1,
            "flag": True,
            "ratio": 1.0,
            "zero": 0,
            "off": False,
            "text_one": "1",
        },
        "when_utc": datetime(2026, 11, 20, 0, 0, tzinfo=UTC),
    }


def _assert_scalar_types(rendered: object) -> None:
    assert isinstance(rendered, dict)
    scalars = rendered["scalars"]
    assert [type(value) for value in scalars] == [int, float, int, float, bool, bool, str, type(None)]
    assert scalars == [1, 1.0, 0, 0.0, True, False, "1", None]
    nested = rendered["nested"]
    assert type(nested["count"]) is int
    assert type(nested["flag"]) is bool
    assert type(nested["ratio"]) is float
    assert type(nested["zero"]) is int
    assert type(nested["off"]) is bool
    assert type(nested["text_one"]) is str


def test_payload_renders_equal_cold_warm_and_after_clear(
    cold_caches: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = _payload()
    cold = pe._public_evidence(payload)
    warm = pe._public_evidence(payload)
    pe._clear_sanitiser_caches()
    cleared = pe._public_evidence(payload)
    uncached = _render_uncached(monkeypatch, payload)

    assert cold == warm == cleared == uncached
    for rendered in (cold, warm, cleared, uncached):
        _assert_scalar_types(rendered)
    # The shape rules are in force, so equality is not vacuous.
    nested = cold["nested"]
    assert "_file_journal_private" not in nested
    assert nested["password"] == "[redacted]"
    assert nested["api\\u005fkey"] == "[redacted]"
    assert nested["log_path"] == "[local-path]"
    assert nested["object_store_path"] == {"value": "[local-path]", "present": True}
    assert nested["artifact_uri"] == "[object-uri]"
    assert "abc123" not in nested["message"]
    assert nested["when"] == "2026-11-20T00:30:00Z"


def test_non_string_scalars_never_enter_a_cache(cold_caches: None) -> None:
    before = _cache_sizes()
    rendered = pe._public_evidence([1, 1.0, 0, 0.0, True, False, None, [2, 2.5, True]])
    assert rendered == [1, 1.0, 0, 0.0, True, False, None, [2, 2.5, True]]
    assert [type(value) for value in rendered[:7]] == [int, float, int, float, bool, bool, type(None)]
    assert [type(value) for value in rendered[7]] == [int, float, bool]
    assert _cache_sizes() == before
    # One more order: True first, then 1 -- neither may borrow the other's type.
    assert [type(value) for value in pe._public_evidence([True, 1, 1.0, False, 0])] == [bool, int, float, bool, int]
    assert _cache_sizes() == before


# --- 3.3 bounds ---------------------------------------------------------------


def test_cache_bounds_are_the_starting_constants() -> None:
    assert pe._TEXT_CACHE_MAX_ENTRIES == 8192
    assert pe._TEXT_CACHE_MAX_LENGTH == 1024
    assert pe._KEY_CACHE_MAX_ENTRIES == 4096
    assert pe._KEY_CACHE_MAX_LENGTH == 256
    assert pe._public_text_cache.cache_info().maxsize == 8192
    assert pe._public_scalar_cache.cache_info().maxsize == 8192
    assert pe._sensitive_key_cache.cache_info().maxsize == 4096


def test_text_caches_stop_at_their_maximum(cold_caches: None) -> None:
    for index in range(8192 + 50):
        assert pe._sanitize_public_text(f"note {index}") == f"note {index}"
        assert pe._sanitize_public_scalar(f"value-{index}") == f"value-{index}"
    assert pe._public_text_cache.cache_info().currsize == 8192
    assert pe._public_scalar_cache.cache_info().currsize == 8192


def test_key_cache_stops_at_its_maximum(cold_caches: None) -> None:
    for index in range(4096 + 50):
        assert pe._is_sensitive_key(f"field_{index}") is False
    assert pe._sensitive_key_cache.cache_info().currsize == 4096


def test_over_long_inputs_bypass_the_caches_and_render_correctly(cold_caches: None) -> None:
    long_text = "x" * 1020 + " /srv/a"
    assert len(long_text) == 1027
    long_path = "/srv/" + "p" * 1100
    long_key = "a" * 250 + "_password"
    assert len(long_key) > 256
    before = _cache_sizes()

    assert pe._sanitize_public_text(long_text) == "x" * 1020 + " [local-path]"
    assert pe._sanitize_public_scalar(long_text) == "x" * 1020 + " [local-path]"
    assert pe._sanitize_public_scalar(long_path) == "[local-path]"
    assert pe._is_sensitive_key(long_key) is True
    assert pe._public_evidence({long_key: "secret", "note": long_path}) == {
        long_key: "[redacted]",
        "note": "[local-path]",
    }
    # "note" is a short key and enters the key cache; nothing else may.
    text_size, scalar_size, key_size = _cache_sizes()
    assert (text_size, scalar_size) == before[:2]
    assert key_size == before[2] + 1

    # At the limit the input is still remembered.
    at_limit = "y" * 1024
    pe._sanitize_public_text(at_limit)
    assert pe._public_text_cache.cache_info().currsize == before[0] + 1


# --- 3.4 reuse happens --------------------------------------------------------


def test_rendering_twice_computes_each_distinct_text_once(
    cold_caches: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []
    original = pe._sanitize_public_text_uncached

    def counting(value: str) -> str:
        seen.append(value)
        return original(value)

    monkeypatch.setattr(pe, "_sanitize_public_text_uncached", counting)
    payload = {
        "events": [
            {"message": "stage failed: retry 1", "note": "same note", "stage": "forecast"},
            {"message": "stage failed: retry 1", "note": "same note", "stage": "forecast"},
            {"message": "stage failed: retry 2", "note": "other note", "stage": "publish"},
        ],
        # Never reach the text sanitiser: placeholders and redacted keys.
        "log_path": "/srv/nhms/logs/1.out",
        "artifact_uri": "s3://nhms/x",
        "password": "hunter2",
        "location": "/srv/nhms/whole",
    }
    pe._public_evidence(payload)
    pe._public_evidence(payload)

    # Hand-enumerated: the distinct strings that reach the text sanitiser.
    expected = {
        "stage failed: retry 1",
        "stage failed: retry 2",
        "same note",
        "other note",
        "forecast",
        "publish",
    }
    assert sorted(seen) == sorted(expected)


# --- the clearing function ------------------------------------------------------


def test_clear_empties_all_three_caches(cold_caches: None) -> None:
    pe._sanitize_public_text("some text")
    pe._sanitize_public_scalar("some scalar")
    pe._is_sensitive_key("some_key")
    assert all(size > 0 for size in _cache_sizes())
    pe._clear_sanitiser_caches()
    assert _cache_sizes() == (0, 0, 0)
