"""Coverage-focused tests for the RDC pack, the clock-network /
reset-fanin structural helpers, and the synchroniser-shape rules
(CDC-006 / CDC-010 / CDC-015 / CDC-016 / CDC-017 / CDC-019 /
CDC-020) in :mod:`rtl_buddy_cdc.rules`.

These exercise paths the committed paired fixtures don't reach on
their own:

* the **lazy-context** branch (``ctx=None``) in every ``check_rdc_NNN``
  / ``check_cdc_NNN`` here — driving the rule directly so it builds
  its own ``_RuleContext`` (and the no-SDC ``a != b`` async fallback);
* the early-return guards in the backward/forward fanin walkers
  (``_backward_port_fanin``, ``_forward_reachable_flops``,
  ``_forward_reachable_cells``) — port hits, ``Q`` boundaries,
  non-int / constant bits;
* the gray-encoding / gated-bus / bus-buffer helpers
  (``_is_gray_encoded_source``, ``_is_gated_bus_crossing``,
  ``_trace_through_bus_buffers``) early-out shapes;
* the ``user_*_flop_names`` attribute-walk inner ``add`` lines;
* ``_clk_polarity`` resolution order (parameter / gate-level segment /
  fallback) and ``_trailing_bit`` decoding;
* the XOR-tail pulse-recovery recogniser
  (``_has_xor_tail_pulse_recovery``) negative shapes.

Everything is built either from a committed Yosys-JSON fixture
(``netlist.load`` — no toolchain) or from hand-constructed
``Module`` / ``Cell`` / ``Netname`` / ``Port`` dataclasses, mirroring
the self-contained style of ``tests/test_cov_rules_a.py`` and
``tests/test_cov_rules_b.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rtl_buddy_cdc import netlist, sdc as sdc_mod
from rtl_buddy_cdc.domain import Crossing, find_crossings
from rtl_buddy_cdc.flops import Flop, find_flops
from rtl_buddy_cdc.netlist import Cell, Module, Netname, Port
from rtl_buddy_cdc.rules import (
    _backward_port_fanin,  # noqa: PLC2701
    _build_context,  # noqa: PLC2701
    _chain_has_inter_stage_comb,  # noqa: PLC2701
    _clk_polarity,  # noqa: PLC2701
    _sync_chain_depth,  # noqa: PLC2701
    _sync_chain_flops,  # noqa: PLC2701
    user_glitchless_clock_mux_bits,
    user_reset_sync_flop_names,
    user_sync_flop_names,
    _forward_reachable_cells,  # noqa: PLC2701
    _forward_reachable_flops,  # noqa: PLC2701
    _has_xor_tail_pulse_recovery,  # noqa: PLC2701
    _is_gated_bus_crossing,  # noqa: PLC2701
    _is_gray_encoded_source,  # noqa: PLC2701
    _trace_through_bus_buffers,  # noqa: PLC2701
    _trailing_bit,  # noqa: PLC2701
    check_cdc_003,
    check_cdc_006,
    check_cdc_009,
    check_cdc_012,
    check_cdc_013,
    check_cdc_014,
    check_cdc_015,
    check_cdc_016,
    check_cdc_017,
    check_cdc_019,
    check_cdc_020,
    check_rdc_001,
    check_rdc_003,
    check_rdc_004,
    check_rdc_005,
    check_rdc_006,
    check_rdc_008,
    user_gray_flop_names,
    user_handshake_flop_names,
    user_static_flop_names,
)
from rtl_buddy_cdc.sdc import parse as parse_sdc

FIX_ROOT = Path(__file__).parent / "fixtures"


# --- shared helpers ---------------------------------------------------------


def _load_fixture(name: str) -> tuple[Module, sdc_mod.ClockSpec]:
    """Load a committed fixture's netlist + SDC. Skips if not built."""
    fix_dir = FIX_ROOT / name
    json_path = fix_dir / f"{name}.json"
    sdc_path = fix_dir / f"{name}.sdc"
    if not json_path.exists():
        pytest.skip(f"fixture not built: {json_path}")
    module = netlist.load(json_path)
    spec = sdc_mod.parse_file(sdc_path)
    sdc_mod.synthesize_unconstrained_inputs(spec, module)
    return module, spec


def _async_crossings(module: Module, spec: sdc_mod.ClockSpec) -> list[Crossing]:
    crossings = find_crossings(module, port_clock=spec.port_clock)
    out: list[Crossing] = []
    for c in crossings:
        a = spec.clock_for_port(c.src_clock) or c.src_clock
        b = spec.clock_for_port(c.dst_clock) or c.dst_clock
        if spec.is_unreachable_crossing(a, b):
            continue
        if spec.are_async(a, b):
            out.append(c)
    return out


def _flop(name: str, *, clk: int, d: tuple[int, ...], q: tuple[int, ...]) -> Flop:
    cell = Cell(name=name, type="$dff", connections={"CLK": (clk,), "D": d, "Q": q})
    return Flop(cell=cell, clk=clk, d=d, q=q)


# --- RDC lazy-context (ctx=None) direct calls -------------------------------
#
# Every fixture-driven RDC test in test_cov_rules_b goes through
# run_all, which pre-builds the context. Calling each rule directly
# with ctx=None exercises the ``if ctx is None: ctx = _build_context``
# lazy branch and the standalone return path.


def test_rdc_001_lazy_context_fires() -> None:
    """``check_rdc_001`` called directly (``ctx=None``) builds its own
    context and still fires on the async ARST crossing fixture."""
    module, spec = _load_fixture("bad_reset_crossing")
    violations = check_rdc_001(module, [], spec)  # no ctx=
    rdc_001 = [v for v in violations if v.rule_id == "RDC-001"]
    assert len(rdc_001) >= 1
    assert rdc_001[0].severity == "error"
    assert "async reset crossing" in rdc_001[0].message


def test_rdc_003_lazy_context_fires() -> None:
    """``check_rdc_003`` direct call builds its own context and fires on
    the SRST-crossing fixture."""
    module, spec = _load_fixture("bad_rdc_003_sync_reset_crossing")
    violations = check_rdc_003(module, [], spec)  # no ctx=
    rdc_003 = [v for v in violations if v.rule_id == "RDC-003"]
    assert len(rdc_003) == 1
    assert "sync reset crossing" in rdc_003[0].message
    assert "SRST" in rdc_003[0].message


def test_rdc_004_lazy_context_fires() -> None:
    """``check_rdc_004`` direct call builds its own context and fires on
    the comb-driven-reset fixture, listing the fanin flops."""
    module, spec = _load_fixture("bad_rdc_004_comb_driven_reset")
    violations = check_rdc_004(module, [], spec)  # no ctx=
    rdc_004 = [v for v in violations if v.rule_id == "RDC-004"]
    assert len(rdc_004) == 1
    assert "reset driven by combinational logic" in rdc_004[0].message


def test_rdc_005_lazy_context_fires() -> None:
    """``check_rdc_005`` direct call builds its own context and warns on
    the multi-source-reset fixture, naming both reset ports."""
    module, spec = _load_fixture("bad_rdc_005_multi_source_reset")
    violations = check_rdc_005(module, [], spec)  # no ctx=
    rdc_005 = [v for v in violations if v.rule_id == "RDC-005"]
    assert len(rdc_005) == 1
    assert rdc_005[0].severity == "warning"
    assert "multiple reset sources converging" in rdc_005[0].message


def test_rdc_006_lazy_context_fires() -> None:
    """``check_rdc_006`` direct call builds its own context and warns on
    the muxed-async-reset fixture."""
    module, spec = _load_fixture("bad_derived_async_reset_unsync")
    violations = check_rdc_006(module, [], spec)  # no ctx=
    rdc_006 = [v for v in violations if v.rule_id == "RDC-006"]
    assert len(rdc_006) == 1
    assert rdc_006[0].severity == "warning"
    assert "muxed async reset without local synchroniser" in rdc_006[0].message


