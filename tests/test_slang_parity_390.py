"""slang frontend: the reset-arm, netname-range, enable and sync-reset
polarity corner cases of rtl-buddy-cdc#390 (items 10-14).

One section per item: a minimal design, the netlist shape the slang path
lowers it to, and, where yosys is on PATH, the same design through the
``lint`` recipe (``proc; flatten; opt_clean``) with the flops, next-state
functions and findings compared. CI's pytest job has no yosys, so there
the slang-side assertions stand alone (they pin the shape the Yosys
comparison was made against locally).
"""

from __future__ import annotations

import importlib.util
import itertools
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


def _warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]


_SDC = """create_clock -name clk_a -period 10 [get_ports clk_a]
create_clock -name clk_b -period 7 [get_ports clk_b]
set_clock_groups -asynchronous -group {clk_a} -group {clk_b}
"""


def _lint(
    tmp_path: Path,
    src: str,
    frontend: str,
    *,
    sdc: str = _SDC,
    extra: list[str] | None = None,
) -> dict:
    sv = _write(tmp_path, src, f"{frontend}.sv")
    sdc_path = tmp_path / "m.sdc"
    sdc_path.write_text(sdc)
    out = tmp_path / f"{frontend}.json"
    args = ["lint", "--top", "m", "--sdc", str(sdc_path), "--frontend", frontend]
    args += [*(extra or []), "--format", "json", "--output", str(out), str(sv)]
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


def _int(vals: list[int]) -> int:
    return sum(v << i for i, v in enumerate(vals))


def _comb_value(cell: Cell, get) -> list[int]:
    """Output bits of one combinational cell (``x`` read as 0)."""
    conn = cell.connections
    width = len(conn["Y"])
    a = [get(b) for b in conn.get("A", ())]
    b = [get(x) for x in conn.get("B", ())]
    t = cell.type
    if t == "$mux":
        return b if get(conn["S"][0]) else a
    av, bv = _int(a), _int(b)
    r = {
        "$not": lambda: ~av,
        "$logic_not": lambda: int(av == 0),
        "$and": lambda: av & bv,
        "$or": lambda: av | bv,
        "$xor": lambda: av ^ bv,
        "$logic_and": lambda: int(av != 0 and bv != 0),
        "$logic_or": lambda: int(av != 0 or bv != 0),
        "$reduce_or": lambda: int(av != 0),
        "$reduce_bool": lambda: int(av != 0),
        "$eq": lambda: int(av == bv),
    }[t]()
    r &= (1 << width) - 1
    return [(r >> i) & 1 for i in range(width)]


def _evaluator(module: Module, values: dict[str, int]):
    """``bits -> int`` over ``module``'s combinational logic, the named
    ports / registers in ``values`` (by port or netname) as its inputs."""
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


def _flops_of(module: Module, q_name: str) -> list[tuple[Cell, list[int]]]:
    """Every flop driving a bit of netname ``q_name``, with the bit
    positions of ``q_name`` it drives."""
    bits = module.netnames[q_name].bits
    out = []
    for c in module.cells.values():
        q = c.connections.get("Q")
        if q is None:
            continue
        hit = [i for i, b in enumerate(bits) if b in q]
        if hit:
            out.append((c, hit))
    return out


def _flop_types(module: Module, regs: tuple[str, ...]) -> dict[str, list[str]]:
    """Register -> per-bit flop cell type (``"-"`` for an undriven bit)."""
    out: dict[str, list[str]] = {}
    for reg in regs:
        types = ["-"] * len(module.netnames[reg].bits)
        for cell, hit in _flops_of(module, reg):
            for i in hit:
                types[i] = cell.type
        out[reg] = types
    return out


def _pin_values(module: Module, reg: str, values: dict[str, int]) -> dict[str, int]:
    """The value of every data / control pin (``D``, ``AD``, ``ARST``, ...)
    of each flop driving ``reg``, per bit of ``reg`` (``pin[i]``), under
    ``values``. A pin a flop does not have is absent."""
    ev = _evaluator(module, values)
    reg_bits = module.netnames[reg].bits
    out: dict[str, int] = {}
    for cell, hit in _flops_of(module, reg):
        q = cell.connections["Q"]
        for i in hit:
            lane = q.index(reg_bits[i])
            for pin, bits in cell.connections.items():
                if pin in ("Q", "CLK"):
                    continue
                pick = bits[lane] if len(bits) == len(q) else bits[0]
                out[f"{pin}[{i}]"] = ev((pick,))
    return out


