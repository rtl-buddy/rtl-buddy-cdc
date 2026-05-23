"""Template library for the CDC/RDC fuzz corpus."""

from .base import ExpectedFinding, Op, RenderedCase, Template
from .cdc001 import UnsyncedSingleBit
from .cdc002 import ShortChain
from .cdc003 import CombBeforeSync
from .cdc004 import UncodedBus
from .cdc016 import OppositeEdgeChain
from .good import GoodTwoFF
from .rdc001 import AsyncResetCrossing

ALL_TEMPLATES: list[type[Template]] = [
    UnsyncedSingleBit,
    ShortChain,
    CombBeforeSync,
    UncodedBus,
    OppositeEdgeChain,
    AsyncResetCrossing,
    GoodTwoFF,
]

__all__ = [
    "ALL_TEMPLATES",
    "ExpectedFinding",
    "Op",
    "RenderedCase",
    "Template",
    "UnsyncedSingleBit",
    "ShortChain",
    "CombBeforeSync",
    "UncodedBus",
    "OppositeEdgeChain",
    "AsyncResetCrossing",
    "GoodTwoFF",
]