def test_rdc_008_lazy_context_fires() -> None:
    """``check_rdc_008`` direct call builds its own context and fires on
    the unsynced-primary-reset fixture."""
    module, spec = _load_fixture("bad_primary_reset_unsynced")
    violations = check_rdc_008(module, [], spec)  # no ctx=
    rdc_008 = [v for v in violations if v.rule_id == "RDC-008"]
    assert len(rdc_008) == 1
    assert "unsynced primary-reset port" in rdc_008[0].message


# --- RDC no-SDC async fallback (clock_spec=None → a != b) -------------------


def test_rdc_001_no_sdc_async_fallback_fires() -> None:
    """With ``clock_spec=None`` RDC-001 treats every distinct flop domain
    as async (the ``a != b`` fallback). A flop in clk_a whose ARST is
    driven by a flop in clk_b fires without any SDC."""
    clk_a, clk_b = 2, 3
    src_q = 4  # driven by the clk_b flop
    dst_q = 5
    module = Module(
        name="rdc_no_sdc",
        ports={
            "clk_a": Port(name="clk_a", direction="input", bits=(clk_a,)),
            "clk_b": Port(name="clk_b", direction="input", bits=(clk_b,)),
        },
        cells={
            # Source flop in clk_b — drives the foreign ARST.
            "src": Cell(
                name="src",
                type="$dff",
                connections={"CLK": (clk_b,), "D": (src_q,), "Q": (src_q,)},
            ),
            # Destination flop in clk_a with an async reset from src.Q.
            "dst": Cell(
                name="dst",
                type="$adff",
                connections={
                    "CLK": (clk_a,),
                    "ARST": (src_q,),
                    "D": (6,),
                    "Q": (dst_q,),
                },
            ),
        },
        netnames={},
    )
    violations = check_rdc_001(module, [], clock_spec=None)  # lazy ctx + a!=b
    rdc_001 = [v for v in violations if v.rule_id == "RDC-001"]
    assert len(rdc_001) == 1
    assert "clk=clk_a" in rdc_001[0].message
    assert "clk=clk_b" in rdc_001[0].message
    assert rdc_001[0].cell_name == "dst"


def test_rdc_001_reset_tree_grouping_truncates_destination_list() -> None:
    """A single foreign-domain source feeding >3 destination flops in
    one domain becomes ONE finding whose message summarises the reset
    distribution tree and truncates the named destinations with
    ', ...' (the ``len(dsts) > 3`` branch)."""
    clk_a, clk_b = 2, 3
    src_q = 4
    module_cells = {
        "src": Cell(
            name="src",
            type="$dff",
            connections={"CLK": (clk_b,), "D": (src_q,), "Q": (src_q,)},
        ),
    }
    # Four destination flops in clk_a all reset from src.Q.
    for i in range(4):
        name = f"dst{i}"
        q = 10 + i
        module_cells[name] = Cell(
            name=name,
            type="$adff",
            connections={"CLK": (clk_a,), "ARST": (src_q,), "D": (20 + i,), "Q": (q,)},
        )
    module = Module(
        name="rdc_tree",
        ports={
            "clk_a": Port(name="clk_a", direction="input", bits=(clk_a,)),
            "clk_b": Port(name="clk_b", direction="input", bits=(clk_b,)),
        },
        cells=module_cells,
        netnames={},
    )
    violations = check_rdc_001(module, [], clock_spec=None)
    rdc_001 = [v for v in violations if v.rule_id == "RDC-001"]
    assert len(rdc_001) == 1
    msg = rdc_001[0].message
    assert "4 destination flops share this source" in msg
    assert "reset distribution tree" in msg
    assert ", ..." in msg


# --- _backward_port_fanin guard branches ------------------------------------


def test_backward_port_fanin_reaches_two_ports_through_comb() -> None:
    """An ``$and`` of two top-level input ports yields both port names —
    the positive path RDC-005 relies on."""
    pa, pb, y = 2, 3, 4
    module = Module(
        name="m",
        ports={
            "rst_a": Port(name="rst_a", direction="input", bits=(pa,)),
            "rst_b": Port(name="rst_b", direction="input", bits=(pb,)),
        },
        cells={
            "g": Cell(
                name="g", type="$and", connections={"A": (pa,), "B": (pb,), "Y": (y,)}
            ),
        },
        netnames={},
    )
    ctx = _build_context(module, clock_spec=None)
    ports = _backward_port_fanin(module, (y,), ctx.bit_drivers)
    assert ports == {"rst_a", "rst_b"}


def test_backward_port_fanin_stops_at_flop_q() -> None:
    """A comb cell fed by a flop's ``Q`` contributes no port — the walk
    stops at the ``port_name == 'Q'`` boundary (rules.py line ~2598)."""
    clk, ff_q, y = 2, 3, 4
    module = Module(
        name="m",
        ports={"clk": Port(name="clk", direction="input", bits=(clk,))},
        cells={
            "ff": Cell(
                name="ff",
                type="$dff",
                connections={"CLK": (clk,), "D": (ff_q,), "Q": (ff_q,)},
            ),
            "g": Cell(name="g", type="$not", connections={"A": (ff_q,), "Y": (y,)}),
        },
        netnames={},
    )
    ctx = _build_context(module, clock_spec=None)
    assert _backward_port_fanin(module, (y,), ctx.bit_drivers) == set()


def test_backward_port_fanin_undriven_bit_yields_nothing() -> None:
    """A start bit that is neither a port nor a cell output (no driver,
    not a port) contributes nothing (the ``drv is None`` continue)."""
    module = Module(name="m", ports={}, cells={}, netnames={})
    ctx = _build_context(module, clock_spec=None)
    assert _backward_port_fanin(module, (99,), ctx.bit_drivers) == set()


# --- _forward_reachable_flops / _forward_reachable_cells guards -------------


def test_forward_reachable_flops_records_only_d_pin() -> None:
    """A flop reached on its ``D`` pin is recorded; the same flop reached
    only on a non-D pin (e.g. ``EN``) is not — the ``port_name == 'D'``
    gate. Two flops, one wired D-side, one wired EN-side."""
    clk = 2
    src_q = 3
    d_flop_q = 4
    en_flop_q = 5
    module = Module(
        name="m",
        ports={"clk": Port(name="clk", direction="input", bits=(clk,))},
        cells={
            "src": Cell(
                name="src",
                type="$dff",
                connections={"CLK": (clk,), "D": (10,), "Q": (src_q,)},
            ),
            # Reached on its D pin → recorded.
            "d_flop": Cell(
                name="d_flop",
                type="$dff",
                connections={"CLK": (clk,), "D": (src_q,), "Q": (d_flop_q,)},
            ),
            # Reached only on its EN pin → not recorded.
            "en_flop": Cell(
                name="en_flop",
                type="$dffe",
                connections={
                    "CLK": (clk,),
                    "EN": (src_q,),
                    "D": (11,),
                    "Q": (en_flop_q,),
                },
            ),
        },
        netnames={},
    )
    ctx = _build_context(module, clock_spec=None)
    reached = _forward_reachable_flops(module, (src_q,), ctx.bit_consumers)
    assert "d_flop" in reached
    assert "en_flop" not in reached


def test_forward_reachable_cells_records_comb_and_flop() -> None:
    """``_forward_reachable_cells`` records EVERY consumer cell — the comb
    gate AND the downstream flop — unlike the flop-only variant."""
    clk = 2
    src_q = 3
    gate_y = 4
    module = Module(
        name="m",
        ports={"clk": Port(name="clk", direction="input", bits=(clk,))},
        cells={
            "src": Cell(
                name="src",
                type="$dff",
                connections={"CLK": (clk,), "D": (10,), "Q": (src_q,)},
            ),
            "gate": Cell(
                name="gate", type="$not", connections={"A": (src_q,), "Y": (gate_y,)}
            ),
            "dst": Cell(
                name="dst",
                type="$dff",
                connections={"CLK": (clk,), "D": (gate_y,), "Q": (5,)},
            ),
        },
        netnames={},
    )
    ctx = _build_context(module, clock_spec=None)
    reached = _forward_reachable_cells(module, (src_q,), ctx.bit_consumers)
    assert "gate" in reached
    assert "dst" in reached


