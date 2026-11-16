"""slang frontend: the Yosys-parity gaps left after rtl-buddy-cdc#377
(rtl-buddy-cdc#384).

One section per item of the issue: a minimal design the slang path
lowered differently from Yosys, the netlist shape it lowers to now, and,
where yosys is on PATH, the same design through the Yosys frontend with
the cells / verdict compared. CI's pytest job has no yosys, so there the
slang-side assertions stand alone (they pin the shape the Yosys
comparison was made against locally).
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
_SLANG_LOG = "rtl_buddy_cdc.frontends.slang"


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


def _yosys(tmp_path: Path, src: str, top: str = "m", extra: str = "") -> Module:
    """The ``lint`` recipe (plus ``extra`` passes after ``proc``), from a
    script file: CI's yosys 0.52 splits a ``-p`` script at commas."""
    sv = _write(tmp_path, src, "y.sv")
    out = tmp_path / "y.json"
    script = tmp_path / "y.ys"
    script.write_text(
        f"read_verilog -sv {sv}\nhierarchy -top {top}\nproc\n{extra}\nflatten\n"
        f"opt_clean\nwrite_json {out}\n"
    )
    subprocess.run(["yosys", "-q", "-s", str(script)], check=True)
    return netlist.load(out)


def _cells(module: Module, cell_type: str) -> list[Cell]:
    return [c for c in module.cells.values() if c.type == cell_type]


def _warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]


_SDC = """create_clock -name clk_a -period 10 [get_ports clk_a]
create_clock -name clk_b -period 7 [get_ports clk_b]
set_clock_groups -asynchronous -group {clk_a} -group {clk_b}
set_input_delay -clock clk_a 1 [get_ports d]
"""


def _lint(
    tmp_path: Path,
    src: str,
    frontend: str,
    *,
    sdc: str = _SDC,
    waivers: str | None = None,
) -> dict:
    sv = _write(tmp_path, src, f"{frontend}.sv")
    sdc_path = tmp_path / "m.sdc"
    sdc_path.write_text(sdc)
    out = tmp_path / f"{frontend}.json"
    args = ["lint", "--top", "m", "--sdc", str(sdc_path), "--frontend", frontend]
    if waivers is not None:
        swl = tmp_path / "m.swl"
        swl.write_text(waivers)
        args += ["--waivers", str(swl)]
    args += ["--format", "json", "--output", str(out), str(sv)]
    result = CliRunner().invoke(app, args)
    assert result.exit_code in (0, 1), result.output
    return json.loads(out.read_text())


def _verdict(report: dict) -> tuple[int, dict[str, int], dict[str, int]]:
    """``(crossings, live findings per rule, suppressed findings per rule)``."""
    live = Counter(v["rule_id"] for v in report["violations"])
    gone = Counter(s["rule_id"] for s in report["suppressed"])
    return report["summary"]["crossings"], dict(live), dict(gone)


def _same_verdict(tmp_path: Path, src: str, **kw) -> tuple:
    """The slang ``lint`` verdict, checked against Yosys' when available."""
    slang = _verdict(_lint(tmp_path, src, "slang", **kw))
    if _HAVE_YOSYS:
        assert _verdict(_lint(tmp_path, src, "yosys", **kw)) == slang
    return slang


def _netname_bits(module: Module, name: str) -> tuple[Bit, ...]:
    return module.netnames[name].bits


def _driver(module: Module, bit: Bit) -> Cell | None:
    for c in module.cells.values():
        for pin in ("Y", "Q"):
            if bit in c.connections.get(pin, ()):
                return c
    return None


def _int(vals: list[int]) -> int:
    return sum(v << i for i, v in enumerate(vals))


