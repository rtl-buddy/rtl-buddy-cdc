"""Reset analysis items of rtl-buddy-cdc#398 (items 3, 6, 7).

3. ``bad_primary_reset_bypassed_sync``: RDC-008 fires on raw-port reset
   consumers in a clock domain that has no synchroniser for the port
   (one exists elsewhere) *and* on ones that bypass the synchroniser
   their own domain has. The message says which, and names the chain's
   tail in the second case.
6. ``good_sync_reset_lanes``: a register whose lanes have different sync
   resets is one cell in the ``proc`` form and one cell per reset after
   ``opt_dff``; the reset-domain map lists the same resets for both.
   ``bad_sync_reset_lane_crossing``: a foreign-domain sync reset on some
   lanes only is one RDC-003 in either form.
7. ``good_sync_reset_lanes``: slang folds the first select of ``if (a)
   ... else if (b)`` into ``SRST`` and leaves the second on ``D``; the
   map reads that ``$sdff`` as both selects, as ``opt_dff`` folds them.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rtl_buddy_cdc import netlist, sdc as sdc_mod
from rtl_buddy_cdc.cli import app
from rtl_buddy_cdc.domain import find_crossings
from rtl_buddy_cdc.domain_map import _hier_path
from rtl_buddy_cdc.netlist import Module
from rtl_buddy_cdc.reset_domain import (
    LaneReset,
    ResetDomain,
    ResetSource,
    assign_reset_domains,
)
from rtl_buddy_cdc.reset_hints import ResetHints, SynchronizerHint
from rtl_buddy_cdc.rules import (
    _build_context,
    _reset_sync_outputs,
    check_rdc_008,
    run_all,
)

REPO = Path(__file__).resolve().parent.parent
FIX = REPO / "tests" / "fixtures"


@pytest.fixture(autouse=True)
def _tokenizer_sdc_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    """Plain SDCs; the tokenizer avoids a Tcl worker per analysis."""
    monkeypatch.setenv(sdc_mod.BACKEND_ENV_VAR, "tokenizer")


def _reg(module: Module, cell_name: str) -> str:
    """The register a designer would search for: the user-written
    netname on the cell's ``Q`` (cell names differ between forms)."""
    q = set(module.cells[cell_name].connections.get("Q", ()))
    for n in sorted(
        module.netnames.values(), key=lambda n: (n.name[:1] == "$", n.name)
    ):
        if q & set(n.bits):
            return n.name
    return cell_name


def _load(name: str, suffix: str = "json") -> tuple[Module, sdc_mod.ClockSpec]:
    module = netlist.load(FIX / name / f"{name}.{suffix}")
    return module, sdc_mod.parse_file(FIX / name / f"{name}.sdc")


def _run(name: str, suffix: str = "json"):
    module, spec = _load(name, suffix)
    crossings = find_crossings(
        module, port_clock=spec.port_clock, **spec.clock_root_inputs()
    )
    return module, run_all(module, crossings, spec)


# --- item 3: RDC-008 names the shape it found -----------------------------

BYP = "bad_primary_reset_bypassed_sync"


def _rdc_008_by_clock(violations) -> dict[str, str]:
    out = {}
    for v in violations:
        assert v.rule_id == "RDC-008", v
        clk = "clk_a" if " in clk_a directly" in v.message else "clk_b"
        out[clk] = v.message
    return out


def test_rdc_008_fires_once_per_clock_and_nothing_else() -> None:
    _module, violations = _run(BYP)
    assert sorted(v.rule_id for v in violations) == ["RDC-008", "RDC-008"]
    assert sorted(_rdc_008_by_clock(violations)) == ["clk_a", "clk_b"]


def test_rdc_008_names_the_bypassed_chain_tail() -> None:
    """clk_a has a synchroniser for ``rst_n``: the message says the
    flops bypass it and names its tail, the reset they should use."""
    module, violations = _run(BYP)
    msg = _rdc_008_by_clock(violations)["clk_a"]
    tail = next(c for c in module.cells if _reg(module, c) == "sync_rst_n")
    assert f"bypassing the reset-synchroniser chain (tail '{tail}')" in msg
    assert "that clk_a already has for 'rst_n'" in msg
    assert f"Reset these flops from the chain's tail '{tail}'" in msg
    assert "no reset-synchroniser chain" not in msg
    raw = sorted(c for c in module.cells if _reg(module, c) in ("a_raw0", "a_raw1"))
    assert all(c in msg for c in raw)


def test_rdc_008_asks_for_a_chain_where_the_domain_has_none() -> None:
    """clk_b has no synchroniser for ``rst_n``: the message says so and
    where one is."""
    _module, violations = _run(BYP)
    msg = _rdc_008_by_clock(violations)["clk_b"]
    assert (
        "with no reset-synchroniser chain in that clock domain "
        "(there is one for it in clk_a)" in msg
    )
    assert "Add a 2FF reset-synchroniser chain in clk_b" in msg
    assert "bypassing" not in msg


