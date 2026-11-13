"""Gate-level variant of the fixture corpus (rtl-buddy-cdc#331, item 1).

Every committed fixture netlist is RTL-level: ``proc; flatten`` leaves
``$dff`` / ``$adff`` / ``$mux`` / ``$and`` cells, so the analyzer's
gate-level paths (``$_DFF_*`` flops, ``$_MUX_``, ``$_AND_``, …) were
exercised only by hand-built cells in unit tests. This module rebuilds
each fixture's ``.sv`` with a generic Yosys gate flow and asserts the
analyzer reaches the same verdict as on the committed RTL netlist.

The flow
~~~~~~~~

Front half, shared by every leg. It reproduces how the committed
``<name>.json`` was built:

    read_verilog -sv <every .sv in the dir>; hierarchy -top <top>;
    blackbox <m> ...; proc; flatten; opt_clean

``<top>`` and the ``blackbox`` list are read from the committed JSON
(its ``top`` / ``blackbox`` module attributes), so the stub-blackboxed
fixtures (``bbx_*``, ``xpm_*``, ``*_abstract`` …) keep the same
boundary. ``opt_clean`` matters: the analyzer's verdict is not
invariant under dead cells (an unloaded ``$not`` of a clock fires
CDC-008), and most committed netlists were written after
``opt_clean``. :data:`_FRONT_HALF_CLEANUP` lists the fixtures whose
committed netlist used a different clean-up pass.

Legs, appended to the front half:

``rtl``
    Nothing. The control leg: it shows this Yosys rebuilds a netlist
    with the committed netlist's verdict, so a divergence on the
    other two legs comes from the lowering, not from front-half drift
    between Yosys versions. It has no allowlist rows.
``gates``
    ``techmap; opt_clean``. Pure lowering: every coarse cell becomes
    ``$_*_`` gates, every W-bit flop W 1-bit ``$_DFF_*`` cells.
    ``techmap`` rather than bare ``simplemap`` because ``simplemap``
    leaves ``$pmux`` / ``$eq`` / arithmetic coarse, so the result is
    not a gate-level netlist (the only extra divergence ``techmap``
    produces on the corpus is ``bad_onehot_decode_independent_sync``).
``synth``
    ``opt_dff; techmap; abc -g AND,OR,XOR,MUX; opt_clean``.
    Synthesis-shaped: ``opt_dff`` first folds enable and sync-reset
    muxes into the flops, so this is the leg that produces
    ``$_DFFE_*`` / ``$_SDFF_*`` / ``$_SDFFE_*``, and ABC then
    restructures the combinational logic. Full ``opt`` is deliberately
    not used: ``opt_merge`` merges the duplicated synchroniser chains
    of the reconvergence fixtures and removes the bug under test.

Every leg compares against the committed netlist, never netlist bytes,
so the result does not depend on the Yosys version's cell naming.

What "agree" means
~~~~~~~~~~~~~~~~~~

Equality of the per-rule fired-count dict from ``analyze -f json``,
the same contract as :mod:`tests.fuzz.test_corpus_diff`. Counts, not
just the fired rule-id set, although bit-blasting turns one W-bit
flop into W cells: violations are meant to be grouped per bus and per
synchroniser, not per cell, so a W-bit crossing that turns into W
CDC-001s (or into nothing) is exactly the regression this corpus
exists to find. On the measured corpus a set-only contract would hide
only count-level divergences that are real bugs anyway
(``bad_scan_mode_functional_crossing``, CDC-001 2 → 1, and on the
``synth`` leg ``bad_untyped_sync_reset_{mux,srst}``, CDC-011 2 → 1),
so the stricter contract costs nothing.

Known-divergence allowlist
~~~~~~~~~~~~~~~~~~~~~~~~~~

:data:`_KNOWN_GATE_DIVERGENCES` maps ``fixture → {leg: root cause}``
and is strict in both directions, like ``test_corpus_diff``: a listed
(fixture, leg) that diverges is an ``xfail`` naming its root cause, an
unlisted one that diverges fails, and a listed one that now agrees
fails until its row is removed. Each root cause is one of the
``_R_*`` constants below; analyzer bugs are fixed in their own
change, never by weakening this comparison.

Committed gate-level netlists
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

A handful of fixtures also carry a committed ``<name>.gates.json`` or
``<name>.synth.json``, built by the leg of the same name, so the
default (no-Yosys) CI job exercises real ``$_DFF_*`` netlists too.
:func:`test_committed_gate_netlist_matches_rtl` holds them to the same
allowlist. Rebuild them (from the repo root, so ``src`` attributes stay
relative) with::

    uv run python tests/test_gate_level_corpus.py good_2ff_sync:gates ...

Each argument is ``<fixture>:<leg>``; with no arguments every committed
gate netlist is rebuilt in place.

Runtime and caching
~~~~~~~~~~~~~~~~~~~

The sweep is marked ``gate_level`` and skipped when ``yosys`` is not on
PATH; CI runs it in the ``fuzz + sim`` job. Built netlists are cached
under ``tests/.gate_level_cache/`` keyed by SHA-256 of the Yosys
version, the script and the source bytes (the
:mod:`tests.fuzz.yosys_cache` scheme), so only the first run pays for
Yosys. ``rm -rf tests/.gate_level_cache`` resets it.
"""

