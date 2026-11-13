"""Inferred-memory ports as crossing endpoints + CDC-026 (issue #326).

Unit tests on hand-built netlists for every port shape the analyzer
decodes — split ``$memwr_v2`` / ``$memrd`` / ``$memrd_v2`` cells (what
``proc`` leaves, the ``lint`` flow) and the packed ``$mem_v2`` cell
(``memory_collect`` / ``memory -nomap``) — plus the domain-map surface
and cross-frontend parity on the memory fixtures.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from rtl_buddy_cdc import netlist, sdc as sdc_mod, waivers
from rtl_buddy_cdc.domain import filter_async, find_crossings
from rtl_buddy_cdc.domain_map import build_domain_map
from rtl_buddy_cdc.memory import (
    MEM_PORT_TYPE,
    crossing_view,
    display_memid,
    find_memories,
)
from rtl_buddy_cdc.netlist import Bit, Cell, Module, Port
from rtl_buddy_cdc.rules import (
    _build_context,
    check_cdc_026,
    memory_crossings,
    memory_synchronizer,
    run_all,
)

FIX = Path(__file__).parent / "fixtures"
MEMORY_FIXTURES = (
    "bad_dualport_ram_crossing",
    "bad_dualport_ram_async_read",
    "bad_ram_write_addr_wrong_domain",
    "good_async_fifo_inferred_ram",
    "good_same_clock_ram",
)

# Clock-port bits used by the hand-built netlists.
WCLK, RCLK, WCLK2, RCLK2 = 2, 3, 4, 5
_CLOCKS = {"wclk": WCLK, "rclk": RCLK, "wclk2": WCLK2, "rclk2": RCLK2}

_ALL_ASYNC_SDC = "\n".join(
    [f"create_clock -name {n} -period 10 [get_ports {n}]" for n in _CLOCKS]
    + ["set_clock_groups -asynchronous " + " ".join(f"-group {{{n}}}" for n in _CLOCKS)]
)


def _spec(extra: str = ""):
    return sdc_mod.parse(_ALL_ASYNC_SDC + "\n" + extra, backend="tokenizer")


def _bin(n: int, width: int = 32) -> str:
    return format(n, f"0{width}b")


def _module(cells: list[Cell], extra_ports: dict[str, Port] | None = None) -> Module:
    ports = {n: Port(name=n, direction="input", bits=(b,)) for n, b in _CLOCKS.items()}
    ports.update(extra_ports or {})
    return Module(
        name="top",
        ports=ports,
        cells={c.name: c for c in cells},
        netnames={},
    )


def _dff(name: str, clk: Bit, d: tuple[Bit, ...], q: tuple[Bit, ...]) -> Cell:
    return Cell(
        name=name,
        type="$dff",
        connections={"CLK": (clk,), "D": d, "Q": q},
        parameters={"CLK_POLARITY": "1", "WIDTH": _bin(len(q))},
    )


def _memwr(name: str, clk: Bit, addr, data, en, *, clocked: bool = True) -> Cell:
    return Cell(
        name=name,
        type="$memwr_v2",
        connections={"CLK": (clk,), "ADDR": addr, "DATA": data, "EN": en},
        parameters={
            "MEMID": "\\mem",
            "ABITS": _bin(len(addr)),
            "WIDTH": _bin(len(data)),
            "CLK_ENABLE": "1" if clocked else "0",
            "CLK_POLARITY": "1",
            "PORTID": _bin(0),
            "PRIORITY_MASK": "",
        },
        attributes={"src": "top.sv:10.5-10.30"},
    )


def _memrd(name: str, addr, data, *, clk: Bit = "x", v2: bool = True) -> Cell:
    clocked = clk != "x"
    return Cell(
        name=name,
        type="$memrd_v2" if v2 else "$memrd",
        connections={"CLK": (clk,), "EN": ("1",), "ADDR": addr, "DATA": data},
        parameters={
            "MEMID": "\\mem",
            "ABITS": _bin(len(addr)),
            "WIDTH": _bin(len(data)),
            "CLK_ENABLE": _bin(1 if clocked else 0),
            "CLK_POLARITY": _bin(1),
        },
        attributes={"src": "top.sv:20.5-20.30"},
    )


# --- decoding ---------------------------------------------------------------


def _packed_mem() -> Cell:
    """2 write ports (wclk, wclk2) + 2 read ports (port 0 clocked on
    rclk2, port 1 asynchronous), ABITS=2, WIDTH=3."""
    return Cell(
        name="mem",
        type="$mem_v2",
        connections={
            "WR_CLK": (WCLK, WCLK2),
            "WR_ADDR": (10, 11, 12, 13),
            "WR_DATA": (20, 21, 22, 23, 24, 25),
            "WR_EN": (30, 30, 30, 31, 31, 31),
            "RD_CLK": (RCLK2, "x"),
            "RD_ADDR": (40, 41, 42, 43),
            "RD_DATA": (50, 51, 52, 53, 54, 55),
            "RD_EN": (60, "1"),
            "RD_ARST": ("0", "0"),
            "RD_SRST": ("0", "0"),
        },
        parameters={
            "MEMID": "\\mem",
            "ABITS": _bin(2),
            "WIDTH": _bin(3),
            "SIZE": _bin(4),
            "WR_PORTS": _bin(2),
            "RD_PORTS": _bin(2),
            # Packed per-port flags, MSB-first: port 0 is the LAST char.
            "WR_CLK_ENABLE": "11",
            "WR_CLK_POLARITY": "11",
            "RD_CLK_ENABLE": "01",
            "RD_CLK_POLARITY": "01",
        },
        attributes={"src": "top.sv:5.5-5.40"},
    )


def test_packed_mem_v2_decodes_every_port_slice() -> None:
    (mem,) = find_memories(_module([_packed_mem()]))
    assert mem.memid == "mem"
    w0, w1 = mem.write_ports
    assert (w0.endpoint, w0.clk, w0.clocked) == ("mem[wr0]", WCLK, True)
    assert (w0.addr, w0.data, w0.en) == ((10, 11), (20, 21, 22), (30, 30, 30))
    assert (w1.endpoint, w1.clk, w1.clocked) == ("mem[wr1]", WCLK2, True)
    assert (w1.addr, w1.data, w1.en) == ((12, 13), (23, 24, 25), (31, 31, 31))
    r0, r1 = mem.read_ports
    assert (r0.endpoint, r0.clk, r0.clocked) == ("mem[rd0]", RCLK2, True)
    assert (r0.addr, r0.data, r0.en) == ((40, 41), (50, 51, 52), (60,))
    assert (r1.endpoint, r1.clk, r1.clocked) == ("mem[rd1]", "x", False)
    assert (r1.addr, r1.data, r1.en) == ((42, 43), (53, 54, 55), ("1",))
    assert r0.control_bits == (40, 41, 60)
    assert r1.control_bits == (42, 43)


def test_split_cells_group_by_memid_and_order_by_portid() -> None:
    w1 = _memwr("w1", WCLK2, (12, 13), (23, 24, 25), (31, 31, 31))
    w1 = Cell(
        name=w1.name,
        type=w1.type,
        connections=w1.connections,
        parameters={**w1.parameters, "PORTID": _bin(1)},
    )
    w0 = _memwr("w0", WCLK, (10, 11), (20, 21, 22), (30, 30, 30))
    other = Cell(
        name="o",
        type="$memrd",
        connections={"CLK": ("x",), "EN": ("x",), "ADDR": (1,), "DATA": (99,)},
        parameters={"MEMID": "\\other", "CLK_ENABLE": _bin(0)},
    )
    init = Cell(
        name="i",
        type="$meminit_v2",
        connections={"ADDR": ("0",), "DATA": ("1",), "EN": ("1",)},
        parameters={"MEMID": "\\mem"},
    )
    mems = find_memories(
        _module([w1, w0, _memrd("r", (40, 41), (50, 51, 52)), other, init])
    )
    assert [m.memid for m in mems] == ["mem", "other"]
    assert [p.cell for p in mems[0].write_ports] == ["w0", "w1"]
    assert [p.endpoint for p in mems[0].read_ports] == ["r"]
    assert mems[1].read_ports[0].clocked is False


def test_display_memid_strips_yosys_escapes() -> None:
    assert display_memid("\\mem") == "mem"
    assert display_memid("$flatten\\u_fifo.\\mem") == "u_fifo.mem"


def test_no_memory_is_a_no_op() -> None:
    m = _module([_dff("f", WCLK, (7,), (8,))])
    assert find_memories(m) == ()
    view = crossing_view(m, ())
    assert view.module is m and view.port_flops == ()
    assert memory_crossings(m, [], _spec()) == []


def test_meminit_alone_is_not_a_memory() -> None:
    init = Cell(
        name="i",
        type="$meminit_v2",
        connections={"ADDR": ("0",), "DATA": ("1",), "EN": ("1",)},
        parameters={"MEMID": "\\mem"},
    )
    assert find_memories(_module([init])) == ()


# --- the crossing view ------------------------------------------------------


def test_view_turns_ports_into_virtual_flops_and_async_reads_into_transit() -> None:
    m = _module([_packed_mem()])
    view = crossing_view(m, find_memories(m))
    assert "mem" not in view.module.cells
    by_name = {f.cell.name: f for f, _ in view.port_flops}
    assert set(by_name) == {"mem[wr0]", "mem[wr1]", "mem[rd0]"}
    assert all(f.cell.type == MEM_PORT_TYPE for f in by_name.values())
    # Write port: address + data + enable are the sink, nothing is a source.
    assert by_name["mem[wr0]"].d == (10, 11, 20, 21, 22, 30, 30, 30)
    assert by_name["mem[wr0]"].q == ()
    # Clocked read port: address + enable in, read data out.
    assert by_name["mem[rd0]"].d == (40, 41, 60)
    assert by_name["mem[rd0]"].q == (50, 51, 52)
    transit = view.module.cells["mem[rd1]"]
    assert transit.connections == {"A": (42, 43), "Y": (53, 54, 55)}


def test_unclocked_write_port_has_no_endpoint() -> None:
    m = _module([_memwr("w", "x", (10,), (20,), (30,), clocked=False)])
    view = crossing_view(m, find_memories(m))
    assert view.port_flops == ()
    assert memory_crossings(m, [], _spec()) == []


def test_foreign_address_into_clocked_read_port_is_a_crossing() -> None:
    """A clocked read port is a sink (address) and a source (data)."""
    m = _module(
        [
            _memwr("w", RCLK, (10, 11), (20,), (30,)),
            _dff("waddr_q", WCLK, (1, 1), (40, 41)),
            _memrd("r", (40, 41), (50,), clk=RCLK),
            _dff("cap", WCLK2, (50,), (70,)),
        ]
    )
    crossings = find_crossings(m)
    pairs = {(c.src_name, c.dst_flop.name, c.src_clock, c.dst_clock) for c in crossings}
    assert ("waddr_q", "r", "wclk", "rclk") in pairs
    assert ("r", "cap", "rclk", "wclk2") in pairs
    # Same clock on both ports: no memory crossing, and the rule pack
    # runs cleanly over crossings whose endpoints are virtual flops.
    spec = _spec()
    assert memory_crossings(m, crossings, spec) == []
    ids = {v.rule_id for v in run_all(m, filter_async(crossings, spec), spec)}
    assert "CDC-026" not in ids
    assert {"CDC-001", "CDC-004"} & ids


def test_foreign_address_passes_through_an_async_read_port() -> None:
    m = _module(
        [
            _memwr("w", RCLK, (10, 11), (20, 21), (30, 30)),
            _dff("raddr_q", WCLK, (1, 1), (40, 41)),
            _memrd("r", (40, 41), (50, 51), v2=False),
            _dff("cap", RCLK, (50, 51), (70, 71)),
        ]
    )
    (c,) = [c for c in find_crossings(m) if c.dst_flop.name == "cap"]
    assert (c.src_name, c.src_clock, c.dst_clock) == ("raddr_q", "wclk", "rclk")
    assert c.min_hops == 1 and c.width == 2


# --- memory crossings / CDC-026 ---------------------------------------------


def _two_writer_memory(*, sync_wclk: bool) -> Module:
    """``$mem_v2`` written on wclk + wclk2; read port 0 clocked on rclk2,
    read port 1 asynchronous and captured on rclk. With ``sync_wclk`` a
    2FF synchroniser from wclk into rclk drives read port 1's address."""
    cells = [
        _packed_mem(),
        _dff("cap", RCLK, (53, 54, 55), (80, 81, 82)),
    ]
    if sync_wclk:
        cells += [
            _dff("src_q", WCLK, (1,), (90,)),
            _dff("meta_q", RCLK, (90,), (91,)),
            _dff("sync_q", RCLK, (91,), (42,)),
            _dff("rptr_q", RCLK, (1,), (43,)),
        ]
    return _module(cells)


