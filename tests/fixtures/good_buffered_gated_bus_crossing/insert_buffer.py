"""Rewrite the Yosys-emitted JSON to splice a chain of ``$_BUF_``
cells between the load-mux output and the destination flop's ``D``
pin.

The on-disk JSON committed alongside the fixture is the *rewritten*
version — this script is the recipe, not a runtime dependency of
the test suite. Re-run after touching the SV or to regenerate the
3-hop variant used by the budget-exceeded test.

Usage:
    python tests/fixtures/good_buffered_gated_bus_crossing/insert_buffer.py [hops]

``hops`` defaults to 1. Pass 3 (or any value > _GATING_BUF_BUDGET in
rules.py) to produce a netlist that the CDC-004 detector must reject.
The output is always written to
``good_buffered_gated_bus_crossing.json`` in the same directory.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent
SV = HERE / "good_buffered_gated_bus_crossing.sv"
JSON = HERE / "good_buffered_gated_bus_crossing.json"


def yosys_build() -> None:
    subprocess.run(
        [
            "yosys",
            "-q",
            "-p",
            (
                f"read_verilog -sv {SV}; "
                f"hierarchy -top good_buffered_gated_bus_crossing; "
                f"proc; flatten; opt_clean; "
                f"write_json {JSON}"
            ),
        ],
        check=True,
    )


def insert_buffer_chain(hops: int) -> None:
    data = json.loads(JSON.read_text())
    mod = data["modules"]["good_buffered_gated_bus_crossing"]

    # Find the data flop (multi-bit $dff with WIDTH>1) and the $mux
    # that drives its D pin.
    dst_flop_name: str | None = None
    for name, cell in mod["cells"].items():
        if cell["type"] != "$dff":
            continue
        width = int(cell["parameters"].get("WIDTH", "1"), 2)
        if width > 1:
            dst_flop_name = name
            break
    if dst_flop_name is None:
        raise RuntimeError("no multi-bit $dff in fixture")
    dst_flop = mod["cells"][dst_flop_name]
    d_bits = list(dst_flop["connections"]["D"])

    mux_name: str | None = None
    for name, cell in mod["cells"].items():
        if cell["type"] != "$mux":
            continue
        if list(cell["connections"]["Y"]) == d_bits:
            mux_name = name
            break
    if mux_name is None:
        raise RuntimeError("no $mux driving the dst-flop D pin")
    mux = mod["cells"][mux_name]

    # Allocate fresh net IDs for each intermediate stage of the chain.
    used_ids: set[int] = set()
    for cell in mod["cells"].values():
        for bits in cell["connections"].values():
            for b in bits:
                if isinstance(b, int):
                    used_ids.add(b)
    for port in mod["ports"].values():
        for b in port["bits"]:
            if isinstance(b, int):
                used_ids.add(b)
    nxt = max(used_ids) + 1
    width = len(d_bits)

    # Build the per-stage net allocation. ``stage_bits[k]`` is the
    # ``width``-wide vector between buffer ``k`` and buffer ``k+1``.
    # The mux drives ``stage_bits[0]``; buffer ``k`` reads
    # ``stage_bits[k]`` and writes ``stage_bits[k+1]``; the final
    # buffer writes ``d_bits`` (the dst flop's D).
    stage_bits: list[list[int]] = []
    for _ in range(hops):
        stage_bits.append(list(range(nxt, nxt + width)))
        nxt += width
    # Rewire the mux Y to the first stage's net.
    mux["connections"]["Y"] = stage_bits[0] if stage_bits else d_bits

    # Insert the chain of single-bit $_BUF_ cells.
    for k in range(hops):
        src_vec = stage_bits[k]
        # Final stage drives the dst flop's original D bits.
        dst_vec = d_bits if k == hops - 1 else stage_bits[k + 1]
        for lane in range(width):
            cell_name = f"_gating_buf_h{k}_b{lane}"
            mod["cells"][cell_name] = {
                "hide_name": 0,
                "type": "$_BUF_",
                "parameters": {},
                "attributes": {},
                "port_directions": {"A": "input", "Y": "output"},
                "connections": {"A": [src_vec[lane]], "Y": [dst_vec[lane]]},
            }

    JSON.write_text(json.dumps(data, indent=2))


def main() -> None:
    hops = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    if hops < 1:
        raise SystemExit("hops must be >= 1")
    yosys_build()
    insert_buffer_chain(hops)
    print(f"wrote {JSON} with {hops} buffer hop(s)")


if __name__ == "__main__":
    main()