from __future__ import annotations

import functools
import hashlib
import json
import shutil
import subprocess
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rtl_buddy_cdc import sdc as sdc_mod
from rtl_buddy_cdc.cli import app

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = REPO_ROOT / "tests" / "fixtures"
CACHE_ROOT = Path(__file__).resolve().parent / ".gate_level_cache"

LEGS: dict[str, str] = {
    "rtl": "",
    "gates": "techmap; opt_clean",
    "synth": "opt_dff; techmap; abc -g AND,OR,XOR,MUX; opt_clean",
}
GATE_LEGS = ("gates", "synth")

_DEFAULT_CLEANUP = "opt_clean"
# Fixtures whose committed netlist was written after a different
# clean-up than ``opt_clean``.
_FRONT_HALF_CLEANUP: dict[str, str] = {
    # Built with ``opt``: the committed netlist holds a ``$sdff``.
    "bad_rdc_003_sync_reset_crossing": "opt",
    # Built with ``opt_dff`` to produce the $sdffce / $adffe / $dffe
    # families (the fixture's reason to exist).
    "flop_zoo_crossing": "opt_dff; opt_clean",
}


# --- Root causes -----------------------------------------------------------
# One constant per root cause, so each can be filed as one issue. "Analyzer
# bug": the gate netlist carries the information and the analyzer misreads
# it. "Limitation": the information survives only in another form (or not
# at all) and the rule's structural definition does not reach it.

_R_RESET_PIN = (
    "analyzer bug: gate-level flops carry async/sync reset on pin R "
    "($_DFF_PN0_, $_DFFE_PN0P_, $_SDFF_*, $_DFFSR_*), but reset_domain "
    "and the RDC / CDC-015 / CDC-011 reset paths only read $adff ARST and "
    "$sdff SRST"
)
_R_CLK_POLARITY = (
    "analyzer bug: rules._clk_polarity decodes a bare N segment "
    "($_DFF_N_) but reads $_DFF_NN0_ (negedge, async reset) as posedge, "
    "so CDC-016 misses the opposite-edge stage"
)
_R_MUX = (
    "analyzer bug: $_MUX_ is not matched where the analyzer looks for "
    "$mux (sync-reset mux in a sync chain, scan-mode mux, toggle hold mux)"
)
_R_CLOCK_GATE = (
    "analyzer bug: the clock tracer's gate set (domain._GATE_TYPES) has no "
    "$_AND_ / $_OR_ / ..., so a gated or combined clock is not traced "
    "through gate-level cells"
)
_R_RESET_PIN_AND_MUX = (
    "analyzer bug: reset pin R not read (see the reset-pin rows) and the "
    "derived-reset logic is a $_MUX_ the reset tracer does not walk"
)
_R_BIT_BLAST = (
    "limitation: a W-bit flop becomes W 1-bit flops, and the bus rules "
    "(CDC-004 / 012 / 020, gated- and gray-bus acceptance, CDC-014's "
    "width guard) key on cell width; the bus survives only as a "
    "multi-bit netname"
)
_R_MUX_AND_BIT_BLAST = (
    "analyzer bug + limitation: the clock-mux $_MUX_ is not traced "
    "(CDC-011 lost) and the bus is bit-blasted (CDC-004 lost)"
)
_R_DECODER = (
    "limitation: techmap splits the shared multi-bit decoder cell into "
    "per-lane gates, and CDC-019 groups lanes by one shared >=2-bit "
    "driver cell"
)
_R_OPT_CONST_FLOP = (
    "inherent: opt_dff removes the stuck-at-0 `meta` flop that is the "
    "bug under test (optimisation changes the design, not the lowering)"
)
_R_ENABLE_TOGGLE = (
    "analyzer bug (RTL-level): opt_dff folds the toggle into $adffe "
    "(D=~Q on EN) and CDC-013 only recognises a toggle through a hold "
    "$mux, so an enable-flop toggle is missed at RTL level too"
)