def _comb_value(cell: Cell, get) -> list[int]:
    """Output bits of one combinational cell (unsigned, ``x`` read as 0)."""
    conn = cell.connections
    width = len(conn["Y"])
    a = [get(b) for b in conn.get("A", ())]
    b = [get(x) for x in conn.get("B", ())]
    t = cell.type
    mask = (1 << width) - 1
    if t == "$mux":
        return b if get(conn["S"][0]) else a
    if t == "$pmux":
        s = [get(x) for x in conn["S"]]
        for i, on in enumerate(s):
            if on:
                return b[i * width : (i + 1) * width]
        return a
    av, bv = _int(a), _int(b)
    if t == "$not":
        r = ~av
    elif t in ("$and", "$or", "$xor", "$xnor"):
        r = {
            "$and": av & bv,
            "$or": av | bv,
            "$xor": av ^ bv,
            "$xnor": ~(av ^ bv),
        }[t]
    elif t in ("$reduce_or", "$reduce_bool"):
        r = int(av != 0)
    elif t == "$reduce_and":
        r = int(av == (1 << len(a)) - 1)
    elif t == "$reduce_xor":
        r = bin(av).count("1") & 1
    elif t == "$logic_not":
        r = int(av == 0)
    elif t == "$logic_and":
        r = int(av != 0 and bv != 0)
    elif t == "$logic_or":
        r = int(av != 0 or bv != 0)
    elif t in ("$eq", "$ne", "$lt", "$le", "$gt", "$ge"):
        r = {
            "$eq": av == bv,
            "$ne": av != bv,
            "$lt": av < bv,
            "$le": av <= bv,
            "$gt": av > bv,
            "$ge": av >= bv,
        }[t]
    elif t in ("$add", "$sub", "$mul"):
        r = {"$add": av + bv, "$sub": av - bv, "$mul": av * bv}[t]
    elif t == "$shiftx":
        r = av >> bv if bv < len(a) else 0
    else:
        raise AssertionError(f"evaluator does not model {t}")
    r &= mask
    return [(r >> i) & 1 for i in range(width)]


def _evaluator(module: Module, values: dict[str, int]):
    """``bits -> int`` over ``module``'s combinational logic, with the
    named ports / registers in ``values`` (by netname or port name) as its
    inputs. Flop outputs not named are 0."""
    env: dict[Bit, int] = {}
    for name, value in values.items():
        bits = (
            module.ports[name].bits
            if name in module.ports
            else module.netnames[name].bits
        )
        for i, bit in enumerate(bits):
            env[bit] = (value >> i) & 1
    comb: dict[Bit, tuple[Cell, int]] = {}
    for c in module.cells.values():
        if "Y" in c.connections and "Q" not in c.connections:
            for i, bit in enumerate(c.connections["Y"]):
                comb[bit] = (c, i)
    memo: dict[str, list[int]] = {}

    def get(bit: Bit) -> int:
        if isinstance(bit, str):
            return 1 if bit == "1" else 0
        if bit in env:
            return env[bit]
        if bit not in comb:
            return 0
        cell, i = comb[bit]
        if cell.name not in memo:
            memo[cell.name] = _comb_value(cell, get)
        return memo[cell.name][i]

    return lambda bits: _int([get(b) for b in bits])


def _flop_of(module: Module, q_name: str) -> Cell:
    """The flop whose ``Q`` is netname ``q_name``."""
    bits = set(module.netnames[q_name].bits)
    for c in module.cells.values():
        if "Q" in c.connections and set(c.connections["Q"]) & bits:
            return c
    raise AssertionError(f"no flop drives {q_name}")


# --- 1. wires get a netname --------------------------------------------------

_WIRES = """module sub (input wire ca, input wire cb, input wire d, output reg q);
    wire mixed_clk;
    assign mixed_clk = ca | cb;
    always @(posedge mixed_clk) q <= d;
endmodule
module m (input wire clk_a, input wire clk_b, input wire d, input wire en,
          output reg q, output reg q2, output wire q3);
    reg a_q;
    always @(posedge clk_a) a_q <= d;
    wire mix;
    assign mix = a_q ^ en;
    reg s1;
    always @(posedge clk_b) begin s1 <= mix; q <= s1; end
    wire both_clk = clk_a ^ clk_b;
    always @(posedge both_clk) q2 <= d;
    sub u (.ca(clk_a), .cb(clk_b), .d(d), .q(q3));
endmodule
"""


def test_wires_get_netnames(tmp_path: Path) -> None:
    """``wire`` declarations (with an ``assign``, an initialiser, or as a
    port) are named like variables, ``inst.wire`` inside an instance, and
    the name sits on the driver's output, as Yosys ``flatten`` names it."""
    module = _slang(tmp_path, _WIRES)
    for name in ("mix", "both_clk", "u.mixed_clk", "clk_a", "d"):
        assert name in module.netnames, name
    assert _driver(module, _netname_bits(module, "mix")[0]).type == "$xor"
    assert _driver(module, _netname_bits(module, "both_clk")[0]).type == "$xor"
    assert _driver(module, _netname_bits(module, "u.mixed_clk")[0]).type == "$or"
    assert _netname_bits(module, "clk_a") == module.ports["clk_a"].bits
    if _HAVE_YOSYS:
        y_mod = _yosys(tmp_path, _WIRES)
        for name in ("mix", "both_clk", "u.mixed_clk"):
            assert name in y_mod.netnames, name


