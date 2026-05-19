"""Hints win over SV attributes on disagreement (issue #129).

The SV-attribute fixture ``bad_marked_reset_polarity`` annotates
``rst_n`` as ``polarity = "low"`` — and the flop is wired posedge
(inferred high), so RDC-002 fires. If a hints file flips the
declaration to ``high``, the disagreement should resolve in favour
of the hint, the declared polarity now matches the flop, and
RDC-002 falls silent.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from rtl_buddy_cdc import netlist, sdc as sdc_mod
from rtl_buddy_cdc.domain import find_crossings
from rtl_buddy_cdc.reset_hints import PortHint, ResetHints
from rtl_buddy_cdc.rules import (
    run_all,
    user_reset_polarity_overrides,
)

PYYAML_INSTALLED = importlib.util.find_spec("yaml") is not None
pytestmark = pytest.mark.skipif(
    not PYYAML_INSTALLED, reason="[hints] extra not installed"
)

FIX = Path(__file__).parent / "fixtures" / "bad_marked_reset_polarity"


def _load_module_and_async_crossings():
    module = netlist.load(FIX / "bad_marked_reset_polarity.json")
    spec = sdc_mod.parse_file(FIX / "bad_marked_reset_polarity.sdc")
    sdc_mod.synthesize_unconstrained_inputs(spec, module)
    crossings = find_crossings(
        module, port_clock=spec.port_clock, pin_clocks=spec.pin_clocks
    )
    async_cs = [
        c
        for c in crossings
        if spec.are_async(
            spec.clock_for_port(c.src_clock) or c.src_clock,
            spec.clock_for_port(c.dst_clock) or c.dst_clock,
        )
    ]
    return module, spec, async_cs


def test_sv_attribute_alone_fires_rdc_002() -> None:
    """Baseline: the SV attribute says ``low``, the flop infers high,
    RDC-002 fires."""
    module, spec, async_cs = _load_module_and_async_crossings()
    overrides = user_reset_polarity_overrides(module)
    assert overrides == {"rst_n": "low"}
    violations = run_all(module, async_cs, spec)
    assert [v.rule_id for v in violations if v.rule_id == "RDC-002"] == ["RDC-002"]


def test_hint_overrides_sv_attribute_to_match_flop() -> None:
    """Hints win: hint declares ``high``, the flop is also high,
    declared == inferred, RDC-002 stays silent."""
    module, spec, async_cs = _load_module_and_async_crossings()
    hints = ResetHints(
        schema_version="1.0",
        ports=(PortHint(name="rst_n", polarity="high"),),
    )
    overrides = user_reset_polarity_overrides(module, hints=hints)
    # Hint wins on the same port name.
    assert overrides == {"rst_n": "high"}
    violations = run_all(module, async_cs, spec, reset_hints=hints)
    assert [v for v in violations if v.rule_id == "RDC-002"] == []


def test_hint_ignored_for_unknown_port() -> None:
    """A hint targeting a port that doesn't exist on the module is
    silently dropped at the consumer — strict validation happens at
    load time, so by the time the hint reaches the rule pack the
    port name is taken on faith. An unknown port can't be a real
    boundary-of-design fact; ignoring it is the safe choice."""
    module, _, _ = _load_module_and_async_crossings()
    hints = ResetHints(
        schema_version="1.0",
        ports=(PortHint(name="rst_zomg_unknown", polarity="low"),),
    )
    overrides = user_reset_polarity_overrides(module, hints=hints)
    # Only the SV-attribute fact remains; the unknown-port hint dropped.
    assert overrides == {"rst_n": "low"}