# --- _trace_through_bus_buffers / _is_gated_bus_crossing guards -------------


def test_trace_through_bus_buffers_undriven_returns_none() -> None:
    """A bit with no driver returns ``None`` immediately (the ``drv is
    None`` early-out)."""
    module = Module(name="m", ports={}, cells={}, netnames={})
    ctx = _build_context(module, clock_spec=None)
    assert _trace_through_bus_buffers(module, 7, ctx.bit_drivers) is None


def test_trace_through_bus_buffers_passes_through_one_buffer() -> None:
    """A single transparent ``$pos`` buffer is stepped through: the
    returned driver is the buffer's input source, not the buffer."""
    src_q, buf_y = 3, 4
    module = Module(
        name="m",
        ports={"clk": Port(name="clk", direction="input", bits=(2,))},
        cells={
            "src": Cell(
                name="src",
                type="$dff",
                connections={"CLK": (2,), "D": (10,), "Q": (src_q,)},
            ),
            "buf": Cell(
                name="buf", type="$pos", connections={"A": (src_q,), "Y": (buf_y,)}
            ),
        },
        netnames={},
    )
    ctx = _build_context(module, clock_spec=None)
    drv = _trace_through_bus_buffers(module, buf_y, ctx.bit_drivers)
    assert drv is not None
    assert drv[0] == "src"  # stepped through the buffer to its source


def test_is_gated_bus_crossing_dffe_en_from_dst_domain_is_gated() -> None:
    """A ``$dffe`` destination whose ``EN`` fans in only from dst-domain
    flops is recognised as a gated bus crossing (Shape 1)."""
    dst_clk_bit = 2
    en_q = 3
    src_q0, src_q1 = 4, 5
    dst_q0, dst_q1 = 6, 7
    module = Module(
        name="m",
        ports={"dclk": Port(name="dclk", direction="input", bits=(dst_clk_bit,))},
        cells={
            # dst-domain flop driving the enable.
            "en_ff": Cell(
                name="en_ff",
                type="$dff",
                connections={"CLK": (dst_clk_bit,), "D": (20,), "Q": (en_q,)},
            ),
            # the gated bus capture flop with EN.
            "dst": Cell(
                name="dst",
                type="$dffe",
                connections={
                    "CLK": (dst_clk_bit,),
                    "EN": (en_q,),
                    "D": (src_q0, src_q1),
                    "Q": (dst_q0, dst_q1),
                },
            ),
        },
        netnames={},
    )
    dst_flop = next(f for f in find_flops(module) if f.cell.name == "dst")
    crossing = Crossing(
        src_clock="sclk",
        dst_flop=dst_flop,
        dst_clock="dclk",
        min_hops=1,
        width=2,
        src_flop=None,
    )
    ctx = _build_context(module, clock_spec=None)
    assert _is_gated_bus_crossing(module, crossing, ctx.domains, ctx.bit_drivers)


def test_is_gated_bus_crossing_split_driver_not_gated() -> None:
    """A destination bus whose D bits trace to two *different* origin
    cells can't be a single gating mux — the ``len(driver_cells) != 1``
    guard returns False."""
    dclk = 2
    d0, d1 = 3, 4
    module = Module(
        name="m",
        ports={"dclk": Port(name="dclk", direction="input", bits=(dclk,))},
        cells={
            "g0": Cell(name="g0", type="$not", connections={"A": (30,), "Y": (d0,)}),
            "g1": Cell(name="g1", type="$not", connections={"A": (31,), "Y": (d1,)}),
            "dst": Cell(
                name="dst",
                type="$dff",
                connections={"CLK": (dclk,), "D": (d0, d1), "Q": (5, 6)},
            ),
        },
        netnames={},
    )
    dst_flop = next(f for f in find_flops(module) if f.cell.name == "dst")
    crossing = Crossing(
        src_clock="sclk",
        dst_flop=dst_flop,
        dst_clock="dclk",
        min_hops=1,
        width=2,
        src_flop=None,
    )
    ctx = _build_context(module, clock_spec=None)
    assert not _is_gated_bus_crossing(module, crossing, ctx.domains, ctx.bit_drivers)


# --- _is_gray_encoded_source guards -----------------------------------------


def test_is_gray_encoded_source_recognises_xor_shift_signature() -> None:
    """A source flop whose D is an ``$xor`` with the ``A[i+1] == B[i]``
    shift signature and a constant MSB pad is recognised as gray-coded
    (the canonical ``g = b ^ (b >> 1)`` pattern)."""
    clk = 2
    # binary value bits b0,b1,b2
    b0, b1, b2 = 3, 4, 5
    g0, g1, g2 = 6, 7, 8  # xor output = gray
    xor = Cell(
        name="gray_xor",
        type="$xor",
        # A = (b0,b1,b2) ; B = (b1,b2,'0')  → A[i+1]==B[i]
        connections={"A": (b0, b1, b2), "B": (b1, b2, "0"), "Y": (g0, g1, g2)},
    )
    src = Cell(
        name="src",
        type="$dff",
        connections={"CLK": (clk,), "D": (g0, g1, g2), "Q": (10, 11, 12)},
    )
    module = Module(
        name="m",
        ports={"clk": Port(name="clk", direction="input", bits=(clk,))},
        cells={"gray_xor": xor, "src": src},
        netnames={},
    )
    src_flop = next(f for f in find_flops(module) if f.cell.name == "src")
    ctx = _build_context(module, clock_spec=None)
    assert _is_gray_encoded_source(module, src_flop, ctx.bit_drivers)


def test_is_gray_encoded_source_stops_at_flop_q_no_match() -> None:
    """When the source flop's D is just another flop's Q (no XOR in the
    fanin), the walk stops at the ``port_name == 'Q'`` boundary and
    returns False — the binary-pointer (non-gray) case."""
    clk = 2
    upstream_q = 3
    module = Module(
        name="m",
        ports={"clk": Port(name="clk", direction="input", bits=(clk,))},
        cells={
            "up": Cell(
                name="up",
                type="$dff",
                connections={"CLK": (clk,), "D": (10,), "Q": (upstream_q,)},
            ),
            "src": Cell(
                name="src",
                type="$dff",
                connections={"CLK": (clk,), "D": (upstream_q, 11), "Q": (12, 13)},
            ),
        },
        netnames={},
    )
    src_flop = next(f for f in find_flops(module) if f.cell.name == "src")
    ctx = _build_context(module, clock_spec=None)
    assert not _is_gray_encoded_source(module, src_flop, ctx.bit_drivers)


# --- user_*_flop_names attribute-walk inner add lines -----------------------


def test_user_gray_flop_names_maps_tagged_netname_to_flop() -> None:
    """A netname carrying ``(* cdc_gray *)`` over a flop's Q bits maps
    that flop into the gray set — exercising the ``isinstance(b, int)``
    add and the Q-match loop."""
    clk, q = 2, 3
    module = Module(
        name="m",
        ports={"clk": Port(name="clk", direction="input", bits=(clk,))},
        cells={
            "g": Cell(
                name="g",
                type="$dff",
                connections={"CLK": (clk,), "D": (10,), "Q": (q,)},
            ),
        },
        netnames={
            "gray_q": Netname(name="gray_q", bits=(q,), attributes={"cdc_gray": "1"}),
        },
    )
    assert user_gray_flop_names(module) == {"g"}


