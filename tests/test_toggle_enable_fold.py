"""CDC-013 on a toggle folded into an enable flop (rtl-buddy-cdc#338).

``if (en) q <= ~q;`` leaves ``proc`` as a flop fed by the hold mux
``D = en ? ~Q : Q``. ``opt_dff`` (and so every synthesis-shaped flow)
folds the mux into the flop: an ``$adffe`` / ``$dffe`` / gate-level
``$_DFFE_*`` with ``D = ~Q`` and ``en`` on the enable pin. The toggle
classifier used to recognise only the mux, so CDC-013 vanished from any
optimised netlist. These tests pin both shapes, both enable polarities,
both cell libraries, and the near-misses that must stay ``"other"``.

The slang frontend never emits an enable flop (its procedural lowering
keeps the hold ``$mux``, like ``proc``), so it has no folded shape to
test; ``bad_toggle_no_xor_tail`` under ``lint --frontend slang`` is
covered by the slang parity tests.

The gate-level netlist is the committed
``bad_toggle_no_xor_tail.synth.json`` (``opt_dff; techmap; abc``), also
held to the RTL verdict by ``tests/test_gate_level_corpus.py``. The
coarse folded netlist is derived from the committed RTL netlist here, so
the enable polarity can be varied; it is the cell shape
``proc; flatten; opt_dff; opt_clean`` writes for this fixture.
"""

from __future__ import annotations

import copy
import json
from collections import Counter
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rtl_buddy_cdc.cli import app
from rtl_buddy_cdc.netlist import Bit, Cell, Module
from rtl_buddy_cdc.pulse import classify_toggle_d_pin
from rtl_buddy_cdc.rules import _bit_drivers

FIX = Path(__file__).parent / "fixtures"
NAME = "bad_toggle_no_xor_tail"
RTL = FIX / NAME / f"{NAME}.json"
SDC = FIX / NAME / f"{NAME}.sdc"
runner = CliRunner()


# --- classifier -------------------------------------------------------------

Q, NQ, EN, OTHER = 2, 3, 9, 5


def _folded(
    flop_type: str,
    *,
    not_type: str = "$not",
    not_input: int = Q,
    enable: Bit = EN,
    enable_pin: str | None = None,
    extra: dict[str, tuple[Bit, ...]] | None = None,
) -> tuple[Module, Cell]:
    """A flop ``flop_type`` with ``D = ~not_input`` and ``enable`` on its
    enable pin (``EN`` coarse, ``E`` gate-level)."""
    gate = flop_type.startswith("$_")
    pin = enable_pin or ("E" if gate else "EN")
    conns: dict[str, tuple[Bit, ...]] = {
        ("C" if gate else "CLK"): (1,),
        "D": (NQ,),
        "Q": (Q,),
        pin: (enable,),
        **(extra or {}),
    }
    flop = Cell("t", flop_type, conns)
    inv = Cell("inv", not_type, {"A": (not_input,), "Y": (NQ,)})
    return Module("m", {}, {"t": flop, "inv": inv}, {}), flop


def _classify(module: Module, flop: Cell | None) -> str:
    return classify_toggle_d_pin(NQ, Q, module, _bit_drivers(module), flop=flop)


@pytest.mark.parametrize(
    ("flop_type", "not_type", "extra"),
    [
        ("$dffe", "$not", {}),
        ("$adffe", "$not", {"ARST": (4,)}),
        ("$sdffe", "$not", {"SRST": (4,)}),
        ("$sdffce", "$logic_not", {"SRST": (4,)}),
        ("$_DFFE_PP_", "$_NOT_", {}),
        ("$_DFFE_PN0P_", "$_NOT_", {"R": (4,)}),
        # Active-low enable: still a toggle, on the other level.
        ("$_DFFE_PN0N_", "$_NOT_", {"R": (4,)}),
        ("$_SDFFCE_PP0N_", "$_NOT_", {"R": (4,)}),
    ],
)
def test_folded_toggle_is_a_toggle(
    flop_type: str, not_type: str, extra: dict[str, tuple[Bit, ...]]
) -> None:
    module, flop = _folded(flop_type, not_type=not_type, extra=extra)
    assert _classify(module, flop) == "toggle"


