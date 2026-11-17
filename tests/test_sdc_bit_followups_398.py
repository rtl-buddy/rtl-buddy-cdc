"""SDC bit-selection follow-ups of rtl-buddy-cdc#398 (items 4, 5, 8, 9).

4. A flop clocked from one bit of a port typed bit by bit takes that
   bit's clock as its domain — in the flat walk, the rule context and
   the abstraction's boundary walk alike. An untyped bit behaves as an
   untyped scalar port used as a clock.
5. The CDC-011 / CDC-025 fix hints for a multi-run bit selection are
   SDC this tool reads back: ``{in[7:4] in[2:0]}``, not ``{in[7:4,2:0]}``.
6. (item 8) One bit, or a range of bits, of an unpacked-array element
   resolves: ``a_reg[1][5]``, ``a[1][7:4]``, ``a_reg[0][7]/Q``.
7. (item 9) Every unbraced bus-index form on the Tcl backend warns as
   ``d[3]`` does, with the targets unchanged.

Every netlist here is written by hand (no yosys needed); the slang
abstraction check skips without pyslang.
"""

from __future__ import annotations

import importlib.util
import json
import re
from collections import Counter
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rtl_buddy_cdc import abstract, netlist, sdc_objects
from rtl_buddy_cdc.cli import app
from rtl_buddy_cdc.domain import (
    _bit_drivers,
    assign_domains,
    find_crossings,
    flop_domain_resolver,
)
from rtl_buddy_cdc.rules import _build_context, check_cdc_021
from rtl_buddy_cdc.sdc import (
    UNCONSTRAINED_SENTINEL,
    ClockSpec,
    parse,
    synthesize_unconstrained_inputs,
)


def _dff(clk: list[int], d: list[int], q: list[int]) -> dict:
    return {
        "type": "$dff",
        "parameters": {"CLK_POLARITY": "1", "WIDTH": str(len(d))},
        "port_directions": {"CLK": "input", "D": "input", "Q": "output"},
        "connections": {"CLK": clk, "D": d, "Q": q},
    }


def _write(
    tmp_path: Path,
    name: str,
    ports: dict,
    cells: dict,
    extra_nets: dict | None = None,
) -> Path:
    netnames = {
        p: {"bits": spec["bits"], "hide_name": 0, "attributes": {}}
        for p, spec in ports.items()
    }
    for p, spec in ports.items():
        for key in ("offset", "upto"):
            if key in spec:
                netnames[p][key] = spec.pop(key)
    netnames.update(extra_nets or {})
    raw = {
        "modules": {
            name: {
                "attributes": {"top": "1"},
                "ports": ports,
                "cells": cells,
                "netnames": netnames,
            }
        }
    }
    path = tmp_path / f"{name}.json"
    path.write_text(json.dumps(raw))
    return path


def _analyze(netlist_path: Path, sdc_text: str, tmp_path: Path) -> dict:
    sdc = tmp_path / "x.sdc"
    sdc.write_text(sdc_text)
    result = CliRunner().invoke(
        app, ["analyze", "-n", str(netlist_path), "-s", str(sdc), "-f", "json"]
    )
    assert result.exit_code in (0, 1), result.output
    return json.loads(result.stdout)


# --- item 4: a flop clocked from one bit of a per-bit typed port ------------

# ``ck`` is ``[3:0]``; ``ck1`` is a scalar. ``ra`` launches on clk_a and
# feeds one flop clocked by each of ``ck[2]``, ``ck[3]`` and ``ck1``.
_CLOCKS = """\
create_clock -name clk_a -period 10 [get_ports clk_a]
create_clock -name clk_v -period 7
set_clock_groups -asynchronous -group {clk_a} -group {clk_v}
set_input_delay -clock clk_a 1 [get_ports d]
"""


