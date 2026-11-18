"""Clock-network views agree flat and abstracted (rtl-buddy-cdc#402 item 5).

Flattened, the flops inside an instance are clock sinks: CDC-021 reads
their clock roots, CDC-008 / CDC-010 / CDC-025 read the clock network
they seed, CDC-023 walks their clocks for combines and the scan-mode tag
for DFT muxes. Under ``--blackbox`` / ``--greybox`` those flops are gone
and the instance's clock pins are where the parent's clock network ends.
Every one of those views now starts from the pins too, so each design
below reports the same clock-network findings in all three views.

The views are elaborated by the slang frontend from one source, so the
module skips without pyslang.
"""

from __future__ import annotations

import importlib.util
import json
import re
from collections import Counter
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rtl_buddy_cdc import netlist
from rtl_buddy_cdc.cli import app
from rtl_buddy_cdc.domain import boundary_clock_sinks

needs_slang = pytest.mark.skipif(
    importlib.util.find_spec("pyslang") is None, reason="pyslang not installed"
)

# Two abstracted modules: ``stage`` takes its clock on an allow-listed
# pin name, ``tap`` on one the parent-side classifier only accepts when
# it traces to a declared clock.
_LEAVES = """\
module stage (input clk, input d, output q);
    logic a, b;
    always_ff @(posedge clk) begin a <= d; b <= a; end
    assign q = b;
endmodule
module tap (input smp_clk, input d, output q);
    logic a, b;
    always_ff @(posedge smp_clk) begin a <= d; b <= a; end
    assign q = b;
endmodule
"""

_TWO_CLOCKS = """\
create_clock -name fast -period 4 [get_ports fast]
create_clock -name slow -period 9 [get_ports slow]
set_clock_groups -asynchronous -group {fast} -group {slow}
"""

# Rules whose verdict depends on the clock network's sinks.
_CLOCK_RULES = ("CDC-008", "CDC-010", "CDC-021", "CDC-023")


def _views(tmp_path: Path, top_sv: str, sdc: str, *extra: str) -> dict[str, dict]:
    sv = tmp_path / "t.sv"
    sv.write_text(_LEAVES + top_sv)
    sdc_path = tmp_path / "t.sdc"
    sdc_path.write_text(sdc)
    reports = {}
    for view in ("flat", "blackbox", "greybox"):
        out = tmp_path / f"{view}.json"
        args = ["lint", "--top", "top", "--sdc", str(sdc_path), "--frontend", "slang"]
        if view != "flat":
            args += [f"--{view}", "stage", f"--{view}", "tap"]
        args += [*extra, "--format", "json", "--output", str(out), str(sv)]
        result = CliRunner().invoke(app, args)
        assert result.exit_code in (0, 1), result.output
        reports[view] = json.loads(out.read_text())
    return reports


def _clock_findings(report: dict) -> Counter:
    return Counter(
        v["rule_id"] for v in report["violations"] if v["rule_id"] in _CLOCK_RULES
    )


def _assert_views_agree(reports: dict[str, dict], want: Counter) -> None:
    for view, report in reports.items():
        assert _clock_findings(report) == want, view


@needs_slang
def test_undeclared_clock_port_reported_in_every_view(tmp_path: Path) -> None:
    """``ck`` has no create_clock and clocks only abstracted flops: the
    flat run's CDC-021 used to vanish under ``--blackbox`` / ``--greybox``.
    The finding names the same port and sits under the same instance."""
    top = """\
module top (input fast, input [1:0] ck, input d, output q0, output q1,
            output logic r);
    logic s;
    always_ff @(posedge fast) s <= d;
    stage u_s (.clk(ck[0]), .d(s), .q(q0));
    tap   u_t (.smp_clk(ck[1]), .d(s), .q(q1));
    always_ff @(posedge fast) r <= q0;
endmodule
"""
    sdc = "create_clock -name fast -period 4 [get_ports fast]\n"
    sdc += "set_input_delay -clock fast 1 [get_ports d]\n"
    reports = _views(tmp_path, top, sdc)
    _assert_views_agree(reports, Counter({"CDC-021": 1}))
    for view, report in reports.items():
        (finding,) = [v for v in report["violations"] if v["rule_id"] == "CDC-021"]
        assert "top-level input port 'ck' drives" in finding["message"], view
        assert finding["instance_path"] == ["u_s"], view
    blackbox = next(
        v for v in reports["blackbox"]["violations"] if v["rule_id"] == "CDC-021"
    )
    # A stub's allow-listed pin stands for its flops.
    assert "the clock pin of abstracted instance u_s.clk" in blackbox["message"]
    greybox = next(
        v for v in reports["greybox"]["violations"] if v["rule_id"] == "CDC-021"
    )
    # A greybox body proves ``smp_clk`` a clock pin by its own flops.
    assert "(u_s.clk, u_t.smp_clk)" in greybox["message"]


