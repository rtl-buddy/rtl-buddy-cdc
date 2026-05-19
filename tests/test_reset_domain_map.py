"""Reset-domain-map contract test (issue #108).

Pairs a golden-file regression for the ``bad_marked_reset_polarity``
fixture with explicit schema/type/ordering assertions. The golden
file is the behaviour pin; the explicit checks are the contract pin —
the parts of the schema downstream consumers (chiefly
``rtl-buddy-view``) commit to. Renaming a documented field requires
a ``schema_version`` bump.

Also exercises every RDC fixture (the issue's "produces a valid file
for all RDC fixtures from #107" acceptance criterion) to make sure
the serializer survives the full structural variety.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rtl_buddy_cdc import netlist, sdc as sdc_mod
from rtl_buddy_cdc.domain import assign_domains
from rtl_buddy_cdc.reset_domain import (
    assign_reset_domains,
    find_reset_crossings,
    find_reset_synchronizers,
)
from rtl_buddy_cdc.reset_domain_map import SCHEMA_VERSION, build_reset_domain_map
from rtl_buddy_cdc.rules import (
    user_reset_polarity_overrides,
    user_reset_sync_flop_names,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"
FIX_DIR = FIXTURES_DIR / "bad_marked_reset_polarity"
JSON = FIX_DIR / "bad_marked_reset_polarity.json"
SDC = FIX_DIR / "bad_marked_reset_polarity.sdc"
GOLDEN = FIX_DIR / "bad_marked_reset_polarity.reset-domain-map.json"


def _build_for_fixture(name: str) -> dict:
    fix_dir = FIXTURES_DIR / name
    json_path = fix_dir / f"{name}.json"
    sdc_path = fix_dir / f"{name}.sdc"
    module = netlist.load(json_path)
    pin_clocks: dict[str, str] | None = None
    if sdc_path.exists():
        spec = sdc_mod.parse_file(sdc_path)
        sdc_mod.synthesize_unconstrained_inputs(spec, module)
        pin_clocks = spec.pin_clocks
    flop_domains_list = assign_domains(module, pin_clocks=pin_clocks)
    flop_clocks = {fd.flop.cell.name: fd.clock for fd in flop_domains_list}
    reset_domains = assign_reset_domains(module)
    polarity_overrides = user_reset_polarity_overrides(module)
    syncs = find_reset_synchronizers(
        module,
        flop_clocks,
        extra_synchronizers=user_reset_sync_flop_names(module),
    )
    crossings = find_reset_crossings(
        module,
        flop_clocks,
        recognised_syncs=syncs,
        polarity_overrides=polarity_overrides,
    )
    return build_reset_domain_map(
        module,
        reset_domains,
        flop_clocks,
        syncs,
        polarity_overrides,
        crossings,
    )


def _build() -> dict:
    return _build_for_fixture("bad_marked_reset_polarity")


def test_golden_matches() -> None:
    """Byte-exact golden diff for the ``bad_marked_reset_polarity`` fixture.

    Regenerate with::

        uv run rtl-buddy-cdc analyze \\
            --netlist tests/fixtures/bad_marked_reset_polarity/bad_marked_reset_polarity.json \\
            --sdc tests/fixtures/bad_marked_reset_polarity/bad_marked_reset_polarity.sdc \\
            --emit-reset-domain-map tests/fixtures/bad_marked_reset_polarity/bad_marked_reset_polarity.reset-domain-map.json \\
            --no-findings
    """
    if not JSON.exists():
        pytest.skip(f"fixture not built: {JSON}")
    expected = json.loads(GOLDEN.read_text())
    actual = _build()
    assert actual == expected, (
        "reset-domain-map output drift — regenerate the golden if intentional"
    )


def test_schema_version_is_string_and_pinned() -> None:
    payload = _build()
    assert payload["schema_version"] == SCHEMA_VERSION
    assert isinstance(payload["schema_version"], str)


def test_top_level_keys() -> None:
    """The v1.0 schema's top-level keys are PUBLIC API."""
    payload = _build()
    expected = {
        "schema_version",
        "generator",
        "design",
        "reset_sources",
        "reset_synchronizers",
        "flop_resets",
        "reset_crossings",
    }
    assert set(payload.keys()) == expected


def test_generator_block() -> None:
    payload = _build()
    gen = payload["generator"]
    assert gen["name"] == "rtl-buddy-cdc"
    assert isinstance(gen["version"], str)


