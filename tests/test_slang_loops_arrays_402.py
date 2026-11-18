"""slang frontend: loop variables, run-time index writes, negated sync
selects and memory names (rtl-buddy-cdc#402 items 1, 2, 3, 4, 8 and 9).

1. A bit or part select of an unrolled ``for`` loop's variable (or of a
   genvar) reads the iteration value's bits; it read 0.
2. A combinational write to a register array at a run-time index is one
   lane-aligned ``mux(i == k, old, d)`` per element; it was a ``$shl`` by
   ``hi - i`` over the whole array, so an ascending array's ``g[i]``
   reached element ``hi - i``. A run-time bit of a packed vector is
   ``(old & ~mask) | data``; it was ``old | data``.
3. A run-time bit of an array word (``m[i][j] <= b``), an array word in a
   concatenation target (``{cx[i], t} <= ..``) and a run-time bit of a
   plain vector in a clocked block are lowered; they were reported and
   dropped.
4. ``if (!ra) .. else if (!rb) ..`` selects on ``rb`` with the mux legs
   swapped, as ``proc`` builds it, so the reset map reads it active low.
8. A memory's ``MEMID`` is its declaration's name, whatever generate
   block its ports are in.
9. Netname differences kept on purpose, pinned here.

Each section has a minimal design, checked on the slang netlist alone
(by simulation against the SystemVerilog semantics, or by shape) and,
where yosys is on PATH, against the ``lint`` recipe's netlist by
simulation, and on the ``lint`` verdict. CI's pytest job has no yosys, so
there the slang-side assertions stand alone.
"""

from __future__ import annotations

import importlib.util
import itertools
import json
import logging
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


def _verdict(report: dict) -> tuple[int, dict[str, int]]:
    """``(crossings, findings per rule)``."""
    return report["summary"]["crossings"], dict(
        Counter(v["rule_id"] for v in report["violations"])
    )


def _same_verdict(tmp_path: Path, src: str) -> tuple[int, dict[str, int]]:
    """The slang ``lint`` verdict, checked against Yosys' when available."""
    slang = _verdict(_lint(tmp_path, src, "slang"))
    if _HAVE_YOSYS:
        assert _verdict(_lint(tmp_path, src, "yosys")) == slang
    return slang


# --- a netlist simulator ---------------------------------------------------


def _param(cell: Cell, name: str) -> int:
    value = cell.parameters.get(name, "0") or "0"
    try:
        return int(value, 2)
    except ValueError:
        return int(value)


def _signed(value: int, width: int, signed: bool) -> int:
    value &= (1 << width) - 1
    if signed and width and value >> (width - 1):
        value -= 1 << width
    return value


def _cell_value(cell: Cell, num: Callable[[tuple[Bit, ...]], int]) -> int:
    """The ``Y`` value of a combinational cell, ``x`` read as 0."""
    c = cell.connections
    width = len(c["Y"])
    if cell.type == "$mux":
        return num(c["B"] if num(c["S"]) else c["A"])
    if cell.type == "$pmux":
        for k, s in enumerate(c["S"]):
            if num((s,)):
                return num(c["B"][k * width : (k + 1) * width])
        return num(c["A"])
    a_bits, b_bits = c.get("A", ()), c.get("B", ())
    ua, ub = num(a_bits), num(b_bits)
    a = _signed(ua, len(a_bits), bool(_param(cell, "A_SIGNED")))
    b = _signed(ub, len(b_bits), bool(_param(cell, "B_SIGNED")))
    if cell.type in ("$shift", "$shiftx"):
        return ua >> b if b >= 0 else ua << -b
    ops: dict[str, Callable[[], int]] = {
        "$not": lambda: ~a,
        "$neg": lambda: -a,
        "$logic_not": lambda: int(a == 0),
        "$and": lambda: a & b,
        "$or": lambda: a | b,
        "$xor": lambda: a ^ b,
        "$xnor": lambda: ~(a ^ b),
        "$logic_and": lambda: int(a != 0 and b != 0),
        "$logic_or": lambda: int(a != 0 or b != 0),
        "$reduce_or": lambda: int(ua != 0),
        "$reduce_bool": lambda: int(ua != 0),
        "$eq": lambda: int(a == b),
        "$ne": lambda: int(a != b),
        "$lt": lambda: int(a < b),
        "$le": lambda: int(a <= b),
        "$gt": lambda: int(a > b),
        "$ge": lambda: int(a >= b),
        "$add": lambda: a + b,
        "$sub": lambda: a - b,
        "$mul": lambda: a * b,
        "$shl": lambda: ua << ub,
        "$shr": lambda: ua >> ub,
    }
    return ops[cell.type]()


def _evaluator(module: Module, values: dict[str, int]) -> Callable[..., int]:
    """``bits -> int`` over ``module``'s combinational cells, the ports
    and netnames in ``values`` its inputs (a register's ``Q`` included)."""
    env: dict[Bit, int] = {}
    for name, value in values.items():
        src = module.ports[name] if name in module.ports else module.netnames[name]
        for k, bit in enumerate(src.bits):
            env[bit] = (value >> k) & 1
    comb: dict[Bit, tuple[Cell, int]] = {}
    for c in module.cells.values():
        if "Y" in c.connections and "Q" not in c.connections:
            for k, bit in enumerate(c.connections["Y"]):
                comb[bit] = (c, k)
    memo: dict[str, int] = {}

    def get(bit: Bit) -> int:
        if isinstance(bit, str):
            return int(bit == "1")
        if bit in env:
            return env[bit]
        if bit not in comb:
            return 0
        cell, lane = comb[bit]
        if cell.name not in memo:
            memo[cell.name] = _cell_value(cell, num)
        return (memo[cell.name] >> lane) & 1

    def num(bits: tuple[Bit, ...]) -> int:
        return sum(get(b) << k for k, b in enumerate(bits))

    return num


