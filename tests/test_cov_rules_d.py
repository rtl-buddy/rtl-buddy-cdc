"""Second coverage batch for :mod:`rtl_buddy_cdc.rules`.

Continues ``tests/test_cov_rules_c.py``, focusing on the deeper
structural helpers whose branches the committed fixtures don't fully
exercise:

* ``_clock_input_domains_for`` — the SDC clock-port resolution branch
  and the comb-traversal hop (reached via CDC-010 with an SDC);
* ``_has_xor_tail_pulse_recovery`` — the positive pulse-synchroniser
  match (CDC-013 suppression) and the missing-follow-on / wrong-domain
  negatives;
* ``_has_dst_to_src_feedback`` — the handshake-feedback present / absent
  paths (CDC-012 suppression heuristic);
* CDC-017 src/dst guards (no src flop, multi-domain dedupe);
* CDC-019 / CDC-020 suppression and shape guards.

Built from hand-constructed ``Module`` / ``Cell`` / ``Port`` /
``Netname`` dataclasses or committed Yosys-JSON fixtures, matching the
self-contained style of the other ``test_cov_rules_*`` files.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rtl_buddy_cdc import netlist, sdc as sdc_mod
from rtl_buddy_cdc.domain import Crossing
from rtl_buddy_cdc.flops import Flop, find_flops
from rtl_buddy_cdc.netlist import Cell, Module, Netname, Port
from rtl_buddy_cdc.rules import (
    _build_context,  # noqa: PLC2701
    _has_dst_to_src_feedback,  # noqa: PLC2701
    _has_xor_tail_pulse_recovery,  # noqa: PLC2701
    _is_gated_bus_crossing,  # noqa: PLC2701
    check_cdc_003,
    check_cdc_010,
    check_cdc_016,
    check_cdc_017,
    check_cdc_018,
    check_cdc_019,
    check_cdc_020,
    check_cdc_021,
    check_rdc_001,
    check_rdc_002,
    check_rdc_003,
    check_rdc_007,
)
from rtl_buddy_cdc.sdc import parse as parse_sdc

FIX_ROOT = Path(__file__).parent / "fixtures"


def _load_fixture(name: str) -> tuple[Module, sdc_mod.ClockSpec]:
    fix_dir = FIX_ROOT / name
    json_path = fix_dir / f"{name}.json"
    sdc_path = fix_dir / f"{name}.sdc"
    if not json_path.exists():
        pytest.skip(f"fixture not built: {json_path}")
    module = netlist.load(json_path)
    spec = sdc_mod.parse_file(sdc_path)
    sdc_mod.synthesize_unconstrained_inputs(spec, module)
    return module, spec


def _flop(name: str, *, clk: int, d: tuple[int, ...], q: tuple[int, ...]) -> Flop:
    cell = Cell(name=name, type="$dff", connections={"CLK": (clk,), "D": d, "Q": q})
    return Flop(cell=cell, clk=clk, d=d, q=q)


def _crossing(
    *, src_clk: str, dst_clk: str, width: int, src_flop: Flop | None, dst_flop: Flop
) -> Crossing:
    return Crossing(
        src_clock=src_clk,
        dst_flop=dst_flop,
        dst_clock=dst_clk,
        min_hops=1,
        width=width,
        src_flop=src_flop,
    )


# --- _clock_input_domains_for via CDC-010 with an SDC -----------------------


def test_cdc_010_clock_inputs_resolved_from_sdc_clock_ports() -> None:
    """A clock mux whose A/B clock inputs are *top-level clock ports*
    (no driving cell) forces ``_clock_input_domains_for`` down the
    ``drv is None`` → ``clock_spec.clock_for_port`` branch (rules.py
    lines 2038-2043). With both clocks declared async in the SDC and
    the select driven by a flop on a third async clock, CDC-010 fires."""
    # Bits.
    ck_a, ck_b, ck_sel = 2, 3, 4
    sel_d, sel_q = 5, 6
    ck_out, d_in, q_out = 7, 8, 9
    ports = {
        "ck_a": Port(name="ck_a", direction="input", bits=(ck_a,)),
        "ck_b": Port(name="ck_b", direction="input", bits=(ck_b,)),
        "ck_sel": Port(name="ck_sel", direction="input", bits=(ck_sel,)),
        "sel_d": Port(name="sel_d", direction="input", bits=(sel_d,)),
        "d_in": Port(name="d_in", direction="input", bits=(d_in,)),
        "q_out": Port(name="q_out", direction="output", bits=(q_out,)),
    }
    cells = {
        # Select flop on the third clock domain.
        "sel_ff": Cell(
            name="sel_ff",
            type="$dff",
            connections={"CLK": (ck_sel,), "D": (sel_d,), "Q": (sel_q,)},
        ),
        # Clock mux: A/B are the raw clock ports (no driver → port path).
        "clk_mux": Cell(
            name="clk_mux",
            type="$mux",
            connections={"A": (ck_b,), "B": (ck_a,), "S": (sel_q,), "Y": (ck_out,)},
        ),
        "out_ff": Cell(
            name="out_ff",
            type="$dff",
            connections={"CLK": (ck_out,), "D": (d_in,), "Q": (q_out,)},
        ),
    }
    module = Module(name="sdc_clk_mux", ports=ports, cells=cells, netnames={})
    spec = parse_sdc(
        "create_clock -name ck_a -period 10.0 [get_ports ck_a]\n"
        "create_clock -name ck_b -period 7.0 [get_ports ck_b]\n"
        "create_clock -name ck_sel -period 11.0 [get_ports ck_sel]\n"
        "set_clock_groups -asynchronous "
        "-group {ck_a} -group {ck_b} -group {ck_sel}\n"
    )
    violations = check_cdc_010(module, [], spec)  # lazy ctx
    cdc_010 = [v for v in violations if v.rule_id == "CDC-010"]
    assert len(cdc_010) == 1
    v = cdc_010[0]
    assert v.cell_name == "clk_mux"
    assert "sel_ff" in v.message
    assert "control pin S" in v.message


def test_cdc_010_clock_input_through_comb_to_flop_q() -> None:
    """The mux's gated-clock input arrives through a comb buffer fed by a
    divider flop's Q. ``_clock_input_domains_for`` must hop through the
    comb cell (rules.py lines 2051-2059) and then classify the flop-Q
    source (lines 2046-2049). With the select on a third async domain,
    CDC-010 fires."""
    ck_a, ck_b, ck_sel = 2, 3, 4
    div_a_q = 5
    buf_y = 6  # comb buffer between div_a.Q and the mux's A pin
    sel_d, sel_q = 7, 8
    ck_out, d_in, q_out = 9, 10, 11
    ports = {
        "ck_a": Port(name="ck_a", direction="input", bits=(ck_a,)),
        "ck_b": Port(name="ck_b", direction="input", bits=(ck_b,)),
        "ck_sel": Port(name="ck_sel", direction="input", bits=(ck_sel,)),
        "sel_d": Port(name="sel_d", direction="input", bits=(sel_d,)),
        "d_in": Port(name="d_in", direction="input", bits=(d_in,)),
        "q_out": Port(name="q_out", direction="output", bits=(q_out,)),
    }
    cells = {
        # Divider flop on ck_a → its Q feeds a comb buffer → mux.A.
        "div_a": Cell(
            name="div_a",
            type="$dff",
            connections={"CLK": (ck_a,), "D": (div_a_q,), "Q": (div_a_q,)},
        ),
        "clk_buf": Cell(
            name="clk_buf",
            type="$and",
            connections={"A": (div_a_q,), "B": (div_a_q,), "Y": (buf_y,)},
        ),
        "sel_ff": Cell(
            name="sel_ff",
            type="$dff",
            connections={"CLK": (ck_sel,), "D": (sel_d,), "Q": (sel_q,)},
        ),
        # A goes through comb (buf_y), B is a raw clock port.
        "clk_mux": Cell(
            name="clk_mux",
            type="$mux",
            connections={"A": (buf_y,), "B": (ck_b,), "S": (sel_q,), "Y": (ck_out,)},
        ),
        "out_ff": Cell(
            name="out_ff",
            type="$dff",
            connections={"CLK": (ck_out,), "D": (d_in,), "Q": (q_out,)},
        ),
    }
    module = Module(name="comb_clk_mux", ports=ports, cells=cells, netnames={})
    spec = parse_sdc(
        "create_clock -name ck_a -period 10.0 [get_ports ck_a]\n"
        "create_clock -name ck_b -period 7.0 [get_ports ck_b]\n"
        "create_clock -name ck_sel -period 11.0 [get_ports ck_sel]\n"
        "set_clock_groups -asynchronous "
        "-group {ck_a} -group {ck_b} -group {ck_sel}\n"
    )
    violations = check_cdc_010(module, [], spec)
    cdc_010 = [v for v in violations if v.rule_id == "CDC-010"]
    assert len(cdc_010) == 1
    assert cdc_010[0].cell_name == "clk_mux"


def test_cdc_010_silent_when_control_shares_a_gated_clock_domain() -> None:
    """When the select flop is clocked by one of the mux's own gated
    clocks (``src_clk in cell_clock_domains``), CDC-010 stays silent —
    the control transition is synchronous to a gated clock, no runt
    pulse (rules.py line ~2160 ``src_clk in cell_clock_domains``)."""
    ck_a, ck_b = 2, 3
    sel_d, sel_q = 5, 6
    ck_out, d_in, q_out = 7, 8, 9
    ports = {
        "ck_a": Port(name="ck_a", direction="input", bits=(ck_a,)),
        "ck_b": Port(name="ck_b", direction="input", bits=(ck_b,)),
        "sel_d": Port(name="sel_d", direction="input", bits=(sel_d,)),
        "d_in": Port(name="d_in", direction="input", bits=(d_in,)),
        "q_out": Port(name="q_out", direction="output", bits=(q_out,)),
    }
    cells = {
        # Select flop is clocked by ck_a — one of the mux's gated clocks.
        "sel_ff": Cell(
            name="sel_ff",
            type="$dff",
            connections={"CLK": (ck_a,), "D": (sel_d,), "Q": (sel_q,)},
        ),
        "clk_mux": Cell(
            name="clk_mux",
            type="$mux",
            connections={"A": (ck_b,), "B": (ck_a,), "S": (sel_q,), "Y": (ck_out,)},
        ),
        "out_ff": Cell(
            name="out_ff",
            type="$dff",
            connections={"CLK": (ck_out,), "D": (d_in,), "Q": (q_out,)},
        ),
    }
    module = Module(name="safe_clk_mux", ports=ports, cells=cells, netnames={})
    spec = parse_sdc(
        "create_clock -name ck_a -period 10.0 [get_ports ck_a]\n"
        "create_clock -name ck_b -period 7.0 [get_ports ck_b]\n"
        "set_clock_groups -asynchronous -group {ck_a} -group {ck_b}\n"
    )
    violations = check_cdc_010(module, [], spec)
    assert [v for v in violations if v.rule_id == "CDC-010"] == []


# --- _has_xor_tail_pulse_recovery positive + negatives ----------------------


def _xor_tail_module() -> tuple[Module, Flop]:
    """Build the canonical pulse-synchroniser dst-side shape:

        head (dst_clk) → tail (dst_clk) ─┬─► follow.D (dst_clk)
                                         └─► xor.A   (with follow.Q on xor.B)

    The tail's Q has TWO readers (follow + xor), so _sync_chain_flops
    returns [head, tail] and the recogniser then looks downstream of
    the tail for the follow-flop + XOR pair. Returns (module, head).
    """
    dclk = 2
    head_d, head_q = 3, 4
    tail_q = 5
    follow_q = 6
    pulse_out = 7
    cells = {
        "head": Cell(
            name="head",
            type="$dff",
            connections={"CLK": (dclk,), "D": (head_d,), "Q": (head_q,)},
        ),
        # tail: D = head.Q ; its Q feeds both follow.D and the xor.
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
        "edge_xor": Cell(
            name="edge_xor",
            type="$xor",
            connections={"A": (tail_q,), "B": (follow_q,), "Y": (pulse_out,)},
        ),
    }
    ports = {
        "dclk": Port(name="dclk", direction="input", bits=(dclk,)),
        "pulse_out": Port(name="pulse_out", direction="output", bits=(pulse_out,)),
    }
    module = Module(name="xor_tail", ports=ports, cells=cells, netnames={})
    head = next(f for f in find_flops(module) if f.cell.name == "head")
    return module, head


def test_has_xor_tail_pulse_recovery_positive_match() -> None:
    """The canonical pulse-sync XOR-tail shape is recognised: tail.Q
    feeds a same-domain follow-on flop AND an XOR whose inputs are
    tail.Q + follow.Q. Returns True (the CDC-013 suppression path)."""
    module, head = _xor_tail_module()
    ctx = _build_context(module, clock_spec=None)
    assert _has_xor_tail_pulse_recovery(head, "dclk", ctx, module)


def test_has_xor_tail_no_xor_returns_false() -> None:
    """Same chain but with the XOR removed: a follow-on flop without an
    edge-detect XOR is not the pulse-sync idiom — returns False
    (``follow_flop_q is None or not xor_cells``)."""
    module, head = _xor_tail_module()
    # Drop the XOR cell.
    cells = dict(module.cells)
    del cells["edge_xor"]
    module2 = Module(
        name=module.name, ports=module.ports, cells=cells, netnames=module.netnames
    )
    head2 = next(f for f in find_flops(module2) if f.cell.name == "head")
    ctx = _build_context(module2, clock_spec=None)
    assert not _has_xor_tail_pulse_recovery(head2, "dclk", ctx, module2)


def test_has_xor_tail_foreign_domain_follow_returns_false() -> None:
    """When the follow-on flop sits in a *different* clock domain it is
    rejected (``ctx.domains.get(cell.name) != head_clock`` continue),
    so the recogniser finds no valid follow-on and returns False."""
    dclk, other_clk = 2, 20
    head_d, head_q = 3, 4
    tail_q = 5
    follow_q = 6
    pulse_out = 7
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
        # Follow-on flop on a foreign clock.
        "follow": Cell(
            name="follow",
            type="$dff",
            connections={"CLK": (other_clk,), "D": (tail_q,), "Q": (follow_q,)},
        ),
        "edge_xor": Cell(
            name="edge_xor",
            type="$xor",
            connections={"A": (tail_q,), "B": (follow_q,), "Y": (pulse_out,)},
        ),
    }
    ports = {
        "dclk": Port(name="dclk", direction="input", bits=(dclk,)),
        "other_clk": Port(name="other_clk", direction="input", bits=(other_clk,)),
        "pulse_out": Port(name="pulse_out", direction="output", bits=(pulse_out,)),
    }
    module = Module(name="foreign_follow", ports=ports, cells=cells, netnames={})
    head = next(f for f in find_flops(module) if f.cell.name == "head")
    ctx = _build_context(module, clock_spec=None)
    assert not _has_xor_tail_pulse_recovery(head, "dclk", ctx, module)


# --- _has_dst_to_src_feedback present / absent ------------------------------


def test_has_dst_to_src_feedback_present() -> None:
    """A src-domain flop whose input fanin reaches a dst-domain flop's Q
    (the ``ack_sync`` shape) yields True — the handshake-feedback path."""
    src_clk_bit, dst_clk_bit = 2, 3
    src_q = 4
    ack_q = 5  # dst-domain ack flop's Q
    ack_sync_q = 6  # src-domain flop conditioned on ack
    cells = {
        # Source payload flop conditioned on the ack_sync flop.
        "src": Cell(
            name="src",
            type="$dffe",
            connections={
                "CLK": (src_clk_bit,),
                "EN": (ack_sync_q,),
                "D": (10,),
                "Q": (src_q,),
            },
        ),
        # ack_sync: src-domain flop whose D is the dst-domain ack Q.
        "ack_sync": Cell(
            name="ack_sync",
            type="$dff",
            connections={"CLK": (src_clk_bit,), "D": (ack_q,), "Q": (ack_sync_q,)},
        ),
        # dst-domain ack flop.
        "ack": Cell(
            name="ack",
            type="$dff",
            connections={"CLK": (dst_clk_bit,), "D": (11,), "Q": (ack_q,)},
        ),
    }
    ports = {
        "sclk": Port(name="sclk", direction="input", bits=(src_clk_bit,)),
        "dclk": Port(name="dclk", direction="input", bits=(dst_clk_bit,)),
    }
    module = Module(name="handshake_fb", ports=ports, cells=cells, netnames={})
    ctx = _build_context(module, clock_spec=None)
    src_flop = next(f for f in find_flops(module) if f.cell.name == "src")
    dst_flop = next(f for f in find_flops(module) if f.cell.name == "ack")
    crossing = _crossing(
        src_clk="sclk", dst_clk="dclk", width=2, src_flop=src_flop, dst_flop=dst_flop
    )
    flops_by_name = {f.cell.name: f for f in ctx.flops}
    assert _has_dst_to_src_feedback(
        module, crossing, ctx.domains, ctx.bit_drivers, flops_by_name
    )


def test_has_dst_to_src_feedback_absent() -> None:
    """A source flop with no path back to a dst-domain flop returns
    False (the no-feedback case CDC-012 fires on)."""
    src_clk_bit, dst_clk_bit = 2, 3
    src_q = 4
    cells = {
        # Source flop with a plain constant-fed input — no ack path.
        "src": Cell(
            name="src",
            type="$dff",
            connections={"CLK": (src_clk_bit,), "D": (10,), "Q": (src_q,)},
        ),
        "dst": Cell(
            name="dst",
            type="$dff",
            connections={"CLK": (dst_clk_bit,), "D": (src_q,), "Q": (5,)},
        ),
    }
    ports = {
        "sclk": Port(name="sclk", direction="input", bits=(src_clk_bit,)),
        "dclk": Port(name="dclk", direction="input", bits=(dst_clk_bit,)),
    }
    module = Module(name="no_fb", ports=ports, cells=cells, netnames={})
    ctx = _build_context(module, clock_spec=None)
    src_flop = next(f for f in find_flops(module) if f.cell.name == "src")
    dst_flop = next(f for f in find_flops(module) if f.cell.name == "dst")
    crossing = _crossing(
        src_clk="sclk", dst_clk="dclk", width=1, src_flop=src_flop, dst_flop=dst_flop
    )
    flops_by_name = {f.cell.name: f for f in ctx.flops}
    assert not _has_dst_to_src_feedback(
        module, crossing, ctx.domains, ctx.bit_drivers, flops_by_name
    )


def test_has_dst_to_src_feedback_no_src_flop_returns_false() -> None:
    """A crossing with no source flop short-circuits to False."""
    dst = _flop("dst", clk=2, d=(3,), q=(4,))
    module = Module(name="m", ports={}, cells={"dst": dst.cell}, netnames={})
    ctx = _build_context(module, clock_spec=None)
    crossing = _crossing(
        src_clk="sclk", dst_clk="dclk", width=1, src_flop=None, dst_flop=dst
    )
    flops_by_name = {f.cell.name: f for f in ctx.flops}
    assert not _has_dst_to_src_feedback(
        module, crossing, ctx.domains, ctx.bit_drivers, flops_by_name
    )


# --- CDC-017 src/dst guards -------------------------------------------------


def test_cdc_017_latch_no_src_flop_silent() -> None:
    """A ``$dlatch`` whose D traces to a top-level input port (no src
    flop) yields no src clock, so CDC-017 stays silent (the
    ``not src_flop_names`` early-out)."""
    dclk = 2
    din = 3
    latch_q = 4
    dst_q = 5
    module = Module(
        name="port_fed_latch",
        ports={
            "dclk": Port(name="dclk", direction="input", bits=(dclk,)),
            "din": Port(name="din", direction="input", bits=(din,)),
        },
        cells={
            # Latch D comes straight from a port — no source flop.
            "lat": Cell(
                name="lat",
                type="$dlatch",
                connections={"EN": (10,), "D": (din,), "Q": (latch_q,)},
            ),
            "dst": Cell(
                name="dst",
                type="$dff",
                connections={"CLK": (dclk,), "D": (latch_q,), "Q": (dst_q,)},
            ),
        },
        netnames={},
    )
    assert check_cdc_017(module, [], clock_spec=None) == []


def test_cdc_017_user_synced_destination_suppressed() -> None:
    """A ``$dlatch`` whose destination flop is ``(* cdc_sync *)``-marked
    is suppressed (``dst.cell.name in ctx.user_syncs``) — user vouches
    for the latch-based shape."""
    sclk, dclk = 2, 3
    src_q = 4
    latch_q = 5
    dst_q = 6
    module = Module(
        name="marked_latch",
        ports={
            "sclk": Port(name="sclk", direction="input", bits=(sclk,)),
            "dclk": Port(name="dclk", direction="input", bits=(dclk,)),
        },
        cells={
            "src": Cell(
                name="src",
                type="$dff",
                connections={"CLK": (sclk,), "D": (10,), "Q": (src_q,)},
            ),
            "lat": Cell(
                name="lat",
                type="$dlatch",
                connections={"EN": (11,), "D": (src_q,), "Q": (latch_q,)},
            ),
            "dst": Cell(
                name="dst",
                type="$dff",
                connections={"CLK": (dclk,), "D": (latch_q,), "Q": (dst_q,)},
            ),
        },
        # Mark the destination flop's Q wire as a user synchroniser.
        netnames={
            "dst_q": Netname(name="dst_q", bits=(dst_q,), attributes={"cdc_sync": "1"}),
        },
    )
    assert check_cdc_017(module, [], clock_spec=None) == []


# --- CDC-019 guards ---------------------------------------------------------


def test_cdc_019_single_lane_decoder_no_fire() -> None:
    """A shared decoder driving only ONE crossing lane doesn't satisfy
    the ``len(lanes) < 2`` requirement — CDC-019 stays silent."""
    sclk, dclk = 2, 3
    dec_y0, dec_y1 = 4, 5  # decoder has 2 output bits (passes width gate)
    src_q = 6
    dst_q = 7
    module = Module(
        name="one_lane",
        ports={
            "sclk": Port(name="sclk", direction="input", bits=(sclk,)),
            "dclk": Port(name="dclk", direction="input", bits=(dclk,)),
        },
        cells={
            "dec": Cell(
                name="dec",
                type="$or",
                connections={"A": (10, 11), "B": (12, 13), "Y": (dec_y0, dec_y1)},
            ),
            # Only one source flop registers a decoder bit and crosses.
            "src": Cell(
                name="src",
                type="$dff",
                connections={"CLK": (sclk,), "D": (dec_y0,), "Q": (src_q,)},
            ),
            "dst": Cell(
                name="dst",
                type="$dff",
                connections={"CLK": (dclk,), "D": (src_q,), "Q": (dst_q,)},
            ),
        },
        netnames={},
    )
    src_flop = next(f for f in find_flops(module) if f.cell.name == "src")
    dst_flop = next(f for f in find_flops(module) if f.cell.name == "dst")
    crossing = _crossing(
        src_clk="sclk", dst_clk="dclk", width=1, src_flop=src_flop, dst_flop=dst_flop
    )
    assert check_cdc_019(module, [crossing], clock_spec=None) == []


def test_cdc_019_gray_marked_source_suppressed() -> None:
    """Two decoder lanes both crossing, but one source flop is
    ``(* cdc_gray *)``-marked → the whole group is suppressed (the
    ``suppressed_groups`` path)."""
    sclk, dclk = 2, 3
    dec_y0, dec_y1 = 4, 5
    src_q0, src_q1 = 6, 7
    dst_q0, dst_q1 = 8, 9
    module = Module(
        name="gray_decode",
        ports={
            "sclk": Port(name="sclk", direction="input", bits=(sclk,)),
            "dclk": Port(name="dclk", direction="input", bits=(dclk,)),
        },
        cells={
            "dec": Cell(
                name="dec",
                type="$or",
                connections={"A": (10, 11), "B": (12, 13), "Y": (dec_y0, dec_y1)},
            ),
            "src0": Cell(
                name="src0",
                type="$dff",
                connections={"CLK": (sclk,), "D": (dec_y0,), "Q": (src_q0,)},
            ),
            "src1": Cell(
                name="src1",
                type="$dff",
                connections={"CLK": (sclk,), "D": (dec_y1,), "Q": (src_q1,)},
            ),
            "dst0": Cell(
                name="dst0",
                type="$dff",
                connections={"CLK": (dclk,), "D": (src_q0,), "Q": (dst_q0,)},
            ),
            "dst1": Cell(
                name="dst1",
                type="$dff",
                connections={"CLK": (dclk,), "D": (src_q1,), "Q": (dst_q1,)},
            ),
        },
        # Mark src0's Q wire (* cdc_gray *).
        netnames={
            "g0": Netname(name="g0", bits=(src_q0,), attributes={"cdc_gray": "1"}),
        },
    )
    ctx = _build_context(module, clock_spec=None)
    src0 = next(f for f in find_flops(module) if f.cell.name == "src0")
    src1 = next(f for f in find_flops(module) if f.cell.name == "src1")
    dst0 = next(f for f in find_flops(module) if f.cell.name == "dst0")
    dst1 = next(f for f in find_flops(module) if f.cell.name == "dst1")
    crossings = [
        _crossing(
            src_clk="sclk", dst_clk="dclk", width=1, src_flop=src0, dst_flop=dst0
        ),
        _crossing(
            src_clk="sclk", dst_clk="dclk", width=1, src_flop=src1, dst_flop=dst1
        ),
    ]
    assert ctx.user_grays == {"src0"}
    assert check_cdc_019(module, crossings, ctx=ctx) == []


# --- CDC-020 guards ---------------------------------------------------------


def test_cdc_020_single_lane_no_fire() -> None:
    """A multi-bit source flop sliced into only ONE dst lane doesn't
    reconverge — the ``len(dst_map) < 2`` guard keeps CDC-020 silent."""
    sclk, dclk = 2, 3
    src_q0, src_q1 = 4, 5
    dst_q = 6
    src = Cell(
        name="src",
        type="$dff",
        connections={"CLK": (sclk,), "D": (10, 11), "Q": (src_q0, src_q1)},
    )
    module = Module(
        name="one_dst",
        ports={
            "sclk": Port(name="sclk", direction="input", bits=(sclk,)),
            "dclk": Port(name="dclk", direction="input", bits=(dclk,)),
        },
        cells={
            "src": src,
            "dst": Cell(
                name="dst",
                type="$dff",
                connections={"CLK": (dclk,), "D": (src_q0,), "Q": (dst_q,)},
            ),
        },
        netnames={},
    )
    src_flop = next(f for f in find_flops(module) if f.cell.name == "src")
    dst_flop = next(f for f in find_flops(module) if f.cell.name == "dst")
    crossing = _crossing(
        src_clk="sclk", dst_clk="dclk", width=1, src_flop=src_flop, dst_flop=dst_flop
    )
    assert check_cdc_020(module, [crossing], clock_spec=None) == []


def test_cdc_020_gray_marked_source_suppressed() -> None:
    """A multi-bit source sliced into 2+ dst lanes but tagged
    ``(* cdc_gray *)`` is suppressed (``src_name in ctx.user_grays``)."""
    sclk, dclk = 2, 3
    src_q0, src_q1 = 4, 5
    dst_q0, dst_q1 = 6, 7
    src = Cell(
        name="src",
        type="$dff",
        connections={"CLK": (sclk,), "D": (10, 11), "Q": (src_q0, src_q1)},
    )
    module = Module(
        name="gray_bus",
        ports={
            "sclk": Port(name="sclk", direction="input", bits=(sclk,)),
            "dclk": Port(name="dclk", direction="input", bits=(dclk,)),
        },
        cells={
            "src": src,
            "dst0": Cell(
                name="dst0",
                type="$dff",
                connections={"CLK": (dclk,), "D": (src_q0,), "Q": (dst_q0,)},
            ),
            "dst1": Cell(
                name="dst1",
                type="$dff",
                connections={"CLK": (dclk,), "D": (src_q1,), "Q": (dst_q1,)},
            ),
        },
        netnames={
            "src_q": Netname(
                name="src_q", bits=(src_q0, src_q1), attributes={"cdc_gray": "1"}
            ),
        },
    )
    ctx = _build_context(module, clock_spec=None)
    src_flop = next(f for f in find_flops(module) if f.cell.name == "src")
    dst0 = next(f for f in find_flops(module) if f.cell.name == "dst0")
    dst1 = next(f for f in find_flops(module) if f.cell.name == "dst1")
    crossings = [
        _crossing(
            src_clk="sclk", dst_clk="dclk", width=1, src_flop=src_flop, dst_flop=dst0
        ),
        _crossing(
            src_clk="sclk", dst_clk="dclk", width=1, src_flop=src_flop, dst_flop=dst1
        ),
    ]
    assert ctx.user_grays == {"src"}
    assert check_cdc_020(module, crossings, ctx=ctx) == []


# --- RDC-002 / RDC-007 lazy context -----------------------------------------


def test_rdc_002_lazy_context_fires() -> None:
    """``check_rdc_002`` direct call (``ctx=None``) builds its own context
    and fires on the flop->flop polarity-mismatch fixture."""
    module, spec = _load_fixture("bad_rdc_002_polarity_mismatch")
    violations = check_rdc_002(module, [], spec)  # no ctx=
    rdc_002 = [v for v in violations if v.rule_id == "RDC-002"]
    assert len(rdc_002) == 1
    assert "reset polarity mismatch" in rdc_002[0].message
    assert "ARST_VALUE" in rdc_002[0].message


def test_rdc_002_port_declared_variant_lazy() -> None:
    """``check_rdc_002`` on the ``bad_marked_reset_polarity`` fixture fires
    the *port-declared* variant — a top-level reset port annotated
    ``(* reset_polarity *)`` whose declared polarity disagrees with a
    consumer flop's inferred edge (the ``port_groups`` path)."""
    module, spec = _load_fixture("bad_marked_reset_polarity")
    violations = check_rdc_002(module, [], spec)  # no ctx=
    rdc_002 = [v for v in violations if v.rule_id == "RDC-002"]
    assert len(rdc_002) >= 1
    assert any("port-level declaration" in v.message for v in rdc_002)


