"""ModelSpend: route each task to the lowest-cost model expected to suit it. Bring your own keys."""
from .catalog import Catalog
from .client import ModelSpend
from .config import Config
from .errors import (
    AllAttemptsFailed, BudgetTooLow, ModelSpendError, NoEligibleModel, ProviderAllowlistRequired, StreamInterrupted,
    UnsupportedInput, ValidationRequired,
)
from .policy import POLICY_VERSION
from .types import Plan, Profile, RouteRequest, RouteResult, TaskCategory

__version__ = "0.6.0b1"
__all__ = [
    "ModelSpend", "Catalog", "Config", "RouteRequest", "RouteResult", "Plan", "Profile", "TaskCategory", "POLICY_VERSION",
    "ModelSpendError", "NoEligibleModel", "BudgetTooLow", "AllAttemptsFailed", "StreamInterrupted",
    "ProviderAllowlistRequired", "ValidationRequired", "UnsupportedInput",
]
