"""`lint --yosys-plugin` reads the RTL_BUDDY_SLANG_PLUGIN env var.

The plugin path is the machine-local yosys-slang `slang.so`; baking it
into a committed config forces the same on-disk location everywhere.
The flag therefore falls back to ``RTL_BUDDY_SLANG_PLUGIN`` (the .env
flow ``rtl_buddy`` populates), with the explicit flag winning.

These tests don't need yosys: ``elaborate`` is monkeypatched to capture
the resolved ``yosys_plugin`` kwarg, so they exercise the CLI wiring
(typer envvar -> parameter) in isolation.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

import rtl_buddy_cdc.cli as cli
from rtl_buddy_cdc.cli import app

runner = CliRunner()

FIX = Path(__file__).parent / "fixtures" / "ip_cdc_handshake"


class _Captured(Exception):
    """Bail out of the command once the kwarg is captured."""


def _patch_elaborate(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """Replace cli.elaborate with a capturing stub; return the box that
    receives the call kwargs."""
    box: dict[str, object] = {}

    def fake_elaborate(*_args: object, **kwargs: object):
        box.update(kwargs)
        raise _Captured

    monkeypatch.setattr(cli, "elaborate", fake_elaborate)
    return box


def _invoke(env: dict[str, str | None], extra_args: list[str]):
    return runner.invoke(
        app,
        [
            "lint",
            "--top",
            "ip_cdc_handshake",
            "--sdc",
            str(FIX / "ip_cdc_handshake.sdc"),
            *extra_args,
            str(FIX / "ip_cdc_sync.sv"),
            str(FIX / "ip_cdc_handshake.sv"),
        ],
        env=env,
    )


def test_env_var_supplies_plugin_when_flag_omitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    box = _patch_elaborate(monkeypatch)
    result = _invoke({"RTL_BUDDY_SLANG_PLUGIN": "/from/env/slang.so"}, [])
    # _Captured propagates as a non-zero exit; that's expected.
    assert isinstance(result.exception, _Captured), result.output
    assert box["yosys_plugin"] == "/from/env/slang.so"


def test_explicit_flag_wins_over_env_var(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    box = _patch_elaborate(monkeypatch)
    result = _invoke(
        {"RTL_BUDDY_SLANG_PLUGIN": "/from/env/slang.so"},
        ["--yosys-plugin", "/from/flag/slang.so"],
    )
    assert isinstance(result.exception, _Captured), result.output
    assert box["yosys_plugin"] == "/from/flag/slang.so"


def test_no_plugin_when_neither_set(monkeypatch: pytest.MonkeyPatch) -> None:
    box = _patch_elaborate(monkeypatch)
    # None clears any RTL_BUDDY_SLANG_PLUGIN already in the real env.
    result = _invoke({"RTL_BUDDY_SLANG_PLUGIN": None}, [])
    assert isinstance(result.exception, _Captured), result.output
    assert box["yosys_plugin"] is None
