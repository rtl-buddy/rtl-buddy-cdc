"""slang frontend: the clocked mux order and unpacked-array netnames of
rtl-buddy-cdc#394 (items 6 and 7).

Each section has a minimal design, the netlist shape the slang path
lowers it to, and, where yosys is on PATH, the same design through the
``lint`` recipe (``proc; flatten; opt_clean``) with the shapes, names and
findings compared. CI's pytest job has no yosys, so there the slang-side
assertions stand alone (they pin the shape the Yosys comparison was made
against locally).
"""

from __future__ import annotations

import importlib.util
import itertools
import json
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
from rtl_buddy_cdc.sdc_objects import NameIndex

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


def _slang(tmp_path: Path, src: str, top: str = "m") -> Module:
    module, _ = slang_fe.elaborate_with_blackboxes([_write(tmp_path, src, "s.sv")], top)
    return module


def _yosys(tmp_path: Path, src: str, top: str = "m") -> Module:
    """The ``lint`` recipe, from a script file: CI's yosys 0.52 splits a
    ``-p`` script at commas."""
    sv = _write(tmp_path, src, "y.sv")
    out = tmp_path / "y.json"
    script = tmp_path / "y.ys"
    script.write_text(
        f"read_verilog -sv {sv}\nhierarchy -top {top}\nproc\nflatten\n"
        f"opt_clean\nwrite_json {out}\n"
    )
    subprocess.run(["yosys", "-q", "-s", str(script)], check=True)
    return netlist.load(out)


_SDC = """create_clock -name clk_a -period 10 [get_ports clk_a]
create_clock -name clk_b -period 7 [get_ports clk_b]
set_clock_groups -asynchronous -group {clk_a} -group {clk_b}
"""


def _lint(tmp_path: Path, src: str, frontend: str, sdc: str) -> dict:
    sv = _write(tmp_path, src, f"{frontend}.sv")
    sdc_path = tmp_path / "m.sdc"
    sdc_path.write_text(sdc)
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


def _same_verdict(tmp_path: Path, src: str, sdc: str) -> tuple:
    """The slang ``lint`` verdict, checked against Yosys' when available."""
    slang = _verdict(_lint(tmp_path, src, "slang", sdc))
    if _HAVE_YOSYS:
        assert _verdict(_lint(tmp_path, src, "yosys", sdc)) == slang
    return slang


def _bit_names(module: Module) -> dict[Bit, str]:
    """Port bits and register (public netname) bits by name, ports first."""
    names: dict[Bit, str] = {}
    for p, port in module.ports.items():
        for i, b in enumerate(port.bits):
            names.setdefault(b, f"{p}[{i}]")
    for n, nn in module.netnames.items():
        if "$" not in n:
            for i, b in enumerate(nn.bits):
                names.setdefault(b, f"{n}[{i}]")
    return names


def _drivers(module: Module) -> dict[Bit, tuple[Cell, int]]:
    out: dict[Bit, tuple[Cell, int]] = {}
    for c in module.cells.values():
        if "Y" in c.connections and "Q" not in c.connections:
            for i, b in enumerate(c.connections["Y"]):
                out[b] = (c, i)
    return out


def _d_trees(module: Module, regs: tuple[str, ...]) -> dict[str, object]:
    """``reg[i]`` -> the ``$mux`` tree driving that bit's ``D``, as nested
    ``(select, A, B)`` tuples down to port / register bits and constants."""
    names = _bit_names(module)
    drivers = _drivers(module)

    def tree(bit: Bit) -> object:
        if isinstance(bit, str) or bit in names:
            return names.get(bit, bit)
        cell, lane = drivers[bit]
        conn = cell.connections
        if cell.type != "$mux":
            return cell.type
        return (tree(conn["S"][0]), tree(conn["A"][lane]), tree(conn["B"][lane]))

    out: dict[str, object] = {}
    for c in module.cells.values():
        q = c.connections.get("Q")
        if q is None:
            continue
        for lane, b in enumerate(q):
            name = names.get(b, "")
            if name.split("[")[0] in regs:
                out[name] = tree(c.connections["D"][lane])
    return out


