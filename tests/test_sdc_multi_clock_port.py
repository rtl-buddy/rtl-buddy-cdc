"""Several clocks on one port: ``create_clock -add`` (rtl-buddy-cdc#330).

SDC semantics: a ``create_clock`` on a source that already carries a
clock *replaces* it, unless ``-add`` is given — then both clocks exist
on the source (typically two modes on one pin, declared exclusive).

Before #330 the parser ignored ``-add``: both forms kept both clocks,
``clock_for_port`` returned the first-declared one (on both backends),
every flop behind the port was named after it, and a crossing that was
asynchronous only to the *other* clock on the port was filtered out as
synchronous — no finding.

The model now:

- without ``-add`` the later clock replaces the earlier one (warning);
- with ``-add`` the port is recorded in ``ClockSpec.multi_clock_ports``;
  its first-declared clock stays the *primary* that names its flops, and
  ``are_async`` / ``is_unreachable_crossing`` read that name as "every
  clock on the port": a crossing is synchronous only if it is
  synchronous to each of them. ``filter_async`` stamps the clock pair it
  is actually asynchronous through on ``Crossing.async_via``, and the
  finding names it.

Every test runs on both SDC reader backends via the autouse fixture.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rtl_buddy_cdc import netlist, sdc as sdc_mod
from rtl_buddy_cdc.cli import app
from rtl_buddy_cdc.compositional import derive_sub_spec
from rtl_buddy_cdc.domain import filter_async, find_crossings
from rtl_buddy_cdc.rules import run_all as run_all_rules
from rtl_buddy_cdc.sdc import parse, validate_clock_graph

FIXTURES = Path(__file__).parent / "fixtures"

# Port clk_a carries clk_a and (via -add) clk_a2; clk_b is a third clock
# asynchronous to clk_a2 only. clk_a ↔ clk_b is undeclared → synchronous.
ADD = """
create_clock -name clk_a  -period 10 [get_ports clk_a]
create_clock -name clk_a2 -period 40 -add [get_ports clk_a]
create_clock -name clk_b  -period 10 [get_ports clk_b]
set_clock_groups -asynchronous -group {clk_a2} -group {clk_b}
"""


@pytest.fixture(autouse=True)
def _every_sdc_backend(sdc_backend):
    return sdc_backend


# ---- parsing: replace vs add -------------------------------------------------


@pytest.mark.parametrize(
    "target",
    ["[get_ports clk_a]", "{clk_a}", "clk_a"],
    ids=["bracket", "brace", "bare"],
)
def test_add_keeps_both_clocks_on_the_port(target: str) -> None:
    spec = parse(
        f"create_clock -name clk_a -period 10 {target}\n"
        f"create_clock -name clk_a2 -period 40 -add {target}\n"
    )
    assert spec.clocks["clk_a"].ports == ("clk_a",)
    assert spec.clocks["clk_a2"].ports == ("clk_a",)
    assert spec.multi_clock_ports == {"clk_a": ("clk_a", "clk_a2")}
    # The first-declared clock is the primary: it names the port's flops.
    assert spec.clock_for_port("clk_a") == "clk_a"
    # -add is a legitimate declaration: neither the parser nor G-11 warns.
    assert spec.partial_warnings == []
    assert validate_clock_graph(spec) == []


def test_primary_is_the_first_declared_clock() -> None:
    spec = parse(
        "create_clock -name clk_a2 -period 40 [get_ports clk_a]\n"
        "create_clock -name clk_a -period 10 -add [get_ports clk_a]\n"
    )
    assert spec.multi_clock_ports == {"clk_a": ("clk_a2", "clk_a")}
    assert spec.clock_for_port("clk_a") == "clk_a2"


def test_without_add_the_later_clock_replaces_the_earlier() -> None:
    spec = parse(
        "create_clock -name clk_a -period 10 [get_ports clk_a]\n"
        "create_clock -name clk_a2 -period 40 [get_ports clk_a]\n"
    )
    assert "clk_a" not in spec.clocks
    assert spec.clock_for_port("clk_a") == "clk_a2"
    assert spec.multi_clock_ports == {}
    (w,) = spec.partial_warnings
    assert "create_clock 'clk_a2' without -add replaces clock 'clk_a'" in w
    assert "'clk_a' is removed" in w
    # Only one clock is left on the port, so G-11 has nothing to add.
    assert validate_clock_graph(spec) == []


def test_replace_keeps_the_earlier_clocks_other_ports() -> None:
    spec = parse(
        "create_clock -name ck -period 10 [get_ports {p q}]\n"
        "create_clock -name ck2 -period 5 [get_ports p]\n"
    )
    assert spec.clocks["ck"].ports == ("q",)
    assert spec.clock_for_port("p") == "ck2"
    assert any("'ck' keeps q" in w for w in spec.partial_warnings)


def test_replace_without_add_clears_an_added_clock_too() -> None:
    spec = parse(f"{ADD}create_clock -name clk_c -period 5 [get_ports clk_a]\n")
    assert "clk_a" not in spec.clocks and "clk_a2" not in spec.clocks
    assert spec.multi_clock_ports == {}
    assert spec.clock_for_port("clk_a") == "clk_c"
    assert len([w for w in spec.partial_warnings if "replaces" in w]) == 2


def test_redefining_the_same_name_is_still_the_duplicate_warning() -> None:
    spec = parse(
        "create_clock -name clk -period 10 [get_ports clk]\n"
        "create_clock -name clk -period 5 [get_ports clk]\n"
    )
    assert spec.clocks["clk"].period == 5.0
    assert [w for w in spec.partial_warnings if "replaces" in w] == []
    assert any("duplicate clock name 'clk'" in w for w in spec.partial_warnings)


def test_virtual_clock_is_never_replaced() -> None:
    spec = parse(
        "create_clock -name vclk -period 10\n"
        "create_clock -name clk -period 10 [get_ports clk]\n"
    )
    assert set(spec.clocks) == {"vclk", "clk"}
    assert spec.partial_warnings == []


def test_generated_clock_port_target_replace_and_add() -> None:
    base = "create_clock -name clk -period 10 [get_ports clk]\n"
    replaced = parse(
        f"{base}create_generated_clock -name g -master_clock clk "
        "-source [get_ports clk] [get_ports clk]\n"
    )
    assert "clk" not in replaced.clocks
    assert any(
        "create_generated_clock 'g' without -add replaces clock 'clk'" in w
        for w in replaced.partial_warnings
    )
    added = parse(
        f"{base}create_generated_clock -name g -master_clock clk -add "
        "-source [get_ports clk] [get_ports clk]\n"
    )
    assert added.multi_clock_ports == {"clk": ("clk", "g")}


def test_generated_clock_pin_target_replace_and_add() -> None:
    base = (
        "create_clock -name clk -period 10 [get_ports clk]\n"
        "create_generated_clock -name g1 -master_clock clk [get_pins u/q]\n"
    )
    replaced = parse(
        f"{base}create_generated_clock -name g2 -master_clock clk [get_pins u/q]\n"
    )
    assert replaced.pin_clocks == {"u/q": "g2"}
    assert any(
        "without -add replaces clock 'g1' on pin 'u/q'" in w
        for w in (replaced.partial_warnings)
    )
    added = parse(
        f"{base}create_generated_clock -name g2 -master_clock clk -add [get_pins u/q]\n"
    )
    # One clock per internal pin: the first stays, the added one is named.
    assert added.pin_clocks == {"u/q": "g1"}
    assert any(
        "-add on pin 'u/q'" in w and "not modelled" in w for w in added.partial_warnings
    )


# ---- the async predicate -----------------------------------------------------


def test_crossing_is_async_if_any_colocated_clock_is() -> None:
    spec = parse(ADD)
    assert spec.colocated_clocks("clk_a") == ("clk_a", "clk_a2")
    assert spec._are_async_pair("clk_a", "clk_b") is False
    # Before #330 this was False and the crossing was dropped.
    assert spec.are_async("clk_a", "clk_b")
    assert spec.are_async("clk_b", "clk_a")
    assert spec.async_witness("clk_a", "clk_b") == ("clk_a2", "clk_b")
    assert spec.async_witness("clk_b", "clk_a") == ("clk_b", "clk_a2")


def test_a_non_primary_clock_means_only_itself() -> None:
    """``set_input_delay -clock clk_a2`` names clk_a2 alone; a port typed
    with the primary ``-clock clk_a`` stands for the whole port."""
    spec = parse(
        "create_clock -name clk_a  -period 10 [get_ports clk_a]\n"
        "create_clock -name clk_a2 -period 40 -add [get_ports clk_a]\n"
        "create_clock -name clk_b  -period 10 [get_ports clk_b]\n"
        "set_clock_groups -asynchronous -group {clk_a} -group {clk_b}\n"
        "set_input_delay -clock clk_a2 1 [get_ports d]\n"
    )
    assert spec.clock_for_port("d") == "clk_a2"
    assert spec.colocated_clocks("clk_a2") == ("clk_a2",)
    assert not spec.are_async("clk_a2", "clk_b")
    assert spec.are_async("clk_a", "clk_b")
    # The primary pair itself is async: no co-located clock is needed.
    assert spec.async_witness("clk_a", "clk_b") == ("clk_a", "clk_b")


def test_synchronous_to_every_clock_on_the_port_stays_synchronous() -> None:
    spec = parse(
        "create_clock -name clk_a  -period 10 [get_ports clk_a]\n"
        "create_clock -name clk_a2 -period 40 -add [get_ports clk_a]\n"
        "create_clock -name clk_b  -period 10 [get_ports clk_b]\n"
    )
    assert not spec.are_async("clk_a", "clk_b")
    assert spec.async_witness("clk_a", "clk_b") is None


def test_exclusive_modes_on_the_port_do_not_hide_the_crossing() -> None:
    """``-physically_exclusive`` between the two modes on the pin — the
    usual companion of ``-add`` — removes only clk_a ↔ clk_a2."""
    spec = parse(
        f"{ADD}set_clock_groups -physically_exclusive "
        "-group {clk_a} -group {clk_a2}\n"
    )
    assert spec._is_unreachable_pair("clk_a", "clk_a2")
    # A domain named clk_a also stands for clk_a2, so even clk_a ↔ clk_a2
    # is not wholly unreachable as a crossing (clk_a2 ↔ clk_a2 is live).
    assert not spec.is_unreachable_crossing("clk_a", "clk_a2")
    assert not spec.is_unreachable_crossing("clk_a", "clk_b")
    assert spec.async_witness("clk_a", "clk_b") == ("clk_a2", "clk_b")


def test_exclusive_to_the_async_clock_is_unreachable() -> None:
    """clk_a2 never coexists with clk_b, and clk_a is synchronous to it:
    no reachable asynchronous reading is left."""
    spec = parse(
        f"{ADD}set_clock_groups -logically_exclusive "
        "-group {clk_a2} -group {clk_b}\n"
    )
    assert not spec.is_unreachable_crossing("clk_a", "clk_b")
    assert spec.async_witness("clk_a", "clk_b") is None
    assert not spec.are_async("clk_a", "clk_b")
    both = parse(
        f"{ADD}set_clock_groups -logically_exclusive "
        "-group {clk_a clk_a2} -group {clk_b}\n"
    )
    assert both.is_unreachable_crossing("clk_a", "clk_b")


def test_single_clock_ports_are_unchanged() -> None:
    spec = parse(
        "create_clock -name ck_a -period 10 [get_ports ck_a]\n"
        "create_clock -name ck_b -period 10 [get_ports ck_b]\n"
        "set_clock_groups -asynchronous -group ck_a -group ck_b\n"
        "set_clock_groups -logically_exclusive -group ck_a -group ck_b\n"
    )
    assert spec.multi_clock_ports == {}
    assert spec.colocated_clocks("ck_a") == ("ck_a",)
    # Per-pair answers, exactly as before #330: are_async alone does
    # not consult exclusivity; the witness (filter_async) does.
    assert spec.are_async("ck_a", "ck_b")
    assert spec.is_unreachable_crossing("ck_a", "ck_b")
    assert spec.async_witness("ck_a", "ck_b") is None


# ---- generated clocks on a multi-clock source -------------------------------


def test_master_clock_disambiguates_a_multi_clock_source() -> None:
    spec = parse(
        f"{ADD}create_generated_clock -name g -source [get_ports clk_a] "
        "-master_clock clk_a2 -divide_by 2 [get_pins u/q]\n"
    )
    assert spec.clocks["g"].source == ("clk_a",)
    assert spec.resolve("g") == "clk_a2"
    assert spec.are_async("g", "clk_b")
    # g is its own (single) clock: no port expansion for it.
    assert spec.colocated_clocks("g") == ("g",)
    assert validate_clock_graph(spec) == []
    on_primary = parse(
        f"{ADD}create_generated_clock -name g -source [get_ports clk_a] "
        "-master_clock clk_a -divide_by 2 [get_pins u/q]\n"
    )
    assert on_primary.resolve("g") == "clk_a"
    assert not on_primary.are_async("g", "clk_b")


def test_no_master_clock_on_a_multi_clock_source_warns() -> None:
    spec = parse(
        f"{ADD}create_generated_clock -name g -source [get_ports clk_a] "
        "-divide_by 2 [get_pins u/q]\n"
    )
    # -source never infers a master: no arbitrary pick, but say so.
    assert spec.clocks["g"].master is None
    (w,) = validate_clock_graph(spec)
    assert "create_generated_clock 'g'" in w
    assert "carries clocks clk_a, clk_a2 and no -master_clock" in w


def test_master_clock_not_on_the_source_warns() -> None:
    spec = parse(
        f"{ADD}create_generated_clock -name g -source [get_ports clk_a] "
        "-master_clock clk_b -divide_by 2 [get_pins u/q]\n"
    )
    (w,) = validate_clock_graph(spec)
    assert "-master_clock 'clk_b' is not one of the clocks on -source port" in w


def test_single_clock_source_without_master_clock_is_not_new_noise() -> None:
    spec = parse(
        "create_clock -name clk -period 10 [get_ports clk]\n"
        "create_generated_clock -name g -source [get_ports clk] "
        "-divide_by 2 [get_pins u/q]\n"
    )
    assert spec.clocks["g"].master is None
    assert validate_clock_graph(spec) == []


def test_sub_spec_keeps_the_colocation() -> None:
    """The compositional pass (#261) must expand a subtree domain exactly
    as the parent does, or the two disagree about the crossing."""
    spec = parse(ADD)
    sub = derive_sub_spec(spec, {"clk": "clk_a", "clk2": "clk_b"})
    assert sub.colocated_clocks("clk_a") == ("clk_a", "clk_a2")
    assert sub.async_witness("clk_a", "clk_b") == ("clk_a2", "clk_b")


# ---- end to end: the paired fixtures -----------------------------------------


def _analyse(name: str):
    d = FIXTURES / name
    module = netlist.load(d / f"{name}.json")
    spec = sdc_mod.parse_file(d / f"{name}.sdc")
    sdc_mod.synthesize_unconstrained_inputs(spec, module)
    crossings = find_crossings(
        module, port_clock=spec.port_clock, clock_for_port=spec.clock_for_port
    )
    async_crossings = filter_async(crossings, spec)
    return module, spec, crossings, async_crossings


def test_bad_fixture_both_directions_fire_cdc_001_naming_clk_a2() -> None:
    module, spec, crossings, async_crossings = _analyse("bad_multi_clock_port")
    assert {(c.src_clock, c.dst_clock) for c in crossings} == {
        ("clk_a", "clk_b"),
        ("clk_b", "clk_a"),
    }
    assert {c.async_via for c in async_crossings} == {
        ("clk_a2", "clk_b"),
        ("clk_b", "clk_a2"),
    }
    violations = run_all_rules(module, async_crossings, spec)
    # Only CDC-001: in particular no CDC-021 (clk_a IS declared).
    assert sorted(v.rule_id for v in violations) == ["CDC-001", "CDC-001"]
    for v in violations:
        assert "[multi-clock port] asynchronous as" in v.message
        assert "clk_a2 shares the port of clk_a" in v.message


def test_bad_fixture_was_silent_before_330() -> None:
    """The pre-#330 per-pair predicate drops both crossings."""
    _module, spec, crossings, _ = _analyse("bad_multi_clock_port")
    assert [
        c
        for c in crossings
        if not spec._is_unreachable_pair(c.src_clock, c.dst_clock)
        and spec._are_async_pair(c.src_clock, c.dst_clock)
    ] == []


def test_good_fixture_sync_chains_recognised_on_the_multi_clock_side() -> None:
    module, spec, _crossings, async_crossings = _analyse("good_multi_clock_port")
    assert len(async_crossings) == 2
    assert run_all_rules(module, async_crossings, spec) == []


def test_json_report_and_domain_map_name_the_async_clock(tmp_path: Path) -> None:
    d = FIXTURES / "bad_multi_clock_port"
    out_map = tmp_path / "map.json"
    result = CliRunner().invoke(
        app,
        [
            "analyze",
            "--netlist",
            str(d / "bad_multi_clock_port.json"),
            "--sdc",
            str(d / "bad_multi_clock_port.sdc"),
            "--format",
            "json",
            "--emit-domain-map",
            str(out_map),
        ],
    )
    assert result.exit_code == 1, result.output
    report = json.loads(result.output)
    assert report["summary"]["violations"] == 2
    vias = sorted(
        (
            v["crossing"]["async_via"]["src_clock"],
            v["crossing"]["async_via"]["dst_clock"],
        )
        for v in report["violations"]
    )
    assert vias == [("clk_a2", "clk_b"), ("clk_b", "clk_a2")]
    dmap = json.loads(out_map.read_text())
    assert dmap["schema_version"] == "1.3"
    entries = {(c["src_clock"], c["dst_clock"]): c for c in dmap["crossings"]}
    assert entries[("clk_a", "clk_b")]["async_per_sdc"] is True
    assert entries[("clk_a", "clk_b")]["async_via"] == {
        "src_clock": "clk_a2",
        "dst_clock": "clk_b",
    }
    assert entries[("clk_a", "clk_b")]["findings"] == ["CDC-001"]
    # Both clocks list the shared port.
    ports = {c["name"]: c["ports"] for c in dmap["clocks"]}
    assert ports["clk_a"] == ports["clk_a2"] == ["clk_a"]


def test_domain_map_has_no_async_via_without_a_multi_clock_port(
    tmp_path: Path,
) -> None:
    d = FIXTURES / "bad_single_ff_sync"
    out_map = tmp_path / "map.json"
    result = CliRunner().invoke(
        app,
        [
            "analyze",
            "--netlist",
            str(d / "bad_single_ff_sync.json"),
            "--sdc",
            str(d / "bad_single_ff_sync.sdc"),
            "--format",
            "json",
            "--emit-domain-map",
            str(out_map),
        ],
    )
    report = json.loads(result.output)
    assert all("async_via" not in v.get("crossing", {}) for v in report["violations"])
    dmap = json.loads(out_map.read_text())
    assert all("async_via" not in c for c in dmap["crossings"])


@pytest.mark.parametrize(("case", "expect_via"), [("1", True), ("0", False)])
def test_case_analysed_clock_mux_leg_on_a_multi_clock_port(
    case: str, expect_via: bool
) -> None:
    """``set_case_analysis`` (#325) picks the mux leg in the tracer; the
    picked port then maps to its primary clock and expands as usual — no
    special case needed. ``clk_b`` also carries ``clk_b2`` (-add), and
    only ``clk_b2`` is async to ``clk_a``."""
    name = "bad_case_analysis_clock_mux"
    d = FIXTURES / name
    module = netlist.load(d / f"{name}.json")
    spec = parse(
        "create_clock -name clk_a  -period 10 [get_ports clk_a]\n"
        "create_clock -name clk_b  -period 7.5 [get_ports clk_b]\n"
        "create_clock -name clk_b2 -period 20 -add [get_ports clk_b]\n"
        "set_clock_groups -asynchronous -group {clk_a} -group {clk_b2}\n"
        "set_input_delay -clock clk_a 1.0 [get_ports d_in]\n"
        "set_input_delay -clock clk_a 1.0 [get_ports sel]\n"
        f"set_case_analysis {case} [get_ports sel]\n"
    )
    sdc_mod.synthesize_unconstrained_inputs(spec, module)
    crossings = find_crossings(
        module,
        port_clock=spec.port_clock,
        clock_for_port=spec.clock_for_port,
        case_analysis=spec.case_analysis,
    )
    async_crossings = filter_async(crossings, spec)
    if not expect_via:
        assert async_crossings == []
        return
    assert [(c.src_clock, c.dst_clock, c.async_via) for c in async_crossings] == [
        ("clk_a", "clk_b", ("clk_a", "clk_b2"))
    ]
    violations = run_all_rules(module, async_crossings, spec)
    assert [v.rule_id for v in violations] == ["CDC-001"]
