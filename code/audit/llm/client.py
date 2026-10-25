"""Groq chat client wrapper: cache -> budget check -> rate limit -> call -> ledger.

Callers never see a rate-limit error. They get a parsed JSON result, or LLMUnavailable,
which means "use your deterministic fallback".
"""
from __future__ import annotations

import base64
import json
import mimetypes
import os
import random
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from ..config import LEDGER_PATH, LLM_CACHE_DIR, MODELS, SAFETY, ModelSpec, Safety, load_dotenv
from .cache import ResponseCache
from .guard import BudgetExhausted, FreeTierGuard, RequestTooLarge
from .ledger import Ledger


class LLMUnavailable(RuntimeError):
    """No cached answer and no model could serve the request within budget."""


class OutputTruncated(LLMUnavailable):
    """The answer did not fit in max_tokens; the caller may retry with a more compact request."""


@dataclass
class LLMResult:
    data: dict
    model: str
    cached: bool
    prompt_tokens: int = 0
    completion_tokens: int = 0


@dataclass
class TransportResponse:
    status: int                          # HTTP status, 0 for connection failures
    headers: dict = field(default_factory=dict)
    body: dict = field(default_factory=dict)


def text_part(text: str) -> dict:
    return {"type": "text", "text": text}


def image_part(path: Path) -> dict:
    mime = mimetypes.guess_type(str(path))[0] or "image/png"
    data = base64.b64encode(Path(path).read_bytes()).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}}


class GroqTransport:
    """Thin adapter over the official SDK with its own retries disabled; the guard owns retries."""

    def __init__(self, api_key: str, timeout: float = 60.0):
        from groq import Groq
        self.client = Groq(api_key=api_key, max_retries=0, timeout=timeout)

    def send(self, model: str, messages: list[dict], response_format: dict,
             max_tokens: int, extra: dict) -> TransportResponse:
        import groq
        try:
            raw = self.client.chat.completions.with_raw_response.create(
                model=model, messages=messages, temperature=0, max_completion_tokens=max_tokens,
                response_format=response_format, **extra)
            return TransportResponse(200, dict(raw.headers), raw.parse().model_dump())
        except groq.APIStatusError as exc:
            try:
                body = exc.response.json()
            except Exception:
                body = {"error": exc.response.text[:500]}
            return TransportResponse(exc.status_code, dict(exc.response.headers), body)
        except (groq.APIConnectionError, groq.APITimeoutError) as exc:
            return TransportResponse(0, {}, {"error": type(exc).__name__})


