"""Template library for the CDC/RDC fuzz corpus."""

from .base import ExpectedFinding, Op, RenderedCase, Template
from .cdc001 import UnsyncedSingleBit
from .cdc002 import ShortChain
from .cdc003 import CombBeforeSync
from .cdc004 import UncodedBus
from .cdc006 import CombSource
from .cdc008 import ClockAsData
from .cdc011 import UnconstrainedInput
from .cdc014 import CombBetweenStages
from .cdc016 import OppositeEdgeChain
from .gap_g1 import GapG1LatchInSyncChain
from .gap_g3 import GapG3PulseSyncFalsePositive
from .gap_g12 import GapG12GateLevelSilent
from .good import GoodTwoFF
from .rdc001 import AsyncResetCrossing
from .rdc004 import CombDrivenReset
from .rdc005 import MultiSourceReset

ALL_TEMPLATES: list[type[Template]] = [
    UnsyncedSingleBit,
    ShortChain,
    CombBeforeSync,
    UncodedBus,
    CombSource,
    ClockAsData,
    UnconstrainedInput,
    CombBetweenStages,
    OppositeEdgeChain,
    AsyncResetCrossing,
    CombDrivenReset,
    MultiSourceReset,
    GapG1LatchInSyncChain,
    GapG3PulseSyncFalsePositive,
    GapG12GateLevelSilent,
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
    "CombSource",
    "ClockAsData",
    "UnconstrainedInput",
    "CombBetweenStages",
    "OppositeEdgeChain",
    "AsyncResetCrossing",
    "CombDrivenReset",
    "MultiSourceReset",
    "GapG1LatchInSyncChain",
    "GapG3PulseSyncFalsePositive",
    "GapG12GateLevelSilent",
    "GoodTwoFF",
]