def test_user_static_flop_names_maps_tagged_netname_to_flop() -> None:
    """A ``(* cdc_static *)``-tagged netname over a flop's Q maps that
    flop into the static set."""
    clk, q = 2, 3
    module = Module(
        name="m",
        ports={"clk": Port(name="clk", direction="input", bits=(clk,))},
        cells={
            "s": Cell(
                name="s",
                type="$dff",
                connections={"CLK": (clk,), "D": (10,), "Q": (q,)},
            ),
        },
        netnames={
            "cfg_q": Netname(name="cfg_q", bits=(q,), attributes={"cdc_static": "1"}),
        },
    )
    assert user_static_flop_names(module) == {"s"}


def test_user_handshake_flop_names_maps_tagged_netname_to_flop() -> None:
    """A ``(* cdc_handshake *)``-tagged netname over a flop's Q maps that
    flop into the handshake set."""
    clk, q = 2, 3
    module = Module(
        name="m",
        ports={"clk": Port(name="clk", direction="input", bits=(clk,))},
        cells={
            "h": Cell(
                name="h",
                type="$dff",
                connections={"CLK": (clk,), "D": (10,), "Q": (q,)},
            ),
        },
        netnames={
            "req_q": Netname(
                name="req_q", bits=(q,), attributes={"cdc_handshake": "1"}
            ),
        },
    )
    assert user_handshake_flop_names(module) == {"h"}


def test_user_gray_flop_names_tagged_bits_no_matching_flop() -> None:
    """A tagged netname whose bits don't coincide with any flop's Q
    yields an empty set — the ``sync_bits`` is non-empty (so the early
    return is skipped) but no flop matches."""
    module = Module(
        name="m",
        ports={},
        cells={},
        netnames={
            "stray": Netname(name="stray", bits=(99,), attributes={"cdc_gray": "1"}),
        },
    )
    assert user_gray_flop_names(module) == set()


# --- _clk_polarity / _trailing_bit ------------------------------------------


def test_clk_polarity_reads_parameter_posedge() -> None:
    """A parametric ``$dff`` with ``CLK_POLARITY='1'`` resolves to 1
    (posedge) via the parameter branch."""
    cell = Cell(
        name="f",
        type="$dff",
        connections={"CLK": (2,), "D": (3,), "Q": (4,)},
        parameters={"CLK_POLARITY": "1"},
    )
    flop = Flop(cell=cell, clk=2, d=(3,), q=(4,))
    assert _clk_polarity(flop) == 1


def test_clk_polarity_reads_parameter_negedge() -> None:
    """``CLK_POLARITY='0'`` resolves to 0 (negedge)."""
    cell = Cell(
        name="f",
        type="$dff",
        connections={"CLK": (2,), "D": (3,), "Q": (4,)},
        parameters={"CLK_POLARITY": "0"},
    )
    flop = Flop(cell=cell, clk=2, d=(3,), q=(4,))
    assert _clk_polarity(flop) == 0


def test_clk_polarity_gate_level_segment_negedge() -> None:
    """A gate-level ``$_DFF_N_`` (no CLK_POLARITY param) resolves to 0 by
    finding the ``N`` segment in the type string."""
    cell = Cell(
        name="f", type="$_DFF_N_", connections={"C": (2,), "D": (3,), "Q": (4,)}
    )
    flop = Flop(cell=cell, clk=2, d=(3,), q=(4,))
    assert _clk_polarity(flop) == 0


def test_clk_polarity_gate_level_segment_posedge() -> None:
    """A gate-level ``$_DFF_P_`` resolves to 1 via the ``P`` segment."""
    cell = Cell(
        name="f", type="$_DFF_P_", connections={"C": (2,), "D": (3,), "Q": (4,)}
    )
    flop = Flop(cell=cell, clk=2, d=(3,), q=(4,))
    assert _clk_polarity(flop) == 1


def test_clk_polarity_unknown_type_falls_back_posedge() -> None:
    """An unclassifiable cell type with no parameter and no P/N segment
    falls back to posedge (1) — the false-negative-biased default."""
    cell = Cell(
        name="f", type="$weirdff", connections={"CLK": (2,), "D": (3,), "Q": (4,)}
    )
    flop = Flop(cell=cell, clk=2, d=(3,), q=(4,))
    assert _clk_polarity(flop) == 1


def test_trailing_bit_decodes_each_form() -> None:
    """``_trailing_bit`` returns the last 0/1 of a binary string, and
    defaults to '0' on empty / non-binary trailing char."""
    assert _trailing_bit("1") == "1"
    assert _trailing_bit("0") == "0"
    assert _trailing_bit("0001") == "1"
    assert _trailing_bit("") == "0"  # empty default
    assert _trailing_bit("1x") == "0"  # non-binary trailing char


# --- CDC-006 lazy context ---------------------------------------------------


def test_cdc_006_lazy_context_fires() -> None:
    """``check_cdc_006`` direct call builds its own context and fires on
    the glitchy-comb-source fixture (the ``ctx is None`` branch)."""
    module, spec = _load_fixture("bad_comb_source")
    crossings = _async_crossings(module, spec)
    violations = check_cdc_006(module, crossings, spec)  # no ctx=
    cdc_006 = [v for v in violations if v.rule_id == "CDC-006"]
    assert len(cdc_006) == 1
    assert "glitchy combinational source" in cdc_006[0].message


# --- CDC-015 lazy context + no-SDC fallback ---------------------------------


def test_cdc_015_lazy_context_fires() -> None:
    """``check_cdc_015`` direct call builds its own context and fires on
    the sync-chain-with-foreign-reset fixture."""
    module, spec = _load_fixture("bad_sync_chain_foreign_reset")
    crossings = _async_crossings(module, spec)
    violations = check_cdc_015(module, crossings, spec)  # no ctx=
    cdc_015 = [v for v in violations if v.rule_id == "CDC-015"]
    assert len(cdc_015) >= 1
    assert cdc_015[0].severity == "error"
    assert "synchroniser chain" in cdc_015[0].message
    assert "foreign-domain" in cdc_015[0].message


# --- CDC-016 lazy context ---------------------------------------------------


def test_cdc_016_lazy_context_fires() -> None:
    """``check_cdc_016`` direct call builds its own context and fires on
    the opposite-edge-sync fixture."""
    module, spec = _load_fixture("bad_opposite_edge_sync")
    crossings = _async_crossings(module, spec)
    violations = check_cdc_016(module, crossings, spec)  # no ctx=
    cdc_016 = [v for v in violations if v.rule_id == "CDC-016"]
    assert len(cdc_016) == 1
    assert "opposite-edge synchroniser" in cdc_016[0].message


# --- CDC-017 lazy context + guards ------------------------------------------


def test_cdc_017_lazy_context_fires() -> None:
    """``check_cdc_017`` direct call builds its own context and fires on
    the latch-in-CDC-path fixture."""
    module, spec = _load_fixture("bad_latch_in_cdc_path")
    violations = check_cdc_017(module, [], spec)  # no ctx=
    cdc_017 = [v for v in violations if v.rule_id == "CDC-017"]
    assert len(cdc_017) == 1
    assert "transparent latch in CDC path" in cdc_017[0].message


def test_cdc_017_same_domain_latch_silent() -> None:
    """A ``$dlatch`` between a src flop and a dst flop in the *same*
    clock domain is not a CDC path — CDC-017 stays silent (the
    ``src_clk == dst_clk`` continue)."""
    clk = 2
    src_q = 3
    latch_q = 4
    dst_q = 5
    module = Module(
        name="same_domain_latch",
        ports={"clk": Port(name="clk", direction="input", bits=(clk,))},
        cells={
            "src": Cell(
                name="src",
                type="$dff",
                connections={"CLK": (clk,), "D": (10,), "Q": (src_q,)},
            ),
            "lat": Cell(
                name="lat",
                type="$dlatch",
                connections={"EN": (11,), "D": (src_q,), "Q": (latch_q,)},
            ),
            "dst": Cell(
                name="dst",
                type="$dff",
                connections={"CLK": (clk,), "D": (latch_q,), "Q": (dst_q,)},
            ),
        },
        netnames={},
    )
    assert check_cdc_017(module, [], clock_spec=None) == []


