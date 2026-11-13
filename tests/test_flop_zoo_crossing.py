"""Flop families the rest of the corpus never produces (rtl-buddy-cdc#331).

``flop_zoo_crossing`` has one dst_clk flop per family: ``$aldff``,
``$dffsr``, ``$sdffce``, ``$adffe`` and ``$dffe``. Each captures a
src_clk flop with no synchroniser, so every one of them is a CDC-001.
The gate-level corpus lowers the same source to the matching ``$_*_``
cells; this file pins the RTL-level result.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from typer.testing import CliRunner

from rtl_buddy_cdc import netlist
from rtl_buddy_cdc.cli import app
from rtl_buddy_cdc.flops import find_flops

FIX_DIR = Path(__file__).parent / "fixtures" / "flop_zoo_crossing"
JSON = FIX_DIR / "flop_zoo_crossing.json"
SDC = FIX_DIR / "flop_zoo_crossing.sdc"


def test_netlist_holds_each_missing_flop_family() -> None:
    types = Counter(f.cell.type for f in find_flops(netlist.load(JSON)))
    assert types == {
        "$dff": 1,  # src_q
        "$aldff": 1,
        "$dffsr": 1,
        "$sdffce": 1,
        "$adffe": 1,
        "$dffe": 1,
    }


def test_every_family_is_a_crossing_destination() -> None:
    result = CliRunner().invoke(
        app, ["analyze", "-n", str(JSON), "-s", str(SDC), "-f", "json"]
    )
    assert result.exit_code == 1, result.output
    report = json.loads(result.stdout)
    fired = Counter(v["rule_id"] for v in report["violations"])
    # One CDC-001 per family. q_dffsr's CLR is the constant-leg mux
    # ``rst_n ? 0 : 1``, an inverter of one reset rather than a reset mux,
    # so RDC-006 stays silent (rtl-buddy-cdc#354).
    assert fired == {"CDC-001": 5}
    dst_flops = {c["dst_flop"] for c in report["crossings"]}
    assert len(dst_flops) == 5
