"""Yosys frontend: shell out to ``yosys`` to elaborate + flatten, then
load the resulting JSON via :func:`netlist.load`.

Centralises the Yosys invocation that the ``lint`` CLI command used to
inline. The CLI command now goes through :func:`rtl_buddy_cdc.frontend.elaborate`,
which dispatches here when the ``yosys`` frontend is selected.

The :func:`rtl_buddy_cdc.cli.analyze` command path remains unchanged —
it loads a pre-produced JSON directly via :func:`netlist.load`; this
module is only used when the caller starts from SV sources.
"""

from __future__ import annotations

import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path

from rtl_buddy_cdc import netlist
from rtl_buddy_cdc.netlist import Module


class YosysError(RuntimeError):
    """Yosys binary was missing or elaboration failed."""


def elaborate(
    sources: list[Path],
    top: str,
    *,
    yosys_bin: str | None = None,
    keep_json: Path | None = None,
    plugin_path: str | None = None,
    single_unit: bool = False,
    blackbox: list[str] | None = None,
    greybox: list[str] | None = None,
) -> Module:
    """Run yosys to produce a flattened netlist, returning the top module.

    Back-compat single-return entry point; drops any blackbox sibling
    modules. Use :func:`elaborate_with_blackboxes` to receive them (the
    lint path does, so a ``--blackbox`` subtree auto-abstracts).
    """
    module, _blackboxes = elaborate_with_blackboxes(
        sources,
        top,
        yosys_bin=yosys_bin,
        keep_json=keep_json,
        plugin_path=plugin_path,
        single_unit=single_unit,
        blackbox=blackbox,
        greybox=greybox,
    )
    return module