def test_folded_toggle_needs_the_flop() -> None:
    """Without the flop the classifier cannot see the enable pin."""
    module, _ = _folded("$adffe")
    assert _classify(module, None) == "other"


def test_enable_flop_inverting_another_signal_is_not_a_toggle() -> None:
    """``if (en) q <= ~d;`` — an ordinary enable flop, not a toggle."""
    module, flop = _folded("$adffe", not_input=OTHER)
    assert _classify(module, flop) == "other"
    module, flop = _folded("$_DFFE_PP_", not_type="$_NOT_", not_input=OTHER)
    assert _classify(module, flop) == "other"


def test_free_running_inverter_is_not_a_toggle() -> None:
    """``D = ~Q`` on a flop without an enable (or with a constant one)
    is a divide-by-two, not an event toggle."""
    module, flop = _folded("$adff", enable_pin="UNUSED")
    assert _classify(module, flop) == "other"
    module, flop = _folded("$_DFF_P_", not_type="$_NOT_", enable_pin="UNUSED")
    assert _classify(module, flop) == "other"
    module, flop = _folded("$dffe", enable="1")
    assert _classify(module, flop) == "other"


def test_folded_d_from_a_non_inverter_is_not_a_toggle() -> None:
    module, flop = _folded("$adffe", not_type="$reduce_or")
    assert _classify(module, flop) == "other"


# --- end to end -------------------------------------------------------------


def _fired(netlist: Path) -> dict[str, int]:
    r = runner.invoke(
        app, ["analyze", "-n", str(netlist), "-s", str(SDC), "-f", "json"]
    )
    assert r.exit_code in (0, 1), r.output
    return dict(Counter(v["rule_id"] for v in json.loads(r.stdout)["violations"]))


def _fold_rtl_toggle(tmp_path: Path, *, en_polarity: str, invert_other: bool) -> Path:
    """The committed RTL netlist with the toggle's hold mux folded into
    the flop: ``$adff`` + ``$mux(Q, ~Q, en)`` -> ``$adffe(D=~Q, EN=en)``."""
    data = json.loads(RTL.read_text())
    m = data["modules"][NAME]
    cells = m["cells"]
    (mux_name,) = [n for n, c in cells.items() if c["type"] == "$mux"]
    mux = cells.pop(mux_name)
    q, nq, en = (
        mux["connections"]["A"],
        mux["connections"]["B"],
        mux["connections"]["S"],
    )
    (flop,) = [c for c in cells.values() if c["connections"].get("Q") == q]
    flop["type"] = "$adffe"
    flop["parameters"] = {**flop["parameters"], "EN_POLARITY": en_polarity}
    flop["port_directions"] = {**flop.get("port_directions", {}), "EN": "input"}
    flop["connections"] = {**flop["connections"], "D": nq, "EN": en}
    if invert_other:
        (inv,) = [c for c in cells.values() if c["connections"].get("Y") == nq]
        inv["connections"] = {**inv["connections"], "A": copy.copy(en)}
    out = tmp_path / f"{NAME}.folded.json"
    out.write_text(json.dumps(data))
    return out


def test_rtl_mux_toggle_fires() -> None:
    assert _fired(RTL) == {"CDC-013": 1}


@pytest.mark.parametrize("en_polarity", ["1", "0"])
def test_coarse_folded_toggle_fires(tmp_path: Path, en_polarity: str) -> None:
    folded = _fold_rtl_toggle(tmp_path, en_polarity=en_polarity, invert_other=False)
    assert _fired(folded) == {"CDC-013": 1}


def test_coarse_enable_flop_without_toggle_is_silent(tmp_path: Path) -> None:
    """Same netlist, but ``D = ~en`` instead of ``~Q``: no toggle, and
    the 2FF chain leaves nothing else to report."""
    folded = _fold_rtl_toggle(tmp_path, en_polarity="1", invert_other=True)
    assert _fired(folded) == {}


def test_gate_level_folded_toggle_fires() -> None:
    synth = FIX / NAME / f"{NAME}.synth.json"
    types = {
        c["type"]
        for c in json.loads(synth.read_text())["modules"][NAME]["cells"].values()
    }
    assert any(t.startswith("$_DFFE_") for t in types), types
    assert "$_MUX_" not in types  # the toggle is folded, not a hold mux
    assert _fired(synth) == {"CDC-013": 1}
