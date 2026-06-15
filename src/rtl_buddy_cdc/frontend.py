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

import importlib.util
from enum import Enum
from pathlib import Path

from rtl_buddy_cdc.netlist import Module


class Frontend(str, Enum):
    """Which elaboration frontend to use."""

    yosys = "yosys"
    slang = "slang"
    # Probe pyslang at runtime and prefer slang when available; fall back
    # to yosys otherwise. The default stays ``yosys`` — ``auto`` is
    # opt-in. See ``resolve_auto`` for the resolution rule.
    auto = "auto"


def resolve_auto() -> Frontend:
    """Resolve ``Frontend.auto`` into a concrete frontend.

    Returns :attr:`Frontend.slang` when pyslang is importable in the
    current environment (the slang frontend has no subprocess overhead
    and no Yosys runtime dependency), :attr:`Frontend.yosys` otherwise.
    """
    if importlib.util.find_spec("pyslang") is not None:
        return Frontend.slang
    return Frontend.yosys


def elaborate(
    sources: list[Path],
    top: str,
    frontend: Frontend = Frontend.yosys,
    *,
    yosys_bin: str | None = None,
    keep_json: Path | None = None,
    yosys_plugin: str | None = None,
    blackbox: list[str] | None = None,
) -> Module:
    """Elaborate ``sources`` into a flattened :class:`Module`.

    The ``frontend`` selector dispatches to the concrete implementation;
    callers only see the resulting ``Module``. Frontend-specific options
    are accepted as keyword arguments and silently ignored by frontends
    that don't use them (e.g. ``yosys_bin`` is meaningless for slang).

    ``blackbox`` names modules to treat as CDC boundary cells (the P1
    ``--blackbox`` surface). It is threaded into the Yosys/``read_slang``
    invocation; the slang (pyslang) frontend does not support it yet.
    """
    module, _blackboxes = elaborate_with_blackboxes(
        sources,
        top,
        frontend,
        yosys_bin=yosys_bin,
        keep_json=keep_json,
        yosys_plugin=yosys_plugin,
        blackbox=blackbox,
    )
    return module


def elaborate_with_blackboxes(
    sources: list[Path],
    top: str,
    frontend: Frontend = Frontend.yosys,
    *,
    yosys_bin: str | None = None,
    keep_json: Path | None = None,
    yosys_plugin: str | None = None,
    blackbox: list[str] | None = None,
) -> tuple[Module, dict[str, Module]]:
    """Elaborate ``sources`` into a top :class:`Module` plus its blackbox
    sibling modules (keyed by module name).

    Same dispatch as :func:`elaborate`, but surfaces the blackbox
    boundary siblings the Yosys ``--blackbox`` surface produces so the
    ``lint`` path can auto-abstract them (#257). The slang frontend has
    no blackbox support yet and returns an empty sibling map.
    """
    if frontend is Frontend.auto:
        frontend = resolve_auto()
    if frontend is Frontend.yosys:
        from rtl_buddy_cdc.frontends import yosys as yosys_fe

        return yosys_fe.elaborate_with_blackboxes(
            sources,
            top,
            yosys_bin=yosys_bin,
            keep_json=keep_json,
            plugin_path=yosys_plugin,
            blackbox=blackbox,
        )
    if frontend is Frontend.slang:
        from rtl_buddy_cdc.frontends import slang as slang_fe

        return slang_fe.elaborate(sources, top), {}
    raise ValueError(f"unknown frontend: {frontend!r}")
