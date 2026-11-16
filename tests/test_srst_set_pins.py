"""One reading of a flop's reset and set pins in every netlist form
(rtl-buddy-cdc#383, rtl-buddy-cdc#378).

The same RTL reaches the analyzer as a ``proc`` netlist (a synchronous
reset is a constant-leg ``$mux`` on ``D``; a set / clear flop's ``CLR``
and ``SET`` are built from muxes), an ``opt_dff`` netlist (the reset is
an ``$sdff`` ``SRST`` pin) and a gate-level one (``$_DFFSR_*`` with
``$_NOT_`` / ``$_AND_`` on ``R`` / ``S``). Each fixture here is committed
in two of those forms, and both must report exactly the findings its
leading comment reasons out:

- ``bad_sync_reset_foreign_select`` (``.json`` / ``.optdff.json``):
  RDC-003 reads the ``proc`` form's reset mux as it reads ``SRST``; the
  crossing it carries is CDC-001 in both forms.
- ``bad_bus_foreign_sync_reset`` (``.json`` / ``.optdff.json``): a reset
  from another domain is still the flop's own reset, so the gated bus and
  the gray synchroniser behind it are read the same in both forms.
- ``bad_set_clear_foreign_controls`` / ``good_set_clear_priority``
  (``.json`` / ``.synth.json``): every async pin counts (``SET`` too), a
  foreign ``CLR`` / ``SET`` is RDC-001, and the cells Yosys builds around
  the pins are not reset logic.
- ``bad_sync_chain_foreign_set`` (``.json`` / ``.synth.json``): CDC-015
  reads every async pin of a synchroniser stage, ``SET`` and ``ALOAD``
  too (rtl-buddy-cdc#390).

Hand-built cells pin the helpers: ``flop_async_controls``,
``reset_domain.effective_control_bit`` / ``control_root`` and
``rules._folded_srst_selects``.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rtl_buddy_cdc import netlist, sdc as sdc_mod
from rtl_buddy_cdc.cli import app
from rtl_buddy_cdc.flops import Flop, flop_async_controls
from rtl_buddy_cdc.netlist import Bit, Cell, Module, Port
from rtl_buddy_cdc.reset_domain import (
    assign_reset_domains,
    control_root,
    effective_control_bit,
)
from rtl_buddy_cdc.rules import (
    _bit_drivers,  # noqa: PLC2701
    _bit_reader_count,  # noqa: PLC2701
    _folded_srst_selects,  # noqa: PLC2701
)

FIX_ROOT = Path(__file__).parent / "fixtures"
runner = CliRunner()

Finding = tuple[str, str | None, str]

# fixture -> (forms, crossings as (src, dst) registers, findings)
EXPECTED: dict[str, tuple[tuple[str, ...], set[tuple[str, str]], list[Finding]]] = {
    "bad_sync_reset_foreign_select": (
        ("json", "optdff.json"),
        {("r", "q"), ("r", "s2"), ("s", "s1")},
        [
            ("CDC-001", "r", "q"),
            ("CDC-001", "r", "s2"),
            # One finding for the shared reset; anchored on a destination.
            ("RDC-003", None, "s2"),
        ],
    ),
    "bad_bus_foreign_sync_reset": (
        ("json", "optdff.json"),
        {
            ("gray", "meta"),
            ("pay", "cap"),
            ("r", "cap"),
            ("r", "gq"),
            ("req", "en_s1"),
        },
        [
            ("CDC-004", "r", "gq"),
            ("CDC-012", "pay", "cap"),
            ("CDC-012", "r", "cap"),
            ("RDC-003", None, "gq"),
        ],
    ),
    "bad_set_clear_foreign_controls": (
        ("json", "synth.json"),
        set(),
        [
            ("RDC-001", None, "qa"),
            ("RDC-001", None, "qc"),
            ("RDC-006", None, "qb"),
        ],
    ),
    "good_set_clear_priority": (("json", "synth.json"), set(), []),
    "bad_sync_chain_foreign_set": (
        ("json", "synth.json"),
        {("src_q", "l1"), ("src_q", "s1")},
        [
            ("CDC-015", "src_q", "l1"),
            ("CDC-015", "src_q", "s1"),
            # Anchored on the alphabetically first destination cell.
            ("RDC-001", None, "l2"),
            ("RDC-001", None, "s2"),
        ],
    ),
}

CASES = [(name, form) for name, (forms, _, _) in EXPECTED.items() for form in forms]


@pytest.fixture(autouse=True)
def _tokenizer_sdc_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    """Plain clock / group / input-delay SDCs; backend parity is
    tests/test_sdc_backends.py's job."""
    monkeypatch.setenv(sdc_mod.BACKEND_ENV_VAR, "tokenizer")


