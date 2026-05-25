"""Parametrised analyzer-differential test across the fuzz corpus."""

from __future__ import annotations

import pytest

from .runner import collect_cases, run_case
from .yosys_cache import yosys_available

# Collect at import time so pytest -k filtering can target case ids.
_CASES = collect_cases()


pytestmark = pytest.mark.fuzz


@pytest.mark.skipif(not yosys_available(), reason="yosys not on PATH")
@pytest.mark.parametrize("case", _CASES, ids=[c.case_id for c in _CASES])
def test_corpus_case(case) -> None:
    result = run_case(case)
    if result.failures:
        formatted = "\n  ".join(result.failures)
        pytest.fail(
            f"\ncase: {case.case_id}\n"
            f"template: {case.template_name}\n"
            f"params: {case.params}\n"
            f"failures:\n  {formatted}\n"
        )
