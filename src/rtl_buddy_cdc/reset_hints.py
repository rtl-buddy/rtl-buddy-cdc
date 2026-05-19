"""External YAML hints for reset analysis (issue #129).

Parallel to the ``(* reset_polarity *)`` / ``(* reset_sync *)`` SV
attributes — same vocabulary, external file when the user can't
touch RTL (vendor IP, generated wrappers, multi-block boards).
Opt-in via the ``[hints]`` install extra; default installs stay
``typer``-only.

Schema reference:
``wiki/raw/articles/rtl-buddy-cdc-reset-hints-schema.md``.

Precedence: when an SV attribute and a YAML hint disagree on the
same port's polarity, the hint wins. The hints file is the
explicit override; the attribute is the in-RTL default. Same for
synchroniser membership (union with no precedence question — both
sides flag the same cell as a sync stage, the structural pass and
the rule pack don't care who flagged it).
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from rtl_buddy_cdc.netlist import Module

SCHEMA_VERSION = "1.0"

ResetPolarity = Literal["high", "low"]
ResetType = Literal["sync", "async"]


class ResetHintsUnavailable(Exception):
    """Raised when ``--reset-hints`` is requested without the [hints] extra.

    The extra (``pip install 'rtl-buddy-cdc[hints]'``) pulls in
    PyYAML, which is otherwise not a runtime dep. Mirrors
    :class:`rtl_buddy_cdc.frontends.slang.SlangFrontendUnavailable`
    for the slang frontend's pyslang dependency.
    """


class ResetHintsError(Exception):
    """Raised on a malformed hints file. Carries the source file path."""


@dataclass(frozen=True)
class PortHint:
    """Declared reset port polarity / type / sampling clock.

    Maps onto the same data model the SV attribute path produces:
    ``polarity`` aligns with ``ResetSource.polarity`` and the
    ``(* reset_polarity *)`` attribute; ``type`` and ``clock`` are
    schema-future-proofing (only ``polarity`` is consumed by the
    rule pack today).
    """

    name: str
    polarity: ResetPolarity
    type: ResetType = "async"
    clock: str | None = None


@dataclass(frozen=True)
class SynchronizerHint:
    """Mark a flop cell (or set of cells) as a vetted sync stage.

    Exactly one of ``instance`` or ``instance_glob`` is non-empty.
    ``role`` is a tag for future expansion (``reset_generator`` etc.);
    only ``reset_synchronizer`` is consumed today.
    """

    instance: str = ""
    instance_glob: str = ""
    role: str = "reset_synchronizer"


@dataclass(frozen=True)
class ResetHints:
    """Container for a parsed hints file.

    Frozen and pure — same convention as the rest of the analyzer's
    data model. Construct via :func:`load`.
    """

    schema_version: str
    ports: tuple[PortHint, ...] = field(default_factory=tuple)
    synchronizers: tuple[SynchronizerHint, ...] = field(default_factory=tuple)

    def port_polarity_overrides(self) -> dict[str, ResetPolarity]:
        """Map ``port_name -> polarity`` for every port hint.

        Output shape matches
        :func:`rtl_buddy_cdc.rules.user_reset_polarity_overrides`,
        so the union step in the rule pack is a dict-update.
        """
        return {p.name: p.polarity for p in self.ports}

    def synchronizer_cell_names(self, module: Module) -> set[str]:
        """Resolve every synchroniser hint against the module's cells.

        Each cell's hierarchical instance path (``<top>.<parents>.<leaf>``,
        per :func:`rtl_buddy_cdc.domain_map._hier_path`) is matched
        against the hint's ``instance`` (exact) or ``instance_glob``
        (shell glob via :mod:`fnmatch`). Cells whose resolved path
        matches any hint are returned as a set of *raw cell names* —
        the same shape :func:`user_reset_sync_flop_names` returns,
        so the union step downstream is a set-union.
        """
        if not self.synchronizers:
            return set()
        # Lazy import: ``domain_map`` pulls in ``reporter``, which
        # pulls in ``rules`` for ``Violation``, which pulls in this
        # module. Lazy keeps the cycle off the import graph.
        from rtl_buddy_cdc.domain_map import _hier_path

        out: set[str] = set()
        for cell_name in module.cells:
            hier = _hier_path(module, cell_name)
            for s in self.synchronizers:
                if s.instance and s.instance == hier:
                    out.add(cell_name)
                    break
                if s.instance_glob and fnmatch.fnmatchcase(hier, s.instance_glob):
                    out.add(cell_name)
                    break
        return out


# --- loader -----------------------------------------------------------------


_ALLOWED_TOP_KEYS = {"reset-hints"}
_ALLOWED_BLOCK_KEYS = {"schema_version", "ports", "synchronizers"}
_ALLOWED_PORT_KEYS = {"name", "polarity", "type", "clock"}
_ALLOWED_SYNC_KEYS = {"instance", "instance_glob", "role"}
_VALID_POLARITIES: frozenset[ResetPolarity] = frozenset({"high", "low"})
_VALID_TYPES: frozenset[ResetType] = frozenset({"sync", "async"})


def load(path: Path) -> ResetHints:
    """Parse a YAML hints file into a :class:`ResetHints`.

    Raises :class:`ResetHintsUnavailable` if the ``[hints]`` extra
    isn't installed (PyYAML missing). Raises :class:`ResetHintsError`
    on any schema violation — unknown keys, wrong types, malformed
    enum values, missing required fields — each with the file path
    in the message. Strict by default: typos and unknown keys fail
    rather than silently no-op.
    """
    try:
        import yaml
    except ImportError as e:
        raise ResetHintsUnavailable(
            "--reset-hints requires the [hints] optional extra. "
            "Install with: pip install 'rtl-buddy-cdc[hints]'"
        ) from e

    try:
        raw = yaml.safe_load(path.read_text())
    except yaml.YAMLError as e:
        raise ResetHintsError(f"{path}: YAML parse error: {e}") from e
    return _parse(raw, path)


def _parse(raw: Any, path: Path) -> ResetHints:
    if raw is None:
        raise ResetHintsError(f"{path}: empty file")
    if not isinstance(raw, dict):
        raise ResetHintsError(
            f"{path}: top-level must be a mapping, got {type(raw).__name__}"
        )
    extra = set(raw.keys()) - _ALLOWED_TOP_KEYS
    if extra:
        raise ResetHintsError(
            f"{path}: unknown top-level keys: {sorted(extra)} (allowed: 'reset-hints')"
        )
    if "reset-hints" not in raw:
        raise ResetHintsError(f"{path}: missing 'reset-hints' top-level key")

    block = raw["reset-hints"]
    if not isinstance(block, dict):
        raise ResetHintsError(
            f"{path}: 'reset-hints' must be a mapping, got {type(block).__name__}"
        )
    extra = set(block.keys()) - _ALLOWED_BLOCK_KEYS
    if extra:
        raise ResetHintsError(
            f"{path}: unknown keys under 'reset-hints': {sorted(extra)} "
            f"(allowed: {sorted(_ALLOWED_BLOCK_KEYS)})"
        )

    schema_version = block.get("schema_version", SCHEMA_VERSION)
    if not isinstance(schema_version, str):
        raise ResetHintsError(
            f"{path}: schema_version must be a string, got "
            f"{type(schema_version).__name__}"
        )

    ports = _parse_ports(block.get("ports", []), path)
    syncs = _parse_synchronizers(block.get("synchronizers", []), path)
    return ResetHints(schema_version=schema_version, ports=ports, synchronizers=syncs)


def _parse_ports(raw: Any, path: Path) -> tuple[PortHint, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ResetHintsError(
            f"{path}: 'ports' must be a list, got {type(raw).__name__}"
        )
    out: list[PortHint] = []
    for idx, item in enumerate(raw):
        ctx = f"{path}: ports[{idx}]"
        if not isinstance(item, dict):
            raise ResetHintsError(f"{ctx}: expected mapping, got {type(item).__name__}")
        extra = set(item.keys()) - _ALLOWED_PORT_KEYS
        if extra:
            raise ResetHintsError(
                f"{ctx}: unknown keys: {sorted(extra)} "
                f"(allowed: {sorted(_ALLOWED_PORT_KEYS)})"
            )
        name = item.get("name")
        if not isinstance(name, str) or not name:
            raise ResetHintsError(f"{ctx}: missing or non-string 'name'")
        polarity = item.get("polarity")
        if polarity not in _VALID_POLARITIES:
            raise ResetHintsError(
                f"{ctx}: polarity must be 'low' or 'high', got {polarity!r}"
            )
        type_ = item.get("type", "async")
        if type_ not in _VALID_TYPES:
            raise ResetHintsError(
                f"{ctx}: type must be 'sync' or 'async', got {type_!r}"
            )
        clock = item.get("clock")
        if clock is not None and not isinstance(clock, str):
            raise ResetHintsError(
                f"{ctx}: clock must be a string when set, got {type(clock).__name__}"
            )
        out.append(PortHint(name=name, polarity=polarity, type=type_, clock=clock))
    return tuple(out)


def _parse_synchronizers(raw: Any, path: Path) -> tuple[SynchronizerHint, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ResetHintsError(
            f"{path}: 'synchronizers' must be a list, got {type(raw).__name__}"
        )
    out: list[SynchronizerHint] = []
    for idx, item in enumerate(raw):
        ctx = f"{path}: synchronizers[{idx}]"
        if not isinstance(item, dict):
            raise ResetHintsError(f"{ctx}: expected mapping, got {type(item).__name__}")
        extra = set(item.keys()) - _ALLOWED_SYNC_KEYS
        if extra:
            raise ResetHintsError(
                f"{ctx}: unknown keys: {sorted(extra)} "
                f"(allowed: {sorted(_ALLOWED_SYNC_KEYS)})"
            )
        instance = item.get("instance", "")
        instance_glob = item.get("instance_glob", "")
        if not isinstance(instance, str) or not isinstance(instance_glob, str):
            raise ResetHintsError(
                f"{ctx}: 'instance' / 'instance_glob' must be strings when set"
            )
        if bool(instance) == bool(instance_glob):
            raise ResetHintsError(
                f"{ctx}: exactly one of 'instance' or 'instance_glob' is required"
            )
        role = item.get("role", "reset_synchronizer")
        if not isinstance(role, str) or not role:
            raise ResetHintsError(f"{ctx}: 'role' must be a non-empty string")
        if role != "reset_synchronizer":
            # v1 only consumes ``reset_synchronizer``; future roles
            # are reserved for backward-compatible additions. Loud
            # failure on the way in so a typo doesn't get silently
            # ignored.
            raise ResetHintsError(
                f"{ctx}: role must be 'reset_synchronizer' (other roles "
                f"reserved for v1.x); got {role!r}"
            )
        out.append(
            SynchronizerHint(instance=instance, instance_glob=instance_glob, role=role)
        )
    return tuple(out)
