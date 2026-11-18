"""slang frontend: hierarchical references (rtl-buddy-cdc#405 item 2).

A dotted name (``gr.lm[ra]``, ``g[1].h.x``, ``u_child.q``, an upward
``top.sig``) whose target is declared in an instance this frontend
inlines reads and writes the target's own bits, a memory through its
ports. It was an opaque ``$_UNKNOWN_`` driver on a read and a dropped,
reported write, so the fan-in behind it was invisible to the rules and a
crossing through it was missed.

- Into named generate blocks (``if`` / ``case`` / ``for``, nested,
  arrayed): Yosys' Verilog frontend resolves these too; both netlists
  are simulated on the same stimulus and must agree, and the ``lint``
  verdict on a crossing through one must match.
- Into child instances and upward: Yosys 0.64 does not resolve these
  (``Identifier `\\u.q' is implicitly declared``: an undriven one-bit
  wire), so the slang netlist is checked against the SystemVerilog
  semantics only.
- Into a blackbox (or above a greybox body lowered on its own) there is
  nothing to read: still an opaque, reported driver / a reported write.

CI's pytest job has no yosys; there the slang-side assertions stand alone.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import random
import shutil
import subprocess
from collections import Counter
from collections.abc import Callable
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rtl_buddy_cdc import netlist
from rtl_buddy_cdc import sdc as sdc_mod
from rtl_buddy_cdc.cli import app
from rtl_buddy_cdc.netlist import Bit, Cell, Module

if importlib.util.find_spec("pyslang") is None:
    pytest.skip("pyslang not installed", allow_module_level=True)

from rtl_buddy_cdc.frontends import slang as slang_fe  # noqa: E402

_HAVE_YOSYS = shutil.which("yosys") is not None
_SLANG_LOG = "rtl_buddy_cdc.frontends.slang"


@pytest.fixture(autouse=True)
def _tokenizer_sdc_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    """Plain SDCs; the tokenizer avoids a Tcl worker per ``lint``."""
    monkeypatch.setenv(sdc_mod.BACKEND_ENV_VAR, "tokenizer")


def _write(tmp_path: Path, src: str, name: str) -> Path:
    sv = tmp_path / name
    sv.write_text(src)
    return sv


def _slang(
    tmp_path: Path, src: str, **kw: list[str]
) -> tuple[Module, dict[str, Module]]:
    return slang_fe.elaborate_with_blackboxes(
        [_write(tmp_path, src, "s.sv")], "m", **kw
    )


def _yosys(tmp_path: Path, src: str) -> Module:
    """The ``lint`` recipe, from a script file: CI's yosys 0.52 splits a
    ``-p`` script at commas."""
    sv = _write(tmp_path, src, "y.sv")
    out = tmp_path / "y.json"
    script = tmp_path / "y.ys"
    script.write_text(
        f"read_verilog -sv {sv}\nhierarchy -top m\nproc\nflatten\n"
        f"opt_clean\nwrite_json {out}\n"
    )
    subprocess.run(["yosys", "-q", "-s", str(script)], check=True)
    return netlist.load(out)


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == _SLANG_LOG]


# --- a cycle simulator: comb cells, $dff, memories ---------------------------


def _signed(value: int, width: int, signed: bool) -> int:
    value &= (1 << width) - 1
    if signed and width and value >> (width - 1):
        value -= 1 << width
    return value


class _Sim:
    """Every flop clocks on every :meth:`tick`; a memory is a dict per
    ``MEMID``; ``x`` reads as 0."""

    def __init__(self, module: Module) -> None:
        self.module = module
        self.comb: dict[Bit, tuple[Cell, int]] = {}
        self.flops: list[Cell] = []
        self.writes: list[Cell] = []
        for c in module.cells.values():
            if c.type == "$dff":
                self.flops.append(c)
            elif c.type == "$memwr_v2":
                self.writes.append(c)
            else:
                out = c.connections.get("Y", c.connections.get("DATA", ()))
                for k, bit in enumerate(out):
                    self.comb[bit] = (c, k)
        self.state: dict[Bit, int] = {}
        self.mem: dict[str, dict[int, int]] = {}

    def _num_fn(self, inputs: dict[str, int]) -> Callable[[tuple[Bit, ...]], int]:
        env = dict(self.state)
        for name, value in inputs.items():
            for k, bit in enumerate(self.module.ports[name].bits):
                env[bit] = (value >> k) & 1
        memo: dict[str, int] = {}

        def get(bit: Bit) -> int:
            if isinstance(bit, str):
                return int(bit == "1")
            if bit in env:
                return env[bit]
            if bit not in self.comb:
                return 0
            cell, lane = self.comb[bit]
            if cell.name not in memo:
                memo[cell.name] = self._cell(cell, num)
            return (memo[cell.name] >> lane) & 1

        def num(bits: tuple[Bit, ...]) -> int:
            return sum(get(b) << k for k, b in enumerate(bits))

        return num

    def _cell(self, cell: Cell, num: Callable[[tuple[Bit, ...]], int]) -> int:
        c = cell.connections
        if cell.type in ("$memrd", "$memrd_v2"):
            memid = cell.parameters.get("MEMID", "")
            return self.mem.get(memid, {}).get(num(c["ADDR"]), 0)
        if cell.type == "$mux":
            return num(c["B"] if num(c["S"]) else c["A"])
        width = len(c["Y"])
        a_bits, b_bits = c.get("A", ()), c.get("B", ())
        a = num(a_bits)
        b = num(b_bits)
        if cell.type in ("$shift", "$shiftx"):
            signed = "1" in (cell.parameters.get("B_SIGNED") or "0")
            b = _signed(b, len(b_bits), signed)
            return (a >> b if b >= 0 else a << -b) & ((1 << width) - 1)
        ops: dict[str, Callable[[], int]] = {
            "$not": lambda: ~a,
            "$and": lambda: a & b,
            "$or": lambda: a | b,
            "$xor": lambda: a ^ b,
            "$add": lambda: a + b,
            "$sub": lambda: a - b,
            "$eq": lambda: int(a == b),
            "$logic_not": lambda: int(a == 0),
            "$reduce_or": lambda: int(a != 0),
            "$reduce_bool": lambda: int(a != 0),
            "$pmux": lambda: self._pmux(c, num),
        }
        return ops[cell.type]() & ((1 << width) - 1)

    @staticmethod
    def _pmux(c: dict[str, tuple[Bit, ...]], num: Callable[..., int]) -> int:
        width = len(c["Y"])
        for k, s in enumerate(c["S"]):
            if num((s,)):
                return num(c["B"][k * width : (k + 1) * width])
        return num(c["A"])

    def outputs(self, inputs: dict[str, int], names: tuple[str, ...]) -> dict:
        num = self._num_fn(inputs)
        return {n: num(self.module.ports[n].bits) for n in names}

    def tick(self, inputs: dict[str, int]) -> None:
        num = self._num_fn(inputs)
        nxt = [(c.connections["Q"], num(c.connections["D"])) for c in self.flops]
        ports = [
            (
                c.parameters.get("MEMID", ""),
                num(c.connections["ADDR"]),
                num(c.connections["DATA"]),
                num(c.connections["EN"]),
            )
            for c in self.writes
        ]
        for q, value in nxt:
            for k, bit in enumerate(q):
                self.state[bit] = (value >> k) & 1
        for memid, addr, data, en in ports:
            mem = self.mem.setdefault(memid, {})
            mem[addr] = (mem.get(addr, 0) & ~en) | (data & en)


def _run(
    module: Module,
    stimulus: list[dict[str, int]],
    outs: tuple[str, ...],
) -> list[dict[str, int]]:
    """Outputs before each clock edge, one edge per stimulus vector."""
    sim = _Sim(module)
    trace = []
    for values in stimulus:
        trace.append(sim.outputs(values, outs))
        sim.tick(values)
    return trace


def _stimulus(module: Module, n: int, seed: int) -> list[dict[str, int]]:
    rnd = random.Random(seed)
    names = [
        p.name
        for p in module.ports.values()
        if p.direction == "input" and not p.name.startswith("clk")
    ]
    return [
        {name: rnd.getrandbits(len(module.ports[name].bits)) for name in names}
        for _ in range(n)
    ]


# --- generate blocks: the issue's reproducer ---------------------------------

_GEN_MEMORY = """module m (input clk, input [1:0] wa, input [1:0] ra, input [3:0] d,
          output [3:0] p);
    if (1) begin : gr
        logic [3:0] lm [0:3];
        always_ff @(posedge clk) lm[wa] <= d;
    end
    assign p = gr.lm[ra];
