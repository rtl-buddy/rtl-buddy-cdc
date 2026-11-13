"""``set_case_analysis`` in clock-root tracing (rtl-buddy-cdc#325).

``set_case_analysis <0|1> [get_ports sel]`` pins a mode port to a
constant. A clock mux whose select is such a constant — directly or
through the buffers / inverters the tracer already looks through —
resolves to the selected leg, so the flops behind it get that one
clock's domain. ``rising`` / ``falling`` are recorded but are not
constants. Every test runs on both SDC reader backends.

The tracer unit tests build ``Module`` / ``Cell`` / ``Port`` netlists
by hand (no Yosys needed); the end-to-end tests drive the committed
``bad_`` / ``good_case_analysis_clock_mux`` fixture netlists.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rtl_buddy_cdc import netlist, sdc as sdc_mod
from rtl_buddy_cdc.cli import app
from rtl_buddy_cdc.domain import (
    _build_case_bits,
    assign_domains,
    case_analysis_warnings,
    filter_async,
    find_clock_combines,
    find_crossings,
    find_inferred_clock_candidates,
    find_scan_mode_flops,
    trace_clock_root,
)
from rtl_buddy_cdc.netlist import Cell, Module, Port
from rtl_buddy_cdc.rules import run_all as run_all_rules
from rtl_buddy_cdc.sdc import parse

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _every_sdc_backend(sdc_backend):
    return sdc_backend


# ---- parsing -----------------------------------------------------------------


@pytest.mark.parametrize(
    "target", ["[get_ports sel]", "{sel}", "sel"], ids=["bracket", "brace", "bare"]
)
def test_target_forms(target: str) -> None:
    spec = parse(f"set_case_analysis 1 {target}\n")
    assert spec.case_analysis == {"sel": "1"}
    assert spec.partial_warnings == []


@pytest.mark.parametrize(
    ("raw", "value"),
    [
        ("0", "0"),
        ("1", "1"),
        ("zero", "0"),
        ("one", "1"),
        ("logic_one", "1"),
        ("rising", "rising"),
        ("falling", "falling"),
    ],
)
def test_values_normalised(raw: str, value: str) -> None:
    assert parse(f"set_case_analysis {raw} [get_ports m]\n").case_analysis == {
        "m": value
    }


def test_multiple_targets_and_last_write_wins() -> None:
    spec = parse(
        "set_case_analysis 1 {a b}\n"
        "set_case_analysis 0 [get_ports c]\n"
        "set_case_analysis 0 [get_ports a]\n"
    )
    assert spec.case_analysis == {"b": "1", "c": "0", "a": "0"}
    assert list(spec.case_analysis) == ["b", "c", "a"]


def test_braced_bus_bit_kept_on_both_backends() -> None:
    spec = parse("set_case_analysis 0 [get_ports {mode[3]}]\n")
    assert spec.case_analysis == {"mode[3]": "0"}
    assert spec.partial_warnings == []


def test_unbraced_bus_bit(sdc_backend: str) -> None:
    """Tcl substitutes ``[3]``; the reader warns rather than staying quiet."""
    spec = parse("set_case_analysis 0 [get_ports mode[3]]\n")
    if sdc_backend == "tcl":
        assert spec.case_analysis == {"mode": "0"}
        assert any("unbraced bus index" in w for w in spec.partial_warnings)
    else:
        assert spec.case_analysis == {"mode[3]": "0"}
        assert spec.partial_warnings == []


def test_vendor_flags_inside_collection_are_skipped() -> None:
    spec = parse("set_case_analysis 1 [get_ports -quiet sel]\n")
    assert spec.case_analysis == {"sel": "1"}


def test_pin_target_is_reported_and_ignored() -> None:
    spec = parse("set_case_analysis 1 [get_pins u_mux/S]\n")
    assert spec.case_analysis == {}
    assert any("only top-level ports" in w for w in spec.partial_warnings)


@pytest.mark.parametrize(
    ("text", "needle"),
    [
        ("set_case_analysis 2 [get_ports m]\n", "unrecognised value"),
        ("set_case_analysis 1\n", "missing target"),
        ("set_case_analysis\n", "missing value"),
        ('set_case_analysis 1 [get_ports -filter "x"]\n', "-filter"),
    ],
)
def test_malformed_commands_warn(text: str, needle: str) -> None:
    spec = parse(text)
    assert spec.case_analysis == {}
    assert any(needle in w for w in spec.partial_warnings)


def test_recognised_command_is_not_a_tokenizer_skip(caplog) -> None:
    caplog.set_level(logging.WARNING, logger="rtl_buddy_cdc.sdc")
    parse("set_case_analysis 1 [get_ports sel]\n")
    assert not [r for r in caplog.records if "tokenizer_skipped" in r.getMessage()]


# ---- synthetic tracer ------------------------------------------------------------
#
# Net bits: 2 = clk_a, 3 = clk_b, 4 = sel; cells allocate from 10 upward.


def _port(name: str, bits: tuple, direction: str = "input") -> Port:
    return Port(name=name, direction=direction, bits=bits)


def _dff(name: str, clk: int, d: int, q: int) -> Cell:
    return Cell(
        name=name, type="$dff", connections={"CLK": (clk,), "D": (d,), "Q": (q,)}
    )


def _module(cells: list[Cell], extra_ports: dict[str, Port] | None = None) -> Module:
    ports = {
        "clk_a": _port("clk_a", (2,)),
        "clk_b": _port("clk_b", (3,)),
        "sel": _port("sel", (4,)),
        **(extra_ports or {}),
    }
    return Module(
        name="syn", ports=ports, cells={c.name: c for c in cells}, netnames={}
    )


def _mux(mux_type: str = "$mux", sel: int = 4) -> Module:
    return _module(
        [
            Cell(
                name="u_mux",
                type=mux_type,
                connections={"A": (2,), "B": (3,), "S": (sel,), "Y": (10,)},
            ),
            _dff("u_ff", 10, 11, 12),
        ]
    )


def _root(module: Module, case: dict[str, str] | None) -> str | None:
    return trace_clock_root(module, 10, case_bits=_build_case_bits(module, case))


@pytest.mark.parametrize("mux_type", ["$mux", "$_MUX_"])
def test_mux_resolves_to_selected_leg(mux_type: str) -> None:
    m = _mux(mux_type)
    assert _root(m, {"sel": "0"}) == "clk_a"
    assert _root(m, {"sel": "1"}) == "clk_b"


def test_unanalysed_mux_unchanged() -> None:
    """No case analysis: first-resolves-wins, for ``$mux`` and (since
    rtl-buddy-cdc#336 reads gate-level muxes) ``$_MUX_`` alike."""
    assert _root(_mux("$mux"), None) == "clk_a"
    assert _root(_mux("$mux"), {}) == "clk_a"
    assert _root(_mux("$_MUX_"), None) == "clk_a"


def test_rising_is_not_a_constant() -> None:
    assert _root(_mux("$mux"), {"sel": "rising"}) == "clk_a"


def test_case_on_unrelated_port_changes_nothing() -> None:
    m = _module(
        [
            Cell(
                name="u_mux",
                type="$mux",
                connections={"A": (2,), "B": (3,), "S": (5,), "Y": (10,)},
            ),
            _dff("u_ff", 10, 11, 12),
        ],
        {"other": _port("other", (5,))},
    )
    assert _root(m, {"sel": "1"}) == "clk_a"


@pytest.mark.parametrize(
    ("chain", "expect_for_1"),
    [
        (["$not"], "clk_a"),
        (["$_NOT_"], "clk_a"),
        (["$logic_not"], "clk_a"),
        (["$buf"], "clk_b"),
        (["$_BUF_", "$pos"], "clk_b"),
        (["$not", "$_NOT_"], "clk_b"),
        (["$reduce_bool"], "clk_b"),
    ],
)
def test_select_through_buffers_and_inverters(
    chain: list[str], expect_for_1: str
) -> None:
    cells: list[Cell] = []
    src = 4
    for i, ctype in enumerate(chain):
        out = 20 + i
        cells.append(
            Cell(name=f"u_b{i}", type=ctype, connections={"A": (src,), "Y": (out,)})
        )
        src = out
    cells += [
        Cell(
            name="u_mux",
            type="$mux",
            connections={"A": (2,), "B": (3,), "S": (src,), "Y": (10,)},
        ),
        _dff("u_ff", 10, 11, 12),
    ]
    m = _module(cells)
    assert _root(m, {"sel": "1"}) == expect_for_1
    other = "clk_a" if expect_for_1 == "clk_b" else "clk_b"
    assert _root(m, {"sel": "0"}) == other


def test_select_through_logic_is_not_resolved() -> None:
    """No general constant propagation: an $and on the select stops it."""
    m = _module(
        [
            Cell(
                name="u_and",
                type="$and",
                connections={"A": (4,), "B": (5,), "Y": (20,)},
            ),
            Cell(
                name="u_mux",
                type="$mux",
                connections={"A": (2,), "B": (3,), "S": (20,), "Y": (10,)},
            ),
            _dff("u_ff", 10, 11, 12),
        ],
        {"en": _port("en", (5,))},
    )
    assert _root(m, {"sel": "1", "en": "1"}) == "clk_a"


@pytest.mark.parametrize(
    "select_cells",
    [
        # Select tied to a literal constant: not a case-analysis bit.
        [],
        # Select driven by an internal net nothing drives.
        [Cell(name="u_b", type="$buf", connections={"A": (50,), "Y": (20,)})],
        # Reduction over a wide operand is not a buffer for the walk.
        [
            Cell(
                name="u_r",
                type="$reduce_bool",
                connections={"A": (4, 5), "Y": (20,)},
            )
        ],
        # Width-extended $not: the output lane has no matching input lane.
        [Cell(name="u_n", type="$not", connections={"A": (5,), "Y": (21, 20)})],
        # A chain longer than the select walk's hop budget.
        [
            Cell(
                name=f"u_c{i}",
                type="$buf",
                connections={"A": (4 if i == 0 else 99 + i,), "Y": (100 + i,)},
            )
            for i in range(20)
        ]
        + [Cell(name="u_last", type="$buf", connections={"A": (119,), "Y": (20,)})],
    ],
    ids=["literal", "undriven", "wide-reduce", "extended-not", "hop-budget"],
)
def test_select_walk_gives_up(select_cells: list[Cell]) -> None:
    sel = "1" if not select_cells else 20
    m = _module(
        [
            *select_cells,
            Cell(
                name="u_mux",
                type="$mux",
                connections={"A": (2,), "B": (3,), "S": (sel,), "Y": (10,)},
            ),
            _dff("u_ff", 10, 11, 12),
        ],
        {"other": _port("other", (5,))},
    )
    assert _root(m, {"sel": "1", "other": "1"}) == "clk_a"


def test_mux_without_select_is_not_resolved() -> None:
    m = _module(
        [
            Cell(
                name="u_mux",
                type="$mux",
                connections={"A": (2,), "B": (3,), "Y": (10,)},
            ),
            _dff("u_ff", 10, 11, 12),
        ]
    )
    assert _root(m, {"sel": "1"}) == "clk_a"


def test_pmux_one_hot() -> None:
    m = _module(
        [
            Cell(
                name="u_pmux",
                type="$pmux",
                connections={"A": (2,), "B": (3, 6), "S": (4, 5), "Y": (10,)},
            ),
            _dff("u_ff", 10, 11, 12),
        ],
        {"sel2": _port("sel2", (5,)), "clk_c": _port("clk_c", (6,))},
    )
    assert _root(m, {"sel": "0", "sel2": "0"}) == "clk_a"
    assert _root(m, {"sel": "1", "sel2": "0"}) == "clk_b"
    assert _root(m, {"sel": "0", "sel2": "1"}) == "clk_c"
    # Two hot selects: undefined, left to the un-analysed walk.
    assert _root(m, {"sel": "1", "sel2": "1"}) == "clk_a"
    # Only part of S constant: not resolved.
    assert _root(m, {"sel": "0"}) == "clk_a"


def test_wide_mux_is_lane_exact() -> None:
    m = _module(
        [
            Cell(
                name="u_mux",
                type="$mux",
                connections={"A": (2, 7), "B": (8, 3), "S": (4,), "Y": (9, 10)},
            ),
            _dff("u_ff", 10, 11, 12),
        ],
        {"clk_v": _port("clk_v", (7, 8))},
    )
    assert _root(m, {"sel": "1"}) == "clk_b"
    assert _root(m, {"sel": "0"}) == "clk_v"


def test_selected_leg_is_followed_exactly() -> None:
    """A selected constant leg leaves the flop unresolved — no fallback."""
    m = _module(
        [
            Cell(
                name="u_mux",
                type="$mux",
                connections={"A": (2,), "B": ("0",), "S": (4,), "Y": (10,)},
            ),
            _dff("u_ff", 10, 11, 12),
        ]
    )
    assert _root(m, {"sel": "1"}) is None
    assert _root(m, {"sel": "0"}) == "clk_a"


def test_bus_bit_target() -> None:
    m = _module(
        [
            Cell(
                name="u_mux",
                type="$mux",
                connections={"A": (2,), "B": (3,), "S": (31,), "Y": (10,)},
            ),
            _dff("u_ff", 10, 11, 12),
        ],
        {"mode": _port("mode", (30, 31, 32))},
    )
    assert _root(m, {"mode[1]": "1"}) == "clk_b"
    assert _root(m, {"mode[0]": "1"}) == "clk_a"
    # Whole bus, then one bit overridden: application order.
    assert _root(m, {"mode": "1", "mode[1]": "0"}) == "clk_a"


def test_case_analysis_warnings() -> None:
    m = _module([], {"mode": _port("mode", (30, 31)), "o": _port("o", (40,), "output")})
    warnings = case_analysis_warnings(
        m, {"sel": "1", "nope": "0", "mode[5]": "1", "o": "0", "mode[1]": "1"}
    )
    assert len(warnings) == 3
    assert "no such top-level port" in warnings[0]
    assert "out of range" in warnings[1]
    assert "output port" in warnings[2]
    assert case_analysis_warnings(m, None) == []


def test_assign_domains_and_crossings_take_case_analysis() -> None:
    m = _module(
        [_mux().cells["u_mux"], _dff("u_ff", 10, 11, 12), _dff("u_src", 2, 13, 11)]
    )
    clocks = {fd.flop.cell.name: fd.clock for fd in assign_domains(m)}
    assert clocks == {"u_ff": "clk_a", "u_src": "clk_a"}
    clocks = {
        fd.flop.cell.name: fd.clock
        for fd in assign_domains(m, case_analysis={"sel": "1"})
    }
    assert clocks == {"u_ff": "clk_b", "u_src": "clk_a"}
    assert find_crossings(m) == []
    (c,) = find_crossings(m, case_analysis={"sel": "1"})
    assert (c.src_clock, c.dst_clock) == ("clk_a", "clk_b")


def _combine_behind_mux() -> tuple[Module, dict[str, str]]:
    """Leg A of a clock mux is clk_a & clk_b (a combine); leg B is clk_b."""
    m = _module(
        [
            Cell(
                name="u_and",
                type="$and",
                connections={"A": (2,), "B": (3,), "Y": (20,)},
            ),
            Cell(
                name="u_mux",
                type="$mux",
                connections={"A": (20,), "B": (3,), "S": (4,), "Y": (10,)},
            ),
            _dff("u_ff", 10, 11, 12),
        ]
    )
    clocks = {"clk_a": "clk_a", "clk_b": "clk_b"}
    return m, clocks


def test_cdc_023_combine_follows_the_selected_leg() -> None:
    m, clocks = _combine_behind_mux()
    cfp = clocks.get
    # Unanalysed: leg A declines (recorded), leg B resolves.
    assert [c.cell for c in find_clock_combines(m, clock_for_port=cfp)] == ["u_and"]
    assert assign_domains(m, clock_for_port=cfp)[0].clock == "clk_b"
    # The mode selects the clean leg: the combine is unreachable.
    assert find_clock_combines(m, clock_for_port=cfp, case_analysis={"sel": "1"}) == []
    # The mode selects the combine: recorded, and the flop stays unknown.
    assert [
        c.cell
        for c in find_clock_combines(m, clock_for_port=cfp, case_analysis={"sel": "0"})
    ] == ["u_and"]
    assert (
        assign_domains(m, clock_for_port=cfp, case_analysis={"sel": "0"})[0].clock
        is None
    )


def test_case_resolved_select_is_not_a_clock_control() -> None:
    """A constant select steers nothing, so it is not a scan control."""
    m = _mux()
    assert find_scan_mode_flops(m, is_scan_control=lambda bits: True) == {"u_ff"}
    assert (
        find_scan_mode_flops(
            m, is_scan_control=lambda bits: True, case_analysis={"sel": "0"}
        )
        == set()
    )


# ---- fixtures -----------------------------------------------------------------


def _fixture(name: str) -> tuple[netlist.Module, str]:
    d = FIXTURES / name
    return netlist.load(d / f"{name}.json"), (d / f"{name}.sdc").read_text()


def _run(module: netlist.Module, text: str):
    spec = sdc_mod.parse(text)
    sdc_mod.synthesize_unconstrained_inputs(spec, module)
    crossings = find_crossings(
        module,
        port_clock=spec.port_clock,
        clock_for_port=spec.clock_for_port,
        case_analysis=spec.case_analysis,
    )
    async_crossings = filter_async(crossings, spec)
    return spec, async_crossings, run_all_rules(module, async_crossings, spec)


def test_bad_fixture_fires_cdc_001_only() -> None:
    """Select pinned to clk_b: the raw crossing is reported. CDC-010 stays
    silent — the case-analysed select comes from a port, not a flop."""
    module, text = _fixture("bad_case_analysis_clock_mux")
    _spec, async_crossings, violations = _run(module, text)
    assert len(async_crossings) == 1
    assert (async_crossings[0].src_clock, async_crossings[0].dst_clock) == (
        "clk_a",
        "clk_b",
    )
    assert {v.rule_id for v in violations} == {"CDC-001"}


@pytest.mark.parametrize(
    "replacement",
    ["set_case_analysis 0 [get_ports sel]", ""],
    ids=["select-clk_a", "no-case-analysis"],
)
def test_bad_fixture_clean_when_mux_passes_clk_a(replacement: str) -> None:
    """``sel = 0`` selects the source's own clock; without case analysis
    the tracer keeps today's first-leg answer (clk_a) — both report
    nothing."""
    module, text = _fixture("bad_case_analysis_clock_mux")
    text = text.replace("set_case_analysis 1 [get_ports sel]", replacement)
    _spec, async_crossings, violations = _run(module, text)
    assert async_crossings == []
    assert violations == []


def test_good_fixture_is_clean() -> None:
    """Dedicated rather than in ``test_good_fixtures.GOOD_FIXTURES``: that
    runner walks crossings without the SDC's case analysis."""
    module, text = _fixture("good_case_analysis_clock_mux")
    _spec, async_crossings, violations = _run(module, text)
    assert len(async_crossings) == 1
    assert violations == []


def test_inferred_clock_candidates_unaffected() -> None:
    module, text = _fixture("good_case_analysis_clock_mux")
    spec = sdc_mod.parse(text)
    assert (
        find_inferred_clock_candidates(
            module, pin_clocks=spec.pin_clocks, clock_for_port=spec.clock_for_port
        )
        == []
    )


def test_cli_domain_map_and_unresolved_warning(tmp_path: Path) -> None:
    d = FIXTURES / "bad_case_analysis_clock_mux"
    sdc_path = tmp_path / "x.sdc"
    sdc_path.write_text(
        (d / "bad_case_analysis_clock_mux.sdc").read_text()
        + "set_case_analysis 0 [get_ports no_such_port]\n"
    )
    out = tmp_path / "map.json"
    result = CliRunner().invoke(
        app,
        [
            "analyze",
            "--netlist",
            str(d / "bad_case_analysis_clock_mux.json"),
            "--sdc",
            str(sdc_path),
            "--emit-domain-map",
            str(out),
        ],
    )
    assert result.exit_code == 1, result.output
    assert "set_case_analysis 0 [get_ports no_such_port]: no such top-level port" in (
        result.output
    )
    payload = json.loads(out.read_text())
    assert payload["case_analysis"] == [
        {"port": "no_such_port", "value": "0"},
        {"port": "sel", "value": "1"},
    ]
    dst_clocks = {f["clock"] for f in payload["flop_domains"]}
    assert dst_clocks == {"clk_a", "clk_b"}
    (crossing,) = [c for c in payload["crossings"] if c["async_per_sdc"]]
    assert crossing["findings"] == ["CDC-001"]