def _busclk(tmp_path: Path) -> Path:
    ports = {
        "clk_a": {"direction": "input", "bits": [2]},
        "ck": {"direction": "input", "bits": [3, 4, 5, 6]},
        "ck1": {"direction": "input", "bits": [7]},
        "d": {"direction": "input", "bits": [8]},
        "q2": {"direction": "output", "bits": [10]},
        "q3": {"direction": "output", "bits": [11]},
        "q1": {"direction": "output", "bits": [12]},
    }
    cells = {
        "ra": _dff([2], [8], [9]),
        "f2": _dff([5], [9], [10]),
        "f3": _dff([6], [9], [11]),
        "f1": _dff([7], [9], [12]),
    }
    extra = {"ra_q": {"bits": [9], "hide_name": 0, "attributes": {}}}
    return _write(tmp_path, "busclk", ports, cells, extra)


def _spec(text: str, module: netlist.Module, *, stamp: bool) -> ClockSpec:
    spec = parse(text)
    if stamp:
        synthesize_unconstrained_inputs(spec, module)
    else:
        sdc_objects.resolve_port_delays(module, spec)
    return spec


def _domains(module: netlist.Module, spec: ClockSpec) -> dict[str, str | None]:
    return {
        fd.flop.cell.name: fd.clock
        for fd in assign_domains(module, **spec.clock_root_inputs())
    }


@pytest.mark.parametrize("stamp", [True, False], ids=["cli", "api"])
def test_bit_clocked_flop_takes_the_bits_clock(
    tmp_path: Path, sdc_backend: str, stamp: bool
) -> None:
    """``ck[2]`` is typed clk_v: its flop is a clk_v flop, as it was when
    the whole port was typed. ``ck[3]`` is untyped and lands exactly where
    the untyped scalar ``ck1`` does — ``<unconstrained>`` once the CLI
    stamps untyped inputs, the raw port name without that."""
    module = netlist.load(_busclk(tmp_path))
    text = _CLOCKS + "set_input_delay -clock clk_v 1 [get_ports {ck[2]}]\n"
    spec = _spec(text, module, stamp=stamp)
    assert set(spec.port_clock) >= {"ck[2]", "d"}
    got = _domains(module, spec)
    untyped = UNCONSTRAINED_SENTINEL if stamp else None
    assert got["ra"] == "clk_a"
    assert got["f2"] == "clk_v"
    assert got["f3"] == (untyped or "ck")
    assert got["f1"] == (untyped or "ck1")
    # The rule context names every flop as the crossing walk does.
    ctx = _build_context(module, spec)
    assert ctx.domains == got
    # The raw roots CDC-021 reads still name the undeclared ports.
    assert ctx.clock_roots["f2"] == "ck[2]"
    assert ctx.clock_roots["f1"] == "ck1"
    crossings = find_crossings(
        module, port_clock=spec.port_clock, **spec.clock_root_inputs()
    )
    cdc_021 = check_cdc_021(module, crossings, spec, ctx=ctx)
    named = {re.search(r"port '([^']+)'", v.message).group(1) for v in cdc_021}  # type: ignore[union-attr]
    assert named == {"ck", "ck1"}


def test_whole_port_typing_is_unchanged(tmp_path: Path, sdc_backend: str) -> None:
    """Typed whole, every bit of ``ck`` clocks clk_v flops, as before."""
    module = netlist.load(_busclk(tmp_path))
    text = _CLOCKS + "set_input_delay -clock clk_v 1 [get_ports ck]\n"
    got = _domains(module, _spec(text, module, stamp=True))
    assert (got["f2"], got["f3"]) == ("clk_v", "clk_v")


def test_declared_clock_port_keeps_its_clock(tmp_path: Path) -> None:
    """A port carrying a ``create_clock`` is that clock, whatever its bits'
    ``set_input_delay`` typing says (``ClockSpec.clock_for_port``)."""
    module = netlist.load(_busclk(tmp_path))
    text = (
        _CLOCKS
        + "create_clock -name ck -period 5 [get_ports ck]\n"
        + "set_input_delay -clock clk_v 1 [get_ports {ck[2]}]\n"
    )
    got = _domains(module, _spec(text, module, stamp=True))
    assert (got["f2"], got["f3"]) == ("ck", "ck")