def test_rdc_007_lazy_context_fires() -> None:
    """``check_rdc_007`` direct call builds its own context and fires on
    the reset-sync-chain deassert-polarity-backwards fixture."""
    module, spec = _load_fixture("bad_reset_sync_deassert_polarity")
    violations = check_rdc_007(module, [], spec)  # no ctx=
    rdc_007 = [v for v in violations if v.rule_id == "RDC-007"]
    assert len(rdc_007) == 1
    assert "reset-synchroniser chain" in rdc_007[0].message
    assert "one-shot" in rdc_007[0].message


# --- CDC-017 multi-domain dedupe --------------------------------------------


def test_cdc_017_multi_bit_latch_dedupes_to_one_finding() -> None:
    """A 2-bit ``$dlatch`` whose two Q lanes feed two dst flops (same
    foreign src/dst clock pair) dedupes to ONE finding via the
    ``(latch, src_clk, dst_clk)`` key (rules.py ``seen`` set)."""
    sclk, dclk = 2, 3
    src_q0, src_q1 = 4, 5
    lat_q0, lat_q1 = 6, 7
    dst_q0, dst_q1 = 8, 9
    module = Module(
        name="multibit_latch",
        ports={
            "sclk": Port(name="sclk", direction="input", bits=(sclk,)),
            "dclk": Port(name="dclk", direction="input", bits=(dclk,)),
        },
        cells={
            "src": Cell(
                name="src",
                type="$dff",
                connections={"CLK": (sclk,), "D": (10, 11), "Q": (src_q0, src_q1)},
            ),
            "lat": Cell(
                name="lat",
                type="$dlatch",
                connections={
                    "EN": (12,),
                    "D": (src_q0, src_q1),
                    "Q": (lat_q0, lat_q1),
                },
            ),
            "dst0": Cell(
                name="dst0",
                type="$dff",
                connections={"CLK": (dclk,), "D": (lat_q0,), "Q": (dst_q0,)},
            ),
            "dst1": Cell(
                name="dst1",
                type="$dff",
                connections={"CLK": (dclk,), "D": (lat_q1,), "Q": (dst_q1,)},
            ),
        },
        netnames={},
    )
    violations = check_cdc_017(module, [], clock_spec=None)
    cdc_017 = [v for v in violations if v.rule_id == "CDC-017"]
    assert len(cdc_017) == 1  # deduped, not two
    assert cdc_017[0].cell_name == "lat"


