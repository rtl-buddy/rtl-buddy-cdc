"""pyslang version guard + namespace shim (issue #300).

Before this, an out-of-range pyslang (11.x at the time) died deep
inside :func:`rtl_buddy_cdc.frontends.slang.elaborate` with
``AttributeError: module 'pyslang' has no attribute
'CompilationOptions'`` — pyslang 11 moved the compilation types into
``pyslang.ast`` and ``SyntaxTree`` into ``pyslang.syntax``. Two things
land here:

- :func:`~rtl_buddy_cdc.frontends.slang._pyslang_namespaces` bridges the
  flat (10.x) and submodule (11.x) layouts, and
- :func:`~rtl_buddy_cdc.frontends.slang._check_pyslang_version` rejects
  a major outside :data:`PYSLANG_SUPPORTED_RANGE` with a one-line
  message before any elaboration work starts.

Every test here injects a **fake** ``pyslang`` module into
``sys.modules``, so the file runs identically with or without a real
pyslang installed (the no-slang CI job included).
"""

from __future__ import annotations

import logging
import sys
import tomllib
import types
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rtl_buddy_cdc.cli import app
from rtl_buddy_cdc.frontends import slang as slang_fe
from rtl_buddy_cdc.frontends.slang import (
    PYSLANG_MAX_MAJOR_EXCLUSIVE,
    PYSLANG_MIN_MAJOR,
    PYSLANG_SUPPORTED_RANGE,
    SlangFrontendUnavailable,
)

FIX_ROOT = Path(__file__).parent / "fixtures"
PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"
runner = CliRunner()


def _fake_pyslang(version: object | None) -> types.ModuleType:
    """A stand-in ``pyslang`` module carrying only ``__version__``.

    Nothing else is needed: the guard runs before any pyslang API is
    touched, so a module that *passes* the guard fails later with an
    ``AttributeError`` — which is what the "accepted" tests assert."""
    mod = types.ModuleType("pyslang")
    if version is not None:
        mod.__version__ = version  # type: ignore[attr-defined]
    return mod


@pytest.fixture
def fake_pyslang(monkeypatch):
    """Install a fake ``pyslang`` for the duration of one test."""

    def _install(version: object | None) -> types.ModuleType:
        mod = _fake_pyslang(version)
        monkeypatch.setitem(sys.modules, "pyslang", mod)
        return mod

    return _install


# --- the range lives in exactly one place ----------------------------------


def test_supported_range_matches_pyproject() -> None:
    """``PYSLANG_SUPPORTED_RANGE`` is the code-side source of truth; the
    ``[slang]`` extra must declare the identical range or an install can
    satisfy the metadata and still be rejected at runtime (or worse, the
    other way round)."""
    data = tomllib.loads(PYPROJECT.read_text())
    extra = data["project"]["optional-dependencies"]["slang"]
    assert extra == [f"pyslang{PYSLANG_SUPPORTED_RANGE}"], (
        f"pyproject [slang] extra {extra!r} disagrees with "
        f"PYSLANG_SUPPORTED_RANGE {PYSLANG_SUPPORTED_RANGE!r}"
    )


def test_range_constants_are_coherent() -> None:
    assert PYSLANG_MIN_MAJOR < PYSLANG_MAX_MAJOR_EXCLUSIVE
    assert PYSLANG_SUPPORTED_RANGE == (
        f">={PYSLANG_MIN_MAJOR},<{PYSLANG_MAX_MAJOR_EXCLUSIVE}"
    )


# --- out-of-range majors are rejected early --------------------------------


@pytest.mark.parametrize("version", ["9.0.0", "7.1.2", "0.9"])
def test_below_range_rejected(fake_pyslang, version: str) -> None:
    fake_pyslang(version)
    with pytest.raises(SlangFrontendUnavailable) as exc:
        slang_fe._import_pyslang()
    msg = str(exc.value)
    assert f"pyslang {version} is not supported" in msg
    assert PYSLANG_SUPPORTED_RANGE in msg
    assert "--frontend yosys" in msg


@pytest.mark.parametrize("version", ["12.0.0", "13.2.1", "99.0.0"])
def test_above_range_rejected(fake_pyslang, version: str) -> None:
    fake_pyslang(version)
    with pytest.raises(SlangFrontendUnavailable) as exc:
        slang_fe._import_pyslang()
    msg = str(exc.value)
    assert f"pyslang {version} is not supported" in msg
    assert PYSLANG_SUPPORTED_RANGE in msg


def test_rejection_message_is_one_line(fake_pyslang) -> None:
    """``cli.py`` renders this as ``error: {e}`` — a multi-line message
    would bury the actionable half."""
    fake_pyslang("12.0.0")
    with pytest.raises(SlangFrontendUnavailable) as exc:
        slang_fe._import_pyslang()
    assert "\n" not in str(exc.value)


