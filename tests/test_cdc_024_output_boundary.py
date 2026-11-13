"""CDC-024 — top-level output launched asynchronously to its contract (#323).

``set_output_delay -clock C [get_ports o]`` declares that the receiver
outside the design captures ``o`` on ``C``. The crossing model has no
sink for that capture, so an output launched from a domain asynchronous
to ``C`` used to produce no crossing, no finding and no diagnostic.

These tests drive the rule on hand-built netlists (the issue's own
reproducer shape plus the corners a fixture can't isolate cleanly) and
run every case under both SDC reader backends. The fixture pair
``bad_output_async_contract`` / ``good_output_sync_contract`` covers the
same ground end to end through the CLI.
"""

from __future__ import annotations

import pytest

from rtl_buddy_cdc import domain, rules, sdc
from rtl_buddy_cdc.netlist import Bit, Cell, Module, Netname, Port
from rtl_buddy_cdc.rules import check_cdc_024


@pytest.fixture(autouse=True)
def _every_sdc_backend(sdc_backend):
    """Run every test here once per SDC reader backend (#298)."""
    return sdc_backend


def _dff(name: str, clk: Bit, d: tuple[Bit, ...], q: tuple[Bit, ...]) -> Cell:
    return Cell(
        name,
        "$dff",
        {"CLK": (clk,), "D": d, "Q": q},
        {"WIDTH": str(len(q)), "CLK_POLARITY": "1"},
    )


def _gate(name: str, ctype: str, a: tuple[Bit, ...], b, y: tuple[Bit, ...]) -> Cell:
    conns: dict[str, tuple[Bit, ...]] = {"A": a, "Y": y}
    if b is not None:
        conns["B"] = b
    return Cell(name, ctype, conns)


_BASE_SDC = """
create_clock -name clk_a -period 10 [get_ports clk_a]
create_clock -name clk_b -period 13 [get_ports clk_b]
set_clock_groups -asynchronous -group {clk_a} -group {clk_b}
set_input_delay 0 -clock clk_a [get_ports d_in]
"""


def _run(module: Module, sdc_text: str):
    spec = sdc.parse(sdc_text)
    sdc.synthesize_unconstrained_inputs(spec, module)
    crossings = domain.find_crossings(
        module,
        port_clock=spec.port_clock,
        pin_clocks=spec.pin_clocks,
        clock_for_port=spec.clock_for_port,
    )
    async_crossings = domain.filter_async(crossings, spec)
    return crossings, rules.run_all(module, async_crossings, spec), spec


def _cdc_024(violations):
    return [v for v in violations if v.rule_id == "CDC-024"]


# --- the issue's reproducer ------------------------------------------------


def _repro(internal_receiver: bool = False) -> Module:
    """``always_ff @(posedge clk_a) q_out <= d_in;`` (issue #323)."""
    ports = {
        "clk_a": Port("clk_a", "input", (2,)),
        "clk_b": Port("clk_b", "input", (3,)),
        "d_in": Port("d_in", "input", (4,)),
        "q_out": Port("q_out", "output", (5,)),
    }
    cells = {"launch": _dff("launch", 2, (4,), (5,))}
    if internal_receiver:
        cells["capture"] = _dff("capture", 3, (5,), (6,))
        ports["captured"] = Port("captured", "output", (6,))
    return Module("output_boundary_demo", ports, cells, {})


def _contract(clock: str, target: str = "[get_ports q_out]") -> str:
    return _BASE_SDC + f"set_output_delay 0 -clock {clock} {target}\n"


def test_same_domain_contract_is_silent() -> None:
    crossings, violations, _ = _run(_repro(), _contract("clk_a"))
    assert crossings == []
    assert violations == []


@pytest.mark.parametrize(
    "target",
    ["[get_ports q_out]", "{q_out}", "q_out"],
    ids=["bracket", "brace", "bare"],
)
def test_async_contract_fires(target: str) -> None:
    crossings, violations, _ = _run(_repro(), _contract("clk_b", target))
    # Still no crossing — the capture is outside the netlist; the rule
    # walks the contract directly instead of faking a sink.
    assert crossings == []
    (v,) = violations
    assert v.rule_id == "CDC-024"
    assert v.severity == "warning"
    assert v.crossing is None
    assert v.cell_name == "launch"
    assert "top-level output `q_out` is launched from clk_a (flop `launch`)" in (
        v.message
    )
    assert "names receiving clock clk_b" in v.message
    assert "asynchronous to clk_a" in v.message


