"""pyslang frontend: a standalone block whose top has an unconnected
SystemVerilog interface port (and a use-before-declaration) elaborates
natively.

Unlike the yosys ``read_slang`` path — where a top-level interface port
is *unsupported* because a yosys netlist has no interface ports — the
pyslang frontend elaborates the interface hierarchy directly, so a
block parameterised on an interface can be CDC-linted on its own. This
is the path designs like the project-template's interface-bearing
subsystem top must use.

The fixture (``fixtures/slang_iface_port_top``) also references a net
declared later in the same module; both leniencies are set explicitly
in :func:`rtl_buddy_cdc.frontends.slang.elaborate`
(``AllowTopLevelIfacePorts`` / ``AllowUseBeforeDeclare``).

Gated on pyslang being importable (the ``[slang]`` extra) — the same
gate every other slang-frontend test uses.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from rtl_buddy_cdc import sdc as sdc_mod
from rtl_buddy_cdc.cli import _filter_async
from rtl_buddy_cdc.domain import find_crossings
from rtl_buddy_cdc.frontend import Frontend, elaborate
from rtl_buddy_cdc.rules import run_all

PYSLANG_INSTALLED = importlib.util.find_spec("pyslang") is not None
if not PYSLANG_INSTALLED:
    pytest.skip(
        "pyslang not installed — slang elaboration tests are gated on it",
        allow_module_level=True,
    )

FIX = Path(__file__).parent / "fixtures" / "slang_iface_port_top"
TOP = "slang_iface_port_top"


def test_pyslang_elaborates_interface_port_top() -> None:
    """The interface-port top elaborates and the analyzer sees the
    clk_a -> clk_b 8-bit bus crossing (CDC-004 fires on the ungated
    bus). A regression to the strict defaults would raise
    ``SlangElaborationError`` here instead."""
    module = elaborate([FIX / f"{TOP}.sv"], TOP, frontend=Frontend.slang)
    spec = sdc_mod.parse_file(FIX / f"{TOP}.sdc")
    crossings = find_crossings(
        module, port_clock=spec.port_clock, pin_clocks=spec.pin_clocks
    )
    async_c = _filter_async(crossings, spec)
    violations = run_all(module, async_c, spec)

    assert len(async_c) >= 1, "interface-port top produced no async crossing"
    cdc_004 = [v for v in violations if v.rule_id == "CDC-004"]
    assert len(cdc_004) >= 1, sorted({v.rule_id for v in violations})
    assert any(v.crossing is not None and v.crossing.width == 8 for v in cdc_004)
