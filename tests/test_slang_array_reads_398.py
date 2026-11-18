"""slang frontend: unpacked-array reads, memory or registers
(rtl-buddy-cdc#398 items 1 and 2).

Yosys' Verilog frontend decides per array whether it stays a memory or
becomes a list of registers (``mem2reg``); the slang frontend makes the
same decision (``_ModuleBuilder._classify_design_arrays``) and lowers reads and
writes to match:

1. a register array read at a run-time index is a ``$pmux`` over every
   element, selected by index compares (it was a ``$memrd_v2`` of a memory
   nothing writes, so a crossing through the read was missed);
2. a memory read at a constant index is a ``$memrd_v2`` at that address
   (it read the array's undriven placeholder bits).

Each section has a minimal design, the shape the slang path lowers it to
(checked without Yosys), and, where yosys is on PATH, the same design
through the ``lint`` recipe with the verdicts (and memory sets) compared.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import shutil
import subprocess
from collections import Counter
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


@pytest.fixture(autouse=True)
def _tokenizer_sdc_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    """Plain SDCs; the tokenizer avoids a Tcl worker per ``lint``."""
    monkeypatch.setenv(sdc_mod.BACKEND_ENV_VAR, "tokenizer")


def _write(tmp_path: Path, src: str, name: str) -> Path:
    sv = tmp_path / name
    sv.write_text(src)
    return sv


def _slang(tmp_path: Path, src: str) -> Module:
    module, _ = slang_fe.elaborate_with_blackboxes([_write(tmp_path, src, "s.sv")], "m")
    return module


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


def _verdict(report: dict) -> tuple[int, dict[str, int], dict[str, int]]:
    """``(crossings, live findings per rule, suppressed findings per rule)``."""
    live = Counter(v["rule_id"] for v in report["violations"])
    gone = Counter(s["rule_id"] for s in report["suppressed"])
    return report["summary"]["crossings"], dict(live), dict(gone)


def _same_verdict(tmp_path: Path, src: str) -> tuple:
    """The slang ``lint`` verdict, checked against Yosys' when available."""
    slang = _verdict(_lint(tmp_path, src, "slang"))
    if _HAVE_YOSYS:
        assert _verdict(_lint(tmp_path, src, "yosys")) == slang
    return slang


def _drivers(module: Module) -> dict[Bit, tuple[Cell, int]]:
    out: dict[Bit, tuple[Cell, int]] = {}
    for c in module.cells.values():
        for pin in ("Y", "DATA"):
            if pin in c.connections and c.type != "$memwr_v2":
                for i, b in enumerate(c.connections[pin]):
                    out[b] = (c, i)
    return out


def _memories(module: Module) -> dict[str, Counter]:
    """``MEMID`` -> count of each memory cell type."""
    out: dict[str, Counter] = {}
    for c in module.cells.values():
        if c.type.startswith("$mem"):
            out.setdefault(c.parameters["MEMID"], Counter())[c.type] += 1
    return out


def _value(module: Module, bits: tuple[Bit, ...], env: dict[Bit, int]) -> int:
    """``bits`` evaluated over the ``$pmux`` / compare cells, ``env`` the
    leaves."""
    drivers = _drivers(module)

    def num(bs: tuple[Bit, ...]) -> int:
        return sum(get(b) << i for i, b in enumerate(bs))

    def get(bit: Bit) -> int:
        if isinstance(bit, str):
            return int(bit == "1")
        if bit in env:
            return env[bit]
        cell, lane = drivers[bit]
        c = cell.connections
        if cell.type == "$pmux":
            width = len(c["Y"])
            hit = [k for k, s in enumerate(c["S"]) if get(s)]
            return get(c["B"][hit[0] * width + lane]) if hit else 0
        if cell.type == "$eq":
            return int(num(c["A"]) == num(c["B"]))
        assert cell.type == "$logic_not", cell.type
        return int(num(c["A"]) == 0)

    return num(bits)


