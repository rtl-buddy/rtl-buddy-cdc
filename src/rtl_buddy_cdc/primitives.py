"""Sanctioned CDC synchroniser primitives, recognised by module name.

Issue #275. Real FPGA designs rarely hand-roll a 2FF chain — they
instantiate a vendor CDC macro. The Xilinx **XPM CDC** library
(``xpm_cdc_single``, ``xpm_cdc_gray``, ``xpm_cdc_handshake``, …; UG974)
is the dominant case. Its sources ship inside the vendor install tree,
so a filelist assembled from project RTL carries only the
*instantiation*: the analyzer sees a bodyless, dual-clock blackbox.

Without recognition that blackbox is "not provably single-clock", so
:func:`~rtl_buddy_cdc.abstract.summarise_subtree` declines it, the CLI
emits a ``CDC-BBX`` error per instance, and — worse — the crossing
through the macro vanishes entirely (a declined instance seeds neither
a boundary source nor a boundary sink). This module is the name-based
recogniser that fixes both.

**Why by name rather than by elaborating the macro's internals.** The
name is the contract: ``xpm_cdc_*`` is a fixed, documented, versioned
library whose port naming is rigid — every data/control port is
``src_*`` or ``dest_*`` and every clock pin is ``src_clk`` /
``dest_clk``. That regularity is enough to summarise the macro
correctly without seeing one line of its body, which matters because
the body is usually *not available*. Users who do put the XPM sources
in their filelist are served by a separate, zero-XPM-code path: XPM
tags its internal stages ``(* ASYNC_REG = "TRUE" *)``, and
``USER_SYNC_ATTRS`` matches attribute names case-insensitively.

Everything here is pure data + pure functions; the summariser that
consumes it lives in :mod:`rtl_buddy_cdc.abstract` and the
orchestration in :mod:`rtl_buddy_cdc.hierarchy`.
"""

from __future__ import annotations

from rtl_buddy_cdc.netlist import Cell

# The Xilinx XPM CDC macro family (UG974). Recognised as synchronisers:
# a crossing landing in one of these is safe by construction, and the
# instance is summarised at its ``dest_clk`` domain rather than declined
# as a multi-clock blackbox.
XPM_CDC_MODULES: frozenset[str] = frozenset(
    {
        "xpm_cdc_single",
        "xpm_cdc_array_single",
        "xpm_cdc_gray",
        "xpm_cdc_handshake",
        "xpm_cdc_pulse",
        "xpm_cdc_sync_rst",
        "xpm_cdc_async_rst",
    }
)

# Per-module synchroniser-depth parameters (the ``--sync-depth`` analog
# living *inside* the macro, invisible to CDC-002). Every XPM CDC macro
# carries ``DEST_SYNC_FF``; only ``xpm_cdc_handshake`` also carries a
# source-side ``SRC_SYNC_FF`` for the returning ack path. Consumed by
# CDC-022.
XPM_DEPTH_PARAMS: dict[str, tuple[str, ...]] = {
    "xpm_cdc_single": ("DEST_SYNC_FF",),
    "xpm_cdc_array_single": ("DEST_SYNC_FF",),
    "xpm_cdc_gray": ("DEST_SYNC_FF",),
    "xpm_cdc_handshake": ("DEST_SYNC_FF", "SRC_SYNC_FF"),
    "xpm_cdc_pulse": ("DEST_SYNC_FF",),
    "xpm_cdc_sync_rst": ("DEST_SYNC_FF",),
    "xpm_cdc_async_rst": ("DEST_SYNC_FF",),
}

# UG974's default for every XPM CDC depth parameter. An instantiation
# that does not override the parameter gets no entry in the Yosys cell's
# ``parameters`` map, so CDC-022 falls back to this documented default
# rather than staying silent. Only applied to XPM modules — a
# user-registered primitive (``--sync-primitive``) has no known default,
# so an absent parameter there is simply not checked.
XPM_DEFAULT_SYNC_FF = 4

# Depth parameter assumed for a user-registered (non-XPM) primitive.
_EXTRA_DEPTH_PARAMS: tuple[str, ...] = ("DEST_SYNC_FF",)

# Substrings that classify a recognised primitive's clock pin as the
# destination / source side. XPM spells them ``dest_clk`` / ``src_clk``;
# the hints are broadened slightly so a site-registered macro using
# ``dst_clk`` is classified too.
_DEST_HINTS: tuple[str, ...] = ("dest", "dst")
_SRC_HINTS: tuple[str, ...] = ("src",)