def _width(module: Module, name: str) -> int:
    src = module.ports[name] if name in module.ports else module.netnames[name]
    return len(src.bits)


def _combos(module: Module, names: tuple[str, ...]):
    for combo in itertools.product(*(range(1 << _width(module, n)) for n in names)):
        yield dict(zip(names, combo))


def _outputs(module: Module, values: dict[str, int], ports: tuple[str, ...]):
    ev = _evaluator(module, values)
    return {p: ev(module.ports[p].bits) for p in ports}


def _d_of(module: Module, reg: str) -> tuple[Bit, ...]:
    """The ``D`` bits loading register ``reg`` (``x`` for a bit no flop
    drives)."""
    bits = module.netnames[reg].bits
    d: dict[int, Bit] = {}
    for c in module.cells.values():
        q = c.connections.get("Q")
        if q is None:
            continue
        for k, b in enumerate(bits):
            if b in q:
                d[k] = c.connections["D"][q.index(b)]
    return tuple(d.get(k, "x") for k in range(len(bits)))


def _next_state(module: Module, values: dict[str, int], regs: tuple[str, ...]):
    ev = _evaluator(module, values)
    return {r: ev(_d_of(module, r)) for r in regs}


def _no_lowering_warnings(caplog: pytest.LogCaptureFixture) -> None:
    msgs = [r.getMessage() for r in caplog.records if r.name == _SLANG_LOG]
    assert not [m for m in msgs if "not lowered" in m], msgs


# --- 1. a select of a loop variable reads the iteration value --------------

_LOOPS = """module m (input [3:0] d, input [2:0] s, output [7:0] o, output [7:0] p,
          output [7:0] q, output [15:0] g, output [7:0] h, output [7:0] c);
    logic arr [2][4];
    logic [1:0] pr [8];
    logic ar [8];
    logic cm [8];
    always_comb for (int r = 0; r < 2; r++)
        for (int k = 0; k < 4; k++) arr[r][k] = r[0] ^ k[0] ^ d[k];
    always_comb for (int k = 0; k < 8; k++) pr[k] = k[2:1] ^ d[1:0];
    always_comb for (int k = 0; k < 8; k++)
        ar[k] = ((k * 3 + 1) >> 1) == {1'b0, s} ? d[0] : d[1];
    always_comb for (int k = 0; k < 8; k++)
        if (k < 3 || k[2]) cm[k] = d[2]; else cm[k] = d[3] & (k > s);
    for (genvar gi = 0; gi < 8; gi++) begin : gl
        assign g[2*gi +: 2] = gi[2:1] + gi[0];
        assign h[gi] = gi[1] ? d[gi[1:0]] : (gi == s);
    end
    assign o = {arr[1][3], arr[1][2], arr[1][1], arr[1][0],
                arr[0][3], arr[0][2], arr[0][1], arr[0][0]};
    assign p = {pr[3], pr[2], pr[1], pr[0]};
    assign q = {ar[7], ar[6], ar[5], ar[4], ar[3], ar[2], ar[1], ar[0]};
    assign c = {cm[7], cm[6], cm[5], cm[4], cm[3], cm[2], cm[1], cm[0]};
endmodule
"""

_LOOP_OUTS = ("o", "p", "q", "g", "h", "c")


def _loops_expected(d: int, s: int) -> dict[str, int]:
    """``_LOOPS`` by the SystemVerilog semantics."""

    def bit(v: int, k: int) -> int:
        return (v >> k) & 1

    o = sum(
        (bit(r, 0) ^ bit(k, 0) ^ bit(d, k)) << (4 * r + k)
        for r in (0, 1)
        for k in range(4)
    )
    p = sum((((k >> 1) & 3) ^ (d & 3)) << (2 * k) for k in range(4)) & 0xFF
    q = sum(
        (bit(d, 0) if ((k * 3 + 1) >> 1) == s else bit(d, 1)) << k for k in range(8)
    )
    g = sum(((((k >> 1) & 3) + (k & 1)) & 3) << (2 * k) for k in range(8))
    h = sum((bit(d, k & 3) if bit(k, 1) else int(k == s)) << k for k in range(8))
    c = sum(
        (bit(d, 2) if (k < 3 or bit(k, 2)) else bit(d, 3) & int(k > s)) << k
        for k in range(8)
    )
    return {"o": o, "p": p, "q": q, "g": g, "h": h, "c": c}


