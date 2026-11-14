"""Open-source integration corpus (rtl-buddy-cdc#331).

Every fixture elsewhere in ``tests/`` is a small design written to
trigger one rule. This module runs the analyzer on real multi-clock RTL
written by other people (async FIFOs, handshake bridges, a CDC
primitive library, a three-clock PTP transfer), fetched at pinned
upstream commits, and holds each to an expectation that was reviewed
finding by finding (``corpus.toml``).

Two groups of tests:

- **Manifest checks** (always run, no network): every design is
  pinned to a full SHA under a permissive licence, every fired rule is
  classified, every analyzer gap is referenced and described.
- **The corpus** (``@pytest.mark.integration``, opt-in): fetch, build
  with Yosys, ``analyze``; then ``lint --frontend slang`` on the same
  sources. Skipped unless selected (``pytest -m integration`` or
  ``RB_CDC_INTEGRATION=1``, see ``tests/conftest.py``), without
  ``yosys``, and without network access for a design not yet cached.

A count that changes fails the test. If the analyzer got better, update
the manifest (counts, the finding / missed entry, and the gap); if it
got worse, that is the regression this corpus exists to catch.
"""

from __future__ import annotations

import importlib.util
import re
from collections import Counter
from types import SimpleNamespace

import pytest

from rtl_buddy_cdc import sdc as sdc_mod

from . import runner

MANIFEST = runner.load()
DESIGNS = {d.name: d for d in MANIFEST.designs}
NAMES = list(DESIGNS)


# --- Manifest checks (no network) -------------------------------------------


def test_corpus_size() -> None:
    assert 4 <= len(NAMES) <= 8
    assert len(set(NAMES)) == len(NAMES)


@pytest.mark.parametrize("name", NAMES)
def test_design_is_pinned_and_permissive(name: str) -> None:
    d = DESIGNS[name]
    assert re.fullmatch(r"[0-9a-f]{40}", d.commit), "pin a full commit SHA"
    assert d.repo.startswith("https://") and d.repo.endswith(".git")
    assert d.license in runner.PERMISSIVE_LICENSES, (
        f"{d.license} is not on the permissive list (see corpus.toml)"
    )
    assert d.license_file
    assert d.sources
    for rel in (*d.sources, *d.incdirs, d.license_file):
        assert not rel.startswith("/") and ".." not in rel.split("/"), rel
    assert d.sdc.is_file()
    for local in d.local_sources:
        assert local.is_file(), local


@pytest.mark.parametrize("name", NAMES)
def test_sdc_declares_the_expected_clocks(name: str) -> None:
    d = DESIGNS[name]
    spec = sdc_mod.parse_file(d.sdc, backend="tokenizer")
    assert sorted(spec.clocks) == sorted(d.clocks)
    assert not spec.partial_warnings


@pytest.mark.parametrize("name", NAMES)
def test_every_finding_is_reviewed(name: str) -> None:
    """The reviewed findings add up to the pinned per-rule counts, and
    every analyzer-wrong verdict names its gap."""
    d = DESIGNS[name]
    reviewed: Counter[str] = Counter()
    for f in d.findings:
        assert f.classification in runner.CLASSIFICATIONS, f
        assert f.count > 0 and f.reason.strip()
        if f.classification == "false_positive":
            assert f.gap, f"{name} {f.rule}: a false positive names its gap"
        if f.gap:
            assert f.gap in MANIFEST.gaps, f.gap
            assert MANIFEST.gaps[f.gap].kind == "false_positive"
            assert f.rule in MANIFEST.gaps[f.gap].rules
        reviewed[f.rule] += f.count
    assert dict(reviewed) == d.rules
    for m in d.missed:
        assert m.count > 0 and m.reason.strip()
        assert MANIFEST.gaps[m.gap].kind == "false_negative"
        assert m.rule in MANIFEST.gaps[m.gap].rules


