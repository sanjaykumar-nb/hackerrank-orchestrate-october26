"""Free-tier-safe LLM access: cache first, budget-checked, rate-limited, with graceful fallback."""
from .client import LLMClient, LLMResult, LLMUnavailable, OutputTruncated, image_part, text_part
from .guard import BudgetExhausted, FreeTierGuard, RequestTooLarge
from .ledger import Ledger

__all__ = [
    "LLMClient", "LLMResult", "LLMUnavailable", "OutputTruncated", "image_part", "text_part",
    "BudgetExhausted", "FreeTierGuard", "RequestTooLarge", "Ledger",
]