def test_loop_variable_selects_read_the_iteration_value(tmp_path: Path) -> None:
    """``r[0]``, ``k[2:1]``, ``gi[1:0]`` read the iteration value's bits in
    procedural and generate loops, in arithmetic, comparisons, indices and
    conditions (every one read 0: ``arr[r][k] = r[0] ^ k[0]`` was
    ``$xor('0', '0')``). Every output matches the SystemVerilog value for
    every input, and the Yosys netlist's."""
    module = _slang(tmp_path, _LOOPS)
    y_mod = _yosys(tmp_path, _LOOPS) if _HAVE_YOSYS else None
    for values in _combos(module, ("d", "s")):
        got = _outputs(module, values, _LOOP_OUTS)
        assert got == _loops_expected(values["d"], values["s"]), values
        if y_mod is not None:
            assert _outputs(y_mod, values, _LOOP_OUTS) == got, values


def test_loop_variable_conditions_fold(tmp_path: Path) -> None:
    """``if (k < 2 || k[2])`` in an unrolled body is decided per iteration,
    as Yosys' frontend folds it: the dead arm's fan-in is not in the
    netlist (``cm[0]`` takes ``d[2]`` and nothing else)."""
    module = _slang(tmp_path, _LOOPS)
    c0 = module.ports["c"].bits[0]
    assert c0 == module.ports["d"].bits[2]
    assert not [x for x in module.cells.values() if x.type in ("$lt", "$logic_or")]


_PARAMS = """module m (input [1:0] i, input j, output [1:0] o, output [1:0] p,
          output [1:0] r, output [1:0] s, output [2:0] t);
    localparam logic [1:0] P [4] = '{2'd3, 2'd1, 2'd2, 2'd0};
    localparam logic [1:0] D [3:0] = '{2'd3, 2'd1, 2'd2, 2'd0};
    localparam logic [7:0] Q = 8'b10_01_11_00;
    localparam logic [2:0] M [2][2] = '{'{3'd1, 3'd2}, '{3'd5, 3'd6}};
    assign o = P[1];
    assign p = P[i];
    assign r = Q[5:4] ^ Q[i];
    assign s = D[i];
    assign t = M[i[0]][j];
endmodule
"""