@pytest.mark.parametrize("name", NAMES)
def test_slang_expectation_is_consistent(name: str) -> None:
    s = DESIGNS[name].slang
    if s.matches_yosys:
        assert s.rules is None and s.crossings is None and not s.gap
        assert s.reads
        return
    assert s.gap in MANIFEST.gaps, "a slang divergence names its gap"
    if s.reads:
        assert s.rules is not None and s.crossings is not None


def test_every_gap_is_referenced_and_described() -> None:
    used = {f.gap for d in DESIGNS.values() for f in d.findings if f.gap}
    used |= {m.gap for d in DESIGNS.values() for m in d.missed}
    used |= {d.slang.gap for d in DESIGNS.values() if d.slang.gap}
    assert used == set(MANIFEST.gaps)
    for gap in MANIFEST.gaps.values():
        assert gap.kind in runner.GAP_KINDS, gap.name
        assert gap.summary and gap.reproducer, gap.name


@pytest.mark.parametrize(
    ("expr", "env", "selected"),
    [
        ("", "", False),
        ("integration", "", True),
        ("fuzz or integration", "", True),
        ("not integration", "", False),
        ("", "1", True),
        ("", "0", False),
    ],
)
def test_integration_opt_in(
    monkeypatch: pytest.MonkeyPatch, expr: str, env: str, selected: bool
) -> None:
    from conftest import INTEGRATION_ENV, integration_selected

    monkeypatch.setenv(INTEGRATION_ENV, env)
    config = SimpleNamespace(getoption=lambda name, default=None: expr)
    assert integration_selected(config) is selected


# --- The corpus (opt-in, needs yosys and the network) -----------------------


def _checkout(name: str):
    d = DESIGNS[name]
    if not runner.is_cached(d) and not runner.network_available():
        pytest.skip(f"{name}: not cached and no network access")
    return d, runner.fetch(d)


@pytest.mark.integration
@pytest.mark.skipif(not runner.yosys_available(), reason="yosys not on PATH")
@pytest.mark.parametrize("name", NAMES)
def test_yosys_verdict(name: str) -> None:
    d, src = _checkout(name)
    netlist = runner.build(d, src)
    cells = runner.cell_count(netlist, d.top)
    assert cells <= runner.MAX_CELLS, f"{name}: {cells} cells, over the cap"

    res = runner.analyze(d, netlist)
    assert res.exit_code in (0, 1), f"{name}: analyze exit {res.exit_code}"
    got = (res.crossings, res.rules)
    want = (d.crossings, d.rules)
    assert got == want, (
        f"{name}: (crossings, per-rule counts) {got} != pinned {want}. "
        "Review the new findings and update corpus.toml."
    )
    # Structural sanity: every declared clock clocks something, and no
    # flop is left without a domain unless the manifest says why.
    unreached = set(d.clocks) - res.flop_clocks
    assert not unreached, f"{name}: declared clocks reach no flop: {unreached}"
    assert res.domain_unknown == d.domain_unknown, res.report["domain_unknown_flops"]


@pytest.mark.integration
@pytest.mark.skipif(
    importlib.util.find_spec("pyslang") is None, reason="pyslang not installed"
)
@pytest.mark.parametrize("name", NAMES)
def test_slang_verdict(name: str) -> None:
    d, src = _checkout(name)
    res = runner.lint_slang(d, src)
    if not d.slang.reads:
        assert res.exit_code == 2, f"{name}: slang now elaborates; update corpus.toml"
        return
    assert res.exit_code in (0, 1), f"{name}: slang lint exit {res.exit_code}"
    got = (res.crossings, res.rules)
    yosys = (d.crossings, d.rules)
    if d.slang.matches_yosys:
        assert got == yosys, f"{name}: slang {got} != Yosys {yosys}"
        return
    # Strict both ways: a pinned divergence that disappears fails too.
    assert got != yosys, (
        f"{name}: slang now agrees with Yosys; set matches_yosys = true and "
        f"drop the slang gap ({d.slang.gap})"
    )
    assert got == (d.slang.crossings, d.slang.rules), (
        f"{name}: slang {got} != pinned slang verdict "
        f"{(d.slang.crossings, d.slang.rules)}"
    )
