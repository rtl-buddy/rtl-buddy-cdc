"""Abstraction limits after rtl-buddy-cdc#408 (rtl-buddy-cdc#411).

Each case compares the flat run with ``--greybox`` (body kept, analysed
once on its own) and ``--blackbox`` (bodyless stub) on one source:

- item 3: a memory inside the block read asynchronously into the parent
  — the greybox publishes the read (a transit from the address pin to the
  data pin, and the memory's write clock) and the parent judges CDC-026;
- items 1 and 2: what a stub, which declares nothing, is assumed to do
  with its pins — every data input reaches every output combinationally
  (CDC-025 / CDC-024 through it), and its payload is not held — and the
  ``// rbcdc: stub`` pragma that describes it instead.

The views are elaborated by the slang frontend, so the module skips
without pyslang.
"""

from __future__ import annotations

import importlib.util
import json
from collections import Counter
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rtl_buddy_cdc.cli import app
from rtl_buddy_cdc.compositional import _AsyncReads
from rtl_buddy_cdc.memory import find_memories
from rtl_buddy_cdc.netlist import Cell, Module, Port

needs_slang = pytest.mark.skipif(
    importlib.util.find_spec("pyslang") is None, reason="pyslang not installed"
)

_SDC = """\
create_clock -name fast -period 4 [get_ports fast]
create_clock -name slow -period 9 [get_ports slow]
set_clock_groups -asynchronous -group {fast} -group {slow}
"""


def _typed(clock: str, *ports: str) -> str:
    return "".join(f"set_input_delay -clock {clock} 1 [get_ports {p}]\n" for p in ports)


def _run(tmp_path: Path, sv: str, sdc: str, *args: str, tag: str = "flat") -> dict:
    src = tmp_path / "t.sv"
    src.write_text(sv)
    sdc_path = tmp_path / "t.sdc"
    sdc_path.write_text(sdc)
    out = tmp_path / f"{tag}.json"
    cli = ["lint", "--top", "top", "--sdc", str(sdc_path), "--frontend", "slang"]
    cli += [*args, "--format", "json", "--output", str(out), str(src)]
    result = CliRunner().invoke(app, cli)
    assert result.exit_code in (0, 1), result.output
    return json.loads(out.read_text())


def _views(
    tmp_path: Path, sv: str, sdc: str, *extra: str, module: str = "blk"
) -> dict[str, dict]:
    reports = {"flat": _run(tmp_path, sv, sdc, *extra, tag="flat")}
    for view in ("greybox", "blackbox"):
        reports[view] = _run(tmp_path, sv, sdc, f"--{view}", module, *extra, tag=view)
    return reports


def _rules(report: dict) -> Counter:
    return Counter(v["rule_id"] for v in report["violations"])


def _messages(report: dict, rule_id: str) -> list[str]:
    return [v["message"] for v in report["violations"] if v["rule_id"] == rule_id]


# --- item 3: a memory inside the block read asynchronously into the parent ---

# Written on the block's clock, read asynchronously: ``q`` is the read
# data, ``ra`` the read address — both pins of the block.
_MEM_BLOCK = """\
module blk (input clk, input [1:0] wa, input [7:0] wd, input we,
            input [1:0] ra, output logic [7:0] q);
    logic [7:0] mem [4];
    always_ff @(posedge clk) if (we) mem[wa] <= wd;
    assign q = mem[ra];
endmodule
"""


def _mem_top(body: str, ports: str = "input [1:0] b, ") -> str:
    return _MEM_BLOCK + (
        "module top (input fast, input slow, input [1:0] a, input [7:0] d, "
        f"input w, {ports}output logic [7:0] q);\n"
        "    logic [7:0] o;\n" + body + "endmodule\n"
    )


_INST = "    blk u (.clk(fast), .wa(a), .wd(d), .we(w), .ra({ra}), .q(o));\n"

# The address from the read clock, the data registered there: CDC-026
# only (the address and the data are a transit through the read port).
_READ_SLOW = _mem_top(_INST.format(ra="b") + "    always_ff @(posedge slow) q <= o;\n")
# The same, with the parent's read pointer advanced on a flag synchronised
# from the write clock: the async-FIFO shape, clean.
_READ_SLOW_SYNCED = _mem_top(
    "    logic f, s1, s2;\n    logic [1:0] rp;\n"
    "    always_ff @(posedge fast) f <= w;\n"
    "    always_ff @(posedge slow) begin s1 <= f; s2 <= s1; if (s2) rp <= rp + 2'd1; end\n"
    + _INST.format(ra="rp")
    + "    always_ff @(posedge slow) q <= o;\n",
    ports="",
)
# The data registered in the write clock: no storage crossing, but the
# slow address crosses into the fast register through the read port.
_READ_FAST = _READ_SLOW.replace("posedge slow) q <= o", "posedge fast) q <= o")

