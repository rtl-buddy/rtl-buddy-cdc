"""Cell-type variety fixtures (rtl-buddy-cdc#331, last item).

Before these fixtures no committed netlist held an ``$aldffe``,
``$dffsre``, ``$shiftx`` or ``$mul``, and ``$aldff`` / ``$dffsr`` /
``$sdffce`` / ``$reduce_*`` appeared in one fixture each. Four fixtures
put those cells on real crossings:

- ``good_flop_variety_handshake`` / ``bad_flop_variety_handshake`` — a
  req/ack handshake whose synchronisers and captures are ``$aldff``,
  ``$dffsr``, ``$dffsre``, ``$aldffe`` and ``$sdffce`` (built with
  ``opt_dff`` after ``proc``: ``proc`` alone leaves the enable and
  sync-reset muxes beside the flops). The bad one breaks one flop family
  per bug.
- ``bad_comb_ops_before_sync`` — ``$mul``, ``$lt``, ``$reduce_*`` and
  ``$shiftx`` in front of 2FF synchronisers.
- ``good_comb_ops_handshake`` — ``$mul``, ``$le``, ``$reduce_*`` and
  ``$shiftx`` between handshake-held operands and a gated capture.

Each fixture's verdict is reasoned from the rule definitions and stated in
its leading comment; :data:`EXPECTED` pins it through ``analyze -f json``.
Where the analyzer disagrees the test is written against the reasoned
verdict and marked ``xfail(strict=True)`` (:data:`_XFAIL`), and
:data:`_CURRENT` pins what it reports today, so a partial fix or a new
regression still shows. The gate-level legs are held by
``tests/test_gate_level_corpus.py`` (and its allowlist); the slang
frontend's verdict is compared with the committed netlist's here.
"""

from __future__ import annotations

import functools
import importlib.util
import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rtl_buddy_cdc import sdc as sdc_mod
from rtl_buddy_cdc.cli import app

FIX_ROOT = Path(__file__).parent / "fixtures"

runner = CliRunner()


@pytest.fixture(autouse=True)
def _tokenizer_sdc_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    """Plain clock / group / input-delay SDCs: both backends read them the
    same (backend parity is tests/test_sdc_backends.py's job), and the
    tokenizer avoids a Tcl worker process per analysis."""
    monkeypatch.setenv(sdc_mod.BACKEND_ENV_VAR, "tokenizer")


# --- helpers ----------------------------------------------------------------


@functools.cache
def _q_names(name: str) -> dict[str, str]:
    """Flop cell name -> the register's own net name (its ``Q`` net), the
    name a reader of the ``.sv`` checks against."""
    mod = json.loads((FIX_ROOT / name / f"{name}.json").read_text())["modules"][name]
    ports = set(mod["ports"])
    by_bits: dict[tuple, list[str]] = {}
    for net, info in mod["netnames"].items():
        if "$" in net:
            continue
        by_bits.setdefault(tuple(info["bits"]), []).append(net)
    out: dict[str, str] = {}
    for cell, info in mod["cells"].items():
        q = info["connections"].get("Q")
        if q is None:
            continue
        nets = sorted(by_bits.get(tuple(q), []), key=lambda n: (n in ports, n))
        if nets:
            out[cell] = nets[0]
    return out


Finding = tuple[str, str | None, str | None]


def _finding(name: str, v: dict) -> Finding:
    """``(rule, source register, destination register)``; a finding with
    no crossing (the RDC rules) keeps only the register it names."""
    names = _q_names(name)
    c = v.get("crossing")
    if c is None:
        return (v["rule_id"], None, names.get(v["cell_name"], v["cell_name"]))
    src = names.get(c["src_flop"]) if c.get("src_flop") else f"port {c['src_port']}"
    return (v["rule_id"], src, names.get(c["dst_flop"]))


def _analyze(name: str) -> tuple[int, dict]:
    fix = FIX_ROOT / name
    res = runner.invoke(
        app,
        [
            "analyze",
            "--netlist",
            str(fix / f"{name}.json"),
            "--sdc",
            str(fix / f"{name}.sdc"),
            "--format",
            "json",
        ],
    )
    assert res.exit_code in (0, 1), res.output
    return res.exit_code, json.loads(res.stdout)


@dataclass(frozen=True)
class Verdict:
    crossings: frozenset[tuple[str, str]]
    findings: tuple[Finding, ...] = ()


def _x(*pairs: str) -> frozenset[tuple[str, str]]:
    """Crossings as ``"src>dst"`` register pairs."""
    return frozenset((src, dst) for src, dst in (p.split(">") for p in pairs))


_HANDSHAKE = ("pay_a>cap_a", "pay_b>cap_b", "pay_c>cap_c", "src_load_q>req_s1")

