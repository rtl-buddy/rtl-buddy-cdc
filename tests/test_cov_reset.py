"""Coverage-raising tests for the reset analysis modules.

Three modules under one roof, each exercised on a path the existing
suite leaves uncovered:

* :mod:`rtl_buddy_cdc.reset_hints` — the strict YAML loader's error
  and edge branches (wrong container types at each nesting level,
  ``None``-valued list slots, bad ``clock`` / ``role`` types) plus the
  ``synchronizer_cell_names`` resolver's exact-match and glob-match
  arms against a hand-built :class:`Module`.
* :mod:`rtl_buddy_cdc.reset_domain` — the defensive arms of the
  per-flop classifier (constant-driven reset, untracked-driver reset,
  reset-bearing cell missing its pin), the ``_polarity_from_param``
  empty-string default, the ``iter_reset_sync_chains`` walk on the
  ``bad_reset_sync_deassert_polarity`` fixture, and the
  ``find_reset_crossings`` reset-less skip.
* :mod:`rtl_buddy_cdc.reset_domain_map` — the serializer's
  reset-less / unknown-cell skips and the ``_reset_source_location``
  port/constant fallbacks (including a netname ``src`` attr that the
  reporter regex can't parse).

YAML-dependent loader tests gate per-test on ``find_spec("yaml")``
the same way :mod:`tests.test_reset_hints_loader` does, so the
no-extras CI jobs stay green while the coverage job (which has the
``[hints]`` extra) runs them.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from rtl_buddy_cdc import netlist
from rtl_buddy_cdc.netlist import Cell, Module, Netname, Port
from rtl_buddy_cdc.reset_domain import (
    ResetSource,
    _classify_reset_source,
    _polarity_from_param,
    assign_reset_domains,
    find_reset_crossings,
    iter_reset_sync_chains,
)
from rtl_buddy_cdc.reset_domain_map import (
    _parse_src_attr,
    _reset_source_location,
    build_reset_domain_map,
)
from rtl_buddy_cdc.reset_hints import (
    PortHint,
    ResetHints,
    ResetHintsError,
    SynchronizerHint,
    load,
)

PYYAML_INSTALLED = importlib.util.find_spec("yaml") is not None

FIX_ROOT = Path(__file__).parent / "fixtures"


def _write(tmp_path: Path, body: str) -> Path:
    """Write a hints YAML body to a temp file and return its path."""
    p = tmp_path / "hints.yaml"
    p.write_text(body)
    return p


# === reset_hints: loader error / edge branches =============================


@pytest.mark.skipif(not PYYAML_INSTALLED, reason="[hints] extra not installed")
def test_missing_reset_hints_top_key_errors(tmp_path: Path) -> None:
    """A mapping whose only key is in the allow-set but is not
    ``reset-hints`` itself fails the "missing top-level key" guard.

    ``_ALLOWED_TOP_KEYS`` only contains ``reset-hints``, so the
    sole way to pass the unknown-key check yet still miss the key is
    an empty mapping ``{}``."""
    p = _write(tmp_path, "{}\n")
    with pytest.raises(ResetHintsError, match="missing 'reset-hints' top-level key"):
        load(p)


@pytest.mark.skipif(not PYYAML_INSTALLED, reason="[hints] extra not installed")
def test_reset_hints_block_not_mapping_errors(tmp_path: Path) -> None:
    """``reset-hints:`` whose value is a scalar (not a mapping) is
    rejected with the block-type message."""
    p = _write(tmp_path, "reset-hints: just-a-string\n")
    with pytest.raises(ResetHintsError, match="'reset-hints' must be a mapping"):
        load(p)


@pytest.mark.skipif(not PYYAML_INSTALLED, reason="[hints] extra not installed")
def test_schema_version_non_string_errors(tmp_path: Path) -> None:
    """A numeric ``schema_version`` (YAML parses ``1.0`` as a float)
    must be rejected — the field is documented as a string."""
    p = _write(
        tmp_path,
        """
        reset-hints:
          schema_version: 1.0
          ports: []
        """,
    )
    with pytest.raises(ResetHintsError, match="schema_version must be a string"):
        load(p)


@pytest.mark.skipif(not PYYAML_INSTALLED, reason="[hints] extra not installed")
def test_ports_null_is_empty(tmp_path: Path) -> None:
    """An explicit null ``ports:`` collapses to the empty tuple rather
    than erroring — same as omitting the key."""
    p = _write(
        tmp_path,
        """
        reset-hints:
          ports:
          synchronizers:
        """,
    )
    hints = load(p)
    assert hints.ports == ()
    assert hints.synchronizers == ()


@pytest.mark.skipif(not PYYAML_INSTALLED, reason="[hints] extra not installed")
def test_ports_not_a_list_errors(tmp_path: Path) -> None:
    """A mapping where a list is expected under ``ports`` is rejected."""
    p = _write(
        tmp_path,
        """
        reset-hints:
          ports:
            name: rst_n
        """,
    )
    with pytest.raises(ResetHintsError, match="'ports' must be a list"):
        load(p)


@pytest.mark.skipif(not PYYAML_INSTALLED, reason="[hints] extra not installed")
def test_port_item_not_mapping_errors(tmp_path: Path) -> None:
    """A bare scalar list item under ``ports`` is rejected with the
    per-item ``ports[idx]`` context prefix."""
    p = _write(
        tmp_path,
        """
        reset-hints:
          ports:
            - rst_n
        """,
    )
    with pytest.raises(ResetHintsError, match=r"ports\[0\]: expected mapping"):
        load(p)


@pytest.mark.skipif(not PYYAML_INSTALLED, reason="[hints] extra not installed")
def test_port_clock_non_string_errors(tmp_path: Path) -> None:
    """``clock`` is optional but, when present, must be a string. A
    numeric value trips the type guard."""
    p = _write(
        tmp_path,
        """
        reset-hints:
          ports:
            - { name: srst, polarity: high, type: sync, clock: 5 }
        """,
    )
    with pytest.raises(ResetHintsError, match="clock must be a string when set"):
        load(p)


@pytest.mark.skipif(not PYYAML_INSTALLED, reason="[hints] extra not installed")
def test_synchronizers_null_is_empty(tmp_path: Path) -> None:
    """An explicit null ``synchronizers:`` collapses to an empty tuple."""
    p = _write(
        tmp_path,
        """
        reset-hints:
          ports:
            - { name: rst_n, polarity: low }
          synchronizers:
        """,
    )
    hints = load(p)
    assert hints.synchronizers == ()
    assert hints.ports == (PortHint(name="rst_n", polarity="low"),)


@pytest.mark.skipif(not PYYAML_INSTALLED, reason="[hints] extra not installed")
def test_synchronizers_not_a_list_errors(tmp_path: Path) -> None:
    """A mapping where a list is expected under ``synchronizers`` is
    rejected."""
    p = _write(
        tmp_path,
        """
        reset-hints:
          synchronizers:
            instance: top.u_a
        """,
    )
    with pytest.raises(ResetHintsError, match="'synchronizers' must be a list"):
        load(p)


@pytest.mark.skipif(not PYYAML_INSTALLED, reason="[hints] extra not installed")
def test_synchronizer_item_not_mapping_errors(tmp_path: Path) -> None:
    """A scalar list item under ``synchronizers`` is rejected with the
    per-item context."""
    p = _write(
        tmp_path,
        """
        reset-hints:
          synchronizers:
            - top.u_a
        """,
    )
    with pytest.raises(ResetHintsError, match=r"synchronizers\[0\]: expected mapping"):
        load(p)


@pytest.mark.skipif(not PYYAML_INSTALLED, reason="[hints] extra not installed")
def test_synchronizer_unknown_key_errors(tmp_path: Path) -> None:
    """An unknown key under a synchronizer item fails the allow-set
    check before selector validation."""
    p = _write(
        tmp_path,
        """
        reset-hints:
          synchronizers:
            - { instance: top.u_a, depth: 2 }
        """,
    )
    with pytest.raises(ResetHintsError, match=r"synchronizers\[0\]: unknown keys"):
        load(p)


@pytest.mark.skipif(not PYYAML_INSTALLED, reason="[hints] extra not installed")
def test_synchronizer_selector_non_string_errors(tmp_path: Path) -> None:
    """``instance`` must be a string when set; a numeric value trips
    the selector-type guard before the exactly-one check."""
    p = _write(
        tmp_path,
        """
        reset-hints:
          synchronizers:
            - { instance: 5 }
        """,
    )
    with pytest.raises(ResetHintsError, match="must be strings when set"):
        load(p)


@pytest.mark.skipif(not PYYAML_INSTALLED, reason="[hints] extra not installed")
def test_synchronizer_empty_role_errors(tmp_path: Path) -> None:
    """An empty ``role`` string is rejected by the non-empty-string
    guard (distinct from the unknown-role guard)."""
    p = _write(
        tmp_path,
        """
        reset-hints:
          synchronizers:
            - { instance: top.u_a, role: "" }
        """,
    )
    with pytest.raises(ResetHintsError, match="'role' must be a non-empty string"):
        load(p)


# === reset_hints: synchronizer_cell_names resolver =========================


def _module_with_two_cells() -> Module:
    """A minimal flattened module with two flop cells whose decoded hier
    paths are ``top.u_rstgen.$procdff$1`` and ``top.u_rstgen.$procdff$2``.

    Yosys ``flatten`` encodes the nested instance path under the
    ``$flatten\\`` prefix; the leaf cell is a ``$``-prefixed auto-name
    (``$procdff$N``). ``_hier_path`` decodes that into the dotted form
    ``synchronizer_cell_names`` matches against. The cells need no real
    connectivity for the name-resolution path under test.
    """
    cells = {
        "$flatten\\u_rstgen.$procdff$1": Cell(
            name="$flatten\\u_rstgen.$procdff$1",
            type="$adff",
            connections={},
        ),
        "$flatten\\u_rstgen.$procdff$2": Cell(
            name="$flatten\\u_rstgen.$procdff$2",
            type="$adff",
            connections={},
        ),
    }
    return Module(name="top", ports={}, cells=cells, netnames={})


def test_synchronizer_cell_names_empty_when_no_synchronizers() -> None:
    """No synchronizer hints means the resolver short-circuits to an
    empty set without walking the module's cells."""
    hints = ResetHints(schema_version="1.0")
    assert hints.synchronizer_cell_names(_module_with_two_cells()) == set()


