"""Content-hash-keyed slang elaboration cache for the fuzz corpus.

Mirror of :mod:`tests.fuzz.yosys_cache`, but on the slang frontend.
Stage-3 Layer C (rtl-buddy-cdc#221) — the cross-frontend differential
oracle runs every case through both Yosys and slang and asserts their
``run_all_rules`` finding sets agree. The slang elaborator runs
in-process so the per-case cost is far smaller than Yosys'
subprocess, but a content-hash cache still pays for itself across CI
runs because the corpus is parameter-stable.

Layout::

    tests/fuzz/.cache/
      <first-byte-of-hash>/
        <full-hash>.slang10p0p0.pkl    - pickled netlist.Module

The cache key includes the pyslang version because rule-pack
parity is pinned per-major (see ``src/rtl_buddy_cdc/frontends/slang.py``
docstring). A pyslang upgrade should invalidate the slang cache
without touching the Yosys cache that sits next to it.

Pickling is intentional: the slang frontend returns a fully populated
:class:`netlist.Module` (frozen dataclass tree of native types) that
``pickle`` round-trips losslessly. JSON is the wrong surface here —
the Yosys cache stores Yosys' own JSON, but the slang frontend never
emits JSON, so any text format would require a bespoke serialiser
whose only benefit is human readability of an opaque cache file.

The cache is intentionally simple — no eviction, no concurrency
locking. Worst case the cache directory grows large; ``rm -rf
tests/fuzz/.cache`` reset.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import pickle
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from rtl_buddy_cdc.netlist import Module

from .templates.base import RenderedCase
from .yosys_cache import CACHE_ROOT


@lru_cache(maxsize=1)
def slang_version() -> str | None:
    """Resolved pyslang version, or ``None`` if pyslang isn't importable.

    Cached so the import probe runs once per process. ``None`` is the
    "skip this case" signal for the differential test — same pattern
    :mod:`tests.sim` uses for an absent iverilog.
    """
    try:
        return importlib.metadata.version("pyslang")
    except importlib.metadata.PackageNotFoundError:
        return None


def slang_available() -> bool:
    return slang_version() is not None


@dataclass(frozen=True)
class SlangCacheResult:
    module: Module
    cache_hit: bool


def _cache_path(case: RenderedCase) -> Path:
    """Slang-cache path for ``case``. Co-located with the Yosys cache
    so a single ``rm -rf tests/fuzz/.cache`` wipes both."""
    version = slang_version()
    if version is None:
        raise RuntimeError("pyslang not importable; slang cache unavailable")
    suffix = "slang" + version.replace(".", "p")
    digest = case.content_hash
    bucket = CACHE_ROOT / digest[:2]
    bucket.mkdir(parents=True, exist_ok=True)
    return bucket / f"{digest}.{suffix}.pkl"


def build(case: RenderedCase) -> SlangCacheResult:
    """Elaborate ``case`` through the slang frontend; cache the result.

    Returns the cached :class:`Module` on a hit; runs slang on a miss
    and pickles the result for next time. Raises
    :class:`RuntimeError` if pyslang isn't importable — the caller
    is expected to gate on :func:`slang_available` first.
    """
    if not slang_available():
        raise RuntimeError("pyslang not importable; cannot build slang case")

    path = _cache_path(case)
    if path.exists():
        with path.open("rb") as fh:
            module: Module = pickle.load(fh)
        return SlangCacheResult(module=module, cache_hit=True)

    # Lazy import — keeps the no-slang default install path quiet.
    slang_fe = importlib.import_module("rtl_buddy_cdc.frontends.slang")

    # Write the SV to the same path the Yosys cache uses
    # (``<digest>.sv``) so both frontends share one source-of-truth
    # file on disk. The pickle and the Yosys JSON sit alongside it
    # under their own suffixes.
    digest = case.content_hash
    bucket = CACHE_ROOT / digest[:2]
    sv_path = bucket / f"{digest}.sv"
    if not sv_path.exists():
        sv_path.write_text(case.sv)

    module = slang_fe.elaborate([sv_path], case.top)

    # Pickle next to the SV so the cache layout stays uniform with
    # the Yosys side.
    with path.open("wb") as fh:
        pickle.dump(module, fh)
    return SlangCacheResult(module=module, cache_hit=False)