def test_multi_port_memory_crossings_per_read_port_and_write_clock() -> None:
    m = _two_writer_memory(sync_wclk=False)
    spec = _spec()
    records = memory_crossings(m, filter_async(find_crossings(m), spec), spec)
    got = sorted((r.read_port, r.read_clock, r.write_clock) for r in records)
    assert got == [
        ("mem[rd0]", "rclk2", "wclk"),
        ("mem[rd0]", "rclk2", "wclk2"),
        ("mem[rd1]", "rclk", "wclk"),
        ("mem[rd1]", "rclk", "wclk2"),
    ]
    assert all(r.async_per_sdc and r.control == () for r in records)
    assert all(r.read_cell == "mem" for r in records)
    violations = check_cdc_026(m, [], spec)
    assert len(violations) == 2  # one per (memory, read clock)
    assert all("`wclk`, `wclk2`" in v.message for v in violations)
    assert all(v.cell_name == "mem" for v in violations)


def test_control_from_one_write_clock_only_covers_that_clock() -> None:
    m = _two_writer_memory(sync_wclk=True)
    spec = _spec()
    async_c = filter_async(find_crossings(m), spec)
    records = {
        (r.read_port, r.write_clock): r for r in memory_crossings(m, async_c, spec)
    }
    covered = records[("mem[rd1]", "wclk")]
    assert [c.dst_flop.name for c in covered.control] == ["meta_q"]
    assert records[("mem[rd1]", "wclk2")].control == ()
    rd1 = [v for v in check_cdc_026(m, async_c, spec) if "`rclk`" in v.message]
    assert len(rd1) == 1
    assert "`wclk2`" in rd1[0].message and "`wclk`," not in rd1[0].message