def test_synchronizer_cell_names_exact_match() -> None:
    """An ``instance`` hint matches exactly one cell by its decoded
    hierarchical path and returns that cell's raw name."""
    module = _module_with_two_cells()
    hints = ResetHints(
        schema_version="1.0",
        synchronizers=(SynchronizerHint(instance="top.u_rstgen.$procdff$1"),),
    )
    assert hints.synchronizer_cell_names(module) == {"$flatten\\u_rstgen.$procdff$1"}


def test_synchronizer_cell_names_glob_match() -> None:
    """An ``instance_glob`` hint matches every cell whose decoded path
    satisfies the shell glob — both sync stages here."""
    module = _module_with_two_cells()
    hints = ResetHints(
        schema_version="1.0",
        synchronizers=(SynchronizerHint(instance_glob="top.u_rstgen.$procdff$*"),),
    )
    assert hints.synchronizer_cell_names(module) == {
        "$flatten\\u_rstgen.$procdff$1",
        "$flatten\\u_rstgen.$procdff$2",
    }


def test_synchronizer_cell_names_no_match_is_empty() -> None:
    """A hint whose selector resolves to nothing yields an empty set —
    a non-matching glob and a non-matching exact name both miss."""
    module = _module_with_two_cells()
    hints = ResetHints(
        schema_version="1.0",
        synchronizers=(
            SynchronizerHint(instance="top.does_not_exist"),
            SynchronizerHint(instance_glob="top.other.*"),
        ),
    )
    assert hints.synchronizer_cell_names(module) == set()


