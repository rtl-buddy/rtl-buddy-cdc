"""Recognition of the DFT / scan-mode SV attributes (issue #44).

Phase 1 is **recognition only**: :data:`~rtl_buddy_cdc.rules.SCAN_MODE_ATTRS`
and :func:`~rtl_buddy_cdc.rules.scan_mode_port_names` name the ports a
DFT insertion flow marks, and nothing in the rule pack consults them
yet. The paired suppression behaviour lands in issue #45, so these
tests pin the helper's contract in isolation — hand-built ``Module``
dataclasses, no fixture, no toolchain — in the self-contained style of
``tests/test_cov_rules_c.py``.
"""

from __future__ import annotations

import pytest

from rtl_buddy_cdc.netlist import Cell, Module, Netname, Port
from rtl_buddy_cdc.rules import SCAN_MODE_ATTRS, scan_mode_port_names


def _module_with(
    *,
    port_attrs: dict[str, str] | None = None,
    internal_attrs: dict[str, str] | None = None,
    direction: str = "input",
) -> Module:
    """A one-port, one-flop design whose ``scan_en`` port and internal
    ``muxed_clk`` net carry the attributes under test."""
    scan_en, func_clk, d, q = 1, 2, 3, 4
    return Module(
        name="m",
        ports={
            "func_clk": Port(name="func_clk", direction="input", bits=(func_clk,)),
            "scan_en": Port(name="scan_en", direction=direction, bits=(scan_en,)),
        },
        cells={
            "f": Cell(
                name="f",
                type="$dff",
                connections={"CLK": (func_clk,), "D": (d,), "Q": (q,)},
            ),
        },
        netnames={
            "func_clk": Netname(name="func_clk", bits=(func_clk,), attributes={}),
            "scan_en": Netname(
                name="scan_en", bits=(scan_en,), attributes=port_attrs or {}
            ),
            "muxed_clk": Netname(
                name="muxed_clk", bits=(9,), attributes=internal_attrs or {}
            ),
        },
    )


def test_scan_en_on_input_port_is_recognised() -> None:
    """The headline case: ``(* scan_en *) input logic scan_en`` — Yosys
    lands the attribute on the port's netname, and the helper reports
    the port name."""
    module = _module_with(port_attrs={"scan_en": "1"})
    assert scan_mode_port_names(module) == {"scan_en"}


@pytest.mark.parametrize("attr", sorted(SCAN_MODE_ATTRS))
def test_every_declared_alias_is_recognised(attr: str) -> None:
    """Each member of ``SCAN_MODE_ATTRS`` is honoured, not just the
    canonical ``scan_en`` spelling."""
    module = _module_with(port_attrs={attr: "1"})
    assert scan_mode_port_names(module) == {"scan_en"}


@pytest.mark.parametrize("spelling", ["SCAN_EN", "Scan_En", "TEST_MODE"])
def test_attribute_name_match_is_case_insensitive(spelling: str) -> None:
    """DFT insertion flows spell the signal in every case convention;
    the match follows ``user_sync_flop_names`` (#275) and lowercases."""
    module = _module_with(port_attrs={spelling: "TRUE"})
    assert scan_mode_port_names(module) == {"scan_en"}


def test_attribute_on_a_non_port_net_does_not_count() -> None:
    """A scan attribute on an internal net conveys nothing the rule
    pack can act on — a scan-enable is by definition an external
    test-mode pin — so it is ignored rather than guessed at."""
    module = _module_with(internal_attrs={"scan_en": "1"})
    assert scan_mode_port_names(module) == set()


def test_attribute_on_an_output_port_does_not_count() -> None:
    """Only *input* ports qualify, mirroring
    ``user_reset_polarity_overrides``' port-direction filter."""
    module = _module_with(port_attrs={"scan_en": "1"}, direction="output")
    assert scan_mode_port_names(module) == set()


def test_no_attributes_yields_empty_set() -> None:
    """The overwhelmingly common case — a design with no DFT
    annotation at all — costs nothing and reports nothing."""
    assert scan_mode_port_names(_module_with()) == set()


def test_unrelated_attribute_on_a_port_is_ignored() -> None:
    """A port carrying some *other* attribute (``reset_polarity``, a
    Yosys ``src`` stamp) is not a scan port."""
    module = _module_with(port_attrs={"reset_polarity": "low", "src": "t.sv:4"})
    assert scan_mode_port_names(module) == set()
