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
==================================  =================

What currently works
~~~~~~~~~~~~~~~~~~~~
- Parse + elaborate one or more SV sources, resolve the top module.
- Ports (input / output / inout) with their bit-id allocation.
- Local ``logic`` / ``reg`` variables become :class:`Netname`s, with
  any ``(* attr *)`` declaration attributes propagated (looked up via
  ``Compilation.getAttributes(symbol)`` — pyslang stores attributes
  on the compilation, not on the symbol).
- ``always_ff`` blocks with the canonical async-reset shape::

      always_ff @(posedge clk or negedge rst_n)
          if (!rst_n) q <= 0; else q <= d;

  emit ``$adff`` cells with ``CLK``/``D``/``Q``/``ARST`` connections.
  Plain ``always_ff @(posedge clk) q <= d;`` emits ``$dff``.
- Verilog-2001 ``always`` blocks (rtl-buddy-cdc#345), by what they
  infer: edge-triggered → the ``always_ff`` lowering; ``@*`` or a
  complete level-sensitive body → ``always_comb``; a latch of the
  ``if (en) q = d;`` shape → ``$dlatch``. Anything else is reported
  with a ``WARNING`` (see :meth:`_ModuleBuilder._warn_dropped`).
- Blackbox boundaries (rtl-buddy-cdc#345): ``(* blackbox *)`` modules
  and ``--blackbox`` / ``--greybox`` names stay as one cell per
  instance, with a sibling module — see :func:`elaborate_with_blackboxes`.
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
- LHS bit-selects (``q[0] <= ...``) and contiguous ranges
  (``bus[3:0] <= ...``) — see :meth:`_lvalue_bits`. Same shapes
  accepted on the RHS via :meth:`_bits_of_expression`.
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
- **``casex`` / ``casez`` wildcard cases** — the
  :meth:`_walk_case_statement` lowering treats every label as a
  full ``$eq`` match, so wildcard bits in the label aren't honored.
  Uncommon in CDC-sensitive code and not yet wired up.
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

import importlib.metadata
import logging
import re
from pathlib import Path
from typing import Any

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


def _literal_base_binary() -> Any:
    import pyslang  # type: ignore[import-not-found]

    return pyslang.LiteralBase.Binary


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
        # True while lowering a plain Verilog-2001 ``always`` block, which,
        # unlike ``always_ff`` / ``always_comb``, carries no style rule
        # about blocking vs nonblocking writes (rtl-buddy-cdc#345).
        self._plain_always = False
        self._next_bit_id = 2
        # Map canonical pyslang VariableSymbol → tuple of bit refs.
        # PortSymbols are normalised to their internal Variable before
        # lookup so a port and its underlying variable share bits.
        # Entries start as integer-only tuples from the allocator, but
        # the continuous-assign aliasing pass can rewrite them to point
        # at constants or other variables' bits — hence ``Bit``.
        self._var_bits: dict[Any, tuple[Bit, ...]] = {}
        self._ports: dict[str, Port] = {}
        self._cells: dict[str, Cell] = {}
        self._netnames: dict[str, Netname] = {}
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
        # Stack of single-bit enable signals active in the current
        # always_ff walk. Pushed when entering a ``ConditionalStatement``
        # arm: ``cond`` for ifTrue, ``$not(cond)`` for ifFalse. Combined
        # via ``$and`` into a per-write enable when an assignment is
        # accumulated; the rule pack's ``_is_gated_bus_crossing`` then
        # recognises the resulting hold-feedback mux as a handshake-
        # gated bus crossing. See issue #64.
        self._enable_stack: list[Bit] = []
        # Per-procedural-block write accumulator: maps each LHS bit
        # tuple to a list of ``(enable, rhs)`` pairs in walk order.
        # ``_emit_assignment_expression`` appends instead of emitting
        # cells; ``_drain_proc_writes`` (called at end of
        # ``_emit_procedural_block``) builds one flop per LHS with a
        # mux tree from the accumulated writes. So
        # ``if (cond) q <= a; else q <= b;`` collapses to one ``$adff``
        # whose D is ``mux(cond, b, a)`` — not two flops with the same
        # Q. Initialised fresh on each procedural-block entry.
        self._proc_writes: dict[
            tuple[Bit, ...],
            list[tuple[Bit | None, tuple[Bit, ...]]],
        ] = {}
        # Per-procedural-block async-reset value accumulator: maps each
        # LHS bit tuple to the integer constant written in the reset
        # arm (``if (!rst_n) q <= 4'd5;`` → 5). Populated by
        # ``_collect_reset_assignments`` from the ifTrue arm and read
        # at drain time to fill the ``$adff`` ``ARST_VALUE`` parameter.
        # Missing entries default to 0 — that's the conservative match
        # for the typical ``q <= '0`` reset and keeps the netlist
        # valid when the reset RHS isn't a recognised literal.
        self._proc_resets: dict[tuple[Bit, ...], int] = {}

    # -- public ----------------------------------------------------------

    def build(self) -> Module:
        # The top instance's ports become the flat ``Module``'s ports;
        # child instances contribute flops / comb / netnames into the
        # same flat namespace, hierarchically prefixed (``u_b0.q``).
        self._walk_instance(self.top, hier_prefix="", bound_internals=frozenset())
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
                    self._kind_name(member) == "VariableSymbol"
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
        bits: tuple[Bit, ...] = tuple(
            range(self._next_bit_id, self._next_bit_id + width)
        )
        self._next_bit_id += width
        self._var_bits[var_sym] = bits
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

    # -- pass 1: variables ----------------------------------------------

    def _collect_variable(self, member: Any, hier_prefix: str = "") -> None:
        if self._kind_name(member) != "VariableSymbol":
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
        self._netnames[full_name] = Netname(name=full_name, bits=bits, attributes=attrs)

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
            if self._is_boundary_instance(member):
                self._emit_boundary_instance(member, hier_prefix)
            else:
                self._emit_child_instance(member, hier_prefix)
        elif kind == "NetSymbol":
            # ``wire foo = <expr>;`` lowers as a NetSymbol whose
            # ``initializer`` carries the RHS. pyslang doesn't emit a
            # separate ContinuousAssignSymbol for this shape, so without
            # this branch the inline initializer is silently dropped
            # — the rule pack then sees the net's bits driven by
            # nothing, which masks inter-stage comb hazards (CDC-014).
            self._emit_net_initializer(member)
        # GenerateBlock(Array)Symbol are pre-expanded by ``_iter_scope``
        # before reaching this dispatch; any other unmodelled kind
        # falls through silently — the missing flops surface in
        # ``analyze`` output, which is a better failure mode than
        # crashing on an unrecognised member kind.

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
        rhs_bits = self._bits_of_expression(init_expr)
        if rhs_bits is None:
            return
        canonical = self._canonical_var(member)
        # Allocate the net's bits if it's the first time we've seen it;
        # then merge them into the RHS bits via the same alias-rewrite
        # primitive ``_emit_continuous_assign`` uses, so any reader of
        # the net follows through to the driving cell's output.
        net_bits = self._alloc_bits(member)
        self._merge_canonical_var(canonical, net_bits, rhs_bits)

    def _emit_child_instance(self, child: Any, parent_prefix: str) -> None:
        """Inline a child instance's body into the flat ``Module``.

        Port connections are the load-bearing wiring step: the child's
        internal variables backing each port get their bits aliased to
        the parent's connection expression so net identity is preserved
        across the hierarchy boundary. After that, the child body is
        walked exactly like the top.
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
            self._var_bits[self._canonical_var(internal)] = parent_bits
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
            port_netname = f"{parent_prefix}{child.name}.{port.name}"
            self._netnames[port_netname] = Netname(
                name=port_netname, bits=parent_bits, attributes={}
            )

        child_prefix = f"{parent_prefix}{child.name}."
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
            return self._lvalue_bits(ex.left)
        if kind == "EmptyArgumentExpression":
            return None
        return self._bits_of_expression(ex)

    # -- blackbox boundaries (rtl-buddy-cdc#345) --------------------------

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

    def _emit_boundary_instance(self, inst: Any, parent_prefix: str) -> None:
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
        cell_name = f"{parent_prefix}{inst.name}"
        self._cells[cell_name] = Cell(
            name=cell_name,
            type=defn_name,
            connections=connections,
            parameters=parameters,
            attributes=attrs,
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
        self._proc_writes = {}
        self._proc_resets = {}
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
                data_branch = inner.ifFalse
                if data_branch is None:
                    return  # no else → no data assignment to model
                # Capture reset-arm literal RHS values before walking
                # the data arm — drain time looks these up to populate
                # ``$adff``'s ``ARST_VALUE`` / ``$sdff``'s ``SRST_VALUE``
                # parameter (issues #40 / #86).
                self._collect_reset_assignments(inner.ifTrue)
                self._emit_assignments_in(
                    data_branch,
                    clk_sym,
                    block_reset_sym,
                    block_reset_active_low,
                    src_node,
                )
            elif self._kind_name(inner) == "ExpressionStatement":
                self._emit_assignment_expression(
                    inner.expr,
                    clk_sym,
                    reset_sym=None,
                    reset_active_low=False,
                    src_node=src_node,
                )
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
            self._proc_writes = {}
            self._proc_resets = {}
            self._clk_negedge = prev_clk_negedge

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

    def _warn_dropped(self, block: Any, reason: str) -> None:
        """Report a procedural block this frontend cannot lower.

        Its flops / logic are missing from the netlist, so the run would
        otherwise come back clean on a design it never saw. Logged at
        ``WARNING`` (stderr) with the source location, like the frontend's
        other non-fatal diagnostics."""
        where = self._src_attr(block) or "<unknown location>"
        _log.warning(
            "slang frontend: %s: procedural block not lowered (%s); its "
            "registers and logic are missing from the CDC netlist",
            where,
            reason,
        )

    def _emit_plain_always(self, block: Any) -> None:
        """Lower a plain ``always`` block by what it infers.

        - every event edge-qualified (``@(posedge clk)``, ``@(posedge clk
          or negedge rst_n)``, ``negedge`` clocks) → the ``always_ff``
          lowering, async reset and enables included;
        - ``@*`` / ``@(*)`` or a level-sensitive list (``@(a or b)``) whose
          body assigns every target on every path → ``always_comb``. Like
          synthesis, the list itself is not checked for completeness;
        - level-sensitive with a target left unassigned on some path (an
          inferred latch) → the ``always_latch`` lowering when the body is
          the ``if (en) q = d;`` shape it models.

        Anything else (mixed edge / level lists, ``edge``, delays, event
        controls inside the body) is reported by :meth:`_warn_dropped`.
        Blocking and nonblocking writes are both accepted.
        """
        body = block.body
        form = self._plain_always_form(body)
        prev = self._plain_always
        self._plain_always = True
        try:
            if form == "ff":
                self._emit_always_ff(block)
                self._warn_blocking_temporaries(block, body.stmt)
            elif form == "level":
                written, always_written = self._assigned_symbols(body.stmt)
                if not (written - always_written):
                    self._walk_comb_statement(body.stmt)
                elif self._is_latch_shape(body.stmt):
                    self._walk_latch_statement(body.stmt)
                else:
                    self._warn_dropped(
                        block,
                        "level-sensitive `always` infers a latch in a shape "
                        "other than `if (en) q = d;`",
                    )
            else:
                self._warn_dropped(
                    block,
                    "`always` is not edge-triggered on every event, nor "
                    "`@*` / level-sensitive",
                )
        finally:
            self._plain_always = prev

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

    def _assigned_symbols(self, stmt: Any) -> tuple[set[Any], set[Any]]:
        """``(written, always_written)``: the variables ``stmt`` assigns
        on some path, and those it assigns on every path.

        A variable in the first set but not the second holds its value on
        some path, which is a latch. Tracked per variable, not per bit;
        a ``case`` without ``default`` counts as incomplete."""
        none: tuple[set[Any], set[Any]] = (set(), set())
        if stmt is None:
            return none
        kind = self._kind_name(stmt)
        if kind == "BlockStatement":
            return self._assigned_symbols(stmt.body)
        if kind in {"StatementList", "ListStatement"}:
            written: set[Any] = set()
            always: set[Any] = set()
            for s in getattr(stmt, "list", []) or []:
                w, a = self._assigned_symbols(s)
                written |= w
                always |= a
            return written, always
        if kind == "ExpressionStatement":
            expr = stmt.expr
            if type(expr).__name__ != "AssignmentExpression":
                return none
            syms = self._lvalue_symbols(expr.left)
            return set(syms), set(syms)
        if kind == "ConditionalStatement":
            conds = getattr(stmt, "conditions", None)
            const = self._const_int(conds[0].expr) if conds else None
            if const is not None:
                return self._assigned_symbols(
                    stmt.ifTrue if const != 0 else stmt.ifFalse
                )
            tw, ta = self._assigned_symbols(stmt.ifTrue)
            if stmt.ifFalse is None:
                return tw, set()
            fw, fa = self._assigned_symbols(stmt.ifFalse)
            return tw | fw, ta & fa
        if kind == "CaseStatement":
            arms = [
                self._assigned_symbols(getattr(it, "stmt", None))
                for it in (getattr(stmt, "items", []) or [])
            ]
            default = getattr(stmt, "defaultCase", None)
            written = set().union(*(w for w, _ in arms)) if arms else set()
            if default is None:
                return written, set()
            dw, da = self._assigned_symbols(default)
            always = set(da)
            for _, a in arms:
                always &= a
            return written | dw, always
        if kind == "ForLoopStatement":
            return self._assigned_symbols(getattr(stmt, "body", None))
        return none

    def _lvalue_symbols(self, expr: Any) -> list[Any]:
        """Root variable symbols an assignment target writes."""
        kind = type(expr).__name__
        if kind == "NamedValueExpression":
            return [self._canonical_var(expr.symbol)]
        if kind in {
            "ElementSelectExpression",
            "RangeSelectExpression",
            "MemberAccessExpression",
        }:
            return self._lvalue_symbols(getattr(expr, "value", None))
        if kind == "ConcatenationExpression":
            out: list[Any] = []
            for op in getattr(expr, "operands", []) or []:
                out.extend(self._lvalue_symbols(op))
            return out
        return []

    def _is_latch_shape(self, stmt: Any) -> bool:
        """Every statement is the single-arm ``if (en) lhs = rhs;`` shape
        :meth:`_walk_latch_statement` lowers to a ``$dlatch``."""
        kind = self._kind_name(stmt)
        if kind == "BlockStatement":
            return self._is_latch_shape(stmt.body)
        if kind in {"StatementList", "ListStatement"}:
            items = getattr(stmt, "list", []) or []
            return bool(items) and all(self._is_latch_shape(s) for s in items)
        if kind != "ConditionalStatement" or stmt.ifFalse is not None:
            return False
        arm = stmt.ifTrue
        while arm is not None and self._kind_name(arm) == "BlockStatement":
            arm = arm.body
        return (
            arm is not None
            and self._kind_name(arm) == "ExpressionStatement"
            and type(arm.expr).__name__ == "AssignmentExpression"
        )

    def _warn_blocking_temporaries(self, block: Any, stmt: Any) -> None:
        """Warn when an edge-triggered plain ``always`` reads a variable
        after writing it with a blocking ``=`` earlier in the same block.

        Each written variable lowers to a register, so such a read sees
        the registered value, where synthesis forwards the value written
        earlier in the block (``t = a; q = t;`` makes ``q`` a copy of
        ``a``, not of ``t``). The netlist can then show more flop stages
        than the hardware has. Reading before writing (``n = n + 1``)
        is exact and not reported."""
        written: set[Any] = set()
        late_reads: set[Any] = set()

        def reads(node: Any) -> set[Any]:
            found: set[Any] = set()

            def visit(n: Any) -> bool:
                if type(n).__name__ == "NamedValueExpression":
                    found.add(self._canonical_var(n.symbol))
                return True

            if node is not None:
                node.visit(visit)
            return found

        def walk(st: Any) -> None:
            if st is None:
                return
            kind = self._kind_name(st)
            if kind == "BlockStatement":
                walk(st.body)
            elif kind in {"StatementList", "ListStatement"}:
                for s in getattr(st, "list", []) or []:
                    walk(s)
            elif kind == "ExpressionStatement":
                expr = st.expr
                if type(expr).__name__ != "AssignmentExpression":
                    late_reads.update(reads(expr) & written)
                    return
                late_reads.update(reads(expr.right) & written)
                if not getattr(expr, "isNonBlocking", False):
                    written.update(self._lvalue_symbols(expr.left))
            elif kind == "ConditionalStatement":
                for cond in getattr(st, "conditions", []) or []:
                    late_reads.update(reads(cond.expr) & written)
                walk(st.ifTrue)
                walk(st.ifFalse)
            elif kind == "CaseStatement":
                late_reads.update(reads(getattr(st, "expr", None)) & written)
                for item in getattr(st, "items", []) or []:
                    walk(getattr(item, "stmt", None))
                walk(getattr(st, "defaultCase", None))
            elif kind == "ForLoopStatement":
                # Twice: a write in one iteration precedes the next one's reads.
                walk(getattr(st, "body", None))
                walk(getattr(st, "body", None))

        walk(stmt)
        if late_reads:
            _log.warning(
                "slang frontend: %s: blocking-assigned %s read later in the "
                "same edge-triggered `always`; modelled as register(s), so "
                "the read sees the registered value rather than the one "
                "written earlier in the block",
                self._src_attr(block) or "<unknown location>",
                ", ".join(f"`{getattr(s, 'name', '?')}`" for s in late_reads),
            )

    def _collect_reset_assignments(self, stmt: Any) -> None:
        """Walk an ``always_ff`` reset arm (``if (!rst_n) <stmt>``) and
        record per-LHS reset values into ``self._proc_resets``.

        Captures nonblocking assignments whose RHS is a compile-time
        constant. Anything else — non-literal RHS, struct/hierref LHS,
        nested conditionals — is silently skipped, and the drain-time
        lookup falls back to 0. The reset values feed ``$adff``'s
        ``ARST_VALUE`` parameter (issue #40); the rule pack doesn't
        read it today, so a default of 0 keeps the netlist valid even
        when this walker doesn't recognise the shape.
        """
        if stmt is None:
            return
        kind = self._kind_name(stmt)
        if kind == "BlockStatement":
            self._collect_reset_assignments(stmt.body)
            return
        if kind in {"StatementList", "ListStatement"}:
            for s in getattr(stmt, "list", []) or []:
                self._collect_reset_assignments(s)
            return
        if kind != "ExpressionStatement":
            return
        expr = stmt.expr
        if type(expr).__name__ != "AssignmentExpression":
            return
        if not getattr(expr, "isNonBlocking", False) and not self._plain_always:
            return
        q_bits = self._lvalue_bits(expr.left)
        if q_bits is None:
            return
        val = self._const_int(expr.right)
        if val is None:
            return
        self._proc_resets[q_bits] = val

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

    @staticmethod
    def _extract_condition_symbol(expr: Any) -> Any | None:
        """For an if-condition that's the reset check (``rst`` or
        ``!rst_n``), return the underlying VariableSymbol. Anything
        more elaborate (``rst && enable``, function calls, …) → None,
        and the caller falls back to the event-list guess."""
        # UnaryExpression(operand=NamedValueExpression)  → !rst form
        if type(expr).__name__ == "UnaryExpression":
            operand = getattr(expr, "operand", None)
            if operand is not None and type(operand).__name__ == "NamedValueExpression":
                return operand.symbol
        # NamedValueExpression directly  → positive-polarity reset
        if type(expr).__name__ == "NamedValueExpression":
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
            if not getattr(expr, "isNonBlocking", False) and not self._plain_always:
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
            self._emit_assignment_expression(
                statement.expr,
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

            # Dynamic condition: push the bit onto the enable stack
            # while walking each arm so ``_emit_assignment_expression``
            # accumulates writes with the right enable.
            # ``_drain_proc_writes`` then collapses all writes to one
            # flop per LHS with a mux tree:
            #
            # - ifTrue arm: push ``cond`` (positive polarity).
            # - ifFalse arm: push ``$not(cond)`` (negate via a $not
            #   cell) — symmetric with the ifTrue case, so an if/else
            #   that writes the same LHS in both arms produces a
            #   single flop whose D is ``mux(not_cond, mux(cond, Q, a), b)``
            #   (equivalent to ``mux(cond, b, a)`` after ``opt_clean``,
            #   but with the explicit hold-feedback shape the rule
            #   pack's ``_is_gated_bus_crossing`` matches).
            #
            # If the condition can't be lowered to a single bit
            # (multi-pattern match, etc.) fall back to walking both
            # arms unconditionally — same conservative policy as
            # before, but rare on real RTL.
            cond_bit = self._lower_condition_to_bit(statement.conditions)
            if cond_bit is None:
                for arm in (statement.ifTrue, statement.ifFalse):
                    if arm is not None:
                        self._emit_assignments_in(
                            arm, clk_sym, reset_sym, reset_active_low, src_node
                        )
                return
            self._enable_stack.append(cond_bit)
            try:
                self._emit_assignments_in(
                    statement.ifTrue,
                    clk_sym,
                    reset_sym,
                    reset_active_low,
                    src_node,
                )
            finally:
                self._enable_stack.pop()
            if statement.ifFalse is not None:
                not_cond = self._lower_not(cond_bit)
                self._enable_stack.append(not_cond)
                try:
                    self._emit_assignments_in(
                        statement.ifFalse,
                        clk_sym,
                        reset_sym,
                        reset_active_low,
                        src_node,
                    )
                finally:
                    self._enable_stack.pop()
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
            # Loop variable declarations are emitted as siblings of the
            # ForLoopStatement (``for (int i = ...; ...; ...)``). They
            # contribute no flops; the loopvar's initial value is
            # already on its VariableSymbol's ``.initializer``.
            return
        # Other statement kinds (function/task call, immediate assert,
        # event, timing control inside always_ff — atypical) fall
        # through silently; the missing flops surface in analyze
        # output and we file the next gap when it bites a real design.

    def _emit_case_statement(
        self,
        statement: Any,
        clk_sym: Any,
        reset_sym: Any | None,
        reset_active_low: bool,
        src_node: Any,
    ) -> None:
        """Walk a ``case`` body, emitting per-arm enables onto the
        enable stack so the deferred-emission drain can build a mux
        tree gated by each arm's ``case_expr == match`` equality.

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
          OR the per-match equalities together. The default arm's
          enable is ``$not($or(all explicit-match equalities))``.
          Each arm walks with its enable pushed onto
          ``_enable_stack``; the drain builds the corresponding
          ``$mux`` tree.
        - **Bail.** If the case-expr can't be lowered (kind we don't
          model on the RHS yet), walk every arm with no enable —
          same conservative policy as the pre-PR behaviour, so flops
          still emit; we just lose the gating-mux shape on this
          particular case.
        """
        case_expr = getattr(statement, "expr", None)
        items = list(getattr(statement, "items", []) or [])
        default_arm = getattr(statement, "defaultCase", None)

        # #84: compile-time-constant case-expr — short-circuit to the
        # matching arm. ``_const_int`` already folds parameter refs
        # and arithmetic, so ``case (MODE)`` with a parameter-bound
        # MODE resolves cleanly.
        case_const = self._const_int(case_expr)
        if case_const is not None:
            live_arm: Any = None
            for item in items:
                for match_expr in getattr(item, "expressions", []) or []:
                    match_const = self._const_int(match_expr)
                    if match_const is not None and match_const == case_const:
                        live_arm = getattr(item, "stmt", None)
                        break
                if live_arm is not None:
                    break
            if live_arm is None:
                live_arm = default_arm
            if live_arm is not None:
                self._emit_assignments_in(
                    live_arm, clk_sym, reset_sym, reset_active_low, src_node
                )
            return

        # #85: dynamic case-expr — build per-item enables.
        case_expr_bits = (
            self._bits_of_expression(case_expr) if case_expr is not None else None
        )
        if case_expr_bits is None:
            # Couldn't lower the case-expr. Conservative bail: walk
            # every item with no enable so writes still surface as
            # unconditional flops. Matches the pre-PR shape, so any
            # design that hit the old path still does.
            for item in items:
                self._emit_assignments_in(
                    getattr(item, "stmt", None),
                    clk_sym,
                    reset_sym,
                    reset_active_low,
                    src_node,
                )
            if default_arm is not None:
                self._emit_assignments_in(
                    default_arm, clk_sym, reset_sym, reset_active_low, src_node
                )
            return

        explicit_match_bits: list[Bit] = []
        for item in items:
            item_match_bits: list[Bit] = []
            for match_expr in getattr(item, "expressions", []) or []:
                match_bits = self._bits_of_expression(match_expr)
                if match_bits is None:
                    continue
                eq_y = self._alloc_anon_bits(1)
                eq_name = self._fresh_cell_name("$eq")
                self._cells[eq_name] = Cell(
                    name=eq_name,
                    type="$eq",
                    connections={
                        "A": case_expr_bits,
                        "B": match_bits,
                        "Y": eq_y,
                    },
                    parameters={},
                    attributes={},
                )
                item_match_bits.append(eq_y[0])
                explicit_match_bits.append(eq_y[0])
            item_enable = self._or_bits(item_match_bits)
            item_stmt = getattr(item, "stmt", None)
            if item_enable is None:
                # No lowerable match in this item (unmodelled pattern
                # shape). Walk without enable so the body's flops still
                # emit — same shape as the case-expr bail above.
                self._emit_assignments_in(
                    item_stmt, clk_sym, reset_sym, reset_active_low, src_node
                )
                continue
            self._enable_stack.append(item_enable)
            try:
                self._emit_assignments_in(
                    item_stmt, clk_sym, reset_sym, reset_active_low, src_node
                )
            finally:
                self._enable_stack.pop()

        if default_arm is not None:
            no_match = self._or_bits(explicit_match_bits)
            default_enable = self._lower_not(no_match) if no_match is not None else None
            if default_enable is None:
                self._emit_assignments_in(
                    default_arm, clk_sym, reset_sym, reset_active_low, src_node
                )
            else:
                self._enable_stack.append(default_enable)
                try:
                    self._emit_assignments_in(
                        default_arm, clk_sym, reset_sym, reset_active_low, src_node
                    )
                finally:
                    self._enable_stack.pop()

    def _emit_for_loop(
        self,
        stmt: Any,
        clk_sym: Any,
        reset_sym: Any | None,
        reset_active_low: bool,
        src_node: Any,
    ) -> None:
        """Virtually unroll a procedural ``for`` loop with compile-time
        constant bounds and walk the body once per iteration with the
        loop variable bound to the iteration value.

        Yosys' frontend unrolls these in elaboration; we have to do
        the same so element selects in the body (``chain[i]`` /
        ``chain[i-1]``) fold to per-iteration bits. Loops whose bounds
        can't be folded — runtime stop expression, non-trivial step,
        multi-variable header — fall through with no emit, matching
        the existing walker's "skip on unrecognised shape" policy.

        Hard iteration cap (1024) is defensive against pathological
        SV that would otherwise spin the unroller.
        """
        loop_vars = list(getattr(stmt, "loopVars", []) or [])
        if len(loop_vars) != 1:
            return
        loopvar = loop_vars[0]
        init_expr = getattr(loopvar, "initializer", None)
        start = self._const_int(init_expr)
        if start is None:
            return

        stop_expr = getattr(stmt, "stopExpr", None)
        if stop_expr is None or type(stop_expr).__name__ != "BinaryExpression":
            return
        stop_op = str(getattr(stop_expr, "op", "")).rsplit(".", 1)[-1]
        left = getattr(stop_expr, "left", None)
        if (
            type(left).__name__ != "NamedValueExpression"
            or getattr(left, "symbol", None) is not loopvar
        ):
            return
        stop_val = self._const_int(getattr(stop_expr, "right", None))
        if stop_val is None:
            return

        steps = list(getattr(stmt, "steps", []) or [])
        if len(steps) != 1:
            return
        step_expr = steps[0]
        step_kind = type(step_expr).__name__
        step_amount: int | None = None
        if step_kind == "UnaryExpression":
            op_name = str(getattr(step_expr, "op", "")).rsplit(".", 1)[-1]
            if op_name in ("Postincrement", "Preincrement"):
                step_amount = 1
            elif op_name in ("Postdecrement", "Predecrement"):
                step_amount = -1
        elif step_kind == "AssignmentExpression":
            # ``i += N`` / ``i -= N`` is the only compound shape we
            # support; pyslang desugars these to ``i = i op N`` and
            # stores the RHS as a BinaryExpression whose ``.left`` is
            # an ``LValueReferenceExpression`` (the read of ``i`` on
            # the right of the assignment) and whose ``.right`` is the
            # step constant. ``_const_int`` returns None on the
            # LValueReferenceExpression (it's not a NamedValueExpression
            # so the loop-binding lookup misses), which is what lets us
            # tell which operand is the constant.
            if not getattr(step_expr, "isCompound", False):
                return
            desugared = getattr(step_expr, "right", None)
            if desugared is None or type(desugared).__name__ != "BinaryExpression":
                return
            left_const = self._const_int(getattr(desugared, "left", None))
            right_const = self._const_int(getattr(desugared, "right", None))
            if left_const is None and right_const is not None:
                step_const = right_const
                lvalue_on_left = True
            elif right_const is None and left_const is not None:
                step_const = left_const
                lvalue_on_left = False
            else:
                return
            bin_op = str(getattr(desugared, "op", "")).rsplit(".", 1)[-1]
            if bin_op == "Add":
                step_amount = step_const
            elif bin_op == "Subtract" and lvalue_on_left:
                # ``i = i - N`` → i decreases by N
                step_amount = -step_const
            # Other compound shapes (``*=``, ``/=``, …) intentionally
            # fall through; they don't produce constant trip counts.
        if step_amount is None or step_amount == 0:
            return

        ITER_CAP = 1024

        def _still_iterating(v: int) -> bool:
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

        v = start
        count = 0
        while _still_iterating(v) and count < ITER_CAP:
            prev = self._loop_bindings.pop(loopvar, _SENTINEL)
            self._loop_bindings[loopvar] = v
            try:
                self._emit_assignments_in(
                    getattr(stmt, "body", None),
                    clk_sym,
                    reset_sym,
                    reset_active_low,
                    src_node,
                )
            finally:
                if prev is _SENTINEL:
                    self._loop_bindings.pop(loopvar, None)
                else:
                    self._loop_bindings[loopvar] = prev
            v += step_amount
            count += 1

    def _emit_assignment_expression(
        self,
        expr: Any,
        clk_sym: Any,
        reset_sym: Any | None,
        reset_active_low: bool,
        src_node: Any = None,
    ) -> None:
        """Accumulate one nonblocking write into ``_proc_writes``.

        Doesn't emit a flop cell — emission happens in
        ``_drain_proc_writes`` after the entire body has been walked,
        so multiple arms writing the same LHS collapse to one flop
        with a mux tree. Reset / clock info is captured at the
        procedural-block level (same for every write in the block);
        the parameters here are kept for signature symmetry with the
        pre-deferred-emission walker but only ``src_node`` is read.
        """
        del clk_sym, reset_sym, reset_active_low  # captured at block level
        if type(expr).__name__ != "AssignmentExpression":
            return
        if not getattr(expr, "isNonBlocking", False) and not self._plain_always:
            # Blocking inside always_ff is a SV style violation; skip
            # rather than try to model it. A plain ``always`` has no such
            # rule and synthesis registers both kinds (#345).
            return
        lhs = expr.left
        rhs = expr.right
        # Resolve the LHS to the subset of a variable's bits that the
        # flop's Q should drive. Supports bare ``NamedValueExpression``
        # (full width), ``ElementSelectExpression`` (single bit), and
        # ``RangeSelectExpression`` (a contiguous range). Other LHS
        # shapes (struct member, hierarchical reference, …) are skipped.
        q_bits = self._lvalue_bits(lhs)
        if q_bits is None:
            return
        d_bits = self._bits_of_expression(rhs)
        if d_bits is None:
            # RHS isn't a bare named value yet — we'd need primitive
            # lowering to model it. Emit a $_UNKNOWN_ driver so the
            # netlist isn't corrupt; the rule pack will treat the D
            # input as opaque (no upstream flops trace through it).
            d_bits = self._unknown_driver(width=len(q_bits))

        # Combine the active enable stack into a single bit (chain
        # $and cells). None when the stack is empty — the write is
        # unconditional in the current control-flow context.
        enable = self._combine_enable_stack()
        # Stash the write source for cell-attribute use at drain time.
        # We only need one src_node per LHS (the procedural block's
        # range); the per-assignment fallback in the old code was
        # rarely used.
        self._proc_writes.setdefault(q_bits, []).append((enable, d_bits))

    def _lower_not(self, bit: Bit) -> Bit:
        """Allocate a ``$not`` cell over ``bit`` and return the output
        bit. Used by ``ConditionalStatement`` to push the negation of
        a condition onto ``_enable_stack`` when entering an ifFalse
        arm — keeps the mux-tree drain symmetric across both arms.
        """
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
        """AND every entry on ``_enable_stack`` into a single bit, or
        return ``None`` if the stack is empty. Chains ``$and`` cells
        for multi-entry stacks; for a single-entry stack returns the
        bit directly with no cell emission."""
        if not self._enable_stack:
            return None
        if len(self._enable_stack) == 1:
            return self._enable_stack[0]
        select_bit: Bit = self._enable_stack[0]
        for next_bit in self._enable_stack[1:]:
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
    ) -> None:
        """Emit one flop per accumulated LHS.

        For each LHS, walks its ``(enable, rhs)`` writes in source
        order to build the flop's D input:

        - The initial "hold" value is the LHS's own Q bits — when no
          enable in any later write fires, D = Q (the flop holds).
        - An unconditional write (``enable is None``) replaces the
          current D entirely; later muxes apply on top.
        - A conditional write wraps D in a hold-feedback mux
          ``mux(S=enable, A=prev_D, B=rhs)`` so the rule pack's
          gated-bus detector can see the select signal.

        ``if (cond) q <= a; else q <= b;`` accumulates as
        ``[(cond, a), (not_cond, b)]`` and drains to
        ``D = mux(not_cond, mux(cond, Q, a), b)`` — equivalent to a
        Yosys ``$mux(cond, b, a)`` after ``opt_clean`` but with the
        explicit hold-feedback shape the detector recognises.

        ``reset_kind`` (``"async"`` / ``"sync"`` / ``None``) controls
        whether ``_emit_flop_cell`` materialises ``$adff``, ``$sdff``,
        or plain ``$dff``. ``None`` is also accepted alongside
        ``reset_sym=None`` for blocks with no reset.
        """
        for q_bits, writes in self._proc_writes.items():
            d_bits: tuple[Bit, ...] = q_bits  # initial Q-feedback
            for enable, rhs in writes:
                if enable is None:
                    d_bits = rhs
                else:
                    d_bits = self._build_hold_mux(enable, hold=d_bits, fire=rhs)
            reset_value = self._proc_resets.get(q_bits, 0)
            self._emit_flop_cell(
                clk_sym,
                reset_sym,
                reset_active_low,
                q_bits,
                d_bits,
                src_node,
                reset_value=reset_value,
                reset_kind=reset_kind,
            )

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
    ) -> None:
        """Allocate a ``$dff`` / ``$adff`` / ``$sdff`` cell with the
        given D and Q.

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
                connections["SRST"] = self._alloc_bits(reset_sym)
                cell_type = "$sdff"
            else:
                # ``"async"`` (or unspecified — pre-#86 default).
                connections["ARST"] = self._alloc_bits(reset_sym)
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
        if reset_sym is not None:
            polarity = _param_int32(0 if reset_active_low else 1)
            value_param = _param_bits(reset_value, width)
            if cell_type == "$sdff":
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
        # ``if (expr)`` treats the expression as a boolean. For a
        # single-bit signal that's already correct; for a multi-bit
        # signal SV semantics are "non-zero is true", but in practice
        # always_ff conditions in production RTL are single-bit
        # qualifiers. Bail when the width doesn't fit.
        if len(bits) != 1:
            return None
        return bits[0]

    def _alloc_anon_bits(self, width: int) -> tuple[Bit, ...]:
        """Allocate ``width`` fresh anonymous bits for a comb cell's
        output. Same allocator pool as named variables; no Symbol is
        registered because these aren't user-visible nets."""
        bits = tuple(range(self._next_bit_id, self._next_bit_id + width))
        self._next_bit_id += width
        return bits

    def _lvalue_bits(self, expr: Any) -> tuple[Bit, ...] | None:
        """Resolve an LHS expression to the variable-bit subset it
        writes."""
        kind = type(expr).__name__
        if kind == "NamedValueExpression":
            return self._alloc_bits(expr.symbol)
        if kind == "ElementSelectExpression":
            return self._element_select_bits(expr)
        if kind == "RangeSelectExpression":
            return self._range_select_bits(expr)
        return None

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
        if kind == "NamedValueExpression":
            sym = getattr(expr, "symbol", None)
            if sym is None:
                return None, (), 0, 0
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
            elem_type = getattr(inner_type, "elementType", None)
            new_stride = 1
            if elem_type is not None:
                new_stride = int(
                    getattr(elem_type, "selectableWidth", None)
                    or getattr(elem_type, "bitWidth", None)
                    or 1
                )
            new_offset = inner_offset + idx * new_stride
            return inner_sym, inner_bits, new_offset, new_stride
        return None, (), 0, 0

    def _range_select_bits(self, expr: Any) -> tuple[Bit, ...] | None:
        """``var[hi:lo]`` — return the contiguous bit subset.

        Yosys stores bits LSB-first, so ``var[3:0]`` returns
        ``var_bits[0:4]``. Non-constant ranges and indexed parts
        (``var[i+:N]``) return ``None``.
        """
        inner = expr.value
        if type(inner).__name__ != "NamedValueExpression":
            return None
        left = self._const_int(getattr(expr, "left", None))
        right = self._const_int(getattr(expr, "right", None))
        if left is None or right is None:
            return None
        lo, hi = min(left, right), max(left, right)
        var_bits = self._alloc_bits(inner.symbol)
        if lo < 0 or hi >= len(var_bits):
            return None
        return tuple(var_bits[lo : hi + 1])

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
        if kind == "NamedValueExpression":
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
            if op == "Add":
                return left + right
            if op == "Subtract":
                return left - right
            if op == "Multiply":
                return left * right
            if op == "Divide" and right != 0:
                return left // right
            if op == "Mod" and right != 0:
                return left % right
            return None
        if kind == "UnaryExpression":
            op = str(getattr(expr, "op", "")).rsplit(".", 1)[-1]
            operand = self._const_int(getattr(expr, "operand", None))
            if operand is None:
                return None
            if op == "Plus":
                return operand
            if op == "Minus":
                return -operand
            return None
        if kind == "ConversionExpression":
            return self._const_int(getattr(expr, "operand", None))

        return None

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

        Anything else (part-select, function call, struct member access,
        …) returns ``None``; the caller substitutes an ``$_UNKNOWN_``
        driver so the rule pack treats the upstream as opaque rather
        than crashing.
        """
        kind = type(expr).__name__
        if kind == "NamedValueExpression":
            return self._alloc_bits(expr.symbol)
        if kind == "ConversionExpression":
            inner = getattr(expr, "operand", None)
            if inner is not None:
                return self._bits_of_expression(inner)
        if kind == "IntegerLiteral":
            return self._bits_of_integer_literal(expr)
        if kind == "BinaryExpression":
            return self._lower_binary(expr)
        if kind == "UnaryExpression":
            return self._lower_unary(expr)
        if kind == "ConditionalExpression":
            return self._lower_conditional(expr)
        if kind == "ElementSelectExpression":
            return self._element_select_bits(expr)
        if kind == "RangeSelectExpression":
            return self._range_select_bits(expr)
        if kind == "ConcatenationExpression":
            return self._lower_concatenation(expr)
        if kind == "ReplicationExpression":
            return self._lower_replication(expr)
        return None

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
        "BitwiseNand": "$reduce_and",  # then invert; we don't model the
        # inversion explicitly — the rule
        # pack doesn't read the cell name
        # beyond category membership.
        "BitwiseNor": "$reduce_or",
        "BitwiseXnor": "$reduce_xor",
    }

    def _lower_binary(self, expr: Any) -> tuple[Bit, ...] | None:
        op_name = str(expr.op).rsplit(".", 1)[-1]
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
        return self._emit_comb_cell(
            cell_type, {"A": a_bits, "B": b_bits}, output_width=width, src_node=expr
        )

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
        return self._emit_comb_cell(
            cell_type, {"A": a_bits}, output_width=output_width, src_node=expr
        )

    def _lower_conditional(self, expr: Any) -> tuple[Bit, ...] | None:
        # pyslang ConditionalExpression has ``conditions`` (a list) and
        # ``left``/``right`` — the conditional-pattern grammar is more
        # general than ``cond ? a : b``, but for our purposes we only
        # need the single-condition shape.
        conds = getattr(expr, "conditions", None)
        if not conds:
            return None
        sel_bits = self._bits_of_expression(conds[0].expr)
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
            inputs={"A": false_bits, "B": true_bits, "S": sel_bits[:1]},
            output_width=width,
            src_node=expr,
        )

    def _bits_of_integer_literal(self, expr: Any) -> tuple[Bit, ...] | None:
        """Map an :class:`IntegerLiteral` to Yosys constant bits.

        Yosys represents constant bits as the strings ``"0"``, ``"1"``,
        ``"x"``, ``"z"``. The bit tuple is LSB-first to match Yosys
        write_json ordering.
        """
        # pyslang exposes the value via ``expr.value`` as an SVInt.
        # SVInt has a ``__int__`` for small concrete values; bigger
        # ones we'd need to walk per-bit. For our fixtures the literals
        # are 1-bit (``1'b0`` / ``1'b1``).
        val = getattr(expr, "value", None)
        if val is None:
            return None
        try:
            as_int = int(val)
        except (TypeError, ValueError):
            return None
        width = self._expr_width(expr) or 1
        # LSB-first per Yosys convention.
        return tuple("1" if (as_int >> i) & 1 else "0" for i in range(width))

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
    ) -> tuple[Bit, ...]:
        """Allocate a fresh Y net and emit a ``cell_type`` cell with the
        given inputs. Returns the Y bits so callers can wire them up.

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
            parameters={},
            attributes=attrs,
        )
        return y_bits

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
        """Walk an ``always_latch`` body and emit a ``$dlatch`` cell
        for each ``if (en) lhs = rhs;`` single-arm shape.

        Issue #39: pre-fix the slang frontend silently dropped every
        ``always_latch`` block, so the ICG enable-latch in the
        ``clock_gating`` fixture (and any other legitimate latch) had
        no driver in the resulting netlist. The latch is intentionally
        outside ``flops.FF_CELL_TYPES`` — latches don't bound clock
        domains and stay transparent to ``find_crossings``; making
        them visible is purely about netlist completeness so the rule
        pack and any future latch-aware rule see them.

        Only the single-arm ``if (cond) lhs = rhs;`` shape is
        modelled (the canonical pattern Yosys's ``proc_dlatch``
        infers). Multi-arm if/else, case, and explicit-`else` shapes
        fall through silently — they don't synthesise to a clean
        ``$dlatch`` either, so leaving them unmodelled mirrors the
        Yosys frontend's behaviour. ``$adlatch`` (latch with async
        reset) is explicitly out of scope per #39.
        """
        self._walk_latch_statement(block.body)

    def _walk_latch_statement(self, stmt: Any) -> None:
        if stmt is None:
            return
        kind = self._kind_name(stmt)
        if kind == "BlockStatement":
            self._walk_latch_statement(stmt.body)
            return
        if kind in {"StatementList", "ListStatement"}:
            for s in getattr(stmt, "list", []) or []:
                self._walk_latch_statement(s)
            return
        if kind != "ConditionalStatement":
            return
        en_bit = self._lower_condition_to_bit(getattr(stmt, "conditions", None))
        if en_bit is None:
            return
        # Drill through the optional BlockStatement on the ifTrue arm.
        arm = stmt.ifTrue
        while arm is not None and self._kind_name(arm) == "BlockStatement":
            arm = arm.body
        if arm is None or self._kind_name(arm) != "ExpressionStatement":
            return
        expr = arm.expr
        if type(expr).__name__ != "AssignmentExpression":
            return
        # Latch bodies should use blocking assigns (``=``); a
        # nonblocking write here would be a SV style violation that
        # we'd rather surface as "latch dropped" than silently model.
        if getattr(expr, "isNonBlocking", False) and not self._plain_always:
            return
        q_bits = self._lvalue_bits(expr.left)
        if q_bits is None:
            return
        d_bits = self._bits_of_expression(expr.right)
        if d_bits is None:
            d_bits = self._unknown_driver(width=len(q_bits))
        self._emit_dlatch_cell(en_bit, q_bits, d_bits, src_node=stmt)
        # An explicit ``else`` would write a second value and break
        # the single-arm-implicit-hold semantics — uncommon, and Yosys
        # ``proc_dlatch`` doesn't infer a clean ``$dlatch`` for it
        # either. Conservative skip; the analyzer surfaces the
        # missing driver if it bites.

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
        """Treat each blocking assignment in an ``always_comb`` block as
        a continuous assign of its RHS to its LHS.

        SV semantics already require an ``always_comb`` to be
        combinational, so the rewrite is semantically faithful (no
        priority encoding or latching to model). We descend through
        the typical ``BlockStatement(body=ExpressionStatement(...))``
        wrapper plus optional ``ListStatement`` for multi-assign
        bodies.
        """
        self._walk_comb_statement(block.body)

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
            self._alias_assign(stmt.expr)
            return
        if kind == "ConditionalStatement":
            self._walk_conditional_statement(stmt)
            return
        if kind == "CaseStatement":
            self._walk_case_statement(stmt)
            return
        # for/while loops etc. → ignore for now.

    def _walk_conditional_statement(self, stmt: Any) -> None:
        """Lower an ``always_comb`` ``if`` / ``else`` into a per-LHS
        ``$mux``.

        Strategy: snapshot the alias state, walk each branch
        independently, capture the per-branch result for every LHS
        that was written, then merge:

        - Written in **both** branches → emit ``$mux(S=cond, A=false,
          B=true)`` and alias the LHS to the mux output.
        - Written in **only one** branch → keep the single-branch
          aliasing. Per SV ``always_comb`` semantics the unwritten side
          is undefined, but the conservative choice (no mux) keeps the
          rule pack's data-cone walk honest — we don't pretend the
          condition gates the value when no real gate exists.

        Cells emitted while walking each branch (e.g. ``$and`` for a
        ``y = a & b`` body) are preserved across the snapshot/restore
        — only the alias maps are rolled back, so the bits returned
        by ``_bits_of_expression`` still resolve to a real Yosys cell.
        """
        conds = getattr(stmt, "conditions", None) or []
        sel_bits = (
            self._bits_of_expression(conds[0].expr)
            if conds and conds[0].expr is not None
            else None
        )
        if sel_bits is None:
            # Can't lower the selector → conservative descend. Matches
            # pre-mux behaviour: unconditional writes get aliased, any
            # branch-mismatched LHS picks "whichever wrote last".
            self._walk_comb_statement(stmt.ifTrue)
            if stmt.ifFalse is not None:
                self._walk_comb_statement(stmt.ifFalse)
            return

        base_var_bits = dict(self._var_bits)
        base_ports = dict(self._ports)
        base_netnames = dict(self._netnames)

        self._walk_comb_statement(stmt.ifTrue)
        true_var_bits = dict(self._var_bits)
        self._var_bits = dict(base_var_bits)
        self._ports = dict(base_ports)
        self._netnames = dict(base_netnames)

        if stmt.ifFalse is not None:
            self._walk_comb_statement(stmt.ifFalse)
            false_var_bits = dict(self._var_bits)
            self._var_bits = dict(base_var_bits)
            self._ports = dict(base_ports)
            self._netnames = dict(base_netnames)
        else:
            # No else → the false side is the prior value.
            false_var_bits = dict(base_var_bits)

        all_vars: set[Any] = set()
        for k, v in true_var_bits.items():
            if base_var_bits.get(k) != v:
                all_vars.add(k)
        for k, v in false_var_bits.items():
            if base_var_bits.get(k) != v:
                all_vars.add(k)

        for var in all_vars:
            base_v = base_var_bits.get(var)
            t = true_var_bits.get(var)
            f = false_var_bits.get(var)
            t_changed = t is not None and t != base_v
            f_changed = f is not None and f != base_v
            if t_changed and f_changed:
                assert t is not None and f is not None
                if t == f:
                    # Both branches assigned the same value — no mux
                    # needed, just alias to the common bits.
                    self._merge_canonical_var(var, base_v, t)
                    continue
                width = max(len(t), len(f))
                y_bits = self._emit_comb_cell(
                    cell_type="$mux",
                    inputs={"A": f, "B": t, "S": sel_bits[:1]},
                    output_width=width,
                    src_node=stmt,
                )
                self._merge_canonical_var(var, base_v, y_bits)
            elif t_changed:
                # Only the true branch wrote → keep single-branch
                # aliasing (issue #36 spec).
                assert t is not None
                self._merge_canonical_var(var, base_v, t)
            elif f_changed:
                assert f is not None
                self._merge_canonical_var(var, base_v, f)

    def _walk_case_statement(self, stmt: Any) -> None:
        """Lower an ``always_comb`` ``case`` into a chained ``$mux``
        per LHS, with item arms wrapped highest-priority-outermost.

        For each LHS, the chain starts at the ``default`` arm's
        contribution (or the prior value, if no default) and folds in
        each item arm that writes the LHS, in reverse order — item 0
        ends up as the outermost mux, matching SV's first-match-wins
        priority semantics. Selector bits for each arm are ``$eq``
        comparisons against ``stmt.expr``; multi-label arms OR the
        per-label matches together.

        LHS written in only one arm keeps single-arm aliasing (no
        mux), matching the conservative shape used by
        :meth:`_walk_conditional_statement`.
        """
        sel_bits = self._bits_of_expression(stmt.expr)
        items = list(getattr(stmt, "items", []) or [])
        default_arm = getattr(stmt, "defaultCase", None)
        if sel_bits is None or not items:
            for item in items:
                self._walk_comb_statement(getattr(item, "stmt", None))
            if default_arm is not None:
                self._walk_comb_statement(default_arm)
            return

        base_var_bits = dict(self._var_bits)
        base_ports = dict(self._ports)
        base_netnames = dict(self._netnames)

        def restore() -> None:
            self._var_bits = dict(base_var_bits)
            self._ports = dict(base_ports)
            self._netnames = dict(base_netnames)

        arms: list[tuple[list[Any], dict[Any, tuple[Bit, ...]]]] = []
        for item in items:
            self._walk_comb_statement(getattr(item, "stmt", None))
            arms.append(
                (list(getattr(item, "expressions", []) or []), dict(self._var_bits))
            )
            restore()

        if default_arm is not None:
            self._walk_comb_statement(default_arm)
            default_state = dict(self._var_bits)
            restore()
        else:
            default_state = dict(base_var_bits)

        def writes(state: dict[Any, tuple[Bit, ...]]) -> set[Any]:
            return {k for k, v in state.items() if base_var_bits.get(k) != v}

        arm_writes = [writes(s) for _, s in arms]
        default_writes = writes(default_state)
        all_written: set[Any] = set(default_writes)
        for w in arm_writes:
            all_written |= w

        for var in all_written:
            base_v = base_var_bits.get(var)
            writer_indices = [i for i, w in enumerate(arm_writes) if var in w]
            default_has = var in default_writes
            total_writers = len(writer_indices) + (1 if default_has else 0)

            if total_writers == 1:
                # Only one arm (or only default) writes → single-branch
                # aliasing.
                if writer_indices:
                    self._merge_canonical_var(
                        var, base_v, arms[writer_indices[0]][1][var]
                    )
                else:
                    self._merge_canonical_var(var, base_v, default_state[var])
                continue

            # 2+ writers → build a chained $mux, default (or prior
            # value) as the innermost fallback, item-arm 0 outermost.
            chain = default_state.get(var) if default_has else base_v
            if chain is None:
                continue
            for i in range(len(arms) - 1, -1, -1):
                if var not in arm_writes[i]:
                    continue
                label_exprs, arm_state = arms[i]
                arm_bits = arm_state[var]
                sel_match = self._lower_case_arm_selector(sel_bits, label_exprs, stmt)
                if sel_match is None:
                    continue
                width = max(len(arm_bits), len(chain))
                chain = self._emit_comb_cell(
                    cell_type="$mux",
                    inputs={"A": chain, "B": arm_bits, "S": sel_match[:1]},
                    output_width=width,
                    src_node=stmt,
                )
            self._merge_canonical_var(var, base_v, chain)

    def _lower_case_arm_selector(
        self,
        sel_bits: tuple[Bit, ...],
        label_exprs: list[Any],
        src_node: Any,
    ) -> tuple[Bit, ...] | None:
        """Build the 1-bit ``$mux.S`` selector for a single case arm:
        OR of ``(case_expr == label_i)`` ``$eq`` cells.

        Returns ``None`` if none of the labels could be lowered, so
        the caller can drop the arm rather than emit a broken mux.
        """
        sel_match: tuple[Bit, ...] | None = None
        for label_expr in label_exprs:
            lb = self._bits_of_expression(label_expr)
            if lb is None:
                continue
            eq_y = self._emit_comb_cell(
                cell_type="$eq",
                inputs={"A": sel_bits, "B": lb},
                output_width=1,
                src_node=src_node,
            )
            if sel_match is None:
                sel_match = eq_y
            else:
                sel_match = self._emit_comb_cell(
                    cell_type="$or",
                    inputs={"A": sel_match, "B": eq_y},
                    output_width=1,
                    src_node=src_node,
                )
        return sel_match

    def _alias_assign(self, expr: Any) -> None:
        """For a blocking assignment ``lhs = rhs``, rewrite ``lhs``'s
        bits to point at ``rhs``'s lowered bits — same aliasing trick
        the continuous-assign path uses."""
        if type(expr).__name__ != "AssignmentExpression":
            return
        if getattr(expr, "isNonBlocking", False) and not self._plain_always:
            return  # nonblocking in always_comb is a SV style violation
        lhs_kind = type(expr.left).__name__
        if lhs_kind == "ElementSelectExpression":
            self._alias_indexed_assign(expr.left, expr.right)
            return
        if lhs_kind != "NamedValueExpression":
            return
        rhs_bits = self._bits_of_expression(expr.right)
        if rhs_bits is None:
            return
        self._rewrite_aliased(expr.left, rhs_bits)

    def _alias_indexed_assign(self, lhs: Any, rhs: Any) -> None:
        """Lower ``base[idx] = rhs;`` in an ``always_comb`` body.

        Models the SV semantics ``base = base | (rhs << idx)`` (1-bit
        rhs) — set bit ``idx`` of ``base`` to ``rhs`` while preserving
        every other bit. When the prior alias of ``base`` is the
        all-zero constant (the canonical ``base = '0; base[idx] = 1;``
        decoder pattern from CDC-019), the ``base | …`` term collapses
        to just ``rhs << idx`` and we emit a single ``$shl`` cell;
        otherwise we OR the shift result onto the previous bits.

        Closes the CDC-019 leg of the cross-frontend parity gap from
        rtl-buddy-cdc#221 / #224. Yosys lowers the same pattern to a
        ``$shift`` cell whose Y-bus drives every downstream flop; the
        rule pack's one-hot-decode detector keys off "comb cell with
        ≥2 Y-bits driving N≥2 src flops in the same dst clock," so
        any multi-output comb cell with the right fan-out matches —
        ``$shl`` and ``$shift`` are equivalent here.

        Indexed-write shapes we *don't* model (silently ignored):

        - LHS chain (``base[a].field[b] = …``); only the simple
          ``NamedValue[idx]`` form is recognised.
        - Range selects (``base[3:1] = …``); the rule pack's CDC-019
          detector keys off per-bit fan-out, so a range write would
          need a different lowering shape.
        - RHS wider than 1 bit; the shift-based encoding assumes a
          single-bit indexed write (the only shape the corpus
          templates exercise).
        """
        base = getattr(lhs, "value", None)
        if base is None or type(base).__name__ != "NamedValueExpression":
            return
        base_sym = base.symbol
        canonical = self._canonical_var(base_sym)
        prev_bits = self._var_bits.get(canonical)
        if prev_bits is None:
            prev_bits = self._alloc_bits(base_sym)
        width = len(prev_bits)
        if width <= 1:
            return  # nothing to spread across; not a CDC-019 shape

        selector = getattr(lhs, "selector", None)
        if selector is None:
            return
        idx_bits = self._bits_of_expression(selector)
        if idx_bits is None:
            return

        rhs_bits = self._bits_of_expression(rhs)
        if rhs_bits is None or len(rhs_bits) == 0:
            return
        # Zero-extend rhs to base width so the shift operand and the
        # ``base | shl(rhs, idx)`` widths line up.
        a_bits: tuple[Bit, ...] = rhs_bits + ("0",) * (width - len(rhs_bits))
        a_bits = a_bits[:width]
        shl_y = self._emit_comb_cell(
            cell_type="$shl",
            inputs={"A": a_bits, "B": idx_bits},
            output_width=width,
            src_node=lhs,
        )

        # If the previous alias is the all-zero constant (canonical
        # ``base = '0; base[idx] = 1;`` pattern), the shift result is
        # the new value directly. Otherwise OR with the old bits.
        if all(b == "0" for b in prev_bits):
            new_bits = shl_y
        else:
            new_bits = self._emit_comb_cell(
                cell_type="$or",
                inputs={"A": prev_bits, "B": shl_y},
                output_width=width,
                src_node=lhs,
            )
        self._merge_canonical_var(canonical, prev_bits, new_bits)

    # -- continuous assigns ---------------------------------------------

    def _emit_continuous_assign(self, member: Any) -> None:
        a = getattr(member, "assignment", None)
        if a is None or type(a).__name__ != "AssignmentExpression":
            return
        lhs, rhs = a.left, a.right
        if type(lhs).__name__ != "NamedValueExpression":
            return
        rhs_bits = self._bits_of_expression(rhs)
        if rhs_bits is None:
            # Comb-driven port — primitive lowering will handle this.
            return
        # Aliasing trick: rewrite the LHS variable's bits to point at
        # the RHS's bits so any reader of the LHS sees the RHS source
        # directly. This matches Yosys' post-opt_clean output for
        # ``assign out = sig`` and means we don't need a $buf cell.
        self._rewrite_aliased(lhs, rhs_bits)

    def _rewrite_aliased(self, lhs: Any, rhs_bits: tuple[Bit, ...]) -> None:
        """Make every alias that currently holds ``lhs``'s bits point at
        ``rhs_bits`` instead.

        Crossing the hierarchy boundary, a single net (the parent's
        ``a_q`` wire, say) is represented by several aliased entries:
        the parent's :class:`VariableSymbol`, the child instance's
        :class:`PortSymbol`-internal variable, and any local wires
        that capture it. When ``assign a_q = q`` runs inside the child,
        every one of those aliases needs to follow — not just the
        child's own a_q entry — or the parent's reader sees stale bits.

        Thin shim over :meth:`_merge_canonical_var`; the
        canonical-keyed variant is the one used by branch-merge sites
        like :meth:`_walk_conditional_statement` that don't have a
        live LHS expression to read ``.symbol`` from.
        """
        canonical = self._canonical_var(lhs.symbol)
        self._merge_canonical_var(canonical, self._var_bits.get(canonical), rhs_bits)

    def _merge_canonical_var(
        self,
        canonical: Any,
        old_bits: tuple[Bit, ...] | None,
        new_bits: tuple[Bit, ...],
    ) -> None:
        """Alias-propagation primitive: rewrite every map entry whose
        bits equal ``old_bits`` to point at ``new_bits``.

        We do the cheap thing: scan ``_var_bits`` / ``_ports`` /
        ``_netnames`` for entries whose bits equal the old tuple and
        rewrite them. O(n) per assign, fine for the design sizes the
        analyzer targets.
        """
        self._var_bits[canonical] = new_bits
        if old_bits is None or old_bits == new_bits:
            return
        for sym, bits in list(self._var_bits.items()):
            if bits == old_bits:
                self._var_bits[sym] = new_bits
        for name, port in list(self._ports.items()):
            if port.bits == old_bits:
                self._ports[name] = Port(
                    name=port.name, direction=port.direction, bits=new_bits
                )
        for name, nn in list(self._netnames.items()):
            if nn.bits == old_bits:
                self._netnames[name] = Netname(
                    name=nn.name, bits=new_bits, attributes=nn.attributes
                )