# === reset_domain: classifier defensive arms ==============================


def test_classify_reset_source_constant() -> None:
    """A non-int reset bit (Yosys constant char) classifies as a
    constant source carrying the literal as its name."""
    module = Module(name="top", ports={}, cells={}, netnames={})
    name, kind = _classify_reset_source(module, "1", drivers={})
    assert (name, kind) == ("1", "constant")


def test_classify_reset_source_untracked_driver_is_comb() -> None:
    """An int reset bit with no port owner and no entry in the
    bit-drivers table is an untracked driver — classified ``comb``
    with an empty name."""
    module = Module(name="top", ports={}, cells={}, netnames={})
    name, kind = _classify_reset_source(module, 42, drivers={})
    assert (name, kind) == ("", "comb")


def test_classify_reset_source_non_q_driver_is_comb() -> None:
    """A bit driven by a cell's ``Y`` output (combinational) — not a
    flop ``Q`` — is classified ``comb``."""
    module = Module(name="top", ports={}, cells={}, netnames={})
    name, kind = _classify_reset_source(module, 7, drivers={7: ("u_and", "Y")})
    assert (name, kind) == ("", "comb")


def test_polarity_from_param_empty_defaults_low() -> None:
    """An empty polarity parameter defaults to the active-low idiom;
    a trailing ``1`` is active-high; anything else is low."""
    assert _polarity_from_param("") == "low"
    assert _polarity_from_param("1") == "high"
    assert _polarity_from_param("0") == "low"
    assert _polarity_from_param("00000000000000000000000000000001") == "high"