def _both(reason: str) -> dict[str, str]:
    return {leg: reason for leg in GATE_LEGS}


_KNOWN_GATE_DIVERGENCES: dict[str, dict[str, str]] = {
    # Reset pin R.
    "bad_marked_reset_polarity": _both(_R_RESET_PIN),
    "bad_primary_reset_unsynced": _both(_R_RESET_PIN),
    "bad_rdc_002_polarity_mismatch": _both(_R_RESET_PIN),
    "bad_rdc_003_sync_reset_crossing": _both(_R_RESET_PIN),
    "bad_rdc_004_comb_driven_reset": _both(_R_RESET_PIN),
    "bad_rdc_005_multi_source_reset": _both(_R_RESET_PIN),
    "bad_reset_crossing": _both(_R_RESET_PIN),
    "bad_reset_sync_deassert_polarity": {
        "gates": _R_RESET_PIN,
        "synth": _R_OPT_CONST_FLOP,
    },
    "bad_reset_tree": _both(_R_RESET_PIN),
    "bad_sync_chain_foreign_reset": _both(_R_RESET_PIN),
    "bad_untyped_sync_reset_mux": {"synth": _R_RESET_PIN},
    "bad_untyped_sync_reset_srst": {"synth": _R_RESET_PIN},
    "flop_zoo_crossing": _both(_R_RESET_PIN),
    "bad_derived_async_reset_unsync": _both(_R_RESET_PIN_AND_MUX),
    # Clock polarity.
    "bad_opposite_edge_sync": _both(_R_CLK_POLARITY),
    # $_MUX_.
    "bad_scan_mode_functional_crossing": _both(_R_MUX),
    "good_scan_mode_ignored": _both(_R_MUX),
    "good_2ff_sync_srst": {"gates": _R_MUX},
    "good_packed_shift_sync_srst": {"gates": _R_MUX},
    "bad_toggle_no_xor_tail": {"gates": _R_MUX, "synth": _R_ENABLE_TOGGLE},
    "bad_unconstrained_input_muxed_clock": _both(_R_MUX_AND_BIT_BLAST),
    # Gate-level clock gating / combining.
    "bad_clock_gating_async_enable": _both(_R_CLOCK_GATE),
    "bad_unconstrained_input_derived_clock": _both(_R_CLOCK_GATE),
    "clock_combine_gate": _both(_R_CLOCK_GATE),
    "clock_combine_generated": _both(_R_CLOCK_GATE),
    "inferred_gate_clock": _both(_R_CLOCK_GATE),
    # Bit-blasted buses.
    "bad_async_fifo_binary_ptrs": _both(_R_BIT_BLAST),
    "bad_bus_crossing": _both(_R_BIT_BLAST),
    "bad_functional_datahold_enable": _both(_R_BIT_BLAST),
    "bad_gray_phase_break": _both(_R_BIT_BLAST),
    "bad_handshake_ack_missing": _both(_R_BIT_BLAST),
    "bad_mixed_handshake_datahold": _both(_R_BIT_BLAST),
    "bad_quasi_static_unmarked": _both(_R_BIT_BLAST),
    "bad_sliced_bus_reconvergence": _both(_R_BIT_BLAST),
    "bad_typed_port_bus_crossing": _both(_R_BIT_BLAST),
    "bad_unconstrained_input_bus_two_domains": _both(_R_BIT_BLAST),
    "blackbox_leaf_crossing": _both(_R_BIT_BLAST),
    "foreign_input_no_abstract": _both(_R_BIT_BLAST),
    "good_buffered_gated_bus_crossing": _both(_R_BIT_BLAST),
    "good_dffe_gated_bus_crossing": _both(_R_BIT_BLAST),
    "good_functional_datahold_handshake": _both(_R_BIT_BLAST),
    "good_gray_counter_crossing": {"gates": _R_BIT_BLAST},
    "good_mixed_handshake_datahold": _both(_R_BIT_BLAST),
    "good_unconstrained_input_bus_two_domains_typed": _both(_R_BIT_BLAST),
    "ip_cdc_handshake": _both(_R_BIT_BLAST),
    "safe_single_input": _both(_R_BIT_BLAST),
    "scaling_perf": _both(_R_BIT_BLAST),
    "shared_subtree_compose": _both(_R_BIT_BLAST),
    "single_clock_leaf_abstract": _both(_R_BIT_BLAST),
    "wide_bus_lane_crossing": _both(_R_BIT_BLAST),
    "wide_bus_mixing_crossing": _both(_R_BIT_BLAST),
    # Decoder structure.
    "bad_onehot_decode_independent_sync": _both(_R_DECODER),
}