def test_bit_clock_reaches_the_report(tmp_path: Path, sdc_backend: str) -> None:
    """End to end: the clk_a → clk_v crossing into the ``ck[2]`` flop is
    reported as one, and the ``ck[3]`` flop as an unconstrained capture
    like the scalar's."""
    report = _analyze(
        _busclk(tmp_path),
        _CLOCKS + "set_input_delay -clock clk_v 1 [get_ports {ck[2]}]\n",
        tmp_path,
    )
    pairs = Counter((c["src_clock"], c["dst_clock"]) for c in report["crossings"])
    assert pairs == Counter(
        {("clk_a", "clk_v"): 1, ("clk_a", UNCONSTRAINED_SENTINEL): 2}
    )


def test_boundary_walk_agrees_with_the_flat_walk(tmp_path: Path) -> None:
    """The abstraction's boundary clock walk roots a pin on ``ck[2]`` /
    ``ck[3]`` exactly as the flat walk names the flop it clocks."""
    module = netlist.load(_busclk(tmp_path))
    spec = _spec(
        _CLOCKS + "set_input_delay -clock clk_v 1 [get_ports {ck[2]}]\n",
        module,
        stamp=True,
    )
    walk = abstract._BoundaryClockWalk.build(module, spec, spec.pin_clocks)
    drivers = _bit_drivers(module)
    flat = flop_domain_resolver(module, **spec.clock_root_inputs())
    for cell in ("f2", "f3", "f1", "ra"):
        f = next(fd.flop for fd in assign_domains(module) if fd.flop.cell.name == cell)
        assert walk.root(module, f.clk, drivers, max_depth=16) == flat(f), cell


_ABS_SV = """module pipe (input clk, input [1:0] d, output [1:0] q);
    reg [1:0] s1, s2;
    always @(posedge clk) begin s1 <= d; s2 <= s1; end
    assign q = s2;
endmodule
module m (input clk_a, input [3:0] ck, input [1:0] d,
          output [1:0] qb, output [1:0] qc, output reg [1:0] qa);
    reg [1:0] ra;
    always @(posedge clk_a) ra <= d;
    pipe u_b (.clk(ck[2]), .d(ra), .q(qb));
    pipe u_c (.clk(ck[3]), .d(ra), .q(qc));
    always @(posedge clk_a) qa <= qb;
endmodule
"""


@pytest.mark.skipif(
    importlib.util.find_spec("pyslang") is None, reason="pyslang not installed"
)
def test_flat_blackbox_and_greybox_views_agree(tmp_path: Path) -> None:
    """``pipe`` clocked from ``ck[2]`` (typed clk_v) and ``ck[3]``
    (untyped): flattened, blackboxed and greyboxed, the run reports the
    same crossings between the same clocks."""
    sv = tmp_path / "m.sv"
    sv.write_text(_ABS_SV)
    sdc = tmp_path / "m.sdc"
    sdc.write_text(_CLOCKS + "set_input_delay -clock clk_v 1 [get_ports {ck[2]}]\n")
    views = {}
    for view in ("flat", "blackbox", "greybox"):
        out = tmp_path / f"{view}.json"
        args = ["lint", "--top", "m", "--sdc", str(sdc), "--frontend", "slang"]
        if view != "flat":
            args += [f"--{view}", "pipe"]
        args += ["--format", "json", "--output", str(out), str(sv)]
        result = CliRunner().invoke(app, args)
        assert result.exit_code in (0, 1), result.output
        report = json.loads(out.read_text())
        views[view] = Counter(
            (c["src_clock"], c["dst_clock"]) for c in report["crossings"]
        )
    assert views["flat"] == Counter(
        {
            ("clk_a", "clk_v"): 1,
            ("clk_v", "clk_a"): 1,
            ("clk_a", UNCONSTRAINED_SENTINEL): 1,
        }
    )
    assert views["blackbox"] == views["flat"]
    assert views["greybox"] == views["flat"]


# --- item 5: fix hints are SDC this tool reads back ---------------------------

_HINT_RE = re.compile(
    r"set_(?:in|out)put_delay -clock (<[^>]*>|\S+) (\[get_ports (?:\{[^}]*\}|\S+)\])"
)


