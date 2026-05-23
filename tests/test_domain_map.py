"""Domain-map contract test (issue #106).

Pairs a golden-file regression for the ``ip_cdc_handshake`` fixture
with explicit schema/type/ordering assertions. The golden file is the
behaviour pin; the explicit checks are the contract pin — the parts of
the schema downstream consumers (chiefly ``rtl-buddy-view``) commit to.
Renaming a documented field requires a ``schema_version`` bump.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rtl_buddy_cdc import netlist, sdc as sdc_mod
from rtl_buddy_cdc.domain import assign_domains, find_crossings
from rtl_buddy_cdc.domain_map import SCHEMA_VERSION, build_domain_map

FIX_DIR = Path(__file__).parent / "fixtures" / "ip_cdc_handshake"
JSON = FIX_DIR / "ip_cdc_handshake.json"
SDC = FIX_DIR / "ip_cdc_handshake.sdc"
GOLDEN = FIX_DIR / "ip_cdc_handshake.domain-map.json"


def _build(sdc_path: Path | None) -> dict:
    module = netlist.load(JSON)
    spec: sdc_mod.ClockSpec | None = None
    pin_clocks: dict[str, str] | None = None
    port_clock: dict[str, str] | None = None
    if sdc_path is not None:
        spec = sdc_mod.parse_file(sdc_path)
        sdc_mod.synthesize_unconstrained_inputs(spec, module)
        pin_clocks = spec.pin_clocks
        port_clock = spec.port_clock
    clock_for_port = spec.clock_for_port if spec is not None else None
    domains = assign_domains(
        module, pin_clocks=pin_clocks, clock_for_port=clock_for_port
    )
    crossings = find_crossings(
        module,
        port_clock=port_clock,
        pin_clocks=pin_clocks,
        clock_for_port=clock_for_port,
    )
    async_cs = []
    if spec is not None:
        for c in crossings:
            a = spec.clock_for_port(c.src_clock) or c.src_clock
            b = spec.clock_for_port(c.dst_clock) or c.dst_clock
            if spec.is_unreachable_crossing(a, b):
                continue
            if spec.are_async(a, b):
                async_cs.append(c)
    return build_domain_map(module, domains, crossings, spec, async_crossings=async_cs)


def test_golden_matches() -> None:
    """Byte-exact golden diff for the ``ip_cdc_handshake`` fixture.

    The golden is the source-of-truth artefact a downstream consumer
    would see; any drift is a contract change that needs an explicit
    update (and probably a ``schema_version`` bump).
    """
    if not JSON.exists():
        pytest.skip(f"fixture not built: {JSON}")
    expected = json.loads(GOLDEN.read_text())
    actual = _build(SDC)
    assert actual == expected, (
        "domain-map output drift — regenerate the golden if intentional:\n"
        f"  uv run rtl-buddy-cdc analyze --netlist {JSON} --sdc {SDC} "
        f"--emit-domain-map {GOLDEN} --no-findings"
    )


def test_schema_version_is_string_and_pinned() -> None:
    payload = _build(SDC)
    assert payload["schema_version"] == SCHEMA_VERSION
    assert isinstance(payload["schema_version"], str)


def test_top_level_keys() -> None:
    """The v1.0 schema's top-level keys are PUBLIC API."""
    payload = _build(SDC)
    expected = {
        "schema_version",
        "generator",
        "design",
        "clocks",
        "generated_clocks",
        "clock_groups",
        "false_path_pairs",
        "flop_domains",
        "port_domains",
        "crossings",
    }
    assert set(payload.keys()) == expected


def test_generator_block() -> None:
    payload = _build(SDC)
    gen = payload["generator"]
    assert gen["name"] == "rtl-buddy-cdc"
    assert isinstance(gen["version"], str)


def test_design_block() -> None:
    payload = _build(SDC)
    assert payload["design"] == {"top": "ip_cdc_handshake", "frontend": "yosys"}


def test_clock_period_is_float() -> None:
    payload = _build(SDC)
    assert payload["clocks"], "ip_cdc_handshake SDC declares two clocks"
    for c in payload["clocks"]:
        assert isinstance(c["period"], float)
        assert isinstance(c["name"], str)
        assert isinstance(c["ports"], list)


def test_clocks_sorted_by_name() -> None:
    payload = _build(SDC)
    names = [c["name"] for c in payload["clocks"]]
    assert names == sorted(names)


def test_flop_domains_sorted_by_instance_path() -> None:
    payload = _build(SDC)
    paths = [fd["instance_path"] for fd in payload["flop_domains"]]
    assert paths == sorted(paths)
    # Every flop carries an instance_path rooted at the top module name.
    for fd in payload["flop_domains"]:
        assert fd["instance_path"].startswith("ip_cdc_handshake.")
        assert isinstance(fd["clock"], str) or fd["clock"] is None