def test_internal_receiver_keeps_exactly_its_cdc_001() -> None:
    """The internal receiving flop is still exactly one CDC-001 —
    unchanged by CDC-024, which never adds a crossing. With a
    same-domain output contract it is the only finding; with the
    issue's async contract the external capture is reported too, by
    CDC-024, as a separate obligation."""
    crossings, violations, _ = _run(_repro(internal_receiver=True), _contract("clk_a"))
    assert len(crossings) == 1
    # The receiver's own output `captured` has no contract while `q_out`
    # does, so CDC-025's self-calibrating warning (#329) reports it too.
    assert sorted(v.rule_id for v in violations) == ["CDC-001", "CDC-025"]
    cdc_001 = violations[[v.rule_id for v in violations].index("CDC-001")]

    crossings_b, violations_b, _ = _run(
        _repro(internal_receiver=True), _contract("clk_b")
    )
    assert crossings_b == crossings
    assert sorted(v.rule_id for v in violations_b) == [
        "CDC-001",
        "CDC-024",
        "CDC-025",
    ]
    assert [v for v in violations_b if v.rule_id == "CDC-001"] == [cdc_001]


# --- related clocks: different names are not a finding ---------------------


def test_generated_clock_of_the_launch_clock_is_silent() -> None:
    sdc_text = _BASE_SDC + (
        "create_generated_clock -name gclk -master_clock clk_a "
        "-source [get_ports clk_a] -divide_by 2 [get_ports q_out]\n"
        "set_output_delay 0 -clock gclk [get_ports q_out]\n"
    )
    _, violations, _ = _run(_repro(), sdc_text)
    assert _cdc_024(violations) == []


def test_clock_never_declared_async_is_silent() -> None:
    """A virtual clock outside every async group is synchronous by the
    tool's conservative default — same answer the crossing filter gives."""
    sdc_text = _BASE_SDC + (
        "create_clock -name vclk -period 7\n"
        "set_output_delay 0 -clock vclk [get_ports q_out]\n"
    )
    _, violations, _ = _run(_repro(), sdc_text)
    assert violations == []


def test_explicit_async_override_on_a_generated_clock_fires() -> None:
    """Same machinery as the crossing filter: an explicit
    ``set_clock_groups -asynchronous`` naming a generated clock beats the
    generated→master collapse."""
    sdc_text = (
        "create_clock -name clk_a -period 10 [get_ports clk_a]\n"
        "create_generated_clock -name gclk -master_clock clk_a "
        "-source [get_ports clk_a] -divide_by 2 [get_ports clk_b]\n"
        "set_clock_groups -asynchronous -group {clk_a} -group {gclk}\n"
        "set_input_delay 0 -clock clk_a [get_ports d_in]\n"
        "set_output_delay 0 -clock gclk [get_ports q_out]\n"
    )
    _, violations, _ = _run(_repro(), sdc_text)
    (v,) = _cdc_024(violations)
    assert "names receiving clock gclk" in v.message


def test_false_path_pair_counts_as_async() -> None:
    sdc_text = (
        "create_clock -name clk_a -period 10 [get_ports clk_a]\n"
        "create_clock -name vclk -period 7\n"
        "set_false_path -from [get_clocks clk_a] -to [get_clocks vclk]\n"
        "set_input_delay 0 -clock clk_a [get_ports d_in]\n"
        "set_output_delay 0 -clock vclk [get_ports q_out]\n"
    )
    _, violations, _ = _run(_repro(), sdc_text)
    assert [v.rule_id for v in violations] == ["CDC-024"]


def test_exclusive_clocks_are_unreachable_not_async() -> None:
    sdc_text = (
        _contract("clk_b")
        + "set_clock_groups -logically_exclusive -group {clk_a} -group {clk_b}\n"
    )
    _, violations, _ = _run(_repro(), sdc_text)
    assert violations == []


def test_canonical_clock_name_is_reported() -> None:
    """The launch domain is the SDC clock name, not the raw port the
    clock trace stopped on (#166 normalisation, as in the crossings)."""
    sdc_text = (
        "create_clock -name sys -period 10 [get_ports clk_a]\n"
        "create_clock -name vclk -period 7\n"
        "set_clock_groups -asynchronous -group {sys} -group {vclk}\n"
        "set_input_delay 0 -clock sys [get_ports d_in]\n"
        "set_output_delay 0 -clock vclk [get_ports q_out]\n"
    )
    _, violations, _ = _run(_repro(), sdc_text)
    (v,) = _cdc_024(violations)
    assert "launched from sys (flop `launch`)" in v.message


