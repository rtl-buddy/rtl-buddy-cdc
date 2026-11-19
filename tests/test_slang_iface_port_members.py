"""Members of a top-level interface port are netlist ports, and nothing
the frontend cannot lower takes a register out of the analysis silently
(rtl-buddy-cdc#414).

0.8.0 lowered no top-level interface member: a read of ``apb.psel`` was an
opaque driver and a write of ``apb.prdata`` was dropped, so every register
whose only observer was an interface member was pruned as unobservable
(:mod:`rtl_buddy_cdc.liveness`) — in the project template a 2FF
synchroniser fed by a comparator, and its CDC-003 with it.

The fixture (``fixtures/slang_iface_port_members``) has members of both
directions, with a modport (``req_if.sub host``) and without
(``flag_if side``), and an output-only cone through ``host.busy`` that
holds a synchroniser with a comparator in front of it (CDC-003).
"""

from __future__ import annotations

import importlib.util
import json
import logging
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rtl_buddy_cdc.cli import app
from rtl_buddy_cdc.frontend import Frontend, elaborate
from rtl_buddy_cdc.liveness import dropped_registers, prune_dead_cells

if importlib.util.find_spec("pyslang") is None:
    pytest.skip(
        "pyslang not installed — slang elaboration tests are gated on it",
        allow_module_level=True,
    )

from rtl_buddy_cdc.frontends.slang import _ModuleBuilder

FIX = Path(__file__).parent / "fixtures" / "slang_iface_port_members"
TOP = "slang_iface_port_members"
SV = FIX / f"{TOP}.sv"
SDC = FIX / f"{TOP}.sdc"

_EXPECTED_PORTS = {
    "host.clk": "input",
    "host.wdata": "input",
    "host.wvalid": "input",
    "host.rdata": "output",
    "host.busy": "output",
    "side.req": "input",
    "side.ack": "output",
}


def _lint(tmp_path: Path, frontend: str, sv: Path = SV, sdc: Path = SDC, top=TOP):
    out = tmp_path / f"{frontend}.json"
    result = CliRunner().invoke(
        app,
        [
            "lint",
            "--top",
            top,
            "--sdc",
            str(sdc),
            "--frontend",
            frontend,
            "--format",
            "json",
            "--output",
            str(out),
            str(sv),
        ],
    )
    assert result.exit_code in (0, 1), result.output
    return json.loads(out.read_text()), result


def test_members_are_ports_with_the_direction_the_design_uses(caplog) -> None:
    caplog.set_level(logging.WARNING)
    module = elaborate([SV], TOP, frontend=Frontend.slang)
    got = {n: p.direction for n, p in module.ports.items() if "." in n}
    assert got == _EXPECTED_PORTS
    # The member's netname is its port's bits: SDC ``get_nets`` and
    # ``get_ports`` name the same net.
    for name in _EXPECTED_PORTS:
        assert module.netnames[name].bits == module.ports[name].bits
    # Nothing was opaque or dropped.
    assert not [r for r in caplog.records if "slang frontend" in r.getMessage()]


def test_no_register_is_lost_behind_an_interface_member() -> None:
    """Every register survives the liveness pass: the synchroniser whose
    only reader is ``host.busy`` and the one driving ``side.ack``."""
    module = elaborate([SV], TOP, frontend=Frontend.slang)
    assert dropped_registers(module, prune_dead_cells(module)) == []
    flops = {c.name for c in module.cells.values() if c.type == "$adff"}
    assert any(n.startswith("u_sync_busy.") for n in flops)
    assert any(n.startswith("u_sync_req.") for n in flops)


def test_output_only_cone_reports_cdc_003(tmp_path: Path) -> None:
    report, result = _lint(tmp_path, "slang")
    rules = sorted(v["rule_id"] for v in report["violations"])
    assert rules == ["CDC-003", "CDC-003"], rules
    assert all(v["cell_name"].startswith("u_sync_busy.") for v in report["violations"])
    # The typed members seed crossings: side.req is synchronised properly.
    srcs = {c.get("src_port") for c in report["crossings"]}
    assert "side.req" in srcs
    assert "not lowered" not in result.output
    assert "left out of the CDC analysis" not in result.output


