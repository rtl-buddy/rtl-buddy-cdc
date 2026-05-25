"""Template protocol + result containers for the fuzz corpus.

A :class:`Template` knows how to render parameterised SV+SDC pairs.
For each rendered case the template also yields the expected analyzer
findings — that's the oracle. The differential harness in
:mod:`tests.fuzz.runner` invokes Yosys + ``run_all_rules`` on the
rendered case and asserts the actual findings match.
"""

from __future__ import annotations

import enum
import hashlib
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Protocol


class Op(str, enum.Enum):
    """Comparison operator for an expected-finding count assertion."""

    EQ = "eq"
    GE = "ge"
    LE = "le"
    ZERO = "zero"


@dataclass(frozen=True)
class ExpectedFinding:
    """A claim about ``run_all_rules``' output for a rendered case.

    ``Op.ZERO`` means "this rule must not fire at all"; ``Op.EQ``
    means "exactly N findings"; ``Op.GE`` / ``Op.LE`` are inequality
    asserts useful when a template knows the bug's lower bound but
    not the exact count (e.g. reset-tree grouping is one-per-source,
    but the template may sweep multiple sources).
    """

    rule_id: str
    op: Op = Op.GE
    count: int = 1

    def check(self, actual: int) -> bool:
        if self.op is Op.ZERO:
            return actual == 0
        if self.op is Op.EQ:
            return actual == self.count
        if self.op is Op.GE:
            return actual >= self.count
        if self.op is Op.LE:
            return actual <= self.count
        raise AssertionError(f"unknown op {self.op}")  # pragma: no cover


@dataclass(frozen=True)
class RenderedCase:
    """A single concrete case ready to be Yosys'd and analyzed.

    ``case_id`` is a stable, deterministic name derived from the
    template and its parameters; used as the cache key suffix and
    the pytest parameter id.

    ``extra_yosys_passes`` is a string of Yosys commands inserted
    between ``flatten`` and ``write_json``. Default empty for the
    canonical recipe. Templates that need to probe gate-level /
    tech-mapped behaviour override it (e.g. ``"simplemap; abc -g
    cmos;"``).

    ``gap_note`` documents a *known* analyzer gap that a template's
    expected/forbidden encodes. Set when a template's expectation
    intentionally captures the analyzer's current (broken) behaviour
    as a sentinel — when the gap is fixed and the test fails, the
    note tells the next reader why.
    """

    template_name: str
    case_id: str
    sv: str
    sdc: str
    top: str
    params: dict
    expected: tuple[ExpectedFinding, ...]
    forbidden: tuple[ExpectedFinding, ...] = field(default_factory=tuple)
    extra_yosys_passes: str = ""
    gap_note: str | None = None

    @property
    def content_hash(self) -> str:
        """SHA-256 over the rendered SV + SDC + top name + extra
        Yosys passes. Used by :mod:`tests.fuzz.yosys_cache` to
        dedupe Yosys invocations."""
        h = hashlib.sha256()
        h.update(self.top.encode())
        h.update(b"\0")
        h.update(self.sv.encode())
        h.update(b"\0")
        h.update(self.sdc.encode())
        h.update(b"\0")
        h.update(self.extra_yosys_passes.encode())
        return h.hexdigest()


class Template(Protocol):
    """A parameterised RTL template that yields concrete cases.

    Concrete templates are *classes* (not instances) so they can be
    enumerated cheaply at collection time. The ``cases`` classmethod
    produces an iterator of :class:`RenderedCase`.
    """

    name: str

    @classmethod
    def cases(cls) -> Iterator[RenderedCase]: ...