class LLMClient:
    def __init__(self, transport=None, ledger: Ledger | None = None, cache: ResponseCache | None = None,
                 run_id: str | None = None, offline: bool | None = None, safety: Safety = SAFETY,
                 clock=time.time, sleep=time.sleep):
        if transport is None and not offline:
            load_dotenv()
            key = os.environ.get("GROQ_API_KEY")
            transport = GroqTransport(key) if key else None
        self.transport = transport
        self.offline = bool(offline) or transport is None
        self.ledger = ledger or Ledger(LEDGER_PATH, clock=clock)
        self.cache = cache or ResponseCache(LLM_CACHE_DIR)
        self.run_id = run_id or time.strftime("run-%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + uuid.uuid4().hex[:6]
        self.safety = safety
        self.clock = clock
        self.sleep = sleep
        self.rng = random.Random(0)
        self.guards: dict[str, FreeTierGuard] = {}
        self.events: list[str] = []      # human-readable notes about fallbacks, for run summaries

    def guard(self, model: str) -> FreeTierGuard:
        if model not in self.guards:
            self.guards[model] = FreeTierGuard(MODELS[model], self.ledger, self.safety, self.clock, self.sleep)
        return self.guards[model]

    @staticmethod
    def estimate_tokens(messages: list[dict], spec: ModelSpec, schema: dict | None = None) -> int:
        """Conservative prompt estimate: ~3 UTF-8 bytes per token, fixed cost per image."""
        total = 0
        for msg in messages:
            total += 8
            content = msg.get("content")
            parts = content if isinstance(content, list) else [{"type": "text", "text": content or ""}]
            for part in parts:
                if part.get("type") == "image_url":
                    total += spec.image_tokens
                else:
                    total += len(part.get("text", "").encode("utf-8")) // 3 + 1
        if schema:
            total += len(json.dumps(schema).encode("utf-8")) // 3
        return total

    def _reserve(self, messages, spec, schema, max_tokens) -> int:
        return self.estimate_tokens(messages, spec, schema) + max_tokens + spec.reasoning_headroom

    # -- main entry point ------------------------------------------------------------
    def complete_json(self, purpose: str, messages: list[dict], schema: dict, max_tokens: int,
                      models: list[str]) -> LLMResult:
        key = self.cache.key(purpose, messages, schema)
        hit = self.cache.get(purpose, key)
        if hit is not None and hit.get("truncated"):
            # Known not to fit: skip the call so a dense input does not cost a failure every run.
            raise OutputTruncated(f"{purpose}: answer known to exceed the output cap ({hit['model']})")
        if hit is not None:
            usage = hit.get("usage", {})
            self.ledger.record(kind="cache_hit", run_id=self.run_id, purpose=purpose, model=hit["model"],
                               prompt_tokens=usage.get("prompt_tokens", 0),
                               completion_tokens=usage.get("completion_tokens", 0))
            return LLMResult(hit["response"], hit["model"], True,
                             usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0))
        if self.offline:
            raise LLMUnavailable(f"{purpose}: no cached answer and no GROQ_API_KEY")

        response_format = {"type": "json_schema",
                           "json_schema": {"name": purpose, "strict": True, "schema": schema}}
        for model in models:
            spec = MODELS[model]
            guard = self.guard(model)
            est = self._reserve(messages, spec, schema, max_tokens)
            max_out = max_tokens + spec.reasoning_headroom
            for attempt in range(self.safety.max_retries + 1):
                try:
                    slot = guard.acquire(est, max_out)
                except (BudgetExhausted, RequestTooLarge) as exc:
                    self.events.append(f"skip {model}: {exc}")
                    break
                started = self.clock()
                resp = self.transport.send(model, messages, response_format, max_out, spec.extra)
                usage = (resp.body or {}).get("usage") or {}
                billed = int(usage.get("total_tokens") or 0)
                guard.observe_headers(resp.headers)
                row = dict(kind="call", run_id=self.run_id, purpose=purpose, model=model, attempt=attempt,
                           status=resp.status, prompt_tokens=int(usage.get("prompt_tokens") or 0),
                           completion_tokens=int(usage.get("completion_tokens") or 0),
                           total_tokens=billed, est_tokens=est,
                           latency_ms=int((self.clock() - started) * 1000))
                if resp.status == 200:
                    guard.settle(slot, billed or est, row["completion_tokens"] or max_out)
                    data = self._parse_content(resp.body)
                    if data is None:
                        self.ledger.record(**row, outcome="bad_json")
                        continue
                    self.ledger.record(**row, outcome="ok")
                    self.cache.put(purpose, key, {
                        "purpose": purpose, "model": model, "response": data,
                        "usage": {"prompt_tokens": row["prompt_tokens"],
                                  "completion_tokens": row["completion_tokens"]},
                        "request": self.cache.stable_request(messages), "schema": schema,
                    })
                    return LLMResult(data, model, False, row["prompt_tokens"], row["completion_tokens"])

                # Groq error text names the organisation; keep it out of the shipped ledger.
                error = re.sub(r"org_[A-Za-z0-9]+", "org_[REDACTED]", str((resp.body or {}).get("error")))
                if resp.status == 400 and "json_validate_failed" in error and "max completion tokens" in error:
                    # The model generated up to max_tokens before failing: that output was spent.
                    guard.settle(slot, est, max_out)
                    self.ledger.record(**row, outcome="truncated", error=error[:300])
                    self.cache.put(purpose, key, {"purpose": purpose, "model": model, "truncated": True,
                                                  "max_tokens": max_out,
                                                  "request": self.cache.stable_request(messages)})
                    raise OutputTruncated(f"{purpose}: answer exceeded {max_out} tokens on {model}")
                guard.release(slot)
                self.ledger.record(**row, outcome="error", error=error[:300])
                if resp.status == 401:
                    raise LLMUnavailable("Groq rejected the API key (401)")
                if resp.status == 429:
                    retry_after = float({k.lower(): v for k, v in resp.headers.items()}.get("retry-after") or 0)
                    wait = retry_after or min(self.safety.max_wait_s, 5 * 2 ** attempt)
                    if wait > self.safety.max_wait_s:
                        self.events.append(f"skip {model}: retry-after {wait:.0f}s exceeds max wait")
                        break
                    guard.penalize(wait + self.rng.uniform(0, 0.5))
                    continue
                if resp.status in (0, 500, 502, 503, 504):
                    self.sleep(min(self.safety.max_wait_s, 2 * 2 ** attempt))
                    continue
                self.events.append(f"skip {model}: HTTP {resp.status}")
                break
        raise LLMUnavailable(f"{purpose}: all models failed or are out of budget")

    @staticmethod
    def _parse_content(body: dict) -> dict | None:
        try:
            content = body["choices"][0]["message"]["content"]
            data = json.loads(content)
            return data if isinstance(data, dict) else None
        except (KeyError, IndexError, TypeError, json.JSONDecodeError):
            return None

    # -- preflight -------------------------------------------------------------------
    def plan(self, jobs: list[dict]) -> dict:
        """Project spend for jobs (purpose, messages, schema, max_tokens, models) against today's budget.
        Uncached jobs are charged to their primary model, inflated by the safety margin."""
        need: dict[str, dict] = {}
        cached = 0
        for job in jobs:
            key = self.cache.key(job["purpose"], job["messages"], job["schema"])
            if self.cache.get(job["purpose"], key) is not None:
                cached += 1
                continue
            model = job["models"][0]
            est = self._reserve(job["messages"], MODELS[model], job["schema"], job["max_tokens"])
            slot = need.setdefault(model, {"requests": 0, "tokens": 0})
            slot["requests"] += 1
            slot["tokens"] += est
        report = {"cached_jobs": cached, "models": {}, "ok": True}
        for model, slot in need.items():
            remaining_requests, remaining_tokens = self.guard(model).day_remaining()
            req = int(slot["requests"] * self.safety.preflight_margin + 0.999)
            tok = int(slot["tokens"] * self.safety.preflight_margin)
            ok = req <= remaining_requests and tok <= remaining_tokens
            report["models"][model] = {"requests": slot["requests"], "tokens": slot["tokens"],
                                       "with_margin": [req, tok],
                                       "remaining": [remaining_requests, remaining_tokens], "ok": ok}
            report["ok"] &= ok
        return report