# --- _is_gated_bus_crossing mux-on-D shape ----------------------------------


def test_is_gated_bus_crossing_mux_on_d_dst_domain_select() -> None:
    """A destination bus whose D is driven by a single ``$mux`` whose
    ``S`` (select) fans in only from dst-domain flops is recognised as a
    gated bus crossing (Shape 2, the golden handshake mux-on-D)."""
    dclk = 2
    sel_q = 3  # dst-domain select flop's Q
    hold0, hold1 = 4, 5  # mux A (hold = dst Q feedback, simplified)
    new0, new1 = 6, 7  # mux B (new payload from src)
    d0, d1 = 8, 9  # mux output → dst flop D
    dst_q0, dst_q1 = 10, 11
    module = Module(
        name="mux_on_d",
        ports={"dclk": Port(name="dclk", direction="input", bits=(dclk,))},
        cells={
            "sel_ff": Cell(
                name="sel_ff",
                type="$dff",
                connections={"CLK": (dclk,), "D": (20,), "Q": (sel_q,)},
            ),
            "hold_mux": Cell(
                name="hold_mux",
                type="$mux",
                connections={
                    "A": (hold0, hold1),
                    "B": (new0, new1),
                    "S": (sel_q,),
                    "Y": (d0, d1),
                },
            ),
            "dst": Cell(
                name="dst",
                type="$dff",
                connections={"CLK": (dclk,), "D": (d0, d1), "Q": (dst_q0, dst_q1)},
            ),
        },
        netnames={},
    )
    dst_flop = next(f for f in find_flops(module) if f.cell.name == "dst")
    crossing = _crossing(
        src_clk="sclk", dst_clk="dclk", width=2, src_flop=None, dst_flop=dst_flop
    )
    ctx = _build_context(module, clock_spec=None)
    assert _is_gated_bus_crossing(module, crossing, ctx.domains, ctx.bit_drivers)