# --- 6. clocked ``if`` / ``else if``: the first condition outermost -------

_CHAIN = """module m (input clk, input rst_n, input c1, input c2, input c3,
          input [3:0] a, input [3:0] b, input [3:0] e,
          output reg [3:0] q, output reg [3:0] p, output reg [3:0] r,
          output reg [3:0] t);
    // An else-if chain under an async reset.
    always_ff @(posedge clk or negedge rst_n)
        if (!rst_n)  q <= 4'h0;
        else if (c1) q <= a;
        else if (c2) q <= b;
        else if (c3) q <= e;
    // Nested if / else, each arm writing a different part.
    always_ff @(posedge clk)
        if (c1) begin
            if (c2) p <= a;
            else    p[1] <= b[1];
        end else    p[3:2] <= e[3:2];
    // Writes before, inside and after the conditionals.
    always_ff @(posedge clk) begin
        r <= e;
        if (c1)      r[2:1] <= a[2:1];
        else if (c2) r <= b;
        if (c3)      r[0] <= a[0];
    end
    // An if nested in an if, no else.
    always_ff @(posedge clk)
        if (c1) begin
            if (c2) t <= a;
            else    t <= b;
        end
endmodule
"""


def test_else_if_chain_puts_the_first_condition_outermost(tmp_path: Path) -> None:
    """``if (c1) .. else if (c2) ..`` in a clocked block drives ``D`` with
    ``mux(c1, mux(c2, .., b), a)``, the first condition's mux outermost,
    as Yosys ``proc`` builds it; it used to be ``mux(~c1 & c2, mux(c1, Q,
    a), b)``. Each register bit's whole mux tree matches the Yosys one:
    nested arms, holds, and writes before and after an ``if``."""
    module = _slang(tmp_path, _CHAIN)
    got = _d_trees(module, ("q", "p", "r", "t"))
    assert got["q[0]"] == (
        "c1[0]",
        ("c2[0]", ("c3[0]", "q[0]", "e[0]"), "b[0]"),
        "a[0]",
    )
    assert got["p[1]"] == ("c1[0]", "p[1]", ("c2[0]", "b[1]", "a[1]"))
    assert got["p[3]"] == ("c1[0]", "e[3]", ("c2[0]", "p[3]", "a[3]"))
    assert got["r[0]"] == (
        "c3[0]",
        ("c1[0]", ("c2[0]", "e[0]", "b[0]"), "e[0]"),
        "a[0]",
    )
    assert got["r[2]"] == ("c1[0]", ("c2[0]", "e[2]", "b[2]"), "a[2]")
    assert got["t[0]"] == ("c1[0]", "t[0]", ("c2[0]", "b[0]", "a[0]"))
    # No enable logic is left over: the selects are the conditions.
    assert not [c for c in module.cells.values() if c.type in ("$and", "$not")]
    if _HAVE_YOSYS:
        assert _d_trees(_yosys(tmp_path, _CHAIN), ("q", "p", "r", "t")) == got


_CASES = """module m (input clk, input c1, input [1:0] s, input a, input b,
          input e, output reg q, output reg w, output reg v);
    // A case under an if: the if's mux outermost, then the items in order.
    always_ff @(posedge clk)
        if (c1)
            case (s)
                2'd0:       q <= a;
                2'd1, 2'd2: q <= b;
                default:    q <= e;
            endcase
    // Overlapping casez items: the first match wins.
    always_ff @(posedge clk)
        casez (s)
            2'b1?: w <= a;
            2'b11: w <= b;
            2'b?1: w <= e;
        endcase
    // An if in a case item, a case in the else arm.
    always_ff @(posedge clk)
        if (c1) v <= a;
        else
            case (s)
                2'd3: if (b) v <= e;
                default: v <= b;
            endcase
endmodule
"""


