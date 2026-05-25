"""Shared parameter-sweep helpers for the fuzz corpus.

Stage 2 of the umbrella plan (rtl-buddy-cdc#190) asks for every
shipping rule to fire at least 10× across the corpus. The cheapest
way to multiply cases without touching template SV is to sweep the
SDC clock periods — same RTL, different timing context. Each entry
yields its own corpus case keyed by ``case_id``; the Yosys cache
keys on ``(top, sv, sdc, extra_passes)`` so distinct SDC values
trigger distinct cache buckets, but the SV elaboration is identical
per template, so the marginal cost is only Yosys-parse-SV.

The ratios span fast-src / slow-dst, slow-src / fast-dst, and
near-equal so the corpus exercises diverse pulse-loss / sampling
regimes (relevant for CDC-009 / CDC-013 sweeps in particular).
"""

from __future__ import annotations

TWO_CLOCK_PERIODS: list[tuple[float, float]] = [
    (10.0, 7.5),
    (10.0, 13.0),
    (5.0, 18.0),
    (20.0, 9.0),
    (8.0, 23.0),
    (15.0, 11.0),
    (3.5, 17.0),
    (12.0, 5.5),
    (25.0, 8.0),
    (6.0, 14.5),
    (9.5, 22.0),
    (16.0, 4.5),
]

ONE_CLOCK_PERIODS: list[float] = [
    10.0,
    7.5,
    18.0,
    5.0,
    12.5,
    8.0,
    20.0,
    15.0,
    4.5,
    25.0,
]


def case_suffix(*values: float | int | str) -> str:
    """Stable suffix for a case_id from a parameter tuple. Floats are
    rendered as ints scaled ×10 so case_ids stay file-system friendly
    (``10p0_7p5`` would also work but ``100_75`` is shorter)."""
    parts: list[str] = []
    for v in values:
        if isinstance(v, float):
            parts.append(str(int(round(v * 10))))
        else:
            parts.append(str(v))
    return "_".join(parts)