def test_memory_synchronizer_is_fifo_when_the_control_is_synchronised() -> None:
    m = _two_writer_memory(sync_wclk=True)
    spec = _spec()
    async_c = filter_async(find_crossings(m), spec)
    ctx = _build_context(m, spec)
    by_key = {
        (r.read_port, r.write_clock): r for r in memory_crossings(m, async_c, spec)
    }
    assert memory_synchronizer(m, by_key[("mem[rd1]", "wclk")], ctx) == "fifo"
    assert memory_synchronizer(m, by_key[("mem[rd1]", "wclk2")], ctx) is None


def test_fwft_read_domain_falls_back_to_the_address() -> None:
    """An async read whose data reaches no flop is read in the domain its
    address is computed in — a flop, or an SDC-typed input port."""
    flop_addr = _module(
        [
            _memwr("w", WCLK, (10,), (20,), (30,)),
            _dff("rptr_q", RCLK, (1,), (40,)),
            _memrd("r", (40,), (50,)),
        ]
    )
    spec = _spec()
    (rec,) = memory_crossings(flop_addr, [], spec)
    assert (rec.write_clock, rec.read_clock) == ("wclk", "rclk")
    assert [v.rule_id for v in check_cdc_026(flop_addr, [], spec)] == ["CDC-026"]

    port_addr = _module(
        [_memwr("w", WCLK, (10,), (20,), (30,)), _memrd("r", (40,), (50,))],
        {"raddr": Port(name="raddr", direction="input", bits=(40,))},
    )
    typed = _spec("set_input_delay -clock rclk 1.0 [get_ports raddr]")
    (rec,) = memory_crossings(port_addr, [], typed)
    assert rec.read_clock == "rclk"
    # Untyped address port: no read domain, no record (like a
    # domain-unknown flop) — never a guessed one.
    assert memory_crossings(port_addr, [], _spec()) == []


