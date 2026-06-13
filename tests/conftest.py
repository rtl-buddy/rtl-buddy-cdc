"""Top-level pytest configuration shared by hand-authored and fuzz tests."""

from __future__ import annotations


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