def test_design_block() -> None:
    payload = _build()
    assert payload["design"] == {
        "top": "bad_marked_reset_polarity",
        "frontend": "yosys",
    }


def test_reset_sources_shape() -> None:
    payload = _build()
    assert payload["reset_sources"], (
        "bad_marked_reset_polarity has at least one upstream reset source"
    )
    for s in payload["reset_sources"]:
        assert isinstance(s["name"], str)
        assert s["source"] in {"port", "inferred", "constant"}
        assert s["polarity"] in {"high", "low"}
        assert s["type"] in {"sync", "async"}
        assert isinstance(s["via_synchronizer"], bool)
        # ``clock`` is null in v1.0 until the rule-context-aware sync-clock
        # population lands (see ResetSource docstring).
        assert s["clock"] is None or isinstance(s["clock"], str)


def test_reset_sources_declared_polarity_present_for_overrides() -> None:
    """A port carrying ``(* reset_polarity *)`` records the declaration."""
    payload = _build()
    port_entries = [s for s in payload["reset_sources"] if s["source"] == "port"]
    assert port_entries, "fixture has at least one port-sourced reset"
    rst_n = next(s for s in port_entries if s["name"] == "rst_n")
    assert rst_n["declared_polarity"] == "low"


def test_reset_sources_sorted() -> None:
    payload = _build()
    keys = [(s["source"], s["name"]) for s in payload["reset_sources"]]
    assert keys == sorted(keys)


def test_flop_resets_sorted_by_instance_path() -> None:
    payload = _build()
    paths = [fr["instance_path"] for fr in payload["flop_resets"]]
    assert paths == sorted(paths)
    for fr in payload["flop_resets"]:
        assert fr["instance_path"].startswith("bad_marked_reset_polarity.")
        assert fr["reset_kind"] in {"port", "inferred", "constant", "comb"}
        assert fr["polarity"] in {"high", "low"}
        assert fr["type"] in {"sync", "async"}


def test_reset_crossings_shape() -> None:
    payload = _build()
    # bad_marked_reset_polarity is engineered to fire exactly one
    # polarity-mismatch crossing.
    assert len(payload["reset_crossings"]) == 1
    c = payload["reset_crossings"][0]
    assert c["kind"] == "polarity-mismatch"
    assert c["reset"] == "rst_n"
    assert c["reset_kind"] == "port"
    assert c["polarity"] == "high"  # the flop's inferred polarity (the bug)
    assert c["flop_clock"] == "clk"


def test_reset_crossings_sorted() -> None:
    payload = _build()
    keys = [(c["instance_path"], c["kind"]) for c in payload["reset_crossings"]]
    assert keys == sorted(keys)


def test_deterministic() -> None:
    """Two builds on the same inputs must emit the same byte sequence."""
    a = json.dumps(_build(), indent=2, sort_keys=False)
    b = json.dumps(_build(), indent=2, sort_keys=False)
    assert a == b


# Every RDC fixture from #107 must serialize without raising. The
# bodies' shapes vary — the acceptance criterion is only "produces a
# valid file" — so we just check the contract envelope on each.
_RDC_FIXTURES = [
    "bad_marked_reset_polarity",
    "good_marked_reset_polarity",
    "bad_rdc_002_polarity_mismatch",
    "good_rdc_002_polarity_match",
    "bad_rdc_003_sync_reset_crossing",
    "good_rdc_003_sync_reset_synced",
    "bad_rdc_004_comb_driven_reset",
    "good_rdc_004_registered_reset",
    "bad_rdc_005_multi_source_reset",
    "good_rdc_005_muxed_reset",
    "bad_reset_crossing",
    "bad_reset_tree",
    "good_reset_sync",
    "marked_reset_sync",
]


@pytest.mark.parametrize("name", _RDC_FIXTURES)
def test_builds_for_every_rdc_fixture(name: str) -> None:
    payload = _build_for_fixture(name)
    assert payload["schema_version"] == SCHEMA_VERSION
    assert payload["design"]["top"] == name
    # Envelope shape — same on every fixture.
    assert set(payload.keys()) == {
        "schema_version",
        "generator",
        "design",
        "reset_sources",
        "reset_synchronizers",
        "flop_resets",
        "reset_crossings",
    }
    # Each collection is a JSON array.
    for k in ("reset_sources", "reset_synchronizers", "flop_resets", "reset_crossings"):
        assert isinstance(payload[k], list)