def test_assign_reset_domains_missing_pin_is_resetless() -> None:
    """A cell typed as reset-bearing (``$adff``) but missing its
    ``ARST`` pin connection is defensively treated as reset-less rather
    than crashing the per-flop walk.

    The flop needs the minimal pins ``find_flops`` keys on (``CLK``,
    ``D``, ``Q``) but deliberately omits ``ARST``.
    """
    cell = Cell(
        name="u_ff",
        type="$adff",
        connections={"CLK": (1,), "D": (2,), "Q": (3,)},
        parameters={"WIDTH": "1", "ARST_POLARITY": "1"},
    )
    module = Module(name="top", ports={}, cells={"u_ff": cell}, netnames={})
    domains = assign_reset_domains(module)
    assert "u_ff" in domains
    assert domains["u_ff"].reset is None


def test_find_reset_crossings_skips_resetless_flop() -> None:
    """A plain ``$dff`` (no reset pin) produces a ``ResetDomain`` whose
    ``reset`` is ``None``; ``find_reset_crossings`` skips it entirely,
    emitting no crossing for that flop."""
    cell = Cell(
        name="u_ff",
        type="$dff",
        connections={"CLK": (1,), "D": (2,), "Q": (3,)},
        parameters={"WIDTH": "1"},
    )
    module = Module(name="top", ports={}, cells={"u_ff": cell}, netnames={})
    crossings = find_reset_crossings(module, clock_domains={"u_ff": "clk"})
    assert crossings == []


# === reset_domain: iter_reset_sync_chains ==================================


def _load(name: str) -> Module:
    """Load a committed netlist-JSON fixture via the pure JSON reader."""
    path = FIX_ROOT / name / f"{name}.json"
    if not path.exists():
        pytest.skip(f"fixture not built: {path}")
    return netlist.load(path)


def test_iter_reset_sync_chains_on_deassert_polarity_fixture() -> None:
    """``bad_reset_sync_deassert_polarity`` is a real 2FF async-reset
    synchroniser whose head ``D`` is tied to the *wrong* constant for
    its polarity. ``iter_reset_sync_chains`` recognises the structural
    chain and surfaces the head's literal D-constant plus the chain's
    polarity — the facts RDC-007 compares.
    """
    from rtl_buddy_cdc.domain import assign_domains

    module = _load("bad_reset_sync_deassert_polarity")
    clock_domains = {fd.flop.cell.name: fd.clock for fd in assign_domains(module)}
    chains = iter_reset_sync_chains(module, clock_domains)
    assert len(chains) == 1, [c.flops for c in chains]
    chain = chains[0]
    # A 2FF synchroniser: head + tail.
    assert len(chain.flops) == 2
    assert chain.polarity in {"high", "low"}
    # The head D constant is a Yosys-encoded literal string.
    assert isinstance(chain.head_d_constant, str)
    assert chain.head_d_constant in {"0", "1", "x", "z"}