def test_cdc_017_latch_no_dst_flop_reader_silent() -> None:
    """A ``$dlatch`` whose Q is not read by any single-bit flop D pin
    yields no dst flop, so CDC-017 stays silent (the ``not dst_clocks``
    early-out)."""
    clk = 2
    src_q = 3
    latch_q = 4
    module = Module(
        name="dangling_latch",
        ports={
            "clk": Port(name="clk", direction="input", bits=(clk,)),
            "out": Port(name="out", direction="output", bits=(latch_q,)),
        },
        cells={
            "src": Cell(
                name="src",
                type="$dff",
                connections={"CLK": (clk,), "D": (10,), "Q": (src_q,)},
            ),
            # Latch Q drives only a top-level output, no flop.
            "lat": Cell(
                name="lat",
                type="$dlatch",
                connections={"EN": (11,), "D": (src_q,), "Q": (latch_q,)},
            ),
        },
        netnames={},
    )
    assert check_cdc_017(module, [], clock_spec=None) == []


# --- CDC-019 lazy context + guards ------------------------------------------


def test_cdc_019_lazy_context_fires() -> None:
    """``check_cdc_019`` direct call builds its own context and warns on
    the independent-one-hot-sync fixture."""
    module, spec = _load_fixture("bad_onehot_decode_independent_sync")
    crossings = _async_crossings(module, spec)
    violations = check_cdc_019(module, crossings, spec)  # no ctx=
    cdc_019 = [v for v in violations if v.rule_id == "CDC-019"]
    assert len(cdc_019) >= 1
    assert "independently-synced one-hot decode" in cdc_019[0].message


# --- CDC-020 lazy context ---------------------------------------------------


def test_cdc_020_lazy_context_fires() -> None:
    """``check_cdc_020`` direct call builds its own context and warns on
    the sliced-bus-reconvergence fixture."""
    module, spec = _load_fixture("bad_sliced_bus_reconvergence")
    crossings = _async_crossings(module, spec)
    violations = check_cdc_020(module, crossings, spec)  # no ctx=
    cdc_020 = [v for v in violations if v.rule_id == "CDC-020"]
    assert len(cdc_020) >= 1
    assert "sliced-bus reconvergence across CDC" in cdc_020[0].message


# --- _has_xor_tail_pulse_recovery negative shapes ---------------------------


def test_has_xor_tail_pulse_recovery_short_chain_returns_false() -> None:
    """A head with no 1-stage same-domain follow-on yields a chain of
    length < 2; the recogniser returns False (the ``len(chain) < 2``
    early-out)."""
    clk = 2
    head = _flop("head", clk=clk, d=(3,), q=(4,))
    module = Module(
        name="m",
        ports={"clk": Port(name="clk", direction="input", bits=(clk,))},
        cells={"head": head.cell},
        netnames={},
    )
    ctx = _build_context(module, clock_spec=None)
    assert not _has_xor_tail_pulse_recovery(head, "clk", ctx, module)


# --- more lazy-context (ctx=None) direct calls ------------------------------


def test_cdc_009_lazy_context_fires() -> None:
    """``check_cdc_009`` direct call builds its own context and warns on
    the fast-to-slow pulse-width fixture."""
    module, spec = _load_fixture("bad_pulse_width_fast_to_slow")
    crossings = _async_crossings(module, spec)
    violations = check_cdc_009(module, crossings, spec)  # no ctx=
    cdc_009 = [v for v in violations if v.rule_id == "CDC-009"]
    assert len(cdc_009) >= 1
    assert "pulse-width risk" in cdc_009[0].message


def test_cdc_009_no_sdc_returns_empty() -> None:
    """CDC-009 needs clock periods; with ``clock_spec=None`` it returns
    early (the ``if clock_spec is None: return []`` guard)."""
    module, _spec = _load_fixture("bad_pulse_width_fast_to_slow")
    assert check_cdc_009(module, [], clock_spec=None) == []


def test_cdc_013_lazy_context_fires() -> None:
    """``check_cdc_013`` direct call builds its own context and warns on
    the toggle-without-XOR-tail fixture."""
    module, spec = _load_fixture("bad_toggle_no_xor_tail")
    crossings = _async_crossings(module, spec)
    violations = check_cdc_013(module, crossings, spec)  # no ctx=
    cdc_013 = [v for v in violations if v.rule_id == "CDC-013"]
    assert len(cdc_013) == 1
    assert "toggle-synchroniser event-loss risk" in cdc_013[0].message


def test_cdc_013_no_sdc_returns_empty() -> None:
    """CDC-013 returns early without an SDC (no clock periods)."""
    module, _spec = _load_fixture("bad_toggle_no_xor_tail")
    assert check_cdc_013(module, [], clock_spec=None) == []


def test_cdc_012_lazy_context_fires() -> None:
    """``check_cdc_012`` direct call builds its own context and warns on
    the gated-bus-without-handshake fixture."""
    module, spec = _load_fixture("bad_functional_datahold_enable")
    crossings = _async_crossings(module, spec)
    violations = check_cdc_012(module, crossings, spec)  # no ctx=
    cdc_012 = [v for v in violations if v.rule_id == "CDC-012"]
    assert len(cdc_012) >= 1
    assert "functional data-hold risk" in cdc_012[0].message


def test_cdc_014_lazy_context_fires() -> None:
    """``check_cdc_014`` direct call builds its own context and fires on
    the comb-between-sync-stages fixture."""
    module, spec = _load_fixture("bad_comb_between_sync_stages")
    crossings = _async_crossings(module, spec)
    violations = check_cdc_014(module, crossings, spec)  # no ctx=
    cdc_014 = [v for v in violations if v.rule_id == "CDC-014"]
    assert len(cdc_014) == 1
    assert "combinational logic between synchroniser stages" in cdc_014[0].message


# --- CDC-003 user-marked skips ----------------------------------------------


def test_cdc_003_skips_marked_user_sync_destination() -> None:
    """A crossing whose destination synchroniser is ``(* cdc_sync *)``-
    marked is skipped by CDC-003 (``c.dst_flop ... in ctx.user_syncs``,
    rules.py line 1141). The ``marked_user_sync`` fixture has the
    marked sync — CDC-003 stays silent on it."""
    module, spec = _load_fixture("marked_user_sync")
    crossings = _async_crossings(module, spec)
    violations = check_cdc_003(module, crossings, spec)
    assert [v for v in violations if v.rule_id == "CDC-003"] == []


def test_cdc_003_skips_quasi_static_source() -> None:
    """A crossing whose source is ``(* cdc_static *)`` is skipped by
    CDC-003 (rules.py line 1143) — the held-constant source can't
    glitch through the comb path."""
    module, spec = _load_fixture("marked_quasi_static")
    crossings = _async_crossings(module, spec)
    violations = check_cdc_003(module, crossings, spec)
    assert [v for v in violations if v.rule_id == "CDC-003"] == []


# --- CDC-015 no-SDC fallback + dedupe + same-domain-reset -------------------


