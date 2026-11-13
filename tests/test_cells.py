"""Gate-level cell-type decoding (rtl-buddy-cdc#336).

:func:`rtl_buddy_cdc.cells.parse_gate_storage_cell` decodes the Yosys
gate-level flop / latch names (``$_DFF_PN0_`` …), and
:mod:`rtl_buddy_cdc.flops` reads coarse and gate-level flops through
it. The hand-written cases below pin every family in both polarities;
:func:`test_parser_agrees_with_yosys_help` checks the parser against the
cell descriptions of the local Yosys for every storage cell it knows,
so a wrong letter position cannot hide behind a test written from the
same misreading.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from rtl_buddy_cdc.cells import (
    GATE_LOGIC_TYPES,
    MUX2_TYPES,
    MUX_PINS,
    GateStorageCell,
    parse_gate_storage_cell,
)
from rtl_buddy_cdc.flops import (
    flop_clk_polarity,
    flop_enable_pin,
    flop_reset,
    flop_reset_bits,
    is_ff_cell,
    is_latch_cell,
)
from rtl_buddy_cdc.netlist import Cell

G = GateStorageCell

# (type, expected decode). Each family appears with both letters at every
# position; the descriptions quoted are from `yosys -h <type>`.
_CASES: list[tuple[str, GateStorageCell]] = [
    # "A positive edge D-type flip-flop."
    ("$_DFF_P_", G("$_DFF_", clk_polarity=1)),
    ("$_DFF_N_", G("$_DFF_", clk_polarity=0)),
    # "A positive edge D-type flip-flop with negative polarity enable."
    ("$_DFFE_PN_", G("$_DFFE_", clk_polarity=1, enable_polarity=0)),
    ("$_DFFE_NP_", G("$_DFFE_", clk_polarity=0, enable_polarity=1)),
    # "A negative edge D-type flip-flop with positive polarity reset."
    (
        "$_DFF_NP0_",
        G("$_DFF_", 0, reset_kind="async", reset_polarity=1, reset_value=0),
    ),
    (
        "$_DFF_PN1_",
        G("$_DFF_", 1, reset_kind="async", reset_polarity=0, reset_value=1),
    ),
    (
        "$_DFF_NN0_",
        G("$_DFF_", 0, reset_kind="async", reset_polarity=0, reset_value=0),
    ),
    # "A negative edge D-type flip-flop with positive polarity set and
    # negative polarity clock enable."
    (
        "$_DFFE_NP1N_",
        G(
            "$_DFFE_",
            0,
            reset_kind="async",
            reset_polarity=1,
            reset_value=1,
            enable_polarity=0,
        ),
    ),
    (
        "$_DFFE_PN0P_",
        G(
            "$_DFFE_",
            1,
            reset_kind="async",
            reset_polarity=0,
            reset_value=0,
            enable_polarity=1,
        ),
    ),
    # "A positive edge D-type flip-flop with negative polarity synchronous set."
    (
        "$_SDFF_PN1_",
        G("$_SDFF_", 1, reset_kind="sync", reset_polarity=0, reset_value=1),
    ),
    (
        "$_SDFF_NP0_",
        G("$_SDFF_", 0, reset_kind="sync", reset_polarity=1, reset_value=0),
    ),
    # "A negative edge D-type flip-flop with positive polarity synchronous
    # reset and negative polarity clock enable (with reset having priority)."
    (
        "$_SDFFE_NP0N_",
        G(
            "$_SDFFE_",
            0,
            reset_kind="sync",
            reset_polarity=1,
            reset_value=0,
            enable_polarity=0,
        ),
    ),
    (
        "$_SDFFE_PN1P_",
        G(
            "$_SDFFE_",
            1,
            reset_kind="sync",
            reset_polarity=0,
            reset_value=1,
            enable_polarity=1,
        ),
    ),
    # "... with clock enable having priority".
    (
        "$_SDFFCE_PN1P_",
        G(
            "$_SDFFCE_",
            1,
            reset_kind="sync",
            reset_polarity=0,
            reset_value=1,
            enable_polarity=1,
            reset_gated_by_enable=True,
        ),
    ),
    (
        "$_SDFFCE_NP0N_",
        G(
            "$_SDFFCE_",
            0,
            reset_kind="sync",
            reset_polarity=1,
            reset_value=0,
            enable_polarity=0,
            reset_gated_by_enable=True,
        ),
    ),
    # "A positive edge D-type flip-flop with negative polarity set and
    # positive polarity reset."  Letters: C, S, R.
    (
        "$_DFFSR_PNP_",
        G(
            "$_DFFSR_",
            1,
            reset_kind="async",
            reset_polarity=1,
            reset_value=0,
            set_polarity=0,
        ),
    ),
    (
        "$_DFFSR_NPN_",
        G(
            "$_DFFSR_",
            0,
            reset_kind="async",
            reset_polarity=0,
            reset_value=0,
            set_polarity=1,
        ),
    ),
    # "A positive edge D-type flip-flop with negative polarity set, negative
    # polarity reset and positive polarity clock enable."  C, S, R, E.
    (
        "$_DFFSRE_PNNP_",
        G(
            "$_DFFSRE_",
            1,
            reset_kind="async",
            reset_polarity=0,
            reset_value=0,
            set_polarity=0,
            enable_polarity=1,
        ),
    ),
    (
        "$_DFFSRE_NPPN_",
        G(
            "$_DFFSRE_",
            0,
            reset_kind="async",
            reset_polarity=1,
            reset_value=0,
            set_polarity=1,
            enable_polarity=0,
        ),
    ),
    # "A positive edge D-type flip-flop with negative polarity async load."
    ("$_ALDFF_PN_", G("$_ALDFF_", 1, aload_polarity=0)),
    ("$_ALDFF_NP_", G("$_ALDFF_", 0, aload_polarity=1)),
    (
        "$_ALDFFE_PNP_",
        G("$_ALDFFE_", 1, aload_polarity=0, enable_polarity=1),
    ),
    (
        "$_ALDFFE_NPN_",
        G("$_ALDFFE_", 0, aload_polarity=1, enable_polarity=0),
    ),
    # "A negative enable D-type latch."
    ("$_DLATCH_N_", G("$_DLATCH_", enable_polarity=0)),
    ("$_DLATCH_P_", G("$_DLATCH_", enable_polarity=1)),
    # "A positive enable D-type latch with negative polarity reset."
    (
        "$_DLATCH_PN0_",
        G(
            "$_DLATCH_",
            reset_kind="async",
            reset_polarity=0,
            reset_value=0,
            enable_polarity=1,
        ),
    ),
    (
        "$_DLATCH_NP1_",
        G(
            "$_DLATCH_",
            reset_kind="async",
            reset_polarity=1,
            reset_value=1,
            enable_polarity=0,
        ),
    ),
    # "A positive enable D-type latch with negative polarity set and
    # negative polarity reset."  E, S, R.
    (
        "$_DLATCHSR_PNP_",
        G(
            "$_DLATCHSR_",
            reset_kind="async",
            reset_polarity=1,
            reset_value=0,
            set_polarity=0,
            enable_polarity=1,
        ),
    ),
    # "A set-reset latch with positive polarity SET and negative polarity
    # RESET."  S, R.
    (
        "$_SR_PN_",
        G(
            "$_SR_",
            reset_kind="async",
            reset_polarity=0,
            reset_value=0,
            set_polarity=1,
        ),
    ),
    (
        "$_SR_NP_",
        G(
            "$_SR_",
            reset_kind="async",
            reset_polarity=1,
            reset_value=0,
            set_polarity=0,
        ),
    ),
]


@pytest.mark.parametrize(("cell_type", "expected"), _CASES, ids=[c for c, _ in _CASES])
def test_parse_gate_storage_cell(cell_type: str, expected: GateStorageCell) -> None:
    assert parse_gate_storage_cell(cell_type) == expected


@pytest.mark.parametrize(
    "cell_type",
    [
        "$dff",
        "$adff",
        "$mux",
        "$_MUX_",
        "$_AND_",
        "$_FF_",
        "$_DFF_X_",  # not a polarity letter
        "$_DFF_PN2_",  # not a reset value
        "$_DFF_PP",  # no trailing underscore
        "$_DFF_PPPP_",  # no 4-letter $_DFF_ layout
        "$_SDFF_P_",  # $_SDFF_ always has a reset
        "$_DFFSR_PN_",
        "$_DLATCH_",
        "$_ICG_PP_",
    ],
)
def test_parse_rejects_non_storage_and_malformed(cell_type: str) -> None:
    assert parse_gate_storage_cell(cell_type) is None


def test_flop_and_latch_predicates() -> None:
    for t in ("$_DFF_N_", "$_DFFE_PN0P_", "$_SDFFCE_NP1N_", "$_DFFSRE_PPPP_"):
        assert is_ff_cell(t) and not is_latch_cell(t), t
    for t in ("$_DLATCH_P_", "$_DLATCH_NN1_", "$_DLATCHSR_PPP_", "$dlatch"):
        assert is_latch_cell(t) and not is_ff_cell(t), t
    # $_SR_ is a latch family but has no data pin: neither a flop nor a
    # transparent latch.
    assert not is_ff_cell("$_SR_PP_") and not is_latch_cell("$_SR_PP_")
    assert not is_ff_cell("$_DFF_Q_")
    assert parse_gate_storage_cell("$_SR_PP_").is_latch  # type: ignore[union-attr]


def _cell(t: str, params: dict[str, str] | None = None, **conns: int) -> Cell:
    return Cell(
        name="f",
        type=t,
        parameters=params or {},
        connections={k: (v,) for k, v in conns.items()},
    )


@pytest.mark.parametrize(
    ("cell", "role", "pin", "kind", "polarity", "value"),
    [
        # Coarse.
        (
            _cell("$adff", {"ARST_POLARITY": "0", "ARST_VALUE": "1"}, ARST=5),
            "arst",
            "ARST",
            "async",
            0,
            "1",
        ),
        (
            _cell("$sdffce", {"SRST_POLARITY": "1", "SRST_VALUE": "0"}, SRST=5),
            "srst",
            "SRST",
            "sync",
            1,
            "0",
        ),
        (_cell("$dffsr", {"CLR_POLARITY": "0"}, CLR=5), "clr", "CLR", "async", 0, "0"),
        (
            _cell("$aldff", {"ALOAD_POLARITY": "1"}, ALOAD=5),
            "aload",
            "ALOAD",
            "async",
            1,
            None,
        ),
        # Gate-level: the same roles on pins R / L.
        (_cell("$_DFF_PN0_", R=5), "arst", "R", "async", 0, "0"),
        (_cell("$_DFFE_NP1N_", R=5), "arst", "R", "async", 1, "1"),
        (_cell("$_SDFF_PP0_", R=5), "srst", "R", "sync", 1, "0"),
        (_cell("$_SDFFE_PN1P_", R=5), "srst", "R", "sync", 0, "1"),
        (_cell("$_SDFFCE_PN0N_", R=5), "srst", "R", "sync", 0, "0"),
        (_cell("$_DFFSR_PNP_", R=5, S=6), "clr", "R", "async", 1, "0"),
        (_cell("$_DFFSRE_PPNP_", R=5, S=6), "clr", "R", "async", 0, "0"),
        (_cell("$_ALDFF_PN_", L=5), "aload", "L", "async", 0, None),
        (_cell("$_ALDFFE_NPP_", L=5), "aload", "L", "async", 1, None),
    ],
)
def test_flop_reset(
    cell: Cell,
    role: str,
    pin: str,
    kind: str,
    polarity: int,
    value: str | None,
) -> None:
    rst = flop_reset(cell)
    assert rst is not None
    assert (rst.role, rst.pin, rst.kind, rst.polarity, rst.value) == (
        role,
        pin,
        kind,
        polarity,
        value,
    )
    assert rst.bits == (5,)
    assert flop_reset_bits(cell, role) == (5,)  # type: ignore[arg-type]
    other = "srst" if role != "srst" else "arst"
    assert flop_reset_bits(cell, other) == ()


@pytest.mark.parametrize(
    "cell_type", ["$dff", "$dffe", "$_DFF_P_", "$_DFFE_PN_", "$_DLATCH_PN0_", "$and"]
)
def test_flop_reset_none(cell_type: str) -> None:
    assert flop_reset(_cell(cell_type)) is None


def test_flop_reset_coarse_defaults() -> None:
    """Missing parameters: polarity defaults to active high and the
    reset value to 0 (a non-binary value bit also reads as 0). An empty
    polarity string reads as active low, and the trailing bit of a
    32-bit padded parameter is what counts — the decoding
    ``reset_domain._polarity_from_param`` used to do."""
    rst = flop_reset(_cell("$adff", ARST=5))
    assert rst is not None and (rst.polarity, rst.value) == (1, "0")
    for raw, pol in (("", 0), ("0", 0), ("1", 1), ("0" * 31 + "1", 1)):
        rst = flop_reset(_cell("$sdff", {"SRST_POLARITY": raw}, SRST=5))
        assert rst is not None and rst.polarity == pol, raw
    rst = flop_reset(_cell("$adff", {"ARST_VALUE": "x"}, ARST=5))
    assert rst is not None and rst.value == "0"


@pytest.mark.parametrize(
    ("cell", "pin"),
    [
        (_cell("$dffe", EN=1), "EN"),
        (_cell("$adffe", EN=1), "EN"),
        (_cell("$dff"), None),
        (_cell("$_DFFE_PP_"), "E"),
        (_cell("$_DFFE_PN0N_"), "E"),
        (_cell("$_SDFFE_PP0P_"), "E"),
        (_cell("$_SDFFCE_NN1P_"), "E"),
        (_cell("$_DFFSRE_PPPN_"), "E"),
        (_cell("$_ALDFFE_PPN_"), "E"),
        (_cell("$_DFF_PN0_"), None),
        (_cell("$_SDFF_PN0_"), None),
        # A latch's E is its gate, not a clock enable.
        (_cell("$_DLATCH_P_"), None),
    ],
)
def test_flop_enable_pin(cell: Cell, pin: str | None) -> None:
    assert flop_enable_pin(cell) == pin


@pytest.mark.parametrize(
    ("cell", "edge"),
    [
        (_cell("$dff", {"CLK_POLARITY": "1"}), 1),
        (_cell("$dff", {"CLK_POLARITY": "0"}), 0),
        (_cell("$adff", {"CLK_POLARITY": "00000000000000000000000000000000"}), 0),
        (_cell("$_DFF_P_"), 1),
        (_cell("$_DFF_N_"), 0),
        # The rtl-buddy-cdc#336 case: reset suffix after the clock letter.
        (_cell("$_DFF_NN0_"), 0),
        (_cell("$_DFF_NP1_"), 0),
        (_cell("$_DFF_PN0_"), 1),
        (_cell("$_DFFE_NP0P_"), 0),
        (_cell("$_SDFFE_NP0P_"), 0),
        (_cell("$_SDFFCE_PN0N_"), 1),
        (_cell("$_DFFSR_NPP_"), 0),
        (_cell("$_DFFSRE_PNNN_"), 1),
        (_cell("$_ALDFF_NP_"), 0),
        (_cell("$_ALDFFE_PNN_"), 1),
        (_cell("$weirdff"), 1),
    ],
)
def test_flop_clk_polarity(cell: Cell, edge: int) -> None:
    assert flop_clk_polarity(cell) == edge


def test_mux_and_gate_tables() -> None:
    assert MUX2_TYPES == {"$mux", "$_MUX_"}
    assert MUX2_TYPES <= set(MUX_PINS)
    assert MUX_PINS["$_MUX4_"] == (("A", "B", "C", "D"), ("S", "T"))
    assert len(MUX_PINS["$_MUX16_"][0]) == 16
    assert {"$_AND_", "$_NAND_", "$_OR_", "$_NOR_", "$_AOI3_"} <= GATE_LOGIC_TYPES
    assert not GATE_LOGIC_TYPES & set(MUX_PINS)


# --- Cross-check against the local Yosys cell library ----------------------

_STORAGE_FAMILY_RE = re.compile(
    r"^\$_(DFF|DFFE|SDFF|SDFFE|SDFFCE|DFFSR|DFFSRE|ALDFF|ALDFFE|DLATCH|DLATCHSR|SR)_"
)
_HEADER_RE = re.compile(r"^\s+(\$_[A-Z0-9_]+_) \(([^)]*)\)\s*$")


def _yosys_cell_help() -> dict[str, tuple[set[str], str]]:
    """``{type: (pins, description)}`` for every gate-level storage cell
    of the local Yosys, from one ``help`` run (a script file, not ``-p``:
    see ``tests/test_gate_level_corpus.py::_run_yosys``)."""
    listing = subprocess.run(
        ["yosys", "-p", "help -cells"], capture_output=True, text=True, check=True
    ).stdout
    types = sorted(
        {
            t
            for t in re.findall(r"\$_[A-Z0-9_]+_", listing)
            if _STORAGE_FAMILY_RE.match(t)
        }
    )
    with tempfile.TemporaryDirectory() as d:
        ys = Path(d) / "help.ys"
        ys.write_text("".join(f"help {t}\n" for t in types))
        out = subprocess.run(
            ["yosys", "-s", str(ys)], capture_output=True, text=True, check=True
        ).stdout
    cells: dict[str, tuple[set[str], str]] = {}
    current: str | None = None
    for line in out.splitlines():
        m = _HEADER_RE.match(line)
        if m:
            current = m.group(1)
            pins = {p.strip() for p in m.group(2).split(",")}
            cells[current] = (pins, "")
            continue
        if current is None:
            continue
        if "Truth table" in line or line.startswith("Run 'help"):
            current = None
            continue
        pins, desc = cells[current]
        cells[current] = (pins, f"{desc} {line.strip()}".strip())
    assert set(cells) == set(types), sorted(set(types) - set(cells))
    return cells


def _pol_word(word: str) -> int:
    return 1 if word == "positive" else 0


def _expected_from_description(cell_type: str, desc: str) -> dict[str, object]:
    """What the Yosys description says, in :class:`GateStorageCell` terms."""
    d = desc.lower()
    out: dict[str, object] = {}
    if m := re.search(r"(positive|negative) edge", d):
        out["clk_polarity"] = _pol_word(m.group(1))
    if m := re.search(r"(positive|negative) enable d-type latch", d):
        out["enable_polarity"] = _pol_word(m.group(1))
    if m := re.search(r"(positive|negative) polarity (?:clock )?enable", d):
        out["enable_polarity"] = _pol_word(m.group(1))
    if m := re.search(r"(positive|negative) polarity async load", d):
        out["aload_polarity"] = _pol_word(m.group(1))
    has_set_pin = cell_type.startswith(("$_DFFSR", "$_DLATCHSR_", "$_SR_"))
    for m in re.finditer(r"(positive|negative) polarity (synchronous )?(reset|set)", d):
        pol = _pol_word(m.group(1))
        sync = m.group(2) is not None
        what = m.group(3)
        if has_set_pin and what == "set":
            out["set_polarity"] = pol
            continue
        out["reset_kind"] = "sync" if sync else "async"
        out["reset_polarity"] = pol
        out["reset_value"] = 1 if what == "set" else 0
    if "clock enable having priority" in d:
        out["reset_gated_by_enable"] = True
    return out


@pytest.mark.gate_level
@pytest.mark.skipif(shutil.which("yosys") is None, reason="yosys not on PATH")
def test_parser_agrees_with_yosys_help() -> None:
    """Every gate-level storage cell the local Yosys documents decodes to
    exactly what its ``help`` description says, and every pin the decode
    implies is one of the cell's documented pins."""
    cells = _yosys_cell_help()
    assert len(cells) > 100
    for cell_type, (pins, desc) in cells.items():
        got = parse_gate_storage_cell(cell_type)
        assert got is not None, cell_type
        expected = _expected_from_description(cell_type, desc)
        assert expected, (cell_type, desc)
        for field in GateStorageCell.__dataclass_fields__:
            if field == "family":
                continue
            default = False if field == "reset_gated_by_enable" else None
            want = expected.get(field, default)
            assert getattr(got, field) == want, (cell_type, field, desc)
        for field, pin in (
            ("clk_polarity", "C"),
            ("reset_kind", "R"),
            ("set_polarity", "S"),
            ("aload_polarity", "L"),
            ("enable_polarity", "E"),
        ):
            if getattr(got, field) is not None:
                assert pin in pins, (cell_type, field, pins)