# --- Fixture discovery ------------------------------------------------------


@dataclass(frozen=True)
class Fixture:
    name: str
    top: str
    blackboxes: tuple[str, ...]
    sources: tuple[Path, ...]
    sdc: Path
    netlist: Path


def _attr_set(module: dict, key: str) -> bool:
    value = module.get("attributes", {}).get(key)
    if value is None:
        return False
    try:
        return int(str(value), 2) != 0
    except ValueError:
        return bool(value)


def _discover() -> list[Fixture]:
    """Every fixture dir with ``<name>.sv`` + ``<name>.sdc`` + ``<name>.json``."""
    out: list[Fixture] = []
    for d in sorted(p for p in FIXTURES.iterdir() if p.is_dir()):
        sv, sdc, js = (d / f"{d.name}{ext}" for ext in (".sv", ".sdc", ".json"))
        if not (sv.exists() and sdc.exists() and js.exists()):
            continue
        modules: dict = json.loads(js.read_text())["modules"]
        tops = [name for name, m in modules.items() if _attr_set(m, "top")]
        assert len(tops) == 1, f"{js}: expected one top module, got {tops}"
        out.append(
            Fixture(
                name=d.name,
                top=tops[0],
                blackboxes=tuple(
                    sorted(n for n, m in modules.items() if _attr_set(m, "blackbox"))
                ),
                sources=tuple(sorted(d.glob("*.sv"))),
                sdc=sdc,
                netlist=js,
            )
        )
    return out


_FIXTURES = _discover()
_BY_NAME = {fx.name: fx for fx in _FIXTURES}


def fixture(name: str) -> Fixture:
    return _BY_NAME[name]


# --- Build ------------------------------------------------------------------


def yosys_available() -> bool:
    return shutil.which("yosys") is not None


def yosys_script(fx: Fixture, leg: str, out: str) -> str:
    """The Yosys script for ``fx`` on ``leg``, writing ``out``.

    Source paths are repo-relative (run Yosys from the repo root), so
    the script, the cache key and the netlists' ``src`` attributes do
    not depend on where the checkout lives.
    """
    sources = " ".join(p.relative_to(REPO_ROOT).as_posix() for p in fx.sources)
    steps = [f"read_verilog -sv {sources}", f"hierarchy -top {fx.top}"]
    steps += [f"blackbox {m}" for m in fx.blackboxes]
    steps += ["proc", "flatten", _FRONT_HALF_CLEANUP.get(fx.name, _DEFAULT_CLEANUP)]
    if LEGS[leg]:
        steps.append(LEGS[leg])
    steps.append(f"write_json {out}")
    return "; ".join(steps)


