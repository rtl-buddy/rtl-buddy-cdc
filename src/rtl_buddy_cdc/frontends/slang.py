"""slang frontend — elaborate SystemVerilog via pyslang into a
Yosys-shape :class:`netlist.Module` consumable by the rule pack.

Status
------
**Stage 2 (fourth slice — full fixture parity).** All 25
SDC-equipped fixtures reach parity with the Yosys frontend,
covering register-to-register CDC, combinational shapes,
multi-bit LHS bit-selects, and now multi-module hierarchies.
The two ``*_source_sync_internal`` fixtures that embed Yosys-
internal ``$_BUF_`` primitives in SV source are correctly
rejected by slang as not being legal SystemVerilog and stay
Yosys-frontend-only by design.

Tested against ``pyslang>=10,<12`` — that's the range the
``[slang]`` extra pins (see ``pyproject.toml`` and issues #26 /
#300), mirrored in code by :data:`PYSLANG_SUPPORTED_RANGE`, which
:func:`_import_pyslang` enforces before any elaboration work
starts. pyslang 11 split the flat top-level namespace into
submodules (``Compilation`` / ``CompilationOptions`` /
``CompilationFlags`` → ``pyslang.ast``; ``SyntaxTree`` →
``pyslang.syntax``); :func:`_pyslang_namespaces` bridges the two
layouts. Older majors had different ``DiagnosticEngine`` /
``getAttributes`` surfaces; widening the cap means re-running
the slang test files against the new wheel.

Rule parity matrix (today)
~~~~~~~~~~~~~~~~~~~~~~~~~~
Driven by ``tests/test_slang_elaboration.py``; each row is a fixture
that produces the same violation set as the Yosys frontend.

==================================  =================
fixture                             expected rules
==================================  =================
bad_single_ff_sync                  CDC-001
bad_port_no_sync                    CDC-001
bad_comb_before_sync                CDC-003
bad_comb_before_sync_with_if        CDC-003 (if/else → $mux)
bad_comb_case_before_sync           CDC-003 (case → chained $mux)
bad_bus_crossing                    CDC-004
bad_reconvergent_sync               CDC-005
bad_comb_source                     CDC-006
bad_input_delay_cross_domain        CDC-006
bad_reset_crossing                  RDC-001
bad_reset_tree                      RDC-001 (grouped)
bad_clock_as_data                   CDC-008
bad_source_sync_chain               CDC-001 × 4 (4-module hierarchy)
good_2ff_sync                       (none)
good_gray_counter_crossing          (none)
good_registered_before_sync         (none)
good_registered_source              (none)
good_exclusive_clock_mux            (none)
good_false_path_pair                (none)
good_generated_clock_div2           (none)
good_port_typed_sync                (none)
marked_user_sync                    (none — attribute suppression)
bad_dualport_ram_crossing           CDC-026 (memory ports, #326)
bad_dualport_ram_async_read         CDC-026
bad_ram_write_addr_wrong_domain     CDC-004 (into a write port)
good_async_fifo_inferred_ram        (none — sanctioned FIFO)
good_same_clock_ram                 (none)
==================================  =================

(The memory rows are driven by ``tests/test_memory_crossings.py``.)

What currently works
~~~~~~~~~~~~~~~~~~~~
- Parse + elaborate one or more SV sources, resolve the top module.
- Ports (input / output / inout) with their bit-id allocation.
- Local ``logic`` / ``reg`` variables and ``wire`` nets become
  :class:`Netname`s (``inst.name`` inside an instance), with
  any ``(* attr *)`` declaration attributes propagated (looked up via
  ``Compilation.getAttributes(symbol)`` — pyslang stores attributes
  on the compilation, not on the symbol).
- ``always_ff`` blocks with the canonical async-reset shape::

      always_ff @(posedge clk or negedge rst_n)
          if (!rst_n) q <= 0; else q <= d;

  emit ``$adff`` cells with ``CLK``/``D``/``Q``/``ARST`` connections; an
  async arm loading a non-constant value (``if (aload) q <= ad``) emits
  the ``$aldff`` Yosys ``proc`` builds, and two or more async controls
  (``@(posedge clk or negedge rst_n or negedge set_n)``) the ``$dffsr``
  (rtl-buddy-cdc#384, :meth:`_ModuleBuilder._emit_multi_async_ff`).
  ``if (en) if (srst) <constants> else <data>`` as a whole body is the
  ``$sdffce`` Yosys ``opt_dff`` folds it into; a sync reset at the top of
  the body is an ``$sdff`` (an enable under it stays a hold mux on ``D``).
  Plain ``always_ff @(posedge clk) q <= d;`` emits ``$dff``.
- Verilog-2001 ``always`` blocks (rtl-buddy-cdc#345), by what they
  infer: edge-triggered → the ``always_ff`` lowering; ``@*`` or a
  level-sensitive list → the ``always_comb`` lowering. Anything else is
  reported with a ``WARNING`` (see :meth:`_ModuleBuilder._warn_dropped`).
- Level-sensitive bodies (``always_comb``, ``always @*``,
  ``always_latch``) lower the way Yosys ``proc`` does
  (rtl-buddy-cdc#377): an ``if`` / ``case`` merges each variable with a
  ``$mux``, an arm that does not write it contributing its prior value,
  and a variable held on some path becomes a ``$dlatch`` enabled by the
  condition it is written under (:meth:`_ModuleBuilder._infer_latches`);
  the rest is combinational. The result reaches readers anywhere in the
  module, earlier in the file included.
- In clocked blocks a blocking write's value reaches the later reads of
  the same block; each register is one flop per contiguous run of the
  bits the block writes, as ``proc`` groups them (rtl-buddy-cdc#377).
- User-defined function calls are inlined; ``for`` loops (SV and
  Verilog-2001 forms) are unrolled in every block; ``n++`` and compound
  assignments are writes; ``casez`` / ``casex`` labels compare their
  non-wildcard bits; parameters, enum values and ``x`` / ``z`` literals
  are constants; gate primitives lower to ``$and`` / ``$or`` / ``$xor``
  / ``$not`` (rtl-buddy-cdc#377).
- Blackbox boundaries (rtl-buddy-cdc#345): ``(* blackbox *)`` modules
  and ``--blackbox`` / ``--greybox`` names stay as one cell per
  instance, with a sibling module — see :func:`elaborate_with_blackboxes`.
- Arrays of instances (rtl-buddy-cdc#353): ``sync2 u_sync[1:0] (...)``
  lowers one instance per element, named ``u_sync[0]`` / ``u_sync[1]``
  as Yosys ``flatten`` names them (``u[1][0]`` for a multi-dimensional
  array, which Yosys rejects), inlined or as boundary cells alike.
  Connections are sliced per the LRM by slang itself; on an ascending
  range Yosys ``read_verilog`` assigns the slices the other way round —
  see :meth:`_ModuleBuilder._emit_instance_array`.
- Direct ``port = var`` continuous assigns are aliased into the
  source variable's bits (matches Yosys post-``opt_clean``). The
  aliasing is propagated globally: every entry in the bit-id maps
  that holds the old tuple is rewritten, so chains across the
  hierarchy boundary (parent ``a_q`` ← child ``q``) collapse to a
  single net rather than leaving stale aliases.
- Multi-module hierarchies — child :class:`InstanceSymbol`s are
  walked recursively; their flops, comb cells, and netnames land
  in the same flat ``Module`` with dotted prefixes (``u_b0.q``,
  ``u_b0.$slang$adff$3``) matching Yosys-flatten output. Port
  connections are the wiring step: each child internal variable
  backing a port is aliased to the parent's connection expression
  bits, so flop A's Q in one instance and flop B's D in the next
  resolve to the same net.
- Combinational primitive lowering: see :meth:`_lower_binary`
  / :meth:`_lower_unary` / :meth:`_lower_conditional`. Pyslang
  expression operators round-trip to the Yosys cell zoo
  (``$and``/``$or``/``$xor``/``$mux``/…), so the rule pack walks
  the comb cone correctly. Pin *order* is part of that contract:
  Yosys' ``$mux`` is ``Y = S ? B : A``, so a ternary's FALSE branch
  goes on ``A`` and its TRUE branch on ``B`` — see
  :meth:`_lower_conditional` and issue #289.
- ``always_comb`` procedural ``if`` / ``case`` selection — see
  :meth:`_walk_conditional_statement` and
  :meth:`_walk_case_statement`. Each LHS written across multiple
  branches lowers to a real ``$mux`` (chained for ``case``, with
  first-match-wins priority and ``default`` as the innermost
  fallback); LHS written in only one branch keeps the existing
  single-branch aliasing.
- LHS bit-selects (``q[0] <= ...``), contiguous ranges (``bus[3:0] <=
  ...``, ``bus[i +: 2]``) and packed-struct members (``s.f <= ...``) —
  see :meth:`_lvalue_bits`. Same shapes accepted on the RHS via
  :meth:`_bits_of_expression`. Indices are translated through the
  declared range, so ``a[1]`` of ``logic [4:1] a`` is its LSB and
  ``b[0]`` of ``logic [0:3] b`` its MSB.
- Concatenation targets (rtl-buddy-cdc#370): ``{q, meta} <= {meta, d}``,
  ``{carry, sum} = a + b``, nested and with any of the part shapes above,
  in clocked, combinational and latch blocks, continuous assigns, reset
  arms and output-port connections. The RHS is sized to the total target
  width by SystemVerilog rules (:meth:`_sized_bits`) and split with the
  rightmost part taking the LSBs; the parts of one register forming a
  contiguous range are one flop (:meth:`_group_parts`). Combinational
  and continuous writes to part of a variable splice into its other bits
  (:meth:`_splice_assign`). Every assignment's RHS is sized to its target.
- An assignment target this frontend cannot lower — a streaming
  concatenation, a run-time index outside the memory-write and
  ``$shl``-decoder shapes, an unpacked-struct member, a hierarchical
  reference — is reported once per location with a ``WARNING`` (see
  :meth:`_ModuleBuilder._warn_lvalue_dropped`), never dropped silently.
- A right-hand side, condition or statement the frontend cannot lower
  is reported with a ``WARNING`` and modelled as an opaque ``$_UNKNOWN_``
  driver or skipped, never dropped silently (rtl-buddy-cdc#377).
- A run-time index read of a packed value (``a[i]``, ``a[i +: W]``,
  ``a[i -: W]``) is the ``$shiftx`` Yosys emits, its position translated
  through the declared range (rtl-buddy-cdc#384); see
  :meth:`_ModuleBuilder._lower_dynamic_select`.
- Concatenation (``{a, b, c}``) and replication (``{N{x}}``) on the
  RHS — see :meth:`_lower_concatenation` and
  :meth:`_lower_replication`. Pure bit-tuple aliasing, no Yosys
  cell is emitted; matches Yosys post-``opt_clean`` behaviour.
- Source locations propagated into ``Cell.attributes["src"]`` in
  Yosys' ``"file:line.col-line.col"`` format. The ``$dff`` /
  ``$adff`` src spans the whole ``always_ff`` block; comb cells
  span the operator expression. JSON / SARIF reporters surface
  these as clickable file:line locations without a
  frontend-specific branch.
- Fatal pyslang diagnostics are surfaced through
  ``TextDiagnosticClient`` with file:line:col + caret summaries —
  the usual compiler-error format users already recognise.

What is NOT yet implemented (next slices)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
- ``while`` / ``repeat`` / ``foreach`` loops, task calls, functions
  with an early ``return`` or an ``output`` argument, tristate gates,
  switches and user-defined primitives: reported, not lowered.
- (was: ``CLK_POLARITY = "0"`` for negedge-clocked flops — landed
  in rtl-buddy-cdc#221 to close the CDC-016 cross-frontend parity
  gap. ``_analyse_event_list`` now reports the clock edge alongside
  the reset edge, ``_emit_procedural_block`` stashes it as
  ``self._clk_negedge``, and ``_emit_flop_cell`` reads it when
  populating the ``CLK_POLARITY`` parameter. ``WIDTH`` /
  ``ARST_VALUE`` / ``ARST_POLARITY`` are still populated in
  Yosys-binary form per issue #40, and width comes straight from
  the Q bit-tuple, so multi-bit flops have correct parameter
  shape.)
- **Yosys-only constructs** (``$_BUF_`` / ``$_NOT_`` primitives in SV
  source) are correctly rejected by slang — they're post-synthesis
  cells, not legal SV. Fixtures that rely on them
  (``*_source_sync_internal``) stay Yosys-frontend-only.
"""

from __future__ import annotations

import functools
import importlib.metadata
import logging
import math
import re
from dataclasses import replace
from collections.abc import Callable
from pathlib import Path
from typing import Any, NamedTuple

from rtl_buddy_cdc.netlist import Bit, Cell, Module, Netname, Port

_log = logging.getLogger(__name__)

_PYSLANG_INSTALL_HINT = (
    "pyslang is required for the slang frontend. Install via:\n"
    "    pip install 'rtl-buddy-cdc[slang]'\n"
    "or directly:\n"
    "    pip install pyslang"
)

# --- supported pyslang range (issue #300) ----------------------------------
#
# THE single source of truth in code. ``pyproject.toml``'s ``[slang]``
# extra declares the same range and
# ``tests/test_slang_version_guard.py::test_supported_range_matches_pyproject``
# fails if the two drift apart.
PYSLANG_MIN_MAJOR = 10
PYSLANG_MAX_MAJOR_EXCLUSIVE = 12
PYSLANG_SUPPORTED_RANGE = f">={PYSLANG_MIN_MAJOR},<{PYSLANG_MAX_MAJOR_EXCLUSIVE}"

# Marker for "no prior binding" in ``_emit_for_loop``'s save/restore
# discipline around ``_loop_bindings``. Using a sentinel (rather than
# ``None``) lets nested loops correctly nest their own loop variable
# even if a previous binding was ``0`` or other falsy int.
_SENTINEL: Any = object()


class SlangFrontendUnavailable(RuntimeError):
    """pyslang import failed (extra not installed)."""


class SlangElaborationError(RuntimeError):
    """pyslang parsed the sources but the elaboration / lowering step
    couldn't produce a usable :class:`Module` (top not found, source
    diagnostics, unsupported construct in this slice)."""


def _pyslang_version(pyslang: Any) -> str | None:
    """Best-effort version string for the imported ``pyslang``.

    pyslang 11 exposes ``__version__``; 10.x does **not**, so fall back
    to the installed distribution metadata. ``None`` when neither the
    attribute nor the metadata is readable."""
    raw = getattr(pyslang, "__version__", None)
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    try:
        return importlib.metadata.version("pyslang")
    except Exception:
        return None


def _check_pyslang_version(pyslang: Any) -> None:
    """Fail early when the installed pyslang major is outside
    :data:`PYSLANG_SUPPORTED_RANGE` (issue #300).

    Without this, an out-of-range wheel dies with an ``AttributeError``
    deep inside elaboration (``module 'pyslang' has no attribute
    'CompilationOptions'`` on 12+, say) instead of naming the problem.

    **Unreadable / unparseable versions warn and proceed.** The guard
    exists to replace a confusing traceback with a clear message, not
    to break an otherwise-working install over a cosmetic version
    string; an install that really is incompatible still fails, just
    with the old traceback."""
    version = _pyslang_version(pyslang)
    if version is None:
        _log.warning(
            "could not determine the installed pyslang version "
            "(supported: %s) — proceeding",
            PYSLANG_SUPPORTED_RANGE,
        )
        return
    match = re.match(r"\s*(\d+)", version)
    if match is None:
        _log.warning(
            "could not parse the installed pyslang version %r "
            "(supported: %s) — proceeding",
            version,
            PYSLANG_SUPPORTED_RANGE,
        )
        return
    major = int(match.group(1))
    if not PYSLANG_MIN_MAJOR <= major < PYSLANG_MAX_MAJOR_EXCLUSIVE:
        raise SlangFrontendUnavailable(
            f"pyslang {version} is not supported by the slang frontend "
            f"(supported: {PYSLANG_SUPPORTED_RANGE}); install a supported "
            f"pyslang (pip install 'pyslang{PYSLANG_SUPPORTED_RANGE}') "
            f"or use --frontend yosys"
        )


def _import_pyslang():
    """Lazy import. Raises :class:`SlangFrontendUnavailable` with an
    install hint if pyslang isn't on the path, or with a version message
    if it is out of :data:`PYSLANG_SUPPORTED_RANGE`. Importing here (not
    at module load) keeps pyslang truly optional — the default install
    is typer-only per the AGENTS.md runtime-deps policy."""
    try:
        import pyslang  # type: ignore[import-not-found]
    except ImportError as e:
        raise SlangFrontendUnavailable(_PYSLANG_INSTALL_HINT) from e
    _check_pyslang_version(pyslang)
    return pyslang


def _pyslang_namespaces(pyslang: Any) -> tuple[Any, Any]:
    """Return ``(ast_ns, syntax_ns)`` for the installed pyslang.

    pyslang 11 split the flat top-level namespace into submodules —
    ``Compilation`` / ``CompilationOptions`` / ``CompilationFlags`` moved
    to ``pyslang.ast`` and ``SyntaxTree`` to ``pyslang.syntax``, while
    ``Bag`` / ``DiagnosticEngine`` / ``TextDiagnosticClient`` stayed at
    the top level. 10.x has everything at the top level and no
    submodules at all, so falling back to the module itself gives one
    lookup path that works on both (issue #300)."""
    return getattr(pyslang, "ast", pyslang), getattr(pyslang, "syntax", pyslang)


def elaborate(
    sources: list[Path], top: str, *, incdirs: list[Path] | None = None
) -> Module:
    """Elaborate ``sources`` via pyslang and produce a :class:`Module`.

    ``incdirs`` are the `` `include `` search directories, searched after
    the including file's own directory. Blackbox boundary siblings are
    dropped; use :func:`elaborate_with_blackboxes` to receive them."""
    module, _blackboxes = elaborate_with_blackboxes(sources, top, incdirs=incdirs)
    return module


def elaborate_with_blackboxes(
    sources: list[Path],
    top: str,
    *,
    incdirs: list[Path] | None = None,
    blackbox: list[str] | None = None,
    greybox: list[str] | None = None,
) -> tuple[Module, dict[str, Module]]:
    """Elaborate ``sources`` into the flat top :class:`Module` plus its
    blackbox boundary siblings, keyed by module name (rtl-buddy-cdc#345).

    Same shape :func:`rtl_buddy_cdc.netlist.load_with_blackboxes` returns
    for a Yosys netlist. An instance of a module that carries a truthy
    ``(* blackbox *)`` attribute, or is named in ``blackbox``, is not
    inlined: it becomes one cell whose ``type`` is the module name, with
    its port connections and the instance's overridden parameters, and
    the module itself becomes a bodyless ``is_blackbox`` sibling. A
    ``greybox`` module gets the same boundary cell, but its sibling keeps
    the lowered body so it can be analysed compositionally (#261).
    """
    overlap = sorted(set(blackbox or ()) & set(greybox or ()))
    if overlap:
        raise SlangElaborationError(
            "--blackbox and --greybox name the same module(s): "
            f"{', '.join(overlap)}. --blackbox discards the body (nothing "
            "left to analyse); --greybox keeps it so the subtree can be "
            "analysed once on its own internals. Pick one."
        )
    pyslang = _import_pyslang()

    # Relax two defaults that are too strict for a *standalone block*
    # CDC lint, keeping this frontend's leniency aligned with the
    # yosys / read_slang path (see frontends/yosys.py): a block linted
    # on its own legitimately has unconnected top-level SystemVerilog
    # interface ports, and module-item references to nets declared
    # later in the same module are valid RTL. ``AllowTopLevelIfacePorts``
    # is already on by default in pyslang; set it explicitly so the
    # intent survives a future default change.
    ast_ns, syntax_ns = _pyslang_namespaces(pyslang)
    options = ast_ns.CompilationOptions()
    options.flags = (
        options.flags
        | ast_ns.CompilationFlags.AllowTopLevelIfacePorts
        | ast_ns.CompilationFlags.AllowUseBeforeDeclare
    )
    bag = pyslang.Bag()
    bag.compilationOptions = options

    if incdirs:
        # pyslang 11 moved PreprocessorOptions into ``pyslang.parsing``.
        parsing_ns = getattr(pyslang, "parsing", pyslang)
        pp_options = parsing_ns.PreprocessorOptions()
        pp_options.additionalIncludePaths = [str(d) for d in incdirs]
        bag.preprocessorOptions = pp_options

    comp = ast_ns.Compilation(bag)
    for src in sources:
        if incdirs:
            # The include paths reach the preprocessor only through the
            # parse-time Bag; the one-argument overload uses default options.
            # The default source manager is the one that overload uses.
            tree = syntax_ns.SyntaxTree.fromFile(
                str(src), syntax_ns.SyntaxTree.getDefaultSourceManager(), bag
            )
        else:
            tree = syntax_ns.SyntaxTree.fromFile(str(src))
        comp.addSyntaxTree(tree)

    # Surface fatal parse / elaboration errors — keep going through
    # warnings (matches Yosys' default behaviour: warnings don't block
    # write_json). We route diagnostics through pyslang's
    # ``TextDiagnosticClient`` which emits the usual file:line:col +
    # caret summary that users already recognise from compiler output.
    diags = comp.getAllDiagnostics()
    fatal = [d for d in diags if d.isError()]
    if fatal:
        engine = pyslang.DiagnosticEngine(comp.sourceManager)
        client = pyslang.TextDiagnosticClient()
        client.showColors(False)
        engine.addClient(client)
        for d in fatal:
            engine.issue(d)
        raise SlangElaborationError(
            "pyslang elaboration produced errors:\n" + client.getString()
        )

    root = comp.getRoot()
    matching = [i for i in root.topInstances if i.name == top]
    if not matching:
        available = [i.name for i in root.topInstances]
        raise SlangElaborationError(
            f"top module {top!r} not found among elaborated instances: {available}"
        )

    siblings: dict[str, Module] = {}
    builder = _ModuleBuilder(
        matching[0],
        comp,
        pyslang,
        blackbox_names=frozenset(blackbox or ()),
        greybox_names=frozenset(greybox or ()),
        siblings=siblings,
    )
    module = builder.build()
    for name in sorted(set(blackbox or ()) | set(greybox or ())):
        if name not in siblings:
            _log.warning(
                "slang frontend: --blackbox/--greybox module %r is not "
                "instantiated under top %r; the option has no effect",
                name,
                top,
            )
    return module, siblings


# --- internal: build a Yosys-shape Module from a pyslang InstanceSymbol -----


def _port_direction(port: Any) -> str:
    """Yosys port direction for a pyslang ``PortSymbol``.

    ``PortSymbol.direction`` is an ``ArgumentDirection`` enum. ``ref``
    ports are uncommon at a boundary; they read as inputs for CDC."""
    d = str(getattr(port, "direction", "")).rsplit(".", 1)[-1]
    return {"In": "input", "Out": "output", "InOut": "inout"}.get(d, "input")


def _param_int32(val: int) -> str:
    """Encode ``val`` as a 32-bit MSB-first binary string. Matches the
    serialisation Yosys writes for integer-typed cell parameters
    (``WIDTH``, ``ARST_POLARITY``) into ``write_json`` output."""
    return format(val & 0xFFFFFFFF, "032b")


def _param_bits(val: int, width: int) -> str:
    """Encode ``val`` as an ``N``-bit MSB-first binary string of length
    ``max(width, 1)``. Matches how Yosys serialises constant-bit-vector
    parameters whose width tracks the flop's data width (``ARST_VALUE``,
    where the string length equals the ``$adff``'s ``WIDTH``)."""
    w = max(int(width), 1)
    return format(val & ((1 << w) - 1), f"0{w}b")


def _encode_param(value: Any) -> str | None:
    """Encode an elaborated parameter value the way Yosys ``write_json``
    does: an integral value as an MSB-first bit string of its own width
    (``integer`` → 32 bits, ``x`` / ``z`` digits kept), a real as its
    decimal text. ``None`` when the value cannot be read."""
    inner = getattr(value, "value", value)
    if isinstance(inner, float):
        return repr(inner)
    width = getattr(inner, "bitWidth", None)
    if width is None:
        return None
    width = max(int(width), 1)
    # ``SVInt.hasUnknown`` is a property on some pyslang builds and a
    # method on others.
    has_unknown = getattr(inner, "hasUnknown", False)
    if callable(has_unknown):
        has_unknown = has_unknown()
    if has_unknown:
        try:
            return str(inner.toString(_literal_base_binary(), False)).rjust(width, "0")
        except Exception:
            return None
    try:
        return _param_bits(int(inner), width)
    except (TypeError, ValueError):
        return None


def _fold_binary(op: str, left: int, right: int) -> int | None:
    """``left <op> right`` for an elaboration-time fold of a loop-bound
    expression (``i - 1``, ``k < 3``, ``k == N``), or ``None`` for an
    operator this does not fold. Comparisons and bitwise operators are
    folded only over non-negative operands, where Python's unbounded
    integers agree with any SystemVerilog width and signedness."""
    arith: dict[str, Callable[[], int | None]] = {
        "Add": lambda: left + right,
        "Subtract": lambda: left - right,
        "Multiply": lambda: left * right,
        "Divide": lambda: left // right if right else None,
        "Mod": lambda: left % right if right else None,
        "Power": lambda: left**right if right >= 0 else None,
    }
    if op in arith:
        return arith[op]()
    if left < 0 or right < 0:
        return None
    other: dict[str, Callable[[], int]] = {
        "Equality": lambda: int(left == right),
        "Inequality": lambda: int(left != right),
        "CaseEquality": lambda: int(left == right),
        "CaseInequality": lambda: int(left != right),
        "LessThan": lambda: int(left < right),
        "LessThanEqual": lambda: int(left <= right),
        "GreaterThan": lambda: int(left > right),
        "GreaterThanEqual": lambda: int(left >= right),
        "LogicalAnd": lambda: int(bool(left) and bool(right)),
        "LogicalOr": lambda: int(bool(left) or bool(right)),
        "BinaryAnd": lambda: left & right,
        "BinaryOr": lambda: left | right,
        "BinaryXor": lambda: left ^ right,
        "LogicalShiftLeft": lambda: left << min(right, 1024),
        "LogicalShiftRight": lambda: left >> right,
    }
    fold = other.get(op)
    return None if fold is None else fold()


def _literal_base_binary() -> Any:
    import pyslang  # type: ignore[import-not-found]

    return pyslang.LiteralBase.Binary


class _Bits:
    """An already-lowered right-hand side, standing in for an expression
    (see :class:`_Assign`); ``syntax`` / ``sourceRange`` are the source
    construct's, for diagnostics."""

    def __init__(self, bits: tuple[Bit, ...], node: Any) -> None:
        self.bits = bits
        self.type = None
        self.syntax = getattr(node, "syntax", None)
        self.sourceRange = getattr(node, "sourceRange", None)


class _Assign:
    """A write the procedural walkers lower like a blocking
    ``AssignmentExpression``: ``n++`` / ``n--`` as ``n = n ± 1``
    (rtl-buddy-cdc#377)."""

    isNonBlocking = False
    isCompound = False

    def __init__(self, left: Any, right: _Bits, node: Any) -> None:
        self.left = left
        self.right = right
        self.syntax = getattr(node, "syntax", None)
        self.sourceRange = getattr(node, "sourceRange", None)


class _ProcCase:
    """One arm of a clocked block's decision tree, the shape Yosys keeps a
    process in before ``proc_mux`` (an RTLIL ``CaseRule``), in walk order:
    writes ``(variable, start, value bits)`` and nested :class:`_ProcSwitch`
    es (rtl-buddy-cdc#394)."""

    __slots__ = ("items", "by_sym")

    def __init__(self) -> None:
        self.items: list[tuple[Any, int, tuple[Bit, ...]] | _ProcSwitch] = []
        # ``items`` that write each variable, in order (filled by
        # :meth:`_ModuleBuilder._index_proc_tree`).
        self.by_sym: dict[
            Any, list[tuple[Any, int, tuple[Bit, ...]] | _ProcSwitch]
        ] = {}


class _ProcSwitch:
    """An ``if`` / ``case`` of a clocked block: its arms in priority order,
    each ``(select bit, arm)``; a ``None`` select is the ``else`` /
    ``default`` arm, last. ``writes`` (filled by
    :meth:`_ModuleBuilder._index_proc_tree`) is the bit ranges each
    variable is written in anywhere below it."""

    __slots__ = ("arms", "writes")

    def __init__(self, arms: list[tuple[Bit | None, _ProcCase]]) -> None:
        self.arms = arms
        self.writes: dict[Any, list[tuple[int, int]]] = {}


class _ArrayAccess(NamedTuple):
    """A select chain over an unpacked-array variable
    (:meth:`_ModuleBuilder._array_access`, rtl-buddy-cdc#398)."""

    sym: Any  # the array variable (canonical)
    dims: list[tuple[int, int]]  # ``(left, right)`` per unpacked dimension
    selectors: list[Any]  # index of each unpacked dimension selected
    element: Any | None  # the select naming one whole element, if any
    packed: list[Any]  # selects applied to that element, innermost first


