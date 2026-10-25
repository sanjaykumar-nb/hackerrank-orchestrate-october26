"""Per-model limiter that keeps traffic under self-imposed fractions of the Groq free tier.

Before a call: reserve (estimated prompt + max completion) tokens in a rolling 60 s window
and check rolling 24 h spend from the ledger. After a call: settle the reservation with the
billed usage and fold in Groq's x-ratelimit-* headers, which also reflect usage by teammates
or other scripts in the same organisation.
"""
from __future__ import annotations

import re
import time
from collections import deque

from ..config import SAFETY, ModelSpec, Safety
from .ledger import Ledger

WINDOW_S = 60.0


class BudgetExhausted(RuntimeError):
    """The daily budget for this model cannot cover the request."""


class RequestTooLarge(ValueError):
    """A single request would exceed the per-minute token cap; split the batch."""


def parse_duration(value: str | None) -> float:
    """Groq reset headers look like '7.66s', '2m59.56s', '1h2m3s' or '120ms'."""
    if not value:
        return 0.0
    total = 0.0
    for amount, unit in re.findall(r"([\d.]+)(ms|h|m|s)", value):
        total += float(amount) * {"ms": 0.001, "h": 3600, "m": 60, "s": 1}[unit]
    return total


class FreeTierGuard:
    def __init__(self, spec: ModelSpec, ledger: Ledger, safety: Safety = SAFETY,
                 clock=time.time, sleep=time.sleep):
        lim = spec.limits
        self.model = spec.model_id
        self.ledger = ledger
        self.safety = safety
        self.clock = clock
        self.sleep = sleep
        self.rpm_cap = max(1, int(lim.rpm * safety.minute_fraction))
        self.tpm_cap = int(lim.tpm * safety.minute_fraction)
        self.rpd_cap = int(lim.rpd * safety.day_fraction)
        self.tpd_cap = int(lim.tpd * safety.day_fraction)
        # Output-token cap: a completion can legitimately use its whole max_tokens, so the
        # reservation is max_tokens against 90% of the limit.
        self.otpm_cap = int(lim.otpm * 0.9) if lim.otpm else None
        self.window: deque[list[float]] = deque()   # [timestamp, tokens, output_tokens]
        self.server_remaining_requests: int | None = None
        self.paused_until = 0.0

    # -- budget views -------------------------------------------------------------
    def day_remaining(self) -> tuple[int, int]:
        requests, tokens = self.ledger.day_usage(self.model, self.clock())
        remaining_requests = self.rpd_cap - requests
        if self.server_remaining_requests is not None:
            remaining_requests = min(
                remaining_requests, self.server_remaining_requests - self.safety.min_remaining_requests)
        return remaining_requests, self.tpd_cap - tokens

    def check_day(self, est_tokens: int) -> None:
        remaining_requests, remaining_tokens = self.day_remaining()
        if remaining_requests < 1 or est_tokens > remaining_tokens:
            raise BudgetExhausted(
                f"{self.model}: daily budget left {remaining_requests} requests / "
                f"{remaining_tokens} tokens, need 1 / {est_tokens}")

    # -- rate limiting --------------------------------------------------------------
    def acquire(self, est_tokens: int, out_tokens: int = 0) -> list[float]:
        """Block until the rolling minute has room, then reserve and return the slot."""
        if est_tokens > self.tpm_cap:
            raise RequestTooLarge(f"{self.model}: {est_tokens} tokens exceeds per-minute cap {self.tpm_cap}")
        if self.otpm_cap and out_tokens > self.otpm_cap:
            raise RequestTooLarge(f"{self.model}: {out_tokens} output tokens exceeds per-minute cap {self.otpm_cap}")
        self.check_day(est_tokens)
        while True:
            now = self.clock()
            if self.paused_until > now:
                self.sleep(min(self.paused_until - now, self.safety.max_wait_s))
                continue
            while self.window and now - self.window[0][0] >= WINDOW_S:
                self.window.popleft()
            used = sum(s[1] for s in self.window)
            used_out = sum(s[2] for s in self.window)
            out_ok = not self.otpm_cap or used_out + out_tokens <= self.otpm_cap
            if len(self.window) < self.rpm_cap and used + est_tokens <= self.tpm_cap and out_ok:
                slot = [now, float(est_tokens), float(out_tokens)]
                self.window.append(slot)
                return slot
            wait = WINDOW_S - (now - self.window[0][0]) + 0.05
            self.sleep(max(0.05, min(wait, self.safety.max_wait_s)))

    def settle(self, slot: list[float], actual_tokens: int, actual_out: int | None = None) -> None:
        """Replace the reservation with what Groq actually billed."""
        slot[1] = float(actual_tokens)
        if actual_out is not None:
            slot[2] = float(actual_out)

    def release(self, slot: list[float]) -> None:
        """A rejected call: keep the request count, drop the token reservations."""
        slot[1] = slot[2] = 0.0

    def observe_headers(self, headers: dict) -> None:
        h = {k.lower(): v for k, v in (headers or {}).items()}
        if "x-ratelimit-remaining-requests" in h:
            try:
                self.server_remaining_requests = int(float(h["x-ratelimit-remaining-requests"]))
            except ValueError:
                pass
        if "x-ratelimit-remaining-tokens" in h:
            try:
                remaining_tokens = int(float(h["x-ratelimit-remaining-tokens"]))
            except ValueError:
                remaining_tokens = None
            if remaining_tokens is not None and remaining_tokens < self.safety.low_minute_tokens:
                reset = parse_duration(h.get("x-ratelimit-reset-tokens")) or WINDOW_S
                self.paused_until = max(self.paused_until, self.clock() + reset)

    def penalize(self, retry_after_s: float) -> None:
        self.paused_until = max(self.paused_until, self.clock() + retry_after_s)
