"""Object-scoped ``set_false_path`` / ``-through`` (rtl-buddy-cdc#328).

Three layers, each on both SDC backends where the parser is involved:

- the parser records object endpoints and ``-through`` groups without
  touching the clock-pair path;
- :class:`sdc_objects.NameIndex` maps SDC register / pin / port / net
  names onto a hand-built netlist whose names follow Yosys ``flatten``;
- :func:`sdc_objects.coverage` / :func:`sdc_objects.acknowledge` decide
  which crossings a statement covers and which findings it may suppress.
"""

from __future__ import annotations

import pytest

from rtl_buddy_cdc import sdc_objects
from rtl_buddy_cdc.domain import Crossing
from rtl_buddy_cdc.flops import Flop, find_flops
from rtl_buddy_cdc.netlist import Cell, Module, Netname, Port
from rtl_buddy_cdc.rules import Violation
from rtl_buddy_cdc.sdc import SdcObject, parse


@pytest.fixture(autouse=True)
def _both_backends(sdc_backend):
    return sdc_backend


CLOCKS = (
    "create_clock -name ck_a -period 10 [get_ports ck_a]\n"
    "create_clock -name ck_b -period 7 [get_ports ck_b]\n"
    "set_clock_groups -asynchronous -group {ck_a} -group {ck_b}\n"
)


# ---- parser -----------------------------------------------------------------


@pytest.mark.parametrize(
    "operand, expected",
    [
        ("[get_cells u_cfg/mode_q_reg]", SdcObject("cell", "u_cfg/mode_q_reg")),
        (
            "[get_cells {u_cfg/mode_q_reg[3]}]",
            SdcObject("cell", "u_cfg/mode_q_reg[3]"),
        ),
        ("[get_cells u_cfg/mode_q_reg*]", SdcObject("cell", "u_cfg/mode_q_reg*")),
        ("[get_pins u_cfg/en_q_reg/Q]", SdcObject("pin", "u_cfg/en_q_reg/Q")),
        ("[get_ports cfg_mode]", SdcObject("port", "cfg_mode")),
        ("[get_nets u_cfg/mode]", SdcObject("net", "u_cfg/mode")),
        (
            "[get_cells -hierarchical sync_reg*]",
            SdcObject("cell", "sync_reg*", hierarchical=True),
        ),
    ],
    ids=["bracket", "brace-bus-bit", "glob", "pin", "port", "net", "hier"],
)
def test_object_from_forms(operand: str, expected: SdcObject) -> None:
    spec = parse(f"{CLOCKS}set_false_path -from {operand} -to [get_clocks ck_b]\n")
    (exc,) = spec.false_path_exceptions
    assert exc.from_objects == (expected,)
    assert exc.to_clocks == ("ck_b",)
    assert spec.false_path_pairs == set()
    assert spec.partial_warnings == []


def test_braced_list_mixing_objects_and_clocks() -> None:
    spec = parse(
        f"{CLOCKS}set_false_path -from {{[get_cells u/a_reg] [get_clocks ck_a]}} "
        "-to [get_cells u/b_reg]\n"
    )
    (exc,) = spec.false_path_exceptions
    assert exc.from_objects == (SdcObject("cell", "u/a_reg"),)
    assert exc.from_clocks == ("ck_a",)
    assert exc.to_objects == (SdcObject("cell", "u/b_reg"),)


def test_mixed_endpoints_never_change_are_async() -> None:
    """``set_clock_groups`` says ck_a/ck_b are async; an object false path
    between them leaves that verdict — and an unrelated sync pair's —
    exactly as it was."""
    text = (
        "create_clock -name ck_a -period 10 [get_ports ck_a]\n"
        "create_clock -name ck_b -period 7 [get_ports ck_b]\n"
    )
    base = parse(text)
    spec = parse(
        text + "set_false_path -from [get_cells u/q_reg] -to [get_clocks ck_b]\n"
    )
    assert base.are_async("ck_a", "ck_b") is spec.are_async("ck_a", "ck_b") is False
    assert spec.false_path_pairs == set()