def _env(module: Module, values: dict[str, int]) -> dict[Bit, int]:
    env: dict[Bit, int] = {}
    for name, val in values.items():
        src = module.ports[name] if name in module.ports else module.netnames[name]
        for k, b in enumerate(src.bits):
            env[b] = (val >> k) & 1
    return env


def _d_of(module: Module, q_port: str) -> tuple[Bit, ...]:
    """``D`` of the flop driving output port ``q_port``."""
    q = module.ports[q_port].bits
    flop = next(c for c in module.cells.values() if c.connections.get("Q") == q)
    return flop.connections["D"]


# --- 1. register array read at a run-time index -------------------------

_REGS = """module m (input clk_a, input clk_b, input [3:0] d, input [1:0] i_in,
          output logic [3:0] q1, output logic [3:0] q2, output logic [3:0] q3,
          output logic q4, output logic [3:0] q5);
    // Written only at constant indices: registers, one netname each.
    logic [3:0] rd [0:3];
    logic [3:0] rr [0:1][0:1];
    logic [1:0] i;
    logic [3:0] o1, o2;
    always_ff @(posedge clk_a) begin
        i <= i_in;
        rd[0] <= d; rd[1] <= rd[0]; rd[2] <= rd[1]; rd[3] <= rd[2];
        rr[0][0] <= d; rr[0][1] <= ~d; rr[1][0] <= d + 4'd1; rr[1][1] <= d - 4'd1;
    end
    // Read at a run-time index from assign, always_comb and always_ff,
    // whole, one bit of the element, and a two-dimensional array.
    assign o1 = rd[i];
    always_comb o2 = rd[i];
    always_ff @(posedge clk_b) begin
        q1 <= o1;
        q2 <= o2;
        q3 <= rd[i];
        q4 <= rd[i][2];
        q5 <= rr[i[1]][i[0]];
    end
endmodule
"""


def test_register_array_read_is_a_mux_of_every_element(tmp_path: Path) -> None:
    """``rd[i]`` of a register array is a ``$pmux`` whose cases are every
    element's register and whose selects compare ``i`` (as Yosys
    ``mem2reg`` + ``proc`` build it); it was a ``$memrd_v2`` of a memory
    nothing writes, cut off from the ``rd`` flops. Evaluated, each read
    picks element ``i``, from ``assign``, ``always_comb`` and ``always_ff``,
    one bit of it, and a two-dimensional array's element."""
    module = _slang(tmp_path, _REGS)
    assert not _memories(module)
    elements = {f"rd[{k}]" for k in range(4)} | {f"rr[{k}]" for k in range(4)}
    assert elements <= set(module.netnames)
    o1 = module.netnames["o1"].bits
    mux, _ = _drivers(module)[o1[0]]
    assert mux.type == "$pmux"
    reg_bits = {b for k in range(4) for b in module.netnames[f"rd[{k}]"].bits}
    assert set(mux.connections["B"]) == reg_bits
    i_bits = module.netnames["i"].bits
    for s in mux.connections["S"]:
        assert _drivers(module)[s][0].connections["A"] == i_bits
    words = {f"rd[{k}]": 1 + 4 * k for k in range(4)}
    rows = {f"rr[{k}]": 9 - 2 * k for k in range(4)}
    for i in range(4):
        env = _env(module, {"i": i, **words, **rows})
        assert _value(module, o1, env) == 1 + 4 * i
        assert _value(module, module.netnames["o2"].bits, env) == 1 + 4 * i
        assert _value(module, _d_of(module, "q3"), env) == 1 + 4 * i
        assert _value(module, _d_of(module, "q4"), env) == (1 + 4 * i) >> 2 & 1
        assert _value(module, _d_of(module, "q5"), env) == 9 - 2 * i


def test_register_array_read_crossing_matches_yosys(tmp_path: Path) -> None:
    """A crossing through the run-time-index read reaches the element
    flops: the same crossings and findings as on the Yosys path (the slang
    path found 4 crossings, no CDC-001, before)."""
    crossings, rules, _ = _same_verdict(tmp_path, _REGS)
    assert crossings == 31
    assert rules["CDC-001"] == 5