def test_synchronous_clocks_are_recorded_but_not_reported() -> None:
    m = _module(
        [
            _memwr("w", WCLK, (10,), (20,), (30,)),
            _memrd("r", (40,), (50,), clk=RCLK),
        ]
    )
    spec = sdc_mod.parse(
        "create_clock -name wclk -period 10 [get_ports wclk]\n"
        "create_clock -name rclk -period 10 [get_ports rclk]\n",
        backend="tokenizer",
    )
    (rec,) = memory_crossings(m, [], spec)
    assert not rec.async_per_sdc
    assert check_cdc_026(m, [], spec) == []
    assert check_cdc_026(m, [], None) == []


def test_cdc_026_waivable_by_memory_name() -> None:
    m = _module(
        [
            _memwr("w", WCLK, (10,), (20,), (30,)),
            _memrd("r", (40,), (50,), clk=RCLK),
        ]
    )
    violations = check_cdc_026(m, [], _spec())
    kept, suppressed = waivers.apply(
        violations, waivers.parse("waive CDC-026 .*`mem`.* config table\n")
    )
    assert kept == [] and len(suppressed) == 1


# --- domain map ---------------------------------------------------------------


def _fixture(name: str):
    module = netlist.load(FIX / name / f"{name}.json")
    spec = sdc_mod.parse_file(FIX / name / f"{name}.sdc")
    crossings = find_crossings(
        module, port_clock=spec.port_clock, clock_for_port=spec.clock_for_port
    )
    return module, spec, crossings, filter_async(crossings, spec)