def test_pin_declared_generated_clock_is_the_launch_domain() -> None:
    """A flop behind a ``create_generated_clock [get_pins …]`` launches
    in that generated clock — the domain ``find_crossings`` gives it —
    not in the master the plain rule-context trace would collapse to."""
    module = Module(
        "pin_gen",
        {
            "clk_a": Port("clk_a", "input", (2,)),
            "d_in": Port("d_in", "input", (4,)),
            "q_out": Port("q_out", "output", (5,)),
        },
        {
            "u_div": _gate("u_div", "$not", (2,), None, (7,)),
            "launch": _dff("launch", 7, (4,), (5,)),
        },
        {"u_gen.clk_out": Netname("u_gen.clk_out", (7,), {})},
    )
    sdc_text = (
        "create_clock -name clk_a -period 10 [get_ports clk_a]\n"
        "create_generated_clock -name gclk -master_clock clk_a "
        "-source [get_ports clk_a] -divide_by 1 [get_pins u_gen/clk_out]\n"
        "set_clock_groups -asynchronous -group {clk_a} -group {gclk}\n"
        "set_input_delay 0 -clock gclk [get_ports d_in]\n"
        "set_output_delay 0 -clock clk_a [get_ports q_out]\n"
    )
    _, violations, _ = _run(module, sdc_text)
    (v,) = _cdc_024(violations)
    assert "launched from gclk (flop `launch`)" in v.message


# --- cone shapes -----------------------------------------------------------


def _two_domain_cone() -> Module:
    """q_out = ra (clk_a) ^ rc (clk_c), contracted to clk_b."""
    return Module(
        "cone",
        {
            "clk_a": Port("clk_a", "input", (2,)),
            "clk_b": Port("clk_b", "input", (3,)),
            "clk_c": Port("clk_c", "input", (8,)),
            "d_in": Port("d_in", "input", (4,)),
            "q_out": Port("q_out", "output", (5,)),
        },
        {
            "ra": _dff("ra", 2, (4,), (10,)),
            "rc": _dff("rc", 8, (4,), (11,)),
            "x": _gate("x", "$xor", (10,), (11,), (5,)),
        },
        {},
    )


_THREE_CLOCK_SDC = (
    "create_clock -name clk_a -period 10 [get_ports clk_a]\n"
    "create_clock -name clk_b -period 13 [get_ports clk_b]\n"
    "create_clock -name clk_c -period 17 [get_ports clk_c]\n"
    "set_input_delay 0 -clock clk_a [get_ports d_in]\n"
    "set_output_delay 0 -clock clk_b [get_ports q_out]\n"
)


def test_combinational_cone_from_two_async_domains_is_one_finding() -> None:
    sdc_text = (
        _THREE_CLOCK_SDC
        + "set_clock_groups -asynchronous -group {clk_a} -group {clk_b} -group {clk_c}\n"
    )
    _, violations, _ = _run(_two_domain_cone(), sdc_text)
    (v,) = _cdc_024(violations)
    assert "launched from clk_a (flop `ra`); clk_c (flop `rc`)" in v.message
    assert "asynchronous to clk_a, clk_c" in v.message
    # Anchored on the cell that drives the output, not on a launcher.
    assert v.cell_name == "x"


def test_only_the_async_launcher_is_named() -> None:
    """clk_c is synchronous to clk_b here (never grouped apart), so only
    the clk_a leg of the cone is reported."""
    sdc_text = (
        _THREE_CLOCK_SDC
        + "set_clock_groups -asynchronous -group {clk_a} -group {clk_b clk_c}\n"
    )
    _, violations, _ = _run(_two_domain_cone(), sdc_text)
    (v,) = _cdc_024(violations)
    assert "launched from clk_a (flop `ra`) but" in v.message
    assert "rc" not in v.message.split(" but ")[0]


