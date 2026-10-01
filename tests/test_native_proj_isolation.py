"""eccodes then pyproj must exit cleanly with a single PROJ mapped (#2573).

Root cause, measured on node-27: a floating ``pip install -e ".[dev]"``
resolved eccodes 2.49 together with the ``eccodeslib``/``eckitlib`` wheels, and
``eckitlib`` bundles PROJ 9.8.1. ``findlibs`` loads libeccodes with
``CDLL(..., mode=RTLD_GLOBAL)``, so eckit's PROJ symbols enter the global
namespace. pyproj, loaded afterwards, ships its own PROJ 9.5.1, but its
``proj_context_*`` calls then bind to eckit's 9.8.1 (``LD_DEBUG=bindings``).
Two PROJ versions share one context object and the heap is corrupted at
interpreter exit: SIGSEGV/SIGABRT ``double free`` after every test passed. The
lock environment (eccodes 2.47.0, no eckitlib) maps only pyproj's PROJ.

The child process reproduces the exact import order. It exits 139 in the
floating environment, so the return-code assertion is the regression signal on
every platform; on Linux the mapped ``libproj`` set is checked as well.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

CHILD = textwrap.dedent(
    """
    import os
    import sys

    try:
        import eccodes

        eccodes.codes_get_api_version()
        loaded = True
    except (ImportError, RuntimeError) as exc:
        loaded = False
        print(f"eccodes-error={type(exc).__name__}: {exc}")
    print(f"eccodes-loaded={loaded}")

    import pyproj

    x, y = pyproj.Transformer.from_crs(4326, 3857, always_xy=True).transform(100.0, 37.0)
    print(f"transform={x:.1f},{y:.1f}")

    if sys.platform.startswith("linux"):
        paths = set()
        with open("/proc/self/maps", encoding="utf-8") as maps:
            for line in maps:
                fields = line.split()
                if len(fields) >= 6 and os.path.basename(fields[5]).startswith("libproj"):
                    paths.add(os.path.realpath(fields[5]))
        for path in sorted(paths):
            print(f"libproj={path}")
    """
)


def test_eccodes_then_pyproj_exits_cleanly_with_a_single_proj() -> None:
    result = subprocess.run([sys.executable, "-c", CHILD], capture_output=True, text=True, timeout=120)
    lines = result.stdout.splitlines()
    eccodes_loaded = next((line for line in lines if line.startswith("eccodes-loaded=")), "eccodes-loaded=?")
    libproj = [line.removeprefix("libproj=") for line in lines if line.startswith("libproj=")]
    print(*lines, sep="\n")  # visible with -rA: the eccodes-loaded flag and mapped PROJ

    assert result.returncode == 0, (
        f"#2573: importing eccodes then using pyproj exited {result.returncode} "
        f"({eccodes_loaded}); a second bundled PROJ is likely mapped.\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "transform=11131949.1,4439106.8" in lines, result.stdout
    if sys.platform.startswith("linux"):
        assert len(libproj) <= 1, (
            f"#2573: {len(libproj)} distinct libproj libraries mapped ({eccodes_loaded}): {libproj}"
        )
