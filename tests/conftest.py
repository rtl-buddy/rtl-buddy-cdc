"""Top-level pytest configuration shared by hand-authored and fuzz tests."""

from __future__ import annotations


def pytest_configure(config) -> None:
    config.addinivalue_line(
        "markers",
        "fuzz: template-driven fuzz corpus (gated; run via `pytest -m fuzz`)",
    )
    config.addinivalue_line(
        "markers",
        "sim: behavioural simulation oracle (opt-in; needs iverilog)",
    )
