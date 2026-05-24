"""Content-hash-keyed Yosys cache for the fuzz corpus.

Yosys elaboration is the inner-loop cost for the differential
harness. Templates are deterministic given a seed, and rendered SV
is byte-identical across re-runs, so caching by SHA-256 of the
SV+SDC content turns the second-and-onward run into an in-memory
JSON load.

Layout::

    tests/fuzz/.cache/
      <first-byte>/
        <full-hash>.sv     - canonical input (debug aid)
        <full-hash>.json   - Yosys-emitted JSON netlist

The cache is intentionally simple — no eviction, no concurrency
locking. Worst case the cache directory grows large; ``rm -rf
tests/fuzz/.cache`` reset.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .templates.base import RenderedCase

CACHE_ROOT = Path(__file__).parent / ".cache"


@dataclass(frozen=True)
class CacheResult:
    json_path: Path
    cache_hit: bool


def yosys_available() -> bool:
    return shutil.which("yosys") is not None


def build(case: RenderedCase) -> CacheResult:
    """Elaborate ``case`` through Yosys and return the JSON path.

    Returns the cached path on a hit; runs Yosys on a miss. Raises
    :class:`RuntimeError` if Yosys is missing or returns non-zero.
    """
    if not yosys_available():
        raise RuntimeError("yosys not on PATH; cannot build fuzz case")

    digest = case.content_hash
    bucket = CACHE_ROOT / digest[:2]
    bucket.mkdir(parents=True, exist_ok=True)
    sv_path = bucket / f"{digest}.sv"
    json_path = bucket / f"{digest}.json"

    if json_path.exists():
        return CacheResult(json_path=json_path, cache_hit=True)

    sv_path.write_text(case.sv)
    # Yosys command string. Mirrors the build recipe used by the
    # hand-authored fixtures: read_verilog -sv, hierarchy -top,
    # proc, flatten, write_json. No ``opt`` — we want the structural
    # shape the analyzer cares about, not optimised gates.
    #
    # ``case.extra_yosys_passes`` is appended between flatten and
    # write_json; templates that need gate-level / tech-mapped
    # behaviour (e.g. the G-12 sentinel) set this to
    # ``"simplemap; abc -g cmos;"``.
    extra = case.extra_yosys_passes.strip()
    if extra and not extra.endswith(";"):
        extra = extra + ";"
    cmd = [
        "yosys",
        "-q",
        "-p",
        (
            f"read_verilog -sv {sv_path}; "
            f"hierarchy -top {case.top}; "
            "proc; flatten; "
            + (f"{extra} " if extra else "")
            + f"write_json {json_path}"
        ),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(
            f"yosys failed for {case.case_id}:\n"
            f"--- stdout ---\n{proc.stdout}\n"
            f"--- stderr ---\n{proc.stderr}\n"
            f"--- SV ---\n{case.sv}\n"
        )
    return CacheResult(json_path=json_path, cache_hit=False)


def clear_cache() -> None:
    """Drop the entire cache. Useful in CI between runs."""
    if CACHE_ROOT.exists():
        shutil.rmtree(CACHE_ROOT)