# Expected verdicts, reasoned from the rule definitions (each fixture's
# leading comment says why). Order inside a tuple does not matter
# (compared as multisets: None and register names do not sort together).
EXPECTED: dict[str, Verdict] = {
    "good_flop_variety_handshake": Verdict(_x(*_HANDSHAKE, "dst_ack>ack_meta")),
    "bad_flop_variety_handshake": Verdict(
        _x(
            *_HANDSHAKE,
            "dst_ack>ack_sync",
            "src_load_q>cap_b",  # through cap_b's enable
            "srst_q>cap_c",  # through cap_c's sync reset
        ),
        (
            ("CDC-001", "dst_ack", "ack_sync"),
            ("CDC-004", "pay_b", "cap_b"),
            ("CDC-004", "src_load_q", "cap_b"),
            ("RDC-003", None, "cap_c"),
            ("CDC-012", "srst_q", "cap_c"),
            ("RDC-001", None, "cap_a"),
        ),
    ),
    "bad_comb_ops_before_sync": Verdict(
        _x(
            "a_q>prod_s1",
            "b_q>prod_s1",
            "a_q>lt_s1",
            "b_q>lt_s1",
            "a_q>par_s1",
            "b_q>all_s1",
            "a_q>sel_s1",
            "idx_q>sel_s1",
        ),
        (
            ("CDC-003", "a_q", "prod_s1"),
            ("CDC-003", "b_q", "prod_s1"),
            ("CDC-003", "a_q", "lt_s1"),
            ("CDC-003", "b_q", "lt_s1"),
            ("CDC-003", "a_q", "par_s1"),
            ("CDC-003", "b_q", "all_s1"),
            ("CDC-003", "a_q", "sel_s1"),
            ("CDC-003", "idx_q", "sel_s1"),
            ("CDC-020", None, "a_q"),
            ("CDC-020", None, "b_q"),
        ),
    ),
    "good_comb_ops_handshake": Verdict(
        _x(
            "a_q>dst_result",
            "b_q>dst_result",
            "idx_q>dst_result",
            "src_load_q>load_s1",
            "dst_ack>ack_meta",
        )
    ),
}

# Fixtures whose analyzer verdict disagrees with the reasoned one. Each is
# a finding for an issue; the test flips to XPASS (a failure, strict) the
# day the analyzer is fixed, and the entry must then be removed together
# with its :data:`_CURRENT` row.
# (bad_flop_variety_handshake's RDC-001 on cap_a's foreign CLR was the
# last row, fixed by rtl-buddy-cdc#383.)
_XFAIL: dict[str, str] = {}

# What the analyzer reports today on each :data:`_XFAIL` fixture.
_CURRENT: dict[str, tuple[Finding, ...]] = {}


def _params() -> list:
    out = []
    for name in EXPECTED:
        marks = []
        if name in _XFAIL:
            marks.append(pytest.mark.xfail(strict=True, reason=_XFAIL[name]))
        out.append(pytest.param(name, marks=marks, id=name))
    return out


def _crossings(name: str, report: dict) -> frozenset[tuple[str, str]]:
    names = _q_names(name)
    return frozenset(
        (names[c["src_flop"]], names[c["dst_flop"]]) for c in report["crossings"]
    )


# --- the verdict table ------------------------------------------------------


@pytest.mark.parametrize("name", _params())
def test_fixture_verdict(name: str) -> None:
    expected = EXPECTED[name]
    exit_code, report = _analyze(name)
    assert _crossings(name, report) == expected.crossings
    assert report["summary"]["async_crossings"] == len(expected.crossings)
    got = Counter(_finding(name, v) for v in report["violations"])
    assert got == Counter(expected.findings)
    assert report["suppressed"] == []
    assert exit_code == (1 if expected.findings else 0)


@pytest.mark.parametrize("name", sorted(_CURRENT))
def test_current_verdict_of_xfail_fixtures(name: str) -> None:
    _, report = _analyze(name)
    assert _crossings(name, report) == EXPECTED[name].crossings
    got = Counter(_finding(name, v) for v in report["violations"])
    assert got == Counter(_CURRENT[name])


def test_tables_are_consistent() -> None:
    for name in EXPECTED:
        assert (FIX_ROOT / name / f"{name}.json").exists(), name
    assert set(_XFAIL) == set(_CURRENT)
    assert set(_XFAIL) <= set(EXPECTED)


# --- the cells are really there ---------------------------------------------