def _value(module: Module, bits: tuple[Bit, ...], inputs: dict[str, int]) -> int:
    """``bits`` evaluated over the combinational cells, ``inputs`` (by port
    or netname) the leaves."""
    env: dict[Bit, int] = {}
    for name, val in inputs.items():
        src = module.ports[name] if name in module.ports else module.netnames[name]
        for i, b in enumerate(src.bits):
            env[b] = (val >> i) & 1
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
        width = len(c["Y"])
        if cell.type == "$mux":
            return get((c["B"] if get(c["S"][0]) else c["A"])[lane])
        if cell.type == "$pmux":
            hit = [i for i, s in enumerate(c["S"]) if get(s)]
            if not hit:
                return get(c["A"][lane])
            return get(c["B"][hit[0] * width + lane])
        a = num(c["A"])
        y = {
            "$eq": lambda: int(a == num(c["B"])),
            "$logic_not": lambda: int(a == 0),
            "$not": lambda: ~a,
            "$or": lambda: a | num(c["B"]),
            "$and": lambda: a & num(c["B"]),
            "$reduce_or": lambda: int(a != 0),
            "$reduce_bool": lambda: int(a != 0),
        }[cell.type]()
        return (y >> lane) & 1

    return num(bits)


def _next_state(module: Module, reg: str) -> dict[tuple[int, ...], int]:
    """Each input combination (and the register's own value) -> ``D``."""
    flop = next(
        c
        for c in module.cells.values()
        if set(c.connections.get("Q", ())) & set(module.netnames[reg].bits)
    )
    names = ("c1", "s", "a", "b", "e", reg)
    widths = (1, 2, 1, 1, 1, 1)
    out = {}
    for combo in itertools.product(*(range(1 << w) for w in widths)):
        out[combo] = _value(module, flop.connections["D"], dict(zip(names, combo)))
    return out


def test_case_items_keep_their_priority(tmp_path: Path) -> None:
    """A clocked ``case`` chains its items' muxes first item outermost
    (Yosys: a ``$pmux``, whose first asserted select wins), under the mux
    of an enclosing ``if``; overlapping ``casez`` items resolve first match
    wins. The next-state function of every register equals Yosys'."""
    module = _slang(tmp_path, _CASES)
    w = _next_state(module, "w")
    for (c1, s, a, b, e, q), d in w.items():
        assert d == (a if s & 2 else e if s & 1 else q), (s, d)
    trees = _d_trees(module, ("q",))
    assert trees["q[0]"][:2] == ("c1[0]", "q[0]")  # type: ignore[index]
    if _HAVE_YOSYS:
        y_mod = _yosys(tmp_path, _CASES)
        for reg in ("q", "w", "v"):
            assert _next_state(y_mod, reg) == _next_state(module, reg), reg


# --- 7. unpacked-array elements are netnames of their own ----------------

_ARRAYS = """module sub (input clk, input [1:0] d, output [1:0] o);
    (* cdc_sync *) logic [1:0] ch [0:1];
    always_ff @(posedge clk) begin ch[0] <= d; ch[1] <= ch[0]; end
    assign o = ch[1];
endmodule
module m (input clk, input [1:0] i, input [7:0] d, output [63:0] o,
          output [1:0] so);
    logic [7:4] a [0:1];        // ascending, offset element range
    logic [0:3] c [3:2];        // descending, ascending element range
    logic [3:0] mf [1:0][2:0];  // two dimensions, both descending
    logic [3:0] mx [2:3][5:4];  // two dimensions, mixed
    logic sb [4];               // single-bit elements
    wire [5:2] w [1:0];         // a net array
    logic [3:0] mem [0:3];      // written at a run-time address: a memory
    assign w[0] = d[3:0];
    assign w[1] = d[7:4];
    always_ff @(posedge clk) begin
        a[0] <= d[3:0];  a[1] <= d[7:4];
        c[2] <= d[3:0];  c[3] <= d[7:4];
        mf[1][2] <= 4'd1; mf[1][1] <= 4'd2; mf[1][0] <= 4'd3;
        mf[0][2] <= 4'd4; mf[0][1] <= 4'd5; mf[0][0] <= 4'd6;
        mx[2][5] <= 4'd7; mx[2][4] <= 4'd8; mx[3][5] <= 4'd9; mx[3][4] <= 4'd10;
        sb[0] <= d[0]; sb[1] <= d[1]; sb[2] <= d[2]; sb[3] <= d[3];
        mem[i] <= d[3:0];
    end
    assign o = {a[1], a[0], c[3], c[2], mf[1][2] ^ mf[1][1] ^ mf[1][0],
                mf[0][2] ^ mf[0][1] ^ mf[0][0], mx[2][5] ^ mx[2][4],
                mx[3][5] ^ mx[3][4], sb[3], sb[2], sb[1], sb[0],
                w[1] ^ w[0], 4'h0};
    sub u (.clk(clk), .d(d[1:0]), .o(so));
endmodule
"""