def _domain_map(name: str, *, with_findings: bool = True):
    module, spec, crossings, async_c = _fixture(name)
    findings = run_all(module, async_c, spec) if with_findings else None
    return build_domain_map(
        module, [], crossings, spec, async_crossings=async_c, findings=findings
    )


def test_domain_map_omits_memory_key_without_memories() -> None:
    assert "memory_crossings" not in _domain_map("good_2ff_sync")


def test_domain_map_memory_entries() -> None:
    good = _domain_map("good_async_fifo_inferred_ram")["memory_crossings"]
    assert len(good) == 2  # registered rd_data + FWFT rd_peek
    for e in good:
        assert (e["memory"], e["write_clock"], e["read_clock"]) == (
            "mem",
            "wr_clk",
            "rd_clk",
        )
        assert e["async_per_sdc"] is True
        assert e["synchronizer"] == "fifo"
        assert e["findings"] == []
        assert len(e["control"]) == 1

    (bad,) = _domain_map("bad_dualport_ram_crossing")["memory_crossings"]
    assert bad["control"] == [] and bad["synchronizer"] is None
    assert bad["findings"] == ["CDC-026"]
    unchecked = _domain_map("bad_dualport_ram_crossing", with_findings=False)
    assert unchecked["memory_crossings"][0]["findings"] is None

    assert _domain_map("good_same_clock_ram")["memory_crossings"] == []


# --- frontends ----------------------------------------------------------------

PYSLANG = importlib.util.find_spec("pyslang") is not None


def _rule_ids(module: Module, spec) -> list[str]:
    crossings = find_crossings(
        module, port_clock=spec.port_clock, clock_for_port=spec.clock_for_port
    )
    return sorted(
        v.rule_id for v in run_all(module, filter_async(crossings, spec), spec)
    )


@pytest.mark.skipif(not PYSLANG, reason="pyslang not installed")
@pytest.mark.parametrize("name", MEMORY_FIXTURES)
def test_slang_and_yosys_agree_on_memory_fixtures(name: str) -> None:
    from rtl_buddy_cdc.frontend import Frontend, elaborate

    spec = sdc_mod.parse_file(FIX / name / f"{name}.sdc")
    yosys_ids = _rule_ids(netlist.load(FIX / name / f"{name}.json"), spec)
    slang_module = elaborate([FIX / name / f"{name}.sv"], name, frontend=Frontend.slang)
    assert _rule_ids(slang_module, spec) == yosys_ids
    yosys_mems = find_memories(netlist.load(FIX / name / f"{name}.json"))
    slang_mems = find_memories(slang_module)
    assert [(m.memid, len(m.write_ports), len(m.read_ports)) for m in slang_mems] == [
        (m.memid, len(m.write_ports), len(m.read_ports)) for m in yosys_mems
    ]