def test_rdc_008_message_of_the_existing_fixture_is_the_missing_chain() -> None:
    _module, violations = _run("bad_primary_reset_unsynced")
    (v,) = [v for v in violations if v.rule_id == "RDC-008"]
    assert "(there is one for it in clk_a)" in v.message


def test_rdc_008_bypass_of_stages_with_no_structural_chain() -> None:
    """Recognised synchroniser stages that form no structural chain
    (e.g. user-marked ones fed from logic) are named as stages: which of
    them is the output is not recoverable."""
    module, spec = _load(BYP)
    ctx = _build_context(module, spec)
    ctx.__dict__["reset_sync_chains"] = ()  # as if no chain were structural
    violations = check_rdc_008(module, [], spec, ctx=ctx)
    msg = _rdc_008_by_clock(violations)["clk_a"]
    stages = sorted(
        c for c in module.cells if _reg(module, c) in ("sync_meta", "sync_rst_n")
    )
    assert f"reset synchroniser (stages '{stages[0]}', '{stages[1]}')" in msg
    assert "Reset these flops from the synchroniser's output" in msg


def test_reset_sync_outputs_per_port_and_clock() -> None:
    """A user-marked stage in a domain with no structural chain is listed
    as a stage; the structural chain's tail as the output."""
    module, spec = _load(BYP)
    b_raw0 = next(c for c in module.cells if _reg(module, c) == "b_raw0")
    hints = ResetHints(
        schema_version="1",
        synchronizers=(SynchronizerHint(instance=_hier_path(module, b_raw0)),),
    )
    ctx = _build_context(module, spec, reset_hints=hints)
    tail = next(c for c in module.cells if _reg(module, c) == "sync_rst_n")
    assert _reset_sync_outputs(ctx, ctx.reset_synchronizers) == {
        ("rst_n", "clk_a"): ([tail], True),
        ("rst_n", "clk_b"): ([b_raw0], False),
    }


# --- item 6: per-lane sync resets ------------------------------------------

LANES = "good_sync_reset_lanes"


def _map(name: str, suffix: str, tmp_path: Path) -> tuple[Module, dict]:
    d = FIX / name
    out = tmp_path / f"{name}.{suffix}.map.json"
    r = CliRunner().invoke(
        app,
        [
            "analyze",
            "-n",
            str(d / f"{name}.{suffix}"),
            "-s",
            str(d / f"{name}.sdc"),
            "--emit-reset-domain-map",
            str(out),
            "--no-findings",
        ],
    )
    assert r.exit_code == 0, r.output
    return netlist.load(d / f"{name}.{suffix}"), json.loads(out.read_text())


def _rows(module: Module, entries: list[dict]) -> list[tuple]:
    """Map entries keyed by register (cell names differ between forms);
    an inferred reset named by its register too."""
    cell_of_path = {_hier_path(module, c): c for c in module.cells}
    rows = []
    for e in entries:
        reset = e["reset"]
        if e["reset_kind"] == "inferred":
            reset = _reg(module, reset)
        rows.append(
            (
                _reg(module, cell_of_path[e["instance_path"]]),
                e.get("kind", ""),
                reset,
                e["reset_kind"],
                e["polarity"],
                e["type"],
            )
        )
    return sorted(rows)


_LANES_EXPECTED = [
    ("q_ce", "", "", "comb", "high", "sync"),
    ("q_mix", "", "", "comb", "high", "sync"),
    ("q_mix", "", "ra", "port", "high", "sync"),
    ("q_part", "", "ra", "port", "high", "sync"),
    ("q_rov", "", "ra", "port", "high", "sync"),
    ("q_split", "", "ra", "port", "high", "sync"),
    ("q_split", "", "rb", "port", "high", "sync"),
    ("q_two", "", "", "comb", "high", "sync"),
    ("q_two_n", "", "", "comb", "low", "sync"),
]


@pytest.mark.parametrize("suffix", ["json", "optdff.json"])
def test_flop_resets_list_every_lane_reset(suffix: str, tmp_path: Path) -> None:
    module, payload = _map(LANES, suffix, tmp_path)
    assert _rows(module, payload["flop_resets"]) == _LANES_EXPECTED
    names = {(s["name"], s["source"]) for s in payload["reset_sources"]}
    assert {("ra", "port"), ("rb", "port")} <= names


def test_proc_form_names_the_lanes_of_each_reset(tmp_path: Path) -> None:
    """The ``proc`` form's one cell carries one entry per reset with the
    lanes it resets (``opt_dff``'s slices need none: each is one cell)."""
    module, payload = _map(LANES, "json", tmp_path)
    lanes = {
        (_reg(module, c), e["reset"] or e["reset_kind"]): e.get("lanes")
        for e in payload["flop_resets"]
        for c in module.cells
        if _hier_path(module, c) == e["instance_path"]
    }
    assert lanes[("q_split", "ra")] == [0, 1]
    assert lanes[("q_split", "rb")] == [2, 3]
    assert lanes[("q_part", "ra")] == [0, 1]
    # 0101 vs 0011: the constants agree on lanes 0 and 3 only.
    assert lanes[("q_mix", "comb")] == [0, 3]
    assert lanes[("q_mix", "ra")] == [1, 2]
    assert lanes[("q_two", "comb")] is None
    _, folded = _map(LANES, "optdff.json", tmp_path)
    assert all("lanes" not in e for e in folded["flop_resets"])


