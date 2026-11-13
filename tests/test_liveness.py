"""Unobservable cells never change a verdict (rtl-buddy-cdc#338).

:mod:`rtl_buddy_cdc.liveness` keeps only the cells whose outputs reach an
observation point (an output / inout port, a ``keep`` net, an input pin
of an instance or other non-pure cell), so ``analyze`` reaches the same
verdict with or without ``opt_clean`` in the netlist flow.

The ``<name>.noclean.json`` netlists are the three fixtures whose
verdict used to change without ``opt_clean``, written by the gate-level
corpus front half with the clean-up pass dropped (from the repo root)::

    uv run python -c "import sys; sys.path.insert(0, 'tests'); \\
      import test_gate_level_corpus as g; fx = g.fixture('<name>'); \\
      g._run_yosys(g.yosys_script(fx, 'rtl', \\
        'tests/fixtures/<name>/<name>.noclean.json', cleanup=False)\\
      ).check_returncode()"

Each case is paired with the same netlist where the dead cell is made
observable by wiring it to a new output port: the rule it used to fire
fires again, so the liveness pass removes only what is unobservable.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rtl_buddy_cdc.cli import app
from rtl_buddy_cdc.liveness import live_cells, prune_dead_cells, pure_cell_outputs
from rtl_buddy_cdc.netlist import Bit, Cell, Module, Netname, Port

FIX = Path(__file__).parent / "fixtures"
runner = CliRunner()


# --- liveness on synthetic modules -------------------------------------------


def _port(name: str, direction: str, *bits: Bit) -> Port:
    return Port(name, direction, tuple(bits))


def _mod(
    cells: list[Cell],
    ports: list[Port],
    netnames: list[Netname] | None = None,
) -> Module:
    return Module(
        "m",
        {p.name: p for p in ports},
        {c.name: c for c in cells},
        {n.name: n for n in netnames or []},
    )


CLK, D, OUT = 1, 2, 3


def _flop(name: str, d: int, q: int, clk: int = CLK, **extra: tuple[Bit, ...]) -> Cell:
    return Cell(name, "$dff", {"CLK": (clk,), "D": (d,), "Q": (q,), **extra})


def test_unloaded_inverter_of_a_clock_is_dead() -> None:
    m = _mod(
        [
            _flop("f", D, OUT),
            Cell("inv", "$not", {"A": (CLK,), "Y": (10,)}),
        ],
        [_port("clk", "input", CLK), _port("d", "input", D), _port("q", "output", OUT)],
    )
    assert live_cells(m) == frozenset({"f"})
    assert set(prune_dead_cells(m).cells) == {"f"}


def test_unread_register_and_its_next_state_logic_are_dead() -> None:
    """The ``for``-loop counter ``proc`` leaves: Q feeds only its own D."""
    m = _mod(
        [
            _flop("f", D, OUT),
            _flop("i", 21, 20),
            Cell("inc", "$add", {"A": (20,), "B": ("1",), "Y": (21,)}),
        ],
        [_port("clk", "input", CLK), _port("d", "input", D), _port("q", "output", OUT)],
    )
    assert live_cells(m) == frozenset({"f"})


def test_live_flop_keeps_its_whole_fanin_including_clock_and_enable() -> None:
    m = _mod(
        [
            Cell(
                "f",
                "$adffe",
                {"CLK": (11,), "D": (12,), "EN": (13,), "ARST": (14,), "Q": (OUT,)},
            ),
            Cell("g", "$and", {"A": (CLK,), "B": (D,), "Y": (11,)}),
            Cell("n", "$_NOT_", {"A": (D,), "Y": (12,)}),
            Cell("e", "$reduce_or", {"A": (D, CLK), "Y": (13,)}),
            _flop("r", D, 14),
        ],
        [_port("clk", "input", CLK), _port("d", "input", D), _port("q", "output", OUT)],
    )
    assert live_cells(m) is None  # everything is live
    assert prune_dead_cells(m) is m


def test_flop_read_only_through_another_live_flop_is_live() -> None:
    m = _mod(
        [_flop("a", D, 10), _flop("b", 10, OUT), _flop("dead", 10, 11)],
        [_port("d", "input", D), _port("q", "output", OUT)],
    )
    assert live_cells(m) == frozenset({"a", "b"})


def test_instance_input_pins_are_observation_points() -> None:
    """A blackbox instance observes its inputs (per the definition's
    port directions) but not its outputs; an instance of an unknown
    module observes every pin."""
    bb = _mod([], [_port("i", "input", 1), _port("o", "output", 2)])
    m = _mod(
        [
            Cell("u", "ip", {"i": (10,), "o": (11,)}),
            _flop("feeds_in", D, 10),
            Cell("reads_out", "$not", {"A": (11,), "Y": (12,)}),
            Cell("w", "lib_cell", {"Z": (13,)}),
            _flop("feeds_unknown", D, 13),
        ],
        [_port("d", "input", D)],
    )
    live = live_cells(m, {"ip": bb})
    assert live == frozenset({"u", "w", "feeds_in", "feeds_unknown"})


def test_non_pure_cells_and_keep_are_observation_points() -> None:
    m = _mod(
        [
            _flop("into_assert", D, 10),
            Cell("chk", "$assert", {"A": (10,), "EN": ("1",)}),
            Cell("kept", "$not", {"A": (D,), "Y": (11,)}, attributes={"keep": "1"}),
            _flop("into_keep_net", D, 12),
            _flop("dead", D, 13),
            _flop("f", D, OUT),
        ],
        [_port("d", "input", D), _port("q", "inout", OUT)],
        [Netname("probe", (12,), {"keep": "00000000000000000000000000000001"})],
    )
    assert live_cells(m) == frozenset(
        {"into_assert", "chk", "kept", "into_keep_net", "f"}
    )


def test_module_without_observation_points_is_analysed_whole() -> None:
    """No output port, instance or keep: a fragment, not a design that
    computes nothing. Nothing is pruned."""
    m = _mod([_flop("a", D, 10), _flop("b", 10, 11)], [_port("d", "input", D)])
    assert live_cells(m) is None
    assert prune_dead_cells(m) is m


def test_bit_with_several_drivers_keeps_every_driver() -> None:
    m = _mod(
        [
            Cell("t1", "$_TBUF_", {"A": (D,), "E": (5,), "Y": (OUT,)}),
            Cell("t2", "$_TBUF_", {"A": (6,), "E": (7,), "Y": (OUT,)}),
            Cell("dead", "$_AND_", {"A": (D,), "B": (6,), "Y": (9,)}),
        ],
        [_port("d", "input", D, 5, 6, 7), _port("q", "output", OUT)],
    )
    assert live_cells(m) == frozenset({"t1", "t2"})


@pytest.mark.parametrize(
    ("cell_type", "outputs"),
    [
        ("$dff", {"Q"}),
        ("$_DFFE_PN0P_", {"Q"}),
        ("$_DLATCH_P_", {"Q"}),
        ("$_FF_", {"Q"}),
        ("$_MUX4_", {"Y"}),
        ("$_AOI3_", {"Y"}),
        ("$pmux", {"Y"}),
        ("$alu", {"X", "Y", "CO"}),
        ("$lcu", {"CO"}),
        ("$mem_v2", None),
        ("$print", None),
        ("$scopeinfo", None),
        ("$_DFF_Q_", None),  # malformed gate name: unknown, never pruned
        ("my_lib_cell", None),
    ],
)
def test_pure_cell_outputs(cell_type: str, outputs: set[str] | None) -> None:
    got = pure_cell_outputs(cell_type)
    assert (None if got is None else set(got)) == outputs


# --- the issue's cases, end to end ------------------------------------------


def _fired(netlist: Path, sdc: Path) -> dict[str, int]:
    r = runner.invoke(
        app, ["analyze", "-n", str(netlist), "-s", str(sdc), "-f", "json"]
    )
    assert r.exit_code in (0, 1), r.output
    report = json.loads(r.stdout)
    return dict(sorted(Counter(v["rule_id"] for v in report["violations"]).items()))


def _paths(name: str) -> tuple[Path, Path, Path]:
    d = FIX / name
    return d / f"{name}.json", d / f"{name}.noclean.json", d / f"{name}.sdc"


def _expose(raw: Path, tmp_path: Path, cell_name: str, pin: str) -> Path:
    """``raw`` with ``cell_name``'s ``pin`` wired to a new output port,
    which makes that cell (and its fan-in) observable."""
    data = json.loads(raw.read_text())
    (top,) = [m for m in data["modules"].values() if m.get("attributes", {}).get("top")]
    bits = top["cells"][cell_name]["connections"][pin]
    top["ports"]["probe"] = {"direction": "output", "bits": bits}
    out = tmp_path / raw.name
    out.write_text(json.dumps(data))
    return out


@pytest.mark.parametrize(
    "name", ["clock_gating", "clock_combine_latch", "ip_cdc_handshake"]
)
def test_noclean_netlist_matches_committed_verdict(name: str) -> None:
    committed, raw, sdc = _paths(name)
    assert _fired(raw, sdc) == _fired(committed, sdc)


def _dead_inverter_of(raw: Path, port: str) -> str:
    """Name of a ``$not`` in ``raw`` that inverts input ``port`` and has
    no reader (one of the dead cells the committed netlist lacks)."""
    data = json.loads(raw.read_text())
    (top,) = [m for m in data["modules"].values() if m.get("attributes", {}).get("top")]
    read: set[int] = set()
    for c in top["cells"].values():
        for pin, bits in c["connections"].items():
            if c["port_directions"].get(pin) != "output":
                read.update(b for b in bits if isinstance(b, int))
    (port_bit,) = top["ports"][port]["bits"]
    for name, c in sorted(top["cells"].items()):
        conns = c["connections"]
        if c["type"] == "$not" and conns["A"] == [port_bit]:
            if not read.intersection(conns["Y"]):
                return name
    raise AssertionError(f"no dead inverter of {port} in {raw}")


@pytest.mark.parametrize(
    ("name", "clock", "expected"),
    [
        ("clock_gating", "clk", {"CDC-008": 1}),
        ("clock_combine_latch", "clkB", {"CDC-008": 1, "CDC-023": 1}),
    ],
)
def test_live_inverter_of_a_clock_still_fires_cdc_008(
    name: str, clock: str, expected: dict[str, int], tmp_path: Path
) -> None:
    """The unloaded ``$not`` of a clock, made observable, is clock-as-data
    again."""
    _, raw, sdc = _paths(name)
    exposed = _expose(raw, tmp_path, _dead_inverter_of(raw, clock), "Y")
    assert _fired(exposed, sdc) == expected


def test_live_counter_register_still_fires_cdc_004_and_cdc_011(
    tmp_path: Path,
) -> None:
    """``ip_cdc_handshake``: the ``for (int i …)`` register ``proc`` keeps
    in each synchroniser's reset branch is captured under an
    unconstrained reset port. Nothing reads it, so it is not reported;
    once its Q drives a port, the crossing into it is real and both
    rules fire for that instance."""
    _, raw, sdc = _paths("ip_cdc_handshake")
    reg = "$flatten\\u_sync_ack.$procdff$49"
    data = json.loads(raw.read_text())
    assert data["modules"]["ip_cdc_handshake"]["cells"][reg]["type"] == "$dff"
    exposed = _expose(raw, tmp_path, reg, "Q")
    assert _fired(exposed, sdc) == {"CDC-004": 1, "CDC-011": 1}