def _hint_module(tmp_path: Path) -> Path:
    """``in[7:0]`` is captured by a clk_b register; ``o[3:0]`` is
    driven by a clk_a register."""
    ports = {
        "clk_a": {"direction": "input", "bits": [2]},
        "clk_b": {"direction": "input", "bits": [3]},
        "in": {"direction": "input", "bits": list(range(10, 18))},
        "src": {"direction": "input", "bits": list(range(30, 34))},
        "q": {"direction": "output", "bits": list(range(20, 28))},
        "o": {"direction": "output", "bits": list(range(40, 44))},
    }
    cells = {
        "r_in": _dff([3], list(range(10, 18)), list(range(20, 28))),
        "r_o": _dff([2], list(range(30, 34)), list(range(40, 44))),
    }
    return _write(tmp_path, "hint", ports, cells)


_HINT_CLOCKS = """\
create_clock -name clk_a -period 10 [get_ports clk_a]
create_clock -name clk_b -period 7 [get_ports clk_b]
set_clock_groups -asynchronous -group {clk_a} -group {clk_b}
set_input_delay -clock clk_a 1 [get_ports src]
set_output_delay -clock clk_b 1 [get_ports q]
"""


def _typed_bits(spec: ClockSpec, module: netlist.Module, port: str) -> dict[int, str]:
    """Declared index → clock of every typed bit of ``port``."""
    sdc_objects.resolve_port_delays(module, spec)
    typing = spec.port_typing(module)
    width = len(module.ports[port].bits)
    out = {}
    for pos in range(width):
        clock = typing.clock_of_bit(port, pos)
        if clock is not None:
            out[module.port_bit_index(port, pos)] = clock
    return out


@pytest.mark.parametrize(
    ("rule", "typed", "port", "word", "rest"),
    [
        ("CDC-011", "set_input_delay -clock clk_b 1 [get_ports {in[3]}]", "in",
         "[get_ports {in[7:4] in[2:0]}]", {0, 1, 2, 4, 5, 6, 7}),
        ("CDC-025", "set_output_delay -clock clk_a 1 [get_ports {o[2]}]", "o",
         "[get_ports {o[3] o[1:0]}]", {0, 1, 3}),
    ],
    ids=["cdc-011", "cdc-025"],
)  # fmt: skip
def test_multi_run_hint_round_trips(
    tmp_path: Path,
    sdc_backend: str,
    rule: str,
    typed: str,
    port: str,
    word: str,
    rest: set[int],
) -> None:
    """One bit of the bus is typed, so the finding names the other bits
    (``in[7:4,2:0]``) and its hint types them with one name per run.
    Fed back to the parser — on either backend — the hint types exactly
    those bits, and with it added the finding is gone."""
    path = _hint_module(tmp_path)
    module = netlist.load(path)
    report = _analyze(path, _HINT_CLOCKS + typed + "\n", tmp_path)
    (finding,) = [v for v in report["violations"] if v["rule_id"] == rule]
    assert "7:4,2:0" in finding["message"] or "3,1:0" in finding["message"]
    m = _HINT_RE.search(finding["message"])
    assert m is not None, finding["message"]
    clock, got_word = m.groups()
    assert got_word == word
    if clock.startswith("<"):  # CDC-025 leaves the receiving clock open
        clock = "clk_a"
    command = "set_input_delay" if rule == "CDC-011" else "set_output_delay"
    statement = f"{command} -clock {clock} 1 {got_word}\n"
    alone = parse(_HINT_CLOCKS + statement)
    assert not alone.partial_warnings
    assert _typed_bits(alone, module, port) == dict.fromkeys(rest, clock)
    fixed = _analyze(path, _HINT_CLOCKS + typed + "\n" + statement, tmp_path)
    assert rule not in {v["rule_id"] for v in fixed["violations"]}


def test_single_bit_and_whole_port_hints_are_unchanged() -> None:
    from rtl_buddy_cdc.rules import _sdc_port_word

    assert _sdc_port_word("d") == "d"
    assert _sdc_port_word("d[3]") == "{d[3]}"
    assert _sdc_port_word("d[2:0]") == "{d[2:0]}"
    assert _sdc_port_word("d[7:4,2,0]") == "{d[7:4] d[2] d[0]}"


