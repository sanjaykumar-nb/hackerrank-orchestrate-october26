"""Paths, model choices and free-tier budgets. Every tunable knob lives here."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

CODE_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = CODE_DIR.parent
DATASET_DIR = Path(os.environ.get("AUDIT_DATASET_DIR", REPO_ROOT / "dataset"))
BUILD_DIR = REPO_ROOT / ".cache"            # rebuildable artifacts, not shipped in code.zip
STORE_PATH = BUILD_DIR / "observations.sqlite"
LLM_CACHE_DIR = CODE_DIR / "llm_cache"      # shipped: reruns are free and deterministic
LEDGER_PATH = LLM_CACHE_DIR / "ledger.jsonl"
PROMPTS_DIR = CODE_DIR / "prompts"
OUTPUT_CSV = REPO_ROOT / "output.csv"


def load_dotenv(path: Path = REPO_ROOT / ".env") -> None:
    """Minimal .env reader so secrets never need to be typed into commands."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass(frozen=True)
class ModelLimits:
    rpm: int
    rpd: int
    tpm: int
    tpd: int
    otpm: int | None = None          # output tokens per minute, where the org has a separate cap


@dataclass(frozen=True)
class ModelSpec:
    model_id: str
    limits: ModelLimits
    vision: bool = False
    image_tokens: int = 0            # tokens Groq bills per image part
    reasoning_headroom: int = 0      # extra completion tokens a reasoning model may spend
    extra: dict = field(default_factory=dict)  # model-specific request parameters
    usd_per_m_in: float = 0.0        # on-demand list price, for cost estimates (free tier bills $0)
    usd_per_m_out: float = 0.0


# Groq free tier, per model per organisation (console.groq.com/docs/rate-limits, Oct 2026).
# Check console.groq.com/settings/limits for your org and edit if they differ.
FREE_TIER = ModelLimits(rpm=30, rpd=1_000, tpm=8_000, tpd=200_000)
# qwen/qwen3.8-27b also enforces 1,000 output tokens per minute on the free tier. It is not on
# the public rate-limit page; Groq names it in the 429 body ("OTPM: Limit 1000").
QWEN_FREE_TIER = ModelLimits(rpm=30, rpd=1_000, tpm=8_000, tpd=200_000, otpm=1_000)

MODELS: dict[str, ModelSpec] = {
    "qwen/qwen3.8-27b": ModelSpec(
        "qwen/qwen3.8-27b", QWEN_FREE_TIER, vision=True, image_tokens=2_048,
        extra={"reasoning_effort": "none", "reasoning_format": "hidden"},
        usd_per_m_in=0.80, usd_per_m_out=4.00,       # console.groq.com/docs/models, Oct 2026
    ),
    "openai/gpt-oss-20b": ModelSpec(
        "openai/gpt-oss-20b", FREE_TIER, reasoning_headroom=512,
        extra={"reasoning_effort": "low", "include_reasoning": False},
        usd_per_m_in=0.075, usd_per_m_out=0.30,
    ),
}
TEXT_MODELS = ["qwen/qwen3.8-27b", "openai/gpt-oss-20b"]   # primary, then fallback
VISION_MODELS = ["qwen/qwen3.8-27b"]


@dataclass(frozen=True)
class Safety:
    minute_fraction: float = 0.75      # use at most 75% of RPM/TPM in any rolling 60 s
    day_fraction: float = 0.60         # use at most 60% of RPD/TPD in any rolling 24 h
    min_remaining_requests: int = 50   # stop spending when Groq reports fewer daily requests left
    low_minute_tokens: int = 2_000     # pause for the reset when Groq reports fewer minute tokens left
    max_retries: int = 3
    max_wait_s: float = 90.0           # never sleep longer than this on one 429
    preflight_margin: float = 1.2      # projected spend is inflated by this before comparing to budget


SAFETY = Safety()