# --- 2. memory read at a constant index ----------------------------------

_MEM = """module m (input clk_a, input clk_b, input [3:0] d, input [1:0] i,
          output logic [3:0] q1, output logic [1:0] q2, output logic [3:0] q3);
    // Written at a run-time index: a memory.
    logic [3:0] wr [0:3];
    logic [3:0] o;
    always_ff @(posedge clk_a) wr[i] <= d;
    // Read at constant indices: from assign, part of a word, always_ff.
    assign o = wr[2];
    always_ff @(posedge clk_b) begin
        q1 <= o;
        q2 <= wr[1][2:1];
        q3 <= wr[3];
    end
endmodule
"""


def _const(bits: tuple[Bit, ...]) -> int | None:
    if not all(b in ("0", "1") for b in bits):
        return None
    return sum((b == "1") << k for k, b in enumerate(bits))


def test_memory_read_at_constant_index_is_a_read_port(tmp_path: Path) -> None:
    """``wr[2]`` of a memory is an asynchronous ``$memrd_v2`` at address 2
    (Yosys: a ``$memrd``), not the array's undriven placeholder bits; a
    part of a word is a slice of the port's data. The memory has no
    netname."""
    module = _slang(tmp_path, _MEM)
    assert _memories(module) == {"\\wr": Counter({"$memwr_v2": 1, "$memrd_v2": 3})}
    assert not [n for n in module.netnames if n.startswith("wr")]
    drivers = _drivers(module)

    def port_of(bits: tuple[Bit, ...]) -> tuple[int | None, list[int]]:
        cells = {drivers[b][0].name for b in bits}
        assert len(cells) == 1
        cell = drivers[bits[0]][0]
        assert cell.type == "$memrd_v2"
        assert cell.parameters["MEMID"] == "\\wr"
        return _const(cell.connections["ADDR"]), [drivers[b][1] for b in bits]

    assert port_of(module.netnames["o"].bits) == (2, [0, 1, 2, 3])
    assert port_of(_d_of(module, "q2")) == (1, [1, 2])
    assert port_of(_d_of(module, "q3")) == (3, [0, 1, 2, 3])


def test_memory_constant_read_crossing_matches_yosys(tmp_path: Path) -> None:
    """The constant-index reads see the memory: CDC-026 reports the
    unsynchronised read of a memory another clock writes, as on the Yosys
    path (the slang path had no CDC-026 before)."""
    _crossings, rules, _ = _same_verdict(tmp_path, _MEM)
    assert rules["CDC-026"] == 1


# --- memory or registers: the Yosys decision -----------------------------