def test_cdc_015_no_sdc_fallback_dedupe_and_same_domain_reset() -> None:
    """A 2-stage sync chain in dst_clk whose head flop is reset from a
    foreign-domain flop fires CDC-015 once even across two crossings to
    the same head (the ``reported_heads`` dedupe). A second chain flop
    reset from a *same-domain* source contributes nothing (the
    ``src_clk == c.dst_clock`` continue). With ``clock_spec=None`` the
    ``a != b`` async fallback classifies the foreign reset."""
    dclk, foreign_clk = 2, 3
    head_d, head_q = 4, 5
    tail_q = 6
    foreign_q = 7  # foreign-domain flop driving the head's ARST
    same_q = 8  # same-domain flop (its reset is harmless)
    cells = {
        # foreign-domain reset source.
        "foreign": Cell(
            name="foreign",
            type="$dff",
            connections={"CLK": (foreign_clk,), "D": (20,), "Q": (foreign_q,)},
        ),
        # same-domain reset source.
        "same_src": Cell(
            name="same_src",
            type="$dff",
            connections={"CLK": (dclk,), "D": (21,), "Q": (same_q,)},
        ),
        # head: async reset from the FOREIGN flop.
        "head": Cell(
            name="head",
            type="$adff",
            connections={
                "CLK": (dclk,),
                "ARST": (foreign_q,),
                "D": (head_d,),
                "Q": (head_q,),
            },
        ),
        # tail: async reset from a SAME-domain flop (no contribution).
        "tail": Cell(
            name="tail",
            type="$adff",
            connections={
                "CLK": (dclk,),
                "ARST": (same_q,),
                "D": (head_q,),
                "Q": (tail_q,),
            },
        ),
    }
    ports = {
        "dclk": Port(name="dclk", direction="input", bits=(dclk,)),
        "foreign_clk": Port(name="foreign_clk", direction="input", bits=(foreign_clk,)),
    }
    module = Module(name="cdc015_nosdc", ports=ports, cells=cells, netnames={})
    head_flop = next(f for f in find_flops(module) if f.cell.name == "head")
    # Two crossings to the same head → exercises reported_heads dedupe.
    crossings = [
        Crossing(
            src_clock="foreign_clk",
            dst_flop=head_flop,
            dst_clock="dclk",
            min_hops=0,
            width=1,
            src_flop=None,
        ),
        Crossing(
            src_clock="foreign_clk",
            dst_flop=head_flop,
            dst_clock="dclk",
            min_hops=0,
            width=1,
            src_flop=None,
        ),
    ]
    violations = check_cdc_015(module, crossings, clock_spec=None)  # lazy ctx + a!=b
    cdc_015 = [v for v in violations if v.rule_id == "CDC-015"]
    assert len(cdc_015) == 1  # deduped despite two crossings
    assert "foreign" in cdc_015[0].message
    assert cdc_015[0].cell_name == "head"


# --- _is_gated_bus_crossing negatives ---------------------------------------


def _bus_crossing(module: Module, dst_name: str, *, width: int) -> Crossing:
    dst_flop = next(f for f in find_flops(module) if f.cell.name == dst_name)
    return Crossing(
        src_clock="sclk",
        dst_flop=dst_flop,
        dst_clock="dclk",
        min_hops=1,
        width=width,
        src_flop=None,
    )


def test_is_gated_bus_crossing_mux_without_select_not_gated() -> None:
    """A mux-on-D whose ``S`` pin carries no bits can't be a gate — the
    ``if not s_bits: return False`` guard (rules.py line 1297)."""
    dclk = 2
    d0, d1 = 3, 4
    module = Module(
        name="no_sel_mux",
        ports={"dclk": Port(name="dclk", direction="input", bits=(dclk,))},
        cells={
            "mux": Cell(
                name="mux",
                type="$mux",
                connections={"A": (10, 11), "B": (12, 13), "S": (), "Y": (d0, d1)},
            ),
            "dst": Cell(
                name="dst",
                type="$dff",
                connections={"CLK": (dclk,), "D": (d0, d1), "Q": (5, 6)},
            ),
        },
        netnames={},
    )
    ctx = _build_context(module, clock_spec=None)
    crossing = _bus_crossing(module, "dst", width=2)
    assert not _is_gated_bus_crossing(module, crossing, ctx.domains, ctx.bit_drivers)


def test_is_gated_bus_crossing_mux_select_from_port_not_gated() -> None:
    """A mux-on-D whose ``S`` fanin reaches no flop (driven by a raw port)
    can't be a synced gate — the ``if not s_fanin_flops: return False``
    guard (rules.py line 1300)."""
    dclk, sel = 2, 3
    d0, d1 = 4, 5
    module = Module(
        name="port_sel_mux",
        ports={
            "dclk": Port(name="dclk", direction="input", bits=(dclk,)),
            "sel": Port(name="sel", direction="input", bits=(sel,)),
        },
        cells={
            "mux": Cell(
                name="mux",
                type="$mux",
                connections={"A": (10, 11), "B": (12, 13), "S": (sel,), "Y": (d0, d1)},
            ),
            "dst": Cell(
                name="dst",
                type="$dff",
                connections={"CLK": (dclk,), "D": (d0, d1), "Q": (6, 7)},
            ),
        },
        netnames={},
    )
    ctx = _build_context(module, clock_spec=None)
    crossing = _bus_crossing(module, "dst", width=2)
    assert not _is_gated_bus_crossing(module, crossing, ctx.domains, ctx.bit_drivers)


def test_is_gated_bus_crossing_constant_d_bit_skipped() -> None:
    """A destination flop with a constant ('0') D bit alongside a real
    driver: the non-int bit is skipped (rules.py line 1285), and the
    single real driver (not a mux) leaves the crossing ungated."""
    dclk = 2
    d_real = 3
    module = Module(
        name="const_d",
        ports={"dclk": Port(name="dclk", direction="input", bits=(dclk,))},
        cells={
            "g": Cell(name="g", type="$not", connections={"A": (10,), "Y": (d_real,)}),
            # D = (d_real, '0') — one int driver + one constant.
            "dst": Cell(
                name="dst",
                type="$dff",
                connections={"CLK": (dclk,), "D": (d_real, "0"), "Q": (4, 5)},
            ),
        },
        netnames={},
    )
    ctx = _build_context(module, clock_spec=None)
    crossing = _bus_crossing(module, "dst", width=2)
    assert not _is_gated_bus_crossing(module, crossing, ctx.domains, ctx.bit_drivers)


# --- RDC-006 mux-from-flops fallback phrase ---------------------------------


def test_rdc_006_mux_data_legs_from_flops_uses_fallback_phrase() -> None:
    """An async reset driven by a ``$mux`` whose data legs trace to flops
    (not top-level ports) reaches no reset ports — RDC-006 still fires
    using the 'multiple reset sources' fallback phrase (rules.py line
    2705)."""
    clk_a, clk_b = 2, 3
    leg_a_q, leg_b_q = 4, 5  # mux data legs are flop Qs
    sel_q = 6
    mux_y = 7
    dst_q = 8
    cells = {
        "leg_a": Cell(
            name="leg_a",
            type="$dff",
            connections={"CLK": (clk_b,), "D": (20,), "Q": (leg_a_q,)},
        ),
        "leg_b": Cell(
            name="leg_b",
            type="$dff",
            connections={"CLK": (clk_b,), "D": (21,), "Q": (leg_b_q,)},
        ),
        "sel_ff": Cell(
            name="sel_ff",
            type="$dff",
            connections={"CLK": (clk_a,), "D": (22,), "Q": (sel_q,)},
        ),
        "rst_mux": Cell(
            name="rst_mux",
            type="$mux",
            connections={
                "A": (leg_a_q,),
                "B": (leg_b_q,),
                "S": (sel_q,),
                "Y": (mux_y,),
            },
        ),
        # Consumer flop with async reset from the mux output.
        "dst": Cell(
            name="dst",
            type="$adff",
            connections={"CLK": (clk_a,), "ARST": (mux_y,), "D": (23,), "Q": (dst_q,)},
        ),
    }
    module = Module(
        name="rdc006_flop_legs",
        ports={
            "clk_a": Port(name="clk_a", direction="input", bits=(clk_a,)),
            "clk_b": Port(name="clk_b", direction="input", bits=(clk_b,)),
        },
        cells=cells,
        netnames={},
    )
    violations = check_rdc_006(module, [], clock_spec=None)  # lazy ctx
    rdc_006 = [v for v in violations if v.rule_id == "RDC-006"]
    assert len(rdc_006) == 1
    assert "multiple reset sources" in rdc_006[0].message
    assert rdc_006[0].cell_name == "dst"


