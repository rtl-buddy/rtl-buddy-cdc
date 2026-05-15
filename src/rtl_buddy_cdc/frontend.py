"""Frontend abstraction: elaborate SV sources into a :class:`netlist.Module`.

The analyzer (``rules.py`` / ``sdc.py`` / ``waivers.py`` / ``reporter.py``)
operates on a :class:`rtl_buddy_cdc.netlist.Module`. How that ``Module``
got there is pluggable:

- ``"yosys"`` — shell out to Yosys (``hierarchy; proc; flatten; opt_clean;
  write_json``) and load the resulting JSON. This is the historical
  primary path and remains the default.
- ``"slang"`` — elaborate SystemVerilog directly via the pyslang Python
  binding (no Yosys subprocess, no flatten step). Currently a stubbed
  skeleton; see issue #5.

Both frontends must produce a ``Module`` shape that satisfies the rule
pack's contract (Yosys-style cell types ``$dff``/``$and``/…, pin names
``CLK``/``D``/``Q``/``Y``/``A``/``B``/…, integer bit IDs for nets,
SV attributes on netnames). Rules don't know which frontend produced
the module; the contract is the only coupling.

This module deliberately stays thin — it picks an implementation and
delegates. The implementations live in :mod:`rtl_buddy_cdc.frontends`.
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path

from rtl_buddy_cdc.netlist import Module


class Frontend(str, Enum):
    """Which elaboration frontend to use."""

    yosys = "yosys"
    slang = "slang"


def elaborate(
    sources: list[Path],
    top: str,
    frontend: Frontend = Frontend.yosys,
    *,
    yosys_bin: str | None = None,
    keep_json: Path | None = None,
    yosys_plugin: str | None = None,
) -> Module:
    """Elaborate ``sources`` into a flattened :class:`Module`.

    The ``frontend`` selector dispatches to the concrete implementation;
    callers only see the resulting ``Module``. Frontend-specific options
    are accepted as keyword arguments and silently ignored by frontends
    that don't use them (e.g. ``yosys_bin`` is meaningless for slang).
    """
    if frontend is Frontend.yosys:
        from rtl_buddy_cdc.frontends import yosys as yosys_fe

        return yosys_fe.elaborate(
            sources,
            top,
            yosys_bin=yosys_bin,
            keep_json=keep_json,
            plugin_path=yosys_plugin,
        )
    if frontend is Frontend.slang:
        from rtl_buddy_cdc.frontends import slang as slang_fe

        return slang_fe.elaborate(sources, top)
    raise ValueError(f"unknown frontend: {frontend!r}")
