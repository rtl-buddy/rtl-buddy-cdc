"""Behavioural simulation oracle for CDC/RDC validation.

Wraps Icarus Verilog (`iverilog` + `vvp`) to drive paired bad/good
DUTs through the metastability-injection meta_flop library and
report whether the simulated outputs diverge from the golden
reference. See ``docs/proposals/fuzzer-and-simulation-oracle.md``.
"""