def test_through_groups_and_bare_through_names() -> None:
    spec = parse(
        f"{CLOCKS}set_false_path -through [get_nets u/n1] -through {{u/p1 u/p2}}\n"
    )
    (exc,) = spec.false_path_exceptions
    assert exc.through == (
        (SdcObject("net", "u/n1"),),
        (SdcObject("any", "u/p1"), SdcObject("any", "u/p2")),
    )
    assert exc.from_clocks == exc.to_clocks == ()


def test_edge_and_check_qualifiers_are_recorded() -> None:
    spec = parse(
        f"{CLOCKS}set_false_path -setup -rise_from [get_cells u/q_reg] "
        "-fall_to [get_clocks ck_b]\n"
    )
    (exc,) = spec.false_path_exceptions
    assert exc.qualifiers == ("-rise_from", "-fall_to", "-setup")
    assert exc.statement == (
        "set_false_path -setup -rise_from [get_cells u/q_reg] "
        "-fall_to [get_clocks ck_b]"
    )


def test_statement_is_backend_independent_and_braces_bus_bits(sdc_backend) -> None:
    spec = parse(
        f"{CLOCKS}set_false_path -from [get_cells {{u/q_reg[3]}}] "
        "-through [get_pins u/x/Y] -to [get_clocks ck_b]\n"
    )
    (exc,) = spec.false_path_exceptions
    assert exc.statement == (
        "set_false_path -from [get_cells {u/q_reg[3]}] "
        "-through [get_pins u/x/Y] -to [get_clocks ck_b]"
    )
    assert exc.line == (4 if sdc_backend == "tcl" else None)


def test_get_nets_endpoint_is_an_object_not_two_clocks() -> None:
    """Before #328 ``get_nets`` was not recognised and its head word was
    read as a clock name."""
    spec = parse(f"{CLOCKS}set_false_path -from [get_nets n] -to [get_clocks ck_b]\n")
    assert spec.false_path_pairs == set()
    (exc,) = spec.false_path_exceptions
    assert exc.from_objects == (SdcObject("net", "n"),)


def test_filter_in_object_false_path_drops_the_statement() -> None:
    """A dropped ``-filter`` would widen its side to "any object"."""
    spec = parse(
        f"{CLOCKS}set_false_path -from [get_cells -filter {{IS_SEQ}}] "
        "-to [get_cells u/b_reg]\n"
    )
    assert spec.false_path_exceptions == []
    assert any("-filter" in w for w in spec.partial_warnings)


def test_empty_through_drops_the_statement() -> None:
    spec = parse(
        f"{CLOCKS}set_false_path -from [get_cells u/a] -through [get_clocks ck_a]\n"
    )
    assert spec.false_path_exceptions == []
    assert any("-through" in w for w in spec.partial_warnings)


def test_clock_pair_path_is_unchanged() -> None:
    spec = parse(f"{CLOCKS}set_false_path -from {{ck_a}} -to ck_b\n")
    assert spec.false_path_pairs == {frozenset({"ck_a", "ck_b"})}
    assert spec.false_path_exceptions == []


# ---- a hand-built netlist with Yosys-flatten names -----------------------------
#
#   u_cfg.mode_q  : 4-bit $dff in ck_a  (cell $flatten\u_cfg.$procdff$1)
#                   aliases: u_cfg.mode (port of u_cfg), mode_w (parent wire)
#   u_cfg.g[0].en_q : 1-bit $dff in ck_a inside a generate scope
#   u_cfg.hi      : 2-bit $dff declared [5:4] (offset 4)
#   top_q         : 1-bit top-level $dff in ck_a, also named u_sub.d (a port
#                   of child instance u_sub, which has its own cell)
#   cap_q         : 4-bit $dff in ck_b capturing mode_q
#   en_cap        : 1-bit $dff in ck_b; D = (en_q & top_q) | ~en_q


def _dff(name: str, clk: int, d: list, q: list) -> Cell:
    return Cell(
        name=name,
        type="$dff",
        connections={"CLK": (clk,), "D": tuple(d), "Q": tuple(q)},
        parameters={"CLK_POLARITY": "1", "WIDTH": str(len(q))},
    )