@pytest.mark.parametrize(
    ("target", "bits"),
    [
        ("{in[7:4]}", {4, 5, 6, 7}),
        ("{in[4:7]}", {4, 5, 6, 7}),
        ("{in[7:4] in[1]}", {1, 4, 5, 6, 7}),
        ("{in[9:6]}", {6, 7}),
        ("{in[12:10]}", set()),
    ],
)
def test_bus_range_types_those_bits(
    tmp_path: Path, sdc_backend: str, target: str, bits: set[int]
) -> None:
    """``d[hi:lo]`` names that range of declared indices, in either
    order; bits outside the port are not there to type."""
    module = netlist.load(_hint_module(tmp_path))
    spec = parse(
        _HINT_CLOCKS + f"set_input_delay -clock clk_b 1 [get_ports {target}]\n"
    )
    assert _typed_bits(spec, module, "in") == dict.fromkeys(bits, "clk_b")


# --- item 8: a bit of an unpacked-array element ---------------------------------

_ELEM_CLOCKS = """\
create_clock -name clk_a -period 10 [get_ports clk_a]
create_clock -name clk_b -period 7 [get_ports clk_b]
set_clock_groups -asynchronous -group {clk_a} -group {clk_b}
set_input_delay -clock clk_a 1 [get_ports d]
"""


def _elements(tmp_path: Path, *, literal: bool = False) -> Path:
    """``logic [7:4] src [0:1]`` on clk_a — element netnames ``src[0]`` /
    ``src[1]`` with offset 4, as both frontends name them — each read by
    a 4-bit clk_b register. ``literal`` adds a 1-bit net literally named
    ``src[1][5]`` on another wire."""
    ports = {
        "clk_a": {"direction": "input", "bits": [2]},
        "clk_b": {"direction": "input", "bits": [3]},
        "d": {"direction": "input", "bits": [4, 5, 6, 7]},
        "q0": {"direction": "output", "bits": [20, 21, 22, 23]},
        "q1": {"direction": "output", "bits": [24, 25, 26, 27]},
    }
    cells = {
        "$procdff$1": _dff([2], [4, 5, 6, 7], [10, 11, 12, 13]),
        "$procdff$2": _dff([2], [4, 5, 6, 7], [14, 15, 16, 17]),
        "$procdff$3": _dff([3], [10, 11, 12, 13], [20, 21, 22, 23]),
        "$procdff$4": _dff([3], [14, 15, 16, 17], [24, 25, 26, 27]),
    }
    extra = {
        "src[0]": {"bits": [10, 11, 12, 13], "offset": 4, "hide_name": 0, "attributes": {}},
        "src[1]": {"bits": [14, 15, 16, 17], "offset": 4, "hide_name": 0, "attributes": {}},
    }  # fmt: skip
    if literal:
        extra["src[1][5]"] = {"bits": [26], "hide_name": 0, "attributes": {}}
    return _write(tmp_path, "elem", ports, cells, extra)


@pytest.mark.parametrize(
    ("kind", "pattern", "want"),
    [
        ("registers", "src_reg[1][5]", {"$procdff$2": {1}}),
        ("registers", "src[1][7:6]", {"$procdff$2": {2, 3}}),
        ("registers", "src_reg[*][4]", {"$procdff$1": {0}, "$procdff$2": {0}}),
        ("registers", "src_reg[0][3]", {}),  # outside [7:4]
        ("registers", "src_reg[0]", {"$procdff$1": {0, 1, 2, 3}}),
        ("nets", "src[0][6]", {12}),
        ("nets", "src[0][4:5]", {10, 11}),
    ],
)
def test_element_bit_selection(
    tmp_path: Path, kind: str, pattern: str, want: dict | set
) -> None:
    index = sdc_objects.NameIndex(netlist.load(_elements(tmp_path)))
    if kind == "nets":
        assert index.net_bits(pattern) == want
    else:
        assert index.registers(pattern) == want


def test_literal_name_wins(tmp_path: Path) -> None:
    """A net literally named ``src[1][5]`` is that net, not bit 5 of
    ``src[1]``; with no such name the element reading applies."""
    index = sdc_objects.NameIndex(netlist.load(_elements(tmp_path, literal=True)))
    assert index.net_bits("src[1][5]") == {26}
    assert index.net_bits("src[1][6]") == {16}