def _compare_next_state(
    module: Module, regs: tuple[str, ...], inputs: tuple[str, ...], y_mod: Module
) -> None:
    """Every pin of every register's flops agrees with ``y_mod`` under all
    values of ``inputs`` and of the register itself."""

    def width(name: str) -> int:
        return len(
            module.ports[name].bits if name in module.ports
            else module.netnames[name].bits
        )  # fmt: skip

    for reg in regs:
        names = (*inputs, reg)
        for combo in itertools.product(*(range(1 << width(n)) for n in names)):
            values = dict(zip(names, combo))
            assert _pin_values(module, reg, values) == _pin_values(
                y_mod, reg, values
            ), (reg, values)


# --- 10. a register the reset arm leaves alone holds under reset ----------

_HELD = """module m (input clk_a, input rst_n, input rst, input srst, input srst_n,
          input en, input d, input e, output reg q, output reg h,
          output reg q2, output reg h2, output reg [1:0] p,
          output reg [1:0] ap, output reg [1:0] pn, output reg [1:0] c);
    // Async active-low: h is not in the reset arm.
    always_ff @(posedge clk_a or negedge rst_n)
        if (!rst_n) q <= 1'b0;
        else begin q <= d; h <= e; end
    // Async active-high.
    always_ff @(posedge clk_a or posedge rst)
        if (rst) q2 <= 1'b0;
        else begin q2 <= d; h2 <= e; end
    // Sync: only p[0] is reset.
    always_ff @(posedge clk_a)
        if (srst) p[0] <= 1'b0;
        else      p <= {e, d};
    // Async, part of one register: ap[1] keeps its value.
    always_ff @(posedge clk_a or negedge rst_n)
        if (!rst_n) ap[0] <= 1'b1;
        else        ap <= {e, d};
    // Sync active-low, the other bit.
    always_ff @(posedge clk_a)
        if (!srst_n) pn[1] <= 1'b1;
        else         pn <= {e, d};
    // Sync under an enable, part of the register.
    always_ff @(posedge clk_a)
        if (en) begin
            if (srst) c[0] <= 1'b0;
            else      c <= {e, d};
        end
endmodule
"""

_HELD_REGS = ("q", "h", "q2", "h2", "p", "ap", "pn", "c")
_HELD_INPUTS = ("rst_n", "rst", "srst", "srst_n", "en", "d", "e")


def test_register_left_out_of_the_reset_arm_holds(tmp_path: Path, caplog) -> None:
    """A register (or bit) the data branch writes but the reset arm does
    not keeps its value while the reset is asserted, as Yosys ``proc_dff``
    builds it: a plain ``$dff`` behind a hold mux selected by the reset when
    the whole register is left alone, an ``$aldff`` loading its own ``Q``
    for the left-alone bits of an async-reset register, and the ``proc``
    reset mux (constant leg for the reset bits, ``Q`` for the others) on a
    plain ``$dff`` for a sync reset. It used to reset to 0."""
    with caplog.at_level(logging.WARNING, logger=_SLANG_LOG):
        module = _slang(tmp_path, _HELD)
    assert not _warnings(caplog)
    types = _flop_types(module, _HELD_REGS)
    assert types == {
        "q": ["$adff"],
        "h": ["$dff"],
        "q2": ["$adff"],
        "h2": ["$dff"],
        "p": ["$dff", "$dff"],
        "ap": ["$aldff", "$aldff"],
        "pn": ["$dff", "$dff"],
        "c": ["$dff", "$dff"],
    }
    for rst_n, rst, e, h in itertools.product((0, 1), repeat=4):
        pins = _pin_values(
            module, "h", {"rst_n": rst_n, "e": e, "h": h, "h2": h, "rst": rst}
        )
        assert pins == {"D[0]": e if rst_n else h}
        pins = _pin_values(module, "h2", {"rst": rst, "e": e, "h2": h})
        assert pins == {"D[0]": h if rst else e}
    ap = _pin_values(module, "ap", {"ap": 0b10, "rst_n": 0})
    assert ap["AD[0]"] == 1 and ap["AD[1]"] == 1  # ap[1] loads its own Q
    if _HAVE_YOSYS:
        y_mod = _yosys(tmp_path, _HELD)
        assert _flop_types(y_mod, _HELD_REGS) == types
        _compare_next_state(module, _HELD_REGS, _HELD_INPUTS, y_mod)