def test_iter_reset_sync_chains_empty_when_min_depth_too_high() -> None:
    """Bumping ``min_depth`` past the chain length drops the chain — no
    chain records survive the length filter."""
    from rtl_buddy_cdc.domain import assign_domains

    module = _load("bad_reset_sync_deassert_polarity")
    clock_domains = {fd.flop.cell.name: fd.clock for fd in assign_domains(module)}
    assert iter_reset_sync_chains(module, clock_domains, min_depth=5) == []


def _two_flop_chain(head_arst_bit: int, head_reset_port: str) -> Module:
    """A tail→head Q→D pair, both async-reset, in the same clock domain.

    ``u_tail``'s ``D`` is driven by ``u_head``'s ``Q`` (Q→D), ``u_head``'s
    ``D`` is tied to the constant ``1``. The head's ``ARST`` is wired to
    ``head_reset_port`` (bit ``head_arst_bit``) so the caller can make the
    two flops share — or *not* share — a reset source. The tail's ``ARST``
    is always the top-level ``rst_a`` (bit 10).
    """
    tail = Cell(
        name="u_tail",
        type="$adff",
        connections={"CLK": (1,), "D": (5,), "Q": (6,), "ARST": (10,)},
        parameters={"WIDTH": "1", "ARST_POLARITY": "0"},
    )
    head = Cell(
        name="u_head",
        type="$adff",
        connections={"CLK": (1,), "D": ("1",), "Q": (5,), "ARST": (head_arst_bit,)},
        parameters={"WIDTH": "1", "ARST_POLARITY": "0"},
    )
    ports = {
        "rst_a": Port(name="rst_a", direction="input", bits=(10,)),
        head_reset_port: Port(
            name=head_reset_port, direction="input", bits=(head_arst_bit,)
        ),
    }
    return Module(
        name="top", ports=ports, cells={"u_tail": tail, "u_head": head}, netnames={}
    )


def test_iter_reset_sync_chains_rejects_mismatched_reset_signature() -> None:
    """The Q→D walk only follows flops that *share* the tail's reset
    source. A head flop reset by a different top-level port breaks the
    chain (reset_domain.py line 415): the walk rejects it and no chain
    is recognised."""
    # head reset by ``rst_b`` (bit 11) — different source than tail's rst_a.
    module = _two_flop_chain(head_arst_bit=11, head_reset_port="rst_b")
    clock_domains = {"u_tail": "clk", "u_head": "clk"}
    assert iter_reset_sync_chains(module, clock_domains) == []


def test_iter_reset_sync_chains_rejects_untraceable_driver_clock() -> None:
    """When the upstream driver flop's clock is untraceable (absent from
    ``clock_domains``), the walk refuses to follow it (reset_domain.py
    line 411). Even though both flops share the same reset port, the
    missing clock breaks recognition."""
    # head and tail share rst_a, but the head's clock isn't in the map.
    module = _two_flop_chain(head_arst_bit=10, head_reset_port="rst_a")
    clock_domains: dict[str, str | None] = {"u_tail": "clk", "u_head": None}
    assert iter_reset_sync_chains(module, clock_domains) == []


# === reset_domain_map: serializer skips + location fallbacks ===============


def test_serialize_synchronizers_skips_unknown_cell() -> None:
    """A ``recognised_syncs`` name with no matching ``reset_domains``
    entry is silently skipped — the resolver only emits members it can
    look up. Here every other input is empty, so the synchronizers
    section is empty despite the dangling name."""
    module = Module(name="top", ports={}, cells={}, netnames={})
    payload = build_reset_domain_map(
        module,
        reset_domains={},
        clock_domains={},
        recognised_syncs={"ghost_cell"},
        polarity_overrides={},
        reset_crossings=[],
    )
    assert payload["reset_synchronizers"] == []
    assert payload["flop_resets"] == []
    assert payload["reset_sources"] == []


