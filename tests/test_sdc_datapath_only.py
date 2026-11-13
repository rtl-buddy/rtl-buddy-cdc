"""``set_max_delay -datapath_only`` as CDC intent (rtl-buddy-cdc#325).

A clock-to-clock datapath-only bound exists only where the launch and
capture clocks are not timed against each other, so the analyzer reads
it as an intended asynchronous crossing. Plain ``set_max_delay`` (no
``-datapath_only``) stays STA noise and is dropped exactly as before.

Every test runs on both SDC reader backends via the autouse fixture.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rtl_buddy_cdc import netlist, sdc as sdc_mod
from rtl_buddy_cdc.cli import app
from rtl_buddy_cdc.domain import filter_async, find_crossings
from rtl_buddy_cdc.rules import run_all as run_all_rules
from rtl_buddy_cdc.sdc import DatapathOnlyDelay, parse

FIXTURES = Path(__file__).parent / "fixtures"
CLOCKS = """
create_clock -name ck_a -period 10 [get_ports ck_a]
create_clock -name ck_b -period 7.5 [get_ports ck_b]
"""


@pytest.fixture(autouse=True)
def _every_sdc_backend(sdc_backend):
    return sdc_backend


# ---- parsing -----------------------------------------------------------------


@pytest.mark.parametrize(
    "endpoints",
    [
        "-from [get_clocks ck_a] -to [get_clocks ck_b]",
        "-from {ck_a} -to {ck_b}",
        "-from ck_a -to ck_b",
    ],
    ids=["bracket", "brace", "bare"],
)
def test_clock_pair_forms(endpoints: str) -> None:
    spec = parse(f"{CLOCKS}set_max_delay 4.0 -datapath_only {endpoints}\n")
    assert spec.datapath_only_pairs == {frozenset({"ck_a", "ck_b"})}
    assert spec.datapath_only_delays == [
        DatapathOnlyDelay(value=4.0, from_clocks=("ck_a",), to_clocks=("ck_b",))
    ]
    assert spec.partial_warnings == []


def test_value_after_endpoints_is_not_a_clock() -> None:
    """A trailing delay is slurped by the GREEDY ``-to``; it is the value."""
    spec = parse(
        f"{CLOCKS}set_max_delay -datapath_only -from ck_a -to [get_clocks ck_b] 3\n"
    )
    (rec,) = spec.datapath_only_delays
    assert rec.value == 3.0
    assert rec.to_clocks == ("ck_b",)


def test_rise_fall_endpoints_count() -> None:
    spec = parse(
        f"{CLOCKS}set_max_delay 2 -datapath_only "
        "-rise_from [get_clocks ck_a] -fall_to [get_clocks ck_b]\n"
    )
    assert spec.datapath_only_pairs == {frozenset({"ck_a", "ck_b"})}


def test_plain_set_max_delay_stays_ignored(caplog, sdc_backend) -> None:
    caplog.set_level(logging.DEBUG, logger="rtl_buddy_cdc.sdc")
    spec = parse(
        f"{CLOCKS}set_max_delay 4.0 -from [get_clocks ck_a] -to [get_clocks ck_b]\n"
    )
    assert spec.datapath_only_delays == []
    assert spec.datapath_only_pairs == set()
    assert spec.partial_warnings == []
    assert not spec.are_async("ck_a", "ck_b")
    debug = [r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG]
    assert "sdc: ignoring unsupported command 'set_max_delay'" in debug
    skipped = [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING and "tokenizer_skipped" in r.getMessage()
    ]
    if sdc_backend == "tokenizer":
        assert skipped and "'set_max_delay'" in skipped[0]
    else:
        assert skipped == []


def test_object_endpoints_recorded() -> None:
    spec = parse(
        f"{CLOCKS}set_max_delay 2 -datapath_only "
        "-from [get_cells u_a/q_reg] -to [get_pins {u_b/sync_reg[0]/D}]\n"
    )
    (rec,) = spec.datapath_only_delays
    assert rec.from_objects == (("cell", "u_a/q_reg"),)
    assert rec.to_objects == (("pin", "u_b/sync_reg[0]/D"),)
    assert not rec.is_clock_pair
    assert spec.datapath_only_pairs == set()
    # Resolved against the netlist by ``sdc_objects`` since #328, so the
    # parser no longer warns that they are ignored.
    assert spec.partial_warnings == []


def test_clock_to_object_is_not_a_clock_pair() -> None:
    spec = parse(
        f"{CLOCKS}set_max_delay 2 -datapath_only "
        "-from [get_clocks ck_a] -to [get_cells u_sync/meta_reg]\n"
    )
    (rec,) = spec.datapath_only_delays
    assert rec.from_clocks == ("ck_a",)
    assert rec.to_objects == (("cell", "u_sync/meta_reg"),)
    assert spec.datapath_only_pairs == set()
    assert not spec.are_async("ck_a", "ck_b")


def test_through_is_partial_warning_and_not_recorded() -> None:
    spec = parse(
        f"{CLOCKS}set_max_delay 2 -datapath_only -from [get_clocks ck_a] "
        "-through [get_pins u/x] -to [get_clocks ck_b]\n"
    )
    assert spec.datapath_only_delays == []
    assert spec.datapath_only_pairs == set()
    assert any("-through" in w for w in spec.partial_warnings)


def test_incomplete_endpoint_list_warns() -> None:
    spec = parse(f"{CLOCKS}set_max_delay 2 -datapath_only -from [get_clocks ck_a]\n")
    assert spec.datapath_only_delays == []
    assert any("incomplete" in w for w in spec.partial_warnings)


def test_filter_clause_warns() -> None:
    spec = parse(
        f"{CLOCKS}set_max_delay 2 -datapath_only "
        '-from [get_clocks -filter "NAME =~ ck_*"] -to [get_clocks ck_b]\n'
    )
    assert any("-filter" in w for w in spec.partial_warnings)


def test_self_pair_adds_no_pair() -> None:
    spec = parse(f"{CLOCKS}set_max_delay 2 -datapath_only -from ck_a -to ck_a\n")
    assert len(spec.datapath_only_delays) == 1
    assert spec.datapath_only_pairs == set()


# ---- are_async / is_unreachable_crossing semantics ---------------------------


def test_undeclared_pair_becomes_async() -> None:
    spec = parse(f"{CLOCKS}set_max_delay 4 -datapath_only -from ck_a -to ck_b\n")
    assert spec.are_async("ck_a", "ck_b")
    assert spec.are_async("ck_b", "ck_a")


def test_overrides_generated_from_master() -> None:
    """Conservative: the datapath-only bound beats the same-root collapse."""
    base = (
        "create_clock -name clk -period 10 [get_ports clk]\n"
        "create_generated_clock -name clk_div2 -master_clock clk "
        "-source [get_ports clk] -divide_by 2 [get_pins div_q/Q]\n"
    )
    assert not parse(base).are_async("clk", "clk_div2")
    spec = parse(f"{base}set_max_delay 8 -datapath_only -from clk -to clk_div2\n")
    assert spec.are_async("clk", "clk_div2")


def test_overrides_same_clock_group() -> None:
    """Two clocks in one ``-group`` are synchronous; the bound still wins."""
    base = (
        f"{CLOCKS}create_clock -name ck_c -period 5 [get_ports ck_c]\n"
        "set_clock_groups -asynchronous -group {ck_a ck_b} -group {ck_c}\n"
    )
    assert not parse(base).are_async("ck_a", "ck_b")
    spec = parse(f"{base}set_max_delay 3 -datapath_only -from ck_a -to ck_b\n")
    assert spec.are_async("ck_a", "ck_b")


def test_pair_between_masters_covers_generated_clocks() -> None:
    spec = parse(
        f"{CLOCKS}create_generated_clock -name ck_a2 -master_clock ck_a "
        "-source [get_ports ck_a] -divide_by 2 [get_pins d/Q]\n"
        "set_max_delay 3 -datapath_only -from ck_a -to ck_b\n"
    )
    assert spec.are_async("ck_a2", "ck_b")


def test_exclusive_pair_with_bound_is_not_unreachable() -> None:
    base = f"{CLOCKS}set_clock_groups -logically_exclusive -group ck_a -group ck_b\n"
    assert parse(base).is_unreachable_crossing("ck_a", "ck_b")
    spec = parse(f"{base}set_max_delay 3 -datapath_only -from ck_a -to ck_b\n")
    assert not spec.is_unreachable_crossing("ck_a", "ck_b")
    assert spec.are_async("ck_a", "ck_b")


# ---- fixtures -----------------------------------------------------------------


def _load(name: str) -> tuple[netlist.Module, Path]:
    d = FIXTURES / name
    return netlist.load(d / f"{name}.json"), d / f"{name}.sdc"


def test_bad_fixture_cdc_001_fires_and_only_it() -> None:
    module, sdc_path = _load("bad_datapath_only_pair")
    spec = sdc_mod.parse_file(sdc_path)
    sdc_mod.synthesize_unconstrained_inputs(spec, module)
    crossings = find_crossings(
        module, port_clock=spec.port_clock, clock_for_port=spec.clock_for_port
    )
    async_crossings = filter_async(crossings, spec)
    assert len(async_crossings) == 1
    violations = run_all_rules(module, async_crossings, spec)
    assert {v.rule_id for v in violations} == {"CDC-001"}


def test_bad_fixture_with_plain_max_delay_reports_nothing(tmp_path: Path) -> None:
    """Without ``-datapath_only`` the pair is undeclared, as before #325."""
    module, sdc_path = _load("bad_datapath_only_pair")
    text = sdc_path.read_text().replace(" -datapath_only", "")
    spec = sdc_mod.parse(text)
    sdc_mod.synthesize_unconstrained_inputs(spec, module)
    crossings = find_crossings(
        module, port_clock=spec.port_clock, clock_for_port=spec.clock_for_port
    )
    assert len(crossings) == 1
    assert filter_async(crossings, spec) == []


