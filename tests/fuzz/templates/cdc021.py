"""CDC-021 — flop CLK on an undeclared port, swept by consumers and declaration.

``gap_g10`` pins the bare shape (one self-looping flop on a port, empty
SDC, ten port names). CDC-021 reports **one finding per undeclared
port**, however many flops it clocks, and its only condition is "the
port is not among any ``create_clock``'s sources". This template sweeps
exactly that (rtl-buddy-cdc#331), next to a properly declared
``clk_main`` domain so the SDC is never empty:

- **consumer count** — 1, 2, 4 or 8 flops (a Johnson counter of
  separate 1-bit registers) on the port. Always ``EQ 1``.
- **declaration** — ``undeclared`` (fires), ``declared`` with
  ``create_clock -name clk_aux`` (silent) and ``renamed``, declared
  under a different clock name (``create_clock -name core_aux
  [get_ports clk_aux]``): the port is a clock source, so silent.
- **clock path** — the port clocks the flops on ``negedge``, or
  through an explicit inverter (``assign aux_n = ~clk_aux``); the trace
  still ends on the port (fires). The inverter cases elaborate with
  ``opt_clean``, as ``lint`` does: Yosys 0.64 folds the inverter into
  the flop's ``CLK_POLARITY`` and leaves the ``$not`` dangling, and
  CDC-008 would report that dead cell as clock-as-data
  (rtl-buddy-cdc#338).
- **two undeclared ports** — two findings.

Suppression variant: ``// rbcdc: disable-rule CDC-021`` over the
consumer flops, run through the CLI analysis path (pragmas are a CLI
feature); CDC-021 has no attribute suppression.
"""

from __future__ import annotations

from collections.abc import Iterator

from .base import ExpectedFinding, Op, RenderedCase

_SDC_BASE = """\
create_clock -name clk_main -period 10.0 [get_ports clk_main]
set_input_delay -clock clk_main 1.0 [get_ports d_in]
"""

_DECL = {
    "undeclared": "",
    "declared": "create_clock -name clk_aux -period 8.0 [get_ports clk_aux]\n",
    "renamed": "create_clock -name core_aux -period 8.0 [get_ports clk_aux]\n",
}


def _counter(clk_expr: str, tag: str, n: int, *, pragma: bool = False) -> str:
    """Johnson counter of ``n`` separate 1-bit flops clocked by ``clk_expr``."""
    decls = "\n".join(f"    logic {tag}{i};" for i in range(n))
    stages = "\n".join(
        f"        {tag}{i} <= {f'~{tag}{n - 1}' if i == 0 else f'{tag}{i - 1}'};"
        for i in range(n)
    )
    lead = (
        "    // rbcdc: disable-rule CDC-021 free-running test counter\n"
        if pragma
        else ""
    )
    return f"""{decls}
{lead}    always_ff @({clk_expr}) begin
{stages}
    end
"""


def _render(
    top: str,
    n: int,
    *,
    clock_path: str = "direct",
    second_port: bool = False,
    pragma: bool = False,
) -> str:
    pre = ""
    if clock_path == "inverter":
        pre = "    logic aux_n;\n    assign aux_n = ~clk_aux;\n"
        clk = "posedge aux_n"
    elif clock_path == "negedge":
        clk = "negedge clk_aux"
    else:
        clk = "posedge clk_aux"
    body = pre + _counter(clk, "a", n, pragma=pragma)
    port2 = ""
    out2 = "1'b0"
    if second_port:
        port2 = "    input  logic clk_aux2,\n"
        body += _counter("posedge clk_aux2", "b", 2)
        out2 = "b1"
    return f"""\
module {top} (
    input  logic clk_main,
    input  logic clk_aux,
{port2}    input  logic d_in,
    output logic q_main,
    output logic q_aux,
    output logic q_aux2
);
    always_ff @(posedge clk_main) q_main <= d_in;

{body}
    assign q_aux = a{n - 1};
    assign q_aux2 = {out2};
endmodule
"""


_FIRES = (ExpectedFinding("CDC-021", Op.EQ, 1),)
_SILENT = (ExpectedFinding("CDC-021", Op.ZERO),)
# The undeclared domain has no crossing and no data port.
_QUIET = (ExpectedFinding("CDC-001", Op.ZERO), ExpectedFinding("CDC-011", Op.ZERO))


class UndeclaredClockPort:
    """Flops clocked by a port the SDC never declares."""

    name = "cdc021_undeclared_clock_port"

    @classmethod
    def cases(cls) -> Iterator[RenderedCase]:
        for decl, fires in (
            ("undeclared", True),
            ("declared", False),
            ("renamed", False),
        ):
            for n in (1, 2, 4, 8):
                top = f"fuzz_{cls.name}_{decl}_n{n}"
                yield RenderedCase(
                    template_name=cls.name,
                    case_id=top,
                    sv=_render(top, n),
                    sdc=_SDC_BASE + _DECL[decl],
                    top=top,
                    params={"declaration": decl, "consumers": n},
                    expected=_FIRES if fires else (),
                    forbidden=_QUIET + (() if fires else _SILENT),
                )

        for path in ("negedge", "inverter"):
            for decl, fires in (("undeclared", True), ("declared", False)):
                top = f"fuzz_{cls.name}_{path}_{decl}"
                yield RenderedCase(
                    template_name=cls.name,
                    case_id=top,
                    sv=_render(top, 2, clock_path=path),
                    sdc=_SDC_BASE + _DECL[decl],
                    top=top,
                    params={"declaration": decl, "consumers": 2, "clock_path": path},
                    expected=_FIRES if fires else (),
                    forbidden=_QUIET
                    + (ExpectedFinding("CDC-008", Op.ZERO),)
                    + (() if fires else _SILENT),
                    extra_yosys_passes="opt_clean;" if path == "inverter" else "",
                )

        top = f"fuzz_{cls.name}_two_ports"
        yield RenderedCase(
            template_name=cls.name,
            case_id=top,
            sv=_render(top, 2, second_port=True),
            sdc=_SDC_BASE,
            top=top,
            params={"declaration": "undeclared", "consumers": 2, "ports": 2},
            expected=(ExpectedFinding("CDC-021", Op.EQ, 2),),
            forbidden=_QUIET,
        )

        top = f"fuzz_{cls.name}_undeclared_pragma"
        yield RenderedCase(
            template_name=cls.name,
            case_id=top,
            sv=_render(top, 4, pragma=True),
            sdc=_SDC_BASE,
            top=top,
            params={
                "declaration": "undeclared",
                "consumers": 4,
                "suppression": "pragma",
                "analysis": "cli",
            },
            expected=(),
            forbidden=_QUIET + _SILENT,
        )