@pytest.mark.parametrize(
    ("constraint", "cut"),
    [
        ("", 0),
        ("set_false_path -from [get_cells {src_reg[1][7:4]}]", 1),
        ("set_false_path -from [get_pins {src_reg[0][*]/Q}]", 1),
        ("set_false_path -through [get_nets {src[1][7:4]}]", 1),
        ("set_false_path -from [get_cells {src_reg[1][5]}]", 0),  # one bit of four
        (
            "\n".join(
                f"set_false_path -from [get_pins {{src_reg[0][{i}]/Q}}]"
                for i in range(4, 8)
            ),
            1,
        ),
    ],
    ids=["none", "cells-range", "pins-glob", "through-range", "one-bit", "per-bit"],
)
def test_element_bits_cut_crossings(
    tmp_path: Path, sdc_backend: str, constraint: str, cut: int
) -> None:
    """A false path naming every bit of an element, by range, glob or
    one statement per bit, acknowledges the crossing out of it; one bit
    of four does not."""
    report = _analyze(_elements(tmp_path), _ELEM_CLOCKS + constraint + "\n", tmp_path)
    assert report["summary"]["crossings"] == 2
    assert report["summary"]["suppressed"] == cut
    assert report["summary"]["violations"] == 2 - cut


# --- item 9: every unbraced bus-index form warns on the Tcl backend -----------

_INDEX_FORMS = ["3", "*", "3:0", "-1", "?", "1*", "7:4,1"]


@pytest.mark.parametrize("idx", _INDEX_FORMS)
@pytest.mark.parametrize(
    "template",
    [
        "set_input_delay -clock c 1 [get_ports d[{i}]]",
        "set_output_delay -clock c 1 [get_ports q[{i}]]",
        "set_false_path -from [get_cells a_reg[{i}]] -to [get_cells b]",
        "set_false_path -through [get_nets n[{i}]]",
        "set_max_delay -datapath_only 2 -from [get_pins a_reg[{i}]/Q] -to [get_cells b]",
        "set_case_analysis 0 [get_ports s[{i}]]",
    ],
    ids=["input-delay", "output-delay", "fp-cells", "fp-through", "dpo-pins", "case"],
)
def test_unbraced_index_warns_like_a_digit(
    sdc_backend: str, idx: str, template: str
) -> None:
    """``d[*]``, ``d[3:0]`` … unbraced run Tcl command substitution just as
    ``d[3]`` does: same warning, and the same targets as the braced form."""
    head = "create_clock -name c -period 1 [get_ports c]\n"
    bare_line = template.format(i=idx)
    braced_line = re.sub(
        r"[\w/]+\[" + re.escape(idx) + r"\][\w/]*",
        lambda m: "{" + m.group(0) + "}",
        bare_line,
    )
    bare = parse(head + bare_line + "\n")
    braced = parse(head + braced_line + "\n")
    warned = [w for w in bare.partial_warnings if "unbraced bus index" in w]
    if sdc_backend == "tcl":
        assert warned and all(f"command {idx!r}" in w for w in warned)
    else:
        assert not warned  # the tokenizer runs nothing
    assert not [w for w in braced.partial_warnings if "unbraced bus index" in w]
    for field in (
        "port_delays",
        "port_clock",
        "false_path_exceptions",
        "datapath_only_delays",
        "case_analysis",
    ):
        got, want = getattr(bare, field), getattr(braced, field)
        if field in ("false_path_exceptions", "datapath_only_delays"):
            got = [(r.__class__.__name__, *_objects(r)) for r in got]
            want = [(r.__class__.__name__, *_objects(r)) for r in want]
        elif field == "port_delays":
            got = [(p.command, p.clock, p.targets) for p in got]
            want = [(p.command, p.clock, p.targets) for p in want]
        assert got == want, field


def _objects(rec) -> tuple:
    return tuple(
        getattr(rec, name)
        for name in ("from_objects", "to_objects", "through")
        if hasattr(rec, name)
    )


def test_issue_reproducer(sdc_backend: str) -> None:
    text = (
        "create_clock -name c -period 1 [get_ports c]\n"
        "set_input_delay -clock c 1 [get_ports d[*]]\n"
    )
    spec = parse(text)
    assert [p.targets for p in spec.port_delays] == [("d[*]",)]
    assert bool(spec.partial_warnings) is (sdc_backend == "tcl")