# --- CDC-003 user-marked skips (hand-built comb-path crossing) --------------


def _comb_path_crossing_module(
    *, mark_attr: str | None, mark_dst: bool
) -> tuple[Module, Crossing]:
    """A 1-bit crossing with a comb gate on the path (min_hops=1) into a
    2FF synchroniser. ``mark_attr`` tags either the dst sync head
    (``mark_dst=True``) or the src flop's Q. Returns (module, crossing).
    """
    sclk, dclk = 2, 3
    src_q = 4
    gate_y = 5
    sync0_q = 6
    sync1_q = 7
    cells = {
        "src": Cell(
            name="src",
            type="$dff",
            connections={"CLK": (sclk,), "D": (10,), "Q": (src_q,)},
        ),
        # comb gate between src and sync first stage.
        "gate": Cell(
            name="gate", type="$not", connections={"A": (src_q,), "Y": (gate_y,)}
        ),
        "sync0": Cell(
            name="sync0",
            type="$dff",
            connections={"CLK": (dclk,), "D": (gate_y,), "Q": (sync0_q,)},
        ),
        "sync1": Cell(
            name="sync1",
            type="$dff",
            connections={"CLK": (dclk,), "D": (sync0_q,), "Q": (sync1_q,)},
        ),
    }
    netnames = {}
    if mark_attr is not None:
        tag_bit = sync0_q if mark_dst else src_q
        netnames = {
            "tagged": Netname(
                name="tagged", bits=(tag_bit,), attributes={mark_attr: "1"}
            ),
        }
    module = Module(
        name="comb_path",
        ports={
            "sclk": Port(name="sclk", direction="input", bits=(sclk,)),
            "dclk": Port(name="dclk", direction="input", bits=(dclk,)),
        },
        cells=cells,
        netnames=netnames,
    )
    src_flop = next(f for f in find_flops(module) if f.cell.name == "src")
    sync0_flop = next(f for f in find_flops(module) if f.cell.name == "sync0")
    crossing = Crossing(
        src_clock="sclk",
        dst_flop=sync0_flop,
        dst_clock="dclk",
        min_hops=1,  # the gate hop
        width=1,
        src_flop=src_flop,
    )
    return module, crossing