def test_serialize_flop_resets_skips_resetless_flop() -> None:
    """The ``flop_resets`` section is a *reset* inventory: a plain
    ``$dff`` (no reset pin) is omitted while a reset-bearing ``$adff``
    is emitted. Only the reset-bearing flop survives the section.
    """
    plain = Cell(
        name="u_plain",
        type="$dff",
        connections={"CLK": (1,), "D": (2,), "Q": (3,)},
        parameters={"WIDTH": "1"},
    )
    rstd = Cell(
        name="u_rstd",
        type="$adff",
        connections={"CLK": (1,), "D": (4,), "Q": (5,), "ARST": (6,)},
        parameters={"WIDTH": "1", "ARST_POLARITY": "1"},
    )
    module = Module(
        name="top",
        ports={"arst": Port(name="arst", direction="input", bits=(6,))},
        cells={"u_plain": plain, "u_rstd": rstd},
        netnames={},
    )
    clock_domains = {"u_plain": "clk", "u_rstd": "clk"}
    reset_domains = assign_reset_domains(module)
    assert reset_domains["u_plain"].reset is None
    assert reset_domains["u_rstd"].reset is not None
    payload = build_reset_domain_map(
        module,
        reset_domains,
        clock_domains,
        recognised_syncs=set(),
        polarity_overrides={},
        reset_crossings=[],
    )
    # Only the reset-bearing flop appears in the reset inventory.
    paths = [fr["instance_path"] for fr in payload["flop_resets"]]
    assert paths == ["top.u_rstd"]
    fr = payload["flop_resets"][0]
    assert fr["reset"] == "arst"
    assert fr["reset_kind"] == "port"
    assert fr["polarity"] == "high"
    assert fr["type"] == "async"


def test_reset_source_location_port_without_src_returns_none() -> None:
    """A ``port`` source whose netname is absent (or carries no ``src``
    attribute) has no resolvable location."""
    module = Module(name="top", ports={}, cells={}, netnames={})
    assert _reset_source_location(module, "rst_n", "port") is None


def test_reset_source_location_port_with_src_attr() -> None:
    """A ``port`` source whose netname carries a parseable ``src``
    attribute resolves to a file/line location."""
    nn = Netname(
        name="rst_n",
        bits=(1,),
        attributes={"src": "design.sv:12.5-12.20"},
    )
    module = Module(name="top", ports={}, cells={}, netnames={"rst_n": nn})
    loc = _reset_source_location(module, "rst_n", "port")
    assert loc == {
        "file": "design.sv",
        "start_line": 12,
        "start_column": 5,
        "end_line": 12,
        "end_column": 20,
    }


def test_reset_source_location_constant_returns_none() -> None:
    """A ``constant`` reset source has no source location."""
    module = Module(name="top", ports={}, cells={}, netnames={})
    assert _reset_source_location(module, "1", "constant") is None


def test_parse_src_attr_unparseable_falls_back_to_file() -> None:
    """A ``src`` attribute the reporter regex can't match (no
    ``:line``) falls back to a file-only location."""
    assert _parse_src_attr("plainname") == {"file": "plainname"}


def test_parse_src_attr_empty_returns_none() -> None:
    """An empty ``src`` attribute resolves to no location."""
    assert _parse_src_attr("") is None


def test_parse_src_attr_full_location() -> None:
    """A fully-specified ``src`` attribute decodes every coordinate."""
    assert _parse_src_attr("foo.sv:3.1-4.9") == {
        "file": "foo.sv",
        "start_line": 3,
        "start_column": 1,
        "end_line": 4,
        "end_column": 9,
    }


def test_resetsource_frozen() -> None:
    """The reset data model is frozen so consumers can hash/cache it."""
    rs = ResetSource(name="rst_n", polarity="low", type="async", source="port")
    with pytest.raises(Exception):
        rs.name = "other"  # type: ignore[misc]
