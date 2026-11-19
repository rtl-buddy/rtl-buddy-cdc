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

# The block registers the read address in its own (the write) clock and
# reads asynchronously from it: the body places the read in the write
# clock, but the parent registers the data in the other one — CDC-026 is
# the parent's, as flat (with CDC-004 for the address register's value
# crossing through the read).
_READ_ADDR_REGISTERED = _READ_SLOW.replace(
    "    logic [7:0] mem [4];\n",
    "    logic [7:0] mem [4];\n    logic [1:0] rr;\n"
    "    always_ff @(posedge clk) rr <= ra;\n",
).replace("assign q = mem[ra];", "assign q = mem[rr];")


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
        (_READ_ADDR_REGISTERED, "fast", {"CDC-004": 1, "CDC-026": 1}, {"CDC-004": 1}),
    ],
    ids=[
        "read-slow",
        "address-from-write-clock",
        "read-fast",
        "fifo-shape",
        "address-registered-inside",
    ],
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


# --- items 1 and 2: what a bodyless stub is read as -------------------------


def _advisories(report: dict) -> list[dict]:
    return report["stub_assumptions"]


# The block's output XORs its register with its input, which the parent
# drives from another clock: the top-level output merges two asynchronous
# launches through the block (flat CDC-025).
_MERGE_THROUGH_BLOCK = """\
module blk (input clk, input d, output q);
    logic s1;
    always_ff @(posedge clk) s1 <= d;
    assign q = s1 ^ d;
endmodule
module top (input fast, input slow, input d, output q);
    logic x;
    always_ff @(posedge slow) x <= d;
    blk u (.clk(fast), .d(x), .q(q));
endmodule
"""
# Registered output: no path through the block, nothing merges flat.
_REGISTERED_THROUGH_BLOCK = _MERGE_THROUGH_BLOCK.replace(
    "assign q = s1 ^ d;",
    "logic s2;\n    always_ff @(posedge clk) s2 <= s1;\n    assign q = s2;",
)
_MERGE_SDC = _SDC + _typed("slow", "d")


@needs_slang
def test_undeclared_stub_feedthrough_is_an_advisory(tmp_path: Path) -> None:
    """Item 1, default: a stub's output is read as registered (no path
    through it), as before — so no finding — and the report says that
    reading kept CDC-025 quiet, with the finding it would give and the
    pragma that declares the stub."""
    reports = _views(tmp_path, _MERGE_THROUGH_BLOCK, _MERGE_SDC)
    assert _rules(reports["flat"]) == Counter({"CDC-001": 1, "CDC-025": 1})
    assert _rules(reports["greybox"]) == _rules(reports["flat"])
    assert _rules(reports["blackbox"]) == Counter({"CDC-001": 1})
    assert _advisories(reports["flat"]) == []
    assert _advisories(reports["greybox"]) == []
    (adv,) = _advisories(reports["blackbox"])
    assert (adv["rule_id"], adv["instance"], adv["module"], adv["port"]) == (
        "CDC-025",
        "u",
        "blk",
        "q",
    )
    assert "merges launches from fast" in adv["message"]
    assert "// rbcdc: stub blk.q feedthrough=" in adv["message"]


@needs_slang
def test_registered_stub_output_still_gets_the_advisory(tmp_path: Path) -> None:
    """The stub cannot show its output is registered: the advisory stands
    until the pragma says so."""
    reports = _views(tmp_path, _REGISTERED_THROUGH_BLOCK, _MERGE_SDC)
    # Flat, the input lands on a 2FF chain; the stub cannot prove it.
    assert _rules(reports["flat"]) == Counter()
    assert _rules(reports["greybox"]) == Counter()
    assert _rules(reports["blackbox"]) == Counter({"CDC-001": 1})
    assert [a["rule_id"] for a in _advisories(reports["blackbox"])] == ["CDC-025"]
    declared = _REGISTERED_THROUGH_BLOCK + "// rbcdc: stub blk.* feedthrough=none\n"
    reports = _views(tmp_path, declared, _MERGE_SDC)
    assert _rules(reports["blackbox"]) == Counter({"CDC-001": 1})
    assert _advisories(reports["blackbox"]) == []


