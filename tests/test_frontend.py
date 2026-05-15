"""Frontend-abstraction tests.

These cover the factory + CLI plumbing introduced for issue #5. The
actual slang elaboration is still a stub, so the "slang works" case
isn't tested here — the focus is that:

- the factory dispatches on the enum,
- the slang path errors cleanly with an install hint when pyslang
  isn't installed,
- the yosys path still produces a Module (skipped if yosys isn't on
  PATH so the suite runs unattended).
"""

from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rtl_buddy_cdc import frontend as frontend_mod
from rtl_buddy_cdc.cli import app
from rtl_buddy_cdc.frontend import Frontend, elaborate, resolve_auto
from rtl_buddy_cdc.frontends.slang import SlangFrontendUnavailable

FIX_ROOT = Path(__file__).parent / "fixtures"
runner = CliRunner()

PYSLANG_INSTALLED = importlib.util.find_spec("pyslang") is not None


def test_frontend_enum_values() -> None:
    """The three frontend names are the stable public API the CLI uses;
    rename = breaking change."""
    assert {f.value for f in Frontend} == {"yosys", "slang", "auto"}


def test_elaborate_unknown_frontend_raises() -> None:
    """Calling :func:`elaborate` with a non-enum frontend value is a
    programmer error — surface it loudly rather than silently picking
    a default."""
    with pytest.raises(ValueError, match="unknown frontend"):
        elaborate([Path("x.sv")], "top", frontend="bogus")  # type: ignore[arg-type]


@pytest.mark.skipif(
    PYSLANG_INSTALLED,
    reason="pyslang is installed — install-hint path not exercised",
)
def test_slang_frontend_missing_pyslang_errors_cleanly() -> None:
    """Without pyslang on the path the slang frontend should raise
    :class:`SlangFrontendUnavailable` with an actionable install hint,
    not a bare ImportError."""
    with pytest.raises(SlangFrontendUnavailable) as exc:
        elaborate([Path("x.sv")], "top", frontend=Frontend.slang)
    assert "pip install" in str(exc.value)
    assert "rtl-buddy-cdc[slang]" in str(exc.value)


@pytest.mark.skipif(
    PYSLANG_INSTALLED,
    reason="pyslang is installed — install-hint path not exercised",
)
def test_cli_lint_slang_frontend_errors_cleanly_when_missing() -> None:
    """The CLI should translate :class:`SlangFrontendUnavailable` into
    exit 2 + an install hint, not a stack trace."""
    fix = FIX_ROOT / "bad_single_ff_sync"
    # mix_stderr=True (default) folds stderr into result.output, which
    # is what we want for substring matching across both streams.
    result = runner.invoke(
        app,
        [
            "lint",
            "--frontend",
            "slang",
            "--top",
            "bad_single_ff_sync",
            "--sdc",
            str(fix / "bad_single_ff_sync.sdc"),
            str(fix / "bad_single_ff_sync.sv"),
        ],
    )
    assert result.exit_code == 2, result.output
    assert "pip install" in result.output
    assert "rtl-buddy-cdc[slang]" in result.output


YOSYS = shutil.which("yosys")


@pytest.mark.skipif(YOSYS is None, reason="yosys not on PATH")
def test_yosys_frontend_factory_produces_module() -> None:
    """The factory dispatch for the yosys frontend should still
    elaborate a fixture end-to-end — same Module as before the
    refactor."""
    fix = FIX_ROOT / "bad_single_ff_sync"
    module = elaborate(
        [fix / "bad_single_ff_sync.sv"],
        "bad_single_ff_sync",
        frontend=Frontend.yosys,
    )
    assert module.name == "bad_single_ff_sync"
    # Two flops are inferred from the two always_ff blocks.
    ff_cells = [
        c for c in module.cells.values() if c.type.startswith("$") and "dff" in c.type
    ]
    assert len(ff_cells) >= 2


@pytest.mark.skipif(YOSYS is None, reason="yosys not on PATH")
def test_cli_lint_default_frontend_is_yosys() -> None:
    """Without ``--frontend``, ``lint`` should behave exactly as before
    (Yosys path); this is the back-compat guarantee from issue #5."""
    fix = FIX_ROOT / "bad_single_ff_sync"
    result = runner.invoke(
        app,
        [
            "lint",
            "--top",
            "bad_single_ff_sync",
            "--sdc",
            str(fix / "bad_single_ff_sync.sdc"),
            str(fix / "bad_single_ff_sync.sv"),
        ],
    )
    # Bad fixture trips CDC-001 → exit 1, same as the legacy lint test.
    assert result.exit_code == 1, result.output
    assert "CDC-001" in result.output
    # Frontend preamble appears in text-mode output.
    assert "frontend: yosys" in result.output


# --- Frontend.auto (issue #31) ----------------------------------------------
#
# ``auto`` resolves at runtime via ``importlib.util.find_spec``. The two
# tests below mock that probe to exercise both branches deterministically;
# the CLI-level test then confirms the preamble surfaces the resolved
# choice so downstream parsers don't see a third frontend name.


def test_resolve_auto_prefers_slang_when_pyslang_available(monkeypatch) -> None:
    """With pyslang importable, ``auto`` resolves to slang — no Yosys
    subprocess, no synth step."""
    monkeypatch.setattr(
        frontend_mod.importlib.util,
        "find_spec",
        lambda name: object() if name == "pyslang" else None,
    )
    assert resolve_auto() is Frontend.slang


def test_resolve_auto_falls_back_to_yosys_without_pyslang(monkeypatch) -> None:
    """Without pyslang importable, ``auto`` falls back to yosys so
    default installs (``typer`` only) still work."""
    monkeypatch.setattr(
        frontend_mod.importlib.util,
        "find_spec",
        lambda name: None,
    )
    assert resolve_auto() is Frontend.yosys


@pytest.mark.skipif(YOSYS is None, reason="yosys not on PATH")
def test_cli_lint_auto_frontend_resolves_in_preamble(monkeypatch) -> None:
    """``--frontend auto`` should echo the *resolved* frontend in the
    preamble (with an ``(auto)`` marker) so log scrapers see ``yosys``
    or ``slang``, not a third value."""
    # Force the resolution path to yosys regardless of whether pyslang
    # happens to be installed in the test env.
    monkeypatch.setattr(
        frontend_mod.importlib.util,
        "find_spec",
        lambda name: None,
    )
    fix = FIX_ROOT / "bad_single_ff_sync"
    result = runner.invoke(
        app,
        [
            "lint",
            "--frontend",
            "auto",
            "--top",
            "bad_single_ff_sync",
            "--sdc",
            str(fix / "bad_single_ff_sync.sdc"),
            str(fix / "bad_single_ff_sync.sv"),
        ],
    )
    assert result.exit_code == 1, result.output
    assert "frontend: yosys (auto)" in result.output