@functools.cache
def _yosys_version() -> str:
    proc = subprocess.run(["yosys", "-V"], capture_output=True, text=True, check=True)
    return proc.stdout.strip()


def _run_yosys(script: str) -> subprocess.CompletedProcess[str]:
    """Run ``script`` through yosys from a script file, not ``-p``.

    Some Yosys releases (0.52, the one GitHub's ``ubuntu-latest`` apt
    installs) split a ``-p`` argument on commas, so
    ``abc -g AND,OR,XOR,MUX`` became the commands ``OR``, ``XOR`` …
    ("No such command: OR"). 0.33 and 0.64 do not. A script file is read
    the same way by all of them.
    """
    with tempfile.TemporaryDirectory() as d:
        ys = Path(d) / "flow.ys"
        ys.write_text(script + "\n")
        return subprocess.run(
            ["yosys", "-q", "-s", str(ys)],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )


def build(fx: Fixture, leg: str) -> Path:
    """Build ``fx`` on ``leg`` and return the cached netlist path."""
    h = hashlib.sha256()
    h.update(_yosys_version().encode())
    h.update(yosys_script(fx, leg, "<out>").encode())
    for src in fx.sources:
        h.update(src.read_bytes())
    digest = h.hexdigest()
    out = CACHE_ROOT / digest[:2] / f"{digest}.json"
    if out.exists():
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    # Write beside the entry and rename, so an interrupted build never
    # leaves a truncated netlist under the final name.
    tmp = out.with_name(f"{digest}.partial.json")
    proc = _run_yosys(yosys_script(fx, leg, tmp.as_posix()))
    if proc.returncode != 0:
        raise RuntimeError(
            f"yosys failed for {fx.name} ({leg}):\n"
            f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
        )
    tmp.replace(out)
    return out


# --- Analyze ----------------------------------------------------------------

_RUNNER = CliRunner()


@pytest.fixture(autouse=True)
def _tokenizer_sdc_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    """Read SDCs with the in-process tokenizer backend.

    Both sides of every comparison read the same SDC, so the backend
    cannot cause a divergence, and the Tcl backend's per-parse worker
    process (~0.1 s) would otherwise be most of this module's runtime.
    Backend parity is covered by tests/test_sdc_backends.py.
    """
    monkeypatch.setenv(sdc_mod.BACKEND_ENV_VAR, "tokenizer")


def fired(netlist: Path, sdc: Path) -> dict[str, int]:
    """Per-rule fired counts from ``analyze -f json`` (the CLI path, so
    blackbox abstraction and composition run as they do for users)."""
    result = _RUNNER.invoke(
        app, ["analyze", "-n", str(netlist), "-s", str(sdc), "-f", "json"]
    )
    assert result.exit_code in (0, 1), (
        f"analyze failed on {netlist} (exit {result.exit_code}):\n{result.output}"
    )
    report = json.loads(result.stdout)
    return dict(sorted(Counter(v["rule_id"] for v in report["violations"]).items()))


@functools.cache
def _committed_fired(name: str) -> dict[str, int]:
    fx = _BY_NAME[name]
    return fired(fx.netlist, fx.sdc)


def _check(name: str, leg: str, rtl: dict[str, int], gate: dict[str, int]) -> None:
    """Strict-both-ways comparison against :data:`_KNOWN_GATE_DIVERGENCES`."""
    reason = _KNOWN_GATE_DIVERGENCES.get(name, {}).get(leg)
    if reason is not None:
        if rtl != gate:
            pytest.xfail(f"known gate-level divergence: {reason}")
        pytest.fail(
            f"\nfixture: {name}  leg: {leg}\n"
            f"Listed in _KNOWN_GATE_DIVERGENCES ({reason}), but the verdicts "
            f"now agree ({rtl}). Remove the row."
        )
    if rtl != gate:
        rows = [
            f"  {r:<9} rtl={rtl.get(r, 0):>3}  {leg}={gate.get(r, 0):>3}"
            for r in sorted(set(rtl) | set(gate))
            if rtl.get(r, 0) != gate.get(r, 0)
        ]
        pytest.fail(
            f"\nfixture: {name}  leg: {leg}\n"
            "committed RTL netlist vs gate-level per-rule disagreement:\n"
            + "\n".join(rows)
            + "\nIf this is a new analyzer gap, add a row to "
            "_KNOWN_GATE_DIVERGENCES with its root cause and file an issue."
        )