_ARRAY_NETS = {
    "a[0]": (4, 4, False),
    "a[1]": (4, 4, False),
    "c[2]": (4, 0, True),
    "c[3]": (4, 0, True),
    **{f"mf[{k}]": (4, 0, False) for k in range(6)},
    **{f"mx[{k}]": (4, 0, False) for k in range(4)},
    **{f"sb[{k}]": (1, 0, False) for k in range(4)},
    "w[0]": (4, 2, False),
    "w[1]": (4, 2, False),
    "u.ch[0]": (2, 0, False),
    "u.ch[1]": (2, 0, False),
}


def _array_nets(module: Module) -> dict[str, tuple[int, int, bool]]:
    return {
        n: (len(nn.bits), nn.offset, nn.upto)
        for n, nn in module.netnames.items()
        if n.split("[")[0] in ("a", "c", "mf", "mx", "sb", "w", "mem", "u.ch")
    }


def _register_values(module: Module) -> dict[str, int]:
    """Netname -> the constant its register loads (``mf`` / ``mx``)."""
    out = {}
    for c in module.cells.values():
        q = c.connections.get("Q")
        for n, nn in module.netnames.items():
            if q is not None and tuple(q) == nn.bits and n[:2] in ("mf", "mx"):
                out[n] = sum(
                    1 << i for i, b in enumerate(c.connections["D"]) if b == "1"
                )
    return out


def _report_names(module: Module) -> dict[str, str]:
    """The name a report gives each element's bits (the narrowest public
    vector holding them, ``rules._bit_vector_names``)."""
    from rtl_buddy_cdc.rules import _bit_vector_names

    names = _bit_vector_names(module)
    return {n: names[module.netnames[n].bits[0]] for n in ("a[1]", "c[3]", "mx[2]")}


def test_unpacked_array_elements_are_netnames(tmp_path: Path) -> None:
    """``logic [7:4] a [0:1]`` is two netnames, ``a[0]`` and ``a[1]``, each
    4 bits with offset 4, as Yosys ``mem2reg`` names them; it used to be
    one 8-bit ``a``. A one-dimensional array names each element by its
    declared index, a multi-dimensional one by its row-major position,
    each dimension counted from its left bound. Every element keeps the
    declaration's attributes, and a report names the element; a memory
    has no netname."""
    module = _slang(tmp_path, _ARRAYS)
    assert _array_nets(module) == _ARRAY_NETS
    assert all(module.netnames[f"u.ch[{k}]"].attributes.get("cdc_sync") for k in (0, 1))
    assert _report_names(module) == {n: n for n in ("a[1]", "c[3]", "mx[2]")}
    # mf[1][2] is the first element, mx[3][4] the last.
    values = _register_values(module)
    assert values == {
        **{f"mf[{k}]": k + 1 for k in range(6)},
        **{f"mx[{k}]": k + 7 for k in range(4)},
    }
    if _HAVE_YOSYS:
        y_mod = _yosys(tmp_path, _ARRAYS)
        assert _array_nets(y_mod) == _ARRAY_NETS
        assert _register_values(y_mod) == values
        assert _report_names(y_mod) == _report_names(module)
        assert all(
            y_mod.netnames[f"u.ch[{k}]"].attributes.get("cdc_sync") for k in (0, 1)
        )


_ARRAY_BASES = ("a", "c", "mf", "mx", "sb", "w", "u.ch")