def _module() -> Module:
    cells = [
        _dff("$flatten\\u_cfg.$procdff$1", 2, [10, 11, 12, 13], [20, 21, 22, 23]),
        _dff("$flatten\\u_cfg.$procdff$2", 2, [14], [24]),
        _dff("$flatten\\u_cfg.$procdff$6", 2, [15, 16], [50, 51]),
        _dff("$procdff$3", 2, [17], [25]),
        _dff("$procdff$4", 3, [20, 21, 22, 23], [30, 31, 32, 33]),
        _dff("$procdff$5", 3, [42], [41]),
        Cell("$and$7", "$and", {"A": (24,), "B": (25,), "Y": (40,)}),
        Cell("$not$8", "$not", {"A": (24,), "Y": (43,)}),
        Cell("$or$9", "$or", {"A": (40,), "B": (43,), "Y": (42,)}),
        Cell("$flatten\\u_sub.$not$10", "$not", {"A": (25,), "Y": (60,)}),
    ]
    nets = [
        Netname("ck_a", (2,)),
        Netname("ck_b", (3,)),
        Netname("cfg_in", (10, 11, 12, 13)),
        Netname("u_cfg.mode_q", (20, 21, 22, 23)),
        Netname("u_cfg.mode", (20, 21, 22, 23)),
        Netname("mode_w", (20, 21, 22, 23)),
        Netname("u_cfg.g[0].en_q", (24,)),
        Netname("u_cfg.hi", (50, 51), offset=4),
        Netname("top_q", (25,)),
        Netname("u_sub.d", (25,)),
        Netname("cap_q", (30, 31, 32, 33)),
        Netname("en_cap", (41,)),
        Netname("n_and", (40,)),
        Netname("n_or", (42,)),
        Netname("$auto$x", (43,)),
    ]
    ports = {
        "ck_a": Port("ck_a", "input", (2,)),
        "ck_b": Port("ck_b", "input", (3,)),
        "cfg_in": Port("cfg_in", "input", (10, 11, 12, 13)),
    }
    return Module(
        name="top",
        ports=ports,
        cells={c.name: c for c in cells},
        netnames={n.name: n for n in nets},
    )


MODE = "$flatten\\u_cfg.$procdff$1"
EN = "$flatten\\u_cfg.$procdff$2"
HI = "$flatten\\u_cfg.$procdff$6"
TOP = "$procdff$3"
CAP = "$procdff$4"
EN_CAP = "$procdff$5"


@pytest.mark.parametrize(
    "pattern, hier, expected",
    [
        ("u_cfg/mode_q", False, {MODE: {0, 1, 2, 3}}),
        ("u_cfg/mode_q_reg", False, {MODE: {0, 1, 2, 3}}),
        ("u_cfg/mode_q_reg[2]", False, {MODE: {2}}),
        ("u_cfg/mode_q[2]", False, {MODE: {2}}),
        ("u_cfg/mode_q_reg[*]", False, {MODE: {0, 1, 2, 3}}),
        ("u_cfg/mode_q_reg*", False, {MODE: {0, 1, 2, 3}}),
        ("u_cfg\\/mode_q_reg\\[1\\]", False, {MODE: {1}}),
        ("*/mode_q_reg", False, {MODE: {0, 1, 2, 3}}),
        ("*mode_q_reg", False, {}),  # a glob never crosses a level
        ("mode_q_reg", True, {MODE: {0, 1, 2, 3}}),
        # Same-scope port alias of the register: indexed too (documented).
        ("u_cfg/mode_reg", False, {MODE: {0, 1, 2, 3}}),
        # The parent's wire it drives through a port is not its name.
        ("mode_w_reg", False, {}),
        ("u_cfg/g[0]/en_q_reg", False, {EN: {0}}),
        ("u_cfg/hi_reg[5]", False, {HI: {1}}),
        ("u_cfg/hi_reg[0]", False, {}),
        ("top_q_reg", False, {TOP: {0}}),
        # A child instance's input port the flop feeds is not its name.
        ("u_sub/d_reg", False, {}),
        ("u_cfg/typo_reg", False, {}),
    ],
)
def test_register_name_mapping(pattern: str, hier: bool, expected: dict) -> None:
    index = sdc_objects.NameIndex(_module())
    assert index.registers(pattern, hier) == expected