# --- The sweep (needs yosys) ------------------------------------------------

_CASES = [(fx.name, leg) for fx in _FIXTURES for leg in LEGS]


@pytest.mark.gate_level
@pytest.mark.skipif(not yosys_available(), reason="yosys not on PATH")
@pytest.mark.parametrize(("name", "leg"), _CASES, ids=[f"{n}-{g}" for n, g in _CASES])
def test_gate_level_verdict_matches_rtl(name: str, leg: str) -> None:
    fx = _BY_NAME[name]
    rebuilt = fired(build(fx, leg), fx.sdc)
    if leg == "rtl":
        # Control leg: no allowlist. A failure here means this Yosys's
        # front half does not reproduce the committed netlist's verdict,
        # which would make the gate legs' comparison meaningless.
        assert rebuilt == _committed_fired(name), (
            f"{name}: rebuilt RTL netlist disagrees with the committed one "
            f"({rebuilt} vs {_committed_fired(name)}); check "
            "_FRONT_HALF_CLEANUP or the Yosys version"
        )
        return
    _check(name, leg, _committed_fired(name), rebuilt)


# --- Committed gate-level netlists (no yosys needed) ------------------------

_COMMITTED_GATE_NETLISTS = sorted(
    p for leg in GATE_LEGS for p in FIXTURES.glob(f"*/*.{leg}.json")
)


@pytest.mark.parametrize(
    "path",
    _COMMITTED_GATE_NETLISTS,
    ids=[p.name for p in _COMMITTED_GATE_NETLISTS],
)
def test_committed_gate_netlist_matches_rtl(path: Path) -> None:
    name = path.parent.name
    leg = path.name.split(".")[-2]
    fx = _BY_NAME[name]
    _check(name, leg, _committed_fired(name), fired(path, fx.sdc))


def test_committed_gate_netlists_are_gate_level() -> None:
    """The committed gate netlists hold no coarse logic cells and,
    between them, cover the gate-level families the corpus lacked."""
    seen: Counter[str] = Counter()
    for path in _COMMITTED_GATE_NETLISTS:
        modules = json.loads(path.read_text())["modules"]
        for m in modules.values():
            if _attr_set(m, "blackbox"):
                continue
            for cell in m.get("cells", {}).values():
                t = cell["type"]
                assert t.startswith("$_") or t == "$scopeinfo", (
                    f"{path.name}: coarse cell {t}"
                )
                seen[t] += 1
    for family in (
        "$_DFF_P_",
        "$_DFF_PN0_",
        "$_DFF_NN0_",
        "$_DFFE_",
        "$_SDFF_",
        "$_DLATCH_",
        "$_MUX_",
    ):
        assert any(t.startswith(family) for t in seen), (family, sorted(seen))


def test_allowlist_rows_name_real_fixtures_and_legs() -> None:
    for name, legs in _KNOWN_GATE_DIVERGENCES.items():
        assert name in _BY_NAME, f"stale allowlist row: {name}"
        assert set(legs) <= set(GATE_LEGS), (name, legs)


def test_sweep_is_not_empty() -> None:
    assert len(_FIXTURES) > 140


def _regenerate(pairs: list[str]) -> None:
    """Rebuild committed gate netlists: ``<fixture>:<leg>`` per argument,
    or every existing ``*.gates.json`` / ``*.synth.json`` when empty."""
    if not pairs:
        pairs = [
            f"{p.parent.name}:{p.name.split('.')[-2]}" for p in _COMMITTED_GATE_NETLISTS
        ]
    for pair in pairs:
        name, leg = pair.split(":")
        fx = _BY_NAME[name]
        out = fx.netlist.with_name(f"{name}.{leg}.json").relative_to(REPO_ROOT)
        _run_yosys(yosys_script(fx, leg, out.as_posix())).check_returncode()
        print(f"wrote {out}")


if __name__ == "__main__":
    import sys

    _regenerate(sys.argv[1:])