# --- RDC-005 single-port comb (no fire) -------------------------------------


def test_rdc_005_single_port_comb_does_not_fire() -> None:
    """An async reset that is the comb-inversion of a SINGLE top-level
    port has only one fanin port (< 2) — RDC-005 stays silent (the
    ``len(fanin_ports) < 2`` guard, rules.py line 2542)."""
    clk = 2
    rst_n = 3
    inv_y = 4
    dst_q = 5
    module = Module(
        name="single_port_rst",
        ports={
            "clk": Port(name="clk", direction="input", bits=(clk,)),
            "rst_n": Port(name="rst_n", direction="input", bits=(rst_n,)),
        },
        cells={
            "inv": Cell(
                name="inv", type="$not", connections={"A": (rst_n,), "Y": (inv_y,)}
            ),
            "dst": Cell(
                name="dst",
                type="$adff",
                connections={
                    "CLK": (clk,),
                    "ARST": (inv_y,),
                    "D": (10,),
                    "Q": (dst_q,),
                },
            ),
        },
        netnames={},
    )
    violations = check_rdc_005(module, [], clock_spec=None)  # lazy ctx
    assert [v for v in violations if v.rule_id == "RDC-005"] == []


# --- user_*_flop_names: non-int bits in a tagged netname are skipped ---------
#
# Each helper iterates a tagged netname's bits and adds only the int
# ones. A netname mixing an int bit (over a real flop Q) with a
# constant string bit exercises the ``isinstance(b, int)`` False arm.


def test_user_sync_flop_names_skips_constant_bit_in_netname() -> None:
    """A ``(* cdc_sync *)`` netname mixing a real flop-Q bit with a
    constant string bit maps only the flop — the string bit is skipped
    by the ``isinstance(b, int)`` filter."""
    clk, q = 2, 3
    module = Module(
        name="m",
        ports={"clk": Port(name="clk", direction="input", bits=(clk,))},
        cells={
            "f": Cell(
                name="f",
                type="$dff",
                connections={"CLK": (clk,), "D": (10,), "Q": (q,)},
            ),
        },
        netnames={
            "sync_q": Netname(
                name="sync_q", bits=(q, "0"), attributes={"cdc_sync": "1"}
            ),
        },
    )
    assert user_sync_flop_names(module) == {"f"}


def test_user_gray_flop_names_skips_constant_bit_in_netname() -> None:
    """A ``(* cdc_gray *)`` netname with a trailing constant bit maps
    only the flop, skipping the string bit."""
    clk, q = 2, 3
    module = Module(
        name="m",
        ports={"clk": Port(name="clk", direction="input", bits=(clk,))},
        cells={
            "g": Cell(
                name="g",
                type="$dff",
                connections={"CLK": (clk,), "D": (10,), "Q": (q,)},
            ),
        },
        netnames={
            "gray_q": Netname(
                name="gray_q", bits=(q, "1"), attributes={"cdc_gray": "1"}
            ),
        },
    )
    assert user_gray_flop_names(module) == {"g"}


def test_user_static_flop_names_skips_constant_bit_in_netname() -> None:
    """A ``(* cdc_static *)`` netname with a constant bit skips it."""
    clk, q = 2, 3
    module = Module(
        name="m",
        ports={"clk": Port(name="clk", direction="input", bits=(clk,))},
        cells={
            "s": Cell(
                name="s",
                type="$dff",
                connections={"CLK": (clk,), "D": (10,), "Q": (q,)},
            ),
        },
        netnames={
            "cfg_q": Netname(
                name="cfg_q", bits=(q, "x"), attributes={"cdc_static": "1"}
            ),
        },
    )
    assert user_static_flop_names(module) == {"s"}


def test_user_handshake_flop_names_skips_constant_bit_in_netname() -> None:
    """A ``(* cdc_handshake *)`` netname with a constant bit skips it."""
    clk, q = 2, 3
    module = Module(
        name="m",
        ports={"clk": Port(name="clk", direction="input", bits=(clk,))},
        cells={
            "h": Cell(
                name="h",
                type="$dff",
                connections={"CLK": (clk,), "D": (10,), "Q": (q,)},
            ),
        },
        netnames={
            "req_q": Netname(
                name="req_q", bits=(q, "0"), attributes={"cdc_handshake": "1"}
            ),
        },
    )
    assert user_handshake_flop_names(module) == {"h"}


def test_user_reset_sync_flop_names_skips_constant_bit_in_netname() -> None:
    """A ``(* reset_sync *)`` netname with a constant bit maps only the
    flop, skipping the string bit."""
    clk, q = 2, 3
    module = Module(
        name="m",
        ports={"clk": Port(name="clk", direction="input", bits=(clk,))},
        cells={
            "rs": Cell(
                name="rs",
                type="$dff",
                connections={"CLK": (clk,), "D": (10,), "Q": (q,)},
            ),
        },
        netnames={
            "rsync_q": Netname(
                name="rsync_q", bits=(q, "1"), attributes={"reset_sync": "1"}
            ),
        },
    )
    assert user_reset_sync_flop_names(module) == {"rs"}


def test_user_glitchless_clock_mux_bits_skips_constant_bit() -> None:
    """A ``(* glitchless_clock_mux *)`` netname with a constant bit
    returns only the int bit, skipping the string."""
    module = Module(
        name="m",
        ports={},
        cells={},
        netnames={
            "sel": Netname(
                name="sel", bits=(5, "0"), attributes={"glitchless_clock_mux": "1"}
            ),
        },
    )
    assert user_glitchless_clock_mux_bits(module) == {5}


# --- _trace_through_bus_buffers edge guards ---------------------------------


def test_trace_through_bus_buffers_width_mismatch_returns_driver() -> None:
    """A buffer whose ``A``/``Y`` widths differ can't be a bit-aligned
    passthrough — the function returns that buffer's own driver tuple
    (rules.py line 1224) instead of stepping through."""
    src_q, buf_y = 3, 4
    module = Module(
        name="m",
        ports={"clk": Port(name="clk", direction="input", bits=(2,))},
        cells={
            "src": Cell(
                name="src",
                type="$dff",
                connections={"CLK": (2,), "D": (10,), "Q": (src_q,)},
            ),
            # $not with A wider than Y → width mismatch.
            "buf": Cell(
                name="buf", type="$not", connections={"A": (src_q, 11), "Y": (buf_y,)}
            ),
        },
        netnames={},
    )
    ctx = _build_context(module, clock_spec=None)
    drv = _trace_through_bus_buffers(module, buf_y, ctx.bit_drivers)
    assert drv is not None
    assert drv[0] == "buf"  # didn't step through; returned the buffer


def test_trace_through_bus_buffers_constant_a_bit_returns_driver() -> None:
    """A buffer whose aligned ``A`` bit is a constant string can't be
    followed — the function returns the buffer's driver (rules.py line
    1227)."""
    buf_y = 4
    module = Module(
        name="m",
        ports={"clk": Port(name="clk", direction="input", bits=(2,))},
        cells={
            # $not whose A bit is a constant '0'.
            "buf": Cell(
                name="buf", type="$not", connections={"A": ("0",), "Y": (buf_y,)}
            ),
        },
        netnames={},
    )
    ctx = _build_context(module, clock_spec=None)
    drv = _trace_through_bus_buffers(module, buf_y, ctx.bit_drivers)
    assert drv is not None
    assert drv[0] == "buf"


# --- _has_xor_tail_pulse_recovery deeper negatives --------------------------


