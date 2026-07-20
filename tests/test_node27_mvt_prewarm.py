from __future__ import annotations

from scripts import node27_mvt_prewarm as prewarm


def test_china_default_working_set_is_small_and_unique() -> None:
    tiles = prewarm.xyz_tiles(prewarm.CHINA_BOUNDS, [3, 4])

    assert tiles
    assert len(tiles) == len(set(tiles))
    assert len(tiles) <= 32
    assert all(z in {3, 4} and 0 <= x < 2**z and 0 <= y < 2**z for z, x, y in tiles)


def test_base_river_is_warmed_even_when_no_valid_time_exists() -> None:
    urls = prewarm.build_warm_urls("http://127.0.0.1:8080/", [(3, 6, 3)], None)

    assert urls == ["http://127.0.0.1:8080/api/v1/tiles/river-network-national/3/6/3.pbf"]


def test_valid_time_adds_discharge_tile_and_is_path_encoded() -> None:
    urls = prewarm.build_warm_urls(
        "http://127.0.0.1:8080",
        [(4, 12, 6)],
        "2026-07-20T00:00:00Z",
    )

    assert urls[0].endswith("/river-network-national/4/12/6.pbf")
    assert urls[1].endswith("/hydro-national/q_down/2026-07-20T00%3A00%3A00Z/4/12/6.pbf")


def test_invalid_zoom_and_worker_bounds_fail_closed() -> None:
    for zoom in (-1, 15):
        try:
            prewarm.xyz_tiles(prewarm.CHINA_BOUNDS, [zoom])
        except ValueError:
            pass
        else:
            raise AssertionError("invalid zoom must fail")

    try:
        prewarm.prewarm(base_url="http://127.0.0.1", zooms=[3], workers=0, timeout=1, valid_time=None)
    except ValueError:
        pass
    else:
        raise AssertionError("invalid worker count must fail")
