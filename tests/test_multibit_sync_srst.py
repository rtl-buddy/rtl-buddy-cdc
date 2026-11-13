"""The multi-bit synchroniser first-stage test looks through a
synchronous-reset ``$mux`` between the stages (rtl-buddy-cdc#366).

``meta <= gray; q <= meta; if (rst) begin meta <= '0; q <= '0; end``
lowers, after ``proc``, to ``meta.Q → $mux → q.D``. The lane-for-lane
test in ``_is_multibit_sync_first_stage`` compared ``meta.Q`` with
``q.D`` and missed the chain, so a gray pointer into such a
synchroniser reported CDC-004. The look-through reuses the single-bit
walk's shape test (``_constant_leg_reset_mux_data_bits``) and adds one
condition: the mux select must be local to the destination (dst-domain
flops, a primary port, or a constant).

Synthetic cells pin each accept / reject branch; ``good_gray_bus_sync_srst``
pins the end-to-end verdict on both the ``proc`` and the ``opt_dff``
(``$sdff``) netlists.

Bit numbering: stage 1 ``Q`` = 10, 11; stage 2 ``D`` = 20, 21 (the mux
``Y``) and ``Q`` = 30, 31; the select is bit 99.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rtl_buddy_cdc import netlist, sdc as sdc_mod
from rtl_buddy_cdc.domain import find_crossings
from rtl_buddy_cdc.flops import Flop
from rtl_buddy_cdc.netlist import Bit, Cell, Module, Port
from rtl_buddy_cdc.rules import _is_multibit_sync_first_stage, run_all

FIX = Path(__file__).parent / "fixtures" / "good_gray_bus_sync_srst"
Q1: tuple[Bit, ...] = (10, 11)
D2: tuple[Bit, ...] = (20, 21)
Q2: tuple[Bit, ...] = (30, 31)
SEL: Bit = 99


def _flop(name: str, d: tuple[Bit, ...], q: tuple[Bit, ...], clk: Bit = 1) -> Flop:
    cell = Cell(name=name, type="$dff", connections={"CLK": (clk,), "D": d, "Q": q})
    return Flop(cell=cell, clk=clk, d=d, q=q)


def _chain(
    a: tuple[Bit, ...] = Q1,
    b: tuple[Bit, ...] = ("0", "0"),
    *,
    sel_driver: str | None = None,
    mux_type: str = "$mux",
    extra: tuple[Cell, ...] = (),
) -> tuple[Module, Flop, dict[str, str | None]]:
    """Stage 1 (``s1``, the crossing's destination) → ``$mux`` → stage 2
    (``s2``). ``sel_driver``: ``None`` = the select is a primary input
    port, ``"dst"`` / ``"src"`` = a flop of that domain drives it."""
    s1 = _flop("s1", (5, 6), Q1)
    s2 = _flop("s2", D2, Q2)
    mux = Cell(
        name="rmux",
        type=mux_type,
        connections={"A": a, "B": b, "S": (SEL,), "Y": D2},
    )
    cells = {c.name: c for c in (s1.cell, s2.cell, mux, *extra)}
    domains: dict[str, str | None] = {"s1": "dst", "s2": "dst"}
    ports = {"src": Port(name="src", direction="input", bits=(5, 6))}
    if sel_driver is None:
        ports["rst"] = Port(name="rst", direction="input", bits=(SEL,))
    else:
        r = _flop("rflop", (98,), (SEL,), clk=2 if sel_driver == "src" else 1)
        cells[r.cell.name] = r.cell
        domains["rflop"] = sel_driver
    return Module(name="m", ports=ports, cells=cells, netnames={}), s1, domains


@pytest.mark.parametrize("sel_driver", [None, "dst"], ids=["port_reset", "dst_reset"])
@pytest.mark.parametrize("const_on", ["B", "A"], ids=["active_high", "active_low"])
def test_reset_mux_between_stages_is_looked_through(
    sel_driver: str | None, const_on: str
) -> None:
    if const_on == "B":
        module, s1, domains = _chain(Q1, ("0", "0"), sel_driver=sel_driver)
    else:
        module, s1, domains = _chain(("1", "0"), Q1, sel_driver=sel_driver)
    assert _is_multibit_sync_first_stage(module, s1, "dst", domains)


def test_constant_select_is_accepted() -> None:
    module, s1, domains = _chain()
    mux = module.cells["rmux"]
    mux.connections["S"] = ("0",)
    assert _is_multibit_sync_first_stage(module, s1, "dst", domains)


def test_foreign_domain_select_is_not_a_reset() -> None:
    """A constant-leg mux selected from the source domain is a second
    crossing into stage 2, not this synchroniser's reset."""
    module, s1, domains = _chain(sel_driver="src")
    assert not _is_multibit_sync_first_stage(module, s1, "dst", domains)


def test_data_select_mux_is_not_looked_through() -> None:
    """Both legs live: the mux picks between two data sources."""
    other = _flop("other", (7, 8), (40, 41))
    module, s1, domains = _chain(Q1, (40, 41), sel_driver="dst", extra=(other.cell,))
    domains["other"] = "dst"
    assert not _is_multibit_sync_first_stage(module, s1, "dst", domains)


def test_enable_mux_is_not_looked_through() -> None:
    """The other leg is stage 2's own Q: an enable, not a reset."""
    module, s1, domains = _chain(Q2, Q1, sel_driver="dst")
    assert not _is_multibit_sync_first_stage(module, s1, "dst", domains)


def test_pmux_is_not_looked_through() -> None:
    module, s1, domains = _chain(mux_type="$pmux")
    assert not _is_multibit_sync_first_stage(module, s1, "dst", domains)


def test_mux_output_read_elsewhere_is_not_looked_through() -> None:
    tap = Cell(name="tap", type="$not", connections={"A": (20,), "Y": (50,)})
    module, s1, domains = _chain(extra=(tap,))
    assert not _is_multibit_sync_first_stage(module, s1, "dst", domains)


def test_lane_permuted_leg_is_not_looked_through() -> None:
    module, s1, domains = _chain((11, 10))
    assert not _is_multibit_sync_first_stage(module, s1, "dst", domains)


def test_next_stage_in_another_domain_is_rejected() -> None:
    module, s1, domains = _chain()
    domains["s2"] = "src"
    assert not _is_multibit_sync_first_stage(module, s1, "dst", domains)


def _analyse(json_name: str) -> list[str]:
    module = netlist.load(FIX / json_name)
    spec = sdc_mod.parse_file(FIX / "good_gray_bus_sync_srst.sdc")
    sdc_mod.synthesize_unconstrained_inputs(spec, module)
    crossings = [
        c
        for c in find_crossings(module, port_clock=spec.port_clock)
        if spec.are_async(c.src_clock, c.dst_clock)
    ]
    assert len(crossings) == 2
    assert all(c.width == 4 for c in crossings)
    return [v.rule_id for v in run_all(module, crossings, spec)]


def test_fixture_proc_form_carries_reset_muxes_and_is_clean() -> None:
    module = netlist.load(FIX / "good_gray_bus_sync_srst.json")
    assert sum(c.type == "$mux" for c in module.cells.values()) == 7
    assert _analyse("good_gray_bus_sync_srst.json") == []


def test_fixture_opt_dff_form_is_sdff_and_clean() -> None:
    module = netlist.load(FIX / "good_gray_bus_sync_srst.optdff.json")
    assert sum(c.type == "$sdff" for c in module.cells.values()) == 7
    assert _analyse("good_gray_bus_sync_srst.optdff.json") == []


def test_binary_source_into_the_reset_synchroniser_still_fires() -> None:
    """Control: the look-through only proves the destination is a
    synchroniser. A binary (non-gray) source into the port-reset chain is
    still CDC-004."""
    module = netlist.load(FIX / "good_gray_bus_sync_srst.json")
    cells = dict(module.cells)
    for name, cell in module.cells.items():
        if cell.type == "$xor":
            plain = next(
                bits
                for bits in (cell.connections["A"], cell.connections["B"])
                if isinstance(bits[-1], int)
            )
            cells[name] = Cell(
                name=name,
                type="$pos",
                connections={"A": plain, "Y": cell.connections["Y"]},
            )
    module = Module(
        name=module.name, ports=module.ports, cells=cells, netnames=module.netnames
    )
    spec = sdc_mod.parse_file(FIX / "good_gray_bus_sync_srst.sdc")
    sdc_mod.synthesize_unconstrained_inputs(spec, module)
    crossings = [
        c
        for c in find_crossings(module, port_clock=spec.port_clock)
        if spec.are_async(c.src_clock, c.dst_clock)
    ]
    fired = [v for v in run_all(module, crossings, spec) if v.rule_id == "CDC-004"]
    assert len(fired) >= 1
