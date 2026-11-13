"""CDC-018 — cascaded synchroniser, swept around the depth threshold.

``gap_g2`` pins the one shape the rule was written for (a 4-deep
chain at the default threshold, SDC periods swept). This template
sweeps the two dimensions the detector actually compares
(rtl-buddy-cdc#331):

- **chain depth vs ``--cdc-018-depth-threshold``.** Depth 2..7 against
  thresholds 3, 4 (the default) and 6; the rule fires iff
  ``depth >= threshold``. The threshold reaches the analyzer through
  ``params["cdc_018_depth_threshold"]``, the same channel
  ``required_depth`` uses for ``--sync-depth``.
- **where the chain stops being a chain.** ``_sync_chain_depth`` only
  follows single-reader flop→flop hops, so a mid-chain tap (the synced
  value used *and* further delayed) ends the chain at the tapped
  stage. A 6-deep run tapped through logic at stage 2 is a 2FF sync
  plus a pipeline (silent); tapped at stage 4 it is a genuine 4-deep
  chain (fires).

  **Pinned gap** (``gap_note`` on the ``tapport`` cases): a tap that
  goes *straight to a top-level output port* is not counted as a
  reader — ``_bit_reader_count`` only counts cell input pins — so the
  walker runs through it and CDC-018 fires on a 2FF sync whose output
  is exported and also pipelined. The sentinel asserts today's
  behaviour; fixing the reader count flips it.

Suppression variant: ``(* cdc_sync *)`` on the chain head vouches for
the depth and silences the rule at any depth.

CDC-001 / CDC-002 must stay silent on every case — the chain is
always at least the textbook 2FF.
"""

from __future__ import annotations

from collections.abc import Iterator
from itertools import product

from .base import ExpectedFinding, Op, RenderedCase

_SDC = """\
create_clock -name src_clk -period 10.0 [get_ports src_clk]
create_clock -name dst_clk -period 7.0 [get_ports dst_clk]
set_clock_groups -asynchronous -group {src_clk} -group {dst_clk}
set_input_delay -clock src_clk 1.0 [get_ports d_in]
"""

_DEPTHS = [2, 3, 4, 5, 6, 7]
_THRESHOLDS = [3, 4, 6]


def _render(
    top: str,
    depth: int,
    *,
    head_attr: str = "",
    tap: int | None = None,
    tap_via_logic: bool = False,
) -> str:
    """``depth``-flop dst chain ``s0 .. s{depth-1}`` behind a src flop.

    ``tap`` adds an output reading stage ``s{tap-1}`` (1-based stage
    number) so that stage has a second reader — through an ``$and``
    with an enable port when ``tap_via_logic``, else wired straight to
    the port.
    """
    decls = "\n".join(
        f"    {head_attr if i == 0 else ''}logic s{i};" for i in range(depth)
    )
    stages = "\n".join(
        f"        s{i} <= {'src_q' if i == 0 else f's{i - 1}'};" for i in range(depth)
    )
    tap_port = ""
    tap_assign = ""
    if tap is not None:
        tap_port = "    output logic tap_out,\n"
        tap_expr = f"s{tap - 1} & tap_en" if tap_via_logic else f"s{tap - 1}"
        tap_assign = f"    assign tap_out = {tap_expr};\n"
        if tap_via_logic:
            tap_port = "    input  logic tap_en,\n" + tap_port
    return f"""\
module {top} (
    input  logic src_clk,
    input  logic dst_clk,
    input  logic d_in,
{tap_port}    output logic q_out
);
    logic src_q;
    always_ff @(posedge src_clk) src_q <= d_in;

{decls}
    always_ff @(posedge dst_clk) begin
{stages}
    end

    assign q_out = s{depth - 1};
{tap_assign}endmodule
"""


_QUIET = (ExpectedFinding("CDC-001", Op.ZERO), ExpectedFinding("CDC-002", Op.ZERO))


class CascadedChainDepth:
    """N-deep dst chain swept against the CDC-018 threshold."""

    name = "cdc018_chain_depth"

    @classmethod
    def cases(cls) -> Iterator[RenderedCase]:
        # Canonical (first) case is a firing one so the mutant layer's
        # parent for this family carries the bug: depth 4, default
        # threshold.
        order = sorted(
            product(_DEPTHS, _THRESHOLDS),
            key=lambda dt: (dt != (4, 4), dt),
        )
        for depth, threshold in order:
            top = f"fuzz_{cls.name}_d{depth}_t{threshold}"
            fires = depth >= threshold
            yield RenderedCase(
                template_name=cls.name,
                case_id=top,
                sv=_render(top, depth),
                sdc=_SDC,
                top=top,
                params={"depth": depth, "cdc_018_depth_threshold": threshold},
                expected=(ExpectedFinding("CDC-018", Op.EQ, 1),) if fires else (),
                forbidden=_QUIET
                + (() if fires else (ExpectedFinding("CDC-018", Op.ZERO),)),
            )

        # Mid-chain tap: the chain the detector measures ends at the
        # first stage with a second reader.
        for tap, fires in ((2, False), (4, True)):
            top = f"fuzz_{cls.name}_d6_taplogic{tap}"
            yield RenderedCase(
                template_name=cls.name,
                case_id=top,
                sv=_render(top, 6, tap=tap, tap_via_logic=True),
                sdc=_SDC,
                top=top,
                params={"depth": 6, "tap": tap, "tap_kind": "logic"},
                expected=(ExpectedFinding("CDC-018", Op.EQ, 1),) if fires else (),
                forbidden=_QUIET
                + (() if fires else (ExpectedFinding("CDC-018", Op.ZERO),)),
            )
        # Same tap wired straight to an output port: not seen as a
        # reader, so the chain runs on to depth 6 and the rule fires.
        top = f"fuzz_{cls.name}_d6_tapport2"
        yield RenderedCase(
            template_name=cls.name,
            case_id=top,
            sv=_render(top, 6, tap=2),
            sdc=_SDC,
            top=top,
            params={"depth": 6, "tap": 2, "tap_kind": "port"},
            expected=(ExpectedFinding("CDC-018", Op.EQ, 1),),
            forbidden=_QUIET,
            gap_note=(
                "rtl-buddy-cdc#331: _bit_reader_count ignores top-level output "
                "ports, so a stage exported straight to a port still extends "
                "the chain. Correct verdict is silent (2FF + pipeline); flip "
                "this expectation when port reads count as readers."
            ),
        )

        # Suppression: the user vouches for the chain (and its depth).
        for depth in (4, 7):
            top = f"fuzz_{cls.name}_d{depth}_cdc_sync"
            yield RenderedCase(
                template_name=cls.name,
                case_id=top,
                sv=_render(top, depth, head_attr="(* cdc_sync *) "),
                sdc=_SDC,
                top=top,
                params={"depth": depth, "suppression": "cdc_sync"},
                expected=(),
                forbidden=(*_QUIET, ExpectedFinding("CDC-018", Op.ZERO)),
            )
