"""Reset analysis corner cases of rtl-buddy-cdc#394 (items 1, 2, 3, 5).

1. ``bad_set_clear_reset_polarity``: the polarity of a set / clear flop's
   reset is the polarity its *signal* acts with. ``proc`` writes an
   active-low clear as an inverter in front of an active-high ``CLR``;
   ``AsyncControl.source.polarity`` used to be the pin's, so RDC-002
   (port-declared and flop-to-flop) reported the correct flops and
   missed the wrong ones, in the ``proc`` and ``opt_dff`` forms.
2. ``bad_sync_reset_over_enable_capture``: a crossing that reaches a
   handshake capture through a synchronous reset the enable does not
   gate (``$sdffe``, reset over enable) is not a gated capture; CDC-001
   used to accept it. Under an ``$sdffce`` (reset only while enabled) it
   is gated.
3. ``good_sync_reset_forms``: a synchronous reset left on ``D`` by
   ``proc`` classifies as the reset ``opt_dff`` folds it into, so the
   ``proc``, ``opt_dff`` and slang reset-domain maps agree.
5. The golden-regeneration command documented for the reset-domain map
   runs as written.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import re
import shlex
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rtl_buddy_cdc import netlist, sdc as sdc_mod, sdc_objects
from rtl_buddy_cdc.cli import app
from rtl_buddy_cdc.domain import find_crossings
from rtl_buddy_cdc.domain_map import _hier_path
from rtl_buddy_cdc.netlist import Module
from rtl_buddy_cdc.reset_domain import (
    assign_reset_domains,
    effective_control,
    find_reset_crossings,
    sync_reset_lanes,
)
from rtl_buddy_cdc.rules import (
    _build_context,
    crossing_synchronizer,
    user_reset_polarity_overrides,
)

REPO = Path(__file__).resolve().parent.parent
FIX = REPO / "tests" / "fixtures"


@pytest.fixture(autouse=True)
def _tokenizer_sdc_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    """Plain SDCs; the tokenizer avoids a Tcl worker per analysis."""
    monkeypatch.setenv(sdc_mod.BACKEND_ENV_VAR, "tokenizer")


def _reg_of_bit(module: Module) -> dict:
    """Net bit -> the register name a designer would search for: the
    user-written netname before any ``$`` one."""
    out: dict = {}
    for n in sorted(
        module.netnames.values(), key=lambda n: (n.name[:1] == "$", n.name)
    ):
        for b in n.bits:
            out.setdefault(b, n.name)
    return out


def _reg(module: Module, cell_name: str) -> str:
    q = module.cells[cell_name].connections.get("Q", ())
    rob = _reg_of_bit(module)
    return next((rob[b] for b in q if b in rob), cell_name)


def _analyze(name: str, suffix: str) -> tuple[Module, dict]:
    d = FIX / name
    net = d / f"{name}.{suffix}"
    r = CliRunner().invoke(
        app, ["analyze", "-n", str(net), "-s", str(d / f"{name}.sdc"), "-f", "json"]
    )
    assert r.exit_code in (0, 1), r.output
    return netlist.load(net), json.loads(r.stdout)


def _by_rule(module: Module, report: dict) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for v in report["violations"]:
        out.setdefault(v["rule_id"], []).append(_reg(module, v["cell_name"]))
    return {k: sorted(v) for k, v in out.items()}


# --- item 1: set / clear flop polarity ------------------------------------

SC = "bad_set_clear_reset_polarity"
_SC_FORMS = ["json", "optdff.json", "synth.json"]


@pytest.mark.parametrize("suffix", _SC_FORMS)
def test_set_clear_reset_polarity_is_the_signals(suffix: str) -> None:
    """Every form classifies each clear (and set) with the polarity the
    RTL writes, whatever pin polarity Yosys gave it."""
    module = netlist.load(FIX / SC / f"{SC}.{suffix}")
    got = {
        _reg(module, name): (
            rd.reset.polarity if rd.reset else None,
            tuple(c.source.polarity for c in rd.controls),
        )
        for name, rd in assign_reset_domains(module).items()
    }
    assert got == {
        "r_lo": ("low", ("low",)),
        "q_ok": ("low", ("low", "low")),
        "q_ok_f": ("low", ("low", "low")),
        "q_bad": ("high", ("high", "low")),
        "q_bad_f": ("high", ("high", "low")),
        "s1": ("low", ("low", "low")),
        "s2": ("low", ("low", "low")),
        "q_sy": ("low", ("low",)),
    }


def test_proc_form_writes_the_active_low_clear_as_an_inverted_pin() -> None:
    """The shape under test: the ``proc`` form's ``CLR`` pins are all
    active high, so only an inverter in front of them carries the
    polarity."""
    module = netlist.load(FIX / SC / f"{SC}.json")
    srs = [c for c in module.cells.values() if c.type == "$dffsr"]
    assert len(srs) == 6
    assert {c.parameters["CLR_POLARITY"][-1] for c in srs} == {"1"}


@pytest.mark.parametrize("suffix", _SC_FORMS)
def test_rdc_002_reports_only_the_wrong_clears(suffix: str) -> None:
    """Only RDC-002, on the two wrongly wired clears. No RDC-007 either:
    the set / clear reset synchroniser ``s1``/``s2`` loads 1, the
    deassertion value of its active-low clear (its chain polarity used
    to be the pin's, active high, in every form)."""
    module, report = _analyze(SC, suffix)
    assert _by_rule(module, report) == {"RDC-002": ["q_bad", "q_bad_f"]}
    msgs = sorted(v["message"] for v in report["violations"])
    # One wrongly wired flop per finding, not a group of right and wrong.
    assert all("destination flop: " in m for m in msgs), msgs
    assert any("port-level declaration" in m for m in msgs)
    assert any("producer flop" in m for m in msgs)


@pytest.mark.parametrize("suffix", ["json", "optdff.json"])
def test_polarity_mismatch_crossing_names_only_the_wrong_clear(suffix: str) -> None:
    module = netlist.load(FIX / SC / f"{SC}.{suffix}")
    crossings = find_reset_crossings(
        module,
        {name: "clk" for name in assign_reset_domains(module)},
        polarity_overrides=user_reset_polarity_overrides(module),
    )
    got = [(_reg(module, c.flop), c.kind, c.pin, c.reset.polarity) for c in crossings]
    assert got == [("q_bad", "polarity-mismatch", "CLR", "high")]


def test_effective_control_flips_polarity_per_inverter() -> None:
    """Unit: the polarity returned flips with each inverter stepped
    through and is the pin's when none is."""
    module = netlist.load(FIX / SC / f"{SC}.json")
    drv = {
        b: (c.name, p)
        for c in module.cells.values()
        for p in ("Y", "Q")
        for b in c.connections.get(p, ())
        if isinstance(b, int)
    }
    rst_n = module.ports["rst_n"].bits[0]
    for cell in module.cells.values():
        if cell.type != "$dffsr" or _reg(module, cell.name) not in ("q_ok", "q_bad"):
            continue
        clr = cell.connections["CLR"][0]
        bit, pol = effective_control(
            module, clr, 1, frozenset(), drv, strip_inverters=True
        )
        assert bit == rst_n
        assert pol == (0 if _reg(module, cell.name) == "q_ok" else 1)
        # Without inverter stripping (a single-reset flop) nothing flips.
        assert effective_control(module, clr, 1, frozenset(), drv)[1] == 1


# --- item 2: a crossing through a sync reset outside the enable -----------

RE = "bad_sync_reset_over_enable_capture"
_RE_FORMS = ["json", "optdff.json", "synth.json"]


def test_reset_enable_fixture_carries_both_priorities() -> None:
    optdff = {
        c.type for c in netlist.load(FIX / RE / f"{RE}.optdff.json").cells.values()
    }
    assert {"$sdffe", "$sdffce"} <= optdff
    synth = {c.type for c in netlist.load(FIX / RE / f"{RE}.synth.json").cells.values()}
    assert any(t.startswith("$_SDFFE_") for t in synth), synth
    assert any(t.startswith("$_SDFFCE_") for t in synth), synth


@pytest.mark.parametrize("suffix", _RE_FORMS)
def test_reset_over_enable_crossing_is_cdc_001(suffix: str) -> None:
    module, report = _analyze(RE, suffix)
    by_rule = _by_rule(module, report)
    assert by_rule.pop("CDC-001") == ["f_rov"]
    # One grouped RDC-003 for kill_a's two sync-reset consumers; its
    # representative cell is whichever sorts first in that form.
    assert list(by_rule) == ["RDC-003"] and len(by_rule["RDC-003"]) == 1
    (cdc,) = [v for v in report["violations"] if v["rule_id"] == "CDC-001"]
    assert "has no second-stage synchronizer" in cdc["message"]
    assert "captures only under an enable" not in cdc["message"]
    # The crossing reported is the reset's, from kill_a.
    src = cdc["crossing"]["src_flop"] if "crossing" in cdc else None
    if src is not None:
        assert _reg(module, src) == "kill_a"


def _crossings(name: str, suffix: str):
    d = FIX / name
    module = netlist.load(d / f"{name}.{suffix}")
    spec = sdc_mod.parse_file(d / f"{name}.sdc")
    sdc_objects.resolve_port_delays(module, spec)
    sdc_mod.synthesize_unconstrained_inputs(spec, module)
    crossings = [
        c
        for c in find_crossings(module, port_clock=spec.port_clock)
        if spec.are_async(c.src_clock, c.dst_clock)
    ]
    return module, spec, crossings


@pytest.mark.parametrize("suffix", _RE_FORMS)
def test_capture_schemes_follow_reset_priority(suffix: str) -> None:
    """``flag_a`` is a handshake capture into both flops; ``kill_a`` is
    one into ``f_eor`` (reset under the enable) and none into ``f_rov``
    (reset over it)."""
    module, spec, crossings = _crossings(RE, suffix)
    ctx = _build_context(module, spec)
    schemes = {
        (_reg(module, c.src_flop.cell.name), _reg(module, c.dst_flop.cell.name)): (
            crossing_synchronizer(module, c, ctx)
        )
        for c in crossings
        if c.src_flop is not None
        and _reg(module, c.dst_flop.cell.name) in ("f_rov", "f_eor")
    }
    assert schemes[("flag_a", "f_rov")] == "handshake"
    assert schemes[("flag_a", "f_eor")] == "handshake"
    assert schemes[("kill_a", "f_eor")] == "handshake"
    assert schemes[("kill_a", "f_rov")] != "handshake"


# --- item 3: proc-form sync resets in the reset-domain map ----------------

SR = "good_sync_reset_forms"
_SR_EXPECTED = {
    "q_hi": ("r", "port", "high", "sync"),
    "q_lo": ("rn", "port", "low", "sync"),
    "q_two": ("", "comb", "high", "sync"),
    "q_rov": ("r", "port", "high", "sync"),
    "q_eor": ("r", "port", "high", "sync"),
    "q_inf": ("r_q", "inferred", "high", "sync"),
    "q_one": ("r", "port", "high", "sync"),
    "r_q": None,
}


def _reset_table(module: Module) -> dict[str, tuple | None]:
    """Per register: ``(reset, kind, polarity, type)``, an inferred reset
    named by its register (cell names differ between forms)."""
    out: dict[str, tuple | None] = {}
    for name, rd in assign_reset_domains(module).items():
        r = rd.reset
        if r is None:
            out[_reg(module, name)] = None
            continue
        src = _reg(module, r.name) if r.source == "inferred" else r.name
        out[_reg(module, name)] = (src, r.source, r.polarity, r.type)
    return out


@pytest.mark.parametrize("suffix", ["json", "optdff.json"])
def test_sync_reset_classified_in_both_forms(suffix: str) -> None:
    assert _reset_table(netlist.load(FIX / SR / f"{SR}.{suffix}")) == _SR_EXPECTED


def test_proc_form_has_no_reset_pins() -> None:
    proc = netlist.load(FIX / SR / f"{SR}.json")
    assert {c.type for c in proc.cells.values() if "dff" in c.type} == {"$dff"}
    folded = netlist.load(FIX / SR / f"{SR}.optdff.json")
    assert {"$sdff", "$sdffe", "$sdffce"} <= {c.type for c in folded.cells.values()}


def _map_by_register(name: str, suffix: str) -> list[tuple]:
    """``flop_resets`` of the CLI's ``--emit-reset-domain-map`` for one
    netlist form, each row keyed by register (cell names differ between
    forms)."""
    d = FIX / name
    net = d / f"{name}.{suffix}"
    out = Path(f"{suffix}.map.json").resolve()
    r = CliRunner().invoke(
        app,
        [
            "analyze",
            "-n",
            str(net),
            "-s",
            str(d / f"{name}.sdc"),
            "--emit-reset-domain-map",
            str(out),
            "--no-findings",
        ],
    )
    assert r.exit_code == 0, r.output
    module = netlist.load(net)
    cell_of_path = {_hier_path(module, c): c for c in module.cells}
    rows = []
    for fr in json.loads(out.read_text())["flop_resets"]:
        reset = fr["reset"]
        if fr["reset_kind"] == "inferred":
            reset = _reg(module, reset)
        rows.append(
            (
                _reg(module, cell_of_path[fr["instance_path"]]),
                reset,
                fr["reset_kind"],
                fr["polarity"],
                fr["type"],
            )
        )
    return sorted(rows)


def test_reset_domain_maps_agree_across_forms(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    proc = _map_by_register(SR, "json")
    assert proc == _map_by_register(SR, "optdff.json")
    assert [row[0] for row in proc] == sorted(r for r, v in _SR_EXPECTED.items() if v)


@pytest.mark.skipif(
    importlib.util.find_spec("pyslang") is None, reason="pyslang not installed"
)
def test_slang_frontend_lists_the_same_sync_resets() -> None:
    """The ``lint`` path never runs ``opt_dff``: the Yosys frontend's map
    now lists the sync resets the slang frontend's ``$sdff*`` carry.
    ``q_two`` differs by design of the two lowerings: slang folds the
    first of its two selects into ``SRST`` and leaves the second on
    ``D``, ``opt_dff`` folds both into one ``$reduce_or``."""
    from rtl_buddy_cdc.frontends import slang as slang_fe

    module, _ = slang_fe.elaborate_with_blackboxes([FIX / SR / f"{SR}.sv"], SR)
    got = _reset_table(module)
    assert got.pop("q_two") == ("r", "port", "high", "sync")
    expected = dict(_SR_EXPECTED)
    del expected["q_two"]
    assert got == expected


def test_partly_reset_register_keeps_no_reset() -> None:
    """A register whose lanes are reset differently is sliced by
    ``opt_dff``; the ``proc`` form's one cell keeps ``reset = None``."""
    module = netlist.load(FIX / SR / f"{SR}.json")
    ctx = _build_context(module, None)
    flop = next(f for f in ctx.flops if _reg(module, f.cell.name) == "q_hi")
    lanes = sync_reset_lanes(flop, module, ctx.bit_drivers, ctx.reader_counts)
    assert len({r for _, r in lanes}) == 1
    # Drop the reset from lane 0 only: rewire D[0] to the mux's data leg.
    mux_name, _, idx = ctx.bit_drivers[flop.d[0]]
    data0 = module.cells[mux_name].connections["A"][idx]
    cell = flop.cell
    new_d = (data0, *cell.connections["D"][1:])
    cells = dict(module.cells)
    cells[cell.name] = type(cell)(
        name=cell.name,
        type=cell.type,
        connections={**cell.connections, "D": new_d},
        parameters=cell.parameters,
        attributes=cell.attributes,
    )
    sliced = Module(
        name=module.name, ports=module.ports, cells=cells, netnames=module.netnames
    )
    assert assign_reset_domains(sliced)[cell.name].reset is None


# --- item 5: the documented regeneration command --------------------------

GOLDEN_FIX = "bad_marked_reset_polarity"


def _documented_args(text: str, fix: str) -> list[str]:
    """The ``analyze`` arguments of the first ``uv run rtl-buddy-cdc
    analyze`` command in ``text``, line continuations joined and ``<fix>``
    filled in."""
    m = re.search(r"uv run rtl-buddy-cdc analyze((?:[^\n]*\\\n)+[^\n]*)", text)
    assert m is not None, "no regeneration command found"
    words = shlex.split(m.group(1).replace("\\\n", " ").replace("<fix>", fix))
    return words


def _doc_sources() -> list[tuple[str, str]]:
    tree = ast.parse((REPO / "tests" / "test_reset_domain_map.py").read_text())
    golden_test = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "test_golden_matches"
    )
    wiki = (
        REPO / "wiki" / "raw" / "articles" / "rtl-buddy-cdc-reset-domain-map-schema.md"
    )
    section = wiki.read_text().split("## Diffability and CI use", 1)[1]
    return [
        ("docstring", ast.get_docstring(golden_test) or ""),
        ("schema doc", section),
    ]


@pytest.mark.parametrize("which", ["docstring", "schema doc"])
def test_documented_regeneration_command_writes_the_golden(
    which: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run the documented command from a copy of the repo layout and
    check it writes the golden where it says, byte-for-byte equal in
    content."""
    text = dict(_doc_sources())[which]
    args = _documented_args(text, GOLDEN_FIX)
    assert "--project-root" in args
    dst = tmp_path / "tests" / "fixtures" / GOLDEN_FIX
    shutil.copytree(FIX / GOLDEN_FIX, dst)
    golden = json.loads((dst / f"{GOLDEN_FIX}.reset-domain-map.json").read_text())
    (dst / f"{GOLDEN_FIX}.reset-domain-map.json").unlink()
    monkeypatch.chdir(tmp_path)
    r = CliRunner().invoke(app, ["analyze", *args])
    assert r.exit_code == 0, r.output
    emitted = tmp_path / args[args.index("--emit-reset-domain-map") + 1]
    assert emitted.is_file(), sorted(str(p) for p in tmp_path.rglob("*.json"))
    assert json.loads(emitted.read_text()) == golden


def test_without_project_root_the_map_lands_under_the_sdc_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Why the flag is documented: a relative emit path anchors at the
    SDC's directory otherwise (rtl-buddy-cdc#245)."""
    args = _documented_args(dict(_doc_sources())["docstring"], GOLDEN_FIX)
    i = args.index("--project-root")
    del args[i : i + 2]
    dst = tmp_path / "tests" / "fixtures" / GOLDEN_FIX
    shutil.copytree(FIX / GOLDEN_FIX, dst)
    monkeypatch.chdir(tmp_path)
    r = CliRunner().invoke(app, ["analyze", *args])
    assert r.exit_code == 0, r.output
    rel = args[args.index("--emit-reset-domain-map") + 1]
    assert (dst / rel).is_file()