_MEM_SDC = _SDC + _typed("fast", "a", "d", "w")


@needs_slang
@pytest.mark.parametrize(
    ("sv", "b_clock", "flat", "blackbox"),
    [
        (_READ_SLOW, "slow", {"CDC-026": 1}, {"CDC-004": 2}),
        # The address from the write clock crosses with the data into the
        # slow register (CDC-004), and nothing controls the read (CDC-026).
        (_READ_SLOW, "fast", {"CDC-004": 1, "CDC-026": 1}, {"CDC-004": 1}),
        (_READ_FAST, "slow", {"CDC-004": 1}, {"CDC-004": 1}),
        (_READ_SLOW_SYNCED, None, {}, {"CDC-004": 2}),
    ],
    ids=["read-slow", "address-from-write-clock", "read-fast", "fifo-shape"],
)
def test_memory_async_read_through_a_greybox_matches_flat(
    tmp_path: Path, sv: str, b_clock: str | None, flat: dict, blackbox: dict
) -> None:
    sdc = _MEM_SDC + (_typed(b_clock, "b") if b_clock else "")
    reports = _views(tmp_path, sv, sdc)
    assert _rules(reports["flat"]) == Counter(flat)
    assert _rules(reports["greybox"]) == Counter(flat)
    # A stub has no memory to show (documented): every non-clock pin is
    # data and its output a launch in its clock.
    assert _rules(reports["blackbox"]) == Counter(blackbox)


@needs_slang
def test_greybox_cdc026_names_the_memory_and_anchors_on_the_instance(
    tmp_path: Path,
) -> None:
    reports = _views(tmp_path, _READ_SLOW, _MEM_SDC + _typed("slow", "b"))
    (v,) = [x for x in reports["greybox"]["violations"] if x["rule_id"] == "CDC-026"]
    assert v["cell_name"] == "u"
    assert "`u.mem`" in v["message"] and "read on `slow`" in v["message"]
    (flat_msg,) = _messages(reports["flat"], "CDC-026")
    assert "`u.mem`" in flat_msg


@needs_slang
def test_greybox_memory_read_captured_inside_stays_with_the_body(
    tmp_path: Path,
) -> None:
    """A read the body registers itself is placed inside: the address pin
    is an ordinary capture (CDC-004 at the pin, as flat at the register
    behind it), and no read is published to the parent."""
    sv = _MEM_BLOCK.replace(
        "assign q = mem[ra];", "always_ff @(posedge clk) q <= mem[ra];"
    ) + (
        "module top (input fast, input slow, input [1:0] a, input [7:0] d, "
        "input w, input [1:0] b, output logic [7:0] q);\n"
        "    blk u (.clk(fast), .wa(a), .wd(d), .we(w), .ra(b), .q(q));\n"
        "endmodule\n"
    )
    reports = _views(tmp_path, sv, _MEM_SDC + _typed("slow", "b"))
    assert _rules(reports["flat"]) == Counter({"CDC-004": 1})
    assert _rules(reports["greybox"]) == Counter({"CDC-004": 1})


def test_async_read_sites_of_a_packed_memory() -> None:
    """``$mem_v2`` packs every port on one cell: port ``i``'s address is
    the ``i``-th ``RD_ADDR`` slice, its enable ``RD_EN[i]``."""
    mem = Cell(
        name="m0",
        type="$mem_v2",
        parameters={
            "MEMID": "\\mem",
            "ABITS": "10",
            "WIDTH": "1",
            "RD_PORTS": "10",
            "WR_PORTS": "1",
            "RD_CLK_ENABLE": "01",
            "WR_CLK_ENABLE": "1",
        },
        connections={
            "RD_CLK": (2, 2),
            "RD_ADDR": (3, 4, 5, 6),
            "RD_DATA": (7, 8),
            "RD_EN": ("1", 9),
            "WR_CLK": (2,),
            "WR_ADDR": (3, 4),
            "WR_DATA": (10,),
            "WR_EN": (11,),
        },
    )
    sub = Module(
        name="blk",
        ports={"clk": Port("clk", "input", (2,))},
        cells={"m0": mem},
        netnames={},
    )
    reads = _AsyncReads.of(sub, find_memories(sub))
    # Port 0 is clocked (RD_CLK_ENABLE bit 0): only port 1 is a transit.
    assert set(reads.sites) == {
        ("m0", "RD_ADDR", 2),
        ("m0", "RD_ADDR", 3),
        ("m0", "RD_EN", 1),
    }
    assert set(reads.data) == {8}