@pytest.mark.skipif(not PYSLANG, reason="pyslang not installed")
def test_slang_lowers_dynamic_unpacked_selects_to_memory_ports(tmp_path: Path) -> None:
    from rtl_buddy_cdc.frontend import Frontend, elaborate

    sv = tmp_path / "m.sv"
    sv.write_text(
        "module m(input logic clk, input logic en, input logic [1:0] a,\n"
        "         input logic [3:0] d, output logic [3:0] q, output logic [3:0] k);\n"
        "  logic [3:0] mem [4];\n"
        "  always_ff @(negedge clk) if (en) mem[a] <= d;\n"
        "  assign q = mem[a];\n"
        "  assign k = mem[1];\n"  # constant index: a plain bit select
        "endmodule\n"
    )
    module = elaborate([sv], "m", frontend=Frontend.slang)
    types = sorted(c.type for c in module.cells.values())
    assert types == ["$memrd_v2", "$memwr_v2"]
    (mem,) = find_memories(module)
    (wp,) = mem.write_ports
    (rp,) = mem.read_ports
    assert mem.memid == "mem"
    assert wp.clocked and len(wp.addr) == 2 and len(wp.data) == 4
    assert len(set(wp.en)) == 1 and isinstance(wp.en[0], int)  # the `en` bit
    wcell = module.cells[wp.cell]
    assert wcell.parameters["CLK_POLARITY"] == "0"
    assert not rp.clocked and rp.addr == wp.addr
    assert module.ports["q"].bits == rp.data


@pytest.mark.skipif(not PYSLANG, reason="pyslang not installed")
def test_slang_resolves_a_net_read_before_its_continuous_assign(
    tmp_path: Path,
) -> None:
    """``if (!flag)`` above ``assign flag = …`` must read the assign's
    driver, not the net's placeholder bits (the async-FIFO flags)."""
    from rtl_buddy_cdc.frontend import Frontend, elaborate

    sv = tmp_path / "f.sv"
    sv.write_text(
        "module f(input logic clk, input logic a, input logic b, output logic q);\n"
        "  logic flag;\n"
        "  always_ff @(posedge clk) if (!flag) q <= a;\n"
        "  assign flag = a & b;\n"
        "endmodule\n"
    )
    module = elaborate([sv], "f", frontend=Frontend.slang)
    driven = {
        b
        for c in module.cells.values()
        for b in c.connections.get("Y", ()) + c.connections.get("Q", ())
    }
    driven |= {b for p in module.ports.values() for b in p.bits}
    for c in module.cells.values():
        for pin, bits in c.connections.items():
            if pin in ("Y", "Q"):
                continue
            for b in bits:
                assert not isinstance(b, int) or b in driven, (c.type, pin, b)


def test_cdc_026_stays_off_data_crossings_through_the_read_port() -> None:
    """A clocked read port is also a flop-like source; CDC-026 anchored on
    its cell belongs to the memory entry, not to the read-data crossing."""
    m = _module(
        [
            _memwr("w", WCLK, (10,), (20,), (30,)),
            _memrd("r", (40,), (50,), clk=RCLK),
            _dff("cap", RCLK2, (50,), (70,)),
        ]
    )
    spec = _spec()
    crossings = find_crossings(m)
    async_c = filter_async(crossings, spec)
    findings = run_all(m, async_c, spec)
    assert "CDC-026" in {v.rule_id for v in findings}
    payload = build_domain_map(
        m, [], crossings, spec, async_crossings=async_c, findings=findings
    )
    (data,) = [c for c in payload["crossings"] if c["dst_flop"].endswith("cap")]
    assert "CDC-026" not in data["findings"]
    (mem,) = payload["memory_crossings"]
    assert mem["findings"] == ["CDC-026"]