_RESET_CHOICE = """module m (input clk_a, input rst_n, input mode, input d, input e,
          output reg [1:0] q, output reg r, output reg [2:0] f);
    always_ff @(posedge clk_a or negedge rst_n)
        if (!rst_n) begin
            if (mode) begin q <= 2'b01; r <= d; end
            else      q <= 2'b11;
        end else begin
            q <= {e, d}; r <= e;
        end
    always_ff @(posedge clk_a or negedge rst_n)
        if (!rst_n) for (int i = 0; i < 3; i++) f[i] <= 1'b1;
        else        f <= {d, e, d};
endmodule
"""


def test_run_time_choice_in_a_reset_arm_is_reported(tmp_path: Path, caplog) -> None:
    """A run-time ``if`` / ``case`` inside a reset arm is not modelled: it
    is reported, and each bit it writes resets to the one constant every
    write in it loads (``q[0]``), else 0 (``q[1]``, and ``r``, loaded with
    a signal); the registers keep their flops. A ``for`` loop in the arm
    is unrolled like the data branch's."""
    with caplog.at_level(logging.WARNING, logger=_SLANG_LOG):
        module = _slang(tmp_path, _RESET_CHOICE)
    assert any("not modelled in a reset" in w for w in _warnings(caplog))
    ((q, _),) = _flops_of(module, "q")
    assert q.type == "$adff" and q.parameters["ARST_VALUE"] == "01"
    ((r, _),) = _flops_of(module, "r")
    assert r.type == "$adff" and r.parameters["ARST_VALUE"] == "0"
    ((f, _),) = _flops_of(module, "f")
    assert f.type == "$adff" and f.parameters["ARST_VALUE"] == "111"


_HELD_CROSSING = """module m (input clk_a, input clk_b, input rst_b_n, input d,
          input e, output reg q, output reg h);
    reg rst_q;
    always_ff @(posedge clk_b) rst_q <= rst_b_n;
    always_ff @(posedge clk_a or negedge rst_q)
        if (!rst_q) q <= 1'b0;
        else begin q <= d; h <= e; end
endmodule
"""


def test_held_register_verdict_matches_yosys(tmp_path: Path) -> None:
    """``h`` is not reset by the foreign-domain ``rst_q``: it is no
    destination of the reset crossing (RDC-001 names ``q`` only), and the
    reset reaches ``h`` as its hold mux's select, a data crossing."""
    sdc = _SDC + (
        "set_input_delay -clock clk_a 1 [get_ports {d e}]\n"
        "set_input_delay -clock clk_b 1 [get_ports rst_b_n]\n"
    )
    crossings, live, _ = _same_verdict(tmp_path, _HELD_CROSSING, sdc=sdc)
    assert crossings == 1
    assert live == {"CDC-001": 1, "RDC-001": 1}


# --- 11. a register only a reset arm writes still gets its flop -----------

