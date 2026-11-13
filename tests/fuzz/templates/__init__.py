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
from .cdc017 import LatchInCdcPath
from .cdc018 import CascadedChainDepth
from .cdc019 import DecoderLanesIndependentSync
from .cdc020 import SlicedBusLanes
from .cdc021 import UndeclaredClockPort
from .cdc022 import XpmShallowSyncDepth
from .cdc023 import ClockCombine
from .cdc_bbx import BlackboxBoundary
from .concat_lvalue import ConcatLvalueSync
from .gap_g1 import GapG1LatchInSyncChain
from .gap_g2 import GapG2CascadedSynchroniser
from .gap_g3 import GapG3PulseSyncFalsePositive
from .gap_g4 import GapG4OneHotDecodeIndependentSync
from .gap_g5 import GapG5HandshakeAckMissing
from .gap_g6 import GapG6SlicedBusReconvergence
from .gap_g7 import GapG7DeassertionPolarityBackwards
from .gap_g8 import GapG8PrimaryResetUnsynced
from .gap_g9 import GapG9GlitchlessMuxMarked, GapG9GlitchlessMuxUnmarked
from .gap_g10 import GapG10FlopClkUndeclaredPort
from .gap_g12 import GapG12GateLevelSilent
from .good import GoodTwoFF
from .inst_array import InstanceArraySync
from .rdc001 import AsyncResetCrossing
from .rdc002 import ResetPolarityMismatch
from .rdc003 import SyncResetCrossing
from .rdc004 import CombDrivenReset
from .rdc005 import MultiSourceReset
from .rdc006 import DerivedAsyncResetUnsync
from .rdc007 import ResetSyncHeadPolarity
from .rdc008 import PrimaryResetAsymmetricSync

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
    LatchInCdcPath,
    # rtl-buddy-cdc#331: per-rule dimension sweeps for the rules that
    # only had a single-shape gap_g* sentinel (or nothing at all).
    CascadedChainDepth,
    DecoderLanesIndependentSync,
    SlicedBusLanes,
    UndeclaredClockPort,
    XpmShallowSyncDepth,
    ClockCombine,
    BlackboxBoundary,
    # rtl-buddy-cdc#353: instance arrays (lowered by the slang frontend).
    InstanceArraySync,
    ConcatLvalueSync,
    AsyncResetCrossing,
    ResetPolarityMismatch,
    SyncResetCrossing,
    CombDrivenReset,
    MultiSourceReset,
    DerivedAsyncResetUnsync,
    ResetSyncHeadPolarity,
    PrimaryResetAsymmetricSync,
    GapG1LatchInSyncChain,
    GapG2CascadedSynchroniser,
    GapG3PulseSyncFalsePositive,
    GapG4OneHotDecodeIndependentSync,
    GapG5HandshakeAckMissing,
    GapG6SlicedBusReconvergence,
    GapG7DeassertionPolarityBackwards,
    GapG8PrimaryResetUnsynced,
    GapG9GlitchlessMuxUnmarked,
    GapG9GlitchlessMuxMarked,
    GapG10FlopClkUndeclaredPort,
    GapG12GateLevelSilent,
    GoodTwoFF,
]

__all__ = [
    "ALL_TEMPLATES",
    "AsyncClockMux",
    "AsyncResetCrossing",
    "BlackboxBoundary",
    "CascadedChainDepth",
    "ClockAsData",
    "ClockCombine",
    "CombBeforeSync",
    "CombBetweenStages",
    "CombDrivenReset",
    "CombSource",
    "ConcatLvalueSync",
    "DecoderLanesIndependentSync",
    "DerivedAsyncResetUnsync",
    "ExpectedFinding",
    "FunctionalDataHoldEnable",
    "GapG10FlopClkUndeclaredPort",
    "GapG12GateLevelSilent",
    "GapG1LatchInSyncChain",
    "GapG2CascadedSynchroniser",
    "GapG3PulseSyncFalsePositive",
    "GapG4OneHotDecodeIndependentSync",
    "GapG5HandshakeAckMissing",
    "GapG6SlicedBusReconvergence",
    "GapG7DeassertionPolarityBackwards",
    "GapG8PrimaryResetUnsynced",
    "GapG9GlitchlessMuxMarked",
    "GapG9GlitchlessMuxUnmarked",
    "GoodTwoFF",
    "InstanceArraySync",
    "LatchInCdcPath",
    "MultiSourceReset",
    "Op",
    "OppositeEdgeChain",
    "PrimaryResetAsymmetricSync",
    "PulseWidthFastToSlow",
    "ReconvergentSync",
    "RenderedCase",
    "ResetPolarityMismatch",
    "ResetSyncHeadPolarity",
    "ShortChain",
    "SlicedBusLanes",
    "SyncChainForeignReset",
    "SyncResetCrossing",
    "Template",
    "ToggleNoXorTail",
    "UncodedBus",
    "UnconstrainedInput",
    "UndeclaredClockPort",
    "UnsyncedSingleBit",
    "XpmShallowSyncDepth",
]