def test_parameter_selects_read_the_value(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A select of a parameter reads its value: ``Q[5:4]`` read ``Q``'s
    undriven placeholder, and an element of an unpacked parameter array
    (``P[1]``, ``P[i]``, ``M[i][j]``, any range) one too or an opaque
    driver. Slang only: Yosys does not parse unpacked parameter arrays."""
    with caplog.at_level(logging.WARNING, logger=_SLANG_LOG):
        module = _slang(tmp_path, _PARAMS)
    _no_lowering_warnings(caplog)
    p_vals, d_vals, q_val, m_vals = (
        [3, 1, 2, 0],
        [0, 2, 1, 3],
        0b10011100,
        [[1, 2], [5, 6]],
    )
    for values in _combos(module, ("i", "j")):
        i, j = values["i"], values["j"]
        assert _outputs(module, values, ("o", "p", "r", "s", "t")) == {
            "o": 1,
            "p": p_vals[i],
            "r": ((q_val >> 4) & 3) ^ ((q_val >> i) & 1),
            "s": d_vals[i],
            "t": m_vals[i & 1][j],
        }, values


_LOOP_FF = """module m (input clk, input [3:0] d, input [1:0] s, output reg [7:0] q,
          output [7:0] o, output [3:0] w);
    logic [1:0] t [4];
    logic [3:0] u;
    always_ff @(posedge clk)
        for (int k = 0; k < 8; k++) begin
            q[k] <= k[0] ? d[k[2:1]] : (k[1:0] == s);
            if (k[2] && k != 7) q[k] <= ~d[k[1:0]];
        end
    always_comb
        for (int i = 0; i < 4; i++)
            case (i[1:0])
                2'd0: t[i] = d[1:0];
                2'd1: t[i] = {i[0], d[2]};
                default: t[i] = i[1:0] - s;
            endcase
    always_comb for (int i = 3; i >= 0; i--) u[i[1:0]] = d[3 - i] ^ i[1];
    assign o = {t[3], t[2], t[1], t[0]};
    assign w = u;
endmodule
"""


def test_loop_variable_selects_in_clocked_and_case_bodies(tmp_path: Path) -> None:
    """The same in a clocked loop (an index, a compare, an ``if``), a
    ``case`` on a loop-variable part select and a down-counting loop
    indexing its target with ``i[1:0]``: next state and outputs match the
    SystemVerilog value, and Yosys'."""
    module = _slang(tmp_path, _LOOP_FF)
    y_mod = _yosys(tmp_path, _LOOP_FF) if _HAVE_YOSYS else None

    def bit(v: int, k: int) -> int:
        return (v >> k) & 1

    for values in _combos(module, ("d", "s")):
        d, s = values["d"], values["s"]
        q = 0
        for k in range(8):
            v = bit(d, (k >> 1) & 3) if k & 1 else int((k & 3) == s)
            if bit(k, 2) and k != 7:
                v = 1 - bit(d, k & 3)
            q |= v << k
        t = [d & 3, (1 << 1) | bit(d, 2), (2 - s) & 3, (3 - s) & 3]
        want = {
            "o": sum(v << (2 * k) for k, v in enumerate(t)),
            "w": sum((bit(d, 3 - i) ^ bit(i, 1)) << i for i in range(4)),
        }
        assert _next_state(module, values, ("q",)) == {"q": q}, values
        assert _outputs(module, values, ("o", "w")) == want, values
        if y_mod is not None:
            assert _next_state(y_mod, values, ("q",)) == {"q": q}, values
            assert _outputs(y_mod, values, ("o", "w")) == want, values


_LOOP_CDC = """module m (input clk_a, input clk_b, input a, input b, output logic q);
    logic ra, rb;
    logic pick [2];
    always_ff @(posedge clk_a) ra <= a;
    always_ff @(posedge clk_b) rb <= b;
    always_comb for (int k = 0; k < 2; k++) pick[k] = k[0] ? ra : rb;
    always_ff @(posedge clk_b) q <= pick[1];
endmodule
"""


def test_loop_variable_select_decides_the_crossing(tmp_path: Path) -> None:
    """``pick[1]`` is ``ra`` (``k[0]`` is 1): a clk_a source reaches the
    clk_b capture. With ``k[0]`` read as 0 it was ``rb``, so the crossing
    was missed (CDC-014 / an extra CDC-011 instead of the CDC-001)."""
    assert _same_verdict(tmp_path, _LOOP_CDC) == (2, {"CDC-001": 1, "CDC-011": 1})


# --- 2. a combinational write at a run-time index --------------------------

_COMB_INDEX = """module m (input [1:0] i, input [1:0] k, input [1:0] j, input [2:0] p,
          input [3:0] d, output [7:0] o1, output [7:0] o2, output [15:0] o3,
          output [7:0] o4, output [7:0] o5, output [11:0] o6);
    logic [1:0] ga [0:3];        // ascending
    logic [1:0] gd [3:0];        // descending
    logic [0:3] gb [7:4];        // offset, ascending element
    logic [1:0] g2 [0:1][0:3];   // two dimensions
    logic [7:0] pv;
    logic [11:0] pw;
    always @* begin
        ga[0] = 2'b01; ga[1] = 2'b10; ga[2] = 2'b11; ga[3] = 2'b00;
        ga[i] = d[1:0];
        gd[0] = 2'b01; gd[1] = 2'b10; gd[2] = 2'b11; gd[3] = 2'b00;
        gd[i] = d[3:2];
        gb[4] = 4'h1; gb[5] = 4'h2; gb[6] = 4'h3; gb[7] = 4'h4;
        gb[{2'b11, i[0]}][j] = d[0];
        gb[3'd4 + i][k] = d[1];
        for (int r = 0; r < 2; r++)
            for (int c = 0; c < 4; c++) g2[r][c] = r[0] ? c[1:0] : ~c[1:0];
        g2[i[0]][k] = d[1:0];
        pv = {d, d};
        pv[p] = 1'b0;
        pv[i +: 2] = k;
        pw = 12'h5a5;
        pw[p + 4'd2 -: 3] = d[2:0];
    end
    assign o1 = {ga[3], ga[2], ga[1], ga[0]};
    assign o2 = {gd[3], gd[2], gd[1], gd[0]};
    assign o3 = {gb[7], gb[6], gb[5], gb[4]};
    assign o4 = {g2[1][3], g2[1][2], g2[1][1], g2[1][0]};
    assign o5 = {g2[0][3], g2[0][2], g2[0][1], g2[0][0]};
    assign o6 = pw ^ {pv, 4'h0};
endmodule
"""

_COMB_OUTS = ("o1", "o2", "o3", "o4", "o5", "o6")


def _set(word: int, pos: int, width: int, value: int) -> int:
    mask = ((1 << width) - 1) << pos
    return (word & ~mask) | ((value << pos) & mask)


def _comb_index_expected(i: int, k: int, j: int, p: int, d: int) -> dict[str, int]:
    """``_COMB_INDEX`` by the SystemVerilog semantics."""
    ga = [1, 2, 3, 0]
    ga[i] = d & 3
    gd = [1, 2, 3, 0]
    gd[i] = d >> 2
    # ``logic [0:3]`` elements: index n is bit 3 - n.
    gb = {4: 1, 5: 2, 6: 3, 7: 4}
    e = 6 + (i & 1)
    gb[e] = _set(gb[e], 3 - j, 1, d & 1)
    gb[4 + i] = _set(gb[4 + i], 3 - k, 1, (d >> 1) & 1)
    g2 = [[~c & 3 for c in range(4)], [c for c in range(4)]]
    g2[i & 1][k] = d & 3
    pv = _set((d << 4) | d, p, 1, 0)
    pv = _set(pv, i, 2, k)
    pw = _set(0x5A5, p, 3, d & 7)  # bits p + 2 down to p
    return {
        "o1": sum(v << (2 * n) for n, v in enumerate(ga)),
        "o2": sum(v << (2 * n) for n, v in enumerate(gd)),
        "o3": sum(gb[4 + n] << (4 * n) for n in range(4)),
        "o4": sum(v << (2 * n) for n, v in enumerate(g2[1])),
        "o5": sum(v << (2 * n) for n, v in enumerate(g2[0])),
        "o6": pw ^ (pv << 4),
    }


def test_comb_runtime_index_write_reaches_the_indexed_element(tmp_path: Path) -> None:
    """``g[i] = d`` writes element ``i`` of an ascending, descending,
    offset or two-dimensional register array (an ascending ``[0:3]`` array
    took ``d`` in element ``3 - i``), a run-time bit of an element too, and
    ``pv[p] = 0`` clears the bit (``old | data`` kept it set). Every output
    matches the SystemVerilog value, and the Yosys netlist's."""
    module = _slang(tmp_path, _COMB_INDEX)
    y_mod = _yosys(tmp_path, _COMB_INDEX) if _HAVE_YOSYS else None
    # Every index and position; each bit of ``d`` both ways.
    for sel, d in itertools.product(_combos(module, ("i", "k", "j", "p")), (6, 9)):
        values = {**sel, "d": d}
        got = _outputs(module, values, _COMB_OUTS)
        assert got == _comb_index_expected(**values), values
        if y_mod is not None:
            assert _outputs(y_mod, values, _COMB_OUTS) == got, values


_LANES = """module m (input [1:0] i, input [3:0] d, input [3:0] e, output [15:0] o);
    logic [3:0] g [0:3];
    always_comb begin
        g[0] = e; g[1] = e; g[2] = e; g[3] = e;
        g[i] = d;
    end
    assign o = {g[3], g[2], g[1], g[0]};
endmodule
"""


def test_comb_runtime_index_write_is_lane_aligned(tmp_path: Path) -> None:
    """Each element is one ``$mux(S = i == k, A = old, B = d)``: bit ``n``
    of every element reads ``d[n]`` and ``e[n]`` only, as in Yosys' per
    element ``$pmux`` (a ``$shl`` of the whole array mixed the lanes)."""
    module = _slang(tmp_path, _LANES)
    assert "$shl" not in {c.type for c in module.cells.values()}
    drivers = {b: c for c in module.cells.values() for b in c.connections.get("Y", ())}
    o = module.ports["o"].bits
    for k in range(4):
        mux = drivers[o[4 * k]]
        assert mux.type == "$mux"
        assert mux.connections["A"] == module.ports["e"].bits
        assert mux.connections["B"] == module.ports["d"].bits
        assert tuple(mux.connections["Y"]) == o[4 * k : 4 * k + 4]
        sel = drivers[mux.connections["S"][0]]
        assert sel.type == ("$logic_not" if k == 0 else "$eq")


_COMB_CDC = """module m (input clk_a, input clk_b, input [1:0] i, input [3:0] b,
          output logic [3:0] q);
    logic [3:0] bin, gray;
    logic [3:0] g [0:3];
    logic [3:0] s1, s2;
    always_ff @(posedge clk_a) begin bin <= bin + 4'd1; gray <= bin ^ (bin >> 1); end
    always_comb begin
        g[0] = b; g[1] = b; g[2] = b; g[3] = b;
        g[i] = gray;
    end
    always_ff @(posedge clk_b) begin s1 <= g[2]; s2 <= s1; q <= s2; end
endmodule
"""


def test_comb_runtime_index_write_verdict(tmp_path: Path) -> None:
    """A bus through a run-time-index array write to a 2FF chain: the
    same findings as Yosys."""
    assert _same_verdict(tmp_path, _COMB_CDC) == (3, {"CDC-004": 2, "CDC-011": 2})


_PARTIAL = """module m (input [2:0] p, input [2:0] d, output reg [11:0] pw);
    always @* begin
        pw = 12'h5a5;
        pw[p -: 3] = d;
    end
endmodule
"""


def test_partly_out_of_range_write_keeps_the_in_range_bits(tmp_path: Path) -> None:
    """``pw[p -: 3]`` with ``p`` below 2 reaches below bit 0: the bits in
    range are written, by the LRM. Yosys computes the position unsigned
    and writes nothing; this frontend does not copy that (the fan-in is
    the same either way)."""
    module = _slang(tmp_path, _PARTIAL)
    for values in _combos(module, ("p", "d")):
        p, d = values["p"], values["d"]
        want = 0x5A5
        for n in range(3):
            if 0 <= p - 2 + n:
                want = _set(want, p - 2 + n, 1, (d >> n) & 1)
        assert _outputs(module, values, ("pw",)) == {"pw": want}, values


# --- 3. run-time bits of words, words in concatenations --------------------

_CLOCKED = """module m (input clk, input rst_n, input we, input [1:0] i,
          input [1:0] j, input [1:0] d, input e, input [1:0] ra,
          output [3:0] om, output [1:0] oc, output [15:0] orw,
          output [7:0] orc, output reg [3:0] v, output reg [5:0] w,
          output reg t, output reg u);
    logic [3:0] mw [0:3];   // memory: a run-time bit of a word written
    logic [1:0] mc [0:3];   // memory: a word in a concatenation target
    logic [3:0] rw [0:3];   // registers: a run-time bit of a word written
    logic [1:0] rc [0:3];   // registers: a word in a concatenation target
    always_ff @(posedge clk) if (we) mw[i][j] <= d[0];
    always_ff @(posedge clk) {mc[i], t} <= {d, e};
    always_ff @(posedge clk or negedge rst_n)
        if (!rst_n) begin
            rw[0] <= '0; rw[1] <= '0; rw[2] <= '0; rw[3] <= '0;
            rc[0] <= '0; rc[1] <= '0; rc[2] <= '0; rc[3] <= '0;
            u <= 1'b0;
        end else begin
            rw[i][j] <= d[1];
            {rc[i], u} <= {d, e};
        end
    always_ff @(posedge clk) if (we) v[j] <= e;
    always_ff @(posedge clk) w[j +: 2] <= d;
    assign om = mw[ra];
    assign oc = mc[ra];
    assign orw = {rw[3], rw[2], rw[1], rw[0]};
    assign orc = {rc[3], rc[2], rc[1], rc[0]};
endmodule
"""

_CLOCKED_IN = ("we", "i", "j", "d", "e")
_CLOCKED_REGS = ("v", "w", "t", "u", *(f"rw[{k}]" for k in range(4)))
_CLOCKED_REGS += tuple(f"rc[{k}]" for k in range(4))


def _write_ports(module: Module) -> dict[str, Cell]:
    return {
        c.parameters["MEMID"]: c
        for c in module.cells.values()
        if c.type.startswith("$memwr")
    }


def _port_effect(module: Module, cell: Cell, values: dict[str, int]):
    """``(address, enabled bits, data under them)`` of a write port, the
    address ``None`` when no bit is enabled."""
    ev = _evaluator(module, values)
    en = ev(cell.connections["EN"])
    addr = ev(cell.connections["ADDR"]) if en else None
    return addr, en, ev(cell.connections["DATA"]) & en


def test_runtime_bits_and_concatenated_words_are_lowered(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """``mw[i][j] <= b`` is a write port with the data and a one-hot bit
    enable shifted to ``j``; ``{mc[i], t} <= {d, e}`` a write port plus a
    register; ``rw[i][j] <= b`` and ``{rc[i], u} <= ..`` on register
    arrays one conditional write per element; ``v[j] <= e`` and ``w[j +:
    2] <= d`` writes of a run-time part of a vector. Each was reported and
    dropped. Ports and next states match the SystemVerilog semantics, and
    Yosys'."""
    with caplog.at_level(logging.WARNING, logger=_SLANG_LOG):
        module = _slang(tmp_path, _CLOCKED)
    _no_lowering_warnings(caplog)
    ports = _write_ports(module)
    assert set(ports) == {"\\mw", "\\mc"}
    y_mod = _yosys(tmp_path, _CLOCKED) if _HAVE_YOSYS else None
    y_ports = _write_ports(y_mod) if y_mod is not None else {}
    state = {"v": 0b1010, "w": 0b110011, **{f"rw[{k}]": 5 * k for k in range(4)}}
    state.update({f"rc[{k}]": k for k in range(4)}, t=1, u=0)
    for values in _combos(module, _CLOCKED_IN):
        we, i, j, d, e = (values[n] for n in _CLOCKED_IN)
        env = {**values, **state}
        assert _port_effect(module, ports["\\mw"], env) == (
            i if we else None,
            (1 << j) if we else 0,
            ((d & 1) << j) if we else 0,
        ), values
        assert _port_effect(module, ports["\\mc"], env) == (i, 3, d), values
        want = dict(state)
        want["v"] = _set(state["v"], j, 1, e) if we else state["v"]
        want["w"] = _set(state["w"], j, 2, d)
        want["t"] = want["u"] = e
        want[f"rw[{i}]"] = _set(state[f"rw[{i}]"], j, 1, d >> 1)
        want[f"rc[{i}]"] = d
        assert _next_state(module, env, _CLOCKED_REGS) == want, values
        if y_mod is not None:
            for memid in ports:
                assert _port_effect(y_mod, y_ports[memid], env) == _port_effect(
                    module, ports[memid], env
                ), (memid, values)
            assert _next_state(y_mod, env, _CLOCKED_REGS) == want, values


_CLOCKED_CDC = """module m (input clk_a, input clk_b, input [1:0] i, input [1:0] j,
          input [1:0] k, input a, output [3:0] q, output reg [1:0] p);
    logic ra;
    logic [3:0] mw [0:3];
    logic [1:0] mc [0:3];
    logic t;
    always_ff @(posedge clk_a) ra <= a;
    always_ff @(posedge clk_b) mw[i][j] <= ra;
    always_ff @(posedge clk_b) {mc[i], t} <= {ra, ra, 1'b0};
    always_ff @(posedge clk_b) p <= mc[k] ^ {t, t};
    assign q = mw[k];
endmodule
"""


def test_runtime_bits_and_concatenated_words_verdict(tmp_path: Path) -> None:
    """A clk_a source written into clk_b memories through a run-time bit
    and a concatenation: the crossings into both write ports are found, as
    Yosys finds them (they were missing, the writes dropped)."""
    assert _same_verdict(tmp_path, _CLOCKED_CDC) == (
        7,
        {"CDC-001": 2, "CDC-004": 4, "CDC-011": 4},
    )


_MORE_WRITES = """module m (input clk, input [1:0] i, input [2:0] x, input e,
          output reg [3:0] y, output reg c, output reg [3:0] q);
    reg [3:0] t;
    always_comb begin
        y = 4'b1001;
        {c, y[i]} = x[1:0];
    end
    always_ff @(posedge clk) begin
        t = {x, e};
        t[i] = ~e;
        q <= t;
    end
endmodule
"""


def test_comb_concatenation_and_blocking_clocked_part_writes(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A combinational ``{c, y[i]} = ..`` writes each part (it was
    reported and dropped); a blocking ``t[i] = ..`` in a clocked block is
    seen by the reads after it. Yosys 0.64 drops a run-time bit of a
    vector inside a concatenation target without a word (``y`` stays
    ``4'b1001``, and a clocked ``{c, y[i]} <= ..`` gets no ``y``
    register), so ``y`` is checked against the LRM only."""
    with caplog.at_level(logging.WARNING, logger=_SLANG_LOG):
        module = _slang(tmp_path, _MORE_WRITES)
    _no_lowering_warnings(caplog)
    y_mod = _yosys(tmp_path, _MORE_WRITES) if _HAVE_YOSYS else None
    for values in _combos(module, ("i", "x", "e")):
        i, x, e = values["i"], values["x"], values["e"]
        want = {"y": _set(0b1001, i, 1, x & 1), "c": (x >> 1) & 1}
        assert _outputs(module, values, ("y", "c")) == want, values
        q = _set((x << 1) | e, i, 1, 1 - e)
        assert _next_state(module, values, ("q",)) == {"q": q}, values
        if y_mod is not None:
            assert _outputs(y_mod, values, ("c",)) == {"c": want["c"]}, values
            assert _next_state(y_mod, values, ("q",)) == {"q": q}, values


# --- 4. a negated sync select reads as proc builds it ----------------------

_NEGATED = """module m (input clk, input ra, input rb, input a, input b, input [1:0] d,
          output logic [1:0] q, output logic [1:0] q3, output logic [1:0] q4,
          output logic [1:0] q5);
    always_ff @(posedge clk)
        if (!ra)      q <= '0;
        else if (!rb) q <= '0;
        else          q <= d;
    always_ff @(posedge clk) if (!(a & b)) q3 <= '0; else q3 <= d;
    always_ff @(posedge clk) if (b) q4 <= d; else if (~a) q4 <= '0;
    always_ff @(posedge clk) begin
        q5 <= d;
        if (!a) q5[0] <= 1'b1;
    end
endmodule
"""


def _d_tree(module: Module, reg: str, lane: int = 0) -> object:
    """The ``$mux`` tree on ``reg[lane]``'s ``D`` as ``(S, A, B)`` tuples
    down to port / register bits and constants."""
    names: dict[Bit, str] = {}
    for p, port in module.ports.items():
        for k, b in enumerate(port.bits):
            names.setdefault(b, f"{p}[{k}]")
    drivers = {
        b: (c, k)
        for c in module.cells.values()
        if "Q" not in c.connections
        for k, b in enumerate(c.connections.get("Y", ()))
    }

    def tree(bit: Bit) -> object:
        if isinstance(bit, str) or bit in names:
            return names.get(bit, bit)
        cell, k = drivers[bit]
        c = cell.connections
        if cell.type != "$mux":
            return cell.type
        return (tree(c["S"][0]), tree(c["A"][k]), tree(c["B"][k]))

    return tree(_d_of(module, reg)[lane])


def test_negated_select_swaps_the_mux_legs(tmp_path: Path) -> None:
    """``else if (!rb)`` / ``if (~a)`` / ``if (!(a & b))`` select on the
    operand with the legs swapped, no ``$logic_not``: the mux trees equal
    Yosys'. The first reset of ``q`` still folds into an ``$sdff``."""
    module = _slang(tmp_path, _NEGATED)
    assert "$logic_not" not in {c.type for c in module.cells.values()}
    assert _d_tree(module, "q") == ("rb[0]", "0", "d[0]")
    assert _d_tree(module, "q3") == ("$and", "0", "d[0]")
    assert _d_tree(module, "q4") == ("b[0]", ("a[0]", "0", "q4[0]"), "d[0]")
    assert _d_tree(module, "q5") == ("a[0]", "1", "d[0]")
    if _HAVE_YOSYS:
        y_mod = _yosys(tmp_path, _NEGATED)
        for reg in ("q3", "q4", "q5"):
            assert _d_tree(y_mod, reg) == _d_tree(module, reg), reg
        # Yosys keeps ``!ra`` on D too: the same tree under it.
        assert _d_tree(y_mod, "q") == ("ra[0]", "0", ("rb[0]", "0", "d[0]"))


_VECTOR_SELECT = """module m (input clk, input [1:0] v, input a, input [1:0] d,
          output logic [1:0] q, output logic [1:0] r, output logic [1:0] w);
    always_ff @(posedge clk) if (!v) q <= '0; else q <= d;
    always_ff @(posedge clk) if (&v) r <= '0; else r <= d;
    always_ff @(posedge clk) if (~a) w <= '0; else w <= d;
endmodule
"""


def test_vector_condition_is_no_sync_reset_pin(tmp_path: Path) -> None:
    """``if (!v)`` / ``if (&v)`` of a vector is a ``$logic_not`` /
    ``$reduce_and`` select on ``D``, as ``proc`` builds it; it was an
    ``$sdff`` with ``v`` on a two-bit ``SRST`` (``&v`` read as an
    active-low ``v``). A one-bit ``~a`` still folds into ``SRST``."""
    module = _slang(tmp_path, _VECTOR_SELECT)
    srst = [c.connections["SRST"] for c in module.cells.values() if c.type == "$sdff"]
    assert srst == [module.ports["a"].bits]
    assert _d_tree(module, "q") == ("$logic_not", "d[0]", "0")
    assert _d_tree(module, "r") == ("$reduce_and", "d[0]", "0")
    if _HAVE_YOSYS:
        y_mod = _yosys(tmp_path, _VECTOR_SELECT)
        for reg in ("q", "r"):
            assert _d_tree(y_mod, reg) == _d_tree(module, reg), reg


_NEGATED_MEM = """module m (input clk, input nwe, input [1:0] wa, input [1:0] ra,
          input [3:0] d, output [3:0] o);
    logic [3:0] mem [0:3];
    always_ff @(posedge clk) if (!nwe) mem[wa] <= d;
    assign o = mem[ra];
endmodule
"""


def test_negated_select_enables_a_memory_port(tmp_path: Path) -> None:
    """A write port under ``if (!nwe)`` is enabled while ``nwe`` is low."""
    module = _slang(tmp_path, _NEGATED_MEM)
    (port,) = _write_ports(module).values()
    for nwe in (0, 1):
        ev = _evaluator(module, {"nwe": nwe})
        assert ev(port.connections["EN"]) == (0 if nwe else 0xF)


# --- 8. a memory's MEMID is its declaration's name -------------------------

_MEMID = """module m (input clk, input [1:0] wa, input [1:0] ra, input [3:0] d,
          output [3:0] o, output [3:0] p);
    logic [3:0] mem [0:3];
    for (genvar g = 0; g < 2; g++) begin : gw
        always_ff @(posedge clk) if (wa[0] == g) mem[wa] <= d ^ g;
    end
    if (1) begin : gr
        assign o = mem[ra];
        logic [3:0] lm [0:3];
        always_ff @(posedge clk) lm[wa] <= d;
        assign p = lm[ra];
    end
endmodule
"""


def test_memid_is_the_declaration_scope(tmp_path: Path) -> None:
    """Ports of ``mem`` in ``gw[0]``, ``gw[1]`` and ``gr`` share ``\\mem``
    (they were ``\\gw[0].mem``, ``\\gw[1].mem``, ``\\gr.mem``: three
    memories); ``gr.lm`` keeps its block's name. Write ports of one memory
    are numbered apart."""
    module = _slang(tmp_path, _MEMID)
    ports = [c for c in module.cells.values() if c.type.startswith("$mem")]

    def memids(mod: Module) -> Counter:
        return Counter(
            (c.type.startswith("$memwr"), c.parameters["MEMID"])
            for c in mod.cells.values()
            if c.type.startswith("$mem")
        )

    assert memids(module) == Counter(
        {
            (True, "\\mem"): 2,
            (False, "\\mem"): 1,
            (True, "\\gr.lm"): 1,
            (False, "\\gr.lm"): 1,
        }
    )
    portids = sorted(
        c.parameters["PORTID"]
        for c in ports
        if c.type == "$memwr_v2" and c.parameters["MEMID"] == "\\mem"
    )
    assert portids == ["0" * 32, "0" * 31 + "1"]
    if _HAVE_YOSYS:
        assert memids(_yosys(tmp_path, _MEMID)) == memids(module)


# --- 9. netname differences kept on purpose --------------------------------

_NAMES = """module m (input clk_a, input clk_b, input rst_n, input [1:0] i,
          input [3:0] d, output [3:0] o, output logic [3:0] p);
    logic [3:0] da;
    logic [3:0] off [4:7];
    logic [3:0] tmp [0:1];
    always_ff @(posedge clk_a) da <= d;
    always_ff @(posedge clk_b or negedge rst_n)
        if (!rst_n) begin off[4] <= '0; off[5] <= '0; off[6] <= '0; off[7] <= '0; end
        else off[3'd4 + i] <= da;
    always_ff @(posedge clk_b) begin
        tmp[0] = da; tmp[1] = ~da;
        p <= tmp[0] ^ tmp[1];
    end
    assign o = off[3'd4 + i];
endmodule
"""


def test_netname_differences_from_yosys_are_pinned(tmp_path: Path) -> None:
    """Two netname differences from Yosys are kept, as neither name is
    wrong and matching Yosys would make the netlist worse:

    - ``off [4:7]`` written at a run-time index is ``off[4]`` .. ``off[7]``
      here. Yosys ``mem2reg`` makes registers ``off[0]`` .. ``off[7]``:
      ``off[0..3]`` are flops no index reaches, loaded from ``da`` under
      the address compare, so Yosys reports crossings into four registers
      that do not exist in the design.
    - ``tmp``, only blocking-written in a clocked block, keeps its element
      netnames here (its flops are dead; the analysis prunes them).
      Yosys' ``opt_clean`` drops the netnames with the flops, as it does
      for a plain blocking temporary, so an SDC or waiver naming ``tmp[0]``
      resolves only on the slang path.
    """
    module = _slang(tmp_path, _NAMES)
    names = {n for n in module.netnames if n.startswith(("off", "tmp"))}
    assert names == {f"off[{k}]" for k in range(4, 8)} | {"tmp[0]", "tmp[1]"}
    slang_v = _verdict(_lint(tmp_path, _NAMES, "slang"))
    assert slang_v == (10, {"CDC-004": 10, "CDC-011": 2})
    if _HAVE_YOSYS:
        y_names = {n for n in _yosys(tmp_path, _NAMES).netnames}
        assert {n for n in y_names if n.startswith(("off", "tmp"))} == {
            f"off[{k}]" for k in range(8)
        }
        y_report = _lint(tmp_path, _NAMES, "yosys")
        # The phantom registers' crossings, and nothing else, are extra.
        assert _verdict(y_report)[0] - slang_v[0] == 12
