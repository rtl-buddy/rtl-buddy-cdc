"""The slang fuzz cache is keyed by the frontend code (rtl-buddy-cdc#384).

The pickle cache under ``tests/fuzz/.cache`` used to be keyed by the case's
content hash and the pyslang version only, so after a frontend edit a local
``fuzz_diff`` run compared Yosys against the slang netlist of the *old*
frontend. Not a fuzz-marked test: it runs in the plain suite.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from . import slang_cache
from .templates.base import RenderedCase


def _case() -> RenderedCase:
    return RenderedCase(
        case_id="key_probe",
        template_name="key_probe",
        sv="module m; endmodule\n",
        sdc="",
        top="m",
        params={},
        expected=(),
    )


@pytest.fixture
def frontend_copy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the fingerprint at a scratch copy of one frontend source and
    the cache root at ``tmp_path``."""
    src = tmp_path / "slang.py"
    src.write_text("# frontend v1\n")
    monkeypatch.setattr(slang_cache, "_FRONTEND_SOURCES", (src,))
    monkeypatch.setattr(slang_cache, "CACHE_ROOT", tmp_path / "cache")
    monkeypatch.setattr(slang_cache, "slang_version", lambda: "10.0.0")
    slang_cache.frontend_fingerprint.cache_clear()
    yield src
    slang_cache.frontend_fingerprint.cache_clear()


def test_frontend_edit_changes_the_cache_path(frontend_copy: Path) -> None:
    case = _case()
    before = slang_cache._cache_path(case)
    frontend_copy.write_text("# frontend v2\n")
    slang_cache.frontend_fingerprint.cache_clear()
    after = slang_cache._cache_path(case)
    assert before != after
    # Same case, same bucket: only the suffix moves.
    assert before.parent == after.parent
    assert before.name.startswith(case.content_hash)


def test_unchanged_frontend_keeps_the_cache_path(frontend_copy: Path) -> None:
    case = _case()
    first = slang_cache._cache_path(case)
    slang_cache.frontend_fingerprint.cache_clear()
    assert slang_cache._cache_path(case) == first


def test_fingerprint_covers_the_real_frontend() -> None:
    """The default source list names the files a slang netlist depends on,
    and they exist (a renamed file would silently drop out of the key)."""
    names = {p.name for p in slang_cache._FRONTEND_SOURCES}
    assert {"slang.py", "netlist.py"} <= names
    assert all(p.is_file() for p in slang_cache._FRONTEND_SOURCES)