# Exact count of each cell type the fixture exists to carry, in its
# committed netlist. Yosys can fold or rename cells between versions; this
# fails if a rebuild loses one.
CELLS: dict[str, dict[str, int]] = {
    "good_flop_variety_handshake": {
        "$aldff": 2,
        "$aldffe": 1,
        "$dffsr": 2,
        "$dffsre": 1,
        "$sdffce": 1,
    },
    "bad_flop_variety_handshake": {
        "$aldff": 1,
        "$aldffe": 1,
        "$dffsr": 2,
        "$dffsre": 1,
        "$sdffce": 1,
    },
    "bad_comb_ops_before_sync": {
        "$mul": 1,
        "$lt": 1,
        "$reduce_or": 1,
        "$reduce_xor": 1,
        "$reduce_and": 1,
        "$shiftx": 1,
    },
    "good_comb_ops_handshake": {
        "$mul": 1,
        "$le": 1,
        "$reduce_xor": 1,
        "$reduce_or": 1,
        "$shiftx": 1,
    },
}

# Register (by Q net) -> flop type, for the flops the flop fixtures put on
# a crossing or its synchroniser.
FLOP_ROLES: dict[str, dict[str, str]] = {
    "good_flop_variety_handshake": {
        "ack_meta": "$aldff",
        "ack_sync": "$aldff",
        "req_s1": "$dffsr",
        "req_s2": "$dffsr",
        "cap_a": "$dffsre",
        "cap_b": "$aldffe",
        "cap_c": "$sdffce",
    },
    "bad_flop_variety_handshake": {
        "ack_sync": "$aldff",
        "req_s1": "$dffsr",
        "req_s2": "$dffsr",
        "cap_a": "$dffsre",
        "cap_b": "$aldffe",
        "cap_c": "$sdffce",
    },
}


_SECOND_STAGES: dict[str, set[str]] = {
    "good_flop_variety_handshake": {"req_s2", "ack_sync"},
    "bad_flop_variety_handshake": {"req_s2"},
}


def _cells(name: str) -> dict[str, dict]:
    mod = json.loads((FIX_ROOT / name / f"{name}.json").read_text())["modules"][name]
    return mod["cells"]


@pytest.mark.parametrize("name", sorted(CELLS))
def test_netlist_holds_the_cell_types(name: str) -> None:
    types = Counter(c["type"] for c in _cells(name).values())
    assert {t: types[t] for t in CELLS[name]} == CELLS[name]


@pytest.mark.parametrize("name", sorted(FLOP_ROLES))
def test_flop_types_sit_on_the_crossings(name: str) -> None:
    names = _q_names(name)
    got = {
        names[cell]: info["type"]
        for cell, info in _cells(name).items()
        if names.get(cell) in FLOP_ROLES[name]
    }
    assert got == FLOP_ROLES[name]
    # Every listed register is a crossing endpoint or the second stage of
    # a synchroniser whose head is one.
    on_crossing = {r for pair in EXPECTED[name].crossings for r in pair}
    assert set(FLOP_ROLES[name]) - on_crossing == _SECOND_STAGES[name]


# --- the slang frontend -----------------------------------------------------

_HAVE_PYSLANG = importlib.util.find_spec("pyslang") is not None

# Fixtures whose slang verdict diverges from the committed netlist's; each
# row is a slang lowering gap. Strict: a row whose fixture starts agreeing
# fails until the row is removed. Empty since rtl-buddy-cdc#384 closed the
# four gaps these fixtures exposed (async load, second async control, sync
# reset under enable, run-time index read).
_SLANG_GAPS: dict[str, str] = {}

# Same, for the structural comparison
# (:func:`test_slang_frontend_structure_matches_committed_netlist`).
_SLANG_STRUCTURE_GAPS: dict[str, str] = {}


def _slang_params(gaps: dict[str, str] = _SLANG_GAPS) -> list:
    return [
        pytest.param(
            name,
            marks=[pytest.mark.xfail(strict=True, reason=gaps[name])]
            if name in gaps
            else [],
            id=name,
        )
        for name in EXPECTED
    ]


def _per_rule(report: dict) -> tuple[int, dict[str, int]]:
    counts = Counter(v["rule_id"] for v in report["violations"])
    return report["summary"]["async_crossings"], dict(sorted(counts.items()))


@pytest.mark.skipif(not _HAVE_PYSLANG, reason="pyslang not installed")
@pytest.mark.parametrize("name", _slang_params())
def test_slang_frontend_agrees_with_committed_netlist(
    name: str, tmp_path: Path
) -> None:
    fix = FIX_ROOT / name
    out = tmp_path / "slang.json"
    res = runner.invoke(
        app,
        [
            "lint",
            "--top",
            name,
            "--sdc",
            str(fix / f"{name}.sdc"),
            "--frontend",
            "slang",
            "--format",
            "json",
            "--output",
            str(out),
            str(fix / f"{name}.sv"),
        ],  # fmt: skip
    )
    assert res.exit_code in (0, 1), res.output
    assert _per_rule(json.loads(out.read_text())) == _per_rule(_analyze(name)[1])