endmodule
"""


def test_generate_block_memory_read_is_a_read_port(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """``gr.lm[ra]`` from outside ``gr`` is a ``$memrd_v2`` of ``gr.lm``
    (``MEMID`` ``\\gr.lm``, address ``ra``), as Yosys reads it; it was an
    opaque driver. The read returns what was last written there."""
    with caplog.at_level(logging.WARNING, logger=_SLANG_LOG):
        module, _ = _slang(tmp_path, _GEN_MEMORY)
    assert _warnings(caplog) == []
    assert not [c for c in module.cells.values() if c.type == "$_UNKNOWN_"]
    (rd,) = [c for c in module.cells.values() if c.type == "$memrd_v2"]
    assert rd.parameters["MEMID"] == "\\gr.lm"
    assert rd.connections["ADDR"] == module.ports["ra"].bits
    assert rd.connections["DATA"] == module.ports["p"].bits
    stim = _stimulus(module, 60, 1)
    mem: dict[int, int] = {}
    want = []
    for v in stim:
        want.append({"p": mem.get(v["ra"], 0)})
        mem[v["wa"]] = v["d"]
    assert _run(module, stim, ("p",)) == want
    if _HAVE_YOSYS:
        assert _run(_yosys(tmp_path, _GEN_MEMORY), stim, ("p",)) == want


_GEN_SHAPES = """module m (input clk, input [1:0] wa, input [1:0] ra, input [3:0] d,
          input s, output [3:0] p, output [3:0] q, output logic [3:0] r,
          output logic [3:0] t, output [1:0] u, output [3:0] v, output w,
          output [3:0] y, output [3:0] z);
    for (genvar i = 0; i < 2; i++) begin : g
        if (i == 1) begin : h
            logic [3:0] x;
            assign x = d - i;
        end else begin : h
            logic [3:0] x;
            assign x = d ^ 4'hf;
        end
        logic [3:0] mem [0:3];
        always_ff @(posedge clk) if (s ^ i[0]) mem[wa] <= d + i;
    end
    if (1) begin : gr
        logic [3:0] rv;
        logic [3:0] fr;
        logic [3:0] am [0:3];
        always_comb rv = d ^ 4'h5;
    end
    case (1)
        1: begin : cs
            logic [3:0] cy;
            assign cy = d & 4'h3;
        end
    endcase
    assign p = g[1].h.x;
    assign q = g[1].mem[ra];
    always_comb begin
        r = g[0].h.x;
        if (s) r = g[0].mem[ra];
    end
    always_ff @(posedge clk) t <= g[1].h.x[ra +: 2] ^ g[0].mem[2];
    assign u = g[0].h.x[3:2];
    assign v = gr.rv;
    assign w = g[0].h.x[ra];
    always_ff @(posedge clk) gr.fr <= d;
    always_ff @(posedge clk) gr.am[wa] <= gr.fr;
    assign y = gr.am[ra];
    assign z = cs.cy;
