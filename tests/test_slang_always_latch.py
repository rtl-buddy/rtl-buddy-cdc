"""Coverage tests for slang-frontend ``always_latch`` lowering.

Issue: rtl-buddy-cdc#39 — the slang frontend silently dropped every
``always_latch`` block (no cell emitted). Latches are sometimes
legitimate (ICG enable-latch in the ``clock_gating`` fixture) and
sometimes a CDC vector — either way the analyzer needs to see them.

The tests pin the expected ``$dlatch`` shape: ``EN``/``D``/``Q``
connections matching the body's ``if (en) lhs = rhs;`` single-branch
implicit-hold pattern, plus the ``WIDTH`` / ``EN_POLARITY``
parameters Yosys writes for the same shape. End-to-end coverage on
the clock-gating fixture's ``en_latched`` shape confirms the slang
frontend now produces a netlist with the ICG latch represented.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from rtl_buddy_cdc.frontend import Frontend, elaborate

PYSLANG_INSTALLED = importlib.util.find_spec("pyslang") is not None
if not PYSLANG_INSTALLED:
    pytest.skip(
        "pyslang not installed — slang always_latch tests are gated on it",
        allow_module_level=True,
    )


def _elaborate(tmp_path: Path, src: str, top: str = "m"):
    sv = tmp_path / f"{top}.sv"
    sv.write_text(src)
    return elaborate([sv], top, frontend=Frontend.slang)


def _dlatches(module) -> list:
    return [c for c in module.cells.values() if c.type == "$dlatch"]


def _build_drivers(mod) -> dict:
    drv = {}
    for name, cell in mod.cells.items():
        for port, bits in cell.connections.items():
            if port in ("Q", "Y"):
                for b in bits:
                    if isinstance(b, int):
                        drv[b] = (name, port)
    return drv


# --- synthetic single-arm latch ------------------------------------------


def test_simple_always_latch_emits_dlatch(tmp_path: Path) -> None:
    """``always_latch if (en) q = d;`` must emit a ``$dlatch`` with
    ``EN=en``, ``D=d``, ``Q=q`` and ``EN_POLARITY`` active-high
    (the condition is bare ``en``, no inversion to fold)."""
    src = """
    module m (input logic en, d, output logic q);
        always_latch begin
            if (en) q = d;
        end
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    latches = _dlatches(mod)
    assert len(latches) == 1, (
        f"expected one $dlatch for q; got {len(latches)}. "
        "Pre-fix the slang frontend silently dropped always_latch blocks."
    )
    latch = latches[0]
    en_bit = mod.ports["en"].bits[0]
    d_bit = mod.ports["d"].bits[0]
    q_bit = mod.ports["q"].bits[0]
    assert latch.connections["EN"] == (en_bit,), (
        f"EN should wire to the ``en`` port; got {latch.connections['EN']}"
    )
    assert latch.connections["D"] == (d_bit,)
    assert latch.connections["Q"] == (q_bit,)
    assert latch.parameters.get("EN_POLARITY", "").endswith("1"), (
        f"EN_POLARITY should be active-high for bare ``if (en)``; got "
        f"{latch.parameters.get('EN_POLARITY')!r}"
    )
    width_param = latch.parameters.get("WIDTH")
    assert width_param is not None, "WIDTH parameter missing"
    assert len(width_param) == 32, "WIDTH should use the 32-bit binary encoding"
    assert int(width_param, 2) == 1, f"WIDTH should be 1; got {int(width_param, 2)}"


