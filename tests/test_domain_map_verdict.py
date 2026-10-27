"""Per-crossing synchroniser verdict in the domain map (issue #307).

Schema 1.2 adds two keys to every ``crossings[]`` and
``clock_network_crossings[]`` entry:

- ``synchronizer`` — the scheme the rule pack recognised at the
  destination (``rules.SYNCHRONIZER_SCHEMES``), or ``null``;
- ``findings`` — the sorted rule ids lint raised on that crossing after
  waivers / pragmas; ``[]`` when clean, ``null`` when the rule pass did
  not run at all.

The end-to-end tests drive the CLI against committed fixture netlists,
so they need no frontend; the pragma case goes through ``lint`` and is
gated on pyslang like the rest of the pragma suite.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from rtl_buddy_cdc import netlist, sdc as sdc_mod
from rtl_buddy_cdc.cli import app
from rtl_buddy_cdc.domain import Crossing, find_crossings
from rtl_buddy_cdc.domain_map import SCHEMA_VERSION, _FindingIndex
from rtl_buddy_cdc.rules import (
    SYNCHRONIZER_SCHEMES,
    Violation,
    _build_context,
    crossing_synchronizer,
)
from rtl_buddy_cdc.sdc import UNCONSTRAINED_SENTINEL

FIXTURES = Path(__file__).parent / "fixtures"
PYSLANG_INSTALLED = importlib.util.find_spec("pyslang") is not None

runner = CliRunner()


def _fixture(name: str) -> tuple[Path, Path]:
    d = FIXTURES / name
    return d / f"{name}.json", d / f"{name}.sdc"


def _analyze_map(
    name: str, tmp_path: Path, *extra: str, sdc: bool = True
) -> dict[str, Any]:
    netlist_path, sdc_path = _fixture(name)
    out = tmp_path / f"{name}.domain-map.json"
    args = ["analyze", "--netlist", str(netlist_path)]
    if sdc:
        args += ["--sdc", str(sdc_path)]
    args += ["--emit-domain-map", str(out), *extra]
    result = runner.invoke(app, args)
    assert result.exit_code in (0, 1), result.output
    payload: dict[str, Any] = json.loads(out.read_text())
    return payload


def _async(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [c for c in payload["crossings"] if c["async_per_sdc"]]


# --- schema ------------------------------------------------------------------


def test_schema_version_is_1_2(tmp_path: Path) -> None:
    assert SCHEMA_VERSION == "1.2"
    payload = _analyze_map("ip_cdc_handshake", tmp_path)
    assert payload["schema_version"] == "1.2"


def test_every_crossing_carries_both_keys(tmp_path: Path) -> None:
    """Additive keys: present on every entry, the 1.1 keys untouched."""
    payload = _analyze_map("ip_cdc_handshake", tmp_path)
    assert payload["crossings"]
    for c in payload["crossings"]:
        assert {"synchronizer", "findings"} <= c.keys()
        assert {
            "src_clock",
            "dst_clock",
            "dst_flop",
            "dst_source_instance_path",
            "min_hops",
            "width",
            "async_per_sdc",
        } <= c.keys()
        assert c["synchronizer"] is None or c["synchronizer"] in SYNCHRONIZER_SCHEMES
        assert isinstance(c["findings"], list)


# --- synchronizer ------------------------------------------------------------


def test_clean_handshake_ip_recognised_and_finding_free(tmp_path: Path) -> None:
    """The golden req/ack IP: 2FF req + ack, handshake-held data bus."""
    payload = _analyze_map("ip_cdc_handshake", tmp_path)
    got = sorted(
        (c["width"], c["synchronizer"], tuple(c["findings"])) for c in _async(payload)
    )
    assert got == [(1, "2ff", ()), (1, "2ff", ()), (8, "handshake", ())]


@pytest.mark.parametrize(
    ("fixture", "width", "scheme"),
    [
        ("good_2ff_sync", 1, "2ff"),
        ("good_packed_shift_sync", 1, "2ff"),
        ("marked_user_sync", 1, "2ff"),
        ("good_pulse_synchronizer", 1, "pulse"),
        ("good_gray_bus_sync_chain", 4, "gray"),
        ("good_gray_counter_crossing", 4, "gray"),
        ("good_dffe_gated_bus_crossing", 8, "handshake"),
        ("marked_quasi_static", 4, "static"),
    ],
)
def test_recognised_scheme(
    tmp_path: Path, fixture: str, width: int, scheme: str
) -> None:
    payload = _analyze_map(fixture, tmp_path)
    hits = [c for c in _async(payload) if c["width"] == width]
    assert hits, f"no {width}-bit async crossing in {fixture}"
    assert {c["synchronizer"] for c in hits} == {scheme}
    assert all(c["findings"] == [] for c in hits)


def test_unsynchronized_crossing_is_null_and_flagged(tmp_path: Path) -> None:
    """A single destination flop: nothing recognised, CDC-001 raised."""
    payload = _analyze_map("bad_single_ff_sync", tmp_path)
    (c,) = _async(payload)
    assert c["synchronizer"] is None
    assert c["findings"] == ["CDC-001"]


def test_unprotected_bus_is_null_and_flagged(tmp_path: Path) -> None:
    payload = _analyze_map("bad_bus_crossing", tmp_path)
    (c,) = _async(payload)
    assert c["synchronizer"] is None
    assert c["findings"] == ["CDC-004"]


def test_recognised_but_flagged(tmp_path: Path) -> None:
    """A real 2FF chain with comb logic in front of it: the scheme is
    recognised, and the finding says why it is still not clean."""
    payload = _analyze_map("bad_comb_before_sync", tmp_path)
    hits = _async(payload)
    assert hits
    for c in hits:
        assert c["synchronizer"] == "2ff"
        assert c["findings"] == ["CDC-003"]


def test_gated_bus_without_ack_is_mux_with_cdc_012(tmp_path: Path) -> None:
    payload = _analyze_map("bad_functional_datahold_enable", tmp_path)
    (bus,) = [c for c in _async(payload) if c["width"] > 1]
    assert bus["synchronizer"] == "mux"
    assert bus["findings"] == ["CDC-012"]


# --- findings matching ---------------------------------------------------------


def test_waived_finding_does_not_appear(tmp_path: Path) -> None:
    """A waiver-file suppression removes the id; the verdict stays null
    because waiving a finding is not the same as recognising a sync."""
    waivers = tmp_path / "w.swl"
    waivers.write_text("waive CDC-001 .* hand-reviewed\n")
    payload = _analyze_map("bad_single_ff_sync", tmp_path, "--waivers", str(waivers))
    (c,) = _async(payload)
    assert c["findings"] == []
    assert c["synchronizer"] is None


def test_waiver_only_removes_the_waived_rule(tmp_path: Path) -> None:
    waivers = tmp_path / "w.swl"
    waivers.write_text("waive CDC-011 .* typed elsewhere\n")
    payload = _analyze_map(
        "bad_unconstrained_input_bus_two_domains", tmp_path, "--waivers", str(waivers)
    )
    assert _async(payload)
    for c in _async(payload):
        assert c["findings"] == ["CDC-004"]


@pytest.mark.skipif(not PYSLANG_INSTALLED, reason="pyslang not installed")
def test_pragma_waived_finding_does_not_appear(tmp_path: Path) -> None:
    fix = FIXTURES / "pragma_waived_single_ff"
    out = tmp_path / "map.json"
    result = runner.invoke(
        app,
        [
            "lint",
            "--frontend",
            "slang",
            "--top",
            "pragma_waived_single_ff",
            "--sdc",
            str(fix / "pragma_waived_single_ff.sdc"),
            "--emit-domain-map",
            str(out),
            str(fix / "pragma_waived_single_ff.sv"),
        ],
    )
    assert result.exit_code == 0, result.output
    (c,) = _async(json.loads(out.read_text()))
    assert c["synchronizer"] is None
    assert c["findings"] == []


def test_findings_sorted_and_deduplicated(tmp_path: Path) -> None:
    payload = _analyze_map("bad_unconstrained_input_bus_two_domains", tmp_path)
    port_sourced = [c for c in _async(payload) if "src_port" in c]
    assert port_sourced
    for c in port_sourced:
        assert c["findings"] == ["CDC-004", "CDC-011"]


def test_cdc_011_reaches_every_destination_of_the_port(tmp_path: Path) -> None:
    """CDC-011 anchors on one representative flop per port; the map
    hands it to every crossing from that port, not just the anchor."""
    payload = _analyze_map("bad_unconstrained_input_two_domains", tmp_path)
    hits = [c for c in payload["crossings"] if c["src_clock"] == UNCONSTRAINED_SENTINEL]
    assert len(hits) == 2
    assert all(c["findings"] == ["CDC-011"] for c in hits)


def test_cdc_011_anchor_does_not_leak_onto_flop_sourced_crossing(
    tmp_path: Path,
) -> None:
    """The CDC-011 anchor flop is also the *source* of a clean 2FF
    crossing; that crossing must stay clean."""
    payload = _analyze_map("marked_user_sync_port", tmp_path)
    (flop_sourced,) = [c for c in _async(payload) if "src_flop" in c]
    assert flop_sourced["synchronizer"] == "2ff"
    assert flop_sourced["findings"] == []


def test_group_finding_reaches_every_chain(tmp_path: Path) -> None:
    """CDC-005 attaches the group's first crossing only; both
    reconvergent chains must show it."""
    payload = _analyze_map("bad_reconvergent_sync", tmp_path)
    hits = _async(payload)
    assert len(hits) == 2
    assert all(c["findings"] == ["CDC-005"] for c in hits)
    assert all(c["synchronizer"] == "2ff" for c in hits)


def test_crossingless_finding_matches_by_destination_cell(tmp_path: Path) -> None:
    payload = _analyze_map("bad_cascaded_synchroniser", tmp_path)
    (c,) = _async(payload)
    assert c["findings"] == ["CDC-018"]


# --- "not checked" vs "clean" -------------------------------------------------


def test_no_findings_mode_reports_null_findings(tmp_path: Path) -> None:
    payload = _analyze_map("bad_single_ff_sync", tmp_path, "--no-findings")
    (c,) = _async(payload)
    assert c["findings"] is None
    assert c["synchronizer"] is None
    payload = _analyze_map("good_2ff_sync", tmp_path, "--no-findings")
    (c,) = _async(payload)
    assert c["findings"] is None
    assert c["synchronizer"] == "2ff"


def test_no_sdc_reports_null_findings(tmp_path: Path) -> None:
    payload = _analyze_map("good_2ff_sync", tmp_path, sdc=False)
    assert payload["crossings"]
    for c in payload["crossings"]:
        assert c["findings"] is None
    assert {c["synchronizer"] for c in payload["crossings"]} == {"2ff"}


# --- clock-network crossings ---------------------------------------------------


def test_clock_network_crossing_flagged(tmp_path: Path) -> None:
    payload = _analyze_map("bad_async_clock_mux", tmp_path)
    cn = payload["clock_network_crossings"]
    assert cn
    for c in cn:
        assert c["synchronizer"] is None
        assert c["findings"] == ["CDC-010"]


def test_clock_network_crossing_glitchless(tmp_path: Path) -> None:
    payload = _analyze_map("good_glitchless_mux_marked", tmp_path)
    cn = payload["clock_network_crossings"]
    assert cn
    for c in cn:
        assert c["synchronizer"] == "glitchless"
        assert c["findings"] == []


# --- unit: matching rule -------------------------------------------------------


def _crossings(name: str) -> tuple[netlist.Module, list[Crossing]]:
    netlist_path, sdc_path = _fixture(name)
    module = netlist.load(netlist_path)
    spec = sdc_mod.parse_file(sdc_path)
    sdc_mod.synthesize_unconstrained_inputs(spec, module)
    cs = find_crossings(
        module,
        port_clock=spec.port_clock,
        pin_clocks=spec.pin_clocks,
        clock_for_port=spec.clock_for_port,
    )
    return module, cs


def test_index_ignores_lifted_and_reset_findings() -> None:
    """A finding whose ``cell_name`` is not an endpoint of the crossing it
    carries (a boundary-lifted finding, re-anchored on the instance) and
    an ``RDC-*`` finding naming an endpoint flop both stay off the map."""
    _module, cs = _crossings("good_2ff_sync")
    (c,) = [x for x in cs if x.src_flop is not None]
    lifted = Violation("CDC-001", "error", "inside", crossing=c, cell_name="u_ip")
    rdc = Violation("RDC-004", "warning", "reset", cell_name=c.dst_flop.cell.name)
    index = _FindingIndex.build([lifted, rdc], cs)
    assert index.for_crossing(c) == []


def test_index_matches_carried_crossing_only() -> None:
    _module, cs = _crossings("bad_comb_before_sync")
    a, b = [x for x in cs if x.src_flop is not None][:2]
    v = Violation("CDC-003", "error", "m", crossing=a, cell_name=a.dst_flop.cell.name)
    index = _FindingIndex.build([v, v], cs)
    assert index.for_crossing(a) == ["CDC-003"]
    # Same dst flop, different source: a crossing-carrying finding does
    # not spread to it through the shared destination cell.
    assert a.dst_flop.cell.name == b.dst_flop.cell.name
    assert index.for_crossing(b) == []


def test_crossing_synchronizer_unit() -> None:
    module, cs = _crossings("good_2ff_sync")
    ctx = _build_context(module, None)
    (c,) = [x for x in cs if x.src_flop is not None]
    assert crossing_synchronizer(module, c, ctx) == "2ff"
    module, cs = _crossings("bad_single_ff_sync")
    ctx = _build_context(module, None)
    (c,) = [x for x in cs if x.src_flop is not None]
    assert crossing_synchronizer(module, c, ctx) is None