@needs_slang
@pytest.mark.parametrize(
    ("pragma", "blackbox"),
    [
        # Declared path: the stub reaches the flat verdict.
        ("// rbcdc: stub blk.q feedthrough=d", {"CDC-001": 1, "CDC-025": 1}),
        # Declared registered (wrongly, here): the user's word stands.
        ("/* rbcdc: stub blk.q feedthrough=none vendor datasheet */", {"CDC-001": 1}),
        # A per-output pragma overrides the module-wide one, key by key.
        (
            "// rbcdc: stub blk.* feedthrough=none\n// rbcdc: stub blk.q feedthrough=d",
            {"CDC-001": 1, "CDC-025": 1},
        ),
    ],
    ids=["feedthrough-declared", "registered-declared", "override"],
)
def test_declared_stub_feedthrough(tmp_path: Path, pragma: str, blackbox: dict) -> None:
    reports = _views(tmp_path, _MERGE_THROUGH_BLOCK + pragma + "\n", _MERGE_SDC)
    assert _rules(reports["blackbox"]) == Counter(blackbox)
    assert _advisories(reports["blackbox"]) == []
    # Flat and greybox runs ignore a stub pragma.
    assert _rules(reports["flat"]) == Counter({"CDC-001": 1, "CDC-025": 1})
    assert _rules(reports["greybox"]) == Counter({"CDC-001": 1, "CDC-025": 1})
    if "CDC-025" in blackbox:
        (msg,) = _messages(reports["blackbox"], "CDC-025")
        assert "rbcdc: stub" not in msg  # declared, not assumed


@needs_slang
def test_undeclared_stub_feedthrough_to_a_contracted_output(tmp_path: Path) -> None:
    """CDC-024 reads the same cone: its advisory, then the declared path."""
    sdc = _MERGE_SDC + "set_output_delay -clock fast 1 [get_ports q]\n"
    reports = _views(tmp_path, _MERGE_THROUGH_BLOCK, sdc)
    assert _rules(reports["flat"])["CDC-024"] == 1
    assert _rules(reports["greybox"])["CDC-024"] == 1
    assert _rules(reports["blackbox"])["CDC-024"] == 0
    assert [a["rule_id"] for a in _advisories(reports["blackbox"])] == ["CDC-024"]
    declared = _MERGE_THROUGH_BLOCK + "// rbcdc: stub blk.q feedthrough=d\n"
    reports = _views(tmp_path, declared, sdc)
    assert _rules(reports["blackbox"])["CDC-024"] == 1
    assert _advisories(reports["blackbox"]) == []


# The payload register and the request flop are inside the block; the
# parent synchronises the request and loads the payload on it.
_HOLD_BEHIND_OUTPUT = """\
module blk (input clk, input go, input [7:0] d, output logic req, output logic [7:0] data);
    always_ff @(posedge clk) begin req <= go; data <= d; end
endmodule
module top (input fast, input slow, input go, input [7:0] d, output logic [7:0] q);
    logic r, r1, r2;
    logic [7:0] x;
    blk u (.clk(slow), .go(go), .d(d), .req(r), .data(x));
    always_ff @(posedge fast) begin r1 <= r; r2 <= r1; if (r2) q <= x; end
endmodule
"""


def _acked(hold: str) -> str:
    """The parent returns an acknowledge from the destination into the
    block's ``ack`` pin; ``hold`` is the payload register's load."""
    return f"""\
module blk (input clk, input go, input ack, input [7:0] d,
            output logic req, output logic [7:0] data);
    logic a1, a2;
    always_ff @(posedge clk) begin
        a1 <= ack; a2 <= a1;
        req <= go;
        {hold}
    end
endmodule
module top (input fast, input slow, input go, input [7:0] d, output logic [7:0] q);
    logic r, r1, r2, ack;
    logic [7:0] x;
    blk u (.clk(slow), .go(go), .ack(ack), .d(d), .req(r), .data(x));
    always_ff @(posedge fast) begin r1 <= r; r2 <= r1; ack <= r2; if (r2) q <= x; end
endmodule
"""


# Holds the payload until the acknowledge; takes it and lets the payload
# run anyway (flat CDC-012).
_HELD = _acked("if (a2) data <= d;")
_NOT_HELD = _acked("data <= d;")
_HOLD_SDC = _SDC + _typed("slow", "go", "d")


@needs_slang
def test_stub_payload_without_an_acknowledge_is_cdc012(tmp_path: Path) -> None:
    reports = _views(tmp_path, _HOLD_BEHIND_OUTPUT, _HOLD_SDC)
    for view in ("flat", "greybox", "blackbox"):
        assert _rules(reports[view]) == Counter({"CDC-012": 1}), view
    assert _advisories(reports["blackbox"]) == []


