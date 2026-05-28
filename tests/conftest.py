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
