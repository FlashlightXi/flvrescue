"""Public library API for flvrescue."""

from .mapfile import BadRange, MapValidationError, RecoveryRange, RescueMap
from .optimize import OptimizationRecommendation, recommend_policy
from .policy import (
    DEFAULT_POLICY,
    PASS_NAME_TO_NUMBER,
    PASS_NUMBER_TO_NAME,
    RecoveryPolicy,
)
from .rescue import RescueResult, rescue
from .storage import DestinationPreparation, prepare_destination
from .version import __version__

__all__ = [
    "BadRange",
    "DEFAULT_POLICY",
    "DestinationPreparation",
    "MapValidationError",
    "OptimizationRecommendation",
    "PASS_NAME_TO_NUMBER",
    "PASS_NUMBER_TO_NAME",
    "RecoveryRange",
    "RecoveryPolicy",
    "RescueMap",
    "RescueResult",
    "prepare_destination",
    "recommend_policy",
    "rescue",
    "__version__",
]