def test_has_xor_tail_multibit_tail_returns_false() -> None:
    """A chain whose tail flop is multi-bit (``len(tail.q) != 1``) can't
    match the pulse-sync shape — returns False (rules.py line 3345-3346).
    """
    dclk = 2
    head_d, head_q = 3, 4
    # tail is multi-bit (Q width 2) — head.Q feeds tail.D[0].
    tail = Cell(
        name="tail",
        type="$dff",
        connections={"CLK": (dclk,), "D": (head_q, 10), "Q": (5, 6)},
    )
    head = Cell(
        name="head",
        type="$dff",
        connections={"CLK": (dclk,), "D": (head_d,), "Q": (head_q,)},
    )
    module = Module(
        name="multibit_tail",
        ports={"dclk": Port(name="dclk", direction="input", bits=(dclk,))},
        cells={"head": head, "tail": tail},
        netnames={},
    )
    head_flop = next(f for f in find_flops(module) if f.cell.name == "head")
    ctx = _build_context(module, clock_spec=None)
    # The chain is just [head] (head.Q has one reader = tail.D[0]), but
    # tail is multi-bit so the chain extension stops; len(chain) < 2.
    assert not _has_xor_tail_pulse_recovery(head_flop, "dclk", ctx, module)


def test_has_xor_tail_follow_without_matching_xor_returns_false() -> None:
    """A follow-on flop plus an XOR that does NOT have both tail.Q and
    follow.Q as inputs fails the final confirmation (rules.py line
    3393-3396) — returns False."""
    dclk = 2
    head_d, head_q = 3, 4
    tail_q = 5
    follow_q = 6
    stray = 7
    xor_out = 8
    cells = {
        "head": Cell(
            name="head",
            type="$dff",
            connections={"CLK": (dclk,), "D": (head_d,), "Q": (head_q,)},
        ),
        "tail": Cell(
            name="tail",
            type="$dff",
            connections={"CLK": (dclk,), "D": (head_q,), "Q": (tail_q,)},
        ),
        "follow": Cell(
            name="follow",
            type="$dff",
            connections={"CLK": (dclk,), "D": (tail_q,), "Q": (follow_q,)},
        ),
        # XOR reads tail_q but its other input is a stray net, NOT follow_q.
        "edge_xor": Cell(
            name="edge_xor",
            type="$xor",
            connections={"A": (tail_q,), "B": (stray,), "Y": (xor_out,)},
        ),
    }
    module = Module(
        name="mismatched_xor",
        ports={"dclk": Port(name="dclk", direction="input", bits=(dclk,))},
        cells=cells,
        netnames={},
    )
    head_flop = next(f for f in find_flops(module) if f.cell.name == "head")
    ctx = _build_context(module, clock_spec=None)
    assert not _has_xor_tail_pulse_recovery(head_flop, "dclk", ctx, module)


# --- CDC-006 unconstrained-port skip ----------------------------------------


def test_cdc_006_skips_unconstrained_port_source() -> None:
    """A 2FF synchroniser whose first stage's D traces through comb to an
    *unconstrained* top-level input port belongs to CDC-011, not CDC-006
    — the rule skips the port (rules.py line 1680-1681). The 'data' port
    is left untyped (no set_input_delay), so
    ``synthesize_unconstrained_inputs`` stamps it with the sentinel."""
    clk = 2
    data_in = 3
    gate_y = 4
    sync0_q = 5
    sync1_q = 6
    module = Module(
        name="unconstrained_src",
        ports={
            "clk": Port(name="clk", direction="input", bits=(clk,)),
            "data_in": Port(name="data_in", direction="input", bits=(data_in,)),
        },
        cells={
            # comb from the untyped port into the sync first stage.
            "gate": Cell(
                name="gate", type="$not", connections={"A": (data_in,), "Y": (gate_y,)}
            ),
            "sync0": Cell(
                name="sync0",
                type="$dff",
                connections={"CLK": (clk,), "D": (gate_y,), "Q": (sync0_q,)},
            ),
            "sync1": Cell(
                name="sync1",
                type="$dff",
                connections={"CLK": (clk,), "D": (sync0_q,), "Q": (sync1_q,)},
            ),
        },
        netnames={},
    )
    spec = parse_sdc("create_clock -name clk -period 10.0 [get_ports clk]\n")
    sdc_mod.synthesize_unconstrained_inputs(spec, module)
    crossings = find_crossings(module, port_clock=spec.port_clock)
    violations = check_cdc_006(module, crossings, spec)
    # data_in is unconstrained → its comb path is CDC-011's concern, not
    # CDC-006: the rule must not fire here.
    assert [v for v in violations if v.rule_id == "CDC-006"] == []


# --- _chain_has_inter_stage_comb constant-Y bit + lazy reader-count ----------


def test_chain_has_inter_stage_comb_skips_constant_y_bit() -> None:
    """When head.Q feeds a comb cell whose ``Y`` bit is a constant string,
    that output bit is skipped (rules.py line 965-966) and no follow-on
    stage is found — the helper returns ``None``."""
    clk = 2
    head_q = 4
    module = Module(
        name="const_y",
        ports={"clk": Port(name="clk", direction="input", bits=(clk,))},
        cells={
            "head": Cell(
                name="head",
                type="$dff",
                connections={"CLK": (clk,), "D": (3,), "Q": (head_q,)},
            ),
            # comb cell whose Y bit is a constant '0' (no real downstream).
            "gate": Cell(
                name="gate", type="$not", connections={"A": (head_q,), "Y": ("0",)}
            ),
        },
        netnames={},
    )
    ctx = _build_context(module, clock_spec=None)
    head_flop = next(f for f in find_flops(module) if f.cell.name == "head")
    assert _chain_has_inter_stage_comb(head_flop, ctx) is None


def test_sync_chain_depth_lazy_skips_constant_reader_bit() -> None:
    """``_sync_chain_depth`` with ``reader_counts=None`` rebuilds the
    reader-count index internally; a cell reading a constant bit on a
    data pin exercises the ``isinstance(b, int)`` filter in
    ``_bit_reader_count`` (rules.py line 729-730). The 2FF chain head
    still reports depth 2."""
    clk = 2
    head_q = 4
    tail_q = 5
    module = Module(
        name="const_reader",
        ports={"clk": Port(name="clk", direction="input", bits=(clk,))},
        cells={
            "head": Cell(
                name="head",
                type="$dff",
                connections={"CLK": (clk,), "D": (3,), "Q": (head_q,)},
            ),
            "tail": Cell(
                name="tail",
                type="$dff",
                connections={"CLK": (clk,), "D": (head_q,), "Q": (tail_q,)},
            ),
            # A comb cell reading a constant bit on a data pin.
            "g": Cell(
                name="g",
                type="$and",
                connections={"A": ("0",), "B": (tail_q,), "Y": (6,)},
            ),
        },
        netnames={},
    )
    domains = {"head": "clk", "tail": "clk", "g": None}
    head_flop = next(f for f in find_flops(module) if f.cell.name == "head")
    depth = _sync_chain_depth(module, head_flop, "clk", domains)  # reader_counts=None
    assert depth == 2


def test_sync_chain_helpers_break_on_constant_q_head() -> None:
    """A head flop whose single ``Q`` bit is a constant string can't
    extend the chain: both ``_sync_chain_depth`` (rules.py line 915-916)
    and ``_sync_chain_flops`` (line 915-916 mirror) stop at the head —
    depth 1, chain == (head,)."""
    clk = 2
    head = _flop("head", clk=clk, d=(3,), q=("0",))  # constant Q bit
    module = Module(
        name="const_q_head",
        ports={"clk": Port(name="clk", direction="input", bits=(clk,))},
        cells={"head": head.cell},
        netnames={},
    )
    domains = {"head": "clk"}
    assert _sync_chain_depth(module, head, "clk", domains) == 1
    chain = _sync_chain_flops(module, head, "clk", domains, {}, {})
    assert chain == (head,)