def test_waiver_naming_a_wire_matches_on_both_frontends(tmp_path: Path) -> None:
    """CDC-023 quotes the clock net by its netname; a waiver written
    against the wire's name suppresses the finding on either frontend
    (slang used to quote ``$slang$xor$1.Y``)."""
    waivers = (
        "waive CDC-023 `both_clk` mixed clock, reviewed\n"
        "waive CDC-023 `u\\.mixed_clk` mixed clock, reviewed\n"
    )
    _, live, gone = _same_verdict(tmp_path, _WIRES, waivers=waivers)
    assert "CDC-023" not in live
    assert gone.get("CDC-023") == 2


def test_get_nets_on_a_wire_matches_on_both_frontends(tmp_path: Path) -> None:
    """``set_false_path -through [get_nets mix]`` covers the a_q -> s1
    crossing through the wire on both frontends (slang found no ``mix``)."""
    sdc = _SDC + "set_false_path -through [get_nets mix]\n"
    base = _same_verdict(tmp_path, _WIRES)
    assert base[1].get("CDC-003") == 1
    _, live, gone = _same_verdict(tmp_path, _WIRES, sdc=sdc)
    assert "CDC-003" not in live
    assert gone.get("CDC-003") == 1


def test_attribute_on_a_wire_reaches_the_rules(tmp_path: Path) -> None:
    """A declaration attribute on a ``wire`` lands on its netname, where
    the attribute-reading rules look (``cdc_sync`` on the flop's Q)."""
    src = """module m (input wire clk_a, input wire clk_b, input wire d,
          output wire q);
    reg a_q, s1, s2;
    always @(posedge clk_a) a_q <= d;
    always @(posedge clk_b) begin s1 <= a_q; s2 <= s1; end
    (* keep_me = "yes" *) wire tapped = s2;
    assign q = tapped;
endmodule
"""
    module = _slang(tmp_path, src)
    assert module.netnames["tapped"].attributes.get("keep_me") is not None


# --- 2. overlapping case labels in a clocked block: first match wins --------

_OVERLAP = """module m (input clk_a, input clk_b, input d, input e, input f,
          input [1:0] sel, output reg q, output reg r);
    always @(posedge clk_a) begin
        casez (sel)
            2'b1?:   q <= d;
            2'b?1:   q <= e;
            default: q <= f;
        endcase
        case (sel)
            2'd1, 2'd2: r <= d;
            2'd2:       r <= e;
        endcase
    end
endmodule
"""


def _next_state(module: Module, reg: str, values: dict[str, int]) -> int:
    flop = _flop_of(module, reg)
    return _evaluator(module, values)(flop.connections["D"])


@pytest.mark.parametrize("sel", range(4))
@pytest.mark.parametrize("data", range(8))
def test_clocked_case_first_matching_label_wins(
    tmp_path: Path, sel: int, data: int
) -> None:
    """``sel == 2'b11`` matches both ``casez`` items and ``sel == 2`` both
    ``case`` items: the first one in source order wins (IEEE 1800-2017
    12.5). The clocked lowering used to let the last write win."""
    d, e, f = data & 1, (data >> 1) & 1, (data >> 2) & 1
    values = {"sel": sel, "d": d, "e": e, "f": f, "q": 0, "r": 0}
    module = _slang(tmp_path, _OVERLAP)
    want_q = d if sel & 2 else e if sel & 1 else f
    want_r = d if sel in (1, 2) else 0  # r holds (0) otherwise
    assert _next_state(module, "q", values) == want_q
    assert _next_state(module, "r", values) == want_r
    if _HAVE_YOSYS and sel == 3 and data == 0b011:
        y_mod = _yosys(tmp_path, _OVERLAP)
        for reg in ("q", "r"):
            for s in range(4):
                for v in range(8):
                    vals = {**values, "sel": s, "d": v & 1, "e": (v >> 1) & 1}
                    vals["f"] = (v >> 2) & 1
                    assert _next_state(module, reg, vals) == _next_state(
                        y_mod, reg, vals
                    ), (reg, s, v)


def test_distinct_constant_labels_add_no_priority_logic(tmp_path: Path) -> None:
    """Labels that are distinct constants cannot overlap, so their arms
    need no "no earlier label matched" term: the netlist is as before."""
    src = """module m (input clk_a, input d, input e, input [1:0] sel,
          output reg q);
    always @(posedge clk_a)
        case (sel)
            2'd0: q <= d;
            2'd1: q <= e;
            2'd2: q <= 1'b0;
        endcase
endmodule
"""
    module = _slang(tmp_path, src)
    assert not _cells(module, "$and")
    assert len(_cells(module, "$not")) == 0