def test_cdc_003_fires_on_unmarked_comb_path() -> None:
    """Baseline: the unmarked comb-path-into-sync crossing fires CDC-003."""
    module, crossing = _comb_path_crossing_module(mark_attr=None, mark_dst=False)
    violations = check_cdc_003(module, [crossing], clock_spec=None)
    cdc_003 = [v for v in violations if v.rule_id == "CDC-003"]
    assert len(cdc_003) == 1
    assert "combinational logic between source flop" in cdc_003[0].message


def test_cdc_003_skips_marked_user_sync_head() -> None:
    """A ``(* cdc_sync *)``-marked destination sync head skips CDC-003
    (rules.py line 1141 ``c.dst_flop ... in ctx.user_syncs``)."""
    module, crossing = _comb_path_crossing_module(mark_attr="cdc_sync", mark_dst=True)
    ctx = _build_context(module, clock_spec=None)
    assert ctx.user_syncs == {"sync0"}
    violations = check_cdc_003(module, [crossing], ctx=ctx)
    assert [v for v in violations if v.rule_id == "CDC-003"] == []


def test_cdc_003_skips_quasi_static_source_flop() -> None:
    """A ``(* cdc_static *)``-marked source flop skips CDC-003 (rules.py
    line 1143) — the held-constant source can't glitch."""
    module, crossing = _comb_path_crossing_module(
        mark_attr="cdc_static", mark_dst=False
    )
    ctx = _build_context(module, clock_spec=None)
    assert ctx.user_statics == {"src"}
    violations = check_cdc_003(module, [crossing], ctx=ctx)
    assert [v for v in violations if v.rule_id == "CDC-003"] == []


# --- CDC-018 lazy context + multi-bit head skip -----------------------------


def test_cdc_018_lazy_context_fires() -> None:
    """A 4FF chain fires CDC-018 at the default threshold via a direct
    (``ctx=None``) call — exercises the lazy-build branch (line 4460)."""
    sclk, dclk = 2, 3
    src_q = 4
    q0, q1, q2, q3 = 5, 6, 7, 8
    cells = {
        "src": Cell(
            name="src",
            type="$dff",
            connections={"CLK": (sclk,), "D": (10,), "Q": (src_q,)},
        ),
        "s0": Cell(
            name="s0",
            type="$dff",
            connections={"CLK": (dclk,), "D": (src_q,), "Q": (q0,)},
        ),
        "s1": Cell(
            name="s1", type="$dff", connections={"CLK": (dclk,), "D": (q0,), "Q": (q1,)}
        ),
        "s2": Cell(
            name="s2", type="$dff", connections={"CLK": (dclk,), "D": (q1,), "Q": (q2,)}
        ),
        "s3": Cell(
            name="s3", type="$dff", connections={"CLK": (dclk,), "D": (q2,), "Q": (q3,)}
        ),
    }
    module = Module(
        name="cascade4",
        ports={
            "sclk": Port(name="sclk", direction="input", bits=(sclk,)),
            "dclk": Port(name="dclk", direction="input", bits=(dclk,)),
        },
        cells=cells,
        netnames={},
    )
    src_flop = next(f for f in find_flops(module) if f.cell.name == "src")
    s0 = next(f for f in find_flops(module) if f.cell.name == "s0")
    crossing = Crossing(
        src_clock="sclk",
        dst_flop=s0,
        dst_clock="dclk",
        min_hops=0,
        width=1,
        src_flop=src_flop,
    )
    violations = check_cdc_018(module, [crossing], clock_spec=None)  # lazy ctx
    cdc_018 = [v for v in violations if v.rule_id == "CDC-018"]
    assert len(cdc_018) == 1
    assert "4-flop chain" in cdc_018[0].message


def test_cdc_018_skips_multibit_head() -> None:
    """A crossing whose destination head is multi-bit (``len(head.q) !=
    1``) is skipped (rules.py line 4471-4474)."""
    sclk, dclk = 2, 3
    src_q = 4
    head = Cell(
        name="head",
        type="$dff",
        connections={"CLK": (dclk,), "D": (src_q, 10), "Q": (5, 6)},
    )
    src = Cell(
        name="src", type="$dff", connections={"CLK": (sclk,), "D": (11,), "Q": (src_q,)}
    )
    module = Module(
        name="multibit_head",
        ports={
            "sclk": Port(name="sclk", direction="input", bits=(sclk,)),
            "dclk": Port(name="dclk", direction="input", bits=(dclk,)),
        },
        cells={"head": head, "src": src},
        netnames={},
    )
    src_flop = next(f for f in find_flops(module) if f.cell.name == "src")
    head_flop = next(f for f in find_flops(module) if f.cell.name == "head")
    crossing = Crossing(
        src_clock="sclk",
        dst_flop=head_flop,
        dst_clock="dclk",
        min_hops=0,
        width=2,
        src_flop=src_flop,
    )
    assert check_cdc_018(module, [crossing], clock_spec=None) == []


# --- CDC-021 lazy context + multi-flop description --------------------------


def test_cdc_021_lazy_multi_flop_undeclared_port() -> None:
    """Two flops clocked by an undeclared port produce ONE CDC-021
    finding whose message uses the multi-flop description (rules.py
    line 4387). Direct call exercises the lazy-build branch (4361)."""
    clk_main, clk_aux = 2, 3
    cells = {
        "main_ff": Cell(
            name="main_ff",
            type="$dff",
            connections={"CLK": (clk_main,), "D": (10,), "Q": (11,)},
        ),
        "aux_ff0": Cell(
            name="aux_ff0",
            type="$dff",
            connections={"CLK": (clk_aux,), "D": (12,), "Q": (13,)},
        ),
        "aux_ff1": Cell(
            name="aux_ff1",
            type="$dff",
            connections={"CLK": (clk_aux,), "D": (14,), "Q": (15,)},
        ),
    }
    module = Module(
        name="undeclared_aux",
        ports={
            "clk_main": Port(name="clk_main", direction="input", bits=(clk_main,)),
            "clk_aux": Port(name="clk_aux", direction="input", bits=(clk_aux,)),
        },
        cells=cells,
        netnames={},
    )
    # Only clk_main has a create_clock; clk_aux is undeclared.
    spec = parse_sdc("create_clock -name clk_main -period 10.0 [get_ports clk_main]\n")
    violations = check_cdc_021(module, [], spec)  # lazy ctx
    cdc_021 = [v for v in violations if v.rule_id == "CDC-021"]
    assert len(cdc_021) == 1
    v = cdc_021[0]
    assert "clk_aux" in v.message
    assert "2 flops" in v.message  # multi-flop description


def test_cdc_021_no_sdc_returns_empty() -> None:
    """CDC-021 returns early without an SDC (line 4358-4359)."""
    module = Module(
        name="m",
        ports={"clk": Port(name="clk", direction="input", bits=(2,))},
        cells={
            "ff": Cell(
                name="ff", type="$dff", connections={"CLK": (2,), "D": (3,), "Q": (4,)}
            ),
        },
        netnames={},
    )
    assert check_cdc_021(module, [], clock_spec=None) == []


# --- CDC-019 driver-is-flop skip --------------------------------------------


def test_cdc_019_chained_register_driver_skipped() -> None:
    """When a source flop's D is driven by another flop's Q (not a comb
    decoder) the lane is skipped (rules.py line 4164 ``is_ff_cell``).
    Two such chained-register lanes → CDC-019 stays silent."""
    sclk, dclk = 2, 3
    up0_q, up1_q = 4, 5
    src0_q, src1_q = 6, 7
    dst0_q, dst1_q = 8, 9
    cells = {
        "up0": Cell(
            name="up0",
            type="$dff",
            connections={"CLK": (sclk,), "D": (10,), "Q": (up0_q,)},
        ),
        "up1": Cell(
            name="up1",
            type="$dff",
            connections={"CLK": (sclk,), "D": (11,), "Q": (up1_q,)},
        ),
        # source flops whose D is a flop Q (chained registers).
        "src0": Cell(
            name="src0",
            type="$dff",
            connections={"CLK": (sclk,), "D": (up0_q,), "Q": (src0_q,)},
        ),
        "src1": Cell(
            name="src1",
            type="$dff",
            connections={"CLK": (sclk,), "D": (up1_q,), "Q": (src1_q,)},
        ),
        "dst0": Cell(
            name="dst0",
            type="$dff",
            connections={"CLK": (dclk,), "D": (src0_q,), "Q": (dst0_q,)},
        ),
        "dst1": Cell(
            name="dst1",
            type="$dff",
            connections={"CLK": (dclk,), "D": (src1_q,), "Q": (dst1_q,)},
        ),
    }
    module = Module(
        name="chained",
        ports={
            "sclk": Port(name="sclk", direction="input", bits=(sclk,)),
            "dclk": Port(name="dclk", direction="input", bits=(dclk,)),
        },
        cells=cells,
        netnames={},
    )
    src0 = next(f for f in find_flops(module) if f.cell.name == "src0")
    src1 = next(f for f in find_flops(module) if f.cell.name == "src1")
    dst0 = next(f for f in find_flops(module) if f.cell.name == "dst0")
    dst1 = next(f for f in find_flops(module) if f.cell.name == "dst1")
    crossings = [
        _crossing(
            src_clk="sclk", dst_clk="dclk", width=1, src_flop=src0, dst_flop=dst0
        ),
        _crossing(
            src_clk="sclk", dst_clk="dclk", width=1, src_flop=src1, dst_flop=dst1
        ),
    ]
    assert check_cdc_019(module, crossings, clock_spec=None) == []