@pytest.mark.parametrize(
    ("kind", "pattern", "want"),
    [
        ("registers", "a[1]", {f"a[1]:{i}" for i in range(4)}),
        ("registers", "a_reg[1]", {f"a[1]:{i}" for i in range(4)}),
        ("registers", "c_reg[2]", {f"c[2]:{i}" for i in range(4)}),
        ("registers", "mf[4]", {f"mf[4]:{i}" for i in range(4)}),
        (
            "registers",
            "mx_reg[*]",
            {f"mx[{k}]:{i}" for k in range(4) for i in range(4)},
        ),
        ("registers", "sb[2]", {"sb[2]:0"}),
        ("registers", "u.ch_reg[1]", {"u.ch[1]:0", "u.ch[1]:1"}),
        ("nets", "a[0]", {f"a[0]:{i}" for i in range(4)}),
        ("nets", "w[1]", {f"w[1]:{i}" for i in range(4)}),
        ("nets", "sb[*]", {f"sb[{k}]:0" for k in range(4)}),
        # A bit of an element, in the element's declared range
        # (rtl-buddy-cdc#398): ``a[1]`` is ``[7:4]``, ``c[3]`` ``[0:3]``.
        ("registers", "a[1][5]", {"a[1]:1"}),
        ("registers", "a_reg[0][7:6]", {"a[0]:2", "a[0]:3"}),
        ("registers", "a_reg[1][2]", set()),  # outside [7:4]
        ("registers", "u.ch_reg[*][0]", {"u.ch[0]:0", "u.ch[1]:0"}),
        ("nets", "c[3][1]", {"c[3]:2"}),
        ("nets", "w[0][5:4]", {"w[0]:2", "w[0]:3"}),
    ],
)
def test_unpacked_element_names_resolve_alike(
    tmp_path: Path, kind: str, pattern: str, want: set[str]
) -> None:
    """``get_cells`` / ``get_nets`` names of an unpacked element (``a[1]``,
    ``a_reg[1]``, ``mx_reg[*]``) pick the same element bits on both
    frontends; on slang ``a[1]`` used to be bit 1 of one 8-bit ``a``."""

    def resolve(module: Module) -> set[str]:
        index = NameIndex(module)
        names = {
            b: f"{n}:{i}"
            for n, nn in module.netnames.items()
            if n.split("[")[0] in _ARRAY_BASES
            for i, b in enumerate(nn.bits)
        }
        if kind == "nets":
            bits = index.net_bits(pattern)
        else:
            sel = index.registers(pattern)
            bits = {
                module.cells[f].connections["Q"][q]
                for f, pos in sel.items()
                for q in pos
            }
        return {names[b] for b in bits}

    assert resolve(_slang(tmp_path, _ARRAYS)) == want
    if _HAVE_YOSYS:
        assert resolve(_yosys(tmp_path, _ARRAYS)) == want


_ARRAY_CDC = """module m (input clk_a, input clk_b, input [3:0] d,
          output reg [3:0] q0, output reg [3:0] q1);
    logic [7:4] src [0:1];
    always_ff @(posedge clk_a) begin src[0] <= d; src[1] <= ~d; end
    always_ff @(posedge clk_b) begin q0 <= src[0]; q1 <= src[1]; end
endmodule
"""


@pytest.mark.parametrize(
    ("constraint", "cut"),
    [
        ("", 0),
        ("set_false_path -from [get_cells src_reg[1]]", 1),
        ("set_false_path -through [get_nets {src[0]}]", 1),
        ("set_false_path -from [get_pins src_reg[0]/Q]", 1),
        ("set_false_path -from [get_pins {src_reg[0][7:4]/Q}]", 1),
        ("set_false_path -through [get_nets {src[1][*]}]", 1),
        ("set_false_path -from [get_cells {src_reg[1][6]}]", 0),  # 1 bit of 4
    ],
)
def test_unpacked_element_constraints_cut_alike(
    tmp_path: Path, constraint: str, cut: int
) -> None:
    """A constraint naming an unpacked element (or one bit of it) cuts the
    same crossing on both frontends, and the findings name the same
    registers."""
    sdc = _SDC + "set_input_delay -clock clk_a 1 [get_ports d]\n" + constraint + "\n"
    crossings, live, gone = _same_verdict(tmp_path, _ARRAY_CDC, sdc)
    assert crossings == 2
    assert sum(live.values()) == 2 - cut
    assert sum(gone.values()) == cut
