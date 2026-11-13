"""CDC-023 — a clock net built by combining two declared clocks.

CDC-023 (rtl-buddy-cdc#269) fires exactly when the clock-root tracer
*declines* on a gate or clock-path latch whose legs carry two distinct
declared clocks; a mux *selects* and never fires. This template sweeps
the combine shape the tracer has to recognise (rtl-buddy-cdc#331):

- **combine cell** — ``and``, ``or``, ``xor``, ``nand`` (gate then
  inverter), ``invleg`` (one leg inverted before the gate) and
  ``latch`` (a transparent ``$dlatch`` with one clock on ``D`` and the
  other on ``EN``).
- **sinks** — 1 or 4 flops behind the combined net. Always one finding:
  the finding is per combining cell, the sinks are listed in it.
- **clock relation** — the two clocks in an asynchronous group (the
  default) or left synchronous (no ``set_clock_groups``). Combining is
  a glitch hazard either way; the rule does not consult the groups.
- **three clocks** — ``a & b & c``: two ``$and`` cells, but the tracer
  declines on the first combine it meets walking back from the flops.

Silent variants: a clock ``mux`` (selects, does not combine) and a
textbook ICG (clock ``&`` an untyped enable port — the
``<unconstrained>`` sentinel is not a clock identity, the #263 guard).

Suppression variant: ``// rbcdc: disable-rule CDC-023`` above the
combine, run through the CLI analysis path; the rule has no attribute.

Every case elaborates with ``opt_clean`` (as ``lint`` does) — see
``_OPT_CLEAN``. ``and_s1_v2001`` repeats ``and_s1`` with plain
``always @(posedge ...)`` flops; it is on the cross-frontend allowlist
because the pyslang frontend drops plain ``always`` blocks.
"""

from __future__ import annotations

from collections.abc import Iterator

from .base import ExpectedFinding, Op, RenderedCase

_GATES = {
    "and": "assign gclk = clk_a & clk_b;",
    "or": "assign gclk = clk_a | clk_b;",
    "xor": "assign gclk = clk_a ^ clk_b;",
    "nand": "assign gclk = ~(clk_a & clk_b);",
    "invleg": "assign gclk = ~clk_a & clk_b;",
    "latch": "always_latch if (clk_b) gclk = clk_a;",
    "three": "assign gclk = clk_a & clk_b & clk_c;",
    "mux": "assign gclk = sel ? clk_a : clk_b;",
    "icg": "assign gclk = clk_a & en;",
}

_PORTS = {
    "three": "    input  logic clk_c,\n",
    "mux": "    input  logic sel,\n",
    "icg": "    input  logic en,\n",
}


def _sdc(*, relation: str = "async", three: bool = False) -> str:
    lines = [
        "create_clock -name clk_a -period 10.0 [get_ports clk_a]",
        "create_clock -name clk_b -period 7.0 [get_ports clk_b]",
    ]
    if three:
        lines.append("create_clock -name clk_c -period 4.5 [get_ports clk_c]")
    if relation == "async":
        groups = "-group {clk_a} -group {clk_b}" + (" -group {clk_c}" if three else "")
        lines.append(f"set_clock_groups -asynchronous {groups}")
    lines.append("set_input_delay -clock clk_a 1.0 [get_ports d_in]")
    return "\n".join(lines) + "\n"


def _render(
    top: str,
    gate: str,
    sinks: int,
    *,
    pragma: bool = False,
    always: str = "always_ff",
) -> str:
    regs = "\n".join(f"    logic q{i};" for i in range(sinks))
    flops = "\n".join(
        f"    {always} @(posedge gclk) q{i} <= {'d_in' if i == 0 else f'q{i - 1}'};"
        for i in range(sinks)
    )
    lead = (
        "    // rbcdc: disable-rule CDC-023 characterised test clock\n"
        if pragma
        else ""
    )
    return f"""\
module {top} (
    input  logic clk_a,
    input  logic clk_b,
{_PORTS.get(gate, "")}    input  logic d_in,
    output logic q_out
);
    logic gclk;
{lead}    {_GATES[gate]}

{regs}
{flops}
    assign q_out = q{sinks - 1};
endmodule
"""