# --- CDC-020 static-marked source suppressed --------------------------------


def test_cdc_020_static_marked_source_suppressed() -> None:
    """A ``(* cdc_static *)``-marked multi-bit source sliced into 2+ dst
    lanes is suppressed (rules.py line 4285 ``src_name in
    ctx.user_statics``)."""
    sclk, dclk = 2, 3
    src_q0, src_q1 = 4, 5
    dst_q0, dst_q1 = 6, 7
    src = Cell(
        name="src",
        type="$dff",
        connections={"CLK": (sclk,), "D": (10, 11), "Q": (src_q0, src_q1)},
    )
    module = Module(
        name="static_bus",
        ports={
            "sclk": Port(name="sclk", direction="input", bits=(sclk,)),
            "dclk": Port(name="dclk", direction="input", bits=(dclk,)),
        },
        cells={
            "src": src,
            "dst0": Cell(
                name="dst0",
                type="$dff",
                connections={"CLK": (dclk,), "D": (src_q0,), "Q": (dst_q0,)},
            ),
            "dst1": Cell(
                name="dst1",
                type="$dff",
                connections={"CLK": (dclk,), "D": (src_q1,), "Q": (dst_q1,)},
            ),
        },
        netnames={
            "cfg": Netname(
                name="cfg", bits=(src_q0, src_q1), attributes={"cdc_static": "1"}
            ),
        },
    )
    ctx = _build_context(module, clock_spec=None)
    src_flop = next(f for f in find_flops(module) if f.cell.name == "src")
    dst0 = next(f for f in find_flops(module) if f.cell.name == "dst0")
    dst1 = next(f for f in find_flops(module) if f.cell.name == "dst1")
    crossings = [
        _crossing(
            src_clk="sclk", dst_clk="dclk", width=1, src_flop=src_flop, dst_flop=dst0
        ),
        _crossing(
            src_clk="sclk", dst_clk="dclk", width=1, src_flop=src_flop, dst_flop=dst1
        ),
    ]
    assert ctx.user_statics == {"src"}
    assert check_cdc_020(module, crossings, ctx=ctx) == []


# --- RDC-003 no-SDC fallback + reset-tree truncation ------------------------


def test_rdc_003_no_sdc_fallback_multi_consumer_truncates() -> None:
    """With ``clock_spec=None`` RDC-003 uses the ``a != b`` async fallback
    (line 2318). A foreign-domain SRST source feeding >3 same-domain
    consumers becomes ONE finding with the truncated reset-tree
    description (line 2347, ', ...')."""
    clk_a, clk_b = 2, 3
    src_q = 4
    cells = {
        "src": Cell(
            name="src",
            type="$dff",
            connections={"CLK": (clk_b,), "D": (src_q,), "Q": (src_q,)},
        ),
    }
    for i in range(4):
        name = f"dst{i}"
        cells[name] = Cell(
            name=name,
            type="$sdff",
            connections={
                "CLK": (clk_a,),
                "SRST": (src_q,),
                "D": (20 + i,),
                "Q": (30 + i,),
            },
        )
    module = Module(
        name="rdc003_tree",
        ports={
            "clk_a": Port(name="clk_a", direction="input", bits=(clk_a,)),
            "clk_b": Port(name="clk_b", direction="input", bits=(clk_b,)),
        },
        cells=cells,
        netnames={},
    )
    violations = check_rdc_003(module, [], clock_spec=None)  # lazy ctx + a!=b
    rdc_003 = [v for v in violations if v.rule_id == "RDC-003"]
    assert len(rdc_003) == 1
    msg = rdc_003[0].message
    assert "4 destination flops share this source" in msg
    assert ", ..." in msg


# --- CDC-016 reported-heads dedupe ------------------------------------------


def test_cdc_016_dedupes_repeated_head() -> None:
    """An opposite-edge chain reported once even when two crossings share
    the same destination head (rules.py line 3933 ``reported_heads``)."""
    sclk, dclk = 2, 3
    src_q = 4
    s0_q = 5
    s1_q = 6
    # stage0 posedge ($dff CLK_POLARITY default), stage1 negedge.
    s0 = Cell(
        name="s0",
        type="$dff",
        connections={"CLK": (dclk,), "D": (src_q,), "Q": (s0_q,)},
        parameters={"CLK_POLARITY": "1"},
    )
    s1 = Cell(
        name="s1",
        type="$dff",
        connections={"CLK": (dclk,), "D": (s0_q,), "Q": (s1_q,)},
        parameters={"CLK_POLARITY": "0"},
    )
    src = Cell(
        name="src", type="$dff", connections={"CLK": (sclk,), "D": (10,), "Q": (src_q,)}
    )
    module = Module(
        name="opp_edge",
        ports={
            "sclk": Port(name="sclk", direction="input", bits=(sclk,)),
            "dclk": Port(name="dclk", direction="input", bits=(dclk,)),
        },
        cells={"s0": s0, "s1": s1, "src": src},
        netnames={},
    )
    src_flop = next(f for f in find_flops(module) if f.cell.name == "src")
    head = next(f for f in find_flops(module) if f.cell.name == "s0")
    crossings = [
        _crossing(
            src_clk="sclk", dst_clk="dclk", width=1, src_flop=src_flop, dst_flop=head
        ),
        _crossing(
            src_clk="sclk", dst_clk="dclk", width=1, src_flop=src_flop, dst_flop=head
        ),
    ]
    violations = check_cdc_016(module, crossings, clock_spec=None)
    cdc_016 = [v for v in violations if v.rule_id == "CDC-016"]
    assert len(cdc_016) == 1  # deduped despite two crossings
    assert "opposite-edge synchroniser" in cdc_016[0].message


# --- CDC-010 unclassifiable clock inputs (silent) ---------------------------


def test_cdc_010_silent_when_clock_inputs_unclassifiable() -> None:
    """A clock mux whose clock inputs trace to neither a flop Q nor an
    SDC clock port leaves ``cell_clock_domains`` empty, so CDC-010 stays
    silent (rules.py line 2151-2155). Here the mux's A/B are fed by
    plain comb gates from untyped data ports — no classifiable domain —
    while the select is a flop, so ctrl_fanin is non-empty."""
    a_in, b_in = 2, 3
    sel_d, sel_q = 4, 5
    a_y, b_y = 6, 7
    ck_out, d_in, q_out = 8, 9, 10
    # No create_clock for the data ports → unclassifiable clock inputs.
    ports = {
        "a_in": Port(name="a_in", direction="input", bits=(a_in,)),
        "b_in": Port(name="b_in", direction="input", bits=(b_in,)),
        "sel_d": Port(name="sel_d", direction="input", bits=(sel_d,)),
        "d_in": Port(name="d_in", direction="input", bits=(d_in,)),
        "q_out": Port(name="q_out", direction="output", bits=(q_out,)),
    }
    cells = {
        # Select flop has a domain only via its own CLK = a comb-derived
        # net? No — give it a real clock so ctrl_fanin classifies. We use
        # the out_ff's own clock-net as the select flop's clock.
        "sel_ff": Cell(
            name="sel_ff",
            type="$dff",
            connections={"CLK": (ck_out,), "D": (sel_d,), "Q": (sel_q,)},
        ),
        # Mux clock inputs are comb-gate outputs from untyped ports.
        "a_buf": Cell(
            name="a_buf", type="$not", connections={"A": (a_in,), "Y": (a_y,)}
        ),
        "b_buf": Cell(
            name="b_buf", type="$not", connections={"A": (b_in,), "Y": (b_y,)}
        ),
        "clk_mux": Cell(
            name="clk_mux",
            type="$mux",
            connections={"A": (b_y,), "B": (a_y,), "S": (sel_q,), "Y": (ck_out,)},
        ),
        "out_ff": Cell(
            name="out_ff",
            type="$dff",
            connections={"CLK": (ck_out,), "D": (d_in,), "Q": (q_out,)},
        ),
    }
    module = Module(name="unclass_clk_mux", ports=ports, cells=cells, netnames={})
    # SDC declares no clocks at all → clock_for_port returns None for the
    # data ports, so _clock_input_domains_for finds no domains.
    spec = parse_sdc("")
    violations = check_cdc_010(module, [], spec)
    assert [v for v in violations if v.rule_id == "CDC-010"] == []