@pytest.mark.skipif(shutil.which("yosys") is None, reason="yosys not on PATH")
def test_yosys_read_verilog_names_the_members_the_same(tmp_path: Path) -> None:
    """Yosys ``read_verilog`` lowers a top-level interface port to the same
    ``<port>.<member>`` ports, so one SDC serves both frontends. It makes a
    member without a modport ``inout``, which the crossing walk does not
    start from — reported, not silent."""
    module = elaborate([SV], TOP, frontend=Frontend.yosys)
    got = {n: p.direction for n, p in module.ports.items() if "." in n}
    assert set(got) == set(_EXPECTED_PORTS)
    assert {n: d for n, d in got.items() if n.startswith("host.")} == {
        n: d for n, d in _EXPECTED_PORTS.items() if n.startswith("host.")
    }
    assert got["side.req"] == got["side.ack"] == "inout"
    report, result = _lint(tmp_path, "yosys")
    rules = sorted(v["rule_id"] for v in report["violations"])
    assert rules == ["CDC-003", "CDC-003"], rules
    assert "inout port(s) side.req, side.ack are not crossing sources" in (
        result.output
    )


_ARRAYS_AND_NESTING = """
interface bus_if (input logic clk);
  logic [3:0] data;
  logic       ack;
  modport sink (input clk, data, output ack);
endinterface
interface outer_if;
  bus_if inner (.clk(1'b0));
  logic flag;
endinterface
module leaf (bus_if.sink b, input logic clk, output logic [3:0] q);
  always_ff @(posedge clk) q <= b.data;
  assign b.ack = q[0];
endmodule
module t (input logic clk_a, clk_b, bus_if.sink arr [2], outer_if o,
          input logic [3:0] din, output logic [3:0] y0, y1, y2);
  bus_if u_if (.clk(clk_a));
  always_ff @(posedge clk_a) u_if.data <= din;
  leaf u_leaf (.b(u_if), .clk(clk_b), .q(y0));
  always_ff @(posedge clk_b) y1 <= arr[1].data;
  assign arr[0].ack = y1[0];
  always_ff @(posedge clk_b) y2 <= o.inner.data ^ {3'b0, o.flag};
endmodule
"""


def test_interface_arrays_nesting_and_inner_instances(tmp_path: Path) -> None:
    """An interface array port is one set of member ports per element (each
    with the declared modport), a nested interface's members are ports
    too (``o.inner.data``; its own ports are bound by the parent
    interface), and an interface instantiated inside the design is
    lowered in place — a module reading it through its interface port
    reads the instance's bits."""
    sv = tmp_path / "t.sv"
    sv.write_text(_ARRAYS_AND_NESTING)
    module = elaborate([sv], "t", frontend=Frontend.slang)
    got = {n: p.direction for n, p in module.ports.items() if "." in n}
    assert got == {
        "arr[0].clk": "input",
        "arr[0].data": "input",
        "arr[0].ack": "output",
        "arr[1].clk": "input",
        "arr[1].data": "input",
        "arr[1].ack": "output",  # modport output, undriven
        "o.inner.data": "input",
        "o.inner.ack": "input",
        "o.flag": "input",
    }
    flops = {c.name: c for c in module.cells.values() if c.type == "$dff"}
    leaf = flops["u_leaf.$slang$dff$2"]
    (src,) = [
        f for f in flops.values() if f.connections["D"] == module.ports["din"].bits
    ]
    assert leaf.connections["D"] == src.connections["Q"]


# --- dropped / opaque logic keeps its inputs observable --------------------