def test_many_launchers_are_sampled() -> None:
    ports = {
        "clk_a": Port("clk_a", "input", (2,)),
        "clk_b": Port("clk_b", "input", (3,)),
        "d_in": Port("d_in", "input", (4,)),
        "q_out": Port("q_out", "output", (5,)),
    }
    cells: dict[str, Cell] = {}
    for i in range(5):
        cells[f"r{i}"] = _dff(f"r{i}", 2, (4,), (20 + i,))
    cells["red"] = Cell(
        "red", "$reduce_xor", {"A": tuple(20 + i for i in range(5)), "Y": (5,)}
    )
    _, violations, _ = _run(Module("many", ports, cells, {}), _contract("clk_b"))
    (v,) = _cdc_024(violations)
    assert "flop `r0`, flop `r1`, flop `r2`, +2 more" in v.message


def _vector(cell_type: str) -> Module:
    """q_out[1:0] = op(ra[1:0] (clk_a), rb[1:0] (clk_b)); op lane-aligned
    or not, so the bit attribution follows the crossing walk's lanes."""
    return Module(
        "vec",
        {
            "clk_a": Port("clk_a", "input", (2,)),
            "clk_b": Port("clk_b", "input", (3,)),
            "d_in": Port("d_in", "input", (4,)),
            "q_out": Port("q_out", "output", (30, 31)),
        },
        {
            "ra": _dff("ra", 2, (4, 4), (10, 11)),
            "rb": _dff("rb", 3, (4, 4), (12, 13)),
            "op": _gate("op", cell_type, (10, 12), (13, 13), (30, 31)),
        },
        {},
    )


@pytest.mark.parametrize(
    "cell_type,expected",
    [
        # Lane-aligned: Y[0] = ra[0] & rb[1], Y[1] = rb[0] & rb[1] — only
        # bit 0 sees a clk_a lane.
        ("$and", "(1 of 2 bits)"),
        # Arithmetic: every output bit depends on every input bit, so the
        # clk_a lane reaches both.
        ("$add", "(2 of 2 bits)"),
    ],
)
def test_vector_bits_are_attributed(cell_type: str, expected: str) -> None:
    _, violations, _ = _run(_vector(cell_type), _contract("clk_b"))
    (v,) = _cdc_024(violations)
    assert f"`q_out` {expected}" in v.message


def test_partial_vector_reports_only_the_async_bits() -> None:
    """q_out = {ra (clk_a), rb (clk_b)} with a 0 constant on top: the
    contract is clk_b, so 1 of the 3 bits is asynchronous."""
    module = Module(
        "partial",
        {
            "clk_a": Port("clk_a", "input", (2,)),
            "clk_b": Port("clk_b", "input", (3,)),
            "d_in": Port("d_in", "input", (4,)),
            "q_out": Port("q_out", "output", (11, 10, "0")),
        },
        {
            "ra": _dff("ra", 2, (4,), (10,)),
            "rb": _dff("rb", 3, (4,), (11,)),
        },
        {},
    )
    _, violations, _ = _run(module, _contract("clk_b"))
    (v,) = _cdc_024(violations)
    assert "`q_out` (1 of 3 bits)" in v.message
    assert v.cell_name == "ra"


def test_typed_input_feedthrough_fires() -> None:
    """A combinational input→output path launches in the input's typed
    clock; contracting the output to an async clock is the same hazard."""
    module = Module(
        "feed",
        {
            "clk_a": Port("clk_a", "input", (2,)),
            "clk_b": Port("clk_b", "input", (3,)),
            "d_in": Port("d_in", "input", (4,)),
            "q_out": Port("q_out", "output", (5,)),
        },
        {"inv": _gate("inv", "$not", (4,), None, (5,))},
        {},
    )
    _, violations, _ = _run(module, _contract("clk_b"))
    (v,) = _cdc_024(violations)
    assert "launched from clk_a (input `d_in`)" in v.message
    assert v.cell_name == "inv"

    # The same feed-through contracted to the input's own clock is fine.
    _, violations, _ = _run(module, _contract("clk_a"))
    assert violations == []


def test_wide_cell_fed_by_every_launcher_bit() -> None:
    """Both bits of a clk_a register enter one adder: every output bit
    is reached, and the adder is expanded once, not once per bit."""
    module = Module(
        "adder",
        {
            "clk_a": Port("clk_a", "input", (2,)),
            "clk_b": Port("clk_b", "input", (3,)),
            "d_in": Port("d_in", "input", (4,)),
            "q_out": Port("q_out", "output", (30, 31)),
        },
        {
            "ra": _dff("ra", 2, (4, 4), (10, 11)),
            "add": _gate("add", "$add", (10, 11), (4, 4), (30, 31)),
        },
        {},
    )
    _, violations, _ = _run(module, _contract("clk_b"))
    (v,) = _cdc_024(violations)
    assert "`q_out` (2 of 2 bits) is launched from clk_a" in v.message