def test_elaborate_fails_before_touching_pyslang(fake_pyslang) -> None:
    """The guard runs in ``_import_pyslang``, i.e. before any compilation
    object is constructed — the whole point of #300."""
    fake_pyslang("12.0.0")
    with pytest.raises(SlangFrontendUnavailable):
        slang_fe.elaborate([Path("nonexistent.sv")], "top")


# --- in-range majors pass the guard ----------------------------------------


@pytest.mark.parametrize(
    "version", ["10.0.0", "10.5.2", "11.0.0", "11.3.0.dev1", " 11.0.0 "]
)
def test_supported_versions_accepted(fake_pyslang, version: str) -> None:
    """A supported major returns the module; the fake has no pyslang API
    so the *next* step is what fails, not the guard."""
    fake_pyslang(version)
    assert slang_fe._import_pyslang() is sys.modules["pyslang"]


# --- unreadable versions warn and proceed ----------------------------------


@pytest.mark.parametrize("version", ["not-a-version", "", "   ", "v11"])
def test_unparseable_version_warns_and_proceeds(
    fake_pyslang, monkeypatch, caplog, version: str
) -> None:
    """Documented policy: a version string we cannot read is a warning,
    never a hard failure. The guard exists to replace a confusing
    traceback with a clear message, not to break a working install over
    a cosmetic version string."""
    fake_pyslang(version)
    # An empty / whitespace ``__version__`` falls through to the
    # distribution metadata; make that unavailable too so the fake is
    # the only source considered.
    monkeypatch.setattr(
        slang_fe.importlib.metadata,
        "version",
        lambda name: (_ for _ in ()).throw(RuntimeError("no metadata")),
    )
    with caplog.at_level(logging.WARNING, logger=slang_fe.__name__):
        assert slang_fe._import_pyslang() is sys.modules["pyslang"]
    messages = [r.getMessage() for r in caplog.records]
    assert any("pyslang version" in m for m in messages), messages
    assert any(PYSLANG_SUPPORTED_RANGE in m for m in messages), messages


def test_missing_version_falls_back_to_distribution_metadata(
    fake_pyslang, monkeypatch
) -> None:
    """pyslang 10.0.0 ships **no** ``__version__`` attribute, so the
    guard reads the installed distribution metadata instead. A fake 12
    there is still rejected."""
    fake_pyslang(None)
    monkeypatch.setattr(slang_fe.importlib.metadata, "version", lambda name: "12.0.0")
    with pytest.raises(SlangFrontendUnavailable, match="pyslang 12.0.0"):
        slang_fe._import_pyslang()


def test_non_string_version_falls_back_to_metadata(fake_pyslang, monkeypatch) -> None:
    fake_pyslang((11, 0, 0))
    monkeypatch.setattr(slang_fe.importlib.metadata, "version", lambda name: "9.0.0")
    with pytest.raises(SlangFrontendUnavailable, match="pyslang 9.0.0"):
        slang_fe._import_pyslang()


def test_undeterminable_version_warns_and_proceeds(
    fake_pyslang, monkeypatch, caplog
) -> None:
    fake_pyslang(None)
    monkeypatch.setattr(
        slang_fe.importlib.metadata,
        "version",
        lambda name: (_ for _ in ()).throw(RuntimeError("no metadata")),
    )
    with caplog.at_level(logging.WARNING, logger=slang_fe.__name__):
        assert slang_fe._import_pyslang() is sys.modules["pyslang"]
    assert any("could not determine" in r.getMessage() for r in caplog.records)


# --- CLI rendering ---------------------------------------------------------


def test_cli_lint_reports_unsupported_pyslang(fake_pyslang) -> None:
    """``lint --frontend slang`` exits 2 with the one-line message, not a
    traceback."""
    fake_pyslang("12.0.0")
    fix = FIX_ROOT / "bad_single_ff_sync"
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
    assert "pyslang 12.0.0 is not supported" in result.output
    assert PYSLANG_SUPPORTED_RANGE in result.output


# --- the 10.x / 11.x namespace shim ----------------------------------------


def test_namespaces_flat_layout_falls_back_to_the_module() -> None:
    """pyslang 10.x has no ``ast`` / ``syntax`` submodules — everything
    is top-level, so both lookups resolve to the module itself."""
    mod = _fake_pyslang("10.0.0")
    assert slang_fe._pyslang_namespaces(mod) == (mod, mod)


def test_namespaces_submodule_layout_is_preferred() -> None:
    """pyslang 11.x moved ``Compilation`` / ``CompilationOptions`` /
    ``CompilationFlags`` to ``pyslang.ast`` and ``SyntaxTree`` to
    ``pyslang.syntax``."""
    mod = _fake_pyslang("11.0.0")
    ast_ns = types.SimpleNamespace(Compilation=object())
    syntax_ns = types.SimpleNamespace(SyntaxTree=object())
    mod.ast = ast_ns  # type: ignore[attr-defined]
    mod.syntax = syntax_ns  # type: ignore[attr-defined]
    assert slang_fe._pyslang_namespaces(mod) == (ast_ns, syntax_ns)