def test_port_domains_sorted() -> None:
    payload = _build(SDC)
    keys = [(p["module"], p["port"]) for p in payload["port_domains"]]
    assert keys == sorted(keys)
    # ip_cdc_handshake.sdc types src_data + src_valid via set_input_delay;
    # the clock-source ports (src_clk, dst_clk) and the unconstrained
    # inputs (src_rst_n, dst_rst_n) are excluded — the first because
    # they're already in clocks[].ports, the second because the unconst-
    # rained sentinel is filtered out.
    assert {(p["port"], p["clock"]) for p in payload["port_domains"]} == {
        ("src_data", "src_clk"),
        ("src_valid", "src_clk"),
    }


def test_crossings_have_async_flag() -> None:
    payload = _build(SDC)
    for c in payload["crossings"]:
        assert isinstance(c["async_per_sdc"], bool)
        assert isinstance(c["width"], int)
        assert isinstance(c["min_hops"], int)
        # Endpoint join: dst_flop must round-trip to a flop_domains entry.
        flop_paths = {fd["instance_path"] for fd in payload["flop_domains"]}
        assert c["dst_flop"] in flop_paths
        if "src_flop" in c:
            assert c["src_flop"] in flop_paths


def test_flop_domains_carry_source_instance_path() -> None:
    """Every flop entry exposes ``source_instance_path`` (issue #136).

    The field is the deepest enclosing SystemVerilog-source module
    instance — the chain reachable by stripping the synth-generated
    leaf handle (``$procdff$N``, ``$slang$sdff$N``) from
    ``instance_path``. Always rooted at the top module name; never
    omitted (``null`` when unresolvable so consumers can distinguish
    "no resolution" from "old producer").
    """
    payload = _build(SDC)
    assert payload["flop_domains"], "fixture has flops"
    for fd in payload["flop_domains"]:
        assert "source_instance_path" in fd
        sip = fd["source_instance_path"]
        assert sip is None or isinstance(sip, str)
        # In this fixture every flop is resolvable to the top or a child
        # module instance — null would signal a regression.
        assert sip is not None
        assert sip == "ip_cdc_handshake" or sip.startswith("ip_cdc_handshake.")
        # The source-instance path is a strict prefix of the synth
        # leaf's instance path: dropping the trailing dot-segment of
        # ``instance_path`` must reproduce it.
        ip = fd["instance_path"]
        assert ip.rsplit(".", 1)[0] == sip


def test_flop_domains_source_instance_path_buckets_synth_flops() -> None:
    """Top-level synth flops collapse to the top instance; nested ones
    keep the parent chain. Bucket assertion documents the resolver
    contract for the two shapes present in the fixture."""
    payload = _build(SDC)
    buckets: dict[str, int] = {}
    for fd in payload["flop_domains"]:
        sip = fd["source_instance_path"]
        assert sip is not None
        buckets[sip] = buckets.get(sip, 0) + 1
    # ip_cdc_handshake has six top-level $procdff cells and two
    # u_sync_{req,ack} instances each holding two synth flops.
    assert buckets == {
        "ip_cdc_handshake": 6,
        "ip_cdc_handshake.u_sync_req": 2,
        "ip_cdc_handshake.u_sync_ack": 2,
    }


def test_crossings_carry_source_instance_path() -> None:
    """Crossings expose ``dst_source_instance_path`` and, when the
    source endpoint is a flop, ``src_source_instance_path`` (issue #136).
    ``src_source_instance_path`` is omitted (not null) when the source
    is a top-level port — port-driven crossings already carry
    ``src_port`` and don't need a source-instance pointer."""
    payload = _build(SDC)
    sip_by_path = {
        fd["instance_path"]: fd["source_instance_path"]
        for fd in payload["flop_domains"]
    }
    assert payload["crossings"], "fixture has crossings"
    for c in payload["crossings"]:
        assert "dst_source_instance_path" in c
        assert c["dst_source_instance_path"] == sip_by_path[c["dst_flop"]]
        if "src_flop" in c:
            assert "src_source_instance_path" in c
            assert c["src_source_instance_path"] == sip_by_path[c["src_flop"]]
        else:
            assert "src_source_instance_path" not in c


def test_crossings_sorted_deterministic() -> None:
    """Two builds on the same inputs must emit the same byte sequence."""
    a = json.dumps(_build(SDC), indent=2, sort_keys=False)
    b = json.dumps(_build(SDC), indent=2, sort_keys=False)
    assert a == b


def test_no_sdc_emits_empty_clocks() -> None:
    """No-SDC runs leave the clock metadata empty so consumers can
    detect the case via ``clocks.length == 0``. ``flop_domains`` and
    ``crossings`` still carry the structural view (matching the
    analyzer's no-SDC ``analyze`` behaviour — every flop's CLK pin
    still traces back to a top-level port name)."""
    payload = _build(None)
    assert payload["clocks"] == []
    assert payload["generated_clocks"] == []
    assert payload["clock_groups"] == []
    assert payload["false_path_pairs"] == []
    assert payload["port_domains"] == []
    # All crossings are emitted, none are tagged async (no SDC to ask).
    for c in payload["crossings"]:
        assert c["async_per_sdc"] is False