_KINDS = """module m (input clk, input rst_n, input [1:0] i,
          input [1:0] j, input [3:0] d, input [3:0] e,
          output logic [3:0] q, output logic [3:0] q1, output logic [3:0] q2,
          output [19:0][3:0] o);
    logic [3:0] c_only [0:3];   // constant writes only: registers
    logic [3:0] v_only [0:3];   // run-time writes only: memory
    logic [3:0] c_and_v [0:3];  // both: memory, a port at address 0
    logic [3:0] x_only [0:3];   // computed index: memory
    logic [3:0] c_and_x [0:3];  // constant and computed: memory
    logic [3:0] blk [0:3];      // blocking write, not read after: memory
    logic [3:0] reread [0:3];   // blocking write, read after: registers
    logic [3:0] comb [0:3];     // always_comb: registers
    logic [3:0] cont [0:3];     // continuous assign: registers
    wire  [3:0] net [0:3];      // a net array: registers
    logic [3:0] lane [0:3];     // one bit of a word: memory
    logic [3:0] rom [0:3];      // never written: memory
    logic [3:0] loop [0:3];     // a for loop's writes: registers
    logic [3:0] srst [0:3];     // sync-reset loop, run-time write: memory
    logic [3:0] arst [0:3];     // async-reset block: registers
    logic [3:0] two [0:3];      // only in an async-reset block's else: registers
    logic [3:0] mdim [0:1][0:1]; // two dimensions, run-time write: memory
    always_ff @(posedge clk) c_only[1] <= d;
    always_ff @(posedge clk) v_only[i] <= d;
    always_ff @(posedge clk) begin c_and_v[i] <= d; c_and_v[0] <= e; end
    always_ff @(posedge clk) x_only[i + 1] <= d;
    always_ff @(posedge clk) begin c_and_x[i + 1] <= d; c_and_x[0] <= e; end
    always_ff @(posedge clk) blk[i] = d;
    always_ff @(posedge clk) begin reread[i] = d; q1 <= reread[j]; end
    always_comb begin
        comb[0] = e; comb[1] = e; comb[2] = e; comb[3] = e; comb[i] = d;
    end
    assign cont[0] = d;
    assign net[0] = d;
    always_ff @(posedge clk) lane[i][1] <= d[0];
    always_ff @(posedge clk) for (int k = 0; k < 4; k++) loop[k] <= d;
    always_ff @(posedge clk)
        if (!rst_n) for (int k = 0; k < 4; k++) srst[k] <= '0;
        else if (e[0]) srst[i] <= d;
    always_ff @(posedge clk or negedge rst_n)
        if (!rst_n) for (int k = 0; k < 4; k++) arst[k] <= '0;
        else arst[i] <= d;
    always_ff @(posedge clk or negedge rst_n)
        if (!rst_n) q2 <= '0;
        else begin q2 <= e; two[i] <= d; end
    always_ff @(posedge clk) mdim[i[1]][i[0]] <= d;
    always_ff @(posedge clk) q <= blk[j];
    assign o[0] = c_only[j];  assign o[1] = v_only[j];  assign o[2] = c_and_v[j];
    assign o[3] = x_only[j];  assign o[4] = c_and_x[j]; assign o[5] = comb[j];
    assign o[6] = cont[j];    assign o[7] = net[j];     assign o[8] = lane[j];
    assign o[9] = rom[j];     assign o[10] = loop[j];   assign o[11] = srst[j];
    assign o[12] = arst[j];   assign o[13] = two[j];    assign o[14] = mdim[j[1]][j[0]];
    assign o[15] = v_only[2]; assign o[16] = srst[1];   assign o[17] = mdim[1][0];
    assign o[18] = arst[3];   assign o[19] = c_only[2];
endmodule
"""

# MEMID -> (write ports, read ports) of each array Yosys keeps a memory.
_KIND_MEMORIES = {
    "\\v_only": (1, 2),
    "\\c_and_v": (2, 1),
    "\\x_only": (1, 1),
    "\\c_and_x": (2, 1),
    "\\blk": (1, 1),
    "\\lane": (1, 1),
    "\\rom": (0, 1),
    "\\srst": (5, 2),
    "\\mdim": (1, 2),
}


def _ports(module: Module) -> dict[str, tuple[int, int]]:
    out = {}
    for memid, kinds in _memories(module).items():
        writes = sum(n for t, n in kinds.items() if t.startswith("$memwr"))
        reads = sum(n for t, n in kinds.items() if t.startswith("$memrd"))
        out[memid] = (writes, reads)
    return out


def test_memory_or_registers_as_yosys_decides(tmp_path: Path) -> None:
    """Which arrays stay memories, and their port counts, are Yosys': a
    run-time-index write in a single-edge process makes a memory (its
    constant-index writes and reads become ports too, a sync reset's
    loop included); constant indices only, a blocking write read back,
    a write outside a single-edge process (``always_comb``, a block
    with an async reset), a continuous assignment or a net make registers.
    Registers keep their element netnames, memories have none."""
    module = _slang(tmp_path, _KINDS)
    assert _ports(module) == _KIND_MEMORIES
    names = set(module.netnames)
    for arr in ("c_only", "reread", "comb", "cont", "net", "loop", "arst", "two"):
        assert {f"{arr}[{k}]" for k in range(4)} <= names, arr
    for memid in _KIND_MEMORIES:
        assert not [n for n in names if n.startswith(memid[1:] + "[")], memid
    if _HAVE_YOSYS:
        assert _ports(_yosys(tmp_path, _KINDS)) == _KIND_MEMORIES


