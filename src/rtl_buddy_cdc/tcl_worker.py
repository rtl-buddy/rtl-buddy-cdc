"""Out-of-process Tcl safe-interp reader for SDC (rtl-buddy-cdc#298).

Run as ``python -m rtl_buddy_cdc.tcl_worker``. Reads **one** JSON
request on stdin and writes **one** JSON response on stdout::

    request   {"text": "<sdc source>",
               "command_limit": 1000000,
               "time_limit_seconds": 30}

    response  {"ok": true,
               "patchlevel": "9.0.3",
               "commands": [{"name": "create_clock",
                             "words": ["create_clock", "-name", "clk"],
                             "line": 1}, ...],
               "partial_warnings": []}

              {"ok": false,
               "kind": "tcl_error" | "resource_limit" | "unavailable",
               "error": "..."}

``kind`` tells :mod:`rtl_buddy_cdc.sdc` which exception to raise:
``resource_limit`` → ``TclResourceLimitError``, anything else →
``TclReadError``. A malformed request is *not* a response — the worker
writes a diagnostic to stderr and exits non-zero, which the parent also
treats as a read error.

WHY THIS RUNS IN ITS OWN PROCESS
================================

**Do not move the interpreter back in-process. This is not a style
choice — in-process Tcl deadlocks rb-cdc on macOS.**

Importing ``_tkinter`` starts Tcl's ``NotifierThreadProc``, a native
thread that lives in ``select()`` for the rest of the process and is
never joined or stopped (there is no public API to retire it). A
``subprocess.Popen`` from a process carrying that thread is a
``fork()`` followed by ``exec()``; between the two, the child runs
single-threaded in an address space forked from a multi-threaded
parent, where only async-signal-safe calls are legal. CPython's
``child_exec`` calls ``_close_open_fds_maybe_unsafe`` there, and on
macOS a ``close()`` in that window can enter uninterruptible kernel
state and never return. The child then never reaches ``exec``, never
writes the errpipe, and the parent blocks forever reading it.

Reproduced in the sibling rtl-buddy repo (identical reader code) in 1
of 6 full pytest runs on uv-managed CPython 3.12 with Tcl 9.0.3: an
orphaned child sat wedged for 9+ hours with the parent's main thread
in ``subprocess_fork_exec → do_fork_exec → child_exec →
_close_open_fds_maybe_unsafe → close`` and a live
``NotifierThreadProc`` on the second thread.

rb-cdc shells out to yosys and the slang frontend via ``subprocess``
*after* it parses the SDC, so an in-process interp puts a permanent
hang hazard on the main analysis path. Keeping ``_tkinter`` out of the
rb-cdc process removes the notifier thread, and with it the hazard.
The cost is one short-lived subprocess per SDC file.

Consequences this module must preserve:

- Import nothing heavy. Stdlib only (``rtl_buddy_cdc.tcl_tokenizer``
  would be acceptable — it is stdlib-only too — but is not needed
  here). The parent imports this module for its pure helpers, so a
  top-level ``tkinter`` import here would reintroduce the hazard; a
  test asserts there is none.
- ``tkinter`` is imported **lazily inside** :func:`_evaluate`.
- stdout carries the JSON response and nothing else.
"""

from __future__ import annotations

import json
import re
import sys
from typing import Any

#: ``python -m`` target, shared with :mod:`rtl_buddy_cdc.sdc` so the
#: spawn site and the diagnostics cannot drift apart.
WORKER_MODULE = "rtl_buddy_cdc.tcl_worker"

__all__ = ["WORKER_MODULE", "main"]


