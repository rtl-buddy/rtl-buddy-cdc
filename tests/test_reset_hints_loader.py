"""Reset-hints YAML loader / validator tests (issue #129).

Strict-by-default: typos, unknown keys, malformed enum values all
fail with file:line context. Mirror of how the SDC parser handles
its tighter subset.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path

import pytest

from rtl_buddy_cdc.reset_hints import (
    PortHint,
    ResetHints,
    ResetHintsError,
    ResetHintsUnavailable,
    SCHEMA_VERSION,
    load,
)

PYYAML_INSTALLED = importlib.util.find_spec("yaml") is not None


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "hints.yaml"
    p.write_text(body)
    return p


# --- happy path -------------------------------------------------------------


@pytest.mark.skipif(not PYYAML_INSTALLED, reason="[hints] extra not installed")
def test_load_minimal_ports(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        """
        reset-hints:
          ports:
            - name: rst_n
              polarity: low
        """,
    )
    h = load(p)
    assert isinstance(h, ResetHints)
    assert h.schema_version == SCHEMA_VERSION
    assert h.ports == (PortHint(name="rst_n", polarity="low", type="async"),)
    assert h.synchronizers == ()


@pytest.mark.skipif(not PYYAML_INSTALLED, reason="[hints] extra not installed")
def test_load_with_sync_resets_and_synchronizers(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        """
        reset-hints:
          schema_version: "1.0"
          ports:
            - name: rst_n
              polarity: low
              type: async
            - name: srst_b
              polarity: high
              type: sync
              clock: clk_a
          synchronizers:
            - instance: top.u_rstgen.u_sync_q1
            - instance_glob: "top.u_*.u_rst_sync_q[12]"
              role: reset_synchronizer
        """,
    )
    h = load(p)
    assert len(h.ports) == 2
    assert h.ports[1].clock == "clk_a"
    assert len(h.synchronizers) == 2
    assert h.synchronizers[0].instance == "top.u_rstgen.u_sync_q1"
    assert h.synchronizers[0].instance_glob == ""
    assert h.synchronizers[1].instance_glob == "top.u_*.u_rst_sync_q[12]"


@pytest.mark.skipif(not PYYAML_INSTALLED, reason="[hints] extra not installed")
def test_port_polarity_overrides_helper(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        """
        reset-hints:
          ports:
            - { name: rst_n,   polarity: low }
            - { name: por_rst, polarity: high }
        """,
    )
    overrides = load(p).port_polarity_overrides()
    assert overrides == {"rst_n": "low", "por_rst": "high"}


# --- error cases ------------------------------------------------------------


@pytest.mark.skipif(not PYYAML_INSTALLED, reason="[hints] extra not installed")
def test_empty_file_errors(tmp_path: Path) -> None:
    p = _write(tmp_path, "")
    with pytest.raises(ResetHintsError, match="empty file"):
        load(p)


@pytest.mark.skipif(not PYYAML_INSTALLED, reason="[hints] extra not installed")
def test_top_level_not_mapping_errors(tmp_path: Path) -> None:
    p = _write(tmp_path, "- just-a-list\n- of-strings\n")
    with pytest.raises(ResetHintsError, match="top-level must be a mapping"):
        load(p)


@pytest.mark.skipif(not PYYAML_INSTALLED, reason="[hints] extra not installed")
def test_unknown_top_level_key_errors(tmp_path: Path) -> None:
    p = _write(tmp_path, "reset_hints: {}\n")  # underscore typo
    with pytest.raises(ResetHintsError, match="unknown top-level keys"):
        load(p)


@pytest.mark.skipif(not PYYAML_INSTALLED, reason="[hints] extra not installed")
def test_unknown_block_key_errors(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        """
        reset-hints:
          ports: []
          surprise: yes
        """,
    )
    with pytest.raises(ResetHintsError, match="unknown keys under 'reset-hints'"):
        load(p)


@pytest.mark.skipif(not PYYAML_INSTALLED, reason="[hints] extra not installed")
def test_missing_port_name_errors(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        """
        reset-hints:
          ports:
            - polarity: low
        """,
    )
    with pytest.raises(ResetHintsError, match="missing or non-string 'name'"):
        load(p)


@pytest.mark.skipif(not PYYAML_INSTALLED, reason="[hints] extra not installed")
def test_bad_polarity_errors(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        """
        reset-hints:
          ports:
            - { name: rst_n, polarity: lo }
        """,
    )
    with pytest.raises(ResetHintsError, match="polarity must be 'low' or 'high'"):
        load(p)


@pytest.mark.skipif(not PYYAML_INSTALLED, reason="[hints] extra not installed")
def test_bad_type_errors(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        """
        reset-hints:
          ports:
            - { name: rst_n, polarity: low, type: tri-state }
        """,
    )
    with pytest.raises(ResetHintsError, match="type must be 'sync' or 'async'"):
        load(p)


@pytest.mark.skipif(not PYYAML_INSTALLED, reason="[hints] extra not installed")
def test_unknown_port_key_errors(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        """
        reset-hints:
          ports:
            - { name: rst_n, polarity: low, level: low }
        """,
    )
    with pytest.raises(ResetHintsError, match="unknown keys"):
        load(p)


@pytest.mark.skipif(not PYYAML_INSTALLED, reason="[hints] extra not installed")
def test_synchronizer_requires_exactly_one_selector(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        """
        reset-hints:
          synchronizers:
            - { instance: top.u_a, instance_glob: "top.u_*" }
        """,
    )
    with pytest.raises(
        ResetHintsError, match="exactly one of 'instance' or 'instance_glob'"
    ):
        load(p)


@pytest.mark.skipif(not PYYAML_INSTALLED, reason="[hints] extra not installed")
def test_synchronizer_neither_selector_errors(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        """
        reset-hints:
          synchronizers:
            - { role: reset_synchronizer }
        """,
    )
    with pytest.raises(
        ResetHintsError, match="exactly one of 'instance' or 'instance_glob'"
    ):
        load(p)


@pytest.mark.skipif(not PYYAML_INSTALLED, reason="[hints] extra not installed")
def test_synchronizer_unknown_role_errors(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        """
        reset-hints:
          synchronizers:
            - { instance: top.u_a, role: reset_generator }
        """,
    )
    with pytest.raises(ResetHintsError, match="role must be 'reset_synchronizer'"):
        load(p)


@pytest.mark.skipif(not PYYAML_INSTALLED, reason="[hints] extra not installed")
def test_yaml_parse_error_surfaces_with_path(tmp_path: Path) -> None:
    p = _write(tmp_path, "reset-hints:\n  ports: [unclosed\n")
    with pytest.raises(ResetHintsError, match="YAML parse error"):
        load(p)


# --- [hints] extra missing path --------------------------------------------


def test_missing_pyyaml_raises_unavailable_with_install_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without PyYAML on the path the loader should raise
    :class:`ResetHintsUnavailable` with the install command, not a
    bare ``ImportError``. Mirrors the slang frontend's
    ``SlangFrontendUnavailable`` contract."""
    p = _write(tmp_path, "reset-hints:\n  ports: []\n")
    # Force the lazy ``import yaml`` to fail regardless of whether
    # PyYAML is on this test's python path.
    monkeypatch.setitem(sys.modules, "yaml", None)
    with pytest.raises(ResetHintsUnavailable) as exc:
        load(p)
    assert "pip install" in str(exc.value)
    assert "rtl-buddy-cdc[hints]" in str(exc.value)