def test_domain_map_serialises_records(tmp_path: Path) -> None:
    d = FIXTURES / "bad_datapath_only_pair"
    sdc_path = tmp_path / "x.sdc"
    sdc_path.write_text(
        (d / "bad_datapath_only_pair.sdc").read_text()
        + "set_max_delay 2 -datapath_only -from [get_cells u/q_reg] "
        "-to [get_clocks ck_b]\n"
    )
    out = tmp_path / "map.json"
    result = CliRunner().invoke(
        app,
        [
            "analyze",
            "--netlist",
            str(d / "bad_datapath_only_pair.json"),
            "--sdc",
            str(sdc_path),
            "--emit-domain-map",
            str(out),
            "--no-findings",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(out.read_text())
    assert payload["schema_version"] == "1.3"
    assert payload["datapath_only_pairs"] == [["ck_a", "ck_b"]]
    assert payload["max_delay_datapath_only"] == [
        {
            "from_clocks": [],
            "to_clocks": ["ck_b"],
            "from_objects": [{"kind": "cell", "name": "u/q_reg"}],
            "to_objects": [],
            "value": 2.0,
            "clock_pair": False,
        },
        {
            "from_clocks": ["ck_a"],
            "to_clocks": ["ck_b"],
            "from_objects": [],
            "to_objects": [],
            "value": 5.0,
            "clock_pair": True,
        },
    ]
    async_crossings = [c for c in payload["crossings"] if c["async_per_sdc"]]
    assert len(async_crossings) == 1