@needs_slang
def test_declared_clock_on_the_pin_is_not_reported(tmp_path: Path) -> None:
    top = """\
module top (input fast, input slow, input d, output q);
    stage u_s (.clk(slow), .d(d), .q(q));
endmodule
"""
    reports = _views(
        tmp_path, top, _TWO_CLOCKS + "set_input_delay -clock fast 1 [get_ports d]\n"
    )
    _assert_views_agree(reports, Counter())


@needs_slang
def test_async_clock_mux_select_reported_in_every_view(tmp_path: Path) -> None:
    """A clock mux in front of an abstracted instance, selected from the
    other domain: CDC-010 in every view, and the mux's clock legs are
    not clock-as-data (CDC-008) because the mux is still on the clock
    network."""
    top = """\
module top (input fast, input fast_alt, input slow, input s, input d,
            output q);
    logic sel;
    always_ff @(posedge slow) sel <= s;
    wire mclk = sel ? fast_alt : fast;
    stage u_s (.clk(mclk), .d(d), .q(q));
endmodule
"""
    sdc = (
        "create_clock -name fast -period 4 [get_ports {fast fast_alt}]\n"
        "create_clock -name slow -period 9 [get_ports slow]\n"
        "set_clock_groups -asynchronous -group {fast} -group {slow}\n"
        "set_input_delay -clock slow 1 [get_ports s]\n"
        "set_input_delay -clock fast 1 [get_ports d]\n"
    )
    _assert_views_agree(_views(tmp_path, top, sdc), Counter({"CDC-010": 1}))


@needs_slang
def test_clock_gate_in_front_of_an_instance_is_not_clock_as_data(
    tmp_path: Path,
) -> None:
    top = """\
module top (input fast, input slow, input e, input d, output q,
            output logic r);
    logic en, x;
    always_ff @(posedge fast) en <= e;
    always_ff @(posedge slow) x <= d;
    wire gclk = fast & en;
    stage u_s (.clk(gclk), .d(x), .q(q));
    always_ff @(posedge slow) r <= q;
endmodule
"""
    sdc = _TWO_CLOCKS + (
        "set_input_delay -clock fast 1 [get_ports e]\n"
        "set_input_delay -clock slow 1 [get_ports d]\n"
    )
    _assert_views_agree(_views(tmp_path, top, sdc), Counter())


@needs_slang
def test_clock_combine_into_an_instance_reported_in_every_view(
    tmp_path: Path,
) -> None:
    top = """\
module top (input fast, input slow, input d, output q);
    wire cc = fast ^ slow;
    stage u_s (.clk(cc), .d(d), .q(q));
endmodule
"""
    reports = _views(
        tmp_path, top, _TWO_CLOCKS + "set_input_delay -clock fast 1 [get_ports d]\n"
    )
    _assert_views_agree(reports, Counter({"CDC-023": 1}))
    blackbox = next(
        v for v in reports["blackbox"]["violations"] if v["rule_id"] == "CDC-023"
    )
    assert "flop(s) and abstracted-instance clock pin(s)" in blackbox["message"]
    assert "`u_s.clk`" in blackbox["message"]
    flat = next(v for v in reports["flat"]["violations"] if v["rule_id"] == "CDC-023")
    assert "abstracted-instance" not in flat["message"]


