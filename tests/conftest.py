"""Top-level pytest configuration shared by hand-authored and fuzz tests."""

from __future__ import annotations

import pytest


def pytest_configure(config) -> None:
    config.addinivalue_line(
        "markers",
        "fuzz: template-driven fuzz corpus (gated; run via `pytest -m fuzz`)",
    )
    config.addinivalue_line(
        "markers",
        "fuzz_diff: cross-frontend (Yosys vs slang) differential oracle "
        "(gated; run via `pytest -m fuzz_diff`; ~5–10× slower than `fuzz`)",
    )
    config.addinivalue_line(
        "markers",
        "fuzz_grammar: Stage-4 grammar-generated topologies "
        "(gated; run via `pytest -m fuzz_grammar`; needs yosys on PATH)",
    )
    config.addinivalue_line(
        "markers",
        "sim: behavioural simulation oracle (opt-in; needs iverilog)",
    )
    config.addinivalue_line(
        "markers",
        "yosys_slang: end-to-end yosys-slang plugin (read_slang) oracle "
        "(gated; needs yosys on PATH + RTL_BUDDY_SLANG_PLUGIN pointing at "
        "a built slang.so; run via `pytest -m yosys_slang`)",
    )


@pytest.fixture(params=["tcl", "tokenizer"])
def sdc_backend(request, monkeypatch):
    """Run the requesting test once per SDC reader backend (#298).

    The parameter is pushed through ``RB_CDC_SDC_BACKEND`` rather than
    a ``backend=`` argument so a test module can opt its whole surface
    in with a one-line autouse fixture, without touching any of its
    ``parse()`` call sites. ``"tcl"`` skips on an interpreter without
    ``_tkinter`` (Homebrew python without ``python-tk``, EL8 system
    python) — the backend simply does not exist there.

    ``sdc.tcl_available()`` is a *subprocess probe*, not an import:
    the Tcl interp runs out of process and this one must never load
    ``_tkinter`` (rtl-buddy-cdc#298 — it wedges macOS fork+exec).
    """
    from rtl_buddy_cdc import sdc as sdc_mod

    if request.param == "tcl" and not sdc_mod.tcl_available():
        pytest.skip("_tkinter is not importable; the Tcl SDC backend is unavailable")
    monkeypatch.setenv(sdc_mod.BACKEND_ENV_VAR, request.param)
    return request.param