class _ModuleBuilder:
    """Translate a pyslang elaborated :class:`InstanceSymbol` into a
    :class:`netlist.Module`.

    The Yosys-shape contract the rule pack consumes:

    - ports + cells + netnames keyed by name, all at one (flat) level,
    - net bits as integers (one ID per signal-bit), with the constants
      ``"0"`` / ``"1"`` / ``"x"`` / ``"z"`` represented as those strings,
    - cell type strings matching the Yosys ``$dff`` / ``$adff`` / ``$and``
      / ``$xor`` / … zoo,
    - pin names ``CLK`` / ``D`` / ``Q`` / ``ARST`` on flops, ``A`` / ``B``
      / ``Y`` (etc.) on comb cells.

    Bit IDs start at 2 so the constant chars ``"0"`` and ``"1"`` never
    collide with a real net — same convention Yosys uses in its JSON.
    """

    def __init__(
        self,
        top_inst: Any,
        compilation: Any,
        pyslang: Any,
        *,
        blackbox_names: frozenset[str] = frozenset(),
        greybox_names: frozenset[str] = frozenset(),
        siblings: dict[str, Module] | None = None,
    ) -> None:
        self.top = top_inst
        self.comp = compilation
        self.pyslang = pyslang
        # Blackbox boundaries (rtl-buddy-cdc#345): module names the CLI
        # asked to keep as boundary cells, and the shared map the bodyless
        # (or, for a greybox, separately lowered) sibling modules land in.
        self._blackbox_names = blackbox_names
        self._greybox_names = greybox_names
        self._siblings: dict[str, Module] = siblings if siblings is not None else {}
        # Blocking writes in the clocked block being lowered: variable ->
        # its register bits (its net). Its entry in ``_var_bits`` holds
        # the value written so far, which later reads in the block see
        # (rtl-buddy-cdc#377).
        self._blocking_nets: dict[Any, tuple[Bit, ...]] = {}
        self._next_bit_id = 2
        # Map canonical pyslang VariableSymbol → tuple of bit refs.
        # PortSymbols are normalised to their internal Variable before
        # lookup so a port and its underlying variable share bits.
        # Entries start as integer-only tuples from the allocator, but
        # the continuous-assign aliasing pass can rewrite them to point
        # at constants or other variables' bits — hence ``Bit``.
        self._var_bits: dict[Any, tuple[Bit, ...]] = {}
        # Each variable's *net*: the bits first handed out for it (or the
        # parent's connection for a bound port), which a continuous driver
        # re-points through ``_forward_renames``; ``_var_bits`` holds the
        # value lowered so far, which can be another variable's bits
        # (rtl-buddy-cdc#405: a second ``assign`` of a net renamed the
        # first driver's input to the second's).
        self._net_bits: dict[Any, tuple[Bit, ...]] = {}
        self._ports: dict[str, Port] = {}
        self._cells: dict[str, Cell] = {}
        self._netnames: dict[str, Netname] = {}
        # Element netnames of each unpacked array, by its ``MEMID``
        # spelling: dropped again for an array that turns out to be a
        # memory (see :meth:`build`).
        self._array_netnames: dict[str, list[str]] = {}
        self._cell_counters: dict[str, int] = {}
        # Stack of dotted hierarchy prefixes used by ``_fresh_cell_name``
        # so cells emitted while walking a child instance carry the
        # ``u_child.`` prefix matching Yosys-flatten output. ``""`` at
        # the top level. Push/pop bracket each ``_walk_instance`` call.
        self._hier_stack: list[str] = [""]
        # Active procedural for-loop iteration bindings. Keyed by the
        # loop variable's VariableSymbol identity, value is the current
        # iteration's integer. Consulted by ``_const_int`` so element
        # selects like ``chain[i]`` fold to the per-iteration bit while
        # the for-loop body is being walked. See ``_emit_for_loop``.
        self._loop_bindings: dict[Any, int] = {}
        # The conditions an always_ff walk is under, one per enclosing
        # ``if`` / ``case`` arm, each built on first use (``$not`` of the
        # condition for an ``else`` arm, ...). Only a memory write port
        # reads them, as its ``EN`` (:meth:`_emit_memory_write`); register
        # writes go into the decision tree below.
        self._enable_stack: list[Callable[[], Bit]] = []
        # Per-procedural-block write accumulator (rtl-buddy-cdc#377 /
        # #394): the register bits of each written variable, and the
        # block's decision tree (:class:`_ProcCase`), the writes in walk
        # order with every ``if`` / ``case`` a :class:`_ProcSwitch` of its
        # arms. ``_emit_assignment_expression`` adds to the arm being
        # walked (``_proc_case``) instead of emitting cells;
        # ``_drain_proc_writes`` (called at the end of the block) builds
        # one flop per contiguous run of written bits, its ``D`` the mux
        # tree Yosys ``proc_mux`` builds from the same tree: the first arm
        # of each ``if`` / ``case`` outermost. So ``if (cond) q <= a; else
        # q <= b;`` is one flop whose D is ``mux(cond, b, a)``, and so is
        # ``q <= 0; if (c) q[1] <= b;``. Initialised fresh on each block.
        self._proc_regs: dict[Any, tuple[Bit, ...]] = {}
        self._proc_root = _ProcCase()
        self._proc_case = self._proc_root
        # Per-procedural-block async-reset value accumulator: maps each
        # LHS bit tuple to the integer constant written in the reset
        # arm (``if (!rst_n) q <= 4'd5;`` → 5). Populated by
        # ``_collect_reset_assignments`` from the ifTrue arm and read
        # at drain time to fill the ``$adff`` ``ARST_VALUE`` parameter.
        # Missing entries default to 0 — that's the conservative match
        # for the typical ``q <= '0`` reset and keeps the netlist
        # valid when the reset RHS isn't a recognised literal.
        self._proc_resets: dict[tuple[Bit, ...], tuple[Bit, ...]] = {}
        # Placeholder bit -> RHS bit for every continuously-assigned net,
        # applied to all cells at the end of the walk so procedural reads
        # that precede the ``assign`` in source order see its driver.
        self._forward_renames: dict[Bit, Bit] = {}
        # Next write-port index per memory (``MEMID``) — the ``PORTID``
        # parameter of each emitted ``$memwr_v2`` (#326).
        self._memory_write_ports: dict[str, int] = {}
        # The unpacked arrays Yosys keeps as memories (canonical symbols)
        # and their ``MEMID`` spellings; every other unpacked array is a
        # list of registers (:meth:`_classify_design_arrays`, rtl-buddy-cdc#398).
        self._memories: set[Any] = set()
        self._memory_ids: set[str] = set()
        # Each memory's ``MEMID`` (its declaration's hierarchical name).
        self._memory_id_of: dict[Any, str] = {}
        # Clock of the clocked block being lowered: a memory write in its
        # reset arm (:meth:`_collect_reset_assignments`) is a port on it.
        self._proc_clk: Any = None
        # ``(location, reason)`` of every assignment already reported by
        # :meth:`_warn_lvalue_dropped` (rtl-buddy-cdc#370).
        self._lvalue_warned: set[tuple[str | None, str]] = set()
        # Every bit :meth:`_alloc_bits` handed to a variable: the net a
        # reader sees until the variable's driver is known. Only these are
        # re-pointed by a forward rename (rtl-buddy-cdc#377).
        self._placeholder_bits: set[int] = set()
        # Branch-merge ``$mux`` cells of the combinational body being
        # lowered, by output bits: ``(S, A, B)``. Read by
        # :meth:`_infer_latches` (rtl-buddy-cdc#377).
        # Function inlining (rtl-buddy-cdc#377): nesting depth, the return
        # variable of each function being inlined, and a count of the
        # constructs any walk failed to lower, which an inlined body must
        # not add to.
        self._inline_depth = 0
        self._inline_returns: list[Any] = []
        # Target of each compound assignment (``x += 1``) whose right-hand
        # side is being lowered: what its ``LValueReferenceExpression``
        # reads.
        self._compound_lhs: list[Any] = []
        self._lowering_failures = 0
        # The instance bodies this builder inlines (rtl-buddy-cdc#405): a
        # hierarchical reference (``gr.lm[i]``, ``u_child.q``, an upward
        # ``top.sig``) resolves to its target's bits when the target is
        # declared in one of them, and is reported otherwise (a name
        # inside a blackbox, or above a greybox body lowered on its own).
        self._inlined_bodies: set[Any] = set()
        self._branch_muxes: dict[
            tuple[Bit, ...], tuple[Bit, tuple[Bit, ...], tuple[Bit, ...]]
        ] = {}

    # -- public ----------------------------------------------------------

    def build(self) -> Module:
        # The top instance's ports become the flat ``Module``'s ports;
        # child instances contribute flops / comb / netnames into the
        # same flat namespace, hierarchically prefixed (``u_b0.q``).
        self._classify_design_arrays()
        self._walk_instance(self.top, hier_prefix="", bound_internals=frozenset())
        self._apply_forward_renames()
        # A memory has no netname in Yosys' output (rtl-buddy-cdc#394,
        # #398): only an array lowered to registers names its elements.
        for memid in self._memory_ids:
            for name in self._array_netnames.get(memid, ()):
                self._netnames.pop(name, None)
        return Module(
            name=self.top.name,
            ports=self._ports,
            cells=self._cells,
            netnames=self._netnames,
        )

    def _walk_instance(
        self,
        inst: Any,
        hier_prefix: str,
        bound_internals: frozenset[Any],
    ) -> None:
        """Walk an :class:`InstanceSymbol`'s body in three passes —
        variables, ports (top only), then cells / continuous assigns /
        child instances.

        ``hier_prefix`` is the dotted instance path (``""`` at the top,
        ``"u_a."`` one level in, ``"u_top.u_a."`` two levels) used to
        namespace netname keys and emitted cell names. ``bound_internals``
        is the set of port-internal variables this instance has already
        had its bits aliased to a parent expression — they get skipped
        in pass 1 so we don't allocate fresh bits for them.
        """
        self._hier_stack.append(hier_prefix)
        try:
            for member, prefix in self._iter_scope(inst.body, hier_prefix):
                if (
                    self._kind_name(member) in self._NAMED_STORAGE
                    and member not in bound_internals
                ):
                    self._collect_variable(member, prefix)
            # Only the top module exposes ports in the flat ``Module``;
            # child ports get folded into their parents' connections.
            # Ports always live directly on the instance body (SV
            # disallows declaring them inside generates) so no
            # scope-flattening is needed for this pass.
            if hier_prefix == "":
                for member in inst.body:
                    self._collect_port(member)
            for member, prefix in self._iter_scope(inst.body, hier_prefix):
                # ``_fresh_cell_name`` reads ``_hier_stack[-1]`` when
                # naming cells; for members inside a generate block
                # that needs to include the ``g_label[i].`` segment so
                # the emitted name matches Yosys-flatten.
                self._hier_stack.append(prefix)
                try:
                    self._emit_for_member(member, prefix)
                finally:
                    self._hier_stack.pop()
        finally:
            self._hier_stack.pop()

    def _iter_scope(self, container: Any, hier_prefix: str):
        """Yield ``(member, prefix)`` for every emittable member of a
        scope, recursively expanding SV ``generate`` blocks.

        pyslang exposes two generate kinds:

        - :class:`GenerateBlockSymbol` — one instantiated generate
          body (an unconditional ``generate ... endgenerate``, or the
          taken branch of ``generate if`` / ``generate case``).
          Iterating the symbol yields its inner members. Untaken
          branches are usually pruned from the parent body already,
          but ``isUninstantiated`` is checked as defense-in-depth.
        - :class:`GenerateBlockArraySymbol` — a labeled
          ``for (genvar ...)`` array; its ``.entries`` list contains
          one :class:`GenerateBlockSymbol` per iteration, each
          carrying ``arrayIndex``.

        Naming follows Yosys-flatten convention: ``g_label[i].`` for
        array entries, ``g_label.`` for named single blocks, no
        segment for anonymous blocks. Stable across the two frontends
        so the rule pack's path reporting and any downstream waiver
        regexes don't have to branch on the frontend.
        """
        for member in container:
            kind = self._kind_name(member)
            if kind == "GenerateBlockArraySymbol":
                array_name = getattr(member, "name", "") or ""
                for entry in getattr(member, "entries", []) or []:
                    if getattr(entry, "isUninstantiated", False):
                        continue
                    idx = getattr(entry, "arrayIndex", None)
                    if array_name and idx is not None:
                        inner_prefix = f"{hier_prefix}{array_name}[{idx}]."
                    else:
                        inner_prefix = hier_prefix
                    yield from self._iter_scope(entry, inner_prefix)
            elif kind == "GenerateBlockSymbol":
                if getattr(member, "isUninstantiated", False):
                    continue
                name = getattr(member, "name", "") or ""
                inner_prefix = f"{hier_prefix}{name}." if name else hier_prefix
                yield from self._iter_scope(member, inner_prefix)
            else:
                yield member, hier_prefix

    # -- helpers ---------------------------------------------------------

    def _alloc_bits(self, var_sym: Any) -> tuple[Bit, ...]:
        """Allocate (or recall) bit refs for ``var_sym``.

        Ports and variables that share an underlying storage symbol
        (i.e. a port and its corresponding internal ``logic``) must
        return the same bit tuple. ``_canonical_var`` resolves that
        aliasing before we hit the cache.
        """
        var_sym = self._canonical_var(var_sym)
        cached = self._var_bits.get(var_sym)
        if cached is not None:
            return cached
        width = self._width_of(var_sym)
        ids = range(self._next_bit_id, self._next_bit_id + width)
        self._next_bit_id += width
        self._placeholder_bits.update(ids)
        bits: tuple[Bit, ...] = tuple(ids)
        self._var_bits[var_sym] = bits
        self._net_bits[var_sym] = bits
        return bits

    def _src_attr(self, node: Any) -> str | None:
        """Format ``node``'s pyslang source range as a Yosys-style
        ``"file:line.col-line.col"`` string for ``Cell.attributes["src"]``.

        Yosys's flatten output puts this attribute on every emitted
        cell so JSON / SARIF reporters can surface a clickable source
        location. Matching the same format means the reporter doesn't
        need a frontend-specific branch.

        Three places we look, in priority order:
        1. ``node.syntax.sourceRange`` — the full SV-level range
           (e.g. ``always_ff`` to the end of the body). This is what
           we want; ProceduralBlockSymbol and ContinuousAssignSymbol
           don't have a useful ``sourceRange`` directly, but their
           syntax does.
        2. ``node.sourceRange`` — Expression-level nodes (binary /
           unary / conditional) carry this directly.
        3. ``node.location`` — a single point. Falls back to a
           degenerate ``line.col-line.col`` range.

        Returns ``None`` when the node has no usable location at all
        — non-fatal: the cell still emits without the attribute
        (matching Yosys's behaviour for sourceless transformations).
        """
        sm = self.comp.sourceManager

        def _fmt_range(start: Any, end: Any) -> str | None:
            try:
                fn = sm.getFileName(start)
                s_ln = sm.getLineNumber(start)
                s_col = sm.getColumnNumber(start)
                e_ln = sm.getLineNumber(end)
                e_col = sm.getColumnNumber(end)
            except Exception:
                return None
            if not fn:
                return None
            return f"{fn}:{s_ln}.{s_col}-{e_ln}.{e_col}"

        syntax = getattr(node, "syntax", None)
        if syntax is not None:
            sr = getattr(syntax, "sourceRange", None)
            if sr is not None:
                fmt = _fmt_range(sr.start, sr.end)
                if fmt is not None:
                    return fmt
        sr = getattr(node, "sourceRange", None)
        if sr is not None:
            fmt = _fmt_range(sr.start, sr.end)
            if fmt is not None:
                return fmt
        loc = getattr(node, "location", None)
        if loc is not None:
            return _fmt_range(loc, loc)
        return None

    @staticmethod
    def _canonical_var(sym: Any) -> Any:
        """Resolve a port symbol to its backing variable symbol.

        pyslang represents a port and its body-level ``logic`` as two
        separate :class:`Symbol`s connected by ``PortSymbol.internalSymbol``.
        We canonicalise to the internal variable so the bit-id cache
        keys on a single object identity.
        """
        internal = getattr(sym, "internalSymbol", None)
        if internal is not None:
            return internal
        return sym

    @staticmethod
    def _width_of(var_sym: Any) -> int:
        t = getattr(var_sym, "type", None)
        if t is None:
            return 1
        # ``selectableWidth`` is the total number of bits the type
        # spans, including any unpacked dimensions. pyslang reports
        # ``bitWidth = 0`` on unpacked-array types and the full
        # storage count on ``selectableWidth``; for packed types and
        # scalars the two agree, so this is the right number to use
        # uniformly. Non-integral types (interfaces, modports) report
        # 0 / no attribute — fall back to 1 as a placeholder that
        # surfaces as a malformed cell downstream rather than
        # crashing the build.
        return int(
            getattr(t, "selectableWidth", None) or getattr(t, "bitWidth", None) or 1
        )

    def _fresh_cell_name(self, type_str: str) -> str:
        # Yosys autogen names look like "$procdff$3" — we don't need
        # bit-exact parity, but a stable prefix-and-counter keeps
        # waiver regexes that target the cell name readable. The
        # current hierarchy prefix (e.g. ``u_b0.``) matches what
        # Yosys ``flatten`` would produce for cells inside a child
        # instance.
        n = self._cell_counters.get(type_str, 0) + 1
        self._cell_counters[type_str] = n
        prefix = self._hier_stack[-1] if self._hier_stack else ""
        return f"{prefix}$slang${type_str.strip('$')}${n}"

    def _kind_name(self, sym: Any) -> str:
        """Class-name shortcut. pyslang exposes ``__class__.__name__``
        and a ``kind`` enum; the class name is sufficient and avoids
        having to thread the enum import through every comparison."""
        return type(sym).__name__

    def _is_named(self, expr: Any) -> bool:
        """``expr`` names a variable or net this builder lowers: a plain
        name, or a hierarchical one (``gr.lm``, ``g[1].x``, ``u_child.q``,
        an upward ``top.sig``) whose target is declared in an instance
        body this builder inlines, so it reads and writes the target's
        own bits (rtl-buddy-cdc#405: it was an opaque driver / a dropped
        write). A hierarchical name of a constant reads its value."""
        kind = type(expr).__name__
        if kind == "NamedValueExpression":
            return True
        if kind != "HierarchicalValueExpression":
            return False
        sym = getattr(expr, "symbol", None)
        if sym is None:
            return False
        if self._kind_name(sym) in self._CONSTANT_SYMBOLS:
            return True
        scope = getattr(sym, "parentScope", None)
        return getattr(scope, "containingInstance", None) in self._inlined_bodies

    # -- pass 1: variables ----------------------------------------------

    # Declarations that get a netname: variables (``logic`` / ``reg``) and
    # nets (``wire`` and the other net types, a port declared as one, an
    # implicit net). Yosys names both (rtl-buddy-cdc#384: nets had no
    # netname here, so a waiver or an SDC ``get_nets`` naming a wire, and
    # an attribute on one, matched only on the Yosys path).
    _NAMED_STORAGE = frozenset({"VariableSymbol", "NetSymbol"})

    def _collect_variable(self, member: Any, hier_prefix: str = "") -> None:
        if self._kind_name(member) not in self._NAMED_STORAGE:
            return
        bits = self._alloc_bits(member)
        # Forward declaration-site attributes. pyslang stores them on
        # the :class:`Compilation`, not the symbol — ``getAttributes``
        # returns the AttributeSymbols whose scope contains ``member``.
        # The rule pack only cares about the attribute **name**
        # (``cdc_sync`` / ``cdc_gray`` / ``async_reg`` / …); the value
        # is rendered as its syntax text for round-tripping but isn't
        # consulted by any rule today.
        attrs: dict[str, str] = {}
        for attr in self.comp.getAttributes(member) or []:
            name = getattr(attr, "name", None)
            if not name:
                continue
            val = getattr(attr, "value", None)
            attrs[name] = str(val) if val is not None else "1"
        # Hierarchical name matches Yosys-flatten output: ``u_b0.q``,
        # ``u_top.u_b0.q``, etc. At the top level ``hier_prefix`` is
        # empty so the bare name is used. The rule pack consults
        # netname attributes via the bits→netname reverse-lookup, so
        # the name is only consulted for debug / source-location use.
        full_name = f"{hier_prefix}{member.name}"
        elements = self._unpacked_elements(member)
        if elements is None:
            offset, upto = self._declared_range(member)
            self._netnames[full_name] = Netname(
                name=full_name, bits=bits, attributes=attrs, offset=offset, upto=upto
            )
            return
        # An unpacked array is one netname per element, as Yosys
        # ``mem2reg`` names them (rtl-buddy-cdc#394: one netname over the
        # whole array, so ``get_nets a[1]`` named bit 1 of it, and a
        # report named the array where Yosys names the element). Each
        # element carries the declaration's attributes.
        offset, upto = elements[0]
        names: list[str] = []
        for label, lo, width in elements[1]:
            name = f"{full_name}[{label}]"
            names.append(name)
            self._netnames[name] = Netname(
                name=name,
                bits=bits[lo : lo + width],
                attributes=dict(attrs),
                offset=offset,
                upto=upto,
            )
        self._array_netnames[f"\\{full_name}"] = names

    def _unpacked_elements(
        self, member: Any
    ) -> tuple[tuple[int, bool], list[tuple[int, int, int]]] | None:
        """The element netnames of an unpacked-array declaration, as Yosys
        ``mem2reg`` emits them: ``((offset, upto) of the element type,
        [(name index, first bit, width)])``; ``None`` for anything else.

        A one-dimensional array names each element by its declared index
        (``a[4]`` / ``a[5]`` of ``a [5:4]``). A multi-dimensional one is
        flattened, row-major, each dimension counted from its left bound:
        ``m[1][0]`` of ``m [1:0][2:0]`` is ``m[0]``, ``m[0][2]`` is
        ``m[5]``. This frontend stores every unpacked dimension lowest
        index first (:meth:`_select_position`)."""
        t = getattr(member, "type", None)
        t = getattr(t, "canonicalType", None) or t
        dims: list[tuple[int, int]] = []
        while getattr(t, "isUnpackedArray", False):
            rng = getattr(t, "range", None)
            left = getattr(rng, "left", None)
            right = getattr(rng, "right", None)
            if left is None or right is None:
                return None
            dims.append((int(left), int(right)))
            t = getattr(t, "elementType", None)
            t = getattr(t, "canonicalType", None) or t
        if not dims or t is None:
            return None
        width = int(
            getattr(t, "selectableWidth", None) or getattr(t, "bitWidth", None) or 0
        )
        if width <= 0:
            return None
        sizes = [abs(left - right) + 1 for left, right in dims]
        elements: list[tuple[int, int, int]] = []
        for flat in range(math.prod(sizes)):
            # ``flat``: the element's Yosys address (positions from the
            # left bound); ``pos``: its place in this frontend's storage.
            pos, rest = 0, flat
            index = 0
            for d in reversed(range(len(dims))):
                step = rest % sizes[d]
                rest //= sizes[d]
                left, right = dims[d]
                index = left - step if left > right else left + step
                pos += (index - min(left, right)) * math.prod(sizes[d + 1 :])
            label = index if len(dims) == 1 else flat
            elements.append((label, pos * width, width))
        return self._type_range(t), elements

    @staticmethod
    def _declared_range(member: Any) -> tuple[int, bool]:
        """``(offset, upto)`` of a declaration, as Yosys ``write_json``
        emits them on its netname (rtl-buddy-cdc#390: always ``(0,
        False)``, so ``get_nets x[k]`` on a ``[7:4]`` or ``[0:7]`` vector
        named another bit than on the Yosys path): the lower bound of a
        one-dimensional packed range and whether it is ascending. Yosys
        flattens anything else (a multi-dimensional packed array, an
        ``integer``, a struct) to ``[N-1:0]``."""
        return _ModuleBuilder._type_range(getattr(member, "type", None))

    @staticmethod
    def _type_range(t: Any) -> tuple[int, bool]:
        """:meth:`_declared_range` of a type."""
        t = getattr(t, "canonicalType", None) or t
        if not getattr(t, "isPackedArray", False):
            return 0, False
        element = getattr(t, "elementType", None)
        element = getattr(element, "canonicalType", None) or element
        rng = getattr(t, "range", None)
        if rng is None or getattr(element, "isPackedArray", True):
            return 0, False
        return int(rng.lower), int(rng.left) < int(rng.right)

    # -- pass 2: ports ---------------------------------------------------

    def _collect_port(self, member: Any) -> None:
        if self._kind_name(member) != "PortSymbol":
            return
        direction = _port_direction(member)
        internal = getattr(member, "internalSymbol", None)
        if internal is None:
            return  # nothing to wire to
        bits = self._alloc_bits(internal)
        self._ports[member.name] = Port(
            name=member.name, direction=direction, bits=bits
        )
        # Forward attributes declared on the port itself
        # (``(* cdc_sync *) output logic q``) onto the netname tied to
        # the port's internal variable. pyslang stores them on the
        # ``PortSymbol``; ``_collect_variable`` only sees the underlying
        # ``VariableSymbol`` and would otherwise miss them. Yosys collapses
        # port + variable attributes onto the same netname, so doing
        # the same here keeps the two frontends symmetric for the rule
        # pack's attribute-aware checks.
        netname = self._netnames.get(internal.name)
        if netname is None:
            return
        for attr in self.comp.getAttributes(member) or []:
            name = getattr(attr, "name", None)
            if not name:
                continue
            val = getattr(attr, "value", None)
            netname.attributes[name] = str(val) if val is not None else "1"

    # -- pass 3: cells + continuous assigns -----------------------------

    def _emit_for_member(self, member: Any, hier_prefix: str = "") -> None:
        kind = self._kind_name(member)
        if kind == "ProceduralBlockSymbol":
            self._emit_procedural_block(member, hier_prefix)
        elif kind == "ContinuousAssignSymbol":
            self._emit_continuous_assign(member)
        elif kind == "InstanceSymbol":
            self._emit_instance(member, hier_prefix, member.name)
        elif kind == "InstanceArraySymbol":
            self._emit_instance_array(member, hier_prefix, member.name)
        elif kind == "PrimitiveInstanceSymbol":
            self._emit_primitive(member)
        elif kind == "NetSymbol":
            # ``wire foo = <expr>;`` lowers as a NetSymbol whose
            # ``initializer`` carries the RHS. pyslang doesn't emit a
            # separate ContinuousAssignSymbol for this shape, so without
            # this branch the inline initializer is silently dropped
            # — the rule pack then sees the net's bits driven by
            # nothing, which masks inter-stage comb hazards (CDC-014).
            self._emit_net_initializer(member)
        # GenerateBlock(Array)Symbol are pre-expanded by ``_iter_scope``
        # before reaching this dispatch. Every other kind (parameters,
        # typedefs, variables — collected in pass 1 —, ports, …) carries
        # no logic of its own and falls through; a logic-bearing construct
        # this frontend cannot lower warns via ``_warn_dropped`` instead.

    def _emit_net_initializer(self, member: Any) -> None:
        """Lower a ``wire foo = <expr>;`` inline assignment.

        Slang's :class:`NetSymbol` exposes the RHS via ``.initializer``.
        We lower it via :meth:`_bits_of_expression` and alias the net's
        own bits to the RHS bits — exact same shape
        :meth:`_emit_continuous_assign` produces for the explicit
        ``assign foo = <expr>;`` form. The two paths converge in
        the underlying ``$not`` / ``$and`` / `$mux`` / … cell emission,
        so the rule pack treats both spellings identically.

        Closes the CDC-014 inter-stage-comb-preservation leg of the
        cross-frontend parity gap tracked in rtl-buddy-cdc#221 /
        rtl-buddy-cdc#224.
        """
        init_expr = getattr(member, "initializer", None)
        if init_expr is None:
            return
        rhs_bits = self._rhs_bits(init_expr, len(self._alloc_bits(member)))
        # The same driver ``_emit_continuous_assign`` records, so any
        # reader of the net (earlier in source order too,
        # rtl-buddy-cdc#377) follows through to the driving cell's output.
        self._drive_net(member, 0, rhs_bits, member)

    def _emit_instance(self, inst: Any, parent_prefix: str, name: str) -> None:
        """Lower one module instance named ``name`` under ``parent_prefix``:
        a boundary cell for a blackbox / greybox module, otherwise its
        body inlined."""
        if self._is_boundary_instance(inst):
            self._emit_boundary_instance(inst, parent_prefix, name)
        else:
            self._emit_child_instance(inst, parent_prefix, name)

    def _emit_instance_array(self, arr: Any, parent_prefix: str, name: str) -> None:
        """Lower an array of instances (``sync2 u_sync[1:0] (...)``) as one
        instance per element (rtl-buddy-cdc#353).

        Element ``k`` is named ``<name>[k]``, the name Yosys ``flatten``
        gives it, so cells land under ``u_sync[0].`` like the entries of a
        ``for``-generate do. A multi-dimensional array is an array of
        arrays in pyslang; its elements are named ``u_m[1][0]``.

        Port connections need no work here: slang already resolves each
        element's connection by the LRM rule (IEEE 1800-2017 23.3.3.5) —
        an expression of the port's width goes to every element, one of
        ``port width * element count`` bits is split so the *leftmost*
        index of the array range gets the leftmost (most significant)
        bits — and hands every element its own sliced expression. Yosys
        ``read_verilog`` follows that rule for a descending range
        (``[1:0]``) but, for an ascending one (``[0:1]``), gives the
        lowest index the least significant slice; this frontend keeps the
        LRM order, so on an ascending array the two frontends attach the
        same bits to opposite element names.
        """
        if getattr(arr, "isUninstantiated", False):
            return
        rng = getattr(arr, "range", None)
        lower = getattr(rng, "lower", None)
        elements = list(getattr(arr, "elements", None) or [])
        if lower is None or not elements:
            self._warn_dropped(arr, "no elaborated elements", "instance array")
            return
        # pyslang lists the elements from the lowest index up, whatever
        # the declared direction (``[1:0]`` and ``[0:1]`` both start at 0).
        for offset, element in enumerate(elements):
            elem_name = f"{name}[{int(lower) + offset}]"
            kind = self._kind_name(element)
            if kind == "InstanceSymbol":
                self._emit_instance(element, parent_prefix, elem_name)
            elif kind == "PrimitiveInstanceSymbol":
                # slang hands each element its own slice of a vector
                # terminal (IEEE 1800-2017 28.3.6).
                self._emit_primitive(element)
            elif kind == "InstanceArraySymbol":
                self._emit_instance_array(element, parent_prefix, elem_name)
            else:
                self._warn_dropped(
                    element,
                    f"element {elem_name} is a {kind}, not a module instance",
                    "instance array element",
                )

    def _emit_child_instance(self, child: Any, parent_prefix: str, name: str) -> None:
        """Inline a child instance's body into the flat ``Module``.

        Port connections are the load-bearing wiring step: the child's
        internal variables backing each port get their bits aliased to
        the parent's connection expression so net identity is preserved
        across the hierarchy boundary. After that, the child body is
        walked exactly like the top. ``name`` is the instance's name in
        the parent (``child.name``, or ``u_sync[0]`` for an array
        element, whose own ``name`` is empty).
        """
        bound: set[Any] = set()
        for pc in getattr(child, "portConnections", []) or []:
            port = pc.port
            internal = getattr(port, "internalSymbol", None)
            if internal is None:
                continue
            parent_bits = self._port_connection_bits(pc)
            if parent_bits is None:
                continue
            # Alias the child's internal-variable bits to the parent's
            # wire. ``_alloc_bits`` keys on the internal Symbol identity
            # which is unique per elaborated instance, so two
            # instantiations of the same module don't share bits.
            canonical = self._canonical_var(internal)
            early = self._var_bits.get(canonical)
            if early is not None:
                # A hierarchical read of the port (``u_child.a``) lowered
                # before this instance (rtl-buddy-cdc#405): its readers
                # see the parent's net.
                for o, n in zip(early, parent_bits):
                    if o in self._placeholder_bits and o != n:
                        self._forward_renames[o] = n
            self._var_bits[canonical] = parent_bits
            self._net_bits[canonical] = parent_bits
            bound.add(internal)
            # Also emit a netname for the port at ``<child_prefix><port_name>``
            # so SDC pin paths like ``[get_pins u_a/clk_out_b0]`` resolve
            # in domain.py's ``_build_bit_to_clock``. Yosys-flatten +
            # opt_clean preserves both the port netname and the driving
            # variable's netname as aliases of the same bits; without
            # this, ``create_generated_clock`` declarations on internal
            # pins are silently dropped (see issue #15).
            #
            # Bits are the parent-side connection bits at this point; if
            # the body's continuous-assign aliasing later rewrites them
            # (e.g. ``assign clk_out = div`` collapses both to ``div``'s
            # freshly-allocated bits), ``_rewrite_aliased`` walks
            # ``_netnames`` and updates this entry too.
            port_netname = f"{parent_prefix}{name}.{port.name}"
            offset, upto = self._declared_range(internal)
            self._netnames[port_netname] = Netname(
                name=port_netname,
                bits=parent_bits,
                attributes={},
                offset=offset,
                upto=upto,
            )

        child_prefix = f"{parent_prefix}{name}."
        self._walk_instance(
            child, hier_prefix=child_prefix, bound_internals=frozenset(bound)
        )

    def _port_connection_bits(self, pc: Any) -> tuple[Bit, ...] | None:
        """Resolve a :class:`PortConnection` to the parent-side bits.

        Three shapes appear in practice:
        - **Input port**: ``pc.expression`` is the parent-side driver
          expression (typically a :class:`NamedValueExpression`).
          Just lower it.
        - **Output port**: ``pc.expression`` is an
          :class:`AssignmentExpression` whose ``.left`` is the
          parent's capturing variable. ``.right`` is an
          :class:`EmptyArgumentExpression` placeholder. Use the LHS.
        - **Unconnected port**: ``pc.expression`` is None — nothing
          to alias to. Return None so the child's internal allocates
          its own fresh bits.
        """
        ex = pc.expression
        if ex is None:
            return None
        kind = type(ex).__name__
        if kind == "AssignmentExpression":
            bits = self._lvalue_bits(ex.left)
            if bits is None and type(ex.left).__name__ != "EmptyArgumentExpression":
                # An output bound to something with no bits here: the
                # child's driver would be cut off from the parent net.
                self._warn_dropped(
                    ex,
                    self._lvalue_unsupported_reason(ex.left),
                    f"output port connection `{pc.port.name}`",
                )
            return bits
        if kind == "EmptyArgumentExpression":
            return None
        return self._bits_of_expression(ex) or self._opaque(
            ex, self._expr_width(ex), f"input port connection `{pc.port.name}`"
        )

    # -- blackbox boundaries (rtl-buddy-cdc#345) --------------------------

    # Built-in gates (IEEE 1800-2017 28.4/28.5) by the cell their
    # inputs fold through and whether the result is inverted, as Yosys'
    # ``read_verilog`` lowers them.
    _GATE_CELLS: dict[str, tuple[str | None, bool]] = {
        "and": ("$and", False),
        "or": ("$or", False),
        "xor": ("$xor", False),
        "nand": ("$and", True),
        "nor": ("$or", True),
        "xnor": ("$xor", True),
        "buf": (None, False),
        "not": (None, True),
    }

    def _emit_primitive(self, prim: Any) -> None:
        """Lower a built-in gate instance (``and g (y, a, b, c);``,
        ``not n (y1, y2, a);``) as Yosys ``read_verilog`` does: the inputs
        folded left to right through 2-input ``$and`` / ``$or`` / ``$xor``
        cells, a ``$not`` for the inverting gates, every output driven
        like a continuous ``assign`` (rtl-buddy-cdc#377; gates were
        reported since rtl-buddy-cdc#353 but not lowered). The delay and
        strengths have no logic. Tristate gates, switches, pulls and
        user-defined primitives are reported, as before."""
        name = getattr(getattr(prim, "primitiveType", None), "name", "")
        conns = list(getattr(prim, "portConnections", None) or [])
        spec = self._GATE_CELLS.get(name)
        if spec is None or len(conns) < 2:
            self._warn_dropped(
                prim,
                f"`{name or '?'}` primitive {prim.name or '<unnamed>'} is not modelled",
                "primitive instance",
            )
            return
        cell, invert = spec
        if cell is None:  # buf / not: n outputs, one input
            outputs, inputs = conns[:-1], conns[-1:]
        else:  # and / or / xor / nand / nor / xnor: one output, n inputs
            outputs, inputs = conns[:1], conns[1:]
        bits = [self._rhs_bits(e, 1, "gate input")[0] for e in inputs]
        value: Bit = bits[0]
        for nxt in bits[1:]:
            assert cell is not None
            value = self._emit_comb_cell(
                cell, {"A": (value,), "B": (nxt,)}, output_width=1, src_node=prim
            )[0]
        if invert:
            value = self._emit_comb_cell(
                "$not", {"A": (value,)}, output_width=1, src_node=prim
            )[0]
        for out in outputs:
            target = getattr(out, "left", None)
            if type(out).__name__ != "AssignmentExpression" or target is None:
                continue
            if not self._splice_assign(target, _Bits((value,), out), continuous=True):
                self._warn_lvalue_dropped(prim, target)

    def _is_boundary_instance(self, inst: Any) -> bool:
        """True iff ``inst`` stays a boundary cell instead of being inlined.

        The pyslang counterparts of what the Yosys path keys on: a truthy
        ``(* blackbox *)`` attribute on the module declaration (what
        ``read_verilog`` honours), or the module named in ``--blackbox`` /
        ``--greybox`` (``read_slang --blackboxed-module`` / ``setattr -mod
        -set blackbox 1``). An undefined module is not a blackbox on
        either path: both fail elaboration.
        """
        defn = getattr(inst, "definition", None)
        name = getattr(defn, "name", None)
        if name is None:
            return False
        if name in self._blackbox_names or name in self._greybox_names:
            return True
        for attr in self.comp.getAttributes(defn) or []:
            if getattr(attr, "name", None) != "blackbox":
                continue
            val = getattr(attr, "value", None)
            is_true = getattr(val, "isTrue", None)
            return bool(is_true()) if callable(is_true) else True
        return False

    def _emit_boundary_instance(self, inst: Any, parent_prefix: str, name: str) -> None:
        """Emit ``inst`` as one boundary cell, Yosys-shaped (#345).

        Mirrors what ``read_verilog`` + ``flatten`` leave for an instance
        of a blackbox module: a cell named by its hierarchical instance
        path whose ``type`` is the module name, ``connections`` keyed by
        port name with the parent-side bits, and ``parameters`` holding
        only the parameters the instantiation overrides (so CDC-022 falls
        back to the XPM default for an omitted ``DEST_SYNC_FF``, exactly
        as on the Yosys path). The module itself is recorded once as a
        sibling :class:`Module` with ``is_blackbox`` set: port directions
        and widths only, or for a ``--greybox`` module its lowered body.
        """
        defn_name = inst.definition.name
        connections: dict[str, tuple[Bit, ...]] = {}
        for pc in getattr(inst, "portConnections", []) or []:
            # An empty ``.io()`` stays as a zero-width connection, as in
            # the Yosys netlist.
            bits = self._port_connection_bits(pc)
            connections[pc.port.name] = bits if bits is not None else ()
        parameters: dict[str, str] = {}
        for member in inst.body:
            if self._kind_name(member) != "ParameterSymbol":
                continue
            if not getattr(member, "isOverridden", False):
                continue
            encoded = _encode_param(getattr(member, "value", None))
            if encoded is not None:
                parameters[member.name] = encoded
        attrs: dict[str, str] = {}
        src = self._src_attr(inst)
        if src is not None:
            attrs["src"] = src
        # Pin directions, as ``write_json`` records them: a boundary
        # cell's output pins are drivers, not readers (rtl-buddy-cdc#344).
        port_directions = {
            member.name: _port_direction(member)
            for member in inst.body
            if self._kind_name(member) == "PortSymbol" and member.name in connections
        }
        cell_name = f"{parent_prefix}{name}"
        self._cells[cell_name] = Cell(
            name=cell_name,
            type=defn_name,
            connections=connections,
            parameters=parameters,
            attributes=attrs,
            port_directions=port_directions,
        )
        if defn_name in self._siblings:
            return
        if defn_name in self._greybox_names:
            body = _ModuleBuilder(
                inst,
                self.comp,
                self.pyslang,
                blackbox_names=self._blackbox_names,
                greybox_names=self._greybox_names,
                siblings=self._siblings,
            ).build()
            self._siblings[defn_name] = Module(
                name=defn_name,
                ports=body.ports,
                cells=body.cells,
                netnames=body.netnames,
                is_blackbox=True,
            )
            return
        # Bodyless: the port list is the whole module. Bits are local to
        # the sibling (Yosys numbers each module's nets from 2 too).
        ports: dict[str, Port] = {}
        netnames: dict[str, Netname] = {}
        next_bit = 2
        for member in inst.body:
            if self._kind_name(member) != "PortSymbol":
                continue
            target = getattr(member, "internalSymbol", None) or member
            width = self._width_of(target)
            port_bits: tuple[Bit, ...] = tuple(range(next_bit, next_bit + width))
            next_bit += width
            ports[member.name] = Port(
                name=member.name, direction=_port_direction(member), bits=port_bits
            )
            netnames[member.name] = Netname(name=member.name, bits=port_bits)
        self._siblings[defn_name] = Module(
            name=defn_name,
            ports=ports,
            cells={},
            netnames=netnames,
            is_blackbox=True,
        )

    # -- always_ff lowering ----------------------------------------------

    def _emit_procedural_block(self, block: Any, hier_prefix: str = "") -> None:
        # hier_prefix is read indirectly through ``_hier_stack`` which
        # the walker keeps in sync via the ``_walk_instance`` push/pop;
        # accept it on the signature for symmetry with the other
        # ``_emit_*`` callbacks.
        del hier_prefix
        kind_name = str(block.procedureKind).rsplit(".", 1)[-1]
        if kind_name == "AlwaysComb":
            self._emit_always_comb(block)
            return
        if kind_name == "AlwaysLatch":
            self._emit_always_latch(block)
            return
        if kind_name == "Always":
            self._emit_plain_always(block)
            return
        if kind_name != "AlwaysFF":
            return  # initial / final — not modelled
        self._emit_always_ff(block)

    def _emit_always_ff(self, block: Any) -> None:
        body = block.body
        # Expected shape: TimedStatement(timing=EventListControl, stmt=...)
        if self._kind_name(body) != "TimedStatement":
            self._warn_dropped(block, "no leading event control")
            return
        events = self._event_signals(body.timing)
        if events is not None and len(events) > 2:
            # A clock and two or more async controls (rtl-buddy-cdc#384).
            self._emit_multi_async_ff(block, body, events)
            return
        clk_sym, reset_sym, reset_active_low, clk_negedge = self._analyse_event_list(
            body.timing
        )
        if clk_sym is None:
            # can't identify clock — skip rather than crash
            self._warn_dropped(block, "clock is not a plain signal")
            return
        clk_sym, reset_sym, reset_active_low, clk_negedge = self._order_clock_and_reset(
            body.stmt, clk_sym, reset_sym, reset_active_low, clk_negedge
        )
        # Stash the clock edge as instance state so ``_emit_flop_cell``
        # can read it without threading a polarity parameter through
        # every ``_emit_assignments_*`` helper. The save/restore in the
        # surrounding try/finally keeps nested always_ff blocks correct
        # (an unusual but legal SV shape).
        prev_clk_negedge = getattr(self, "_clk_negedge", False)
        self._clk_negedge = clk_negedge
        prev_proc_clk = self._proc_clk
        self._proc_clk = clk_sym
        # Drill through the optional BlockStatement wrapper.
        inner = body.stmt
        if self._kind_name(inner) == "BlockStatement":
            inner = inner.body
        # Two canonical shapes:
        #   (a) ConditionalStatement: if (<reset_check>) q <= 0;
        #                             else                q <= d;
        #   (b) ExpressionStatement: q <= d;   (no async reset)
        # The whole always_ff block is the natural source-range anchor
        # for every flop cell it produces — matches Yosys's $procdff
        # ``src`` attribute, which spans the block too.
        src_node = block
        # Fresh write accumulator per procedural block. Drained at the
        # end so one flop emits per LHS even when multiple arms write
        # the same target (the if/else both-arms case).
        self._reset_proc_tree()
        self._proc_resets = {}
        prev_blocking = self._blocking_nets
        self._blocking_nets = {}
        block_reset_sym = reset_sym
        block_reset_active_low = reset_active_low
        block_reset_kind: str | None = None
        try:
            reset_check = self._classify_reset_check(inner, reset_sym, reset_active_low)
            if reset_check is not None:
                # Canonical reset-check shape — either async-reset
                # ``always_ff @(posedge clk or negedge rst_n) begin
                #     if (!rst_n) <reset_assigns> else <data> end``
                # or its sync-reset cousin
                # ``always_ff @(posedge clk) begin
                #     if (!rst_n) <reset_assigns> else <data> end``
                # In both, the reset arm's writes are absorbed into
                # the flop's reset value rather than emitted as
                # separate writes (we only walk ``ifFalse``, the data
                # branch). For async reset, the reset signal and
                # polarity come from the event list; for sync reset
                # the shape is identified heuristically (constant-only
                # ifTrue arm) and the polarity is read off the
                # condition itself (``!rst_n`` vs bare ``rst``).
                # Falling outside that envelope (a runtime ``if (cond)``
                # at the top of a no-reset always_ff, where the ifTrue
                # arm computes a real value) flows into the general
                # walker below so both arms drain into one flop per
                # LHS, not silently dropped.
                block_reset_sym, block_reset_kind, block_reset_active_low = reset_check
                # Capture reset-arm literal RHS values before walking
                # the data arm — drain time looks these up to populate
                # ``$adff``'s ``ARST_VALUE`` / ``$sdff``'s ``SRST_VALUE``
                # parameter (issues #40 / #86). With no ``else`` the
                # registers the reset arm writes hold between resets
                # (rtl-buddy-cdc#390: the block was skipped). A memory
                # write port in either arm is enabled by its arm's
                # condition (rtl-buddy-cdc#398); only a sync-reset block
                # can write a memory.
                reset_on = self._lazy_bit(lambda: self._condition_bit(inner))
                self._enable_stack.append(reset_on)
                try:
                    self._collect_reset_assignments(inner.ifTrue)
                finally:
                    self._enable_stack.pop()
                if inner.ifFalse is not None:
                    self._enable_stack.append(
                        self._lazy_bit(lambda: self._lower_not(reset_on()))
                    )
                    try:
                        self._emit_assignments_in(
                            inner.ifFalse,
                            clk_sym,
                            block_reset_sym,
                            block_reset_active_low,
                            src_node,
                        )
                    finally:
                        self._enable_stack.pop()
            elif (
                reset_sym is None
                and (srst_ce := self._sync_reset_under_enable(inner)) is not None
            ):
                # ``if (en) if (srst) <constants> else <data>``: the reset
                # acts only while loading, an ``$sdffce`` (rtl-buddy-cdc#384).
                enable_bit, srst_stmt, (srst_sym, _kind, srst_low) = srst_ce
                srst_on = self._lazy_bit(lambda: self._condition_bit(srst_stmt))
                self._enable_stack.append(lambda: enable_bit)
                self._enable_stack.append(srst_on)
                try:
                    self._collect_reset_assignments(srst_stmt.ifTrue)
                    self._enable_stack[-1] = self._lazy_bit(
                        lambda: self._lower_not(srst_on())
                    )
                    self._emit_assignments_in(
                        srst_stmt.ifFalse, clk_sym, srst_sym, srst_low, src_node
                    )
                finally:
                    del self._enable_stack[-2:]
                self._drain_proc_writes(
                    clk_sym,
                    srst_sym,
                    srst_low,
                    src_node,
                    reset_kind="sync",
                    clock_enable=enable_bit,
                )
                return
            else:
                # Body isn't a single conditional or expression — defer
                # to the general statement walker. Reached for no-reset
                # blocks whose body is a multi-statement ``begin..end``,
                # a ``case``, a procedural ``for``, etc.
                self._emit_assignments_in(
                    inner,
                    clk_sym,
                    reset_sym=None,
                    reset_active_low=False,
                    src_node=src_node,
                )
            self._drain_proc_writes(
                clk_sym,
                block_reset_sym,
                block_reset_active_low,
                src_node,
                reset_kind=block_reset_kind,
            )
        finally:
            # Outside the block a blocking-written variable is its register.
            self._var_bits.update(self._blocking_nets)
            self._blocking_nets = prev_blocking
            self._reset_proc_tree()
            self._proc_resets = {}
            self._clk_negedge = prev_clk_negedge
            self._proc_clk = prev_proc_clk

    def _event_signals(self, timing: Any) -> list[tuple[Any, bool]] | None:
        """``(signal symbol, negedge)`` per event of an edge-triggered event
        control, or ``None`` when an event is not an edge of a plain
        signal."""
        kind = self._kind_name(timing)
        if kind == "SignalEventControl":
            events = [timing]
        elif kind == "EventListControl":
            events = list(getattr(timing, "events", []) or [])
        else:
            return None
        out: list[tuple[Any, bool]] = []
        for ev in events:
            sym = getattr(getattr(ev, "expr", None), "symbol", None)
            edge = str(getattr(ev, "edge", "")).rsplit(".", 1)[-1]
            if sym is None or edge not in {"PosEdge", "NegEdge"}:
                return None
            out.append((sym, edge == "NegEdge"))
        return out

    def _emit_multi_async_ff(
        self, block: Any, body: Any, events: list[tuple[Any, bool]]
    ) -> None:
        """Lower an edge-triggered block with a clock and two or more async
        controls (``@(posedge clk or negedge rst_n or negedge set_n)``), as
        Yosys ``proc`` does (rtl-buddy-cdc#384: the third event was
        ignored, so its arm became synchronous logic on ``D``).

        The body must open with an ``if`` / ``else if`` chain testing every
        event signal but one, each once; the untested one is the clock.
        Each arm's writes are the values its control loads while asserted
        (polarity from its event edge), the final ``else`` is the clocked
        data. :meth:`_emit_multi_async_flop` builds each register. Any
        other shape is reported (:meth:`_warn_dropped`)."""
        node = body.stmt
        arms: list[tuple[Any, bool, Any]] = []
        while len(arms) < len(events) - 1:
            while self._kind_name(node) == "BlockStatement":
                node = node.body
            if self._kind_name(node) != "ConditionalStatement":
                break
            conds = list(getattr(node, "conditions", None) or [])
            sym = (
                self._extract_condition_symbol(conds[0].expr)
                if len(conds) == 1
                else None
            )
            match = [
                (s, neg)
                for s, neg in events
                if s is sym and all(a is not s for a, _n, _t in arms)
            ]
            if not match:
                break
            arms.append((sym, match[0][1], node.ifTrue))
            node = node.ifFalse
        clocks = [
            (s, neg) for s, neg in events if all(a is not s for a, _n, _t in arms)
        ]
        if len(arms) != len(events) - 1 or len(clocks) != 1:
            self._warn_dropped(
                block,
                "the leading if / else-if chain does not test each async "
                "control of the event list once",
            )
            return
        clk_sym, clk_negedge = clocks[0]
        prev_clk_negedge = getattr(self, "_clk_negedge", False)
        self._clk_negedge = clk_negedge
        self._reset_proc_tree()
        self._proc_resets = {}
        prev_blocking = self._blocking_nets
        self._blocking_nets = {}
        try:
            controls: list[
                tuple[tuple[Bit, ...], bool, dict[tuple[Bit, ...], tuple[Bit, ...]]]
            ] = []
            for sym, active_low, arm in arms:
                values: dict[tuple[Bit, ...], tuple[Bit, ...]] = {}
                self._collect_reset_assignments(arm, values)
                controls.append((self._alloc_bits(sym), active_low, values))
            if node is not None:
                self._emit_assignments_in(node, clk_sym, None, False, block)
            self._drain_proc_writes(
                clk_sym, None, False, block, async_controls=controls
            )
        finally:
            self._var_bits.update(self._blocking_nets)
            self._blocking_nets = prev_blocking
            self._reset_proc_tree()
            self._proc_resets = {}
            self._clk_negedge = prev_clk_negedge

    def _sync_reset_under_enable(
        self, inner: Any
    ) -> tuple[Bit, Any, tuple[Any, str, bool]] | None:
        """``(enable bit, inner if, sync-reset classification)`` when a
        no-async-reset block's whole body is ``if (en) begin if (srst)
        <constants> else <data> end`` with no outer ``else``: the shape
        Yosys ``opt_dff`` folds into an ``$sdffce`` (the reset takes effect
        only while the enable loads). ``None`` otherwise. The enable is
        lowered only on a match."""
        if self._kind_name(inner) != "ConditionalStatement":
            return None
        if inner.ifFalse is not None:
            return None
        conds = list(getattr(inner, "conditions", None) or [])
        if len(conds) != 1 or self._const_int(conds[0].expr) is not None:
            return None
        nested = inner.ifTrue
        while self._kind_name(nested) == "BlockStatement":
            nested = nested.body
        if self._kind_name(nested) != "ConditionalStatement" or nested.ifFalse is None:
            return None
        check = self._classify_reset_check(nested, None, False)
        if check is None:
            return None
        return self._condition_bit(inner), nested, check

    def _order_clock_and_reset(
        self,
        stmt: Any,
        clk_sym: Any,
        reset_sym: Any | None,
        reset_active_low: bool,
        clk_negedge: bool,
    ) -> tuple[Any, Any | None, bool, bool]:
        """Swap the provisional clock / reset pick when the reset event
        is listed first (``@(negedge rst_n or posedge clk)``).

        :meth:`_analyse_event_list` takes the first event as the clock.
        The body decides: when its outer ``if`` tests the first event's
        signal, that signal is the reset and the other one is the clock,
        which is how synthesis reads the block in either order.
        """
        if reset_sym is None:
            return clk_sym, reset_sym, reset_active_low, clk_negedge
        inner = stmt
        while self._kind_name(inner) == "BlockStatement":
            inner = inner.body
        if self._kind_name(inner) != "ConditionalStatement":
            return clk_sym, reset_sym, reset_active_low, clk_negedge
        conds = getattr(inner, "conditions", None)
        if not conds:
            return clk_sym, reset_sym, reset_active_low, clk_negedge
        if self._extract_condition_symbol(conds[0].expr) is clk_sym:
            # The reset polarity is its event's edge, and vice versa.
            return reset_sym, clk_sym, clk_negedge, reset_active_low
        return clk_sym, reset_sym, reset_active_low, clk_negedge

    # -- plain ``always`` (Verilog-2001) lowering, rtl-buddy-cdc#345 -------

    def _warn_dropped(
        self, block: Any, reason: str, what: str = "procedural block"
    ) -> None:
        """Report a construct (a procedural block, by default, or an
        instance array, rtl-buddy-cdc#353) this frontend cannot lower.

        Its flops / logic are missing from the netlist, so the run would
        otherwise come back clean on a design it never saw. Logged at
        ``WARNING`` (stderr) with the source location, like the frontend's
        other non-fatal diagnostics."""
        # (``getattr``: also reached on a bare builder in unit tests.)
        self._lowering_failures = getattr(self, "_lowering_failures", 0) + 1
        where = self._src_attr(block) or "<unknown location>"
        _log.warning(
            "slang frontend: %s: %s not lowered (%s); its "
            "registers and logic are missing from the CDC netlist",
            where,
            what,
            reason,
        )

    def _emit_plain_always(self, block: Any) -> None:
        """Lower a plain ``always`` block by what it infers.

        - every event edge-qualified (``@(posedge clk)``, ``@(posedge clk
          or negedge rst_n)``, ``negedge`` clocks) → the ``always_ff``
          lowering, async reset and enables included;
        - ``@*`` / ``@(*)`` or a level-sensitive list (``@(a or b)``) →
          :meth:`_lower_comb_body`: combinational logic, plus a ``$dlatch``
          for each target left unassigned on some path (rtl-buddy-cdc#377;
          the whole block used to be dropped unless it was the single
          ``if (en) q = d;`` shape). Like synthesis, the list itself is not
          checked for completeness;

        Anything else (mixed edge / level lists, ``edge``, delays, event
        controls inside the body) is reported by :meth:`_warn_dropped`.
        Blocking and nonblocking writes are both accepted, here as in every
        procedural block (rtl-buddy-cdc#377).
        """
        body = block.body
        form = self._plain_always_form(body)
        if form == "ff":
            self._emit_always_ff(block)
        elif form == "level":
            self._lower_comb_body(body.stmt, block)
        else:
            self._warn_dropped(
                block,
                "`always` is not edge-triggered on every event, nor "
                "`@*` / level-sensitive",
            )

    def _plain_always_form(self, body: Any) -> str | None:
        """``"ff"`` / ``"level"`` / ``None`` (not lowerable) for the
        timing control at the head of a plain ``always`` body."""
        if self._kind_name(body) != "TimedStatement":
            return None
        timing = body.timing
        kind = self._kind_name(timing)
        if kind == "ImplicitEventControl":
            return "level"
        if kind == "SignalEventControl":
            events = [timing]
        elif kind == "EventListControl":
            events = list(getattr(timing, "events", []) or [])
        else:
            return None
        if not events or any(
            self._kind_name(ev) != "SignalEventControl" for ev in events
        ):
            return None
        edges = {str(getattr(ev, "edge", "")).rsplit(".", 1)[-1] for ev in events}
        if edges <= {"PosEdge", "NegEdge"}:
            return "ff"
        if edges <= {"None_", "None"}:
            return "level"
        return None

    def _collect_reset_assignments(
        self,
        stmt: Any,
        into: dict[tuple[Bit, ...], tuple[Bit, ...]] | None = None,
    ) -> None:
        """Walk an ``always_ff`` reset / async-load arm (``if (!rst_n)
        <stmt>``) and record the value each write loads, as LSB-first bits
        keyed by the written register bits, into ``into`` (default
        ``self._proc_resets``).

        A compile-time constant is its ``0`` / ``1`` bits (``$adff``'s
        ``ARST_VALUE``, ``$sdff``'s ``SRST_VALUE``, issues #40 / #86). Any
        other right-hand side is lowered, and a run whose value is not
        constant becomes an ``$aldff`` loading it (rtl-buddy-cdc#384: it
        was skipped, so ``if (aload) q <= ad`` reset ``q`` to 0). A
        statement other than an assignment (a nested ``if``, a ``case``)
        is reported; the registers it writes keep the default value 0.
        """
        if stmt is None:
            return
        if into is None:
            into = self._proc_resets
        kind = self._kind_name(stmt)
        if kind == "BlockStatement":
            self._collect_reset_assignments(stmt.body, into)
            return
        if kind in {"StatementList", "ListStatement"}:
            for s in getattr(stmt, "list", []) or []:
                self._collect_reset_assignments(s, into)
            return
        if kind == "VariableDeclStatement":
            return
        if kind == "ForLoopStatement":
            # Unrolled like the data branch: ``for (i) q[i] <= 0``.
            self._unroll_for(
                stmt, lambda body: self._collect_reset_assignments(body, into)
            )
            return
        if kind in {"ConditionalStatement", "CaseStatement"}:
            # A run-time choice inside the arm is not modelled (reported);
            # each bit it writes takes the one constant every write in it
            # loads, else 0, so its register still gets a flop.
            self._other_statement(stmt, "reset / async-load arm")
            self._approximate_reset_writes(stmt, into)
            return
        if kind != "ExpressionStatement":
            self._other_statement(stmt, "reset / async-load arm")
            return
        expr = stmt.expr
        if type(expr).__name__ != "AssignmentExpression":
            self._other_statement(stmt, "reset / async-load arm")
            return
        acc = self._array_access(expr.left)
        if acc is not None and acc.sym in self._memories:
            # A memory word written in a sync-reset arm: a write port
            # enabled by the reset (rtl-buddy-cdc#398).
            if not self._emit_memory_write(acc, expr.right, self._proc_clk, stmt):
                self._warn_lvalue_dropped(expr, expr.left)
            return
        val = self._const_int(expr.right)
        if type(expr.left).__name__ == "ConcatenationExpression":
            # ``{q, meta} <= 2'b01``: each register takes its slice of
            # the value (rtl-buddy-cdc#370), keyed as the data arm's
            # :meth:`_concat_writes` keys its writes.
            targets = self._lvalue_targets(expr.left)
            if targets is None:
                self._warn_lvalue_dropped(expr, expr.left)
                return
            self._note_reset_written(targets)
            if val is None:
                for q_part, bits in self._concat_writes(expr.left, expr.right) or []:
                    into[q_part] = bits
                return
            total = sum(width for _sym, _start, width in targets)
            for sym, start, bits in self._group_parts(
                targets, self._split_rhs(self._int_bits(val, total), targets)
            ):
                into[self._alloc_bits(sym)[start : start + len(bits)]] = bits
            return
        q_bits = self._lvalue_bits(expr.left)
        if q_bits is None:
            self._warn_lvalue_dropped(expr, expr.left)
            return
        self._note_reset_written(self._lvalue_targets(expr.left) or [])
        if val is None:
            into[q_bits] = self._rhs_bits(expr.right, len(q_bits))
        else:
            into[q_bits] = self._int_bits(val, len(q_bits))

    def _approximate_reset_writes(
        self, stmt: Any, into: dict[tuple[Bit, ...], tuple[Bit, ...]]
    ) -> None:
        """The value of each bit an unmodelled reset-arm statement
        (:meth:`_collect_reset_assignments`) writes: the constant every
        write to it loads when they agree, else 0."""
        seen: dict[Bit, set[Bit]] = {}
        order: list[tuple[Any, int, int]] = []

        def visit(node: Any) -> None:
            if node is None:
                return
            kind = self._kind_name(node)
            if kind == "BlockStatement":
                visit(node.body)
            elif kind in {"StatementList", "ListStatement"}:
                for sub in getattr(node, "list", []) or []:
                    visit(sub)
            elif kind == "ConditionalStatement":
                visit(node.ifTrue)
                visit(node.ifFalse)
            elif kind == "CaseStatement":
                for item in getattr(node, "items", []) or []:
                    visit(getattr(item, "stmt", None))
                visit(getattr(node, "defaultCase", None))
            elif kind == "ExpressionStatement":
                expr = node.expr
                if type(expr).__name__ != "AssignmentExpression":
                    return
                targets = self._lvalue_targets(expr.left)
                if targets is None:
                    return
                val = self._const_int(expr.right)
                total = sum(width for _sym, _start, width in targets)
                value = (
                    self._int_bits(val, total) if val is not None else ("x",) * total
                )
                for (sym, start, width), piece in zip(
                    targets, self._split_rhs(value, targets)
                ):
                    order.append((sym, start, width))
                    for bit, v in zip(
                        self._alloc_bits(sym)[start : start + width], piece
                    ):
                        seen.setdefault(bit, set()).add(v)

        visit(stmt)
        self._note_reset_written(order)
        for bit, vals in seen.items():
            agreed = next(iter(vals)) if len(vals) == 1 else "0"
            into[(bit,)] = (agreed if agreed in ("0", "1") else "0",)

    def _note_reset_written(self, targets: list[tuple[Any, int, int]]) -> None:
        """Register the variables a reset / async-load arm writes with the
        block's write accumulator, so a register only that arm writes is
        drained into a flop too (rtl-buddy-cdc#390: it got none)."""
        for sym, _start, _width in targets:
            self._proc_regs.setdefault(sym, self._alloc_bits(sym))

    @staticmethod
    def _int_bits(value: int, width: int) -> tuple[Bit, ...]:
        """``value`` as ``width`` LSB-first ``0`` / ``1`` bits."""
        return tuple("1" if (value >> i) & 1 else "0" for i in range(width))

    def _analyse_event_list(
        self, timing: Any
    ) -> tuple[Any | None, Any | None, bool, bool]:
        """Pick clock + (optional) async-reset symbols out of the
        timing control.

        Returns ``(clock_sym, reset_sym, reset_active_low,
        clock_negedge)``. The reset identification is provisional —
        the canonical conditional body shape gives the authoritative
        answer and we override here when we see it. The polarity
        comes from the event edge: ``negedge`` on the reset event
        means the reset asserts low. ``clock_negedge`` reports the
        clock-event edge directly; it threads through to
        :meth:`_emit_flop_cell` as the ``CLK_POLARITY`` parameter
        (rtl-buddy-cdc#221 — closes the CDC-016 parity gap tracked
        in #224).

        pyslang exposes two timing-control shapes:

        - :class:`SignalEventControl` — a single ``@(<edge> <expr>)``;
          ``.expr`` and ``.edge`` sit directly on the node, with no
          ``.events`` list.
        - :class:`EventListControl` — the multi-event ``@(<a> or <b>)``
          shape used by async-reset always_ff; ``.events`` is the list
          of :class:`SignalEventControl` entries.
        """
        # SignalEventControl: single event, no ``.events`` list.
        if getattr(timing, "events", None) is None and hasattr(timing, "expr"):
            clk_edge = str(getattr(timing, "edge", "")).rsplit(".", 1)[-1]
            return (
                getattr(timing.expr, "symbol", None),
                None,
                False,
                clk_edge == "NegEdge",
            )
        events = list(getattr(timing, "events", []) or [])
        if not events:
            return None, None, False, False
        # Single-event case: that's the clock, no reset.
        if len(events) == 1:
            ev = events[0]
            clk_edge = str(getattr(ev, "edge", "")).rsplit(".", 1)[-1]
            return (
                getattr(ev.expr, "symbol", None),
                None,
                False,
                clk_edge == "NegEdge",
            )
        # Two-event case (the common async-reset shape): the event
        # whose edge matches the conventional clock side is the clock;
        # the other is provisionally the reset.
        clk_ev, rst_ev = events[0], events[1]
        clk_sym = getattr(clk_ev.expr, "symbol", None)
        rst_sym = getattr(rst_ev.expr, "symbol", None)
        clk_edge = str(getattr(clk_ev, "edge", "")).rsplit(".", 1)[-1]
        rst_edge = str(getattr(rst_ev, "edge", "")).rsplit(".", 1)[-1]
        return clk_sym, rst_sym, rst_edge == "NegEdge", clk_edge == "NegEdge"

    def _extract_condition_symbol(self, expr: Any) -> Any | None:
        """For an if-condition that's the reset check (``rst`` or
        ``!rst_n``), return the underlying VariableSymbol. Anything
        more elaborate (``rst && enable``, function calls, …) → None,
        and the caller falls back to the event-list guess."""
        # UnaryExpression(operand=NamedValueExpression)  → !rst form
        if type(expr).__name__ == "UnaryExpression":
            operand = getattr(expr, "operand", None)
            if operand is not None and self._is_named(operand):
                return operand.symbol
        # NamedValueExpression directly  → positive-polarity reset
        if self._is_named(expr):
            return expr.symbol
        return None

    def _classify_reset_check(
        self,
        inner: Any,
        reset_sym_from_event_list: Any | None,
        reset_active_low_from_event_list: bool,
    ) -> tuple[Any, str, bool] | None:
        """Classify ``inner`` against the canonical reset-check shape.

        Returns ``(reset_sym, kind, active_low)`` when matched, else
        ``None`` so the caller walks the body as a normal procedural
        statement (no reset semantics).

        ``kind`` is ``"async"`` when the event list carried a
        dedicated reset event (``or negedge rst_n``); the body's
        outer ``if`` then gates on that same symbol, and
        ``active_low`` comes from the event-list edge.

        ``kind`` is ``"sync"`` for the synthesizable-into-``$sdff``
        shape — no reset in the event list, but the body's outer
        ``ConditionalStatement`` writes only constants in its
        ``ifTrue`` arm (matches ``if (!rst_n) <constants> else
        <data>``). ``active_low`` is derived from the condition
        shape: a ``UnaryExpression`` wrapping the symbol (``!rst_n``
        / ``~rst_n``) marks active-low, a bare reference marks
        active-high. The event list has no reset event to read in
        this case, so the condition shape is the only source.

        The constant-only check is what distinguishes a real reset
        check from a runtime ``if (cond)`` at the top of a no-reset
        always_ff body — the latter has a real RHS expression in
        ifTrue and must be walked as a normal mux-tree.
        """
        if self._kind_name(inner) != "ConditionalStatement":
            return None
        conds = getattr(inner, "conditions", None)
        if not conds:
            return None
        cond_expr = conds[0].expr
        cond_sym = self._extract_condition_symbol(cond_expr)
        if cond_sym is None:
            return None
        if reset_sym_from_event_list is not None:
            if cond_sym is not reset_sym_from_event_list:
                return None
            return cond_sym, "async", reset_active_low_from_event_list
        # Sync-reset heuristic: the ifTrue arm assigns only constants.
        if not self._is_constant_only_assignment_tree(inner.ifTrue):
            return None
        # ... and the condition is a one-bit value or its negation. ``if
        # (!v)`` of a vector, or ``if (&v)``, is a ``$logic_not`` /
        # ``$reduce_and`` select on ``D`` (Yosys ``proc``), not an
        # ``SRST`` pin carrying the vector (rtl-buddy-cdc#402).
        op = str(getattr(cond_expr, "op", "")).rsplit(".", 1)[-1]
        if self._width_of(cond_sym) != 1 or (
            type(cond_expr).__name__ == "UnaryExpression"
            and op not in {"LogicalNot", "BitwiseNot"}
        ):
            return None
        # Polarity from the condition shape — same convention as the
        # async edge: ``!rst_n`` / ``~rst_n`` is active-low, bare
        # ``rst`` is active-high. ``_extract_condition_symbol`` already
        # accepts both shapes; mirror its branch here.
        active_low = type(cond_expr).__name__ == "UnaryExpression"
        return cond_sym, "sync", active_low

    def _is_constant_only_assignment_tree(self, stmt: Any) -> bool:
        """``True`` if every nonblocking assignment reachable from
        ``stmt`` has a compile-time-constant RHS (IntegerLiteral,
        ConversionExpression wrapping a literal, or another expression
        ``_const_int`` can fold). Used to distinguish a reset-arm
        body (all-constant assigns) from a runtime if/else arm.

        Recurses through the same control-flow shapes the walker
        handles — BlockStatement, StatementList, ConditionalStatement
        (both arms), CaseStatement, ForLoopStatement. Bails on
        anything it can't reason about; that returns ``False`` and
        the caller falls through to the dynamic-condition path,
        which is the conservative choice.
        """
        if stmt is None:
            return True  # vacuously: no assignments → no non-constants
        kind = self._kind_name(stmt)
        if kind == "BlockStatement":
            return self._is_constant_only_assignment_tree(stmt.body)
        if kind in {"StatementList", "ListStatement"}:
            return all(
                self._is_constant_only_assignment_tree(s)
                for s in (getattr(stmt, "list", []) or [])
            )
        if kind == "ConditionalStatement":
            return all(
                self._is_constant_only_assignment_tree(arm)
                for arm in (stmt.ifTrue, stmt.ifFalse)
                if arm is not None
            )
        if kind == "CaseStatement":
            ok = all(
                self._is_constant_only_assignment_tree(getattr(it, "stmt", None))
                for it in (getattr(stmt, "items", []) or [])
            )
            default = getattr(stmt, "defaultCase", None)
            if default is not None:
                ok = ok and self._is_constant_only_assignment_tree(default)
            return ok
        if kind == "ForLoopStatement":
            return self._is_constant_only_assignment_tree(getattr(stmt, "body", None))
        if kind == "ExpressionStatement":
            expr = getattr(stmt, "expr", None)
            if expr is None or type(expr).__name__ != "AssignmentExpression":
                # Non-assignment ExpressionStatement (e.g. function
                # call) — not a reset assign; bail conservatively.
                return False
            return self._const_int(expr.right) is not None
        if kind == "VariableDeclStatement":
            return True  # loop-var declarations etc. contribute no flop
        # Anything else (timing control, immediate assert, …) — bail.
        return False

    def _emit_assignments_in(
        self,
        statement: Any,
        clk_sym: Any,
        reset_sym: Any | None,
        reset_active_low: bool,
        src_node: Any = None,
    ) -> None:
        """Find every nonblocking assignment reachable from
        ``statement`` and emit a flop cell per assignment.

        Walks through whichever procedural control-flow nodes the
        ``always_ff`` data branch contains: nested ``if/else``
        (``ConditionalStatement``), ``case`` arms (``CaseStatement``
        + ``ItemGroup``), and statement blocks. Clock / reset
        bindings are inherited from the enclosing ``always_ff`` —
        every leaf nonblocking assignment fires on the same edge.
        """
        if statement is None:
            return
        kind = self._kind_name(statement)
        if kind == "BlockStatement":
            self._emit_assignments_in(
                statement.body, clk_sym, reset_sym, reset_active_low, src_node
            )
            return
        if kind == "ExpressionStatement":
            write = self._as_assignment(statement.expr)
            if write is None:
                self._other_statement(statement, "clocked block")
                return
            self._emit_assignment_expression(
                write,
                clk_sym,
                reset_sym,
                reset_active_low,
                src_node=src_node,
            )
            return
        if kind in {"StatementList", "ListStatement"}:
            # Older pyslang names; included defensively. Real walk is
            # via the .list attribute when present.
            for s in getattr(statement, "list", []) or []:
                self._emit_assignments_in(
                    s, clk_sym, reset_sym, reset_active_low, src_node
                )
            return
        if kind == "ConditionalStatement":
            # Compile-time-constant fold: if the condition is a
            # parameter-driven (``IS_DIAGONAL``-style) or literal
            # constant, walk only the live arm with no enable push.
            # Matches Yosys-flatten + opt_clean's pruning of dead
            # arms, so the resulting netlist doesn't leak fanin
            # from a statically unreachable RHS into the mux tree
            # (the cause of #72's 36 false-positive md→mr crossings
            # on tiny-NPU's non-diagonal mxp_cors).
            cond_expr = statement.conditions[0].expr if statement.conditions else None
            cond_const = self._const_int(cond_expr)
            if cond_const is not None:
                live = statement.ifTrue if cond_const != 0 else statement.ifFalse
                if live is not None:
                    self._emit_assignments_in(
                        live, clk_sym, reset_sym, reset_active_low, src_node
                    )
                return

            # Dynamic condition: a switch of two arms, each walked into
            # its own branch of the decision tree, ``cond`` selecting the
            # first. ``_drain_proc_writes`` builds ``mux(S=cond, A=else
            # value, B=then value)`` per register from it, the value
            # before the ``if`` standing in for an arm that does not write
            # the register (the hold-feedback ``mux(cond, Q, a)`` the rule
            # pack's ``_is_gated_bus_crossing`` matches), and an ``else
            # if`` nested in the ``else`` leg, so the first condition's
            # mux is outermost, as Yosys ``proc`` orders it
            # (rtl-buddy-cdc#394: ``if (c1) .. else if (c2) ..`` put the
            # ``~c1 & c2`` mux outermost).
            #
            # A condition that does not lower becomes an opaque select
            # (reported), never an unconditional walk of both arms.
            #
            # A negated one-bit condition (``if (!rb)``, ``if (~rb)``)
            # selects on ``rb`` with the arms swapped, as Yosys ``proc``
            # builds it (rtl-buddy-cdc#402: a ``$logic_not`` select read as
            # an active-high reset where ``proc`` reads active low).
            negated = self._negated_condition_bit(statement)
            cond_bit = (
                negated if negated is not None else self._condition_bit(statement)
            )

            def walk(arm: Any) -> None:
                self._emit_assignments_in(
                    arm, clk_sym, reset_sym, reset_active_low, src_node
                )

            base = self._blocking_values()
            not_cond = self._lazy_bit(lambda: self._lower_not(cond_bit))
            true_on = not_cond if negated is not None else (lambda: cond_bit)
            true_state, true_case = self._clocked_arm(
                true_on, statement.ifTrue, walk, base
            )
            false_state, false_case = base, _ProcCase()
            if statement.ifFalse is not None:
                false_on = (lambda: cond_bit) if negated is not None else not_cond
                false_state, false_case = self._clocked_arm(
                    false_on, statement.ifFalse, walk, base
                )
            if negated is not None:
                self._proc_case.items.append(
                    _ProcSwitch([(cond_bit, false_case), (None, true_case)])
                )
                self._merge_blocking(base, [(cond_bit, false_state)], true_state)
                return
            arms: list[tuple[Bit | None, _ProcCase]] = [(cond_bit, true_case)]
            if statement.ifFalse is not None:
                arms.append((None, false_case))
            self._proc_case.items.append(_ProcSwitch(arms))
            self._merge_blocking(base, [(cond_bit, true_state)], false_state)
            return
        if kind == "CaseStatement":
            self._emit_case_statement(
                statement, clk_sym, reset_sym, reset_active_low, src_node
            )
            return
        if kind == "ForLoopStatement":
            self._emit_for_loop(
                statement, clk_sym, reset_sym, reset_active_low, src_node
            )
            return
        if kind == "VariableDeclStatement":
            # A block-local declaration (``for (int i = ...)`` emits its
            # loop variable as one too): its initialiser is its first
            # value, visible to the rest of the block.
            self._declare_local(statement, clocked=True)
            return
        self._other_statement(statement, "clocked block")

    def _emit_case_statement(
        self,
        statement: Any,
        clk_sym: Any,
        reset_sym: Any | None,
        reset_active_low: bool,
        src_node: Any,
    ) -> None:
        """Walk a ``case`` body into a switch of the block's decision tree,
        one arm per item selected by its ``case_expr == match`` equality,
        so the deferred-emission drain can build the mux tree.

        Three paths, in order of decreasing precision:

        - **Compile-time-constant case-expr (#84).** ``_const_int``
          folds the case expression and (best-effort) each match
          expression; walk only the matching arm — or the default
          when none match — and return. Dead arms contribute no
          fanin. Mirrors Yosys-flatten + opt_clean's pruning and the
          if/else fold landed in #72.
        - **Dynamic case-expr we can lower (#85).** Emit a ``$eq``
          per match expression with the case-expr bits on ``A`` and
          the match constant on ``B``. Items with multiple matches
          OR the per-match equalities together. The drain chains the
          items' muxes first item outermost, as Yosys ``proc`` orders
          them, so the first matching item wins (IEEE 1800-2017 12.5;
          rtl-buddy-cdc#384 / #394), with the ``default`` arm (else the
          value before the ``case``) innermost.
        - **Bail.** If the case-expr can't be lowered (kind we don't
          model on the RHS yet), walk every arm with no enable —
          same conservative policy as the pre-PR behaviour, so flops
          still emit; we just lose the gating-mux shape on this
          particular case.
        """
        items = list(getattr(statement, "items", []) or [])
        default_arm = getattr(statement, "defaultCase", None)

        # #84: compile-time-constant case-expr — short-circuit to the
        # matching arm. ``_const_int`` already folds parameter refs
        # and arithmetic, so ``case (MODE)`` with a parameter-bound
        # MODE resolves cleanly (only while every label up to the match
        # is a constant too: :meth:`_constant_case_arm`).
        folded, live_arm = self._constant_case_arm(statement)
        if folded:
            if live_arm is not None:
                self._emit_assignments_in(
                    live_arm, clk_sym, reset_sym, reset_active_low, src_node
                )
            return

        # #85: dynamic case-expr — build per-item enables. A case
        # expression or label that does not lower is a reported opaque
        # value (rtl-buddy-cdc#377), never a walk of every arm as if
        # unconditional.
        case_expr_bits = self._case_expr_bits(statement)

        def walk(arm: Any) -> None:
            self._emit_assignments_in(
                arm, clk_sym, reset_sym, reset_active_low, src_node
            )

        base = self._blocking_values()
        explicit_match_bits: list[Bit] = []
        arm_states: list[tuple[Bit, dict[Any, tuple[Bit, ...]]]] = []
        arms: list[tuple[Bit | None, _ProcCase]] = []
        # A memory write port's enable (``_enable_stack``) is no priority
        # chain: when two items can match the same value, an item's enable
        # also carries "no earlier item matched" (rtl-buddy-cdc#384).
        overlap = self._case_labels_may_overlap(statement, items)
        earlier: list[Bit] = []
        for item in items:
            item_match_bits = [
                self._case_label_match(case_expr_bits, match_expr, statement)
                for match_expr in getattr(item, "expressions", []) or []
            ]
            explicit_match_bits.extend(item_match_bits)
            own_match = self._or_bits(item_match_bits)
            item_stmt = getattr(item, "stmt", None)
            if own_match is None:
                continue  # an item with no labels never matches
            item_enable = self._lazy_bit(
                functools.partial(
                    self._first_match, own_match, list(earlier) if overlap else []
                )
            )
            earlier.append(own_match)
            state, case = self._clocked_arm(item_enable, item_stmt, walk, base)
            arm_states.append((own_match, state))
            arms.append((own_match, case))

        default_state = base
        if default_arm is not None:
            if not explicit_match_bits:
                walk(default_arm)
                default_state = self._blocking_values()
            else:
                default_state, default_case = self._clocked_arm(
                    self._lazy_bit(
                        functools.partial(self._first_match, "1", explicit_match_bits)
                    ),
                    default_arm,
                    walk,
                    base,
                )
                arms.append((None, default_case))
        if arms:
            self._proc_case.items.append(_ProcSwitch(arms))
        self._merge_blocking(base, arm_states, default_state)

    def _first_match(self, match: Bit, earlier: list[Bit]) -> Bit:
        """``match`` and none of ``earlier``: a ``case`` item's condition
        for a memory write port's enable."""
        prior = self._or_bits(earlier)
        return match if prior is None else self._and_bit(match, self._lower_not(prior))

    def _case_labels_may_overlap(self, stmt: Any, items: list[Any]) -> bool:
        """Whether two items of ``case`` statement ``stmt`` can match the
        same value: ``False`` only for a plain ``case`` whose labels are
        all distinct, fully known (no ``x`` / ``z``) constants."""
        condition = str(getattr(stmt, "condition", "")).rsplit(".", 1)[-1]
        if condition not in {"Normal", ""}:
            return True
        seen: set[tuple[Bit, ...]] = set()
        for item in items:
            for label in getattr(item, "expressions", []) or []:
                if label is None or self._kind_name(label) == "RangeExpression":
                    return True
                bits = self._constant_label_bits(label)
                if bits is None or bits in seen:
                    return True
                seen.add(bits)
        return False

    def _constant_label_bits(self, label: Any) -> tuple[Bit, ...] | None:
        """A ``case`` label's value as ``0`` / ``1`` bits with the leading
        zeros stripped, or ``None`` when it is not a fully known constant."""
        value = self._const_int(label)
        if value is None or value < 0:
            return None
        bits = self._constant_bits(label)
        if bits is not None and any(b not in ("0", "1") for b in bits):
            return None
        return tuple(
            "1" if (value >> i) & 1 else "0" for i in range(value.bit_length())
        )

    def _constant_case_arm(self, stmt: Any) -> tuple[bool, Any]:
        """``(True, arm)`` when the ``case`` is decided at elaboration time:
        its expression is a constant and so is every label up to the first
        one equal to it (``arm`` that item's statement, else the
        ``default``, else ``None``). ``(False, None)`` otherwise — a label
        that is not a constant (``case (1'b1) a[3]: ..``), a range or a
        wildcard comes first, so the match is a run-time one
        (rtl-buddy-cdc#405: such labels were skipped, so ``case (1'b1)``
        walked the ``default`` alone and every arm's fan-in was lost)."""
        case_const = self._const_int(getattr(stmt, "expr", None))
        if case_const is None:
            return False, None
        for item in getattr(stmt, "items", []) or []:
            for label in getattr(item, "expressions", []) or []:
                if self._kind_name(label) == "ValueRangeExpression":
                    return False, None
                if not self._is_plain_constant(label):
                    return False, None
                if self._const_int(label) == case_const:
                    return True, getattr(item, "stmt", None)
        return True, getattr(stmt, "defaultCase", None)

    def _is_plain_constant(self, label: Any) -> bool:
        """``label`` folds to a constant with no ``x`` / ``z`` digit."""
        if self._const_int(label) is None:
            return False
        bits = self._constant_bits(label)
        return bits is None or all(b in ("0", "1") for b in bits)

    def _case_expr_bits(self, stmt: Any) -> tuple[Bit, ...]:
        """The bits of a ``case`` statement's expression, or a reported
        opaque value of its width."""
        expr = getattr(stmt, "expr", None)
        bits = self._bits_of_expression(expr) if expr is not None else None
        if bits:
            return bits
        return self._opaque(
            expr if expr is not None else stmt,
            self._expr_width(expr),
            "`case` expression",
        )

    def _case_label_match(
        self, sel_bits: tuple[Bit, ...], label: Any, stmt: Any
    ) -> Bit:
        """One bit: ``case`` expression ``sel_bits`` matches ``label``.

        ``$eq`` of the two, like Yosys. In a ``casez`` a ``z`` / ``?``
        digit of a constant label matches anything, in a ``casex`` (and
        ``case … inside``) an ``x`` too, so those bit positions are left
        out of the comparison (rtl-buddy-cdc#377: they were compared as
        literal ``z`` / ``x`` values, and such a label used to drop its
        arm). A label that does not lower (a ``case inside`` range, …) is
        a reported opaque match."""
        if label is None:
            return self._opaque(stmt, 1, "`case` label")[0]
        if self._kind_name(label) == "ValueRangeExpression":
            # ``case … inside`` ``[lo:hi]`` (rtl-buddy-cdc#405: opaque).
            hit = self._range_match(sel_bits, label)
            return hit if hit is not None else self._opaque(label, 1, "`case` label")[0]
        bits = self._bits_of_expression(label)
        if not bits:
            return self._opaque(label, 1, "`case` label")[0]
        width = max(len(bits), len(sel_bits))
        bits = self._resize_bits(bits, width)
        sel = self._resize_bits(sel_bits, width)
        condition = str(getattr(stmt, "condition", "")).rsplit(".", 1)[-1]
        wild = {
            "WildcardJustZ": {"z"},
            "WildcardXOrZ": {"x", "z"},
            "Inside": {"x", "z"},
        }.get(condition, set())
        lanes = [i for i in range(width) if bits[i] not in wild]
        if not lanes:
            return "1"
        return self._emit_comb_cell(
            "$eq",
            {"A": tuple(sel[i] for i in lanes), "B": tuple(bits[i] for i in lanes)},
            output_width=1,
            src_node=label,
        )[0]

    def _emit_for_loop(
        self,
        stmt: Any,
        clk_sym: Any,
        reset_sym: Any | None,
        reset_active_low: bool,
        src_node: Any,
    ) -> None:
        """Virtually unroll a procedural ``for`` loop in a clocked block
        (see :meth:`_for_loop_iterations`) and walk the body once per
        iteration with the loop variable bound to the iteration value."""

        def walk(body: Any) -> None:
            self._emit_assignments_in(
                body, clk_sym, reset_sym, reset_active_low, src_node
            )

        self._unroll_for(stmt, walk)

    def _unroll_for(self, stmt: Any, walk: Any) -> None:
        """Walk ``stmt``'s body once per iteration (``walk(body)``) with the
        loop variable bound in :attr:`_loop_bindings`, so element selects
        like ``chain[i]`` / ``chain[i-1]`` fold to per-iteration bits, as
        Yosys' frontend unrolls them. A loop whose trip count is not known
        at elaboration time is reported, never skipped silently
        (rtl-buddy-cdc#377)."""
        header = self._for_loop_iterations(stmt)
        if header is None:
            self._warn_dropped(
                stmt,
                "bounds, step or loop variable are not constant",
                "`for` loop",
            )
            return
        loopvar, values = header
        for v in values:
            prev = self._loop_bindings.pop(loopvar, _SENTINEL)
            self._loop_bindings[loopvar] = v
            try:
                walk(getattr(stmt, "body", None))
            finally:
                if prev is _SENTINEL:
                    self._loop_bindings.pop(loopvar, None)
                else:
                    self._loop_bindings[loopvar] = prev

    _ITER_CAP = 1024

    def _for_loop_iterations(self, stmt: Any) -> tuple[Any, list[int]] | None:
        """``(loop variable, iteration values)`` of a ``for`` loop with one
        loop variable, a constant start, a ``var <op> const`` stop
        condition and a constant step (``i++`` / ``i--``, ``i += N`` /
        ``i -= N``, ``i = i + N`` / ``i = i - N``); ``None`` otherwise.

        The variable may be declared in the header (``for (int i = 0;
        …)``) or outside it (``integer i; for (i = 0; …)``, the
        Verilog-2001 form). At most :data:`_ITER_CAP` iterations, a guard
        against pathological source.
        """
        loop_vars = list(getattr(stmt, "loopVars", []) or [])
        if len(loop_vars) == 1:
            loopvar = loop_vars[0]
            start = self._const_int(getattr(loopvar, "initializer", None))
        elif not loop_vars:
            inits = list(getattr(stmt, "initializers", []) or [])
            if len(inits) != 1 or type(inits[0]).__name__ != "AssignmentExpression":
                return None
            target = inits[0].left
            if type(target).__name__ != "NamedValueExpression":
                return None
            loopvar = target.symbol
            start = self._const_int(inits[0].right)
        else:
            return None
        if start is None:
            return None

        stop_expr = getattr(stmt, "stopExpr", None)
        if stop_expr is None or type(stop_expr).__name__ != "BinaryExpression":
            return None
        stop_op = str(getattr(stop_expr, "op", "")).rsplit(".", 1)[-1]
        if self._loop_var_of(getattr(stop_expr, "left", None)) is not loopvar:
            return None
        stop_val = self._const_int(getattr(stop_expr, "right", None))
        if stop_val is None:
            return None

        steps = list(getattr(stmt, "steps", []) or [])
        if len(steps) != 1:
            return None
        step_amount = self._loop_step(steps[0], loopvar)
        if step_amount is None or step_amount == 0:
            return None

        def still_iterating(v: int) -> bool:
            if stop_op == "LessThan":
                return v < stop_val
            if stop_op == "LessThanEqual":
                return v <= stop_val
            if stop_op == "GreaterThan":
                return v > stop_val
            if stop_op == "GreaterThanEqual":
                return v >= stop_val
            if stop_op == "Inequality":
                return v != stop_val
            return False

        values: list[int] = []
        v = start
        while still_iterating(v) and len(values) < self._ITER_CAP:
            values.append(v)
            v += step_amount
        return loopvar, values

    @staticmethod
    def _loop_var_of(expr: Any) -> Any | None:
        """The variable a (possibly implicitly converted) name refers to."""
        while type(expr).__name__ == "ConversionExpression":
            expr = getattr(expr, "operand", None)
        if type(expr).__name__ in {"NamedValueExpression", "LValueReferenceExpression"}:
            return getattr(expr, "symbol", None)
        return None

    def _loop_step(self, step_expr: Any, loopvar: Any) -> int | None:
        """The constant increment of a ``for`` loop step expression."""
        step_kind = type(step_expr).__name__
        if step_kind == "UnaryExpression":
            op_name = str(getattr(step_expr, "op", "")).rsplit(".", 1)[-1]
            if op_name in ("Postincrement", "Preincrement"):
                return 1
            if op_name in ("Postdecrement", "Predecrement"):
                return -1
            return None
        if step_kind != "AssignmentExpression":
            return None
        if self._loop_var_of(getattr(step_expr, "left", None)) is not loopvar:
            return None
        # ``i += N`` desugars to ``i = <lvalue ref> + N``; ``i = i + N`` is
        # written out. Either way the right-hand side is a binary operator
        # with the loop variable on one side and a constant on the other.
        desugared: Any = getattr(step_expr, "right", None)
        while type(desugared).__name__ == "ConversionExpression":
            desugared = getattr(desugared, "operand", None)
        if type(desugared).__name__ != "BinaryExpression":
            return None
        left, right = desugared.left, desugared.right
        bin_op = str(getattr(desugared, "op", "")).rsplit(".", 1)[-1]
        if self._is_loop_var_ref(left, loopvar):
            step_const = self._const_int_unbound(right, loopvar)
            if step_const is None:
                return None
            if bin_op == "Add":
                return step_const
            if bin_op == "Subtract":
                return -step_const
            return None
        if self._is_loop_var_ref(right, loopvar) and bin_op == "Add":
            return self._const_int_unbound(left, loopvar)
        return None

    def _is_loop_var_ref(self, expr: Any, loopvar: Any) -> bool:
        """``expr`` reads the loop variable (or is the compound-assignment
        reference to the target, which is the loop variable)."""
        while type(expr).__name__ == "ConversionExpression":
            expr = getattr(expr, "operand", None)
        if type(expr).__name__ == "LValueReferenceExpression":
            return True
        return self._loop_var_of(expr) is loopvar

    def _const_int_unbound(self, expr: Any, loopvar: Any) -> int | None:
        """:meth:`_const_int` with ``loopvar`` unbound (its iteration value
        must not leak into a step constant)."""
        prev = self._loop_bindings.pop(loopvar, _SENTINEL)
        try:
            return self._const_int(expr)
        finally:
            if prev is not _SENTINEL:
                self._loop_bindings[loopvar] = prev

    def _emit_assignment_expression(
        self,
        expr: Any,
        clk_sym: Any,
        reset_sym: Any | None,
        reset_active_low: bool,
        src_node: Any = None,
    ) -> None:
        """Add one write to the block's decision tree (``_proc_case``).

        Doesn't emit a flop cell — emission happens in
        ``_drain_proc_writes`` after the entire body has been walked,
        so multiple arms writing the same LHS collapse to one flop
        with a mux tree. Reset / clock info is captured at the
        procedural-block level (same for every write in the block);
        the parameters here are kept for signature symmetry with the
        pre-deferred-emission walker but only ``src_node`` is read.
        """
        del reset_sym, reset_active_low  # captured at block level
        self._compound_lhs.append(expr.left)
        try:
            self._clocked_write(expr, clk_sym, src_node)
        finally:
            self._compound_lhs.pop()

    def _clocked_write(self, expr: Any, clk_sym: Any, src_node: Any) -> None:
        """:meth:`_emit_assignment_expression` for one write."""
        lhs = expr.left
        rhs = expr.right
        # A write to a memory's word is a memory write port, not a flop
        # (#326) — emitted immediately as the ``$memwr_v2`` Yosys' ``proc``
        # produces for the same source, at a constant address too
        # (rtl-buddy-cdc#398). A register array's element at a run-time
        # index is a conditional write of each element.
        acc = self._array_access(lhs)
        if acc is not None:
            if acc.sym in self._memories:
                if not self._emit_memory_write(acc, rhs, clk_sym, src_node):
                    self._warn_lvalue_dropped(expr, lhs)
                return
            if any(self._const_int(sel) is None for sel in acc.selectors):
                if not self._indexed_clocked_write(lhs, expr):
                    self._warn_lvalue_dropped(expr, lhs)
                return
        # Resolve the target to ``(variable, start, width)`` parts: a whole
        # variable, a constant bit / part select, a packed-struct member,
        # or a concatenation of those (``{q, meta} <= {meta, d}``,
        # rtl-buddy-cdc#370), one write per register run, each taking its
        # slice of the sized RHS. Anything else (run-time index, streaming
        # concatenation, hierarchical reference, …) is reported.
        targets = self._lvalue_targets(lhs)
        if targets is None:
            if type(lhs).__name__ == "ConcatenationExpression":
                # A part with a run-time index (rtl-buddy-cdc#402).
                for part in self._concat_part_writes(lhs, rhs, expr):
                    self._clocked_write(part, clk_sym, src_node)
            elif not self._indexed_clocked_write(lhs, expr):
                # A run-time bit position (``v[j] <= b``, rtl-buddy-cdc#402).
                self._warn_lvalue_dropped(expr, lhs)
            return
        total = sum(width for _sym, _start, width in targets)
        # An unlowerable RHS gets a reported $_UNKNOWN_ driver: the rule
        # pack treats the D input as opaque (no upstream flops trace
        # through it). Read before the write: ``n = n + 1`` reads ``n``.
        rhs_bits = self._rhs_bits(rhs, total)
        blocking = not getattr(expr, "isNonBlocking", False)
        for sym, start, bits in self._group_parts(
            targets, self._split_rhs(rhs_bits, targets)
        ):
            net = self._blocking_nets.get(sym) or self._alloc_bits(sym)
            self._proc_regs.setdefault(sym, net)
            self._proc_case.items.append((sym, start, bits))
            if blocking:
                self._forward_blocking_write(sym, net, start, bits)

    def _forward_blocking_write(
        self,
        sym: Any,
        net: tuple[Bit, ...],
        start: int,
        bits: tuple[Bit, ...],
    ) -> None:
        """Make a blocking write in a clocked block visible to the reads
        after it in the same block, as synthesis does: ``t = a + 1; q <=
        t;`` loads ``q`` with ``a + 1``, not with the register ``t``
        (rtl-buddy-cdc#377; it read the register, which added a flop stage
        that is not in the hardware). The variable is still a register
        (its writes drive its D); until the block ends its value is the
        written one, within the ``if`` / ``case`` arm that wrote it and,
        after the statement, behind the arms' merge mux
        (:meth:`_clocked_arm`)."""
        self._blocking_nets.setdefault(sym, net)
        current = self._var_bits.get(sym, net)
        self._var_bits[sym] = current[:start] + bits + current[start + len(bits) :]

    def _blocking_values(self) -> dict[Any, tuple[Bit, ...]]:
        """The current value of every blocking-written variable."""
        return {
            sym: self._var_bits.get(sym, net)
            for sym, net in self._blocking_nets.items()
        }

    def _clocked_arm(
        self,
        enable: Callable[[], Bit],
        stmt: Any,
        walk: Any,
        base: dict[Any, tuple[Bit, ...]],
    ) -> tuple[dict[Any, tuple[Bit, ...]], _ProcCase]:
        """Walk one arm of a clocked ``if`` / ``case`` into a fresh branch
        of the decision tree, with ``enable`` (the arm's condition, for a
        memory write port) on the enable stack. Return the blocking-written
        values it leaves and the branch, and restore the values from
        before it. Inside the arm a blocking write is seen as written, as
        Yosys ``proc`` evaluates the arm's statements;
        :meth:`_merge_blocking` muxes the arms afterwards."""
        case = _ProcCase()
        parent = self._proc_case
        self._proc_case = case
        self._enable_stack.append(enable)
        try:
            walk(stmt)
        finally:
            self._enable_stack.pop()
            self._proc_case = parent
        state = self._blocking_values()
        for sym, net in self._blocking_nets.items():
            self._var_bits[sym] = base.get(sym, net)
        return state, case

    @staticmethod
    def _lazy_bit(build: Callable[[], Bit]) -> Callable[[], Bit]:
        """``build``, run once on the first call: an arm condition only a
        memory write port reads, so its cells are emitted only then."""
        memo: list[Bit] = []

        def get() -> Bit:
            if not memo:
                memo.append(build())
            return memo[0]

        return get

    def _reset_proc_tree(self) -> None:
        """Start a clocked block with no writes."""
        self._proc_regs = {}
        self._proc_root = _ProcCase()
        self._proc_case = self._proc_root

    def _merge_blocking(
        self,
        base: dict[Any, tuple[Bit, ...]],
        arms: list[tuple[Bit, dict[Any, tuple[Bit, ...]]]],
        fallback: dict[Any, tuple[Bit, ...]],
    ) -> None:
        """Merge the values blocking writes left in each arm (first arm
        highest priority; ``fallback`` when none is selected) into one
        ``$mux`` chain per variable."""
        for sym, net in self._blocking_nets.items():
            prior = base.get(sym, net)
            chain = fallback.get(sym, prior)
            for select, state in reversed(arms):
                value = state.get(sym, prior)
                if value != chain:
                    chain = self._build_hold_mux(select, chain, value)
            self._var_bits[sym] = chain

    def _lower_not(self, bit: Bit) -> Bit:
        """Allocate a ``$not`` cell over ``bit`` and return the output
        bit (an ``else`` arm's condition on ``_enable_stack``, a latch
        enable, ...)."""
        y = self._alloc_anon_bits(1)
        name = self._fresh_cell_name("$not")
        self._cells[name] = Cell(
            name=name,
            type="$not",
            connections={"A": (bit,), "Y": y},
            parameters={},
            attributes={},
        )
        return y[0]

    def _or_bits(self, bits: list[Bit]) -> Bit | None:
        """OR a list of single-bit ``Bit`` values into one bit via a
        left-folded chain of ``$or`` cells. Returns ``None`` for an
        empty list and the bit itself for a single-element list (no
        cell emission). Used by the dynamic ``case`` lowering to OR
        multi-match equalities within an item, and to OR explicit
        matches for the default arm's negation."""
        if not bits:
            return None
        if len(bits) == 1:
            return bits[0]
        result: Bit = bits[0]
        for next_bit in bits[1:]:
            or_y = self._alloc_anon_bits(1)
            cell_name = self._fresh_cell_name("$or")
            self._cells[cell_name] = Cell(
                name=cell_name,
                type="$or",
                connections={
                    "A": (result,),
                    "B": (next_bit,),
                    "Y": or_y,
                },
                parameters={},
                attributes={},
            )
            result = or_y[0]
        return result

    def _combine_enable_stack(self) -> Bit | None:
        """AND every entry on ``_enable_stack`` (building each) into a
        single bit, or return ``None`` if the stack is empty. Chains
        ``$and`` cells for multi-entry stacks; for a single-entry stack
        returns the bit directly with no cell emission."""
        if not self._enable_stack:
            return None
        enables = [enable() for enable in self._enable_stack]
        select_bit: Bit = enables[0]
        for next_bit in enables[1:]:
            and_y = self._alloc_anon_bits(1)
            cell_name = self._fresh_cell_name("$and")
            self._cells[cell_name] = Cell(
                name=cell_name,
                type="$and",
                connections={
                    "A": (select_bit,),
                    "B": (next_bit,),
                    "Y": and_y,
                },
                parameters={},
                attributes={},
            )
            select_bit = and_y[0]
        return select_bit

    def _drain_proc_writes(
        self,
        clk_sym: Any,
        reset_sym: Any | None,
        reset_active_low: bool,
        src_node: Any,
        reset_kind: str | None = None,
        async_controls: list[
            tuple[tuple[Bit, ...], bool, dict[tuple[Bit, ...], tuple[Bit, ...]]]
        ]
        | None = None,
        clock_enable: Bit | None = None,
    ) -> None:
        """Emit one flop per contiguous run of the bits each variable is
        written in, as Yosys ``proc`` groups them (rtl-buddy-cdc#377: one
        flop per written part, so ``q <= 0; if (c) q[1] <= b;`` gave two
        flops driving ``q[1]``, and ``p[0] <= a; p[1] <= b;`` two 1-bit
        flops where Yosys has one 2-bit flop; ``r[3]`` and ``r[1:0]`` stay
        two).

        Each run's ``D`` is built from the block's decision tree as Yosys
        ``proc_mux`` builds it (rtl-buddy-cdc#394), one mux tree per
        *snippet* of the run (the bits no write splits, ``proc``'s
        ``SigSnippets``; :meth:`_proc_value`):

        - the value entering the block is the snippet's own ``Q`` bits —
          a register no taken arm writes holds;
        - a write replaces its slice of the current value;
        - an ``if`` / ``case`` (a :class:`_ProcSwitch`) that writes the
          snippet becomes a chain of ``mux(S=arm select, A=rest, B=arm
          value)``, the first arm outermost and the ``else`` / ``default``
          arm (else the value before the statement) innermost, each arm's
          value built from the value before the statement.

        ``if (cond) q <= a; else q <= b;`` drains to ``D = mux(cond, b,
        a)``, ``if (c1) q <= a; else if (c2) q <= b;`` to ``mux(c1,
        mux(c2, Q, b), a)``: the hold-feedback mux the rule pack's
        gated-bus detector reads the select of.

        ``reset_kind`` (``"async"`` / ``"sync"`` / ``None``) controls
        whether ``_emit_flop_cell`` materialises ``$adff``, ``$sdff``,
        or plain ``$dff``. ``None`` is also accepted alongside
        ``reset_sym=None`` for blocks with no reset. Reset values are
        assembled per bit from the reset arm's writes.

        ``async_controls`` (``(control bits, active low, values)`` in
        priority order) replaces the single reset for a block with several
        async controls: each run is built by
        :meth:`_emit_multi_async_flop`.

        ``clock_enable`` (with a ``"sync"`` reset) makes each flop an
        ``$sdffce`` loading only while that bit is high
        (rtl-buddy-cdc#384).
        """
        reset_bit: dict[Bit, Bit] = {}
        for key, value in self._proc_resets.items():
            for bit, v in zip(key, value):
                reset_bit[bit] = v
        # Bits a reset / async-load arm writes are register bits too, in
        # the same runs as the data writes (rtl-buddy-cdc#390).
        reset_written = set(reset_bit)
        for _bits, _low, control_values in async_controls or ():
            for key in control_values:
                reset_written.update(key)
        tree_writes = self._index_proc_tree(self._proc_root)
        for sym, net in self._proc_regs.items():
            writes = tree_writes.get(sym, [])
            written = sorted(
                {pos for start, end in writes for pos in range(start, end)}
                | {pos for pos, bit in enumerate(net) if bit in reset_written}
            )
            cuts = {pos for span in writes for pos in span}
            # The elements of an unpacked array are separate registers
            # (Yosys ``mem2reg``): a run never spans two of them.
            element = self._unpacked_element_width(sym)
            runs: list[tuple[int, int]] = []
            for pos in written:
                if runs and runs[-1][1] == pos and (not element or pos % element):
                    runs[-1] = (runs[-1][0], pos + 1)
                else:
                    runs.append((pos, pos + 1))
            for lo, hi in runs:
                q_bits = net[lo:hi]
                d: list[Bit] = []
                bounds = sorted({lo, hi} | {c for c in cuts if lo < c < hi})
                for a, b in zip(bounds, bounds[1:]):
                    d.extend(self._proc_value(self._proc_root, sym, a, b, net[a:b]))
                if async_controls is not None:
                    self._emit_multi_async_flop(
                        clk_sym, q_bits, tuple(d), async_controls, src_node
                    )
                    continue
                # A bit the reset arm does not write keeps its value while
                # the reset is asserted: its reset value is its own ``Q``,
                # as Yosys ``proc_dff`` reads it (rtl-buddy-cdc#390: it
                # reset to 0).
                values = tuple(reset_bit.get(bit, bit) for bit in q_bits)
                if reset_sym is not None and reset_kind == "sync":
                    if not self._sync_reset_folds(q_bits, d, values):
                        self._emit_sync_reset_muxes(
                            clk_sym, reset_sym, reset_active_low, q_bits,
                            d, values, src_node, clock_enable,
                        )  # fmt: skip
                        continue
                elif reset_sym is not None:
                    if values == q_bits:
                        # Nothing is reset: a plain flop holding under
                        # the reset (``proc_dff``'s mux on ``D``).
                        held = self._async_priority_mux(
                            self._signal_bits(reset_sym)[0],
                            reset_active_low,
                            rest=tuple(d),
                            asserted=q_bits,
                        )
                        self._emit_flop_cell(
                            clk_sym, None, False, q_bits, held, src_node
                        )
                        continue
                    if any(isinstance(v, int) for v in values):
                        # A non-constant async value: an async load.
                        self._emit_aldff(
                            clk_sym, reset_sym, reset_active_low, q_bits,
                            tuple(d), values, src_node,
                        )  # fmt: skip
                        continue
                reset_value = sum((1 << i) for i, v in enumerate(values) if v == "1")
                self._emit_flop_cell(
                    clk_sym,
                    reset_sym,
                    reset_active_low,
                    q_bits,
                    tuple(d),
                    src_node,
                    reset_value=reset_value,
                    reset_kind=reset_kind,
                    clock_enable=clock_enable,
                )

    def _index_proc_tree(self, case: _ProcCase) -> dict[Any, list[tuple[int, int]]]:
        """The ``(start, end)`` bit ranges each variable is written in under
        ``case``; fills each switch's :attr:`_ProcSwitch.writes` on the
        way."""
        out: dict[Any, list[tuple[int, int]]] = {}
        case.by_sym = {}
        for item in case.items:
            if isinstance(item, _ProcSwitch):
                item.writes = {}
                for _select, arm in item.arms:
                    for sym, spans in self._index_proc_tree(arm).items():
                        item.writes.setdefault(sym, []).extend(spans)
                for sym, spans in item.writes.items():
                    out.setdefault(sym, []).extend(spans)
                    case.by_sym.setdefault(sym, []).append(item)
            else:
                sym, start, bits = item
                out.setdefault(sym, []).append((start, start + len(bits)))
                case.by_sym.setdefault(sym, []).append(item)
        return out

    def _proc_value(
        self, case: _ProcCase, sym: Any, a: int, b: int, value: tuple[Bit, ...]
    ) -> tuple[Bit, ...]:
        """Bits ``[a, b)`` of ``sym`` after the decision-tree arm ``case``,
        ``value`` before it: ``proc_mux``'s ``signal_to_mux_tree``
        (see :meth:`_drain_proc_writes`)."""
        for item in case.by_sym.get(sym, ()):
            if isinstance(item, _ProcSwitch):
                if not any(s < b and a < e for s, e in item.writes.get(sym, ())):
                    continue
                result = value
                for select, arm in reversed(item.arms):
                    arm_value = self._proc_value(arm, sym, a, b, value)
                    if select is None:
                        result = arm_value
                    elif arm_value != result:
                        result = self._build_hold_mux(
                            select, hold=result, fire=arm_value
                        )
                value = result
                continue
            _sym, start, bits = item
            lo, hi = max(a, start), min(b, start + len(bits))
            if lo < hi:
                value = (
                    value[: lo - a] + bits[lo - start : hi - start] + value[hi - a :]
                )
        return value

    @staticmethod
    def _sync_reset_folds(
        q_bits: tuple[Bit, ...], d_bits: list[Bit], values: tuple[Bit, ...]
    ) -> bool:
        """Can a sync-reset register be an ``$sdff`` that reads the same as
        the ``proc`` form the ``lint`` recipe leaves (a constant-leg mux on
        ``D``)? Only when the reset arm loads a constant into every bit and
        the data branch writes every bit: a bit the reset leaves alone holds
        under it, and a bit only the reset writes has its own ``Q`` on the
        mux's other leg, which reads as an enable, not a reset
        (rtl-buddy-cdc#390)."""
        return all(isinstance(v, str) for v in values) and all(
            d != q for d, q in zip(d_bits, q_bits)
        )

    def _emit_sync_reset_muxes(
        self,
        clk_sym: Any,
        reset_sym: Any,
        reset_active_low: bool,
        q_bits: tuple[Bit, ...],
        d_bits: list[Bit],
        values: tuple[Bit, ...],
        src_node: Any,
        clock_enable: Bit | None,
    ) -> None:
        """A sync-reset register :meth:`_sync_reset_folds` rejects, as Yosys
        ``proc`` builds it: a plain ``$dff`` whose ``D`` is the reset mux
        (the reset value, or ``Q`` for a bit the reset leaves alone, while
        the reset is asserted, else the data), one mux per run of bits the
        same arms write, behind the load mux of a ``clock_enable``."""
        reset_bit = self._signal_bits(reset_sym)[0]

        def kind(i: int) -> tuple[bool, bool]:
            return isinstance(values[i], str), d_bits[i] != q_bits[i]

        out: list[Bit] = []
        lo = 0
        while lo < len(q_bits):
            hi = lo + 1
            while hi < len(q_bits) and kind(hi) == kind(lo):
                hi += 1
            out.extend(
                self._async_priority_mux(
                    reset_bit,
                    reset_active_low,
                    rest=tuple(d_bits[lo:hi]),
                    asserted=values[lo:hi],
                )
            )
            lo = hi
        d = tuple(out)
        if clock_enable is not None:
            d = self._build_hold_mux(clock_enable, hold=q_bits, fire=d)
        self._emit_flop_cell(clk_sym, None, False, q_bits, d, src_node)

    @staticmethod
    def _unpacked_element_width(sym: Any) -> int | None:
        """Bits per innermost element of an unpacked-array variable, or
        ``None`` for a packed one."""
        t = getattr(sym, "type", None)
        t = getattr(t, "canonicalType", None) or t
        if not getattr(t, "isUnpackedArray", False):
            return None
        while getattr(t, "isUnpackedArray", False):
            t = getattr(t, "elementType", None)
            t = getattr(t, "canonicalType", None) or t
        width = getattr(t, "selectableWidth", None) or getattr(t, "bitWidth", None)
        return int(width) if width else None

    def _build_hold_mux(
        self, enable: Bit, hold: tuple[Bit, ...], fire: tuple[Bit, ...]
    ) -> tuple[Bit, ...]:
        """Allocate a ``$mux(S=enable, A=hold, B=fire)`` cell and
        return its output bits."""
        width = len(hold)
        mux_y = self._alloc_anon_bits(width)
        mux_name = self._fresh_cell_name("$mux")
        self._cells[mux_name] = Cell(
            name=mux_name,
            type="$mux",
            connections={
                "A": hold,
                "B": fire,
                "S": (enable,),
                "Y": mux_y,
            },
            parameters={},
            attributes={},
        )
        return mux_y

    def _emit_flop_cell(
        self,
        clk_sym: Any,
        reset_sym: Any | None,
        reset_active_low: bool,
        q_bits: tuple[Bit, ...],
        d_bits: tuple[Bit, ...],
        src_node: Any,
        reset_value: int = 0,
        reset_kind: str | None = None,
        clock_enable: Bit | None = None,
    ) -> None:
        """Allocate a ``$dff`` / ``$adff`` / ``$sdff`` cell with the
        given D and Q (an ``$sdffce`` for a sync reset with a
        ``clock_enable``: the reset acts only while it is high).

        ``reset_kind`` picks the reset shape when ``reset_sym`` is
        non-None: ``"async"`` → ``$adff`` (ARST/ARST_POLARITY/
        ARST_VALUE), ``"sync"`` → ``$sdff`` (SRST/SRST_POLARITY/
        SRST_VALUE). ``None`` is treated as async to preserve the
        pre-#86 behaviour for any caller that hasn't been updated;
        ``_emit_procedural_block`` always passes an explicit kind
        when there is a reset.
        """
        connections: dict[str, tuple[Bit, ...]] = {
            "CLK": self._alloc_bits(clk_sym),
            "D": d_bits,
            "Q": q_bits,
        }
        cell_type = "$dff"
        if reset_sym is not None:
            if reset_kind == "sync":
                connections["SRST"] = self._signal_bits(reset_sym)
                cell_type = "$sdff"
            else:
                # ``"async"`` (or unspecified — pre-#86 default).
                connections["ARST"] = self._signal_bits(reset_sym)
                cell_type = "$adff"
        # CLK_POLARITY: read off the procedural-block lowering's
        # active clock edge (rtl-buddy-cdc#221 — closes the CDC-016
        # parity gap tracked in #224). ``_emit_procedural_block``
        # stashes ``self._clk_negedge`` from ``_analyse_event_list``
        # around its drain call. WIDTH / *RST_POLARITY match Yosys'
        # 32-bit binary-string param encoding; *RST_VALUE matches the
        # flop's bit width so the string length lines up with the
        # D / Q nets.
        width = len(q_bits)
        clk_polarity = "0" if getattr(self, "_clk_negedge", False) else "1"
        parameters: dict[str, str] = {
            "CLK_POLARITY": clk_polarity,
            "WIDTH": _param_int32(width),
        }
        if cell_type == "$sdff" and clock_enable is not None:
            cell_type = "$sdffce"
            connections["EN"] = (clock_enable,)
            parameters["EN_POLARITY"] = _param_int32(1)
        if reset_sym is not None:
            polarity = _param_int32(0 if reset_active_low else 1)
            value_param = _param_bits(reset_value, width)
            if cell_type in ("$sdff", "$sdffce"):
                parameters["SRST_POLARITY"] = polarity
                parameters["SRST_VALUE"] = value_param
            else:
                parameters["ARST_POLARITY"] = polarity
                parameters["ARST_VALUE"] = value_param
        attrs: dict[str, str] = {}
        src = self._src_attr(src_node) if src_node is not None else None
        if src is not None:
            attrs["src"] = src
        name = self._fresh_cell_name(cell_type)
        self._cells[name] = Cell(
            name=name,
            type=cell_type,
            connections=connections,
            parameters=parameters,
            attributes=attrs,
        )

    def _emit_multi_async_flop(
        self,
        clk_sym: Any,
        q_bits: tuple[Bit, ...],
        d_bits: tuple[Bit, ...],
        controls: list[
            tuple[tuple[Bit, ...], bool, dict[tuple[Bit, ...], tuple[Bit, ...]]]
        ],
        src_node: Any,
    ) -> None:
        """One register of a block with several async controls, as Yosys
        ``proc_dff`` builds it (rtl-buddy-cdc#384).

        Each control's value for the run is what its arm writes, or ``Q``
        for a bit the arm leaves alone. When every control loads the same
        value they collapse into one trigger, the ``$reduce_or`` of the
        controls made active-high: a plain ``$dff`` holding ``Q`` under it
        when that value is ``Q`` itself, else an ``$adff`` (constant) or
        ``$aldff``. Otherwise a ``$dffsr`` whose ``SET`` / ``CLR`` are
        priority mux chains, the first control outermost: ``SET`` takes
        the value and ``CLR`` its complement while a control is asserted.
        """
        width = len(q_bits)
        per_control: list[tuple[tuple[Bit, ...], bool, tuple[Bit, ...]]] = []
        for bits, active_low, values in controls:
            by_bit: dict[Bit, Bit] = {}
            for key, val in values.items():
                by_bit.update(zip(key, val))
            per_control.append(
                (bits, active_low, tuple(by_bit.get(b, b) for b in q_bits))
            )
        first = per_control[0][2]
        if all(v == first for _b, _l, v in per_control):
            triggers: list[Bit] = [
                self._lower_not(bits[0]) if active_low else bits[0]
                for bits, active_low, _v in per_control
            ]
            trigger = self._emit_comb_cell(
                "$reduce_or", {"A": tuple(triggers)}, output_width=1
            )
            if first == q_bits:
                held = self._build_hold_mux(trigger[0], hold=d_bits, fire=q_bits)
                self._emit_flop_cell(clk_sym, None, False, q_bits, held, src_node)
            elif any(isinstance(v, int) for v in first):
                self._emit_aldff(
                    clk_sym, trigger, False, q_bits, d_bits, first, src_node
                )
            else:
                self._emit_flop_cell(
                    clk_sym, trigger, False, q_bits, d_bits, src_node,
                    reset_value=sum(1 << i for i, v in enumerate(first) if v == "1"),
                    reset_kind="async",
                )  # fmt: skip
            return
        set_bits: tuple[Bit, ...] = ("0",) * width
        clr_bits: tuple[Bit, ...] = ("0",) * width
        for bits, active_low, value in reversed(per_control):
            if all(isinstance(v, str) for v in value):
                inverse: tuple[Bit, ...] = tuple(
                    {"0": "1", "1": "0"}.get(str(v), v) for v in value
                )
            else:
                inverse = self._emit_comb_cell("$not", {"A": value}, output_width=width)
            set_bits = self._async_priority_mux(bits[0], active_low, set_bits, value)
            clr_bits = self._async_priority_mux(bits[0], active_low, clr_bits, inverse)
        attrs: dict[str, str] = {}
        src = self._src_attr(src_node) if src_node is not None else None
        if src is not None:
            attrs["src"] = src
        name = self._fresh_cell_name("$dffsr")
        self._cells[name] = Cell(
            name=name,
            type="$dffsr",
            connections={
                "CLK": self._alloc_bits(clk_sym),
                "SET": set_bits,
                "CLR": clr_bits,
                "D": d_bits,
                "Q": q_bits,
            },
            parameters={
                "CLK_POLARITY": _param_int32(
                    0 if getattr(self, "_clk_negedge", False) else 1
                ),
                "SET_POLARITY": _param_int32(1),
                "CLR_POLARITY": _param_int32(1),
                "WIDTH": _param_int32(width),
            },
            attributes=attrs,
        )

    def _async_priority_mux(
        self,
        control: Bit,
        active_low: bool,
        rest: tuple[Bit, ...],
        asserted: tuple[Bit, ...],
    ) -> tuple[Bit, ...]:
        """``asserted`` while ``control`` is asserted, else ``rest``: a
        ``$mux`` selected by the control itself, its legs ordered by the
        polarity (Yosys' shape); no cell when both legs are the same."""
        if rest == asserted:
            return rest
        if active_low:
            return self._build_hold_mux(control, hold=asserted, fire=rest)
        return self._build_hold_mux(control, hold=rest, fire=asserted)

    def _signal_bits(self, signal: Any) -> tuple[Bit, ...]:
        """A control signal's bits: a variable symbol's net, or bits
        already built (the OR of several async controls)."""
        if isinstance(signal, tuple):
            return signal
        return self._alloc_bits(signal)

    def _emit_aldff(
        self,
        clk_sym: Any,
        aload_sym: Any,
        aload_active_low: bool,
        q_bits: tuple[Bit, ...],
        d_bits: tuple[Bit, ...],
        ad_bits: tuple[Bit, ...],
        src_node: Any,
    ) -> None:
        """An ``$aldff``: ``Q`` follows ``AD`` while ``ALOAD`` is asserted,
        else loads ``D`` on the clock edge — what Yosys ``proc`` builds for
        an async control whose value is not constant (rtl-buddy-cdc#384).
        Same parameters as ``proc_dff``'s ``gen_aldff``."""
        attrs: dict[str, str] = {}
        src = self._src_attr(src_node) if src_node is not None else None
        if src is not None:
            attrs["src"] = src
        name = self._fresh_cell_name("$aldff")
        self._cells[name] = Cell(
            name=name,
            type="$aldff",
            connections={
                "CLK": self._alloc_bits(clk_sym),
                "ALOAD": self._signal_bits(aload_sym),
                "AD": ad_bits,
                "D": d_bits,
                "Q": q_bits,
            },
            parameters={
                "ALOAD_POLARITY": "0" if aload_active_low else "1",
                "CLK_POLARITY": "0" if getattr(self, "_clk_negedge", False) else "1",
                "WIDTH": _param_int32(len(q_bits)),
            },
            attributes=attrs,
        )

    def _condition_bit(self, stmt: Any) -> Bit:
        """The single-bit condition of an ``if`` statement, or, when it does
        not lower (a pattern match, an unmodelled operator), a reported
        opaque select (rtl-buddy-cdc#377: such an ``if`` used to be walked
        as if both arms were unconditional)."""
        conds = getattr(stmt, "conditions", None)
        bit = self._lower_condition_to_bit(conds)
        if bit is not None:
            return bit
        node = conds[0].expr if conds and len(conds) == 1 else stmt
        return self._opaque(node, 1, "`if` condition")[0]

    def _negated_condition_bit(self, stmt: Any) -> Bit | None:
        """The operand bit of an ``if`` condition that negates a one-bit
        value (``!x``, ``~x``, ``!(a & b)``), or ``None``."""
        conds = list(getattr(stmt, "conditions", None) or [])
        expr = getattr(conds[0], "expr", None) if len(conds) == 1 else None
        while type(expr).__name__ == "ConversionExpression":
            expr = getattr(expr, "operand", None)
        if type(expr).__name__ != "UnaryExpression":
            return None
        op = str(getattr(expr, "op", "")).rsplit(".", 1)[-1]
        operand = getattr(expr, "operand", None)
        if op not in {"LogicalNot", "BitwiseNot"} or self._expr_width(operand) != 1:
            return None
        bits = self._bits_of_expression(operand)
        return bits[0] if bits is not None and len(bits) == 1 else None

    def _lower_condition_to_bit(self, conditions: Any) -> Bit | None:
        """Lower a ``ConditionalStatement``'s condition list to a
        single Yosys-shape bit suitable for use as a mux ``S`` input.

        pyslang represents the conditions as a list of
        ``ConditionalPattern`` entries; for the canonical
        ``if (expr)`` form there is exactly one entry whose ``.expr``
        is the bool/scalar expression. Returns ``None`` if the
        condition can't be lowered to a single bit (multi-pattern
        match, anything ``_bits_of_expression`` doesn't model yet) —
        the caller treats that as "no enable inferred" and falls
        back to the unconditional emit path.
        """
        condition_list = list(conditions) if conditions else []
        if len(condition_list) != 1:
            return None
        expr = getattr(condition_list[0], "expr", None)
        if expr is None:
            return None
        bits = self._bits_of_expression(expr)
        if bits is None:
            return None
        # ``if (expr)`` treats the expression as a boolean: a multi-bit
        # value is true when non-zero, the ``$reduce_bool`` Yosys emits
        # for it (rtl-buddy-cdc#377; such a condition used to lose its
        # enable or, in a combinational block, its mux).
        if len(bits) == 1:
            return bits[0]
        if not bits:
            return None
        return self._emit_comb_cell(
            "$reduce_bool", {"A": bits}, output_width=1, src_node=expr
        )[0]

    def _alloc_anon_bits(self, width: int) -> tuple[Bit, ...]:
        """Allocate ``width`` fresh anonymous bits for a comb cell's
        output. Same allocator pool as named variables; no Symbol is
        registered because these aren't user-visible nets."""
        bits = tuple(range(self._next_bit_id, self._next_bit_id + width))
        self._next_bit_id += width
        return bits

    def _lvalue_bits(self, expr: Any) -> tuple[Bit, ...] | None:
        """Resolve an LHS expression to the variable-bit subset it
        writes. A concatenation target gives its parts' bits joined
        LSB-first (an output port bound to ``{a, b}``); procedural and
        continuous writes split one first, see :meth:`_concat_writes`."""
        kind = type(expr).__name__
        if self._is_named(expr):
            return self._alloc_bits(expr.symbol)
        if kind in {"ElementSelectExpression", "MemberAccessExpression"}:
            return self._element_select_bits(expr)
        if kind == "RangeSelectExpression":
            return self._range_select_bits(expr)
        if kind == "ConcatenationExpression":
            targets = self._lvalue_targets(expr)
            if targets is None:
                return None
            out: list[Bit] = []
            for sym, start, width in reversed(targets):
                out.extend(self._alloc_bits(sym)[start : start + width])
            return tuple(out)
        return None

    # -- concatenation / partial l-values (rtl-buddy-cdc#370) -------------

    def _lvalue_targets(self, expr: Any) -> list[tuple[Any, int, int]] | None:
        """The bit ranges an assignment target writes, as ``(variable,
        start, width)`` triples listed MSB-first (source order of a
        concatenation), ``start`` counted LSB-first within the
        variable's bits. ``None`` when any part is not a constant
        select of a variable (a run-time index, a streaming
        concatenation, a hierarchical reference, …).

        A part is a whole variable, a bit / part select (any declared
        range, ``+:`` / ``-:`` included, via :meth:`_range_select_span`),
        a packed-struct member, or a nested concatenation.
        """
        kind = type(expr).__name__
        if kind == "ConcatenationExpression":
            out: list[tuple[Any, int, int]] = []
            for op in getattr(expr, "operands", None) or []:
                sub = self._lvalue_targets(op)
                if sub is None:
                    return None
                out.extend(sub)
            return out or None
        if self._is_named(expr):
            sym = self._canonical_var(expr.symbol)
            return [(sym, 0, len(self._alloc_bits(sym)))]
        if kind == "StreamingConcatenationExpression":
            return self._streaming_targets(expr)
        if kind in {"ElementSelectExpression", "MemberAccessExpression"}:
            sym, bits, start, width = self._resolve_select_chain(expr)
        elif kind == "RangeSelectExpression":
            span = self._range_select_span(expr)
            if span is None:
                return None
            sym, bits, start, width = span
        else:
            return None
        if sym is None or width < 1 or start < 0 or start + width > len(bits):
            return None
        return [(self._canonical_var(sym), start, width)]

    def _streaming_targets(self, expr: Any) -> list[tuple[Any, int, int]] | None:
        """A streaming-concatenation target (``{<<{o}} = a``,
        ``{<<4{x, y}} <= d``): its operands' bits, one target per bit,
        in the order :meth:`_lower_streaming` reads them, so the source's
        MSB lands in the stream's first bit (rtl-buddy-cdc#405: dropped
        and reported). ``None`` for a ``with`` range or a dynamic size."""
        if not getattr(expr, "isFixedSize", False):
            return None
        stream: list[tuple[Any, int, int]] = []  # MSB first
        for st in getattr(expr, "streams", None) or []:
            if getattr(st, "withExpr", None) is not None:
                return None
            sub = self._lvalue_targets(st.operand)
            if sub is None:
                return None
            for sym, start, width in sub:
                stream.extend((sym, start + k, 1) for k in reversed(range(width)))
        if self._streaming_op(expr) == "<<":
            size = int(getattr(expr, "sliceSize", 0) or 0) or 1
            chunks = [stream[i : i + size] for i in range(0, len(stream), size)]
            stream = [t for chunk in reversed(chunks) for t in chunk]
        return stream or None

    @staticmethod
    def _streaming_op(expr: Any) -> str:
        """``"<<"`` or ``">>"``: a streaming concatenation's direction."""
        token = getattr(getattr(expr, "syntax", None), "operatorToken", None)
        return str(getattr(token, "valueText", "") or "").strip()

    @staticmethod
    def _group_parts(
        targets: list[tuple[Any, int, int]], pieces: list[tuple[Bit, ...]]
    ) -> list[tuple[Any, int, tuple[Bit, ...]]]:
        """Pair each target with its right-hand side ``piece`` and merge
        the parts of one variable into contiguous runs, whatever their
        order in the concatenation: ``{q[3], b, q[2:0]}`` and ``{hi[1:2],
        hi[0], hi[3]}`` write ``q[3:0]`` / all of ``hi`` as one register,
        as Yosys ``proc`` groups them; ``{g[3], g[1:0]}`` stays two.
        Returns ``(variable, start, bits)`` with ``bits`` LSB-first."""
        by_var: dict[Any, list[tuple[int, tuple[Bit, ...]]]] = {}
        for (sym, start, _width), piece in zip(targets, pieces):
            by_var.setdefault(sym, []).append((start, piece))
        out: list[tuple[Any, int, tuple[Bit, ...]]] = []
        for sym, parts in by_var.items():
            parts.sort(key=lambda p: p[0])
            run_start, run_bits = parts[0][0], list(parts[0][1])
            for start, piece in parts[1:]:
                if start == run_start + len(run_bits):
                    run_bits.extend(piece)
                else:
                    out.append((sym, run_start, tuple(run_bits)))
                    run_start, run_bits = start, list(piece)
            out.append((sym, run_start, tuple(run_bits)))
        return out

    @staticmethod
    def _resize_bits(
        bits: tuple[Bit, ...], width: int, signed: bool = False
    ) -> tuple[Bit, ...]:
        """Truncate ``bits`` (LSB-first) to ``width``, or extend them with
        zeros, or with copies of the MSB when ``signed``."""
        if len(bits) >= width:
            return tuple(bits[:width])
        pad: Bit = bits[-1] if signed and bits else "0"
        return tuple(bits) + (pad,) * (width - len(bits))

    def _sized_bits(self, expr: Any) -> tuple[Bit, ...] | None:
        """:meth:`_bits_of_expression`, except that the implicit width
        conversions slang wraps around an assignment's right-hand side
        are applied: zero extension, sign extension of a signed operand,
        truncation. Operator results are already sized by their type
        (``acc + inc`` assigned to a 5-bit target is a 5-bit ``$add``, so
        its top bit is the real carry)."""
        if type(expr).__name__ == "ConversionExpression":
            operand = getattr(expr, "operand", None)
            if operand is None:
                return None
            inner = self._sized_bits(operand)
            if inner is None:
                return None
            width = self._expr_width(expr) or len(inner)
            # Sign extension needs a signed operand *and* a signed target:
            # slang converts a signed operand of an unsigned expression
            # (``a + b``, ``a`` signed, ``b`` not) straight to the
            # unsigned type, which zero-extends (IEEE 1800-2017 11.8.2),
            # and an assignment's signed right-hand side to its own signed
            # type first, then to the target's.
            signed = all(
                getattr(getattr(e, "type", None), "isSigned", False)
                for e in (operand, expr)
            )
            return self._resize_bits(inner, width, signed)
        return self._bits_of_expression(expr)

    def _sized_rhs_bits(self, expr: Any, width: int) -> tuple[Bit, ...] | None:
        """Right-hand side bits sized to a ``width``-bit target."""
        bits = self._sized_bits(expr)
        if bits is None:
            return None
        return self._resize_bits(bits, width)

    @staticmethod
    def _split_rhs(
        rhs_bits: tuple[Bit, ...], targets: list[tuple[Any, int, int]]
    ) -> list[tuple[Bit, ...]]:
        """Slice LSB-first ``rhs_bits`` across MSB-first ``targets``:
        the last target takes the least significant bits."""
        pieces: list[tuple[Bit, ...]] = [()] * len(targets)
        offset = 0
        for i in range(len(targets) - 1, -1, -1):
            width = targets[i][2]
            pieces[i] = tuple(rhs_bits[offset : offset + width])
            offset += width
        return pieces

    def _concat_writes(
        self, lhs: Any, rhs: Any
    ) -> list[tuple[tuple[Bit, ...], tuple[Bit, ...]]] | None:
        """Split ``{a, b, …} <= rhs`` into one ``(target bits, rhs
        bits)`` write per register run (see :meth:`_group_parts`) — what a register / latch
        lowering needs. ``rhs`` is sized to the concatenation's total
        width first; an unlowerable ``rhs`` gets an ``$_UNKNOWN_`` driver
        like any other register input. ``None`` when a part cannot be
        resolved (the caller reports the assignment)."""
        targets = self._lvalue_targets(lhs)
        if targets is None:
            return None
        total = sum(width for _sym, _start, width in targets)
        rhs_bits = self._rhs_bits(rhs, total)
        return [
            (self._alloc_bits(sym)[start : start + len(bits)], bits)
            for sym, start, bits in self._group_parts(
                targets, self._split_rhs(rhs_bits, targets)
            )
        ]

    def _splice_assign(self, lhs: Any, rhs: Any, *, continuous: bool) -> bool:
        """Lower a combinational write to a concatenation or to part of a
        variable (``{c, s} = a + b``, ``bus[3:0] = x``, ``st.f = y``):
        each written range of each variable is re-pointed at its slice of
        the right-hand side, the variable's other bits keep their current
        driver. ``False`` when the target cannot be resolved.

        ``continuous`` records the re-pointing for
        :meth:`_apply_forward_renames` as :meth:`_emit_continuous_assign`
        does for a whole-variable ``assign``.
        """
        targets = self._lvalue_targets(lhs)
        if targets is None:
            return False
        total = sum(width for _sym, _start, width in targets)
        rhs_bits = self._rhs_bits(rhs, total)
        for (sym, start, width), piece in zip(
            targets, self._split_rhs(rhs_bits, targets)
        ):
            if continuous:
                self._drive_net(sym, start, piece, lhs)
                continue
            old = self._alloc_bits(sym)
            new = old[:start] + piece + old[start + width :]
            self._merge_canonical_var(sym, old, new)
        return True

    # Resolved net types whose drivers combine (IEEE 1800-2017 6.6.3).
    _WIRED_NETS = {"WOr": "$or", "TriOr": "$or", "WAnd": "$and", "TriAnd": "$and"}

    def _drive_net(
        self, sym: Any, start: int, piece: tuple[Bit, ...], node: Any
    ) -> None:
        """A continuous driver of ``sym``'s bits ``start ..``: its net's
        placeholder bits are renamed to ``piece`` and its value becomes
        ``piece`` there.

        A bit that already has a continuous driver has two: on a ``wor`` /
        ``wand`` net their ``$or`` / ``$and``, as the net resolves them; on
        any other net their ``$or`` too, reported, so neither driver's
        fan-in is lost (rtl-buddy-cdc#405: the second ``assign`` renamed
        the first one's source net to its own, shorting two inputs)."""
        canonical = self._canonical_var(sym)
        net = self._net_bits.get(canonical) or self._alloc_bits(canonical)
        value = list(self._var_bits.get(canonical, net))
        clashes: list[tuple[int, Bit, Bit]] = []
        for k, n in enumerate(piece):
            i = start + k
            o = net[i]
            if o not in self._placeholder_bits:
                value[i] = n
                continue
            prev = self._forward_renames.get(o)
            if prev is None or prev == n:
                if o != n:
                    self._forward_renames[o] = n
                value[i] = n
            else:
                clashes.append((i, prev, n))
        if clashes:
            kind = str(getattr(getattr(canonical, "netType", None), "netKind", ""))
            cell = self._WIRED_NETS.get(kind.rsplit(".", 1)[-1])
            if cell is None:
                cell = "$or"
                _log.warning(
                    "slang frontend: %s: net `%s` has more than one continuous "
                    "driver; modelled as their OR, so neither driver's fan-in "
                    "is lost",
                    self._src_attr(node) or "<unknown location>",
                    getattr(canonical, "name", "?"),
                )
            merged = self._emit_comb_cell(
                cell,
                {
                    "A": tuple(prev for _i, prev, _n in clashes),
                    "B": tuple(n for _i, _prev, n in clashes),
                },
                len(clashes),
                node,
            )
            for (i, _prev, _n), m in zip(clashes, merged):
                self._forward_renames[net[i]] = m
                value[i] = m
        self._var_bits[canonical] = tuple(value)

    def _lvalue_unsupported_reason(self, expr: Any) -> str:
        """Why :meth:`_lvalue_targets` cannot resolve ``expr``."""
        kind = type(expr).__name__
        if kind == "ConcatenationExpression":
            for op in getattr(expr, "operands", None) or []:
                if self._lvalue_targets(op) is None:
                    return self._lvalue_unsupported_reason(op)
        if kind == "StreamingConcatenationExpression":
            return "target is a streaming concatenation"
        base = expr
        while type(base).__name__ in self._SELECT_KINDS:
            base = getattr(base, "value", None)
        if type(base).__name__ == "HierarchicalValueExpression" and not self._is_named(
            base
        ):
            return "target is a hierarchical reference outside the lowered design"
        if kind in {"ElementSelectExpression", "RangeSelectExpression"}:
            return "target select has a run-time index or is out of range"
        if kind == "MemberAccessExpression":
            return "target is a member of an unpacked or indexed aggregate"
        return f"target `{kind}` is not modelled"

    def _warn_lvalue_dropped(self, node: Any, lhs: Any) -> None:
        """:meth:`_warn_dropped` for an assignment whose target cannot be
        lowered, once per source location (an unrolled ``for`` body is
        walked once per iteration)."""
        reason = self._lvalue_unsupported_reason(lhs)
        key = (self._src_attr(node), reason)
        if key in self._lvalue_warned:
            self._lowering_failures += 1
            return
        self._lvalue_warned.add(key)
        self._warn_dropped(node, reason, "assignment")

    def _element_select_bits(self, expr: Any) -> tuple[Bit, ...] | None:
        """``var[i]`` (and ``var[i][j]``, ``var[i][j][k]``, ...) —
        return the bit subset that the chain picks out of the
        underlying named variable.

        Walks an ``ElementSelectExpression`` chain inward through
        ``.value`` until it reaches a ``NamedValueExpression``, then
        slices the allocated bit pool at the linearised offset.
        Stride at each level is the inner type's
        ``selectableWidth``:

        - Packed array ``logic [W-1:0] data``: scalar element,
          stride 1 — ``data[i]`` returns one bit.
        - Unpacked array ``logic chain [N]``: scalar element,
          stride 1 — ``chain[i]`` returns one bit.
        - Packed-and-unpacked ``logic [W-1:0] chain [STAGES]``:
          W-bit element, stride W — ``chain[i]`` returns W bits.
        - 2-D unpacked ``logic [W-1:0] arr [R][C]``: outer
          ``arr[i]`` returns the C*W-bit row, inner ``arr[i][j]``
          returns the W-bit element.

        Falls back to ``None`` when any selector isn't a compile-time
        constant or the chain bottoms out somewhere other than a
        bare named variable (e.g. a hierarchical or virtual-interface
        select).
        """
        base_sym, base_bits, offset, stride = self._resolve_select_chain(expr)
        if base_sym is None:
            return None
        start = offset
        end = offset + stride
        if start < 0 or end > len(base_bits):
            return None
        return tuple(base_bits[start:end])

    def _resolve_select_chain(
        self, expr: Any
    ) -> tuple[Any | None, tuple[Bit, ...], int, int]:
        """Walk an ``ElementSelectExpression`` chain inward to the
        underlying named variable, accumulating a linearised bit
        offset and current stride.

        Returns ``(base_sym, base_bits, offset_in_bits, stride_in_bits)``.
        ``base_sym`` is ``None`` when the chain can't be resolved
        (non-constant selector, non-NamedValueExpression base, …).
        """
        kind = type(expr).__name__
        if self._is_named(expr):
            sym = getattr(expr, "symbol", None)
            if sym is None or self._canonical_var(sym) in self._memories:
                # A memory's words are read and written through ports.
                return None, (), 0, 0
            if self._is_constant_name(sym):
                # An unrolled loop's variable, a genvar or a parameter: a
                # select of it reads its value's bits (rtl-buddy-cdc#402:
                # ``k[0]`` read the variable's undriven placeholder).
                value = self._bits_of_expression(expr)
                if not value:
                    return None, (), 0, 0
                return sym, value, 0, len(value)
            bits = self._alloc_bits(sym)
            t = getattr(sym, "type", None)
            stride = int(
                getattr(t, "selectableWidth", None)
                or getattr(t, "bitWidth", None)
                or len(bits)
                or 1
            )
            return sym, bits, 0, stride
        if kind == "ElementSelectExpression":
            inner_sym, inner_bits, inner_offset, inner_stride = (
                self._resolve_select_chain(expr.value)
            )
            if inner_sym is None:
                return None, (), 0, 0
            idx = self._const_int(getattr(expr, "selector", None))
            if idx is None:
                return None, (), 0, 0
            # Stride at this level = the inner type's element width.
            inner_type = getattr(expr.value, "type", None)
            new_stride = self._element_stride(inner_type)
            pos = self._select_position(inner_type, idx)
            new_offset = inner_offset + pos * new_stride
            return inner_sym, inner_bits, new_offset, new_stride
        if kind == "RangeSelectExpression":
            # A select of a part-select: slang builds ``src[3:2][2]`` for
            # an instance-array element's share of ``src[3:2]``
            # (rtl-buddy-cdc#353). The part-select's own type keeps the
            # selected indices (``logic[3:2]``), so the outer select is
            # translated against it by the ``ElementSelect`` branch.
            span = self._range_select_span(expr)
            if span is None:
                return None, (), 0, 0
            return span
        if kind == "MemberAccessExpression":
            # A member of a packed struct / union (rtl-buddy-cdc#370):
            # ``FieldSymbol.bitOffset`` counts from the aggregate's LSB,
            # as Yosys lays the struct out. Unpacked aggregates have no
            # integral layout here and stay unresolved.
            value = getattr(expr, "value", None)
            value_type = getattr(value, "type", None)
            member = getattr(expr, "member", None)
            offset = getattr(member, "bitOffset", None)
            if (
                value is None
                or not getattr(value_type, "isIntegral", False)
                or offset is None
            ):
                return None, (), 0, 0
            inner_sym, inner_bits, inner_offset, _inner_stride = (
                self._resolve_select_chain(value)
            )
            if inner_sym is None:
                return None, (), 0, 0
            width = int(getattr(getattr(member, "type", None), "bitWidth", 0) or 0)
            if width < 1:
                return None, (), 0, 0
            return inner_sym, inner_bits, inner_offset + int(offset), width
        return None, (), 0, 0

    def _is_constant_name(self, sym: Any) -> bool:
        """``sym`` reads as a value, not a net: a bound loop variable, a
        parameter (a genvar inside its generate block is one) or an enum
        value."""
        return sym in self._loop_bindings or self._kind_name(sym) in (
            self._CONSTANT_SYMBOLS
        )

    @staticmethod
    def _element_stride(array_type: Any) -> int:
        """Bits per element of ``array_type`` (1 for a bit vector)."""
        elem_type = getattr(array_type, "elementType", None)
        if elem_type is None:
            return 1
        return int(
            getattr(elem_type, "selectableWidth", None)
            or getattr(elem_type, "bitWidth", None)
            or 1
        )

    @staticmethod
    def _select_position(array_type: Any, index: int) -> int:
        """Element position of declared ``index`` within ``array_type``'s
        outermost dimension, counted from the start of its bit storage.

        A packed dimension is stored LSB-first, as Yosys numbers port bits:
        ``a[1]`` of ``logic [4:1] a`` is position 0, and ``b[0]`` of
        ``logic [0:3] b`` is position 3 (the MSB). An unpacked dimension
        keeps this frontend's existing layout, lowest index first. A type
        without a declared range (``int``, a packed struct) is taken as
        ``[W-1:0]``.
        """
        t = getattr(array_type, "canonicalType", None) or array_type
        rng = getattr(t, "range", None)
        left = getattr(rng, "left", None)
        right = getattr(rng, "right", None)
        if left is None or right is None:
            return index
        left, right = int(left), int(right)
        if getattr(t, "isUnpackedArray", False):
            return index - min(left, right)
        if left >= right:
            return index - right
        return right - index

    def _range_select_span(
        self, expr: Any
    ) -> tuple[Any, tuple[Bit, ...], int, int] | None:
        """Resolve a constant part-select to ``(base_sym, base_bits,
        offset, width)`` in the same form :meth:`_resolve_select_chain`
        returns, or ``None``.

        Handles ``var[hi:lo]`` and the indexed forms ``var[i +: W]`` /
        ``var[i -: W]`` on any resolvable base (a variable, or a select
        of one), with the indices translated through the base's declared
        range by :meth:`_select_position`.
        """
        base_sym, base_bits, base_offset, _stride = self._resolve_select_chain(
            expr.value
        )
        if base_sym is None:
            return None
        offsets = self._range_offsets(expr)
        if offsets is None:
            return None
        start = base_offset + offsets[0]
        if start + offsets[1] > len(base_bits):
            return None
        return base_sym, base_bits, start, offsets[1]

    def _range_offsets(self, expr: Any) -> tuple[int, int] | None:
        """``(start, width)`` of a constant part-select within the bits of
        the value it selects from (:meth:`_range_select_span`), or
        ``None``."""
        left = self._const_int(getattr(expr, "left", None))
        right = self._const_int(getattr(expr, "right", None))
        if left is None or right is None:
            return None
        sel_kind = str(getattr(expr, "selectionKind", "")).rsplit(".", 1)[-1]
        if sel_kind == "IndexedUp":
            if right < 1:
                return None
            left, right = left, left + right - 1
        elif sel_kind == "IndexedDown":
            if right < 1:
                return None
            left, right = left, left - right + 1
        value_type = getattr(expr.value, "type", None)
        stride = self._element_stride(value_type)
        p_left = self._select_position(value_type, left)
        p_right = self._select_position(value_type, right)
        lo, hi = min(p_left, p_right), max(p_left, p_right)
        if lo < 0:
            return None
        return lo * stride, (hi - lo + 1) * stride

    def _range_select_bits(self, expr: Any) -> tuple[Bit, ...] | None:
        """``var[hi:lo]`` / ``var[i +: W]`` / ``var[i -: W]`` — return the
        contiguous bit subset (see :meth:`_range_select_span`).

        Yosys stores bits LSB-first, so ``var[3:0]`` of ``logic [3:0]
        var`` returns ``var_bits[0:4]``. Non-constant ranges return
        ``None``.
        """
        span = self._range_select_span(expr)
        if span is None:
            return None
        _sym, base_bits, start, width = span
        return tuple(base_bits[start : start + width])

    def _const_int(self, expr: Any) -> int | None:
        """Best-effort: extract an ``int`` from a constant pyslang
        expression. Returns ``None`` if the expression isn't a
        compile-time constant — selectors on the LHS need to be
        static for the flop-shape model to work.

        Shapes handled, in order:

        - :class:`NamedValueExpression` referring to a procedural
          for-loop iteration variable that's currently bound by
          :attr:`_loop_bindings` — returns the iteration value.
          Without this, ``chain[i]`` inside an unrolled body wouldn't
          fold to a per-iteration constant (pyslang considers ``i``
          a runtime variable; only the unroller knows its current
          value).
        - :class:`IntegerLiteral` — ``expr.value`` is an :class:`SVInt`
          which :func:`int` accepts directly.
        - :class:`NamedValueExpression` referring to a parameter or a
          genvar iteration value — ``expr.constant`` is a
          :class:`ConstantValue` wrapper; the inner scalar lives on
          ``.value`` (also :class:`SVInt`) or comes back from
          :meth:`ConstantValue.convertToInt`.
        - Other constant-foldable expressions — ``.constant`` is
          populated post-compilation; same unwrapping applies.
        - :class:`BinaryExpression` / :class:`UnaryExpression` over
          already-foldable operands. ``chain[i-1]`` parses as
          ``BinaryExpression(Subtract, NamedValue(i), IntegerLiteral(1))``
          and pyslang won't fold the outer node because ``i`` is a
          runtime variable from its perspective — recursing
          per-operand and combining works because the loop-binding
          path makes each operand foldable.
        """
        if expr is None:
            return None

        kind = type(expr).__name__

        # Loop-variable binding takes priority — pyslang considers the
        # loopvar a runtime variable, so its own ``.constant`` is None.
        if kind in self._NAME_KINDS:
            sym = getattr(expr, "symbol", None)
            if sym is not None and sym in self._loop_bindings:
                return self._loop_bindings[sym]
            # ParameterSymbol carries its compile-time value on the
            # symbol's ``.value`` field; the NamedValueExpression that
            # references it doesn't populate ``.constant`` (pyslang
            # leaves that for explicit constant expressions).
            if sym is not None and type(sym).__name__ == "ParameterSymbol":
                pv = getattr(sym, "value", None)
                if pv is not None:
                    # Reuse the lower-block's coercer below by falling
                    # through — but inline the unwrap so we don't have
                    # to lift _coerce out of scope yet.
                    try:
                        return int(pv)
                    except (TypeError, ValueError):
                        inner_v = getattr(pv, "value", None)
                        if inner_v is not None:
                            try:
                                return int(inner_v)
                            except (TypeError, ValueError):
                                pass

        def _coerce(v: Any) -> int | None:
            # Direct path (SVInt, IntegerLiteral.value, plain int).
            try:
                return int(v)
            except (TypeError, ValueError):
                pass
            # ConstantValue → inner SVInt via ``.value``.
            inner = getattr(v, "value", None)
            if inner is not None and inner is not v:
                try:
                    return int(inner)
                except (TypeError, ValueError):
                    pass
            # ConstantValue → explicit conversion.
            convert = getattr(v, "convertToInt", None)
            if callable(convert):
                try:
                    return int(convert())
                except (TypeError, ValueError):
                    pass
            return None

        for attr in ("constant", "value"):
            v = getattr(expr, attr, None)
            if v is None:
                continue
            n = _coerce(v)
            if n is not None:
                return n

        # Recursive fold for arithmetic over already-foldable operands.
        # The common shapes are ``i - 1`` / ``i + 1`` / ``i * N`` in
        # the body of an unrolled for-loop; without this fold, pyslang
        # would leave the outer BinaryExpression unfolded because ``i``
        # is a runtime variable from its perspective.
        if kind == "BinaryExpression":
            op = str(getattr(expr, "op", "")).rsplit(".", 1)[-1]
            left = self._const_int(getattr(expr, "left", None))
            right = self._const_int(getattr(expr, "right", None))
            if left is None or right is None:
                return None
            return _fold_binary(op, left, right)
        if kind == "UnaryExpression":
            op = str(getattr(expr, "op", "")).rsplit(".", 1)[-1]
            operand = self._const_int(getattr(expr, "operand", None))
            if operand is None:
                return None
            if op == "Plus":
                return operand
            if op == "Minus":
                return -operand
            if op == "LogicalNot":
                return int(operand == 0)
            return None
        if kind == "ConversionExpression":
            return self._const_int(getattr(expr, "operand", None))
        if kind in ("ElementSelectExpression", "RangeSelectExpression"):
            return self._const_select_int(expr)

        return None

    def _const_select_int(self, expr: Any) -> int | None:
        """A constant select of an unrolled loop variable, a genvar or a
        parameter (``k[0]``, ``k[2:1]``) as an unsigned ``int``
        (rtl-buddy-cdc#402); ``None`` for a select of anything else or at
        a run-time position. Emits no cells."""
        base = expr
        while type(base).__name__ in (
            "ElementSelectExpression",
            "RangeSelectExpression",
            "ConversionExpression",
        ):
            base = getattr(base, "value", None) or getattr(base, "operand", None)
        sym = getattr(base, "symbol", None)
        if not self._is_named(base) or sym is None:
            return None
        if not self._is_constant_name(sym):
            return None
        base_sym, bits, offset, width = self._resolve_select_chain(expr)
        if base_sym is None or offset < 0 or offset + width > len(bits):
            return None
        picked = bits[offset : offset + width]
        if any(b not in ("0", "1") for b in picked):
            return None
        return sum(1 << i for i, b in enumerate(picked) if b == "1")

    def _bits_of_expression(self, expr: Any) -> tuple[Bit, ...] | None:
        """Return the bit tuple for an RHS expression, or ``None`` if
        the shape isn't supported yet.

        Handles the common cases:
        - :class:`NamedValueExpression` — direct variable read.
        - :class:`ConversionExpression` — pass through (pyslang inserts
          these for implicit width / type unification; the underlying
          bit identity is what we want).
        - :class:`IntegerLiteral` — constants encoded as Yosys constant
          chars (``"0"`` / ``"1"``) in LSB-first order.
        - :class:`BinaryExpression` — lowered to a Yosys-shape comb
          cell (``$and``/``$or``/``$xor``/…) via :meth:`_lower_binary`.
        - :class:`UnaryExpression` — lowered to ``$not``/``$logic_not``/
          ``$neg``/``$reduce_*`` via :meth:`_lower_unary`.
        - :class:`ConditionalExpression` — lowered to a ``$mux`` cell.
        - :class:`ElementSelectExpression` / :class:`RangeSelectExpression`
          — return the matching bit subset of the underlying variable.
        - :class:`ConcatenationExpression` — concatenate operand bit
          tuples in MSB→LSB SV order, producing a single LSB-first
          tuple. See :meth:`_lower_concatenation`.
        - :class:`ReplicationExpression` — ``{N{x}}`` repeats the inner
          concat's bits ``N`` times. See :meth:`_lower_replication`.

        - :class:`MemberAccessExpression` on a packed struct / union —
          the field's bits (rtl-buddy-cdc#370).

        Anything else (function call, unpacked-struct member access,
        …) returns ``None``; the caller substitutes an ``$_UNKNOWN_``
        driver so the rule pack treats the upstream as opaque rather
        than crashing.
        """
        kind = type(expr).__name__
        if self._is_named(expr):
            if expr.symbol in self._loop_bindings:
                # An unrolled loop's variable read as a value.
                value = self._loop_bindings[expr.symbol]
                width = self._expr_width(expr) or 32
                return tuple("1" if (value >> i) & 1 else "0" for i in range(width))
            if self._kind_name(expr.symbol) in self._CONSTANT_SYMBOLS:
                # A parameter / enum value is its value, not a net
                # (rtl-buddy-cdc#377: it read as an undriven placeholder).
                param = getattr(expr.symbol, "value", None)
                bits = self._value_bits(param, self._expr_width(expr))
                if bits is None:
                    bits = self._unpacked_value_bits(param, expr.symbol.type)
                return bits
            return self._alloc_bits(expr.symbol)
        if kind == "ConversionExpression":
            # A width conversion slang inserts anywhere an operand meets a
            # wider or narrower context: zero or sign extension (a signed
            # operand's MSB fans out to the new bits), or truncation
            # (rtl-buddy-cdc#405: only an assignment's outermost conversion
            # was applied, so a signed operand of ``s ? a : b`` or ``a * b``
            # was zero-extended and the MSB's fan-in to the upper bits lost).
            return self._sized_bits(expr)
        if kind in {"IntegerLiteral", "UnbasedUnsizedIntegerLiteral"}:
            return self._value_bits(
                getattr(expr, "value", None), self._expr_width(expr)
            )
        if kind == "BinaryExpression":
            return self._lower_binary(expr)
        if kind == "UnaryExpression":
            return self._lower_unary(expr)
        if kind == "ConditionalExpression":
            return self._lower_conditional(expr)
        if kind in self._SELECT_KINDS:
            return self._lower_select(expr)
        if kind == "ConcatenationExpression":
            return self._lower_concatenation(expr)
        if kind == "ReplicationExpression":
            return self._lower_replication(expr)
        if kind == "InsideExpression":
            return self._lower_inside(expr)
        if kind == "StreamingConcatenationExpression":
            return self._lower_streaming(expr)
        if kind == "CallExpression":
            return self._lower_call(expr)
        if kind == "_Bits":
            return expr.bits
        if kind == "LValueReferenceExpression":
            # ``x += 1`` reads its own target (rtl-buddy-cdc#377).
            if self._compound_lhs:
                return self._bits_of_expression(self._compound_lhs[-1])
            return None
        # Anything else slang folded at elaboration time (``$bits(x)``, …).
        return self._constant_bits(expr)

    def _lower_select(self, expr: Any) -> tuple[Bit, ...] | None:
        """An element / part / member select read.

        A memory's word is its ``$memrd_v2`` port (at a constant address
        too); a select resolvable to a variable's bits is those bits; a
        register array's element at a run-time index is a ``$pmux``
        (:meth:`_array_mux`); a packed value at a run-time position a
        ``$shiftx`` (:meth:`_lower_dynamic_select`); a constant select of
        any other value (a word read through a port or a mux:
        ``mem[i][3:0]``, ``regs[i].f``) slices that value's bits
        (rtl-buddy-cdc#398: those reads were opaque)."""
        kind = type(expr).__name__
        acc = self._array_access(expr)
        if acc is not None and acc.sym in self._memories:
            if acc.element is None:
                return None  # a row or slice of a memory: not modelled
            if not acc.packed:
                return self._emit_memory_read(acc, expr)
        else:
            if kind == "RangeSelectExpression":
                bits = self._range_select_bits(expr)
            else:
                bits = self._element_select_bits(expr)
            if bits is not None:
                return bits
        if kind == "ElementSelectExpression":
            if self._const_int(getattr(expr, "selector", None)) is None:
                vtype = getattr(expr.value, "type", None)
                vtype = getattr(vtype, "canonicalType", None) or vtype
                if getattr(vtype, "isUnpackedArray", False):
                    return self._array_mux(expr)
                return self._lower_dynamic_select(expr)
        elif kind == "RangeSelectExpression" and self._range_offsets(expr) is None:
            return self._lower_dynamic_select(expr)
        return self._slice_select(expr)

    def _slice_select(self, expr: Any) -> tuple[Bit, ...] | None:
        """A constant select of a computed value: its bits, sliced."""
        value = getattr(expr, "value", None)
        if value is None:
            return None
        vtype = getattr(value, "type", None)
        kind = type(expr).__name__
        if kind == "MemberAccessExpression" and not getattr(vtype, "isIntegral", False):
            return None
        base = self._bits_of_expression(value)
        if base is None:
            return None
        sub = self._packed_subrange([expr], len(base))
        if sub is None:
            return None
        start, size = sub
        return tuple(base[start : start + size])

    def _lower_dynamic_select(self, expr: Any) -> tuple[Bit, ...] | None:
        """A read of a packed vector at a run-time position — ``a[i]``,
        ``a[i +: W]``, ``a[i -: W]`` (``a`` a packed vector or packed
        array, or any packed value such as a memory word) — as the
        ``$shiftx`` Yosys emits: ``A`` the whole vector, ``B`` the LSB-first
        bit position of the selection's lowest bit, ``Y`` the selection
        (rtl-buddy-cdc#384: it was an opaque driver, so the vector and the
        index were invisible to the rules). The position is the index
        translated through the declared range (``$sub`` / ``$mul`` cells,
        as Yosys builds them). ``None`` for anything else (an unpacked
        aggregate that is not a memory, a run-time width)."""
        value = getattr(expr, "value", None)
        vtype = getattr(value, "type", None)
        vtype = getattr(vtype, "canonicalType", None) or vtype
        if (
            value is None
            or vtype is None
            or getattr(vtype, "isUnpackedArray", False)
            or not getattr(vtype, "isIntegral", False)
        ):
            return None
        width = self._expr_width(expr)
        if width < 1:
            return None
        # A constant index that did not resolve is out of range: not a
        # run-time select (it stays an opaque, reported read).
        if type(expr).__name__ == "ElementSelectExpression":
            selector = getattr(expr, "selector", None)
            if self._const_int(selector) is not None:
                return None
            idx = self._bits_of_expression(selector)
            if not idx:
                return None
            base = self._bits_of_expression(value)
            if base is None:
                return None
            shift, signed = self._position_bits(vtype, idx, expr)
        else:
            sel_kind = str(getattr(expr, "selectionKind", "")).rsplit(".", 1)[-1]
            count = self._const_int(getattr(expr, "right", None))
            start = getattr(expr, "left", None)
            if (
                sel_kind not in {"IndexedUp", "IndexedDown"}
                or not count
                or self._const_int(start) is not None
            ):
                return None
            idx = self._bits_of_expression(start)
            if not idx:
                return None
            base = self._bits_of_expression(value)
            if base is None:
                return None
            shift, signed = self._position_bits(
                vtype, idx, expr, count=count, down=sel_kind == "IndexedDown"
            )
        # With the width / signedness parameters Yosys gives it: a computed
        # position can be negative (a select reaching below the vector's
        # LSB), which reads ``x``, not a far-away bit.
        return self._emit_comb_cell(
            "$shiftx",
            {"A": base, "B": shift},
            output_width=width,
            src_node=expr,
            parameters={
                "A_SIGNED": _param_int32(0),
                "A_WIDTH": _param_int32(len(base)),
                "B_SIGNED": _param_int32(1 if signed else 0),
                "B_WIDTH": _param_int32(len(shift)),
                "Y_WIDTH": _param_int32(width),
            },
        )

    # Expressions that name a symbol: a plain name, a dotted one.
    _NAME_KINDS = frozenset({"NamedValueExpression", "HierarchicalValueExpression"})

    # Symbols a NamedValueExpression reads as a constant.
    _CONSTANT_SYMBOLS = frozenset(
        {"ParameterSymbol", "EnumValueSymbol", "SpecparamSymbol"}
    )

    def _value_bits(self, value: Any, width: int) -> tuple[Bit, ...] | None:
        """LSB-first bits of an elaboration-time value (``SVInt`` or
        ``ConstantValue``) with ``x`` / ``z`` digits kept, sized to
        ``width`` (its own width when ``width`` is 0): zero-extended, or
        sign-extended when the value is signed."""
        digits = _encode_param(value) if value is not None else None
        if digits is None:
            return None
        bits: tuple[Bit, ...] = tuple(digits[::-1])
        if width < 1:
            return bits
        inner = getattr(value, "value", value)
        signed = bool(getattr(inner, "isSigned", False))
        return self._resize_bits(bits, width, signed)

    # -- function calls (rtl-buddy-cdc#377) -------------------------------

    _INLINE_DEPTH_CAP = 16

    def _unpacked_value_bits(self, value: Any, vtype: Any) -> tuple[Bit, ...] | None:
        """The bits of an unpacked-array parameter's value (``P[0]`` lowest,
        this frontend's layout of an unpacked array), so a select of it
        reads its element (rtl-buddy-cdc#402); ``None`` for anything
        else."""
        t = getattr(vtype, "canonicalType", None) or vtype
        if not getattr(t, "isUnpackedArray", False):
            return self._value_bits(value, int(getattr(t, "bitWidth", 0) or 0))
        items = getattr(value, "value", value)
        rng = getattr(t, "range", None)
        if not isinstance(items, list) or rng is None:
            return None
        left, right = int(rng.left), int(rng.right)
        if len(items) != abs(left - right) + 1:
            return None
        out: list[Bit] = []
        for item in reversed(items) if left > right else items:
            bits = self._unpacked_value_bits(item, getattr(t, "elementType", None))
            if bits is None:
                return None
            out.extend(bits)
        return tuple(out)

    def _lower_call(self, expr: Any) -> tuple[Bit, ...] | None:
        """A function call on the right-hand side: its constant value when
        slang folds it (``$clog2(N)``, a constant function of parameters),
        the operand of a ``$signed`` / ``$unsigned`` cast (no logic of its
        own; the width conversion around it does any extension), else the
        body of a user-defined function inlined (:meth:`_inline_function`).
        ``None`` for anything else (another system function)."""
        constant = self._constant_bits(expr)
        if constant is not None:
            return constant
        if getattr(expr, "isSystemCall", False):
            args = list(getattr(expr, "arguments", None) or [])
            name = getattr(expr, "subroutineName", "")
            if name in {"$signed", "$unsigned"} and len(args) == 1:
                return self._bits_of_expression(args[0])
            return None
        return self._inline_function(expr)

    def _constant_bits(self, expr: Any) -> tuple[Bit, ...] | None:
        """LSB-first bits of ``expr``'s elaboration-time value, sized to its
        type, with ``x`` / ``z`` digits kept; ``None`` when slang did not
        fold it."""
        value = getattr(expr, "constant", None)
        inner = getattr(value, "value", value)
        width = self._expr_width(expr)
        if inner is None or width < 1 or getattr(inner, "bitWidth", None) is None:
            return None
        return self._value_bits(inner, width)

    def _inline_function(self, expr: Any) -> tuple[Bit, ...] | None:
        """Inline a call to a combinational user-defined function, as
        Yosys' frontend does: bind each formal argument to its actual
        argument's bits, walk the body as a combinational block, and read
        the result off the function's return variable.

        Locals (formals, the return variable, declared variables) start
        as ``x``, are visible only during the walk, and are removed
        afterwards, so two calls never share a value. ``None`` (the
        caller then treats the call as unlowered) for a task or void
        function, an ``output`` / ``inout`` / ``ref`` argument, a ``return``
        anywhere but at the end of the body, a write to a variable outside
        the function, a statement or target the walker cannot lower,
        recursion deeper than :data:`_INLINE_DEPTH_CAP`.
        """
        sub = getattr(expr, "subroutine", None)
        if sub is None or self._inline_depth >= self._INLINE_DEPTH_CAP:
            return None
        if str(getattr(sub, "subroutineKind", "")).rsplit(".", 1)[-1] != "Function":
            return None
        ret = getattr(sub, "returnValVar", None)
        locals_: list[Any] = []
        for member in sub:
            if self._kind_name(member) not in {
                "VariableSymbol",
                "FormalArgumentSymbol",
            }:
                continue
            locals_.append(member)
            if (
                ret is None
                and self._kind_name(member) == "VariableSymbol"
                and member.name == sub.name
            ):
                ret = member
        formals = list(getattr(sub, "arguments", None) or [])
        actuals = list(getattr(expr, "arguments", None) or [])
        if ret is None or len(formals) != len(actuals):
            return None
        if any(
            str(getattr(f, "direction", "")).rsplit(".", 1)[-1] != "In" for f in formals
        ):
            return None
        body = getattr(sub, "body", None)
        if not self._returns_only_at_end(body, top=True):
            return None
        arg_bits: list[tuple[Bit, ...]] = []
        for formal, actual in zip(formals, actuals):
            bits = self._sized_rhs_bits(actual, self._width_of(formal))
            if bits is None:
                return None
            arg_bits.append(bits)
        saved = dict(self._var_bits)
        failures = self._lowering_failures
        self._inline_depth += 1
        try:
            for local in locals_:
                self._var_bits[local] = ("x",) * self._width_of(local)
            for formal, bits in zip(formals, arg_bits):
                self._var_bits[formal] = bits
            self._inline_returns.append(ret)
            try:
                self._walk_comb_statement(body)
            finally:
                self._inline_returns.pop()
            result = self._var_bits.get(ret)
            local_set = set(locals_)
            clean = self._lowering_failures == failures and all(
                self._var_bits.get(k) == v
                for k, v in saved.items()
                if k not in local_set
            )
        finally:
            self._inline_depth -= 1
            self._var_bits = saved
        return result if clean else None

    def _returns_only_at_end(self, stmt: Any, *, top: bool) -> bool:
        """``return`` appears in ``stmt`` only as the last statement of the
        function body's top-level list (the shape inlining supports)."""
        if stmt is None:
            return True
        kind = self._kind_name(stmt)
        if kind == "ReturnStatement":
            return top
        if kind == "BlockStatement":
            return self._returns_only_at_end(stmt.body, top=top)
        if kind in {"StatementList", "ListStatement"}:
            items = list(getattr(stmt, "list", []) or [])
            return all(
                self._returns_only_at_end(s, top=top and i == len(items) - 1)
                for i, s in enumerate(items)
            )
        found = False

        def visit(node: Any) -> bool:
            nonlocal found
            if type(node).__name__ == "ReturnStatement":
                found = True
            return not found

        stmt.visit(visit)
        return not found

    def _lower_concatenation(self, expr: Any) -> tuple[Bit, ...] | None:
        """``{a, b, c}`` — return a single LSB-first bit tuple.

        SV concatenation lists operands MSB-first (``{a, b}`` has ``a``
        in the upper bits and ``b`` in the lower bits). Yosys stores
        bit tuples LSB-first, so we **reverse the operand order** and
        flatten — the last operand's LSB-first bits land at the start
        of the result tuple.

        Returns ``None`` if any operand can't be lowered, so the
        caller can fall back to the ``$_UNKNOWN_`` placeholder
        instead of emitting partial garbage.
        """
        operand_bits: list[tuple[Bit, ...]] = []
        for op in expr.operands:
            bits = self._bits_of_expression(op)
            if bits is None:
                return None
            operand_bits.append(bits)
        result: list[Bit] = []
        for bits in reversed(operand_bits):
            result.extend(bits)
        return tuple(result)

    def _lower_replication(self, expr: Any) -> tuple[Bit, ...] | None:
        """``{N{x}}`` — repeat the inner pattern ``N`` times.

        pyslang wraps the replicated expression in a
        :class:`ConcatenationExpression` accessible via ``.concat``,
        so ``{N{a}}`` and ``{N{a, b}}`` go through the same path.
        ``N`` (the ``.count`` attribute) must be a compile-time
        constant; dynamic replication counts return ``None``.
        """
        count = self._const_int(getattr(expr, "count", None))
        if count is None or count < 0:
            return None
        pattern = self._bits_of_expression(expr.concat)
        if pattern is None:
            return None
        return tuple(pattern) * count

    # --- expression lowering --------------------------------------------

    # pyslang BinaryOperator → Yosys cell type. Where Yosys distinguishes
    # bitwise (``$and``) from reduction-style logical (``$logic_and``),
    # we follow the same split. Comparisons and arithmetic round-trip to
    # the same Yosys op names the post-``proc`` netlist would carry.
    _BINOP_CELL: dict[str, str] = {
        "BinaryAnd": "$and",
        "BinaryOr": "$or",
        "BinaryXor": "$xor",
        "BinaryXnor": "$xnor",
        "LogicalAnd": "$logic_and",
        "LogicalOr": "$logic_or",
        "Equality": "$eq",
        "Inequality": "$ne",
        "CaseEquality": "$eqx",
        "CaseInequality": "$nex",
        "LessThan": "$lt",
        "LessThanEqual": "$le",
        "GreaterThan": "$gt",
        "GreaterThanEqual": "$ge",
        "Add": "$add",
        "Subtract": "$sub",
        "Multiply": "$mul",
        "Divide": "$div",
        "Mod": "$mod",
        "LogicalShiftLeft": "$shl",
        "LogicalShiftRight": "$shr",
        "ArithmeticShiftLeft": "$sshl",
        "ArithmeticShiftRight": "$sshr",
        "Power": "$pow",
    }

    _UNOP_CELL: dict[str, str] = {
        "BitwiseNot": "$not",
        "LogicalNot": "$logic_not",
        "Minus": "$neg",
        # Reduction operators — these collapse a multi-bit operand to a
        # single bit. ``Plus`` is identity and skipped at the call site.
        "BitwiseAnd": "$reduce_and",
        "BitwiseOr": "$reduce_or",
        "BitwiseXor": "$reduce_xor",
        # ``~&`` / ``~|`` are the reduction then a ``$logic_not``, as Yosys
        # lowers them (rtl-buddy-cdc#405: the inversion was dropped, so
        # ``~&a`` computed ``&a``); ``~^`` is ``$reduce_xnor``.
        "BitwiseNand": "$reduce_and",
        "BitwiseNor": "$reduce_or",
        "BitwiseXnor": "$reduce_xnor",
    }

    # Reductions whose result is inverted by a following ``$logic_not``.
    _INVERTED_REDUCTIONS = frozenset({"BitwiseNand", "BitwiseNor"})

    def _lower_binary(self, expr: Any) -> tuple[Bit, ...] | None:
        op_name = str(expr.op).rsplit(".", 1)[-1]
        if op_name in ("WildcardEquality", "WildcardInequality"):
            a_bits = self._bits_of_expression(expr.left)
            b_bits = self._bits_of_expression(expr.right)
            if a_bits is None or b_bits is None:
                return None
            hit = self._wildcard_match(a_bits, b_bits, expr)
            if op_name == "WildcardInequality":
                hit = self._lower_not(hit)
            return (hit,)
        cell_type = self._BINOP_CELL.get(op_name)
        if cell_type is None:
            return None  # unmodelled op → unknown driver
        a_bits = self._bits_of_expression(expr.left)
        b_bits = self._bits_of_expression(expr.right)
        if a_bits is None or b_bits is None:
            return None
        # Constant-shift fold: when ``x << N`` / ``x >> N`` has a
        # compile-time-constant shift amount, emit the result as a
        # wire-rerouting of ``x``'s bits rather than a ``$shl`` / ``$shr``
        # cell. Matches Yosys-flatten's structural output and is what
        # the gray-code detector (``_is_gray_encoded_source``) keys on:
        # ``b ^ (b >> 1)`` only matches the gray-pattern signature when
        # the shifted operand shares bit IDs with the unshifted one.
        # Runtime shift amounts can't be folded — fall through to the
        # cell-emit path.
        if cell_type in ("$shl", "$shr"):
            shift = self._const_int(expr.right)
            if shift is not None and shift >= 0:
                width = self._expr_width(expr) or len(a_bits)
                return self._wire_route_shift(
                    a_bits, shift, width, left=(cell_type == "$shl")
                )
        width = self._expr_width(expr)
        if cell_type == "$pow":
            # ``2**N`` of constants is a constant (Yosys folds it).
            value = self._const_int(expr)
            if value is not None and width:
                return tuple("1" if (value >> i) & 1 else "0" for i in range(width))
        return self._emit_comb_cell(
            cell_type,
            {"A": a_bits, "B": b_bits},
            output_width=width,
            src_node=expr,
            parameters=self._signedness(cell_type, expr.left, expr.right),
        )

    # Cells whose ``B`` (a shift amount) is unsigned whatever its type.
    _UNSIGNED_B_CELLS = frozenset({"$shl", "$shr", "$sshl", "$sshr"})

    def _signedness(self, cell_type: str, left: Any, right: Any) -> dict[str, str]:
        """``A_SIGNED`` / ``B_SIGNED`` of a binary cell, as Yosys sets them:
        an operand of a signed type (slang has already converted both
        operands of a comparison or an arithmetic operator to the common
        type, so they agree), a shift amount never (rtl-buddy-cdc#405:
        every cell was unsigned, so ``a < b``, ``a >>> s``, ``a / b`` of
        signed operands computed the unsigned result)."""

        def signed(e: Any) -> bool:
            return bool(getattr(getattr(e, "type", None), "isSigned", False))

        b_signed = signed(right) and cell_type not in self._UNSIGNED_B_CELLS
        return {
            "A_SIGNED": _param_int32(1 if signed(left) else 0),
            "B_SIGNED": _param_int32(1 if b_signed else 0),
        }

    @staticmethod
    def _wire_route_shift(
        a_bits: tuple[Bit, ...], shift: int, width: int, left: bool
    ) -> tuple[Bit, ...]:
        """Constant-shift fold result as a bit tuple.

        Bits are stored LSB-first throughout the frontend. For a right
        shift by ``N``, output bit ``i`` is input bit ``i + N`` for
        ``i < len(a)-N`` and a constant ``'0'`` for the high pad. Left
        shift mirrors: output bit ``i`` is ``'0'`` for ``i < N`` and
        input bit ``i - N`` afterwards.

        ``width`` is the result type's bit width (usually equals
        ``len(a_bits)``). When wider, the extra high bits pad with
        ``'0'``; when narrower (truncated shift result), drop from
        the top. SystemVerilog sizes shifts to the wider of the
        operands, so most call sites pass ``width == len(a_bits)``.
        """
        n = len(a_bits)
        out: list[Bit] = []
        for i in range(width):
            if left:
                src_idx = i - shift
            else:
                src_idx = i + shift
            if 0 <= src_idx < n:
                out.append(a_bits[src_idx])
            else:
                out.append("0")
        return tuple(out)

    def _wildcard_match(
        self, a_bits: tuple[Bit, ...], b_bits: tuple[Bit, ...], node: Any
    ) -> Bit:
        """``a ==? b``: equal on every bit where ``b`` is not ``x`` / ``z``
        (IEEE 1800-2017 11.4.6), the ``$eq`` of those lanes; ``"1"`` when
        every bit of ``b`` is a wildcard."""
        width = max(len(a_bits), len(b_bits))
        a = self._resize_bits(a_bits, width)
        b = self._resize_bits(b_bits, width)
        lanes = [i for i in range(width) if b[i] not in ("x", "z")]
        if not lanes:
            return "1"
        return self._emit_comb_cell(
            "$eq",
            {"A": tuple(a[i] for i in lanes), "B": tuple(b[i] for i in lanes)},
            output_width=1,
            src_node=node,
        )[0]

    def _range_match(self, value_bits: tuple[Bit, ...], rng: Any) -> Bit | None:
        """``value`` lies in the ``[lo:hi]`` of an ``inside`` list or a
        ``case … inside`` label: ``$ge`` and ``$le`` (``$`` leaves a side
        open). ``None`` when a bound does not lower."""
        hit: Bit = "1"
        for bound, cell in ((rng.left, "$ge"), (rng.right, "$le")):
            inner = bound
            while self._kind_name(inner) == "ConversionExpression":
                inner = getattr(inner, "operand", None)
            if self._kind_name(inner) == "UnboundedLiteral":
                continue
            bits = self._bits_of_expression(bound)
            if not bits:
                return None
            width = max(len(bits), len(value_bits))
            # slang converts the value and the bounds to their common
            # type, so a signed bound means a signed comparison.
            signed = bool(getattr(getattr(bound, "type", None), "isSigned", False))
            hit = self._and_bit(
                hit,
                self._emit_comb_cell(
                    cell,
                    {
                        "A": self._resize_bits(value_bits, width),
                        "B": self._resize_bits(bits, width),
                    },
                    output_width=1,
                    src_node=bound,
                    parameters={
                        "A_SIGNED": _param_int32(int(signed)),
                        "B_SIGNED": _param_int32(int(signed)),
                    },
                )[0],
            )
        return hit

    def _lower_inside(self, expr: Any) -> tuple[Bit, ...] | None:
        """``a inside {v, [lo:hi], …}``: the OR of ``a ==? v`` and the
        range checks (rtl-buddy-cdc#405: an opaque driver). ``None`` for an
        item that does not lower (an unpacked array to search, …)."""
        left = self._bits_of_expression(expr.left)
        if left is None:
            return None
        hits: list[Bit] = []
        for item in getattr(expr, "rangeList", None) or []:
            if self._kind_name(item) == "ValueRangeExpression":
                hit = self._range_match(left, item)
            else:
                itype = getattr(item, "type", None)
                if not getattr(itype, "isIntegral", False):
                    return None
                bits = self._bits_of_expression(item)
                hit = None if bits is None else self._wildcard_match(left, bits, item)
            if hit is None:
                return None
            hits.append(hit)
        result = self._or_bits(hits)
        return (result if result is not None else "0",)

    def _lower_streaming(self, expr: Any) -> tuple[Bit, ...] | None:
        """A streaming concatenation read (``{<<{a}}``, ``{<<4{a, b}}``,
        ``{>>{a, b}}``): the operands' bits packed MSB first and, for
        ``<<``, the slices of ``sliceSize`` bits (1 by default) taken in
        reverse order (IEEE 1800-2017 11.4.14), left-aligned in the
        target as slang sizes it. Pure rewiring (rtl-buddy-cdc#405: an
        opaque driver). ``None`` for a ``with`` range or a dynamic size."""
        stream: list[Bit] = []  # MSB first
        for st in getattr(expr, "streams", None) or []:
            if getattr(st, "withExpr", None) is not None:
                return None
            bits = self._bits_of_expression(st.operand)
            if bits is None:
                return None
            stream.extend(reversed(bits))
        if not getattr(expr, "isFixedSize", False):
            return None
        if self._streaming_op(expr) == "<<":
            size = int(getattr(expr, "sliceSize", 0) or 0) or 1
            chunks = [stream[i : i + size] for i in range(0, len(stream), size)]
            stream = [b for chunk in reversed(chunks) for b in chunk]
        width = self._expr_width(expr) or len(stream)
        # Left-aligned: the stream's MSB is the target's MSB, zero-filled
        # below (a wider target) or its trailing bits dropped (narrower).
        if width >= len(stream):
            msb_first = stream + ["0"] * (width - len(stream))
        else:
            msb_first = stream[:width]
        return tuple(reversed(msb_first))

    def _lower_unary(self, expr: Any) -> tuple[Bit, ...] | None:
        op_name = str(expr.op).rsplit(".", 1)[-1]
        if op_name == "Plus":
            # Unary plus is identity in SV — peek through.
            return self._bits_of_expression(expr.operand)
        cell_type = self._UNOP_CELL.get(op_name)
        if cell_type is None:
            return None
        a_bits = self._bits_of_expression(expr.operand)
        if a_bits is None:
            return None
        # Reduction operators produce a single bit regardless of input
        # width; everything else preserves the operand width.
        if cell_type.startswith("$reduce_") or cell_type == "$logic_not":
            output_width = 1
        else:
            output_width = self._expr_width(expr) or len(a_bits)
        y = self._emit_comb_cell(
            cell_type, {"A": a_bits}, output_width=output_width, src_node=expr
        )
        if op_name in self._INVERTED_REDUCTIONS:
            y = self._emit_comb_cell("$logic_not", {"A": y}, 1, src_node=expr)
        return y

    def _lower_conditional(self, expr: Any) -> tuple[Bit, ...] | None:
        # pyslang ConditionalExpression has ``conditions`` (a list) and
        # ``left``/``right`` — the conditional-pattern grammar is more
        # general than ``cond ? a : b``, but for our purposes we only
        # need the single-condition shape.
        conds = getattr(expr, "conditions", None)
        if not conds:
            return None
        # A condition known at elaboration time picks its arm, as Yosys
        # folds it: the dead arm may not even be a valid select (``EN ?
        # bus[OFF +: W] : '0`` with the field disabled).
        const = self._const_int(conds[0].expr) if len(conds) == 1 else None
        if const is not None:
            return self._bits_of_expression(expr.left if const else expr.right)
        # A multi-bit condition is true when non-zero: the ``$reduce_bool``
        # Yosys emits (rtl-buddy-cdc#405: ``S`` took bit 0 alone, so the
        # other bits' fan-in was lost).
        sel = self._lower_condition_to_bit(conds) if len(conds) == 1 else None
        sel_bits = None if sel is None else (sel,)
        # pyslang names the branches from source order: ``left`` is the
        # TRUE branch of ``s ? left : right``, ``right`` the FALSE one.
        true_bits = self._bits_of_expression(expr.left)
        false_bits = self._bits_of_expression(expr.right)
        if sel_bits is None or true_bits is None or false_bits is None:
            return None
        width = self._expr_width(expr) or max(len(true_bits), len(false_bits))
        # Yosys ``$mux`` is ``Y = S ? B : A`` — ``A`` is the S=0 leg and
        # ``B`` the S=1 leg. Source order is therefore NOT the pin order:
        # the ternary's FALSE branch goes on ``A`` and its TRUE branch on
        # ``B``. Emitting them the other way round leaves the legs
        # swapped relative to the Yosys frontend, and every positional
        # consumer (``trace_clock_root``'s clock-mux clause, CDC-012's
        # gating detection, ``_is_gated_bus_crossing``) then reads the
        # wrong leg — see issue #289. The S pin takes the (single-bit)
        # selector.
        return self._emit_comb_cell(
            cell_type="$mux",
            inputs={"A": false_bits, "B": true_bits, "S": sel_bits},
            output_width=width,
            src_node=expr,
        )

    def _expr_width(self, expr: Any) -> int:
        t = getattr(expr, "type", None)
        if t is None:
            return 0
        return int(getattr(t, "bitWidth", 0) or 0)

    def _emit_comb_cell(
        self,
        cell_type: str,
        inputs: dict[str, tuple[Bit, ...]],
        output_width: int,
        src_node: Any = None,
        parameters: dict[str, str] | None = None,
    ) -> tuple[Bit, ...]:
        """Allocate a fresh Y net and emit a ``cell_type`` cell with the
        given inputs (and ``parameters``, none by default). Returns the Y
        bits so callers can wire them up.

        Width defensively clamped to at least 1 so we never emit a
        zero-width cell (which would confuse the rule pack's bit walks).
        Optionally attaches a ``src`` attribute formatted as Yosys'
        ``file:line.col-line.col`` convention so the reporter can
        surface a clickable source location.
        """
        width = max(int(output_width or 1), 1)
        y_bits: tuple[Bit, ...] = tuple(
            range(self._next_bit_id, self._next_bit_id + width)
        )
        self._next_bit_id += width
        name = self._fresh_cell_name(cell_type)
        attrs: dict[str, str] = {}
        src = self._src_attr(src_node) if src_node is not None else None
        if src is not None:
            attrs["src"] = src
        self._cells[name] = Cell(
            name=name,
            type=cell_type,
            connections={**inputs, "Y": y_bits},
            parameters=dict(parameters or {}),
            attributes=attrs,
        )
        return y_bits

    # -- inferred memories (#326) and register arrays (#398) --------------

    # Expressions that select part of a value.
    _SELECT_KINDS = frozenset(
        {"ElementSelectExpression", "RangeSelectExpression", "MemberAccessExpression"}
    )

    @staticmethod
    def _unpacked_ranges(sym: Any) -> list[tuple[int, int]] | None:
        """``(left, right)`` of each unpacked dimension of ``sym``'s type,
        outermost first, when its element is an integral (packed) type;
        ``None`` for anything else."""
        t = getattr(sym, "type", None)
        t = getattr(t, "canonicalType", None) or t
        dims: list[tuple[int, int]] = []
        while getattr(t, "isUnpackedArray", False):
            rng = getattr(t, "range", None)
            left = getattr(rng, "left", None)
            right = getattr(rng, "right", None)
            if left is None or right is None:
                return None
            dims.append((int(left), int(right)))
            t = getattr(t, "elementType", None)
            t = getattr(t, "canonicalType", None) or t
        if not dims or not getattr(t, "isIntegral", False):
            return None
        return dims

    def _array_access(self, expr: Any) -> _ArrayAccess | None:
        """Split a select chain over an unpacked-array variable into the
        array, the selectors of its unpacked dimensions and the selects
        applied to the element (:class:`_ArrayAccess`); ``None`` when
        ``expr`` is no such chain."""
        nodes: list[Any] = []
        e = expr
        while type(e).__name__ in self._SELECT_KINDS:
            nodes.append(e)
            e = getattr(e, "value", None)
        if not self._is_named(e) or not nodes:
            return None
        sym = self._canonical_var(e.symbol)
        dims = self._unpacked_ranges(sym)
        if dims is None:
            return None
        selectors: list[Any] = []
        element = None
        packed: list[Any] = []
        for node in reversed(nodes):
            if element is not None:
                packed.append(node)
            elif type(node).__name__ != "ElementSelectExpression":
                break  # a slice of the array (or of a row): no element
            else:
                selectors.append(node.selector)
                if len(selectors) == len(dims):
                    element = node
        return _ArrayAccess(sym, dims, selectors, element, packed)

    def _design_instances(
        self, inst: Any, prefix: str, out: list[tuple[Any, str]]
    ) -> None:
        """Append ``(instance, hierarchy prefix)`` for ``inst`` and every
        instance below it that this builder inlines (boundary instances
        stay cells), named as :meth:`_emit_instance` /
        :meth:`_emit_instance_array` name them."""
        out.append((inst, prefix))
        self._inlined_bodies.add(inst.body)

        def child(member: Any, scope_prefix: str, name: str) -> None:
            kind = self._kind_name(member)
            if kind == "InstanceSymbol":
                if not self._is_boundary_instance(member):
                    self._design_instances(member, f"{scope_prefix}{name}.", out)
            elif kind == "InstanceArraySymbol":
                if getattr(member, "isUninstantiated", False):
                    return
                lower = getattr(getattr(member, "range", None), "lower", None)
                if lower is None:
                    return
                for offset, element in enumerate(
                    getattr(member, "elements", None) or []
                ):
                    child(element, scope_prefix, f"{name}[{int(lower) + offset}]")

        for member, scope_prefix in self._iter_scope(inst.body, prefix):
            child(member, scope_prefix, getattr(member, "name", "") or "")

    def _classify_design_arrays(self) -> None:
        """Decide which unpacked arrays of the design are memories, as Yosys'
        Verilog frontend does before ``proc`` (``mem2reg``), checked
        against Yosys 0.64 for each case (rtl-buddy-cdc#398). An array is
        a list of registers, named per element, when

        - it is a net (``wire``), not a variable;
        - any write to it is in a process that is not clocked by exactly
          one edge (``always_comb``, ``always_latch``, ``always @*``, an
          ``always_ff`` with an asynchronous reset) or in a continuous
          assignment;
        - a process reads it after writing it with a blocking assignment;
        - a write targets the whole array or a slice of it; or
        - every write (``initial`` blocks aside) selects its element with
          constant indices, a ``for`` loop's variable counting as
          constant (Yosys unrolls the loop first).

        Anything else is a memory, also one nothing writes: every read of
        it is a ``$memrd_v2``, every write a ``$memwr_v2``, at a constant
        address too, and it has no netname. Arrays bound to ports stay
        registers.

        The whole inlined design is scanned before anything is lowered
        (rtl-buddy-cdc#405): a hierarchical write (``u_child.a[i] <= d``)
        counts against the array it names, and a read of a memory through
        a hierarchical name (``gr.lm[ra]``, ``u_child.mem[ra]``) is a read
        port wherever it sits in source order."""
        instances: list[tuple[Any, str]] = []
        self._design_instances(self.top, "", instances)
        flags: dict[Any, set[str]] = {}
        arrays: list[tuple[Any, str]] = []
        ports: set[Any] = set()
        for inst, hier_prefix in instances:
            ports.update(
                self._canonical_var(m)
                for m in inst.body
                if self._kind_name(m) == "PortSymbol"
                and getattr(m, "internalSymbol", None) is not None
            )
            for member, prefix in self._iter_scope(inst.body, hier_prefix):
                kind = self._kind_name(member)
                if kind == "VariableSymbol":
                    arrays.append((member, prefix))
                elif kind == "ProceduralBlockSymbol":
                    proc = str(member.procedureKind).rsplit(".", 1)[-1]
                    if proc not in {"Initial", "Final"}:
                        sync = self._edge_event_count(member.body) == 1
                        self._scan_array_process(member.body, sync, flags)
                elif kind == "ContinuousAssignSymbol":
                    assign = getattr(member, "assignment", None)
                    if assign is not None:
                        self._scan_array_process(assign, False, flags)
        for member, prefix in arrays:
            sym = self._canonical_var(member)
            if sym in ports or self._unpacked_ranges(sym) is None:
                continue
            seen = flags.get(sym, set())
            if seen & {"async", "reread", "whole"}:
                continue
            if "const" in seen and "var" not in seen:
                continue
            self._memories.add(sym)
            memid = f"\\{prefix}{member.name}"
            self._memory_ids.add(memid)
            # Named after the declaration's scope, not the scope of the
            # process using it (rtl-buddy-cdc#402): ports of one memory in
            # different generate blocks share one ``MEMID``, as in Yosys.
            self._memory_id_of[sym] = memid

    def _edge_event_count(self, body: Any) -> int:
        """Number of edge events in a procedural block's leading event
        control (``@(posedge clk or negedge rst_n)`` is 2)."""
        if self._kind_name(body) != "TimedStatement":
            return 0
        timing = body.timing
        events = getattr(timing, "events", None)
        if events is None:
            events = [timing]
        return sum(
            str(getattr(ev, "edge", "")).rsplit(".", 1)[-1] in {"PosEdge", "NegEdge"}
            for ev in events
        )

    def _scan_array_process(
        self, node: Any, sync: bool, flags: dict[Any, set[str]]
    ) -> None:
        """Record in ``flags`` how one process (or continuous assignment)
        writes and reads unpacked arrays, for :meth:`_classify_design_arrays`:
        ``async`` (written outside a single-edge process), ``reread``
        (read after a blocking write in the same process), ``whole`` (the
        whole array or a slice written), ``const`` / ``var`` (an element
        written at a constant / run-time index)."""
        pyslang = self.pyslang
        loop_vars: set[Any] = set()
        blocking: set[Any] = set()

        def static(sel: Any) -> bool:
            if self._const_int(sel) is not None:
                return True
            names: list[Any] = []

            def name(n: Any) -> None:
                if type(n).__name__ in self._NAME_KINDS:
                    names.append(n.symbol)

            sel.visit(name)
            return bool(names) and all(
                s in loop_vars or self._kind_name(s) in self._CONSTANT_SYMBOLS
                for s in names
            )

        # pyslang 11 moved VisitAction into ``pyslang.ast``.
        visit_action = _pyslang_namespaces(pyslang)[0].VisitAction

        def visit(n: Any) -> Any:
            kind = type(n).__name__
            if kind == "ForLoopStatement":
                loop_vars.update(getattr(n, "loopVars", None) or [])
                for init in getattr(n, "initializers", None) or []:
                    target = getattr(init, "left", None)
                    if type(target).__name__ == "NamedValueExpression":
                        loop_vars.add(getattr(target, "symbol", None))
            elif kind in self._NAME_KINDS:
                sym = self._canonical_var(n.symbol)
                if sym in blocking:
                    flags.setdefault(sym, set()).add("reread")
            elif kind == "AssignmentExpression":
                written = self._scan_array_writes(n.left, sync, static, flags, visit)
                n.right.visit(visit)
                if not getattr(n, "isNonBlocking", False):
                    blocking.update(written)
                return visit_action.Skip
            return visit_action.Advance

        node.visit(visit)

    def _scan_array_writes(
        self,
        lhs: Any,
        sync: bool,
        static: Callable[[Any], bool],
        flags: dict[Any, set[str]],
        visit: Callable[[Any], Any],
    ) -> list[Any]:
        """:meth:`_scan_array_process` for one assignment target: flag the
        arrays it writes (returned) and scan what it reads (indices)."""
        if type(lhs).__name__ == "ConcatenationExpression":
            out: list[Any] = []
            for op in getattr(lhs, "operands", None) or []:
                out += self._scan_array_writes(op, sync, static, flags, visit)
            return out
        acc = self._array_access(lhs)
        if acc is None:
            if self._is_named(lhs):
                sym = self._canonical_var(lhs.symbol)
                if self._unpacked_ranges(sym) is not None:
                    flags.setdefault(sym, set()).add("whole")
                    return [sym]
                return []
            # Not an array element: what its selects read still counts.
            for node in self._select_nodes(lhs):
                self._visit_select_operands(node, visit)
            return []
        seen = flags.setdefault(acc.sym, set())
        if not sync:
            seen.add("async")
        if acc.element is None:
            seen.add("whole")
        else:
            seen.add("const" if all(static(s) for s in acc.selectors) else "var")
        for node in self._select_nodes(lhs):
            self._visit_select_operands(node, visit)
        return [acc.sym]

    def _select_nodes(self, expr: Any) -> list[Any]:
        """The select expressions of a chain, outermost first."""
        nodes: list[Any] = []
        while type(expr).__name__ in self._SELECT_KINDS:
            nodes.append(expr)
            expr = getattr(expr, "value", None)
        return nodes

    @staticmethod
    def _visit_select_operands(node: Any, visit: Callable[[Any], Any]) -> None:
        """Visit the index / bound expressions of one select."""
        for attr in ("selector", "left", "right"):
            sub = getattr(node, attr, None)
            if sub is not None:
                sub.visit(visit)

    def _memory_address(self, acc: _ArrayAccess, node: Any) -> tuple[Bit, ...]:
        """The ``ADDR`` of a memory port for ``acc``, as Yosys builds it: a
        constant index is a 32-bit constant; a run-time one its own bits;
        several dimensions are flattened row-major (``i * N + j``, ``$mul``
        / ``$add``)."""
        consts = [self._const_int(s) for s in acc.selectors]
        if all(c is not None for c in consts):
            flat = 0
            for (left, right), c in zip(acc.dims, consts):
                flat = flat * (abs(left - right) + 1) + int(c or 0)
            return self._int_bits(flat, 32)
        parts: list[tuple[Bit, ...]] = []
        for s, c in zip(acc.selectors, consts):
            if c is not None:
                parts.append(self._int_bits(c, 32))
            else:
                parts.append(
                    self._bits_of_expression(s) or self._opaque(s, 1, "memory address")
                )
        if len(parts) == 1:
            return parts[0]
        addr = parts[0]
        for (left, right), part in zip(acc.dims[1:], parts[1:]):
            size = self._int_bits(abs(left - right) + 1, 32)
            addr = self._emit_comb_cell("$mul", {"A": addr, "B": size}, 32, node)
            addr = self._emit_comb_cell("$add", {"A": addr, "B": part}, 32, node)
        return addr

    def _memory_params(self, acc: _ArrayAccess, abits: int) -> dict[str, str]:
        width = self._unpacked_element_width(acc.sym) or 1
        return {
            "MEMID": self._memory_id_of.get(
                acc.sym, f"\\{self._hier_stack[-1]}{acc.sym.name}"
            ),
            "ABITS": _param_int32(abits),
            "WIDTH": _param_int32(width),
        }

    def _memory_cell(
        self,
        cell_type: str,
        connections: dict[str, tuple[Bit, ...]],
        parameters: dict[str, str],
        src_node: Any,
    ) -> None:
        attrs: dict[str, str] = {}
        src = self._src_attr(src_node) if src_node is not None else None
        if src is not None:
            attrs["src"] = src
        name = self._fresh_cell_name(cell_type)
        self._cells[name] = Cell(
            name=name,
            type=cell_type,
            connections=connections,
            parameters=parameters,
            attributes=attrs,
        )

    def _packed_subrange(self, nodes: list[Any], width: int) -> tuple[int, int] | None:
        """``(start, width)`` of the bits constant selects ``nodes``
        (innermost first) pick out of a ``width``-bit value; ``None`` when
        one is not constant or falls outside it."""
        start, size = 0, width
        for node in nodes:
            kind = type(node).__name__
            vtype = getattr(getattr(node, "value", None), "type", None)
            if kind == "ElementSelectExpression":
                idx = self._const_int(getattr(node, "selector", None))
                if idx is None:
                    return None
                size = self._element_stride(vtype)
                start += self._select_position(vtype, idx) * size
            elif kind == "RangeSelectExpression":
                offsets = self._range_offsets(node)
                if offsets is None:
                    return None
                start += offsets[0]
                size = offsets[1]
            else:
                member = getattr(node, "member", None)
                offset = getattr(member, "bitOffset", None)
                size = int(getattr(getattr(member, "type", None), "bitWidth", 0) or 0)
                if offset is None or size < 1:
                    return None
                start += int(offset)
        if start < 0 or start + size > width:
            return None
        return start, size

    def _emit_memory_write(
        self, acc: _ArrayAccess, rhs: Any, clk_sym: Any, src_node: Any
    ) -> bool:
        """Emit a ``$memwr_v2`` for ``mem[addr] <= data`` (or a constant
        bit / part select of the word) in a clocked block; ``False`` when
        the select of the word is not constant.

        Same shape as Yosys ``proc``: ``CLK`` the block's clock,
        ``CLK_ENABLE`` 1, ``EN`` the enclosing conditions (``_enable_stack``)
        on the written lanes and 0 on the others. Unlowerable address /
        data expressions get an ``$_UNKNOWN_`` driver, as for any other
        unmodelled RHS.
        """
        if acc.element is None:
            return False
        width = self._unpacked_element_width(acc.sym) or 1
        write = self._word_write(acc.packed, width, rhs)
        if write is None:
            return False
        start, size, written, mask = write
        addr = self._memory_address(acc, src_node)
        params = self._memory_params(acc, len(addr))
        enable = self._combine_enable_stack()
        en_bit: Bit = enable if enable is not None else "1"
        lanes: tuple[Bit, ...] = (en_bit,) * size
        if mask is not None:
            # A run-time bit position in the word (``mem[i][j] <= b``,
            # rtl-buddy-cdc#402): the shifted data under a shifted one-hot
            # bit enable, as Yosys ``proc`` builds it.
            lanes = mask()
            if en_bit != "1":
                lanes = self._emit_comb_cell(
                    "$and", {"A": lanes, "B": (en_bit,) * size}, size
                )
        tail = width - start - size
        data = ("x",) * start + written + ("x",) * tail
        en = ("0",) * start + lanes + ("0",) * tail
        portid = self._memory_write_ports.get(params["MEMID"], 0)
        self._memory_write_ports[params["MEMID"]] = portid + 1
        params.update(
            {
                "CLK_ENABLE": "1",
                "CLK_POLARITY": "0" if getattr(self, "_clk_negedge", False) else "1",
                "PORTID": _param_int32(portid),
                "PRIORITY_MASK": "",
            }
        )
        self._memory_cell(
            "$memwr_v2",
            {"CLK": self._alloc_bits(clk_sym), "ADDR": addr, "DATA": data, "EN": en},
            params,
            src_node,
        )
        return True

    def _emit_memory_read(self, acc: _ArrayAccess, node: Any) -> tuple[Bit, ...]:
        """Emit an asynchronous ``$memrd_v2`` for an R-value ``mem[addr]``,
        a constant ``addr`` too (rtl-buddy-cdc#398: ``mem[2]`` read the
        array's undriven bits), and return its data bits — what Yosys
        ``proc`` leaves too; a registered read is this port followed by the
        ordinary flop."""
        addr = self._memory_address(acc, node)
        params = self._memory_params(acc, len(addr))
        width = int(params["WIDTH"], 2)
        data = self._alloc_anon_bits(width)
        params.update(
            {
                "CLK_ENABLE": "0",
                "CLK_POLARITY": "0",
                "TRANSPARENCY_MASK": "",
                "COLLISION_X_MASK": "",
                "CE_OVER_SRST": "0",
                "ARST_VALUE": "x" * width,
                "SRST_VALUE": "x" * width,
                "INIT_VALUE": "x" * width,
            }
        )
        self._memory_cell(
            "$memrd_v2",
            {
                "CLK": ("x",),
                "EN": ("1",),
                "ARST": ("0",),
                "SRST": ("0",),
                "ADDR": addr,
                "DATA": data,
            },
            params,
            node,
        )
        return data

    def _index_match(self, idx: tuple[Bit, ...], index: int, node: Any) -> Bit:
        """One bit: run-time index ``idx`` equals ``index`` (``$eq``, or
        ``$logic_not`` for 0, as Yosys ``proc_mux`` compares)."""
        if index == 0:
            return self._emit_comb_cell("$logic_not", {"A": idx}, 1, node)[0]
        width = max(len(idx), index.bit_length() + (1 if index < 0 else 0))
        value = self._int_bits(index % (1 << width), width)
        return self._emit_comb_cell("$eq", {"A": idx, "B": value}, 1, node)[0]

    def _array_mux(self, expr: Any) -> tuple[Bit, ...] | None:
        """A register array's element (or row) read at a run-time index:
        the ``$pmux`` Yosys ``mem2reg`` + ``proc`` build, its cases every
        element (highest index first), each selected by an index compare
        (rtl-buddy-cdc#398: it was a ``$memrd_v2`` of a memory nothing
        writes, so the read was cut off from the element registers)."""
        value = expr.value
        vtype = getattr(value, "type", None)
        vtype = getattr(vtype, "canonicalType", None) or vtype
        rng = getattr(vtype, "range", None)
        if rng is None:
            return None
        base = self._bits_of_expression(value)
        idx = self._bits_of_expression(expr.selector)
        left, right = int(rng.left), int(rng.right)
        count = abs(left - right) + 1
        stride = self._element_stride(vtype)
        if not idx or base is None or len(base) != count * stride:
            return None
        lo = min(left, right)
        words: list[Bit] = []
        selects: list[Bit] = []
        for pos in reversed(range(count)):
            words.extend(base[pos * stride : (pos + 1) * stride])
            selects.append(self._index_match(idx, lo + pos, expr))
        return self._emit_comb_cell(
            "$pmux",
            {"A": ("x",) * stride, "B": tuple(words), "S": tuple(selects)},
            stride,
            src_node=expr,
            parameters={
                "WIDTH": _param_int32(stride),
                "S_WIDTH": _param_int32(count),
            },
        )

    def _rhs_bits(
        self, expr: Any, width: int, what: str = "right-hand side"
    ) -> tuple[Bit, ...]:
        """``expr`` sized to ``width`` bits, or, when it does not lower, an
        opaque driver reported by :meth:`_warn_opaque` — never nothing: an
        undriven target reads as a clean run (rtl-buddy-cdc#377)."""
        bits = self._sized_rhs_bits(expr, width)
        if bits is not None:
            return bits
        return self._opaque(expr, width, what)

    def _opaque(self, node: Any, width: int, what: str) -> tuple[Bit, ...]:
        """An ``$_UNKNOWN_`` driver standing in for ``node``, reported."""
        self._warn_opaque(node, what)
        return self._unknown_driver(max(width, 1))

    def _warn_opaque(self, node: Any, what: str) -> None:
        """Report an expression replaced by an opaque ``$_UNKNOWN_`` driver,
        once per source location (an unrolled loop or an inlined function
        reaches it repeatedly). The logic behind it is invisible to the
        rules, so a crossing through it can be missed."""
        self._lowering_failures = getattr(self, "_lowering_failures", 0) + 1
        where = self._src_attr(node)
        key = (where, what)
        if key in self._lvalue_warned:
            return
        self._lvalue_warned.add(key)
        # An implicit conversion has no syntax of its own: name its operand.
        while getattr(node, "syntax", None) is None and getattr(node, "operand", None):
            node = node.operand
        text = " ".join(str(getattr(node, "syntax", "") or "").split())
        if len(text) > 60:
            text = text[:57] + "..."
        _log.warning(
            "slang frontend: %s: %s%s not lowered; modelled as an opaque "
            "driver, so the logic feeding it is invisible to the CDC rules",
            where or "<unknown location>",
            what,
            f" `{text}`" if text else "",
        )

    def _unknown_driver(self, width: int) -> tuple[Bit, ...]:
        """Allocate a fresh net driven by a stub ``$_UNKNOWN_`` cell.

        Used for RHS shapes the elaborator doesn't model yet. The
        driver cell exists so the rule pack's driver lookup still
        finds *something* — but with type ``$_UNKNOWN_`` it isn't
        recognised as a comb primitive or a flop, so traversal stops
        there. That's the conservative behaviour: we miss real
        crossings rather than emit phantom ones.
        """
        bits: tuple[Bit, ...] = tuple(
            range(self._next_bit_id, self._next_bit_id + width)
        )
        self._next_bit_id += width
        name = self._fresh_cell_name("$_UNKNOWN_")
        self._cells[name] = Cell(
            name=name,
            type="$_UNKNOWN_",
            connections={"Y": bits},
            parameters={},
            attributes={},
        )
        return bits

    # -- always_latch ---------------------------------------------------

    def _emit_always_latch(self, block: Any) -> None:
        """Lower an ``always_latch`` block like any level-sensitive body
        (:meth:`_lower_comb_body`): each variable held on some path is a
        ``$dlatch`` loading its value under the condition it is written.

        Issue #39 first lowered the single-arm ``if (en) q = d;`` shape;
        every other body (an ``else`` arm, a ``case``, several statements
        in the arm) was skipped silently, until rtl-buddy-cdc#377 routed
        the block through the general lowering. The latch stays outside
        ``flops.FF_CELL_TYPES``: it does not bound a clock domain and is
        transparent to ``find_crossings``. ``$adlatch`` (async reset) is
        not modelled. A body that assigns every variable on every path
        infers no latch, as on the Yosys path.
        """
        self._lower_comb_body(block.body, block)

    def _emit_dlatch_cell(
        self,
        en: Bit,
        q_bits: tuple[Bit, ...],
        d_bits: tuple[Bit, ...],
        src_node: Any,
    ) -> None:
        """Allocate a ``$dlatch`` with the given enable / data / Q.

        ``EN_POLARITY`` is hard-coded to active-high. Inverted-
        enable shapes (``if (~en) lhs = rhs``) come through here with
        ``en`` already routed through a ``$not`` cell by
        ``_lower_condition_to_bit`` → ``_bits_of_expression``, which
        mirrors how the slang frontend models conditional polarity
        elsewhere. Folding the inversion into ``EN_POLARITY=0`` would
        diverge from that convention; the rule pack doesn't read the
        polarity parameter today, so the netlist shape is what
        matters.
        """
        width = len(q_bits)
        parameters: dict[str, str] = {
            "EN_POLARITY": "1",
            "WIDTH": _param_int32(width),
        }
        attrs: dict[str, str] = {}
        src = self._src_attr(src_node) if src_node is not None else None
        if src is not None:
            attrs["src"] = src
        name = self._fresh_cell_name("$dlatch")
        self._cells[name] = Cell(
            name=name,
            type="$dlatch",
            connections={
                "EN": (en,),
                "D": d_bits,
                "Q": q_bits,
            },
            parameters=parameters,
            attributes=attrs,
        )

    # -- always_comb ----------------------------------------------------

    def _emit_always_comb(self, block: Any) -> None:
        """Lower an ``always_comb`` block: see :meth:`_lower_comb_body`.

        An ``always_comb`` that leaves a variable unassigned on some path
        describes a latch, which the language forbids there; Yosys stops
        with an error. This frontend lowers the latch and says so."""
        self._lower_comb_body(block.body, block, comb_keyword=True)

    def _lower_comb_body(
        self, stmt: Any, block: Any = None, *, comb_keyword: bool = False
    ) -> None:
        """Lower a level-sensitive procedural body (``always_comb``,
        ``always @*``, ``always @(a or b)``) the way Yosys ``proc`` does.

        Each blocking write re-points the written variable's current value
        at its right-hand side; an ``if`` / ``case`` merges the values its
        branches leave with a ``$mux`` per variable, a branch that does
        not write a variable contributing the variable's value before the
        statement (:meth:`_walk_conditional_statement`). A variable whose
        final value still holds its own value on some path is a latch:
        :meth:`_infer_latches` turns that path into the enable of a
        ``$dlatch`` (rtl-buddy-cdc#377), so one incompletely assigned
        scratch variable no longer costs the whole block. Every other
        written variable is combinational.

        The block is order-independent in the language (and on the Yosys
        path), so a reader earlier in source order (``always @(posedge
        clk) s1 <= x;`` above ``always @* x = a_q;``) must see the result
        too: each written placeholder bit is recorded for
        :meth:`_apply_forward_renames`, as a continuous assign records its
        own.
        """
        before = dict(self._var_bits)
        first_cell = len(self._cells)
        prev_muxes = self._branch_muxes
        self._branch_muxes = {}
        try:
            self._walk_comb_statement(stmt)
            latched = self._infer_latches(before, block if block is not None else stmt)
            if latched:
                self._drop_dead_branch_muxes(before, first_cell)
        finally:
            self._branch_muxes = prev_muxes
        if latched and comb_keyword:
            _log.warning(
                "slang frontend: %s: `always_comb` leaves %s unassigned on "
                "some path; lowered as a latch",
                self._src_attr(block) or "<unknown location>",
                ", ".join(f"`{name}`" for name in latched),
            )
        self._record_forward_renames(before)

    def _record_forward_renames(self, before: dict[Any, tuple[Bit, ...]]) -> None:
        """Record ``old placeholder bit -> new driver bit`` for every
        variable whose bits differ from ``before`` (see
        :meth:`_lower_comb_body`)."""
        for sym, old in before.items():
            new = self._var_bits.get(sym)
            if new is None or new == old or len(new) != len(old):
                continue
            for o, n in zip(old, new):
                # Only a variable's own placeholder bit is renamed: an old
                # bit that is some cell's output (a variable written by two
                # blocks) must keep its driver.
                if o in self._placeholder_bits and o != n:
                    self._forward_renames[o] = n

    def _branch_mux(
        self, sel: Bit, a: tuple[Bit, ...], b: tuple[Bit, ...], src_node: Any
    ) -> tuple[Bit, ...]:
        """``$mux(S=sel, A=a, B=b)`` merging two branch values, recorded
        so :meth:`_infer_latches` can follow it. The whole value goes
        through one mux even where the legs agree, as Yosys ``proc`` muxes
        the assigned signal: a one-hot ``case`` decode is then one
        multi-bit driver per arm, the shape CDC-019 keys on."""
        width = max(len(a), len(b))
        a = self._resize_bits(a, width)
        b = self._resize_bits(b, width)
        if a == b:
            return a
        y = self._emit_comb_cell(
            cell_type="$mux",
            inputs={"A": a, "B": b, "S": (sel,)},
            output_width=width,
            src_node=src_node,
        )
        self._branch_muxes[y] = (sel, a, b)
        return y

    def _infer_latches(
        self, before: dict[Any, tuple[Bit, ...]], src_node: Any
    ) -> list[str]:
        """Turn every variable a level-sensitive body holds on some path
        into a ``$dlatch``; return their names.

        A held path shows up in the variable's final value as a branch
        ``$mux`` leg that is the variable's own value from before the body
        (its placeholder net). Following the merge muxes from the final
        value splits it into the data loaded when the latch is open and
        the condition under which it is (Yosys ``proc_dlatch``):

        - a leg that is the held value contributes no data and enable 0;
        - ``mux(s, held, v)`` loads ``v`` when ``s`` (enable ``s & en_v``),
          ``mux(s, v, held)`` when ``!s``;
        - ``mux(s, a, b)`` with both legs live keeps the mux for the data
          (the original one when neither leg changed), enable ``s ? en_b :
          en_a``;
        - anything else is loaded unconditionally (enable 1).

        A variable with enable 1 is combinational and keeps its value.
        For a latch, bits no path writes (data is their own net) stay out
        of it and undriven, as before; the latch's ``Q`` is the variable's
        net, which becomes its value again.
        """
        latched: list[str] = []
        for sym, held in before.items():
            final = self._var_bits.get(sym)
            if final is None or final == held or len(final) != len(held):
                continue
            if not all(b in self._placeholder_bits for b in held):
                continue  # driven elsewhere too: no hold value to find
            split = self._split_hold(final, held)
            if split is None:
                continue  # never written on any path
            data, enable = split
            if enable == "1":
                continue
            # A bit whose loaded data is its own net is never written.
            lanes = [i for i in range(len(held)) if data[i] != held[i]]
            self._emit_dlatch_cell(
                enable,
                tuple(held[i] for i in lanes),
                tuple(data[i] for i in lanes),
                src_node=src_node,
            )
            self._var_bits[sym] = held
            latched.append(str(getattr(sym, "name", "?")))
        return latched

    def _drop_dead_branch_muxes(
        self, before: dict[Any, tuple[Bit, ...]], first_cell: int
    ) -> None:
        """Remove the branch muxes a latch made dead: the hold-feedback
        merges :meth:`_infer_latches` replaced by the latch's data and
        enable. Left in place, they would read the latch output and its
        enable condition as data with nothing to observe them (Yosys
        ``proc_dlatch`` leaves no such cell either). Only cells emitted
        by this body can read them, and only values it changed."""
        new_cells = list(self._cells)[first_cell:]
        live_values: set[Bit] = {
            b
            for sym, bits in self._var_bits.items()
            if before.get(sym) != bits
            for b in bits
        }
        candidates = {
            name
            for name in new_cells
            if self._cells[name].connections.get("Y") in self._branch_muxes
        }
        changed = True
        while changed:
            changed = False
            used = set(live_values)
            for name in new_cells:
                if name in self._cells:
                    for pin, bits in self._cells[name].connections.items():
                        if pin != "Y":
                            used.update(bits)
            for name in sorted(candidates):
                cell = self._cells.get(name)
                if cell is not None and not used.intersection(cell.connections["Y"]):
                    del self._cells[name]
                    candidates.discard(name)
                    changed = True

    def _split_hold(
        self, value: tuple[Bit, ...], held: tuple[Bit, ...]
    ) -> tuple[tuple[Bit, ...], Bit] | None:
        """``(data, enable)`` of ``value`` relative to the held value
        ``held``, or ``None`` when ``value`` is ``held`` on every path (see
        :meth:`_infer_latches`). ``enable`` is ``"1"`` when no path holds."""
        if value == held:
            return None
        mux = self._branch_muxes.get(value)
        if mux is None:
            return value, "1"
        sel, a, b = mux
        split_a = self._split_hold(a, held)
        split_b = self._split_hold(b, held)
        if split_a is None and split_b is None:
            return None
        if split_a is None:
            assert split_b is not None
            return split_b[0], self._and_bit(sel, split_b[1])
        if split_b is None:
            return split_a[0], self._and_bit(self._lower_not(sel), split_a[1])
        (data_a, en_a), (data_b, en_b) = split_a, split_b
        if (data_a, data_b) == (a, b):
            data = value
        else:
            data = self._branch_mux(sel, data_a, data_b, None)
        if en_a == en_b:
            return data, en_a
        enable = self._emit_comb_cell(
            "$mux", {"A": (en_a,), "B": (en_b,), "S": (sel,)}, output_width=1
        )
        return data, enable[0]

    def _and_bit(self, a: Bit, b: Bit) -> Bit:
        """``a & b`` for single bits, folding a constant ``"1"``."""
        if b == "1":
            return a
        if a == "1":
            return b
        return self._emit_comb_cell("$and", {"A": (a,), "B": (b,)}, output_width=1)[0]

    def _walk_comb_statement(self, stmt: Any) -> None:
        kind = self._kind_name(stmt)
        if kind == "BlockStatement":
            self._walk_comb_statement(stmt.body)
            return
        if kind in {"StatementList", "ListStatement"}:
            for s in getattr(stmt, "list", []) or []:
                self._walk_comb_statement(s)
            return
        if kind == "ExpressionStatement":
            write = self._as_assignment(stmt.expr)
            if write is None:
                self._other_statement(stmt, "combinational block")
                return
            self._alias_assign(write)
            return
        if kind == "ConditionalStatement":
            self._walk_conditional_statement(stmt)
            return
        if kind == "CaseStatement":
            self._walk_case_statement(stmt)
            return
        if kind == "ReturnStatement" and self._inline_returns:
            # ``return expr;`` ending an inlined function body.
            ret = self._inline_returns[-1]
            value = getattr(stmt, "expr", None)
            bits = (
                self._sized_rhs_bits(value, self._width_of(ret))
                if value is not None
                else None
            )
            if bits is None:
                self._lowering_failures += 1
            else:
                self._var_bits[ret] = bits
            return
        if kind == "ForLoopStatement":
            self._unroll_for(stmt, self._walk_comb_statement)
            return
        if kind == "VariableDeclStatement":
            self._declare_local(stmt, clocked=False)
            return
        self._other_statement(stmt, "combinational block")

    # Statements with no logic of their own: skipped without a word.
    _NO_LOGIC_STATEMENTS = frozenset(
        {
            "EmptyStatement",
            "ImmediateAssertionStatement",
            "ConcurrentAssertionStatement",
            "ProceduralCheckerStatement",
        }
    )

    def _other_statement(self, stmt: Any, where: str) -> None:
        """A statement the procedural walkers have no dedicated lowering
        for. Increments / decrements (``n++``) are writes; a system task
        (``$display``) and an assertion have no logic; anything else (a
        task call, a ``while`` / ``repeat`` / ``foreach`` loop, an event
        control inside the body, ``disable``, …) is reported rather than
        skipped silently (rtl-buddy-cdc#377)."""
        kind = self._kind_name(stmt)
        if kind in self._NO_LOGIC_STATEMENTS:
            return
        if kind == "ExpressionStatement":
            expr = stmt.expr
            if type(expr).__name__ == "CallExpression" and getattr(
                expr, "isSystemCall", False
            ):
                return
        self._warn_dropped(stmt, f"`{kind}` is not modelled in a {where}", "statement")

    _STEP_OPS = {
        "Postincrement": "$add",
        "Preincrement": "$add",
        "Postdecrement": "$sub",
        "Predecrement": "$sub",
    }

    def _as_assignment(self, expr: Any) -> Any | None:
        """``expr`` as a write the walkers lower: an assignment itself, an
        ``n++`` / ``n--`` statement as ``n = n ± 1``, else ``None``."""
        kind = type(expr).__name__
        if kind == "AssignmentExpression":
            return expr
        if kind != "UnaryExpression":
            return None
        cell = self._STEP_OPS.get(str(getattr(expr, "op", "")).rsplit(".", 1)[-1])
        if cell is None:
            return None
        operand = expr.operand
        current = self._bits_of_expression(operand)
        if not current:
            return None
        one: tuple[Bit, ...] = ("1",) + ("0",) * (len(current) - 1)
        bits = self._emit_comb_cell(
            cell, {"A": current, "B": one}, output_width=len(current), src_node=expr
        )
        return _Assign(operand, _Bits(bits, expr), expr)

    def _declare_local(self, stmt: Any, *, clocked: bool) -> None:
        """A block-local variable declaration: its initialiser, when it has
        one, is its value for the rest of the block."""
        sym = getattr(stmt, "symbol", None)
        init = getattr(sym, "initializer", None)
        if sym is None or init is None:
            return
        canonical = self._canonical_var(sym)
        net = self._alloc_bits(sym)
        value = self._rhs_bits(init, len(net))
        if clocked:
            self._blocking_nets.setdefault(canonical, net)
        self._var_bits[canonical] = value

    def _walk_branches(self, arms: list[Any]) -> list[dict[Any, tuple[Bit, ...]]]:
        """Walk each statement of ``arms`` (``None`` for an empty arm) from
        the same starting values; return the values each leaves. The
        current values are restored afterwards, and cells emitted in each
        arm are kept."""
        base = dict(self._var_bits)
        states: list[dict[Any, tuple[Bit, ...]]] = []
        for arm in arms:
            if arm is not None:
                self._walk_comb_statement(arm)
            states.append(dict(self._var_bits))
            self._var_bits = dict(base)
        return states

    def _walk_conditional_statement(self, stmt: Any) -> None:
        """Lower a combinational ``if`` / ``else`` into a per-variable
        ``$mux(S=cond, A=else value, B=then value)``.

        Each arm is walked from the same starting values; a variable
        written in either arm gets the mux, an arm that does not write it
        contributing its value before the ``if`` (Yosys ``proc``). When
        that value is the variable's own net — not assigned yet in this
        body — the leg is a held value, and :meth:`_infer_latches` turns
        it into a latch enable. A condition known at elaboration time
        walks the live arm only.
        """
        conds = getattr(stmt, "conditions", None) or []
        cond_expr = conds[0].expr if conds else None
        const = self._const_int(cond_expr) if len(conds) == 1 else None
        if const is not None:
            live = stmt.ifTrue if const != 0 else stmt.ifFalse
            if live is not None:
                self._walk_comb_statement(live)
            return
        sel = self._condition_bit(stmt)
        base = dict(self._var_bits)
        true_state, false_state = self._walk_branches([stmt.ifTrue, stmt.ifFalse])
        written = {
            k
            for state in (true_state, false_state)
            for k, v in state.items()
            if base.get(k) != v
        }
        for var in written:
            base_v = base.get(var)
            t = true_state.get(var, base_v)
            f = false_state.get(var, base_v)
            if t is None or f is None or t == f:
                chosen = t if t is not None else f
                assert chosen is not None
                self._var_bits[var] = chosen
                continue
            self._var_bits[var] = self._branch_mux(sel, f, t, stmt)

    def _walk_case_statement(self, stmt: Any) -> None:
        """Lower a combinational ``case`` into a chained ``$mux`` per
        variable, item 0 outermost (first match wins).

        The chain starts at the ``default`` arm's value, or, with no
        ``default``, at the variable's value before the ``case`` — unless
        the items' constant labels cover every value of the case
        expression, when the last item is the fallback (Yosys infers no
        latch for a full ``case`` either). An item that does not write
        the variable contributes its value before the ``case``. Each
        item's select is the OR of ``case_expr == label`` per label.
        A case expression known at elaboration time walks the matching
        arm only.
        """
        items = list(getattr(stmt, "items", []) or [])
        default_arm = getattr(stmt, "defaultCase", None)
        folded, live = self._constant_case_arm(stmt)
        if folded:
            if live is not None:
                self._walk_comb_statement(live)
            return
        sel_bits = self._case_expr_bits(stmt)
        if not items:
            if default_arm is not None:
                self._walk_comb_statement(default_arm)
            return
        selects: list[Bit] = []
        for item in items:
            labels = list(getattr(item, "expressions", []) or [])
            match = self._or_bits(
                [self._case_label_match(sel_bits, label, stmt) for label in labels]
            )
            selects.append(match if match is not None else "0")
        fallback_arm = default_arm
        if fallback_arm is None and self._case_is_full(stmt, items, len(sel_bits)):
            fallback_arm = getattr(items[-1], "stmt", None)
            items, selects = items[:-1], selects[:-1]
        base = dict(self._var_bits)
        states = self._walk_branches(
            [getattr(item, "stmt", None) for item in items] + [fallback_arm]
        )
        arm_states, fallback_state = states[:-1], states[-1]
        written = {k for state in states for k, v in state.items() if base.get(k) != v}
        for var in written:
            base_v = base.get(var)
            chain = fallback_state.get(var, base_v)
            for sel, state in zip(reversed(selects), reversed(arm_states)):
                value = state.get(var, base_v)
                if chain is None or value is None:
                    chain = value if value is not None else chain
                    continue
                if value != chain:
                    chain = self._branch_mux(sel, chain, value, stmt)
            assert chain is not None
            self._var_bits[var] = chain

    def _case_is_full(self, stmt: Any, items: list[Any], width: int) -> bool:
        """The items' labels are constants covering all ``2**width`` values
        of a plain ``case`` expression (no wildcards)."""
        if width > 16:
            return False
        if str(getattr(stmt, "condition", "")).rsplit(".", 1)[-1] not in {
            "Normal",
            "",
        }:
            return False
        seen: set[int] = set()
        for item in items:
            for expr in getattr(item, "expressions", []) or []:
                value = self._const_int(expr)
                if value is None:
                    return False
                seen.add(value & ((1 << width) - 1))
        return len(seen) == 1 << width

    def _alias_assign(self, expr: Any) -> None:
        """For a blocking assignment ``lhs = rhs``, rewrite ``lhs``'s
        bits to point at ``rhs``'s lowered bits — same aliasing trick
        the continuous-assign path uses."""
        self._compound_lhs.append(expr.left)
        try:
            self._comb_write(expr)
        finally:
            self._compound_lhs.pop()

    def _comb_write(self, expr: Any) -> None:
        """:meth:`_alias_assign` for one write."""
        # A nonblocking write in a combinational block is a style
        # violation the language allows; Yosys lowers it like a blocking
        # one, and so does this frontend (rtl-buddy-cdc#377: it was
        # skipped without a word).
        lhs = expr.left
        lhs_kind = type(lhs).__name__
        if self._is_named(lhs):
            rhs_bits = self._rhs_bits(expr.right, len(self._alloc_bits(lhs.symbol)))
            self._rewrite_aliased(lhs, rhs_bits)
            return
        if self._lvalue_targets(lhs) is None:
            if lhs_kind == "ConcatenationExpression":
                # A part with a run-time index (rtl-buddy-cdc#402).
                for part in self._concat_part_writes(lhs, expr.right, expr):
                    self._comb_write(part)
                return
            # Run-time index or position (rtl-buddy-cdc#402).
            if not self._indexed_comb_write(lhs, expr.right):
                self._warn_lvalue_dropped(expr, lhs)
            return
        # Concatenation, constant bit / part select, struct member
        # (rtl-buddy-cdc#370): splice the RHS into the written bits.
        if not self._splice_assign(lhs, expr.right, continuous=False):
            self._warn_lvalue_dropped(expr, lhs)

    # -- writes at a run-time index or position (rtl-buddy-cdc#402) -------

    def _concat_part_writes(self, lhs: Any, rhs: Any, node: Any) -> list[Any]:
        """``{a[i], b} = rhs`` as one write per part, each taking its slice
        of ``rhs`` (sized to the concatenation first, read once), for a
        target :meth:`_lvalue_targets` cannot resolve as a whole: a part
        with a run-time index (rtl-buddy-cdc#402). Yosys writes each part
        the same way."""
        parts = list(getattr(lhs, "operands", None) or [])
        widths = [self._expr_width(p) for p in parts]
        rhs_bits = self._rhs_bits(rhs, sum(widths))
        blocking = not getattr(node, "isNonBlocking", False)
        out: list[Any] = []
        low = 0
        for part, width in reversed(list(zip(parts, widths))):
            write = _Assign(part, _Bits(rhs_bits[low : low + width], node), node)
            write.isNonBlocking = not blocking
            out.append(write)
            low += width
        out.reverse()
        return out

    def _indexed_targets(
        self, lhs: Any
    ) -> tuple[Any, int, list[tuple[int, Bit | None]], list[Any]] | None:
        """Split a write target with a run-time index or position into
        ``(variable, word width, words, packed selects)``: ``words`` the
        ``(first bit, select)`` of every word it can reach (``select``
        ``None`` for the one word of a constant index), ``packed selects``
        (innermost first) what is written of the word.

        For a register array the words are its elements, each selected by
        its index compares (Yosys ``mem2reg`` turns ``a[i] = d`` into ``if
        (i == k) a[k] = d`` for every ``k``); otherwise the word is the
        whole variable. ``None`` for anything else (a memory, a slice of an
        array, a hierarchical reference)."""
        acc = self._array_access(lhs)
        if acc is not None:
            if acc.sym in self._memories or acc.element is None:
                return None
            width = self._unpacked_element_width(acc.sym) or 1
            words: list[tuple[int, Bit | None]] = [(0, None)]
            for (left, right), sel in zip(acc.dims, acc.selectors):
                lo, count = min(left, right), abs(left - right) + 1
                c = self._const_int(sel)
                picks: list[tuple[int, Bit | None]]
                if c is not None:
                    picks = [(c - lo, None)] if 0 <= c - lo < count else []
                else:
                    idx = self._bits_of_expression(sel) or self._opaque(
                        sel, 1, "array index"
                    )
                    picks = [
                        (p, self._index_match(idx, lo + p, sel)) for p in range(count)
                    ]
                nxt: list[tuple[int, Bit | None]] = []
                for pos, cond in words:
                    for p, hit in picks:
                        both = hit if cond is None else cond
                        if cond is not None and hit is not None:
                            both = self._and_bit(cond, hit)
                        nxt.append((pos * count + p, both))
                words = nxt
            return acc.sym, width, [(p * width, c) for p, c in words], acc.packed
        nodes: list[Any] = []
        e = lhs
        while type(e).__name__ in self._SELECT_KINDS:
            nodes.append(e)
            e = getattr(e, "value", None)
        if not self._is_named(e) or not nodes:
            return None
        sym = self._canonical_var(e.symbol)
        sym_type = getattr(sym, "type", None)
        if (
            self._is_constant_name(sym)
            or self._unpacked_ranges(sym) is not None
            or not getattr(sym_type, "isIntegral", False)
        ):
            return None
        width = len(self._alloc_bits(sym))
        return sym, width, [(0, None)], list(reversed(nodes))

    def _word_write(
        self, nodes: list[Any], width: int, rhs: Any
    ) -> tuple[int, int, tuple[Bit, ...], Callable[[], tuple[Bit, ...]] | None] | None:
        """What a write through packed selects ``nodes`` (innermost first)
        does to a ``width``-bit word: ``(start, size, data, mask)`` over
        bits ``[start, start + size)``. Constant selects give the written
        bits as ``data`` and no ``mask``. A run-time position in the last
        select (``w[j]``, ``w[j +: N]``, ``w[j -: N]``) gives ``data`` the
        right-hand side shifted to that position and ``mask`` (built on
        first call) the written bits, the ``$shl`` pair Yosys builds
        (``$shift`` by the negated position when it can be negative).
        ``None`` when a select is neither."""
        sub = self._packed_subrange(nodes, width)
        if sub is not None:
            return sub[0], sub[1], self._rhs_bits(rhs, sub[1]), None
        if not nodes:
            return None
        last = nodes[-1]
        sub = self._packed_subrange(nodes[:-1], width)
        vtype = getattr(getattr(last, "value", None), "type", None)
        vtype = getattr(vtype, "canonicalType", None) or vtype
        if (
            sub is None
            or vtype is None
            or getattr(vtype, "isUnpackedArray", False)
            or not getattr(vtype, "isIntegral", False)
        ):
            return None
        kind = type(last).__name__
        count, down = 1, False
        if kind == "ElementSelectExpression":
            start_expr = getattr(last, "selector", None)
        elif kind == "RangeSelectExpression":
            sel_kind = str(getattr(last, "selectionKind", "")).rsplit(".", 1)[-1]
            start_expr = getattr(last, "left", None)
            count = self._const_int(getattr(last, "right", None)) or 0
            down = sel_kind == "IndexedDown"
            if sel_kind not in {"IndexedUp", "IndexedDown"} or count < 1:
                return None
        else:
            return None
        if self._const_int(start_expr) is not None:
            return None  # a constant select out of range
        idx = self._bits_of_expression(start_expr)
        if not idx:
            return None
        start, size = sub
        pos, signed = self._position_bits(vtype, idx, last, count=count, down=down)
        part = count * self._element_stride(vtype)
        data = self._shift_into(self._rhs_bits(rhs, part), pos, signed, size, last)
        memo: list[tuple[Bit, ...]] = []

        def mask() -> tuple[Bit, ...]:
            if not memo:
                memo.append(self._shift_into(("1",) * part, pos, signed, size, last))
            return memo[0]

        return start, size, data, mask

    def _shift_into(
        self,
        value: tuple[Bit, ...],
        pos: tuple[Bit, ...],
        signed: bool,
        width: int,
        node: Any,
    ) -> tuple[Bit, ...]:
        """``value`` moved up ``pos`` bits into a zero-filled ``width``-bit
        vector: a ``$shl``, or, for a position that can be negative, the
        ``$shift`` by ``-pos`` Yosys emits."""
        if not signed:
            return self._emit_comb_cell("$shl", {"A": value, "B": pos}, width, node)
        neg = self._emit_comb_cell(
            "$neg",
            {"A": pos},
            len(pos),
            node,
            parameters={"A_SIGNED": _param_int32(1)},
        )
        return self._emit_comb_cell(
            "$shift",
            {"A": value, "B": neg},
            width,
            node,
            parameters={"A_SIGNED": _param_int32(0), "B_SIGNED": _param_int32(1)},
        )

    def _masked_update(
        self,
        old: tuple[Bit, ...],
        data: tuple[Bit, ...],
        mask: Callable[[], tuple[Bit, ...]] | None,
        node: Any,
    ) -> tuple[Bit, ...]:
        """``old`` with the ``mask`` bits replaced by ``data``: ``(old &
        ~mask) | data`` (``data`` is zero outside ``mask``), or just
        ``data`` for a constant select or an all-zero ``old`` (the
        ``base = '0; base[i] = 1;`` decoder stays one ``$shl``, the
        CDC-019 shape)."""
        if mask is None or all(b == "0" for b in old):
            return data
        width = len(old)
        keep = self._emit_comb_cell("$not", {"A": mask()}, width, node)
        kept = self._emit_comb_cell("$and", {"A": old, "B": keep}, width, node)
        return self._emit_comb_cell("$or", {"A": kept, "B": data}, width, node)

    def _indexed_comb_write(self, lhs: Any, rhs: Any) -> bool:
        """Lower a combinational write with a run-time index or position:
        ``v[i] = d`` of a packed vector, ``g[i] = d`` / ``g[i][j] = b`` of
        a register array (rtl-buddy-cdc#402).

        Each word the target can reach becomes ``mux(i == k, old, new)``
        over the written bits, lane-aligned, as Yosys ``mem2reg`` + ``proc``
        build it (it was a ``$shl`` by ``hi - i`` over the whole array, so
        an ascending array's index ``i`` reached element ``hi - i``); a
        run-time bit position inside the word is ``(old & ~mask) | data``
        (:meth:`_word_write`; it was ``old | data``, so a 0 never cleared
        a bit). ``False`` for a shape this does not model (the caller
        reports it)."""
        found = self._indexed_targets(lhs)
        if found is None:
            return False
        sym, width, words, nodes = found
        if all(c is None for _b, c in words) and (
            self._packed_subrange(nodes, width) is not None
        ):
            return False  # nothing at a run-time position
        write = self._word_write(nodes, width, rhs)
        if write is None:
            return False
        start, size, data, mask = write
        current = self._var_bits.get(sym)
        if current is None:
            current = self._alloc_bits(sym)
        new = list(current)
        for base, cond in words:
            lo = base + start
            old = tuple(current[lo : lo + size])
            value = self._masked_update(old, data, mask, lhs)
            if cond is not None:
                value = self._build_hold_mux(cond, old, value)
            new[lo : lo + size] = value
        self._merge_canonical_var(sym, current, tuple(new))
        return True

    def _indexed_clocked_write(self, lhs: Any, expr: Any) -> bool:
        """A clocked write with a run-time index or position: one write
        of each word the target can reach, under its index compare
        (:meth:`_indexed_targets`), as Yosys ``mem2reg`` rewrites ``a[i]
        <= d`` (``if (i == k) a[k] <= d`` for each ``k``), so each
        element's ``D`` is ``mux(i == k, Q, d)``. A run-time bit position
        inside the word (``a[i][j] <= b``, ``v[j] <= b``) writes ``(Q &
        ~mask) | data`` over the word, as Yosys does (rtl-buddy-cdc#402:
        it was reported and dropped). ``False`` for a shape this does not
        model."""
        found = self._indexed_targets(lhs)
        if found is None:
            return False
        sym, width, words, nodes = found
        if all(c is None for _b, c in words) and (
            self._packed_subrange(nodes, width) is not None
        ):
            return False  # nothing at a run-time position
        write = self._word_write(nodes, width, expr.right)
        if write is None:
            return False
        start, size, data, mask = write
        net = self._blocking_nets.get(sym) or self._alloc_bits(sym)
        self._proc_regs.setdefault(sym, net)
        blocking = not getattr(expr, "isNonBlocking", False)
        for base, cond in words:
            lane = base + start
            current = self._var_bits.get(sym, net)
            old = tuple(current[lane : lane + size])
            value = self._masked_update(old, data, mask, lhs)
            if cond is None:
                self._proc_case.items.append((sym, lane, value))
                if blocking:
                    self._forward_blocking_write(sym, net, lane, value)
                continue
            case = _ProcCase()
            case.items.append((sym, lane, value))
            self._proc_case.items.append(_ProcSwitch([(cond, case)]))
            if blocking:
                held = self._build_hold_mux(cond, old, value)
                self._forward_blocking_write(sym, net, lane, held)
        return True

    def _position_bits(
        self,
        base_type: Any,
        idx_bits: tuple[Bit, ...],
        node: Any,
        count: int = 1,
        down: bool = False,
    ) -> tuple[tuple[Bit, ...], bool]:
        """``(bits, signed)``: the LSB-first position of the lowest bit of
        ``v[idx]`` (``count`` 1) or of ``v[idx +: count]`` / ``v[idx -:
        count]`` (``down``) in a vector of ``base_type``. A descending
        ``[hi:lo]`` range gives ``idx - lo`` (``-:`` also ``- (count -
        1)``), an ascending ``[lo:hi]`` one ``hi - idx`` (``+:`` also ``-
        (count - 1)``); a packed array multiplies by its element width.
        ``signed`` when the position is computed and so can be negative
        (Yosys marks such a ``$shiftx`` ``B`` signed)."""
        t = getattr(base_type, "canonicalType", None) or base_type
        rng = getattr(t, "range", None)
        left = getattr(rng, "left", None)
        right = getattr(rng, "right", None)
        pos = idx_bits
        signed = False
        if left is not None and right is not None:
            left, right = int(left), int(right)
            span = max(abs(left), abs(right))
            if count > 1:
                span = abs(left) + abs(right) + count
            width = max(len(idx_bits), span.bit_length()) + 1
            idx = self._resize_bits(idx_bits, width)

            def const(v: int) -> tuple[Bit, ...]:
                return tuple("1" if (v >> i) & 1 else "0" for i in range(width))

            if left >= right:
                offset = right + (count - 1 if down else 0)
                if offset > 0:
                    pos = self._emit_comb_cell(
                        "$sub", {"A": idx, "B": const(offset)}, width, src_node=node
                    )
                elif offset < 0:
                    pos = self._emit_comb_cell(
                        "$add", {"A": idx, "B": const(-offset)}, width, src_node=node
                    )
                signed = offset != 0
            else:
                top = right - (0 if down else count - 1)
                pos = self._emit_comb_cell(
                    "$sub", {"A": const(top), "B": idx}, width, src_node=node
                )
                signed = True
        stride = self._element_stride(t)
        if stride > 1:
            factor: tuple[Bit, ...] = tuple(
                "1" if (stride >> i) & 1 else "0" for i in range(stride.bit_length())
            )
            out_width = len(pos) + len(factor)
            if signed:
                pos = self._resize_bits(pos, out_width, signed=True)
            pos = self._emit_comb_cell(
                "$mul", {"A": pos, "B": factor}, out_width, src_node=node
            )
        return pos, signed

    # -- continuous assigns ---------------------------------------------

    def _emit_continuous_assign(self, member: Any) -> None:
        a = getattr(member, "assignment", None)
        if a is None or type(a).__name__ != "AssignmentExpression":
            return
        lhs, rhs = a.left, a.right
        if not self._is_named(lhs):
            # ``assign {c, s} = a + b;`` / ``assign bus[3:0] = x;``
            # (rtl-buddy-cdc#370).
            if not self._splice_assign(lhs, rhs, continuous=True):
                self._warn_lvalue_dropped(member, lhs)
            return
        rhs_bits = self._rhs_bits(rhs, len(self._alloc_bits(lhs.symbol)))
        # Aliasing trick: point the LHS variable at the RHS's bits so any
        # reader of the LHS sees the RHS source directly. This matches
        # Yosys' post-opt_clean output for ``assign out = sig`` and means
        # we don't need a $buf cell. A continuous assign holds for the
        # whole simulation, so a cell emitted *earlier in source order*
        # that already read the net (``if (pop && !empty)`` above ``assign
        # empty = …``) must see the RHS too, not the net's undriven
        # placeholder bits: :meth:`_drive_net` records that, applied to
        # every cell once the walk is done (#326: the async-FIFO flags are
        # the canonical forward use).
        self._drive_net(lhs.symbol, 0, rhs_bits, member)

    def _apply_forward_renames(self) -> None:
        """Rewrite every cell connection through ``_forward_renames``
        (see :meth:`_emit_continuous_assign`), following chains
        (``assign a = b; assign b = c;`` in either order)."""
        renames = self._forward_renames
        if not renames:
            return

        def resolve(b: Bit) -> Bit:
            seen: set[Bit] = set()
            while b in renames and b not in seen:
                seen.add(b)
                b = renames[b]
            return b

        for name, cell in list(self._cells.items()):
            conns = {
                pin: tuple(resolve(b) for b in bits)
                for pin, bits in cell.connections.items()
            }
            if conns != cell.connections:
                self._cells[name] = Cell(
                    name=cell.name,
                    type=cell.type,
                    connections=conns,
                    parameters=cell.parameters,
                    attributes=cell.attributes,
                    port_directions=cell.port_directions,
                )
        # Ports and netnames too: a child output bound to *part* of a
        # parent net (``.q(dst[0])``, and every element of an instance
        # array with a sliced output, rtl-buddy-cdc#353) re-points only
        # those bits, which the whole-tuple alias merge in
        # :meth:`_merge_canonical_var` cannot express.
        for name, port in list(self._ports.items()):
            bits = tuple(resolve(b) for b in port.bits)
            if bits != port.bits:
                self._ports[name] = Port(
                    name=port.name, direction=port.direction, bits=bits
                )
        for name, nn in list(self._netnames.items()):
            bits = tuple(resolve(b) for b in nn.bits)
            if bits != nn.bits:
                self._netnames[name] = replace(nn, bits=bits)

    def _rewrite_aliased(self, lhs: Any, rhs_bits: tuple[Bit, ...]) -> None:
        """Set ``lhs``'s current value to ``rhs_bits`` (see
        :meth:`_merge_canonical_var`)."""
        canonical = self._canonical_var(lhs.symbol)
        self._merge_canonical_var(canonical, self._var_bits.get(canonical), rhs_bits)

    def _merge_canonical_var(
        self,
        canonical: Any,
        old_bits: tuple[Bit, ...] | None,
        new_bits: tuple[Bit, ...],
    ) -> None:
        """Set ``canonical``'s current value to ``new_bits``.

        ``_var_bits`` holds each variable's *value* as the walk has
        lowered it so far; its *net* (the placeholder bits
        :meth:`_alloc_bits` allocated, shared with every hierarchy alias
        and recorded in the variable's netname and port) is re-pointed at
        the final driver only by :meth:`_apply_forward_renames`, which
        every writer feeds: continuous assigns, net initialisers and
        combinational blocks (:meth:`_record_forward_renames`).

        Nothing else is rewritten here. Rewriting every other entry that
        held ``old_bits`` (as this did before rtl-buddy-cdc#377) cannot
        tell a hierarchy alias of the same net from a variable that
        merely holds the same value: after ``y = b; if (sel) y = a;`` it
        re-pointed the input ``b`` itself at ``a``. ``old_bits`` is kept
        for the callers' symmetry.
        """
        del old_bits
        self._var_bits[canonical] = new_bits