def test_port_and_net_lookups() -> None:
    index = sdc_objects.NameIndex(_module())
    assert index.ports("cfg_in") == {"cfg_in": {0, 1, 2, 3}}
    assert index.ports("cfg_in[2]") == {"cfg_in": {2}}
    assert index.ports("cfg_*") == {"cfg_in": {0, 1, 2, 3}}
    assert index.net_bits("u_cfg/mode_q[1]") == {21}
    assert index.net_bits("n_and") == {40}
    assert index.net_bits("$auto$x") == set()  # internal names are not SDC names


def test_meaningful_cell_names_are_matched() -> None:
    """A gate-level netlist read back keeps the synthesis register name on
    the cell itself."""
    m = _module()
    cell = _dff("$flatten\\u_g.\\cnt_reg[0]", 2, [10], [70])
    m.cells[cell.name] = cell
    index = sdc_objects.NameIndex(m)
    assert index.registers("u_g/cnt_reg[0]") == {cell.name: {0}}


# ---- coverage + acknowledgement ----------------------------------------------


def _flops(m: Module) -> dict[str, Flop]:
    return {f.cell.name: f for f in find_flops(m)}


def _crossings(m: Module) -> list[Crossing]:
    f = _flops(m)
    return [
        Crossing(
            src_clock="ck_a",
            dst_flop=f[CAP],
            dst_clock="ck_b",
            min_hops=0,
            width=4,
            src_flop=f[MODE],
        ),
        Crossing(
            src_clock="ck_a",
            dst_flop=f[EN_CAP],
            dst_clock="ck_b",
            min_hops=1,
            width=1,
            src_flop=f[EN],
        ),
        Crossing(
            src_clock="ck_a",
            dst_flop=f[EN_CAP],
            dst_clock="ck_b",
            min_hops=2,
            width=1,
            src_flop=f[TOP],
        ),
    ]


def _cover(sdc_lines: str) -> tuple[dict, list[Crossing], sdc_objects.Resolution]:
    m = _module()
    spec = parse(CLOCKS + sdc_lines)
    res = sdc_objects.resolve(m, spec)
    xs = _crossings(m)
    return sdc_objects.coverage(m, xs, res, spec), xs, res


def _state(cov: dict, c: Crossing) -> str:
    entry = cov.get(sdc_objects.crossing_key(c))
    if entry is None:
        return "uncovered"
    return "acknowledged" if entry.acknowledged_by else "covered"


def test_source_object_with_clock_destination_acknowledges() -> None:
    cov, xs, res = _cover(
        "set_false_path -from [get_cells u_cfg/mode_q_reg*] -to [get_clocks ck_b]\n"
    )
    assert res.warnings == ()
    assert [_state(cov, c) for c in xs] == ["acknowledged", "uncovered", "uncovered"]


def test_per_bit_lines_cover_together_and_partial_does_not() -> None:
    lines = "".join(
        f"set_false_path -from [get_cells {{u_cfg/mode_q_reg[{i}]}}]\n"
        for i in range(3)
    )
    cov, xs, _ = _cover(lines)
    assert _state(cov, xs[0]) == "uncovered"  # bit 3 not named
    cov, xs, _ = _cover(
        lines + "set_false_path -from [get_cells {u_cfg/mode_q_reg[3]}]\n"
    )
    assert _state(cov, xs[0]) == "acknowledged"
    assert len(cov[sdc_objects.crossing_key(xs[0])].acknowledged_by) == 4


def test_destination_only_object_covers_but_does_not_acknowledge() -> None:
    """``-to sync_reg[0]`` marks a capture point; it says nothing about the
    source being static, so no finding is acknowledged."""
    cov, xs, _ = _cover("set_false_path -to [get_cells cap_q_reg]\n")
    assert _state(cov, xs[0]) == "covered"


def test_clock_source_with_object_destination_does_not_acknowledge() -> None:
    cov, xs, _ = _cover(
        "set_false_path -from [get_clocks ck_a] -to [get_cells cap_q_reg]\n"
    )
    assert _state(cov, xs[0]) == "covered"


def test_wrong_destination_does_not_cover() -> None:
    cov, xs, _ = _cover(
        "set_false_path -from [get_cells u_cfg/mode_q_reg] -to [get_cells en_cap_reg]\n"
    )
    assert _state(cov, xs[0]) == "uncovered"