def _register_names(path: Path, top: str) -> dict[str, str]:
    """Flop cell -> the register its ``Q`` drives (first named net of its
    first ``Q`` bit), so the ``proc``, ``opt_dff`` and gate-level forms
    name the same flops the same way."""
    mod = json.loads(path.read_text())["modules"][top]
    by_bit: dict[object, str] = {}
    for net in sorted(mod["netnames"], key=lambda n: (n in mod["ports"], n)):
        if "$" in net:
            continue
        for b in mod["netnames"][net]["bits"]:
            by_bit.setdefault(b, net)
    out: dict[str, str] = {}
    for cell, info in mod["cells"].items():
        q = info["connections"].get("Q")
        if q and q[0] in by_bit:
            out[cell] = by_bit[q[0]]
    return out


def _analyze(name: str, form: str) -> tuple[set[tuple[str, str]], Counter[Finding]]:
    fix = FIX_ROOT / name
    path = fix / f"{name}.{form}"
    res = runner.invoke(
        app, ["analyze", "-n", str(path), "-s", str(fix / f"{name}.sdc"), "-f", "json"]
    )
    assert res.exit_code in (0, 1), res.output
    report = json.loads(res.stdout)
    names = _register_names(path, name)
    crossings = {
        (names[c["src_flop"]], names[c["dst_flop"]]) for c in report["crossings"]
    }
    findings: Counter[Finding] = Counter()
    for v in report["violations"]:
        c = v.get("crossing")
        if c is None:
            findings[(v["rule_id"], None, names[v["cell_name"]])] += 1
        else:
            findings[(v["rule_id"], names[c["src_flop"]], names[c["dst_flop"]])] += 1
    return crossings, findings


@pytest.mark.parametrize(("name", "form"), CASES, ids=[f"{n}-{f}" for n, f in CASES])
def test_fixture_verdict_in_every_form(name: str, form: str) -> None:
    _, crossings, findings = EXPECTED[name]
    got_crossings, got_findings = _analyze(name, form)
    assert got_crossings == crossings
    assert got_findings == Counter(findings)


def _types(name: str, form: str) -> Counter[str]:
    module = netlist.load(FIX_ROOT / name / f"{name}.{form}")
    return Counter(c.type for c in module.cells.values())


def test_fixture_forms_carry_the_shapes_under_test() -> None:
    for name in ("bad_sync_reset_foreign_select", "bad_bus_foreign_sync_reset"):
        proc, optdff = _types(name, "json"), _types(name, "optdff.json")
        assert proc["$mux"] and not any(t.startswith("$sdff") for t in proc)
        assert any(t.startswith("$sdff") for t in optdff)
    assert _types("bad_bus_foreign_sync_reset", "optdff.json")["$sdffe"] == 1
    for name in ("bad_set_clear_foreign_controls", "good_set_clear_priority"):
        assert _types(name, "json")["$dffsr"] == 3
        synth = _types(name, "synth.json")
        assert sum(n for t, n in synth.items() if t.startswith("$_DFFSR_")) == 3
        assert synth["$_NOT_"] and not synth["$mux"]


# --- flop_async_controls ----------------------------------------------------