_DROPS = """
module sync2 (input logic clk, input logic d, output logic q);
  logic m;
  always_ff @(posedge clk) begin m <= d; q <= m; end
endmodule
(* blackbox *)
module bb (input logic i, output logic y);
  logic x;
  assign y = x;
endmodule
module t (input logic clk_a, clk_b, input logic a, b, output logic o, o2);
  logic sa, sb;
  always_ff @(posedge clk_a) begin sa <= a; sb <= b; end
  bb u_bb (.i(1'b0), .y(o));
  // Output bound to a name inside a blackbox: not lowered.
  sync2 u_s (.clk(clk_b), .d(sa ^ sb), .q(u_bb.x));
  logic r1, r2;
  always_ff @(posedge clk_b) r1 <= sb;
  always_ff @(posedge clk_b) r2 <= sa;
  // Right-hand side not lowered: an opaque driver.
  assign o2 = $urandom(r1);
  // Target not lowered: the write is dropped.
  always_comb u_bb.x = r2;
endmodule
"""


def test_dropped_and_opaque_logic_keeps_its_registers(tmp_path: Path) -> None:
    """Each construct the frontend reports as not lowered still observes
    what it reads (an ``$_OPAQUE_SINK_`` cell), so the synchroniser behind
    the dropped output connection, and ``r1`` / ``r2`` behind the opaque
    right-hand side and the dropped write, stay crossing destinations
    instead of being pruned without a word."""
    sv = tmp_path / "t.sv"
    sv.write_text(_DROPS)
    module = elaborate([sv], "t", frontend=Frontend.slang)
    sinks = [c for c in module.cells.values() if c.type == _ModuleBuilder.OPAQUE_SINK]
    assert len(sinks) == 3
    assert dropped_registers(module, prune_dead_cells(module)) == []
    sdc = tmp_path / "t.sdc"
    sdc.write_text(
        "create_clock -name ca -period 10 [get_ports clk_a]\n"
        "create_clock -name cb -period 7 [get_ports clk_b]\n"
        "set_clock_groups -asynchronous -group ca -group cb\n"
        "set_input_delay -clock ca 1 [get_ports {a b}]\n"
    )
    report, _ = _lint(tmp_path, "slang", sv=sv, sdc=sdc, top="t")
    dsts = {c["dst_flop"] for c in report["crossings"]}
    assert "u_s.$slang$dff$3" in dsts  # behind the dropped output
    assert {"$slang$dff$5", "$slang$dff$6"} <= dsts  # r1, r2
    assert "CDC-003" in {v["rule_id"] for v in report["violations"]}


def test_an_unobserved_register_is_named(tmp_path: Path) -> None:
    """A synchroniser whose output is unconnected cannot cause a failure
    and is left out, as Yosys ``opt_clean`` would delete it — but never
    silently: the run names it on stderr."""
    sv = tmp_path / "t.sv"
    sv.write_text(
        """
module sync2 (input logic clk, input logic d, output logic q);
  logic m;
  always_ff @(posedge clk) begin m <= d; q <= m; end
endmodule
module t (input logic clk_a, clk_b, input logic a, output logic o);
  logic sa;
  always_ff @(posedge clk_a) sa <= a;
  assign o = sa;
  sync2 u_s (.clk(clk_b), .d(sa), .q());
endmodule
"""
    )
    sdc = tmp_path / "t.sdc"
    sdc.write_text(
        "create_clock -name ca -period 10 [get_ports clk_a]\n"
        "create_clock -name cb -period 7 [get_ports clk_b]\n"
        "set_clock_groups -asynchronous -group ca -group cb\n"
        "set_input_delay -clock ca 1 [get_ports a]\n"
        "set_output_delay -clock ca 1 [get_ports o]\n"
    )
    for frontend in ("slang", "yosys"):
        if frontend == "yosys" and shutil.which("yosys") is None:
            continue
        report, result = _lint(tmp_path, frontend, sv=sv, sdc=sdc, top="t")
        assert report["crossings"] == []
        assert "2 register(s) left out of the CDC analysis" in result.output, (
            frontend,
            result.output,
        )
        assert "u_s." in result.output