_SCAN_TOP = """\
module top (input fast, input slow, input tclk, (* scan_en *) input se,
            input d, output q);
    logic x;
    always_ff @(posedge slow) x <= d;
    wire sclk = se ? tclk : fast;
    stage u_s (.clk(sclk), .d(x), .q(q));
endmodule
"""
_SCAN_SDC = _TWO_CLOCKS + (
    "create_clock -name tclk -period 20 [get_ports tclk]\n"
    "set_clock_groups -asynchronous -group {tclk} -group {slow}\n"
    "set_input_delay -clock slow 1 [get_ports d]\n"
    "set_input_delay -clock fast 1 [get_ports se]\n"
)


@needs_slang
def test_scan_mode_tag_reaches_the_boundary_sink(tmp_path: Path) -> None:
    """The crossing into an instance clocked through a scan mux carries
    the scan-mode tag in every view, so ``--ignore-scan-mode`` drops the
    same crossings flattened and abstracted."""
    for view, report in _views(tmp_path, _SCAN_TOP, _SCAN_SDC).items():
        tags = Counter(
            (c["src_clock"], c["dst_clock"], c["scan_mode"])
            for c in report["crossings"]
        )
        assert tags == Counter({("slow", "fast", True): 1}), view
    ignored = _views(tmp_path, _SCAN_TOP, _SCAN_SDC, "--ignore-scan-mode")
    for view, report in ignored.items():
        assert report["violations"] == [], view


def test_boundary_clock_sinks_names_each_pin_bit(tmp_path: Path) -> None:
    """One sink per connected bit, named ``inst.pin`` (``inst.pin[i]`` on
    a multi-bit pin); a constant bit and an unknown instance are skipped."""
    raw = {
        "modules": {
            "top": {
                "attributes": {"top": "1"},
                "ports": {"c": {"direction": "input", "bits": [2, 3]}},
                "cells": {
                    "u": {
                        "type": "ip",
                        "port_directions": {"clk": "input", "ck2": "input"},
                        "connections": {"clk": [2], "ck2": [3, "0"]},
                    }
                },
                "netnames": {"c": {"bits": [2, 3], "hide_name": 0, "attributes": {}}},
            }
        }
    }
    path = tmp_path / "t.json"
    path.write_text(json.dumps(raw))
    module = netlist.load(path)
    pins = {"u": frozenset({"clk", "ck2"}), "gone": frozenset({"clk"})}
    assert boundary_clock_sinks(module, pins) == {"u.ck2[0]": 3, "u.clk": 2}


def test_cdc_021_message_lists_flops_and_pins(tmp_path: Path) -> None:
    """A port clocking both flat flops and abstracted pins names both."""
    from rtl_buddy_cdc.rules import _build_context, check_cdc_021
    from rtl_buddy_cdc.sdc import parse

    raw = {
        "attributes": {"top": "1"},
        "ports": {
            "ck": {"direction": "input", "bits": [2]},
            "d": {"direction": "input", "bits": [3]},
            "q": {"direction": "output", "bits": [4]},
        },
        "cells": {
            "f": {
                "type": "$dff",
                "parameters": {"CLK_POLARITY": "1", "WIDTH": "1"},
                "port_directions": {"CLK": "input", "D": "input", "Q": "output"},
                "connections": {"CLK": [2], "D": [3], "Q": [4]},
            },
            "u": {
                "type": "ip",
                "port_directions": {"clk": "input", "d": "input"},
                "connections": {"clk": [2], "d": [4]},
            },
        },
        "netnames": {},
    }
    path = tmp_path / "t.json"
    path.write_text(json.dumps({"modules": {"top": raw}}))
    module = netlist.load(path)
    spec = parse("set_input_delay -clock x 1 [get_ports d]\n")
    ctx = _build_context(module, spec, boundary_clock_pins={"u": frozenset({"clk"})})
    (v,) = check_cdc_021(module, [], spec, ctx=ctx)
    assert re.search(
        r"port 'ck' drives flop f and the clock pin of abstracted instance u\.clk ",
        v.message,
    )
    assert v.cell_name == "f"