def _cell(ctype: str, params: dict[str, str] | None = None, **conns: tuple) -> Cell:
    return Cell(name="f", type=ctype, connections=dict(conns), parameters=params or {})


@pytest.mark.parametrize(
    ("ctype", "conns", "expected"),
    [
        ("$dff", {"CLK": (1,)}, []),
        ("$sdff", {"CLK": (1,), "SRST": (2,)}, []),
        ("$adff", {"CLK": (1,), "ARST": (2,)}, [("arst", "ARST")]),
        ("$aldff", {"CLK": (1,), "ALOAD": (2,)}, [("aload", "ALOAD")]),
        (
            "$dffsr",
            {"CLK": (1,), "CLR": (2,), "SET": (3,)},
            [("clr", "CLR"), ("set", "SET")],
        ),
        (
            "$dffsre",
            {"CLK": (1,), "CLR": (2,), "SET": (3,), "EN": (4,)},
            [("clr", "CLR"), ("set", "SET")],
        ),
        (
            "$_DFFSR_PNP_",
            {"C": (1,), "R": (2,), "S": (3,)},
            [("clr", "R"), ("set", "S")],
        ),
        ("$_DFF_PN0_", {"C": (1,), "R": (2,)}, [("arst", "R")]),
        ("$_SDFF_PP0_", {"C": (1,), "R": (2,)}, []),
        ("$_ALDFF_PP_", {"C": (1,), "L": (2,)}, [("aload", "L")]),
    ],
)
def test_flop_async_controls(ctype: str, conns: dict, expected: list) -> None:
    assert [(p.role, p.pin) for p in flop_async_controls(_cell(ctype, **conns))] == (
        expected
    )


def test_set_pin_polarity_and_value() -> None:
    coarse = flop_async_controls(
        _cell("$dffsr", {"SET_POLARITY": "0"}, CLK=(1,), CLR=(2,), SET=(3,))
    )
    assert (coarse[1].polarity, coarse[1].value, coarse[1].bits) == (0, "1", (3,))
    # ``$_DFFSR_<C><S><R>_``: set active low, clear active high.
    gate = flop_async_controls(_cell("$_DFFSR_PNP_", C=(1,), R=(2,), S=(3,)))
    assert [(p.pin, p.polarity) for p in gate] == [("R", 1), ("S", 0)]


# --- effective_control_bit / control_root -----------------------------------

# Bits: 1 = clr root (port), 2 = set root (port), 3 = flop Q, 10.. = cell
# outputs.


def _comb_module(*cells: Cell) -> tuple[Module, dict[Bit, tuple[str, str]]]:
    module = Module(
        name="m",
        ports={
            "clr": Port(name="clr", direction="input", bits=(1,)),
            "set": Port(name="set", direction="input", bits=(2,)),
        },
        cells={c.name: c for c in cells},
        netnames={},
    )
    return module, {b: (c.name, "Y") for c in cells for b in c.connections["Y"]}


def _mux(name: str, a: Bit, b: Bit, s: Bit, y: Bit, ctype: str = "$mux") -> Cell:
    return Cell(
        name=name, type=ctype, connections={"A": (a,), "B": (b,), "S": (s,), "Y": (y,)}
    )


def _gate(name: str, ctype: str, y: Bit, *ins: Bit) -> Cell:
    conns: dict[str, tuple[Bit, ...]] = {"Y": (y,)}
    for pin, b in zip("AB", ins, strict=False):
        conns[pin] = (b,)
    return Cell(name=name, type=ctype, connections=conns)


def test_control_root_steps_through_buffers_and_inverters() -> None:
    module, drv = _comb_module(
        _mux("inv", "1", "0", 1, 10),
        _gate("n", "$_NOT_", 11, 10),
        _mux("buf", "0", "1", 11, 12, "$_MUX_"),
        _mux("live", 2, 1, 3, 13),
    )
    assert control_root(module, 12, drv) == 1
    assert control_root(module, 13, drv) == 13  # a live mux is a signal of its own
    assert control_root(module, "0", drv) == "0"


