"""Regression sentinel: G-9 glitchless-clock-mux attribute suppression (rtl-buddy-cdc#208).

A simple 2:1 ``$mux`` driven by a foreign-domain select that the
user vouches is glitch-free (e.g. via an external library cell or
a hand-built cross-coupled-latch envelope). The standard CDC-010
fix advice — "synchronise the select onto one of the gated clocks"
— is wrong for this case: it would actually break a correctly-
constructed glitchless mux by introducing a single-clock dependency
that defeats the other-clock-aware gating.

The new ``(* glitchless_clock_mux *)`` attribute is the user's
explicit promise, parallel to ``(* cdc_sync *)`` for synchronisers
and ``(* cdc_gray *)`` for gray-coded buses. Two cases:

* Unmarked — CDC-010 fires on the select (today's behaviour).
* Marked — CDC-010 stays silent on the marked select wire.
"""

from __future__ import annotations

from collections.abc import Iterator

from .base import ExpectedFinding, Op, RenderedCase

_TEMPLATE_UNMARKED = """\
module {top} (
    input  logic ck0_a,
    input  logic ck0_b,
    input  logic ck1,
    input  logic sel_d,
    input  logic d_in,
    output logic q_out
);
    logic sel_q;
    always_ff @(posedge ck1) sel_q <= sel_d;

    logic ck_out;
    assign ck_out = sel_q ? ck0_a : ck0_b;

    always_ff @(posedge ck_out) q_out <= d_in;
endmodule
"""

_TEMPLATE_MARKED = """\
module {top} (
    input  logic ck0_a,
    input  logic ck0_b,
    input  logic ck1,
    input  logic sel_d,
    input  logic d_in,
    output logic q_out
);
    // User vouches that the surrounding clock-mux topology is
    // glitchless (e.g. an external cross-coupled-latch envelope or
    // a foundry library cell that handles the safe handoff).
    (* glitchless_clock_mux *) logic sel_q;
    always_ff @(posedge ck1) sel_q <= sel_d;

    logic ck_out;
    assign ck_out = sel_q ? ck0_a : ck0_b;

    always_ff @(posedge ck_out) q_out <= d_in;
endmodule
"""

_SDC = """\
create_clock -name ck0 -period 10.0 [get_ports {{ck0_a ck0_b}}]
create_clock -name ck1 -period 7.5  [get_ports ck1]
set_clock_groups -asynchronous -group {{ck0}} -group {{ck1}}
set_input_delay -clock ck1 1.0 [get_ports sel_d]
set_input_delay -clock ck0 1.0 [get_ports d_in]
"""


class GapG9GlitchlessMuxUnmarked:
    """Regression sentinel: unmarked clock-mux select fires CDC-010 today."""

    name = "gap_g9_glitchless_mux_unmarked"

    @classmethod
    def cases(cls) -> Iterator[RenderedCase]:
        top = f"fuzz_{cls.name}"
        yield RenderedCase(
            template_name=cls.name,
            case_id=top,
            sv=_TEMPLATE_UNMARKED.format(top=top),
            sdc=_SDC,
            top=top,
            params={},
            expected=(ExpectedFinding("CDC-010", Op.GE, 1),),
        )


class GapG9GlitchlessMuxMarked:
    """Regression sentinel: `(* glitchless_clock_mux *)`-marked select suppresses CDC-010."""

    name = "gap_g9_glitchless_mux_marked"

    @classmethod
    def cases(cls) -> Iterator[RenderedCase]:
        top = f"fuzz_{cls.name}"
        yield RenderedCase(
            template_name=cls.name,
            case_id=top,
            sv=_TEMPLATE_MARKED.format(top=top),
            sdc=_SDC,
            top=top,
            params={},
            expected=(),
            forbidden=(ExpectedFinding("CDC-010", Op.ZERO),),
        )