def test_always_latch_multibit_width(tmp_path: Path) -> None:
    """Multi-bit LHS — ``WIDTH`` parameter must match the latch's
    actual bit width (4) and D/Q tuples must each have 4 bits."""
    src = """
    module m (input logic en, input logic [3:0] d, output logic [3:0] q);
        always_latch begin
            if (en) q = d;
        end
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    latches = _dlatches(mod)
    assert len(latches) == 1
    latch = latches[0]
    assert len(latch.connections["D"]) == 4
    assert len(latch.connections["Q"]) == 4
    width_param = latch.parameters.get("WIDTH", "")
    assert int(width_param, 2) == 4, f"WIDTH should be 4; got {int(width_param, 2)}"


# --- clock_gating ICG shape ----------------------------------------------


def test_clock_gating_icg_latch_shape(tmp_path: Path) -> None:
    """The exact ``clock_gating`` ICG body: ``always_latch if (~clk)
    en_latched = en;``. Must emit a ``$dlatch`` for ``en_latched``
    whose D resolves to ``en`` and whose EN traces back to ``clk``
    (through a ``$not`` for the inversion — the slang frontend models
    the polarity in the upstream cell rather than as ``EN_POLARITY=0``,
    matching how it already lowers conditional expressions
    elsewhere)."""
    src = """
    module m (input logic clk, en, output logic en_latched);
        always_latch begin
            if (~clk) en_latched = en;
        end
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    latches = _dlatches(mod)
    assert len(latches) == 1, f"expected one $dlatch for en_latched; got {len(latches)}"
    latch = latches[0]
    en_bit = mod.ports["en"].bits[0]
    en_latched_bit = mod.ports["en_latched"].bits[0]
    assert latch.connections["D"] == (en_bit,)
    assert latch.connections["Q"] == (en_latched_bit,)
    # EN should resolve back to clk through the $not cell.
    drivers = _build_drivers(mod)
    en_pin = latch.connections["EN"][0]
    en_drv = drivers.get(en_pin)
    assert en_drv is not None, "latch EN must have a driver (the $not on ~clk)"
    not_cell = mod.cells[en_drv[0]]
    assert not_cell.type == "$not", (
        f"~clk should lower to a $not cell driving EN; got {not_cell.type}"
    )
    clk_bit = mod.ports["clk"].bits[0]
    assert clk_bit in not_cell.connections.get("A", ()), (
        "the $not driving EN should consume the clk port bit"
    )


# --- regression: latch is not a flop --------------------------------------


def test_latch_not_classified_as_flop(tmp_path: Path) -> None:
    """``$dlatch`` is intentionally outside ``flops.FF_CELL_TYPES`` —
    latches don't bound clock domains and are transparent to
    ``find_crossings``. Confirm the new cell type doesn't sneak into
    the flop set (which would change rule-pack semantics)."""
    from rtl_buddy_cdc import flops

    assert "$dlatch" not in flops.FF_CELL_TYPES, (
        "$dlatch must stay out of FF_CELL_TYPES — latches are transparent "
        "to the domain-assignment BFS by design (issue #39 out-of-scope)."
    )

    src = """
    module m (input logic en, d, output logic q);
        always_latch begin
            if (en) q = d;
        end
    endmodule
    """
    mod = _elaborate(tmp_path, src)
    flop_list = flops.find_flops(mod)
    assert flop_list == [], (
        f"latch should not be discoverable as a flop; got {flop_list}"
    )


# --- end-to-end: clock_gating fixture under slang frontend ---------------


def test_clock_gating_fixture_under_slang() -> None:
    """End-to-end: elaborate the clock_gating fixture via slang (not
    the pre-built Yosys JSON) and confirm assign_domains resolves the
    flop's clock back to ``clk`` and the rule pack stays silent —
    same acceptance the Yosys frontend already meets in
    :mod:`tests.test_clock_gating`. Pre-fix the always_latch was
    dropped so ``en_latched`` had no driver and the clock-network
    resolution could diverge from the Yosys path."""
    fixture = Path(__file__).parent / "fixtures" / "clock_gating" / "clock_gating.sv"
    sdc = Path(__file__).parent / "fixtures" / "clock_gating" / "clock_gating.sdc"
    if not fixture.exists() or not sdc.exists():
        pytest.skip("clock_gating fixture missing")

    from rtl_buddy_cdc import sdc as sdc_mod
    from rtl_buddy_cdc.domain import assign_domains, find_crossings
    from rtl_buddy_cdc.rules import run_all as run_all_rules

    mod = elaborate([fixture], "clock_gating", frontend=Frontend.slang)
    # The ICG latch should now be present.
    assert len(_dlatches(mod)) == 1, (
        f"expected one $dlatch for the ICG enable; got "
        f"{[c.type for c in mod.cells.values()]}"
    )
    spec = sdc_mod.parse_file(sdc)
    domains = assign_domains(mod)
    assert len(domains) == 1
    assert domains[0].clock == "clk", (
        f"flop's domain should resolve to clk through the ICG; got {domains[0].clock!r}"
    )
    crossings = find_crossings(mod)
    violations = run_all_rules(mod, crossings, spec)
    assert violations == [], (
        f"clock_gating must stay silent under the slang frontend; got "
        f"{[(v.rule_id, v.message) for v in violations]}"
    )