# The ``lint`` Yosys script runs ``opt_clean`` after ``flatten``; the fuzz
# recipe does not. Without it the inverter of ``nand`` (folded into the
# flop's CLK_POLARITY by Yosys 0.64) and the hold ``$not`` that
# ``proc_dlatch`` builds for ``latch`` are left dangling, and CDC-008
# reports those dead cells as clock-as-data (rtl-buddy-cdc#338). This
# template is about CDC-023, so it elaborates the way ``lint`` does.
_OPT_CLEAN = "opt_clean;"

_FIRES = (ExpectedFinding("CDC-023", Op.EQ, 1),)
_SILENT = (ExpectedFinding("CDC-023", Op.ZERO),)
# Flops behind a combined clock are domain-unknown, so no crossing rule
# can see them; and a clock leg must never read as clock-as-data.
_QUIET = (
    ExpectedFinding("CDC-001", Op.ZERO),
    ExpectedFinding("CDC-008", Op.ZERO),
    ExpectedFinding("CDC-021", Op.ZERO),
)


class ClockCombine:
    """Two declared clocks combined onto one flop clock net."""

    name = "cdc023_clock_combine"

    @classmethod
    def cases(cls) -> Iterator[RenderedCase]:
        n = cls.name
        for gate in ("and", "or", "xor", "nand", "invleg", "latch"):
            for sinks in (1, 4):
                top = f"fuzz_{n}_{gate}_s{sinks}"
                yield RenderedCase(
                    template_name=n,
                    case_id=top,
                    sv=_render(top, gate, sinks),
                    sdc=_sdc(),
                    top=top,
                    params={"gate": gate, "sinks": sinks, "relation": "async"},
                    expected=_FIRES,
                    forbidden=_QUIET,
                    extra_yosys_passes=_OPT_CLEAN,
                )

        for gate in ("and", "or"):
            top = f"fuzz_{n}_{gate}_s2_sync"
            yield RenderedCase(
                template_name=n,
                case_id=top,
                sv=_render(top, gate, 2),
                sdc=_sdc(relation="sync"),
                top=top,
                params={"gate": gate, "sinks": 2, "relation": "sync"},
                expected=_FIRES,
                forbidden=_QUIET,
            )

        top = f"fuzz_{n}_three_s2"
        yield RenderedCase(
            template_name=n,
            case_id=top,
            sv=_render(top, "three", 2),
            sdc=_sdc(three=True),
            top=top,
            params={"gate": "three", "sinks": 2, "relation": "async"},
            expected=_FIRES,
            forbidden=_QUIET,
            extra_yosys_passes=_OPT_CLEAN,
        )

        for gate in ("mux", "icg"):
            top = f"fuzz_{n}_{gate}_s2"
            yield RenderedCase(
                template_name=n,
                case_id=top,
                sv=_render(top, gate, 2),
                sdc=_sdc(),
                top=top,
                params={"gate": gate, "sinks": 2, "relation": "async"},
                expected=(),
                forbidden=_SILENT,
                extra_yosys_passes=_OPT_CLEAN,
            )

        top = f"fuzz_{n}_and_s2_pragma"
        yield RenderedCase(
            template_name=n,
            case_id=top,
            sv=_render(top, "and", 2, pragma=True),
            sdc=_sdc(),
            top=top,
            params={
                "gate": "and",
                "sinks": 2,
                "relation": "async",
                "suppression": "pragma",
                "analysis": "cli",
            },
            expected=(),
            forbidden=_QUIET + _SILENT,
            extra_yosys_passes=_OPT_CLEAN,
        )

        # Plain Verilog-2001 ``always @(posedge ...)`` flops. Same design
        # as ``and_s1`` otherwise. On the cross-frontend allowlist: the
        # pyslang frontend only lowers ``always_ff`` / ``always_comb`` /
        # ``always_latch`` and silently drops a plain ``always`` block, so
        # slang sees no flop at all.
        top = f"fuzz_{n}_and_s1_v2001"
        yield RenderedCase(
            template_name=n,
            case_id=top,
            sv=_render(top, "and", 1, always="always"),
            sdc=_sdc(),
            top=top,
            params={"gate": "and", "sinks": 1, "relation": "async", "style": "v2001"},
            expected=_FIRES,
            forbidden=_QUIET,
            extra_yosys_passes=_OPT_CLEAN,
        )