endmodule
"""

_GEN_OUTS = ("p", "q", "r", "t", "u", "v", "w", "y", "z")


def _gen_shapes_expected(stim: list[dict[str, int]]) -> list[dict[str, int]]:
    """``_GEN_SHAPES`` by the SystemVerilog semantics."""
    mem: list[dict[int, int]] = [{}, {}]
    am: dict[int, int] = {}
    fr = t = 0
    out = []
    for v in stim:
        d, s, ra, wa = v["d"], v["s"], v["ra"], v["wa"]
        x0, x1 = d ^ 0xF, (d - 1) & 0xF
        out.append(
            {
                "p": x1,
                "q": mem[1].get(ra, 0),
                "r": mem[0].get(ra, 0) if s else x0,
                "t": t,
                "u": x0 >> 2,
                "v": d ^ 5,
                "w": (x0 >> ra) & 1,
                "y": am.get(ra, 0),
                "z": d & 3,
            }
        )
        t = (((x1 >> ra) & 3) ^ mem[0].get(2, 0)) & 0xF
        for i in (0, 1):
            if s ^ i:
                mem[i][wa] = (d + i) & 0xF
        am[wa] = fr
        fr = d
    return out


def test_generate_block_references(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Dotted names into ``for`` / ``if`` / ``case`` generate blocks,
    nested (``g[1].h.x``) and arrayed, read as scalars, vectors, constant
    and run-time part / bit selects and memory words, in continuous
    assignments, combinational and clocked blocks, and written from
    outside the block (``gr.fr <= d``, ``gr.am[wa] <= ..``): every output
    matches the SystemVerilog value on every cycle, and Yosys'."""
    with caplog.at_level(logging.WARNING, logger=_SLANG_LOG):
        module, _ = _slang(tmp_path, _GEN_SHAPES)
    assert _warnings(caplog) == []
    assert not [c for c in module.cells.values() if c.type == "$_UNKNOWN_"]
    stim = _stimulus(module, 80, 2)
    want = _gen_shapes_expected(stim)
    assert _run(module, stim, _GEN_OUTS) == want
    if _HAVE_YOSYS:
        assert _run(_yosys(tmp_path, _GEN_SHAPES), stim, _GEN_OUTS) == want