def test_buffer_mux_is_stepped_through_on_any_flop() -> None:
    module, drv = _comb_module(_mux("buf", "0", "1", 3, 10))
    assert effective_control_bit(module, 10, 1, frozenset(), drv) == 3


def test_inverter_is_stepped_through_only_on_a_set_clear_flop() -> None:
    module, drv = _comb_module(_mux("inv", "1", "0", 3, 10), _gate("n", "$not", 11, 3))
    for bit in (10, 11):
        assert effective_control_bit(module, bit, 1, frozenset(), drv) == bit
        assert (
            effective_control_bit(
                module, bit, 1, frozenset(), drv, strip_inverters=True
            )
            == 3
        )


def test_priority_mux_is_stepped_through() -> None:
    """``SET = clr ? 0 : set`` (active-high SET): the clear term can only
    deassert SET, so SET acts on ``set``."""
    module, drv = _comb_module(_mux("prio", 2, "0", 1, 10))
    assert effective_control_bit(module, 10, 1, frozenset({1}), drv) == 2
    # Not the flop's own other control: real logic, kept.
    assert effective_control_bit(module, 10, 1, frozenset({3}), drv) == 10
    # A term that would *assert* the pin is not the priority encoding.
    module, drv = _comb_module(_mux("prio", 2, "1", 1, 10))
    assert effective_control_bit(module, 10, 1, frozenset({1}), drv) == 10
    # ... but it is for an active-low pin.
    assert effective_control_bit(module, 10, 0, frozenset({1}), drv) == 2


def test_priority_mux_with_the_constant_on_a() -> None:
    module, drv = _comb_module(_mux("prio", "0", 2, 1, 10))
    assert effective_control_bit(module, 10, 1, frozenset({1}), drv) == 2


@pytest.mark.parametrize(
    ("ctype", "polarity", "ins", "expected"),
    [
        # ``abc``'s ``SET = ~clr & set``: either input may be the term.
        ("$_AND_", 1, (11, 2), 2),
        ("$_AND_", 1, (2, 11), 2),
        ("$and", 1, (1, 2), 2),
        # An OR can only assert an active-high pin: not the encoding.
        ("$_OR_", 1, (1, 2), 10),
        ("$_OR_", 0, (1, 2), 2),
        # ``A & ~B``: only the inverted input may be the term.
        ("$_ANDNOT_", 1, (2, 1), 2),
        ("$_ANDNOT_", 1, (1, 2), 10),
        ("$_ORNOT_", 0, (2, 1), 2),
        # Neither input is the other control.
        ("$_AND_", 1, (2, 3), 10),
    ],
)
def test_priority_gate(ctype: str, polarity: int, ins: tuple, expected: Bit) -> None:
    module, drv = _comb_module(_gate("g", ctype, 10, *ins), _gate("n", "$_NOT_", 11, 1))
    got = effective_control_bit(module, 10, polarity, frozenset({1}), drv)
    assert got == expected


def test_inverter_flips_the_tracked_polarity() -> None:
    """``SET_N = ~(clr | set)`` on an active-low pin: through the inverter
    the OR is on an active-high signal and may only be stripped as an
    AND would be — it is not, so the OR stays."""
    module, drv = _comb_module(
        _gate("o", "$_OR_", 11, 1, 2), _gate("n", "$_NOT_", 10, 11)
    )
    got = effective_control_bit(
        module, 10, 0, frozenset({1}), drv, strip_inverters=True
    )
    assert got == 11