def test_memory_write_ports_carry_their_conditions(tmp_path: Path) -> None:
    """A memory written under a sync reset gets one port per reset-loop
    element, enabled by the reset, and the data port enabled by the other
    arm (``srst``); a write of one bit of a word enables that lane only
    (``lane``). Their ``EN`` reaches ``rst_n``, as Yosys' does."""
    module = _slang(tmp_path, _KINDS)
    srst = [
        c
        for c in module.cells.values()
        if c.type == "$memwr_v2" and c.parameters["MEMID"] == "\\srst"
    ]
    rst_n = module.ports["rst_n"].bits[0]
    drivers = {b: c for c in module.cells.values() for b in c.connections.get("Y", ())}

    def fanin(bit: Bit) -> set[Bit]:
        seen: set[Bit] = set()
        todo = [bit]
        while todo:
            b = todo.pop()
            if b in seen:
                continue
            seen.add(b)
            cell = drivers.get(b)
            if cell is not None:
                for pin, bs in cell.connections.items():
                    if pin != "Y":
                        todo.extend(bs)
        return seen

    addrs = sorted(a for c in srst if (a := _const(c.connections["ADDR"])) is not None)
    assert addrs == [0, 1, 2, 3]
    for c in srst:
        assert all(rst_n in fanin(b) for b in c.connections["EN"])
    (lane,) = [
        c
        for c in module.cells.values()
        if c.type == "$memwr_v2" and c.parameters["MEMID"] == "\\lane"
    ]
    assert lane.connections["EN"] == ("0", "1", "0", "0")
    assert lane.connections["DATA"][1] == module.ports["d"].bits[0]


def test_register_array_write_at_runtime_index(tmp_path: Path) -> None:
    """A register array written at a run-time index (an async-reset block)
    loads each element through ``mux(i == k, Q, d)``, as Yosys ``mem2reg``
    rewrites it; a blocking one is read back through the same muxes."""
    module = _slang(tmp_path, _KINDS)
    drivers = _drivers(module)
    i_bits = module.ports["i"].bits
    for k in range(4):
        q = module.netnames[f"arst[{k}]"].bits
        flop = next(c for c in module.cells.values() if c.connections.get("Q") == q)
        assert flop.type == "$adff"
        mux, _ = drivers[flop.connections["D"][0]]
        assert mux.type == "$mux"
        assert mux.connections["A"] == q
        assert mux.connections["B"] == module.ports["d"].bits
        select, _ = drivers[mux.connections["S"][0]]
        assert select.connections["A"] == i_bits
        assert select.type == ("$logic_not" if k == 0 else "$eq")


# --- the shapes around them ------------------------------------------------

