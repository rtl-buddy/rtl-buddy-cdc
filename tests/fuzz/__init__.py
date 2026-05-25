"""Template-driven CDC/RDC fuzz corpus.

See ``docs/proposals/fuzzer-and-simulation-oracle.md`` for the full
design. This is the stage-1 prototype: template-driven random
generation + analyzer-differential harness, gated behind
``@pytest.mark.fuzz``.
"""