def normalise_type(module_type: str) -> str:
    """Strip Yosys decoration from a cell ``type`` to the plain module name.

    Yosys writes escaped identifiers with a leading backslash and derives
    a parameterised module into ``$paramod\\<name>\\<PARAM>=<value>...``
    (or ``$paramod$<hash>\\<name>\\...``). Both forms have to resolve back
    to ``xpm_cdc_single`` for the registry lookup to work.
    """
    if module_type.startswith("$paramod"):
        parts = module_type.split("\\")
        if len(parts) >= 2:
            module_type = parts[1]
    return module_type.lstrip("\\")


def is_xpm_primitive(module_type: str) -> bool:
    """True iff ``module_type`` names a module in the XPM CDC family."""
    return normalise_type(module_type) in XPM_CDC_MODULES


def is_sync_primitive(module_type: str, extra: frozenset[str] = frozenset()) -> bool:
    """True iff ``module_type`` is a recognised CDC synchroniser primitive.

    ``extra`` carries site-registered module names from the repeatable
    ``--sync-primitive`` CLI option — an in-house or other-vendor CDC
    macro gets the same treatment as XPM, minus the XPM port-naming
    convention (see :func:`port_domain`).
    """
    name = normalise_type(module_type)
    return name in XPM_CDC_MODULES or name in extra


def port_domain(module_type: str, port: str) -> str:
    """Which side of the macro drives ``port`` — ``"src"`` or ``"dest"``.

    XPM's naming is rigid, so an output named ``src_*`` (only
    ``xpm_cdc_handshake.src_rcv``) is driven in the *source* domain while
    everything else leaves in the destination domain. A user-registered
    primitive gets no such promise: all of its outputs are attributed to
    the destination clock, the conservative reading (its outputs are then
    checked against every other domain they reach).
    """
    if is_xpm_primitive(module_type) and normalise_type(port).startswith("src"):
        return "src"
    return "dest"


def clock_pin_role(port: str) -> str | None:
    """Classify a clock pin name as ``"dest"`` / ``"src"``, else ``None``."""
    low = port.lower()
    if any(h in low for h in _DEST_HINTS):
        return "dest"
    if any(h in low for h in _SRC_HINTS):
        return "src"
    return None


def parse_param_int(raw: object) -> int | None:
    """Best-effort integer read of a Yosys JSON parameter value.

    ``write_json`` emits integer parameters as fixed-width bit-vector
    strings (``"00000000000000000000000000000100"``); hand-built netlists
    and some Yosys options emit a decimal string or a real int. Anything
    else (a string parameter, an ``x``-carrying vector) reads as ``None``
    and the caller skips the check.

    Bit-vector spelling wins over decimal for all-``0``/``1`` strings
    longer than one character — that is what Yosys actually produces, and
    treating ``"100"`` as decimal 100 would be far more wrong than
    treating a genuinely-decimal ``"100"`` as 4.
    """
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    if not s:
        return None
    if len(s) > 1 and set(s) <= {"0", "1"}:
        return int(s, 2)
    try:
        return int(s, 10)
    except ValueError:
        return None


def depth_params(
    module_type: str, extra: frozenset[str] = frozenset()
) -> tuple[str, ...]:
    """The synchroniser-depth parameter names ``module_type`` declares."""
    name = normalise_type(module_type)
    if name in XPM_DEPTH_PARAMS:
        return XPM_DEPTH_PARAMS[name]
    if name in extra:
        return _EXTRA_DEPTH_PARAMS
    return ()


def sync_depths(cell: Cell, extra: frozenset[str] = frozenset()) -> dict[str, int]:
    """Declared synchroniser depth per depth-parameter on ``cell``.

    Reads the *instance*'s parameter overrides, falling back to
    :data:`XPM_DEFAULT_SYNC_FF` for an XPM macro that left the parameter
    at its default (Yosys records only overridden parameters on the
    cell). Returns an empty map for a cell that is not a recognised
    primitive, or whose parameter value doesn't read as an integer.
    """
    out: dict[str, int] = {}
    is_xpm = is_xpm_primitive(cell.type)
    for param in depth_params(cell.type, extra):
        if param in cell.parameters:
            value = parse_param_int(cell.parameters[param])
            if value is not None:
                out[param] = value
        elif is_xpm:
            out[param] = XPM_DEFAULT_SYNC_FF
    return out