# --- child instances and upward references (slang only) ----------------------

_CHILD = """module c (input clk, input [1:0] wa, input [3:0] d, output [3:0] o,
          output [3:0] o2, output [3:0] o3);
    logic [3:0] mem [0:3];
    logic [3:0] last;
    logic [3:0] q;
    logic [3:0] w;
    always_ff @(posedge clk) begin mem[wa] <= d; last <= d; end
    assign o = last;
    assign o2 = q;
    assign o3 = w;
endmodule
module up (output [3:0] o);
    assign o = m.top_sig;
endmodule
module m (input clk, input [1:0] wa, input [1:0] ra, input [3:0] d, input e,
          output [3:0] p, output [3:0] q, output [3:0] r, output [3:0] s,
          output [1:0] t, output [3:0] f, output [3:0] g, output [3:0] h);
    logic [3:0] top_sig;
    assign p = u.mem[ra];
    assign q = ua[1].last;
    assign s = u.d;
    assign t = u.last[3:2];
    c u (.clk(clk), .wa(wa), .d(d), .o(r), .o2(f), .o3(g));
    c ua [1:0] (.clk(clk), .wa(wa), .d(d ^ 4'h3), .o(), .o2(), .o3());
    always_ff @(posedge clk) if (e) u.q <= d;
    always_comb begin u.w = d; if (e) u.w = ~d; end
    assign top_sig = d ^ 4'ha;
    up u_up (.o(h));
endmodule
"""

_CHILD_OUTS = ("p", "q", "r", "s", "t", "f", "g", "h")