def test_lane_resets_on_the_reset_domain() -> None:
    module = netlist.load(FIX / LANES / f"{LANES}.json")
    rds = {_reg(module, n): rd for n, rd in assign_reset_domains(module).items()}
    split = rds["q_split"]
    assert split.reset is None
    assert [g.lanes for g in split.lane_resets] == [(0, 1), (2, 3)]
    assert split.reset_groups() == split.lane_resets
    two = rds["q_two"]
    assert two.lane_resets == ()
    assert two.reset_groups() == (LaneReset(lanes=(), reset=two.reset),)
    assert ResetDomain(flop="x", reset=None).reset_groups() == ()


LX = "bad_sync_reset_lane_crossing"


@pytest.mark.parametrize("suffix", ["json", "optdff.json"])
def test_foreign_reset_on_some_lanes_is_one_rdc_003(suffix: str) -> None:
    """RDC-003 reads the reset muxes of every lane (``proc``) or the
    ``SRST`` of the slice (``opt_dff``): one finding either way, not one
    per lane group."""
    module, violations = _run(LX, suffix)
    fired = sorted(v.rule_id for v in violations)
    assert fired == ["CDC-001", "RDC-003"], fired
    (rdc,) = [v for v in violations if v.rule_id == "RDC-003"]
    kill = next(c for c in module.cells if _reg(module, c) == "kill_a")
    assert f"driven by flop {kill} " in rdc.message


@pytest.mark.parametrize("suffix", ["json", "optdff.json"])
def test_lane_crossing_in_the_map(suffix: str, tmp_path: Path) -> None:
    module, payload = _map(LX, suffix, tmp_path)
    assert _rows(module, payload["reset_crossings"]) == [
        ("q", "sync-crossing", "kill_a", "inferred", "high", "sync")
    ]
    (crossing,) = payload["reset_crossings"]
    assert crossing.get("lanes") == ([0, 1] if suffix == "json" else None)
    assert _rows(module, payload["flop_resets"]) == [
        ("q", "", "kill_a", "inferred", "high", "sync"),
        ("q", "", "rb", "port", "high", "sync"),
    ]


def test_polarity_mismatch_on_a_lane_reset(tmp_path: Path) -> None:
    """A port-declared polarity disagreeing with a lane reset is a
    ``polarity-mismatch`` record on those lanes, as on the slice."""
    from rtl_buddy_cdc.reset_domain import find_reset_crossings

    module = netlist.load(FIX / LANES / f"{LANES}.json")
    crossings = find_reset_crossings(module, {}, polarity_overrides={"rb": "low"})
    got = sorted(
        (_reg(module, c.flop), c.lanes, c.pin)
        for c in crossings
        if c.kind == "polarity-mismatch"
    )
    assert got == [("q_split", (2, 3), "SRST")]


# --- item 7: an $sdff with a reset mux left on D ---------------------------


def _reset_table(module: Module) -> dict[str, list[tuple]]:
    """Per register: every reset with its lanes, the lanes of an
    ``opt_dff`` slice being its ``Q`` bits' offsets in the register."""
    out: dict[str, list[tuple]] = {}
    for name, rd in assign_reset_domains(module).items():
        for g in rd.reset_groups():
            r: ResetSource = g.reset
            out.setdefault(_reg(module, name), []).append(
                (r.name, r.source, r.polarity, r.type, len(g.lanes))
            )
    return {k: sorted(v) for k, v in out.items()}


@pytest.mark.skipif(
    importlib.util.find_spec("pyslang") is None, reason="pyslang not installed"
)
def test_slang_reset_table_matches_the_proc_form() -> None:
    """Every register classifies as the ``proc`` form does, lane groups
    included: ``else if (!rb)`` selects on ``rb`` with the mux legs
    swapped, as ``proc`` builds it, so the combined reset of ``q_two_n``
    reads active low on both (rtl-buddy-cdc#402; slang read it active
    high, through a ``$logic_not``)."""
    from rtl_buddy_cdc.frontends import slang as slang_fe

    module, _ = slang_fe.elaborate_with_blackboxes([FIX / LANES / f"{LANES}.sv"], LANES)
    srst_types = {
        _reg(module, c.name): c.type for c in module.cells.values() if "sdff" in c.type
    }
    # The registers item 7 is about: one select on SRST, one on D.
    assert srst_types["q_two"] == "$sdff"
    assert srst_types["q_ce"] == "$sdffce"
    got = _reset_table(module)
    proc = _reset_table(netlist.load(FIX / LANES / f"{LANES}.json"))
    assert got["q_two_n"] == [("", "comb", "low", "sync", 0)]
    assert got == proc
