"""CDC-BBX — a blackboxed instance the analyzer cannot soundly abstract.

``CDC-BBX`` is emitted by the CLI orchestration, not the rule pack: a
bodyless ``(* blackbox *)`` sibling is summarised to its port boundary
when it is provably single-clock and safe, and otherwise left opaque
and reported, one ``error`` per instance (rtl-buddy-cdc#256 / #259 /
#273). This template sweeps the decline reasons and their boundaries
(rtl-buddy-cdc#331). Every case runs through the CLI analysis path
(``params["analysis"] = "cli"``) because that is the only path that
composes blackbox boundaries.

Fires (one finding per declined instance):

- **multi-clock** — the stub's two clock pins are driven by two
  declared, asynchronous clocks. Swept over clock-like pin names
  (``wr_clk``/``rd_clk``, ``src_clk``/``dest_clk``: off the
  ``clk``/``C`` allow-list, so found by tracing what drives them — the
  #259 fix) and over 1 or 2 instances.
- **reconvergence-unsafe** — a single-clock stub with crossings into
  two distinct input ports: reconvergence between them cannot be
  checked at the boundary.
- **clock output** — a stub whose output clocks a flop in the parent:
  clock generation would be elided.

Silent for CDC-BBX:

- **data-named** clock pins (``p0``/``p1``): a pin that does not look
  like a clock is data by contract, so each clock reaching one is
  CDC-008 (clock as data) and the instance is abstracted;
- a **single-clock** stub with one crossing in: it is abstracted, and
  the crossing into it is reported by the ordinary rule (CDC-004) at the
  boundary instead of vanishing;
- the same multi-clock stub **registered with ``--sync-primitive``**
  (``params["sync_primitives"]``) with ``src_clk`` / ``dest_clk`` pins:
  summarised as a synchroniser;
- the multi-clock stub with ``// rbcdc: disable-rule CDC-BBX`` on the
  instantiation (the in-RTL form of ``waive CDC-BBX <instance>``).

``clock_output``: the stub's own clock *output* pin is a driver, not a
reader, so CDC-008 stays silent (rtl-buddy-cdc#344). A registered
primitive with ``wr_clk`` / ``rd_clk`` pins is still declined (no
src/dest hint to find its destination clock from); the CDC-BBX message
says so.

Cross-frontend: both frontends keep the stub as a boundary cell with a
bodyless sibling module (the slang frontend since rtl-buddy-cdc#345), so
the two agree on every case. Cases where the Yosys path reports anything
are family ``cdc_bbx_blackbox_boundary``; the cases where it reports
nothing are ``cdc_bbx_blackbox_boundary_ok``.
"""

from __future__ import annotations

from collections.abc import Iterator

from .base import ExpectedFinding, Op, RenderedCase

_SDC = """\
create_clock -name clk_a -period 10.0 [get_ports clk_a]
create_clock -name clk_b -period 7.0 [get_ports clk_b]
set_clock_groups -asynchronous -group {clk_a} -group {clk_b}
set_input_delay -clock clk_a 1.0 [get_ports d_in]
"""


def _stub(
    name: str, clock_pins: tuple[str, ...], data_in: int, *, clk_out: bool
) -> str:
    pins = [f"    input  wire {p}," for p in clock_pins]
    pins += [f"    input  wire [3:0] din{i}," for i in range(data_in)]
    if clk_out:
        pins.append("    output wire clk_out,")
    pins.append("    output wire [3:0] dout")
    return f"""\
(* blackbox *)
module {name} (
{chr(10).join(pins)}
);
endmodule
"""


def _render(
    top: str,
    *,
    clock_pins: tuple[str, ...] = ("wr_clk", "rd_clk"),
    pin_clocks: tuple[str, ...] = ("clk_a", "clk_b"),
    instances: int = 1,
    data_in: int = 1,
    clk_out: bool = False,
    pragma: bool = False,
) -> str:
    stub = _stub("ip_core", clock_pins, data_in, clk_out=clk_out)
    src = "\n".join(
        f"    logic [3:0] s{i};\n    always_ff @(posedge clk_a) s{i} <= d_in + 4'd{i};"
        for i in range(data_in)
    )
    insts = []
    sinks = []
    for k in range(instances):
        conns = [f".{p}({c})" for p, c in zip(clock_pins, pin_clocks, strict=True)]
        conns += [f".din{i}(s{i})" for i in range(data_in)]
        if clk_out:
            conns.append(f".clk_out(gclk{k})")
        conns.append(f".dout(r{k})")
        lead = "    // rbcdc: disable-rule CDC-BBX signed off as IP\n" if pragma else ""
        insts.append(
            f"    logic [3:0] r{k};\n"
            + (f"    logic gclk{k};\n" if clk_out else "")
            + f"{lead}    ip_core u_ip{k} ({', '.join(conns)});"
        )
        sink_clk = f"gclk{k}" if clk_out else "clk_b"
        sinks.append(
            f"    logic [3:0] q{k};\n    always_ff @(posedge {sink_clk}) q{k} <= r{k};"
        )
    outs = " ^ ".join(f"q{k}" for k in range(instances))
    return f"""\
{stub}
module {top} (
    input  logic clk_a,
    input  logic clk_b,
    input  logic [3:0] d_in,
    output logic [3:0] q_out
);
{src}
{chr(10).join(insts)}
{chr(10).join(sinks)}
    assign q_out = {outs};
endmodule
"""