@needs_slang
@pytest.mark.parametrize(("sv", "flat"), [(_HELD, {}), (_NOT_HELD, {"CDC-012": 1})])
def test_stub_payload_with_an_acknowledge_is_an_advisory(
    tmp_path: Path, sv: str, flat: dict
) -> None:
    """Item 2, default: an acknowledge from the destination enters the
    stub, so its payload is read as held (no finding) — the stub cannot
    show whether it really holds it, and the report says so."""
    reports = _views(tmp_path, sv, _HOLD_SDC)
    assert _rules(reports["flat"]) == Counter(flat)
    assert _rules(reports["greybox"]) == Counter(flat)
    assert _rules(reports["blackbox"])["CDC-012"] == 0
    (adv,) = _advisories(reports["blackbox"])
    assert (adv["rule_id"], adv["instance"], adv["port"]) == ("CDC-012", "u", "data")
    assert "enters it on `ack`" in adv["message"]
    assert "// rbcdc: stub blk.data hold=ack" in adv["message"]


@needs_slang
@pytest.mark.parametrize(
    ("pragma", "cdc012"),
    [
        ("// rbcdc: stub blk.data hold=ack", 0),
        ("// rbcdc: stub blk.data hold=none", 1),
        # The named pin carries no acknowledge from the destination.
        ("// rbcdc: stub blk.data hold=go", 1),
    ],
    ids=["held-on-ack", "not-held", "wrong-pin"],
)
def test_declared_stub_hold(tmp_path: Path, pragma: str, cdc012: int) -> None:
    reports = _views(tmp_path, _NOT_HELD + pragma + "\n", _HOLD_SDC)
    assert _rules(reports["blackbox"])["CDC-012"] == cdc012
    assert _advisories(reports["blackbox"]) == []
    if cdc012:
        (msg,) = _messages(reports["blackbox"], "CDC-012")
        assert "is declared to hold `data`" in msg


@needs_slang
def test_stub_advisory_in_the_text_report(tmp_path: Path) -> None:
    src = tmp_path / "t.sv"
    src.write_text(_MERGE_THROUGH_BLOCK)
    sdc = tmp_path / "t.sdc"
    sdc.write_text(_MERGE_SDC)
    result = CliRunner().invoke(
        app,
        ["lint", "--top", "top", "--sdc", str(sdc), "--frontend", "slang"]
        + ["--blackbox", "blk", "--no-color", str(src)],
    )
    assert result.exit_code == 1, result.output
    assert "1 verdict rests on an undeclared bodyless stub" in result.output
    assert "[CDC-025]" in result.output


@needs_slang
def test_stub_pragma_typos_are_reported(tmp_path: Path) -> None:
    src = tmp_path / "t.sv"
    src.write_text(
        _MERGE_THROUGH_BLOCK
        + "// rbcdc: stub blk.qq feedthrough=d\n"
        + "// rbcdc: stub blk.q feedthrough=e\n"
        + "// rbcdc: stub blk.q holds=d\n"
        + "// rbcdc: stub other.q feedthrough=none\n"
    )
    sdc = tmp_path / "t.sdc"
    sdc.write_text(_MERGE_SDC)
    result = CliRunner().invoke(
        app,
        ["lint", "--top", "top", "--sdc", str(sdc), "--frontend", "slang"]
        + ["--blackbox", "blk", "--no-color", str(src)],
    )
    assert result.exit_code == 1
    out = result.output
    assert "blk.qq ignored: qq is not an output" in out
    assert "blk.q ignored: not an input of blk: e" in out
    assert "cannot read `holds=d`" in out
    # A module that is no stub in this run is not this run's business.
    assert "other.q" not in out
    # Nothing was declared, so the advisory stands.
    assert "rests on an undeclared bodyless stub" in out


def test_stub_pragma_grammar(tmp_path: Path) -> None:
    from rtl_buddy_cdc import pragma

    a = tmp_path / "a.sv"
    a.write_text(
        "// rbcdc: stub blk.q feedthrough=d,e hold=ack  per the IP spec\n"
        "/* rbcdc: stub blk.* feedthrough=none */\n"
        "//rbcdc:stub blk.z hold=none\n"
        "// rbcdc: stub blk.y\n"
        "// rbcdc: disable-rule CDC-001 not a stub pragma\n"
    )
    b = tmp_path / "b.sv"
    b.write_text("// rbcdc: stub blk.q feedthrough=none\n")
    decls = pragma.scan_stubs([a, b])
    q = decls.ports[("blk", "q")]
    # b.sv's pragma overrides feedthrough and keeps a.sv's hold.
    assert (q.feedthrough, q.hold) == ((), ("ack",))
    assert q.origin == f"{b}:1"
    assert decls.ports[("blk", "*")].feedthrough == ()
    assert decls.ports[("blk", "z")].hold == ()
    assert ("blk", "y") not in decls.ports
    assert decls.problems == (
        f"{a}:4: stub pragma for blk.y sets neither feedthrough= nor hold=",
    )
    # The waiver scan does not read a stub pragma, and vice versa.
    assert [w.rule_pattern for w in pragma.scan([a])] == ["CDC-001"]