_RESET_ONLY = """module m (input clk_a, input rst_n, input rst, input srst, input srst_n,
          input set_n, input d, output reg q, output reg z, output reg zn,
          output reg [1:0] zp, output reg q3, output reg zs, output reg zs2,
          output reg [1:0] zq, output reg k, output reg k2, output reg k3);
    // Async: z is written by the reset arm only.
    always_ff @(posedge clk_a or negedge rst_n)
        if (!rst_n) begin q <= 1'b0; z <= 1'b1; end
        else q <= d;
    // Async, no data branch at all.
    always_ff @(posedge clk_a or posedge rst)
        if (rst) zn <= 1'b1;
    // Async: the reset writes zp[0], the data branch zp[1].
    always_ff @(posedge clk_a or negedge rst_n)
        if (!rst_n) zp[0] <= 1'b1;
        else        zp[1] <= d;
    // Sync active-low: zs is written by the reset arm only.
    always_ff @(posedge clk_a)
        if (!srst_n) zs <= 1'b1;
        else         q3 <= d;
    // Sync, no data branch at all.
    always_ff @(posedge clk_a)
        if (srst) zs2 <= 1'b0;
    // Sync: the reset writes both bits, the data branch zq[0] only.
    always_ff @(posedge clk_a)
        if (srst) zq <= 2'b10;
        else      zq[0] <= d;
    // Several async controls: k / k2 written by one control arm only.
    always_ff @(posedge clk_a or negedge rst_n or negedge set_n)
        if (!rst_n)      k <= 1'b0;
        else if (!set_n) k2 <= 1'b1;
        else             k3 <= d;
endmodule
"""

_RESET_ONLY_REGS = ("q", "z", "zn", "zp", "q3", "zs", "zs2", "zq", "k", "k2", "k3")
_RESET_ONLY_INPUTS = ("rst_n", "rst", "srst", "srst_n", "set_n", "d")


def test_register_written_only_in_a_reset_arm_gets_a_flop(
    tmp_path: Path, caplog
) -> None:
    """A register (or bit) only a reset arm writes is still a register, as
    Yosys ``proc_dff`` builds it: an ``$adff`` holding its value (``D`` =
    ``Q``) for an async reset, the reset-arm bits of a register the data
    branch also writes in the same flop (an ``$aldff`` loading its own
    ``Q`` for the other bits), a plain ``$dff`` behind the ``proc`` reset
    mux for a sync reset, and a ``$dffsr`` for one of several async
    controls. It used to get no flop at all, and a block with no ``else``
    was skipped whole."""
    with caplog.at_level(logging.WARNING, logger=_SLANG_LOG):
        module = _slang(tmp_path, _RESET_ONLY)
    assert not _warnings(caplog)
    types = _flop_types(module, _RESET_ONLY_REGS)
    assert types == {
        "q": ["$adff"],
        "z": ["$adff"],
        "zn": ["$adff"],
        "zp": ["$aldff", "$aldff"],
        "q3": ["$dff"],
        "zs": ["$dff"],
        "zs2": ["$dff"],
        "zq": ["$dff", "$dff"],
        "k": ["$dffsr"],
        "k2": ["$dffsr"],
        "k3": ["$dff"],
    }
    z = _pin_values(module, "z", {"z": 0, "rst_n": 1})
    assert z == {"ARST[0]": 1, "D[0]": 0}
    for srst_n, zs in itertools.product((0, 1), repeat=2):
        got = _pin_values(module, "zs", {"srst_n": srst_n, "zs": zs})
        assert got == {"D[0]": zs if srst_n else 1}
    if _HAVE_YOSYS:
        y_mod = _yosys(tmp_path, _RESET_ONLY)
        assert _flop_types(y_mod, _RESET_ONLY_REGS) == types
        _compare_next_state(module, _RESET_ONLY_REGS, _RESET_ONLY_INPUTS, y_mod)


_RESET_ONLY_CROSSING = """module m (input clk_a, input clk_b, input rst_n, input d,
          output reg q, output reg flag);
    reg rst_q;
    always_ff @(posedge clk_b) rst_q <= d;
    // flag is set by the foreign-domain reset only and read out.
    always_ff @(posedge clk_a or negedge rst_q)
        if (!rst_q) flag <= 1'b1;
    always_ff @(posedge clk_a or negedge rst_n)
        if (!rst_n) q <= 1'b0;
        else        q <= flag;
endmodule
"""


def test_reset_only_register_verdict_matches_yosys(tmp_path: Path) -> None:
    """``flag`` exists only through its reset arm; its foreign-domain
    reset is an unsynchronised reset crossing on both frontends (slang had
    no flop, so no finding)."""
    sdc = _SDC + "set_input_delay -clock clk_b 1 [get_ports d]\n"
    _crossings, live, _ = _same_verdict(tmp_path, _RESET_ONLY_CROSSING, sdc=sdc)
    assert live.get("RDC-001") == 1