def test_edge_or_check_qualified_false_path_does_not_acknowledge() -> None:
    for line in (
        "set_false_path -rise_from [get_cells u_cfg/mode_q_reg]\n",
        "set_false_path -setup -from [get_cells u_cfg/mode_q_reg]\n",
    ):
        cov, xs, _ = _cover(line)
        assert _state(cov, xs[0]) == "covered", line


def test_datapath_only_objects_associate_without_acknowledging() -> None:
    cov, xs, _ = _cover(
        "set_max_delay 2 -datapath_only -from [get_cells u_cfg/mode_q_reg] "
        "-to [get_cells cap_q_reg]\n"
    )
    entry = cov[sdc_objects.crossing_key(xs[0])]
    assert not entry.acknowledged_by
    assert [rc.command for rc in entry.constraints] == ["set_max_delay -datapath_only"]


def test_through_every_path_acknowledges() -> None:
    # en_q reaches en_cap's D via the AND and via the NOT; both pass n_or.
    cov, xs, _ = _cover("set_false_path -through [get_nets n_or]\n")
    assert [_state(cov, c) for c in xs] == ["uncovered", "acknowledged", "acknowledged"]


def test_through_some_paths_only_does_not_cover() -> None:
    # The NOT path from en_q bypasses n_and; top_q's only path uses it.
    cov, xs, _ = _cover("set_false_path -through [get_nets n_and]\n")
    assert [_state(cov, c) for c in xs] == ["uncovered", "uncovered", "acknowledged"]


def test_through_a_source_pin_cuts_every_path() -> None:
    cov, xs, _ = _cover("set_false_path -through [get_pins {u_cfg/g[0]/en_q_reg/Q}]\n")
    assert _state(cov, xs[1]) == "acknowledged"


def test_hierarchical_pin_through_resolves_to_a_net() -> None:
    """``u_cfg/mode`` is a pin of a flattened instance: its net."""
    cov, xs, res = _cover("set_false_path -through [get_pins u_cfg/mode]\n")
    assert res.warnings == ()
    assert _state(cov, xs[0]) == "acknowledged"


def test_unmatched_pattern_warns_with_its_name() -> None:
    _, _, res = _cover(
        "set_false_path -from [get_cells u_cfg/mode_typo_reg*] -to [get_clocks ck_b]\n"
        "set_false_path -through [get_nets nope]\n"
    )
    assert len(res.warnings) == 2
    assert "'u_cfg/mode_typo_reg*'" in res.warnings[0]
    assert "constraint typo" in res.warnings[0]
    assert "'nope'" in res.warnings[1]


def test_no_object_constraints_resolves_to_nothing() -> None:
    m = _module()
    spec = parse(CLOCKS + "set_false_path -from ck_a -to ck_b\n")
    res = sdc_objects.resolve(m, spec)
    assert res.constraints == () and res.warnings == ()
    assert sdc_objects.coverage(m, _crossings(m), res, spec) == {}


def _violation(rule: str, c: Crossing | None, cell: str | None = None) -> Violation:
    return Violation(
        rule_id=rule,
        severity="error",
        message=f"{rule} finding",
        crossing=c,
        cell_name=cell or (c.dst_flop.cell.name if c else None),
    )


def test_acknowledge_suppresses_static_class_only() -> None:
    cov, xs, _ = _cover("set_false_path -from [get_cells u_cfg/mode_q_reg]\n")
    m = _module()
    vs = [
        _violation("CDC-004", xs[0]),
        _violation("CDC-005", xs[0]),  # reconvergence: never acknowledged
        _violation("CDC-014", xs[0]),  # broken synchroniser: never
        _violation("CDC-001", xs[1]),  # different crossing: not covered
    ]
    kept, suppressed = sdc_objects.acknowledge(m, vs, xs, cov, sdc_file="x.sdc")
    assert [v.rule_id for v in kept] == ["CDC-005", "CDC-014", "CDC-001"]
    (s,) = suppressed
    assert s.violation.rule_id == "CDC-004"
    assert s.waiver.sdc is not None
    assert s.waiver.sdc.statement == "set_false_path -from [get_cells u_cfg/mode_q_reg]"
    assert s.waiver.sdc.file == "x.sdc"
    assert s.waiver.reason.startswith("SDC false path: ")