def test_assign_reset_domains_lists_every_async_control() -> None:
    """A ``$dffsr`` gets both pins in ``controls``; its ``reset`` is the
    CLR classified on the signal behind Yosys's buffer mux."""
    clr_flop = Cell(
        name="clr_q",
        type="$dff",
        connections={"CLK": (20,), "D": (21,), "Q": (3,)},
    )
    q = Cell(
        name="q",
        type="$dffsr",
        connections={"CLK": (22,), "D": (23,), "Q": (24,), "CLR": (10,), "SET": (11,)},
        parameters={"CLR_POLARITY": "1", "SET_POLARITY": "1"},
    )
    module = Module(
        name="m",
        ports={"set": Port(name="set", direction="input", bits=(2,))},
        cells={
            c.name: c
            for c in (
                clr_flop,
                q,
                _mux("buf", "0", "1", 3, 10),
                _mux("prio", 12, "0", 3, 11),
                _mux("inv", "1", "0", 2, 12),
            )
        },
        netnames={},
    )
    rd = assign_reset_domains(module)["q"]
    assert [(c.pin.pin, c.bits, c.source.source) for c in rd.controls] == [
        ("CLR", (3,), "inferred"),
        ("SET", (2,), "port"),
    ]
    assert rd.reset == rd.controls[0].source
    assert assign_reset_domains(module)["clr_q"].controls == ()


# --- _folded_srst_selects ---------------------------------------------------


def _flop(ctype: str = "$dff", **extra: tuple) -> Flop:
    cell = Cell(
        name="q",
        type=ctype,
        connections={"CLK": (90,), "D": (40,), "Q": (50,), **extra},
    )
    return Flop(cell=cell, clk=90, d=(40,), q=(50,))


def _selects(flop: Flop, *muxes: Cell) -> tuple[Bit, ...]:
    module = Module(
        name="m",
        ports={"o": Port(name="o", direction="output", bits=(50,))},
        cells={flop.cell.name: flop.cell, **{c.name: c for c in muxes}},
        netnames={},
    )
    return _folded_srst_selects(
        flop, module, _bit_drivers(module), _bit_reader_count(module)
    )


def test_folded_srst_selects_reset_then_enable() -> None:
    """``if (r) q <= 0; else if (en) q <= d;`` — ``$sdffe``."""
    flop = _flop()
    assert _selects(flop, _mux("rst", 41, "0", 7, 40), _mux("en", 50, 1, 8, 41)) == (7,)


def test_folded_srst_selects_enable_then_reset() -> None:
    """``if (en) begin if (r) q <= 0; else q <= d; end`` — ``$sdffce``,
    the gate-level mux this time."""
    flop = _flop()
    got = _selects(
        flop,
        _mux("en", 50, 41, 8, 40, "$_MUX_"),
        _mux("rst", 1, "1", 7, 41, "$_MUX_"),
    )
    assert got == (7,)


def test_folded_srst_selects_stops_at_a_reset_under_the_enable() -> None:
    """A reset over an enable and a second one under it cannot share one
    ``SRST``; ``opt_dff`` leaves the inner one on ``D``."""
    flop = _flop()
    got = _selects(
        flop,
        _mux("r1", 41, "0", 7, 40),
        _mux("en", 50, 42, 8, 41),
        _mux("r2", 1, "0", 9, 42),
    )
    assert got == (7,)


def test_folded_srst_selects_chained_resets() -> None:
    flop = _flop()
    assert _selects(flop, _mux("r1", 41, "0", 7, 40), _mux("r2", 1, "0", 9, 41)) == (
        7,
        9,
    )


@pytest.mark.parametrize(
    "mux",
    [
        _mux("both", "0", "1", 7, 40),  # both legs constant
        _mux("live", 1, 2, 7, 40),  # a data mux
        _mux("xleg", 1, "x", 7, 40),  # an x leg is not a reset value
    ],
)
def test_folded_srst_selects_rejects(mux: Cell) -> None:
    assert _selects(_flop(), mux) == ()


def test_folded_srst_selects_not_on_an_async_flop() -> None:
    flop = _flop("$adff", ARST=(6,))
    assert _selects(flop, _mux("rst", 1, "0", 7, 40)) == ()


def test_folded_srst_selects_mux_output_read_twice() -> None:
    flop = _flop()
    reader = Cell(name="tap", type="$not", connections={"A": (40,), "Y": (60,)})
    assert _selects(flop, _mux("rst", 1, "0", 7, 40), reader) == ()