class BlackboxBoundary:
    """Bodyless sibling the CLI must abstract or report."""

    name = "cdc_bbx_blackbox_boundary"

    @classmethod
    def cases(cls) -> Iterator[RenderedCase]:
        n = cls.name

        def case(
            tag: str,
            sv_kwargs: dict,
            expected: tuple[ExpectedFinding, ...],
            forbidden: tuple[ExpectedFinding, ...],
            params: dict,
            gap_note: str | None = None,
        ) -> RenderedCase:
            top = f"fuzz_{n}_{tag}"
            fires = bool(expected)
            return RenderedCase(
                template_name=n if fires else f"{n}_ok",
                case_id=top,
                sv=_render(top, **sv_kwargs),
                sdc=_SDC,
                top=top,
                params={"analysis": "cli", **params},
                expected=expected,
                forbidden=forbidden,
                gap_note=gap_note,
            )

        bbx = ExpectedFinding("CDC-BBX", Op.EQ, 1)
        no_bbx = ExpectedFinding("CDC-BBX", Op.ZERO)

        for pins in (("wr_clk", "rd_clk"), ("src_clk", "dest_clk")):
            for instances in (1, 2):
                yield case(
                    f"multiclk_{pins[0]}_{pins[1]}_x{instances}",
                    {"clock_pins": pins, "instances": instances},
                    (ExpectedFinding("CDC-BBX", Op.EQ, instances),),
                    (),
                    {
                        "reason": "multi_clock",
                        "pins": list(pins),
                        "instances": instances,
                    },
                )

        # Clock-named is part of the contract (abstract._looks_like_clock_name):
        # a pin that does not look like a clock is DATA even when a clock
        # drives it, so the instance is abstracted as clock-less and each
        # clock-on-a-data-pin is CDC-008 (FIX 4), not a CDC-BBX decline.
        yield case(
            "datanamed_p0_p1",
            {"clock_pins": ("p0", "p1")},
            (
                ExpectedFinding("CDC-008", Op.EQ, 2),
                ExpectedFinding("CDC-004", Op.GE, 1),
            ),
            (no_bbx,),
            {"reason": "data_named_clock_pins", "pins": ["p0", "p1"]},
        )

        yield case(
            "reconv_2in",
            {"clock_pins": ("clk",), "pin_clocks": ("clk_b",), "data_in": 2},
            (bbx,),
            (),
            {"reason": "reconvergence", "data_in": 2},
        )

        yield case(
            "clock_output",
            {"clock_pins": ("clk",), "pin_clocks": ("clk_a",), "clk_out": True},
            (bbx,),
            (ExpectedFinding("CDC-008", Op.ZERO),),
            {"reason": "clock_output"},
        )

        # Abstracted: the crossing into the single-clock stub survives as
        # a boundary crossing and is reported by the ordinary bus rule.
        yield case(
            "singleclk_1in",
            {"clock_pins": ("clk",), "pin_clocks": ("clk_b",)},
            (ExpectedFinding("CDC-004", Op.GE, 1),),
            (no_bbx,),
            {"reason": "single_clock", "data_in": 1},
        )

        # --sync-primitive: summarised at the destination clock, which is
        # found from the pin names (primitives.clock_pin_role: dest/dst,
        # src). XPM-style names work ...
        yield case(
            "multiclk_src_dest_registered",
            {"clock_pins": ("src_clk", "dest_clk")},
            (),
            (no_bbx,),
            {
                "reason": "registered",
                "pins": ["src_clk", "dest_clk"],
                "sync_primitives": ["ip_core"],
            },
        )
        # ... other names do not, and the registered macro is still declined.
        yield case(
            "multiclk_wr_rd_registered",
            {"clock_pins": ("wr_clk", "rd_clk")},
            (bbx,),
            (),
            {
                "reason": "registered",
                "pins": ["wr_clk", "rd_clk"],
                "sync_primitives": ["ip_core"],
            },
        )

        yield case(
            "multiclk_pragma",
            {"pragma": True},
            (),
            (no_bbx,),
            {"reason": "multi_clock", "suppression": "pragma"},
        )