def test_acknowledge_group_rule_needs_every_lane() -> None:
    m = _module()
    f = _flops(m)
    lanes = [
        Crossing("ck_a", f[CAP], "ck_b", 0, 1, src_flop=f[MODE]),
        Crossing("ck_a", f[EN_CAP], "ck_b", 0, 1, src_flop=f[MODE]),
    ]
    v = _violation("CDC-020", None, cell=MODE)
    spec = parse(
        CLOCKS
        + "set_false_path -from [get_cells u_cfg/mode_q_reg] -to [get_cells cap_q_reg]\n"
    )
    res = sdc_objects.resolve(m, spec)
    cov = sdc_objects.coverage(m, lanes, res, spec)
    kept, suppressed = sdc_objects.acknowledge(m, [v], lanes, cov)
    assert kept == [v] and suppressed == []
    spec = parse(CLOCKS + "set_false_path -from [get_cells u_cfg/mode_q_reg]\n")
    res = sdc_objects.resolve(m, spec)
    cov = sdc_objects.coverage(m, lanes, res, spec)
    kept, suppressed = sdc_objects.acknowledge(m, [v], lanes, cov)
    assert kept == [] and len(suppressed) == 1


def test_domain_map_exceptions_shape() -> None:
    cov, xs, _ = _cover(
        "set_false_path -from [get_cells u_cfg/mode_q_reg] -to [get_clocks ck_b]\n"
    )
    ann = sdc_objects.domain_map_exceptions(cov)
    entry = ann[sdc_objects.crossing_key(xs[0])]
    assert entry["sdc_acknowledged"] is True
    (exc,) = entry["sdc_exceptions"]
    assert exc["command"] == "set_false_path"
    assert exc["statement"].startswith("set_false_path -from [get_cells")


# ---- endpoint kinds beyond registers ------------------------------------------


def _port_crossing(m: Module) -> Crossing:
    return Crossing(
        src_clock="ck_a",
        dst_flop=_flops(m)[CAP],
        dst_clock="ck_b",
        min_hops=0,
        width=4,
        src_port="cfg_in",
    )


def _cover_one(sdc_lines: str, c_of) -> str:
    m = _module()
    spec = parse(CLOCKS + sdc_lines)
    res = sdc_objects.resolve(m, spec)
    c = c_of(m)
    return _state(sdc_objects.coverage(m, [c], res, spec), c)


@pytest.mark.parametrize(
    "line",
    [
        "set_false_path -from [get_ports cfg_in]\n",
        "set_false_path -from [get_nets cfg_in]\n",  # an input port's net
        "set_false_path -through [get_ports cfg_in]\n",
    ],
)
def test_port_sourced_crossing(line: str) -> None:
    assert _cover_one(line, _port_crossing) == "acknowledged"


def test_partial_port_bits_do_not_cover() -> None:
    assert _cover_one(
        "set_false_path -from [get_ports {cfg_in[0]}]\n", _port_crossing
    ) == ("uncovered")


@pytest.mark.parametrize(
    "line, expected",
    [
        # A pin whose owner is not a register reads as the instance pin's net,
        # and as -from that net names the flop driving it.
        ("set_false_path -from [get_pins u_cfg/mode]\n", "acknowledged"),
        ("set_false_path -from [get_nets u_cfg/mode_q]\n", "acknowledged"),
        # As -to, a net names the flops whose D it feeds.
        ("set_false_path -to [get_nets u_cfg/mode_q]\n", "covered"),
        ("set_false_path -to [get_pins cap_q_reg/D]\n", "covered"),
    ],
)
def test_net_and_pin_endpoints(line: str, expected: str) -> None:
    assert _cover_one(line, lambda m: _crossings(m)[0]) == expected


def test_boundary_sourced_crossing_only_matches_clocks() -> None:
    def boundary(m: Module) -> Crossing:
        return Crossing(
            src_clock="ck_a",
            dst_flop=_flops(m)[CAP],
            dst_clock="ck_b",
            min_hops=0,
            width=1,
            src_boundary=("u_ip", "dout"),
        )

    assert (
        _cover_one("set_false_path -to [get_cells cap_q_reg]\n", boundary) == "covered"
    )
    assert (
        _cover_one("set_false_path -through [get_nets n_and]\n", boundary)
        == "uncovered"
    )
    m = _module()
    assert sdc_objects.crossing_key(boundary(m))[3:] == ("boundary", "u_ip.dout")