def test_child_instance_and_upward_references(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Into a child instance: a memory word read before the instance in
    source order (a ``$memrd_v2`` of ``\\u.mem``), an element of an
    instance array (``ua[1].last``), an input port (``u.d``), a part
    select; a clocked and a combinational write from the parent
    (``u.q <= d``, ``u.w = ~d``); and an upward reference from a child
    (``m.top_sig``). Every output matches the SystemVerilog value on
    every cycle. Yosys 0.64 reads each of these names as a new undriven
    one-bit wire, so there is no Yosys netlist to compare with."""
    with caplog.at_level(logging.WARNING, logger=_SLANG_LOG):
        module, _ = _slang(tmp_path, _CHILD)
    assert _warnings(caplog) == []
    assert not [c for c in module.cells.values() if c.type == "$_UNKNOWN_"]
    stim = _stimulus(module, 80, 3)
    mem: dict[int, int] = {}
    last = ua_last = q = 0
    want = []
    for v in stim:
        d = v["d"]
        want.append(
            {
                "p": mem.get(v["ra"], 0),
                "q": ua_last,
                "r": last,
                "s": d,
                "t": last >> 2,
                "f": q,
                "g": (~d & 0xF) if v["e"] else d,
                "h": d ^ 0xA,
            }
        )
        mem[v["wa"]] = d
        last, ua_last = d, d ^ 3
        if v["e"]:
            q = d
    assert _run(module, stim, _CHILD_OUTS) == want


# --- outside the lowered design ----------------------------------------------

_BLACKBOX = """(* blackbox *) module bb (input a, output o);
    logic t;
    assign t = a;
    assign o = t;
endmodule
module m (input a, input d, output o, output p);
    bb u (.a(a), .o(o));
    assign p = u.t;
    assign u.t = d;
endmodule
"""


def test_reference_into_a_blackbox_is_reported(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A name inside a blackbox has no bits here: the read is an opaque,
    reported driver and the write a reported, dropped assignment."""
    with caplog.at_level(logging.WARNING, logger=_SLANG_LOG):
        module, _ = _slang(tmp_path, _BLACKBOX)
    msgs = _warnings(caplog)
    assert any("`u.t` not lowered" in m for m in msgs), msgs
    assert any("hierarchical reference outside the lowered design" in m for m in msgs)
    (unknown,) = [c for c in module.cells.values() if c.type == "$_UNKNOWN_"]
    assert unknown.connections["Y"] == module.ports["p"].bits


_GREYBOX = """module g (input clk, output logic o);
    always_ff @(posedge clk) o <= m.src;
endmodule
module m (input clk, input d, output o);
    logic src;
    assign src = d;
    g u (.clk(clk), .o(o));
endmodule
"""


def test_upward_reference_from_a_greybox_body_is_reported(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A greybox body lowered on its own cannot see its parent: the
    upward reference is an opaque, reported driver there; inlined, it
    reads the parent's net."""
    with caplog.at_level(logging.WARNING, logger=_SLANG_LOG):
        _, siblings = _slang(tmp_path, _GREYBOX, greybox=["g"])
    assert any("`m.src` not lowered" in m for m in _warnings(caplog))
    assert [c.type for c in siblings["g"].cells.values()].count("$_UNKNOWN_") == 1
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=_SLANG_LOG):
        flat, _ = _slang(tmp_path, _GREYBOX)
    assert _warnings(caplog) == []
    (ff,) = [c for c in flat.cells.values() if c.type == "$dff"]
    assert ff.connections["D"] == flat.ports["d"].bits


# --- verdicts ----------------------------------------------------------------

_SDC = """create_clock -name clk_a -period 10 [get_ports clk_a]
create_clock -name clk_b -period 7 [get_ports clk_b]
set_clock_groups -asynchronous -group {clk_a} -group {clk_b}
"""


def _lint(tmp_path: Path, src: str, frontend: str) -> dict:
    sv = _write(tmp_path, src, f"{frontend}.sv")
    sdc_path = tmp_path / "m.sdc"
    sdc_path.write_text(_SDC)
    out = tmp_path / f"{frontend}.json"
    args = ["lint", "--top", "m", "--sdc", str(sdc_path), "--frontend", frontend]
    args += ["--format", "json", "--output", str(out), str(sv)]
    result = CliRunner().invoke(app, args)
    assert result.exit_code in (0, 1), result.output
    return json.loads(out.read_text())


def _verdict(report: dict) -> tuple[int, dict[str, int]]:
    return report["summary"]["crossings"], dict(
        Counter(v["rule_id"] for v in report["violations"])
    )


_GEN_CROSSING = """module m (input clk_a, input clk_b, input [1:0] wa, input [1:0] ra,
          input [3:0] d, output logic [3:0] q);
    if (1) begin : gr
        logic [3:0] lm [0:3];
        always_ff @(posedge clk_a) lm[wa] <= d;
    end
    always_ff @(posedge clk_b) q <= gr.lm[ra];
endmodule
"""


def test_crossing_through_a_generate_block_memory(tmp_path: Path) -> None:
    """The ``clk_a`` memory read through ``gr.lm[ra]`` into a ``clk_b``
    register is a crossing on slang (there was none: the read was opaque)
    with the same verdict as Yosys'."""
    slang = _verdict(_lint(tmp_path, _GEN_CROSSING, "slang"))
    assert slang[0] >= 1
    assert slang[1]
    if _HAVE_YOSYS:
        assert _verdict(_lint(tmp_path, _GEN_CROSSING, "yosys")) == slang


_CHILD_CROSSING = """module src (input clk, input d);
    logic q;
    always_ff @(posedge clk) q <= d;
endmodule
module m (input clk_a, input clk_b, input d, output logic o);
    src u (.clk(clk_a), .d(d));
    always_ff @(posedge clk_b) o <= u.q;
endmodule
"""


def test_crossing_through_a_child_reference(tmp_path: Path) -> None:
    """A ``clk_b`` register sampling a ``clk_a`` flop through ``u.q`` is
    an unsynchronised crossing from that flop (CDC-001). Yosys 0.64 reads ``u.q`` as an
    undriven wire and finds nothing, so slang alone is checked."""
    report = _lint(tmp_path, _CHILD_CROSSING, "slang")
    (cdc001,) = [v for v in report["violations"] if v["rule_id"] == "CDC-001"]
    assert "src: flop u." in cdc001["message"]