def test_slang_gap_rows_name_real_fixtures() -> None:
    assert set(_SLANG_GAPS) <= set(EXPECTED)
    assert set(_SLANG_STRUCTURE_GAPS) <= set(EXPECTED)


# The slang frontend folds no enable into a flop (an enable stays a hold
# mux on D, which the rules read the same way as an EN pin), so a family
# opt_dff gave an EN pin compares as its enable-less form. A sync reset is
# different: under an enable it has no hold-mux equivalent, and slang
# builds the $sdffce opt_dff does (rtl-buddy-cdc#384).
_ENABLE_FOLDED = {
    "$dffe": "$dff",
    "$adffe": "$adff",
    "$aldffe": "$aldff",
    "$dffsre": "$dffsr",
    "$sdffe": "$sdff",
}


def _module_q_names(module) -> dict[str, str]:
    """:func:`_q_names` over a loaded :class:`netlist.Module`."""
    by_bits: dict[tuple, list[str]] = {}
    for net, nn in module.netnames.items():
        if "$" not in net:
            by_bits.setdefault(tuple(nn.bits), []).append(net)
    out: dict[str, str] = {}
    for cell in module.cells.values():
        q = cell.connections.get("Q")
        if q is None:
            continue
        nets = sorted(by_bits.get(tuple(q), []), key=lambda n: (n in module.ports, n))
        if nets:
            out[cell.name] = nets[0]
    return out


def _flop_shapes(module, registers: set[str]) -> dict[str, tuple[str, frozenset]]:
    """Register -> (flop type with an EN pin folded back, its connected
    pins other than EN) for ``registers``."""
    names = _module_q_names(module)
    out: dict[str, tuple[str, frozenset]] = {}
    for cell in module.cells.values():
        reg = names.get(cell.name)
        if reg in registers:
            pins = frozenset(p for p in cell.connections if p != "EN")
            out[reg] = (_ENABLE_FOLDED.get(cell.type, cell.type), pins)
    return out


@pytest.mark.skipif(not _HAVE_PYSLANG, reason="pyslang not installed")
@pytest.mark.parametrize("name", _slang_params(_SLANG_STRUCTURE_GAPS))
def test_slang_frontend_structure_matches_committed_netlist(
    name: str, tmp_path: Path
) -> None:
    """Structural parity with the committed netlist, not only the finding
    counts: the same register pairs cross, each flop the fixture exists
    for is the same family with the same pins driven, and each comb cell
    type it carries is there as often."""
    from rtl_buddy_cdc import netlist
    from rtl_buddy_cdc.frontends import slang as slang_fe

    fix = FIX_ROOT / name
    committed = netlist.load(fix / f"{name}.json")
    module, _ = slang_fe.elaborate_with_blackboxes([fix / f"{name}.sv"], name)
    out = tmp_path / "slang.json"
    res = runner.invoke(
        app,
        [
            "lint",
            "--top",
            name,
            "--sdc",
            str(fix / f"{name}.sdc"),
            "--frontend",
            "slang",
            "--format",
            "json",
            "--output",
            str(out),
            str(fix / f"{name}.sv"),
        ],  # fmt: skip
    )
    assert res.exit_code in (0, 1), res.output
    names = _module_q_names(module)
    slang_report = json.loads(out.read_text())
    committed_report = _analyze(name)[1]
    slang_crossings = frozenset(
        (names[c["src_flop"]], names[c["dst_flop"]]) for c in slang_report["crossings"]
    )
    assert slang_crossings == _crossings(name, committed_report)

    def slang_finding(v: dict) -> Finding:
        c = v.get("crossing")
        if c is None:
            return (v["rule_id"], None, names.get(v["cell_name"], v["cell_name"]))
        src = names.get(c["src_flop"]) if c.get("src_flop") else f"port {c['src_port']}"
        return (v["rule_id"], src, names.get(c["dst_flop"]))

    # The same findings on the same registers, whatever the analyzer reports
    # today (both sides go through the same rules).
    assert Counter(slang_finding(v) for v in slang_report["violations"]) == Counter(
        _finding(name, v) for v in committed_report["violations"]
    )
    registers = set(FLOP_ROLES.get(name, {}))
    assert _flop_shapes(module, registers) == _flop_shapes(committed, registers)
    comb = {t: n for t, n in CELLS[name].items() if t not in FLOP_ROLES_TYPES}
    got = Counter(c.type for c in module.cells.values())
    assert {t: got[t] for t in comb} == comb


FLOP_ROLES_TYPES = frozenset(t for roles in FLOP_ROLES.values() for t in roles.values())