def elaborate_with_blackboxes(
    sources: list[Path],
    top: str,
    *,
    yosys_bin: str | None = None,
    keep_json: Path | None = None,
    plugin_path: str | None = None,
    single_unit: bool = False,
    blackbox: list[str] | None = None,
    greybox: list[str] | None = None,
) -> tuple[Module, dict[str, Module]]:
    """Run yosys to produce a flattened netlist JSON, then load it.

    ``keep_json``, if set, copies the intermediate JSON to that path
    before the temp file is deleted — useful for debugging or for
    re-running ``analyze`` against the same netlist.

    ``plugin_path``, if set, loads a Yosys plugin (typically
    ``yosys-slang``'s ``slang.so``) and elaborates via ``read_slang``
    instead of ``read_verilog``. This is required for designs that use
    SystemVerilog-2017 constructs (e.g. ``import pkg::*``) that Yosys's
    built-in frontend rejects.

    ``single_unit`` makes ``read_slang`` compile all sources together, sharing
    preprocessor definitions. It is useful for legacy filelists that define a
    macro in one source and consume it in a later source.

    ``blackbox``, if set, names modules to treat as CDC boundary cells:
    each becomes a ``--blackboxed-module <name>`` flag on the
    ``read_slang`` line. The listed module survives ``flatten`` with its
    real name and an ``attributes.blackbox`` flag (P1 prep probe), so
    ``netlist.load`` keys on the attribute — no rename-to-``$`` pass. The
    boundary cell carries port directions and zero internals; the parent
    keeps the instance as an ordinary cell of that ``type``.
    ``--blackboxed-module`` is a ``read_slang`` option, so blackboxing
    requires the slang plugin path; requesting it without a plugin is an
    error.

    ``greybox``, if set, names modules to keep as CDC boundary cells
    **with their internals intact** (#261). Implemented as
    ``setattr -mod -set blackbox 1 <module>`` between ``proc`` and
    ``flatten``: the attribute makes ``flatten`` leave the module standing
    (so the parent keeps an ordinary instance cell and ``load_with_black
    boxes`` picks it up as a boundary sibling) while its cells survive, so
    :mod:`rtl_buddy_cdc.compositional` can analyse the module ONCE on its
    own internals and lift reconvergence / sync-depth / gray facts back
    into the boundary. This is the difference between a boundary that
    merely scales and one that scales *without* trading away coverage.

    Unlike ``--blackbox`` this is a plain Yosys pass, so it needs no
    plugin and works with the built-in ``read_verilog`` frontend. Naming
    the same module in both is contradictory — the ``read_slang`` stub
    would win and there would be no internals to analyse — and is
    rejected.
    """
    yosys = yosys_bin or shutil.which("yosys")
    if yosys is None or not Path(yosys).exists():
        raise YosysError("yosys not found on PATH (use --yosys to override)")

    if plugin_path is not None and not Path(plugin_path).exists():
        # Report the absolute path the CLI resolved (see #245): a bare
        # relative string is unactionable when the caller's cwd is not
        # where they think it is.
        raise YosysError(f"yosys plugin not found: {Path(plugin_path).resolve()}")

    overlap = sorted(set(blackbox or ()) & set(greybox or ()))
    if overlap:
        raise YosysError(
            "--blackbox and --greybox name the same module(s): "
            f"{', '.join(overlap)}. --blackbox discards the body (nothing "
            "left to analyse); --greybox keeps it so the subtree can be "
            "analysed once on its own internals. Pick one."
        )
    if blackbox and plugin_path is None:
        raise YosysError(
            "--blackbox requires the yosys-slang plugin "
            "(--yosys-plugin / RTL_BUDDY_SLANG_PLUGIN): blackboxing is a "
            "read_slang feature, the built-in read_verilog frontend has no "
            "equivalent"
        )
    if single_unit and plugin_path is None:
        raise YosysError(
            "--single-unit requires the yosys-slang plugin "
            "(--yosys-plugin / RTL_BUDDY_SLANG_PLUGIN)"
        )

    tmp_json = Path(tempfile.mkstemp(suffix=".json", prefix="rtl-buddy-cdc-")[1])
    try:
        srcs = " ".join(shlex.quote(str(s)) for s in sources)
        if plugin_path is None:
            read_cmd = f"read_verilog -sv {srcs}"
        else:
            bb = "".join(
                f"--blackboxed-module {shlex.quote(m)} " for m in (blackbox or ())
            )
            # ``--allow-use-before-declare`` relaxes a yosys-slang default
            # that is too strict for ordinary RTL: a module-item reference
            # to a net declared later in the same module is valid SV the
            # built-in ``read_verilog`` frontend already accepts, so the
            # read_slang frontend should not newly reject it. (Top-level
            # SystemVerilog interface ports are a separate matter — yosys
            # netlists can't represent them, so ``--allow-toplevel-iface-
            # ports`` is *unsupported* by yosys-slang; designs with an
            # interface at the lint top must use the pyslang ``--frontend
            # slang`` path, which elaborates them natively.)
            read_cmd = (
                f"plugin -i {shlex.quote(plugin_path)}; "
                f"read_slang --std 1800-2017 --top {shlex.quote(top)} "
                f"{'--single-unit ' if single_unit else ''}"
                f"--allow-use-before-declare "
                f"{bb}{srcs}"
            )
        # ``setattr -mod -set blackbox 1`` must land AFTER ``proc`` (the
        # module still needs its processes lowered to cells — those cells
        # are the whole point) and BEFORE ``flatten``, which is the pass
        # that honours the attribute by leaving the module standing.
        grey = "".join(
            f"setattr -mod -set blackbox 1 {shlex.quote(m)}; " for m in (greybox or ())
        )
        script = (
            f"{read_cmd}; "
            f"hierarchy -top {shlex.quote(top)}; "
            f"proc; {grey}flatten; opt_clean; "
            f"write_json {shlex.quote(str(tmp_json))}"
        )
        proc = subprocess.run(
            [yosys, "-q", "-p", script],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            # Surface both streams; yosys writes the actually-useful
            # error to stderr but legacy builds split it across both.
            msg = "yosys elaboration failed"
            if proc.stderr.strip():
                msg += f": {proc.stderr.strip()}"
            if proc.stdout.strip():
                msg += f"\n{proc.stdout.strip()}"
            raise YosysError(msg)

        module, blackboxes = netlist.load_with_blackboxes(tmp_json)
        if keep_json is not None:
            shutil.copy(tmp_json, keep_json)
        return module, blackboxes
    finally:
        try:
            tmp_json.unlink()
        except FileNotFoundError:
            pass