def test_sdc_object_render() -> None:
    assert SdcObject("cell", "u/q_reg[3]").render() == "[get_cells {u/q_reg[3]}]"
    assert SdcObject("cell", "q*", hierarchical=True).render() == (
        "[get_cells -hierarchical q*]"
    )
    assert SdcObject("any", "u/n").render() == "{u/n}"


def test_lifted_finding_is_not_matched_by_raw_names() -> None:
    """A finding whose anchor is not an endpoint of its crossing (lifted from
    an abstracted block, #261) is never acknowledged."""
    cov, xs, _ = _cover("set_false_path -from [get_cells u_cfg/mode_q_reg]\n")
    v = _violation("CDC-004", xs[0], cell="u_blackbox")
    kept, suppressed = sdc_objects.acknowledge(_module(), [v], xs, cov)
    assert kept == [v] and suppressed == []
    assert sdc_objects.acknowledge(_module(), [v], xs, {}) == ([v], [])


def _decoder_module() -> Module:
    cells = [
        Cell("$shl$1", "$shl", {"A": (90,), "B": (91,), "Y": (80, 81)}),
        _dff("$procdff$a", 2, [80], [82]),
        _dff("$procdff$b", 2, [81], [83]),
        _dff("$procdff$c", 2, [91], [84]),  # 1-bit, not fed by the decoder
        _dff("$procdff$x", 3, [82], [85]),
        _dff("$procdff$y", 3, [83], [86]),
        _dff("$procdff$z", 3, [84], [87]),
    ]
    nets = [
        Netname("lane_a", (82,)),
        Netname("lane_b", (83,)),
        Netname("other", (84,)),
    ]
    return Module(
        name="dec",
        ports={},
        cells={c.name: c for c in cells},
        netnames={n.name: n for n in nets},
    )


@pytest.mark.parametrize(
    "lines, acknowledged",
    [
        ("set_false_path -from [get_cells lane_a_reg]\n", False),
        (
            "set_false_path -from [get_cells lane_a_reg]\n"
            "set_false_path -from [get_cells lane_b_reg]\n",
            True,
        ),
        ("set_false_path -from [get_cells lane_*_reg]\n", True),
    ],
)
def test_onehot_decode_needs_every_lane(lines: str, acknowledged: bool) -> None:
    m = _decoder_module()
    f = _flops(m)
    xs = [
        Crossing("ck_a", f[f"$procdff${d}"], "ck_b", 0, 1, src_flop=f[f"$procdff${s}"])
        for s, d in (("a", "x"), ("b", "y"), ("c", "z"))
    ]
    spec = parse(CLOCKS + lines)
    res = sdc_objects.resolve(m, spec)
    cov = sdc_objects.coverage(m, xs, res, spec)
    v = _violation("CDC-019", None, cell="$procdff$a")
    kept, suppressed = sdc_objects.acknowledge(m, [v], xs, cov)
    assert bool(suppressed) is acknowledged
    assert len(kept) == (0 if acknowledged else 1)
    # A CDC-019 anchored on a register no decoder feeds is never matched.
    stray = _violation("CDC-019", None, cell="$procdff$nope")
    assert sdc_objects.acknowledge(m, [stray], xs, cov)[0] == [stray]


@pytest.mark.parametrize(
    "file, line, expected",
    [
        ("a.sdc", 7, "sdc a.sdc:7"),
        ("a.sdc", None, "sdc a.sdc"),
        (None, 7, "sdc line 7"),
        (None, None, "sdc"),
    ],
)
def test_text_provenance_of_an_sdc_suppression(file, line, expected) -> None:
    import re

    from rtl_buddy_cdc.reporter import _waiver_provenance
    from rtl_buddy_cdc.waivers import SdcJustification, Waiver

    w = Waiver(
        rule_pattern="CDC-001",
        regex=re.compile(""),
        reason="r",
        source_line=line or 0,
        sdc=SdcJustification(statement="set_false_path -from x", line=line, file=file),
    )
    assert _waiver_provenance(w) == expected