_EDGES = """module m (input clk, input en, input srst, input rst_n, input [1:0] i, input [1:0] j,
          input [3:0] d, output logic [3:0] q, output [1:0] o1, output [1:0] o2,
          output [3:0] o3, output [3:0] o4, output [3:0] o5,
          output logic [3:0] q2, output [3:0] o6, output [3:0] o7, output [3:0] o8);
    typedef struct packed { logic [1:0] hi; logic [1:0] lo; } pair_t;
    logic [3:0] cat [0:3];      // a word in a concatenation target
    logic [3:0] t;
    logic [3:0] whole [0:1];    // written whole: registers
    pair_t      sa [0:3];       // packed-struct elements, constant writes
    logic [3:0] mw [0:3];       // memory, one run-time bit of a word written
    logic [3:0] rw [0:3];       // registers, one run-time bit of a word written
    logic [3:0] ce [0:3];       // memory under an enable and a sync reset
    logic [3:0] ng [-2:1];      // negative indices
    logic [3:0] r2 [0:1][0:1];  // registers (async-reset block), 2-D
    logic [3:0] md2 [0:1][0:1]; // memory, 2-D
    logic [3:0] rom [0:3];      // written by an initial block only: memory
    integer n;
    always_ff @(posedge clk) {cat[i], t} <= {d, d};
    always_ff @(posedge clk) whole <= '{d, d};
    always_ff @(posedge clk) begin sa[0] <= d; sa[1].hi <= d[1:0]; end
    always_ff @(posedge clk) mw[i][j] <= d[0];
    always_ff @(posedge clk or negedge rst_n)
        if (!rst_n) q <= '0; else begin q <= d; rw[i][j] <= d[0]; end
    always_ff @(posedge clk)
        if (en) begin
            if (srst) ce[0] <= '0;
            else ce[i] <= d;
        end
    always_ff @(posedge clk) begin ng[-2] <= d; ng[1] <= ~d; end
    always_ff @(posedge clk or negedge rst_n)
        if (!rst_n) q2 <= '0;
        else begin
            q2 <= d; r2[1][i[0]] <= d; r2[i[1]][i[0]] <= ~d; r2[0] <= r2[1];
        end
    always_ff @(posedge clk) md2[i[1]][i[0]] <= d;
    always_ff @(posedge clk) for (n = 0; n < 2; n = n + 1) md2[n][1] <= d;
    initial rom[0] = 4'd1;
    assign o6 = r2[j[1]][j[0]];
    assign o7 = md2[1][j[0]];
    assign o8 = rom[j];
    assign o1 = sa[j].lo;
    assign o2 = ng[i][2:1];
    assign o3 = ce[j];
    assign o4 = whole[j[0]];
    assign o5 = cat[j];
endmodule
"""


def test_array_shapes_around_the_reads(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Selects of a computed word (a struct member, a part of an element
    with negative indices) slice the ``$pmux``; a two-dimensional register
    array is read through a ``$pmux`` per dimension, a two-dimensional
    memory through one port at the flattened address; an array only an
    ``initial`` block writes is a memory; a memory written under an enable
    and a sync reset gets a port per arm, enabled by both. A word in a
    concatenation target and a run-time bit of a word are lowered
    (rtl-buddy-cdc#402; they were reported and dropped), see
    ``tests/test_slang_loops_arrays_402.py``."""
    with caplog.at_level(logging.WARNING, logger="rtl_buddy_cdc.frontends.slang"):
        module = _slang(tmp_path, _EDGES)
    dropped = [r.getMessage() for r in caplog.records if "assignment" in r.getMessage()]
    assert dropped == []
    assert set(_memories(module)) == {"\\cat", "\\ce", "\\md2", "\\mw", "\\rom"}
    assert "mw[0]" not in module.netnames  # a memory: no element names
    drivers = _drivers(module)
    for port, memid in (("o7", "\\md2"), ("o8", "\\rom")):
        cell = drivers[module.ports[port].bits[0]][0]
        assert (cell.type, cell.parameters["MEMID"]) == ("$memrd_v2", memid)
    r2 = {f"r2[{k}]": 15 - k for k in range(4)}
    sa = {f"sa[{k}]": 4 * k + 3 for k in range(4)}
    ng = {f"ng[{k}]": 3 * (k + 2) for k in range(-2, 2)}
    for j in range(4):
        env = _env(module, {"i": j, "j": j, **sa, **ng, **r2})
        assert _value(module, module.ports["o6"].bits, env) == 15 - j
        assert _value(module, module.ports["o1"].bits, env) == (4 * j + 3) & 3
        if j < 2:  # ng[0], ng[1]; ng[2], ng[3] are out of range
            assert _value(module, module.ports["o2"].bits, env) == 3 * (j + 2) >> 1 & 3
    ce = [
        c
        for c in module.cells.values()
        if c.type == "$memwr_v2" and c.parameters["MEMID"] == "\\ce"
    ]
    assert len(ce) == 2
    en = module.ports["en"].bits[0]
    for c in ce:
        (name,) = {_drivers(module)[b][0].name for b in c.connections["EN"]}
        gate = module.cells[name]
        assert gate.type == "$and" and gate.connections["A"] == (en,)