class _WorkerError(Exception):
    """An evaluation failure, tagged with the response ``kind``."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


# Commands whose *names* are collections in SDC/Tcl ("get_ports",
# "all_inputs", …). Under the interp reader they are aliased through
# ``unknown`` and must hand back something ``_extract_names`` peels the
# same way it peels the tokenizer's opaque ``[get_ports clk]`` word —
# so we re-wrap the evaluated operands in the canonical bracket form.
def _is_collection_command(cmd: str) -> bool:
    return cmd.startswith("get_") or cmd.startswith("all_")


_NESTED_COLLECTION_RE = re.compile(r"\[(?:get|all)_[A-Za-z0-9_]*\s*([^][]*)\]")


def _strip_collection_wrappers(word: str) -> str:
    """Collapse nested ``[get_cells u/*]`` wrappers back to bare names.

    ``[get_pins [get_cells u/*]/C]`` evaluates inside-out: ``get_cells``
    returns ``[get_cells u/*]``, Tcl concatenates ``/C`` onto it, and
    ``get_pins`` receives ``[get_cells u/*]/C``. Peeling the inner
    wrapper yields ``u/*/C``, which is what a timer would resolve the
    nested collection to.
    """
    prev = None
    out = word
    while prev != out:
        prev = out
        out = _NESTED_COLLECTION_RE.sub(r"\1", out)
    return out


# Evaluated inside the safe child before the SDC text. ``puts`` is
# stubbed out because a safe interp has no stdout channel and vendor
# SDC files print progress — and because this process reserves stdout
# for the JSON response. ``unknown`` catches every command the safe
# interp does not define — every SDC command, every ``get_*``
# collection, and the absent-by-construction ``exec`` / ``open`` /
# ``file`` / ``socket`` / ``source``.
_TCL_BOOTSTRAP = r"""
proc puts args {}
proc unknown args {
    set rb_line {}
    catch {
        set rb_frame [info frame [expr {[info frame] - 1}]]
        if {[dict exists $rb_frame line]} {
            set rb_line [dict get $rb_frame line]
        }
    }
    return [rb_cdc_record $rb_line {*}$args]
}
"""


def _evaluate(
    text: str, command_limit: int, time_limit_seconds: int
) -> tuple[str, list[dict[str, Any]], list[str]]:
    """Evaluate ``text`` in a ``tkinter.Tcl()`` safe interp.

    ``tkinter.Tcl()`` needs no display (never call ``Tk()`` here). The
    child is created with ``interp create -safe``, so it has no
    ``exec`` / ``open`` / ``file`` / ``socket`` / ``load`` / ``source``
    and cannot touch the filesystem or spawn a process; those names
    fall through to ``unknown`` like any other undefined command and
    are recorded, never run.

    ``-safe`` bounds *capability*, not *cost*, so the child also
    carries ``interp limit`` budgets, armed before **any** eval — the
    bootstrap included. The command counter stops runaway recursion
    and million-iteration loops; the wall-clock deadline (an absolute
    epoch second, per ``interp limit``'s contract) stops a bare
    ``while 1 {}``, whose empty body never ticks the command counter.

    Returns ``(patchlevel, commands, partial_warnings)``. Raises
    :class:`_WorkerError` tagged ``unavailable`` when there is no Tcl
    to run in, ``resource_limit`` when a budget was exceeded, and
    ``tcl_error`` for an ordinary evaluation failure.
    """
    # Lazy, and deliberately so: see this module's docstring. Nothing
    # above this line may pull in ``_tkinter``.
    try:
        import tkinter
    except ImportError as exc:  # pragma: no cover - environment-dependent
        raise _WorkerError("unavailable", f"_tkinter is not importable: {exc}") from exc

    records: list[dict[str, Any]] = []
    warnings: list[str] = []

    def record(line: str, *words: str) -> str:
        if not words:  # pragma: no cover - Tcl never dispatches an empty command
            return ""
        cmd, args = words[0], list(words[1:])
        if _is_collection_command(cmd):
            inner = " ".join(
                s for s in (_strip_collection_wrappers(a) for a in args) if s
            )
            return f"[{cmd} {inner}]" if inner else f"[{cmd}]"
        try:
            lineno: int | None = int(line)
        except (TypeError, ValueError):
            lineno = None
        records.append({"name": cmd, "words": [cmd, *args], "line": lineno})
        # Mirror the reference ``unknown`` from issue #298: hand back
        # the last word so a command used as a nested substitution
        # still produces something printable.
        return args[-1] if args else ""

    try:
        interp = tkinter.Tcl()
    except Exception as exc:  # pragma: no cover - broken Tcl install
        raise _WorkerError("unavailable", f"tkinter.Tcl() failed: {exc}") from exc
    try:
        patchlevel = str(interp.eval("info patchlevel"))
        interp.createcommand("rb_cdc_record", record)
        interp.eval("interp create -safe rb_cdc_sdc")
        interp.eval(
            f"interp limit rb_cdc_sdc command "
            f"-value {int(command_limit)} -granularity 1"
        )
        interp.eval(
            f"interp limit rb_cdc_sdc time -granularity 10 "
            f"-seconds [expr {{[clock seconds] + {int(time_limit_seconds)}}}]"
        )
        interp.eval("interp alias rb_cdc_sdc rb_cdc_record {} rb_cdc_record")
        interp.call("rb_cdc_sdc", "eval", _TCL_BOOTSTRAP)
        interp.call("rb_cdc_sdc", "eval", text)
    except tkinter.TclError as exc:
        kind = "resource_limit" if "limit exceeded" in str(exc) else "tcl_error"
        raise _WorkerError(kind, str(exc)) from exc
    finally:
        try:
            interp.eval("interp delete rb_cdc_sdc")
            interp.deletecommand("rb_cdc_record")
        except Exception:  # pragma: no cover - teardown is best-effort
            pass
    return patchlevel, records, warnings


def main() -> int:
    """Read one request from stdin, write one response to stdout."""
    raw = sys.stdin.read()
    try:
        request = json.loads(raw)
        text = request["text"]
        command_limit = int(request["command_limit"])
        time_limit_seconds = int(request["time_limit_seconds"])
        if not isinstance(text, str):
            raise TypeError("'text' must be a string")
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        # Not a protocol response: the parent reads a non-zero exit as
        # a read error and falls back to the tokenizer.
        print(f"{WORKER_MODULE}: malformed request: {exc}", file=sys.stderr)
        return 2

    response: dict[str, Any]
    try:
        patchlevel, commands, warnings = _evaluate(
            text, command_limit, time_limit_seconds
        )
    except _WorkerError as exc:
        response = {"ok": False, "kind": exc.kind, "error": str(exc)}
    else:
        response = {
            "ok": True,
            "patchlevel": patchlevel,
            "commands": commands,
            "partial_warnings": warnings,
        }
    json.dump(response, sys.stdout)
    sys.stdout.write("\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