# --- CDC-017 latch-shape guards ---------------------------------------------


def test_cdc_017_latch_empty_q_or_d_skipped() -> None:
    """A ``$dlatch`` with empty Q (or D) connections is skipped (rules.py
    line 4021-4022)."""
    module = Module(
        name="empty_latch",
        ports={"clk": Port(name="clk", direction="input", bits=(2,))},
        cells={
            # Latch with no Q bits.
            "lat": Cell(
                name="lat", type="$dlatch", connections={"EN": (3,), "D": (4,), "Q": ()}
            ),
            "ff": Cell(
                name="ff", type="$dff", connections={"CLK": (2,), "D": (5,), "Q": (6,)}
            ),
        },
        netnames={},
    )
    assert check_cdc_017(module, [], clock_spec=None) == []


def test_cdc_017_latch_constant_q_bit_skipped() -> None:
    """A ``$dlatch`` whose Q bit is a constant string contributes no dst
    flop (the ``not isinstance(q_bit, int)`` continue, line 4028), so
    CDC-017 stays silent."""
    module = Module(
        name="const_q_latch",
        ports={"clk": Port(name="clk", direction="input", bits=(2,))},
        cells={
            "lat": Cell(
                name="lat",
                type="$dlatch",
                connections={"EN": (3,), "D": (4,), "Q": ("0",)},
            ),
        },
        netnames={},
    )
    assert check_cdc_017(module, [], clock_spec=None) == []


def test_cdc_017_latch_constant_d_bit_no_src_skipped() -> None:
    """A ``$dlatch`` whose D is all-constant has no int D bits — the
    ``if not d_int_bits: continue`` guard (line 4044-4045) keeps CDC-017
    silent even though a dst flop reads its Q."""
    dclk = 2
    latch_q = 3
    dst_q = 4
    module = Module(
        name="const_d_latch",
        ports={"dclk": Port(name="dclk", direction="input", bits=(dclk,))},
        cells={
            # Latch D is a constant — no source flop.
            "lat": Cell(
                name="lat",
                type="$dlatch",
                connections={"EN": (10,), "D": ("0",), "Q": (latch_q,)},
            ),
            "dst": Cell(
                name="dst",
                type="$dff",
                connections={"CLK": (dclk,), "D": (latch_q,), "Q": (dst_q,)},
            ),
        },
        netnames={},
    )
    assert check_cdc_017(module, [], clock_spec=None) == []


# --- RDC-002 port-declared multi-flop truncation ----------------------------


def test_rdc_002_port_declared_multi_consumer_truncates() -> None:
    """A reset port annotated ``(* reset_polarity = "high" *)`` whose
    declared polarity disagrees with >3 active-low consumer flops emits
    one RDC-002 finding using the truncated port-group description
    (rules.py line 2874)."""
    clk = 2
    rst = 3
    cells = {}
    for i in range(4):
        name = f"ff{i}"
        # active-low flops (ARST_POLARITY=0) reset directly from the port.
        cells[name] = Cell(
            name=name,
            type="$adff",
            connections={"CLK": (clk,), "ARST": (rst,), "D": (20 + i,), "Q": (30 + i,)},
            parameters={"ARST_POLARITY": "0", "ARST_VALUE": "0"},
        )
    module = Module(
        name="rdc002_port_tree",
        ports={
            "clk": Port(name="clk", direction="input", bits=(clk,)),
            "rst": Port(name="rst", direction="input", bits=(rst,)),
        },
        cells=cells,
        # Port declared active-high — disagrees with the active-low flops.
        netnames={
            "rst": Netname(
                name="rst", bits=(rst,), attributes={"reset_polarity": "high"}
            ),
        },
    )
    violations = check_rdc_002(module, [], clock_spec=None)  # lazy ctx
    rdc_002 = [v for v in violations if v.rule_id == "RDC-002"]
    assert len(rdc_002) == 1
    msg = rdc_002[0].message
    assert "port-level declaration" in msg
    assert "disagree with the" in msg
    assert ", ..." in msg


# --- RDC-001 synchronous-domain ARST is not a crossing ----------------------


