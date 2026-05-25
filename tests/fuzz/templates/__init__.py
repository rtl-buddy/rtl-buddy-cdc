"""Template library for the CDC/RDC fuzz corpus."""

from .base import ExpectedFinding, Op, RenderedCase, Template
from .cdc001 import UnsyncedSingleBit
from .cdc002 import ShortChain
from .cdc003 import CombBeforeSync
from .cdc004 import UncodedBus
from .cdc005 import ReconvergentSync
from .cdc006 import CombSource
from .cdc008 import ClockAsData
from .cdc009 import PulseWidthFastToSlow
from .cdc010 import AsyncClockMux
from .cdc011 import UnconstrainedInput
from .cdc012 import FunctionalDataHoldEnable
from .cdc013 import ToggleNoXorTail
from .cdc014 import CombBetweenStages
from .cdc015 import SyncChainForeignReset
from .cdc016 import OppositeEdgeChain
from .gap_g1 import GapG1LatchInSyncChain
from .gap_g3 import GapG3PulseSyncFalsePositive
from .gap_g12 import GapG12GateLevelSilent
from .good import GoodTwoFF
from .rdc001 import AsyncResetCrossing
from .rdc002 import ResetPolarityMismatch
from .rdc003 import SyncResetCrossing
from .rdc004 import CombDrivenReset
from .rdc005 import MultiSourceReset
from .rdc006 import DerivedAsyncResetUnsync

ALL_TEMPLATES: list[type[Template]] = [
    UnsyncedSingleBit,
    ShortChain,
    CombBeforeSync,
    UncodedBus,
    ReconvergentSync,
    CombSource,
    ClockAsData,
    PulseWidthFastToSlow,
    AsyncClockMux,
    UnconstrainedInput,
    FunctionalDataHoldEnable,
    ToggleNoXorTail,
    CombBetweenStages,
    SyncChainForeignReset,
    OppositeEdgeChain,
    AsyncResetCrossing,
    ResetPolarityMismatch,
    SyncResetCrossing,
    CombDrivenReset,
    MultiSourceReset,
    DerivedAsyncResetUnsync,
    GapG1LatchInSyncChain,
    GapG3PulseSyncFalsePositive,
    GapG12GateLevelSilent,
    GoodTwoFF,
]

__all__ = [
    "ALL_TEMPLATES",
    "AsyncClockMux",
    "AsyncResetCrossing",
    "ClockAsData",
    "CombBeforeSync",
    "CombBetweenStages",
    "CombDrivenReset",
    "CombSource",
    "DerivedAsyncResetUnsync",
    "ExpectedFinding",
    "FunctionalDataHoldEnable",
    "GapG1LatchInSyncChain",
    "GapG12GateLevelSilent",
    "GapG3PulseSyncFalsePositive",
    "GoodTwoFF",
    "MultiSourceReset",
    "Op",
    "OppositeEdgeChain",
    "PulseWidthFastToSlow",
    "ReconvergentSync",
    "RenderedCase",
    "ResetPolarityMismatch",
    "ShortChain",
    "SyncChainForeignReset",
    "SyncResetCrossing",
    "Template",
    "ToggleNoXorTail",
    "UnconstrainedInput",
    "UncodedBus",
    "UnsyncedSingleBit",
]