def test_direct_input_to_output_wire_fires_without_an_anchor() -> None:
    module = Module(
        "wire",
        {
            "clk_a": Port("clk_a", "input", (2,)),
            "clk_b": Port("clk_b", "input", (3,)),
            "d_in": Port("d_in", "input", (4,)),
            "q_out": Port("q_out", "output", (4,)),
        },
        {},
        {},
    )
    _, violations, _ = _run(module, _contract("clk_b"))
    (v,) = _cdc_024(violations)
    assert "input `d_in`" in v.message
    assert v.cell_name is None


def test_untyped_input_feedthrough_is_not_a_launch_domain() -> None:
    """An input with no set_input_delay has no known launch clock (the
    ``<unconstrained>`` sentinel); CDC-011 owns untyped inputs."""
    module = Module(
        "feed",
        {
            "clk_b": Port("clk_b", "input", (3,)),
            "u_in": Port("u_in", "input", (4,)),
            "q_out": Port("q_out", "output", (5,)),
        },
        {"inv": _gate("inv", "$not", (4,), None, (5,))},
        {},
    )
    sdc_text = (
        "create_clock -name clk_b -period 13 [get_ports clk_b]\n"
        "set_output_delay 0 -clock clk_b [get_ports q_out]\n"
    )
    _, violations, _ = _run(module, sdc_text)
    assert violations == []


def test_unresolved_and_non_register_launchers_are_skipped() -> None:
    """A flop with no traceable clock is ``domain_unknown`` (already
    reported there) and a latch is not a recognised register: neither
    launches a CDC-024 finding."""
    module = Module(
        "unknown",
        {
            "clk_b": Port("clk_b", "input", (3,)),
            "q_out": Port("q_out", "output", (5,)),
            "q_lat": Port("q_lat", "output", (6,)),
        },
        {
            # CLK from an undriven internal net: domain unknown.
            "floating": _dff("floating", 99, (5,), (5,)),
            "lat": Cell("lat", "$dlatch", {"EN": (3,), "D": (6,), "Q": (6,)}),
        },
        {},
    )
    sdc_text = (
        "create_clock -name clk_b -period 13 [get_ports clk_b]\n"
        "create_clock -name vclk -period 7\n"
        "set_clock_groups -asynchronous -group {clk_b} -group {vclk}\n"
        "set_output_delay 0 -clock vclk [get_ports {q_out q_lat}]\n"
    )
    spec = sdc.parse(sdc_text)
    assert check_cdc_024(module, [], spec) == []


# --- scope -----------------------------------------------------------------


def test_outputs_without_a_contract_are_not_checked() -> None:
    _, violations, _ = _run(_repro(), _BASE_SDC)
    assert violations == []


def test_no_sdc_means_no_findings() -> None:
    assert check_cdc_024(_repro(), [], None) == []


def test_only_output_ports_are_checked() -> None:
    """An inout or an input named by set_output_delay is not an output
    contract (an input so typed is walked by the crossing model as
    before), and a sentinel-typed port has no real receiving clock."""
    module = Module(
        "dirs",
        {
            "clk_a": Port("clk_a", "input", (2,)),
            "d_in": Port("d_in", "input", (4,)),
            "io": Port("io", "inout", (5,)),
            "q_out": Port("q_out", "output", (6,)),
        },
        {
            "r": _dff("r", 2, (4,), (5,)),
            "s": _dff("s", 2, (4,), (6,)),
        },
        {},
    )
    spec = sdc.parse(
        "create_clock -name clk_a -period 10 [get_ports clk_a]\n"
        "create_clock -name vclk -period 7\n"
        "set_clock_groups -asynchronous -group {clk_a} -group {vclk}\n"
        "set_output_delay 0 -clock vclk [get_ports {io d_in missing}]\n"
    )
    spec.port_clock["q_out"] = sdc.UNCONSTRAINED_SENTINEL
    assert check_cdc_024(module, [], spec) == []


def test_standalone_call_builds_its_own_context() -> None:
    spec = sdc.parse(_contract("clk_b"))
    (v,) = check_cdc_024(_repro(), [], spec)
    assert v.rule_id == "CDC-024"


def test_registered_in_the_rule_pack() -> None:
    assert rules.RULES["CDC-024"] is check_cdc_024