def test_rdc_001_synchronous_domains_not_reported() -> None:
    """When the SDC declares the source and destination clocks
    *synchronous* (same group, or simply not in any async group),
    ``_async`` returns False and RDC-001 skips the ARST edge (rules.py
    line 2250-2251) — no finding."""
    clk_a, clk_b = 2, 3
    src_q = 4
    dst_q = 5
    module = Module(
        name="sync_domains",
        ports={
            "clk_a": Port(name="clk_a", direction="input", bits=(clk_a,)),
            "clk_b": Port(name="clk_b", direction="input", bits=(clk_b,)),
        },
        cells={
            "src": Cell(
                name="src",
                type="$dff",
                connections={"CLK": (clk_b,), "D": (src_q,), "Q": (src_q,)},
            ),
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
    # Both clocks declared, but NO set_clock_groups -asynchronous → they
    # are treated as synchronous, so are_async() is False.
    spec = parse_sdc(
        "create_clock -name clk_a -period 10.0 [get_ports clk_a]\n"
        "create_clock -name clk_b -period 10.0 [get_ports clk_b]\n"
    )
    violations = check_rdc_001(module, [], spec)
    assert [v for v in violations if v.rule_id == "RDC-001"] == []


# --- CDC-021 internal generated clock is not a port -------------------------


def test_cdc_021_internal_generated_clock_skipped() -> None:
    """A flop whose CLK is an *internal* divided clock (a flop Q, not a
    top-level port) yields a domain that isn't a module port — the
    ``port is None`` skip (rules.py line 4375-4376). CDC-021 stays
    silent on it."""
    clk = 2
    div_q = 3  # internal divided clock net
    leaf_q = 4
    module = Module(
        name="internal_clk",
        ports={"clk": Port(name="clk", direction="input", bits=(clk,))},
        cells={
            # Divider flop: produces an internal clock net.
            "div": Cell(
                name="div",
                type="$dff",
                connections={"CLK": (clk,), "D": (div_q,), "Q": (div_q,)},
            ),
            # Leaf flop clocked by the internal divided net.
            "leaf": Cell(
                name="leaf",
                type="$dff",
                connections={"CLK": (div_q,), "D": (10,), "Q": (leaf_q,)},
            ),
        },
        netnames={},
    )
    spec = parse_sdc("create_clock -name clk -period 10.0 [get_ports clk]\n")
    violations = check_cdc_021(module, [], spec)
    # The leaf's domain is an internal net, not a port → not flagged.
    assert [v for v in violations if v.rule_id == "CDC-021"] == []


# --- CDC-018 constant-head skip ---------------------------------------------


def test_cdc_018_constant_head_d_skipped() -> None:
    """A crossing whose destination head's single D bit is a constant
    string is skipped (rules.py line 4473-4474 ``not isinstance(head.d[0],
    int)``)."""
    sclk, dclk = 2, 3
    src_q = 4
    head_q = 5
    head = Cell(
        name="head",
        type="$dff",
        connections={"CLK": (dclk,), "D": ("0",), "Q": (head_q,)},
    )
    src = Cell(
        name="src", type="$dff", connections={"CLK": (sclk,), "D": (10,), "Q": (src_q,)}
    )
    module = Module(
        name="const_head",
        ports={
            "sclk": Port(name="sclk", direction="input", bits=(sclk,)),
            "dclk": Port(name="dclk", direction="input", bits=(dclk,)),
        },
        cells={"head": head, "src": src},
        netnames={},
    )
    src_flop = next(f for f in find_flops(module) if f.cell.name == "src")
    head_flop = next(f for f in find_flops(module) if f.cell.name == "head")
    crossing = Crossing(
        src_clock="sclk",
        dst_flop=head_flop,
        dst_clock="dclk",
        min_hops=0,
        width=1,
        src_flop=src_flop,
    )
    assert check_cdc_018(module, [crossing], clock_spec=None) == []


# --- RDC-003 synchronous SRST source not reported ---------------------------


def test_rdc_003_synchronous_srst_source_not_reported() -> None:
    """An SRST driven by a foreign-domain flop whose clocks are declared
    *synchronous* (no async group) is skipped by RDC-003 (the
    ``not _async`` continue, rules.py line 2336-2337)."""
    clk_a, clk_b = 2, 3
    src_q = 4
    dst_q = 5
    module = Module(
        name="sync_srst",
        ports={
            "clk_a": Port(name="clk_a", direction="input", bits=(clk_a,)),
            "clk_b": Port(name="clk_b", direction="input", bits=(clk_b,)),
        },
        cells={
            "src": Cell(
                name="src",
                type="$dff",
                connections={"CLK": (clk_b,), "D": (src_q,), "Q": (src_q,)},
            ),
            "dst": Cell(
                name="dst",
                type="$sdff",
                connections={
                    "CLK": (clk_a,),
                    "SRST": (src_q,),
                    "D": (6,),
                    "Q": (dst_q,),
                },
            ),
        },
        netnames={},
    )
    spec = parse_sdc(
        "create_clock -name clk_a -period 10.0 [get_ports clk_a]\n"
        "create_clock -name clk_b -period 10.0 [get_ports clk_b]\n"
    )
    violations = check_rdc_003(module, [], spec)
    assert [v for v in violations if v.rule_id == "RDC-003"] == []


def test_rdc_003_unclassifiable_consumer_clock_skipped() -> None:
    """An SRST consumer flop whose CLK is unclassifiable (constant →
    domain None) is skipped (rules.py line 2329-2330) — no finding even
    with a foreign SRST source."""
    clk_b = 3
    src_q = 4
    dst_q = 5
    module = Module(
        name="rdc003_no_clk",
        ports={"clk_b": Port(name="clk_b", direction="input", bits=(clk_b,))},
        cells={
            "src": Cell(
                name="src",
                type="$dff",
                connections={"CLK": (clk_b,), "D": (src_q,), "Q": (src_q,)},
            ),
            # consumer CLK is a constant → domain None.
            "dst": Cell(
                name="dst",
                type="$sdff",
                connections={"CLK": ("0",), "SRST": (src_q,), "D": (6,), "Q": (dst_q,)},
            ),
        },
        netnames={},
    )
    assert check_rdc_003(module, [], clock_spec=None) == []


# --- CDC-015 synchronous reset source not reported --------------------------


def test_cdc_015_synchronous_reset_source_not_reported() -> None:
    """A sync-chain whose ARST comes from a foreign flop in a
    *synchronous* domain is not reported by CDC-015 (the ``not _async``
    continue, rules.py line 3837-3838)."""
    from rtl_buddy_cdc.rules import check_cdc_015

    dclk, other_clk = 2, 3
    head_d, head_q = 4, 5
    tail_q = 6
    other_q = 7
    cells = {
        "other": Cell(
            name="other",
            type="$dff",
            connections={"CLK": (other_clk,), "D": (20,), "Q": (other_q,)},
        ),
        "head": Cell(
            name="head",
            type="$adff",
            connections={
                "CLK": (dclk,),
                "ARST": (other_q,),
                "D": (head_d,),
                "Q": (head_q,),
            },
        ),
        "tail": Cell(
            name="tail",
            type="$dff",
            connections={"CLK": (dclk,), "D": (head_q,), "Q": (tail_q,)},
        ),
    }
    module = Module(
        name="cdc015_sync",
        ports={
            "dclk": Port(name="dclk", direction="input", bits=(dclk,)),
            "other_clk": Port(name="other_clk", direction="input", bits=(other_clk,)),
        },
        cells=cells,
        netnames={},
    )
    head_flop = next(f for f in find_flops(module) if f.cell.name == "head")
    crossing = Crossing(
        src_clock="other_clk",
        dst_flop=head_flop,
        dst_clock="dclk",
        min_hops=0,
        width=1,
        src_flop=None,
    )
    spec = parse_sdc(
        "create_clock -name dclk -period 10.0 [get_ports dclk]\n"
        "create_clock -name other_clk -period 10.0 [get_ports other_clk]\n"
    )
    violations = check_cdc_015(module, [crossing], spec)
    assert [v for v in violations if v.rule_id == "CDC-015"] == []


# --- _has_dst_to_src_feedback gate-level C-pin + no-input src ----------------


def test_has_dst_to_src_feedback_gate_level_c_pin_present() -> None:
    """A gate-level ``$_DFF_P_`` source flop (clock on pin ``C``) whose
    ``D`` fanin reaches a dst-domain flop's Q still detects feedback —
    ``_flop_input_fanin_bits`` skips the ``C`` pin (rules.py line 3516)
    but walks ``D``."""
    src_clk_bit, dst_clk_bit = 2, 3
    src_q = 4
    ack_q = 5  # dst-domain ack flop's Q feeds the src flop's D
    cells = {
        # Gate-level src flop: clock on C, D from the dst-domain ack.
        "src": Cell(
            name="src",
            type="$_DFF_P_",
            connections={"C": (src_clk_bit,), "D": (ack_q,), "Q": (src_q,)},
        ),
        "ack": Cell(
            name="ack",
            type="$dff",
            connections={"CLK": (dst_clk_bit,), "D": (11,), "Q": (ack_q,)},
        ),
    }
    module = Module(
        name="gatelevel_fb",
        ports={
            "sclk": Port(name="sclk", direction="input", bits=(src_clk_bit,)),
            "dclk": Port(name="dclk", direction="input", bits=(dst_clk_bit,)),
        },
        cells=cells,
        netnames={},
    )
    ctx = _build_context(module, clock_spec=None)
    src_flop = next(f for f in find_flops(module) if f.cell.name == "src")
    dst_flop = next(f for f in find_flops(module) if f.cell.name == "ack")
    crossing = _crossing(
        src_clk="sclk", dst_clk="dclk", width=1, src_flop=src_flop, dst_flop=dst_flop
    )
    flops_by_name = {f.cell.name: f for f in ctx.flops}
    assert _has_dst_to_src_feedback(
        module, crossing, ctx.domains, ctx.bit_drivers, flops_by_name
    )


def test_has_dst_to_src_feedback_src_with_only_constant_input() -> None:
    """A src flop whose only D input is a constant has no int input bits;
    ``_flop_input_fanin_bits`` returns empty and the walk skips it
    (rules.py line 3565-3566), yielding no feedback."""
    src_clk_bit, dst_clk_bit = 2, 3
    src_q = 4
    cells = {
        # D is a constant — no int fanin bits.
        "src": Cell(
            name="src",
            type="$dff",
            connections={"CLK": (src_clk_bit,), "D": ("0",), "Q": (src_q,)},
        ),
        "dst": Cell(
            name="dst",
            type="$dff",
            connections={"CLK": (dst_clk_bit,), "D": (src_q,), "Q": (5,)},
        ),
    }
    module = Module(
        name="const_src_fb",
        ports={
            "sclk": Port(name="sclk", direction="input", bits=(src_clk_bit,)),
            "dclk": Port(name="dclk", direction="input", bits=(dst_clk_bit,)),
        },
        cells=cells,
        netnames={},
    )
    ctx = _build_context(module, clock_spec=None)
    src_flop = next(f for f in find_flops(module) if f.cell.name == "src")
    dst_flop = next(f for f in find_flops(module) if f.cell.name == "dst")
    crossing = _crossing(
        src_clk="sclk", dst_clk="dclk", width=1, src_flop=src_flop, dst_flop=dst_flop
    )
    flops_by_name = {f.cell.name: f for f in ctx.flops}
    assert not _has_dst_to_src_feedback(
        module, crossing, ctx.domains, ctx.bit_drivers, flops_by_name
    )


# --- CDC-017 src/dst unclassifiable-clock guards ----------------------------


def test_cdc_017_unclassifiable_dst_clock_skipped() -> None:
    """A latch whose Q is read by a flop with an unclassifiable clock
    (CLK tied to a constant → domain None) contributes no dst clock —
    CDC-017 stays silent (rules.py line 4034-4036)."""
    src_q = 4
    latch_q = 5
    dst_q = 6
    module = Module(
        name="unclass_dst",
        ports={"sclk": Port(name="sclk", direction="input", bits=(2,))},
        cells={
            "src": Cell(
                name="src",
                type="$dff",
                connections={"CLK": (2,), "D": (10,), "Q": (src_q,)},
            ),
            "lat": Cell(
                name="lat",
                type="$dlatch",
                connections={"EN": (11,), "D": (src_q,), "Q": (latch_q,)},
            ),
            "dst": Cell(
                name="dst",
                type="$dff",
                connections={"CLK": ("0",), "D": (latch_q,), "Q": (dst_q,)},
            ),
        },
        netnames={},
    )
    assert check_cdc_017(module, [], clock_spec=None) == []


def test_cdc_017_unclassifiable_src_clock_skipped() -> None:
    """A latch whose D is driven by a flop with an unclassifiable clock
    (CLK tied to a constant → domain None) yields no src clock — the
    ``if not src_clocks: continue`` guard keeps CDC-017 silent (rules.py
    lines 4052-4056)."""
    dclk = 2
    src_q = 4
    latch_q = 5
    dst_q = 6
    module = Module(
        name="unclass_src",
        ports={"dclk": Port(name="dclk", direction="input", bits=(dclk,))},
        cells={
            "src": Cell(
                name="src",
                type="$dff",
                connections={"CLK": ("0",), "D": (10,), "Q": (src_q,)},
            ),
            "lat": Cell(
                name="lat",
                type="$dlatch",
                connections={"EN": (11,), "D": (src_q,), "Q": (latch_q,)},
            ),
            "dst": Cell(
                name="dst",
                type="$dff",
                connections={"CLK": (dclk,), "D": (latch_q,), "Q": (dst_q,)},
            ),
        },
        netnames={},
    )
    assert check_cdc_017(module, [], clock_spec=None) == []
