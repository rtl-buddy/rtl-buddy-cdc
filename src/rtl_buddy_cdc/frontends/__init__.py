"""Concrete elaboration frontends.

Each submodule exposes an ``elaborate(sources, top, **kw) -> Module``
function. The factory in :mod:`rtl_buddy_cdc.frontend` picks one by
name; this package only collects the implementations.
"""
