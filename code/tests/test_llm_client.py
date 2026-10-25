"""Offline tests for the free-tier guard and client: fake clock, fake transport, no network."""
from __future__ import annotations

import json

import pytest

from audit.config import FREE_TIER, MODELS, ModelSpec
from audit.llm.cache import ResponseCache
from audit.llm.client import LLMClient, LLMUnavailable, TransportResponse
from audit.llm.guard import BudgetExhausted, FreeTierGuard, RequestTooLarge, parse_duration
from audit.llm.ledger import Ledger

PRIMARY, FALLBACK = "qwen/qwen3.8-27b", "openai/gpt-oss-20b"
SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}},
          "required": ["ok"], "additionalProperties": False}


class FakeClock:
    def __init__(self, start: float = 1_000_000.0):
        self.t = start

    def __call__(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


def ok_body(total=300):
    return {"choices": [{"message": {"content": '{"ok": true}'}}],
            "usage": {"prompt_tokens": total - 20, "completion_tokens": 20, "total_tokens": total}}


class FakeTransport:
    def __init__(self, script):
        self.script = list(script)       # list of (model or None, TransportResponse)
        self.calls = []

    def send(self, model, messages, response_format, max_tokens, extra):
        self.calls.append(model)
        expect, resp = self.script.pop(0)
        assert expect in (None, model)
        return resp


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def ledger(tmp_path, clock):
    return Ledger(tmp_path / "ledger.jsonl", clock=clock)


def make_guard(ledger, clock):
    return FreeTierGuard(MODELS[PRIMARY], ledger, clock=clock, sleep=clock.sleep)


def make_client(tmp_path, clock, transport, ledger=None):
    return LLMClient(transport=transport, ledger=ledger or Ledger(tmp_path / "ledger.jsonl", clock=clock),
                     cache=ResponseCache(tmp_path / "cache"), run_id="test", clock=clock, sleep=clock.sleep)


def msgs(text="classify this"):
    return [{"role": "system", "content": "You classify."}, {"role": "user", "content": text}]


def test_caps_are_fractions_of_free_tier(ledger, clock):
    g = make_guard(ledger, clock)
    assert (g.rpm_cap, g.tpm_cap, g.rpd_cap, g.tpd_cap) == (22, 6_000, 600, 120_000)


def test_minute_token_window_blocks_until_room(ledger, clock):
    g = make_guard(ledger, clock)
    g.acquire(3_500)
    start = clock.t
    g.acquire(3_500)                     # 7,000 > 6,000 cap: must wait for the first to age out
    assert clock.t - start >= 60


def test_minute_request_window_blocks(ledger, clock):
    g = make_guard(ledger, clock)
    for _ in range(g.rpm_cap):
        g.acquire(10)
    start = clock.t
    g.acquire(10)
    assert clock.t - start >= 60


def test_single_request_over_minute_cap_is_rejected(ledger, clock):
    with pytest.raises(RequestTooLarge):
        make_guard(ledger, clock).acquire(6_001)


def test_daily_budget_counts_only_last_24h(ledger, clock):
    ledger.record(kind="call", model=PRIMARY, total_tokens=119_000, ts=clock.t - 3_600)
    ledger.record(kind="call", model=PRIMARY, total_tokens=500_000, ts=clock.t - 90_000)  # older than a day
    g = make_guard(ledger, clock)
    with pytest.raises(BudgetExhausted):
        g.acquire(2_000)
    g.acquire(900)                       # still fits under 120,000


def test_server_header_floor_stops_spending(ledger, clock):
    g = make_guard(ledger, clock)
    g.observe_headers({"x-ratelimit-remaining-requests": "40"})
    with pytest.raises(BudgetExhausted):
        g.acquire(100)


def test_low_minute_tokens_header_pauses(ledger, clock):
    g = make_guard(ledger, clock)
    g.observe_headers({"x-ratelimit-remaining-tokens": "500", "x-ratelimit-reset-tokens": "7.5s"})
    start = clock.t
    g.acquire(100)
    assert clock.t - start >= 7.5


@pytest.mark.parametrize("raw,seconds", [("7.66s", 7.66), ("2m59.5s", 179.5), ("1h2m3s", 3723), ("120ms", 0.12), (None, 0)])
def test_parse_duration(raw, seconds):
    assert parse_duration(raw) == pytest.approx(seconds)


def test_cache_hit_makes_no_call(tmp_path, clock):
    transport = FakeTransport([(PRIMARY, TransportResponse(200, {}, ok_body()))])
    client = make_client(tmp_path, clock, transport)
    first = client.complete_json("unit", msgs(), SCHEMA, 50, [PRIMARY, FALLBACK])
    second = client.complete_json("unit", msgs(), SCHEMA, 50, [PRIMARY, FALLBACK])
    assert (first.cached, second.cached, len(transport.calls)) == (False, True, 1)
    assert second.data == {"ok": True}


def test_429_honours_retry_after_then_succeeds(tmp_path, clock):
    transport = FakeTransport([
        (PRIMARY, TransportResponse(429, {"retry-after": "4"}, {"error": "rate limited"})),
        (PRIMARY, TransportResponse(200, {}, ok_body())),
    ])
    client = make_client(tmp_path, clock, transport)
    start = clock.t
    result = client.complete_json("unit", msgs(), SCHEMA, 50, [PRIMARY])
    assert result.data == {"ok": True} and clock.t - start >= 4
    assert [r["status"] for r in client.ledger.rows if r["kind"] == "call"] == [429, 200]


def test_exhausted_primary_falls_back(tmp_path, clock):
    ledger = Ledger(tmp_path / "ledger.jsonl", clock=clock)
    ledger.record(kind="call", model=PRIMARY, total_tokens=120_000, ts=clock.t - 60)
    transport = FakeTransport([(FALLBACK, TransportResponse(200, {}, ok_body()))])
    result = make_client(tmp_path, clock, transport, ledger).complete_json(
        "unit", msgs(), SCHEMA, 50, [PRIMARY, FALLBACK])
    assert result.model == FALLBACK and transport.calls == [FALLBACK]


def test_offline_without_cache_raises(tmp_path, clock):
    client = LLMClient(offline=True, ledger=Ledger(tmp_path / "l.jsonl", clock=clock),
                       cache=ResponseCache(tmp_path / "cache"), clock=clock, sleep=clock.sleep)
    with pytest.raises(LLMUnavailable):
        client.complete_json("unit", msgs(), SCHEMA, 50, [PRIMARY])


def test_plan_flags_over_budget(tmp_path, clock):
    client = make_client(tmp_path, clock, FakeTransport([]))
    job = {"purpose": "unit", "schema": SCHEMA, "max_tokens": 1_000, "models": [PRIMARY]}
    small = client.plan([{**job, "messages": msgs(f"text {i}")} for i in range(10)])
    huge = client.plan([{**job, "messages": msgs(f"text {i}")} for i in range(200)])
    assert small["ok"] and not huge["ok"]


def test_image_tokens_counted(clock):
    spec = ModelSpec("m", FREE_TIER, vision=True, image_tokens=2_048)
    content = [{"type": "text", "text": "read"}, {"type": "image_url", "image_url": {"url": "data:x"}}]
    assert LLMClient.estimate_tokens([{"role": "user", "content": content}], spec) >= 2_048


def test_output_token_window_blocks(ledger, clock):
    g = make_guard(ledger, clock)                       # qwen: 1,000 OTPM -> 900 planned
    g.acquire(1_000, 600)
    start = clock.t
    g.acquire(1_000, 600)
    assert clock.t - start >= 60
    with pytest.raises(RequestTooLarge):
        g.acquire(1_000, 901)


def test_truncation_is_cached_and_not_repeated(tmp_path, clock):
    from audit.llm.client import OutputTruncated
    err = {"error": {"code": "json_validate_failed",
                     "failed_generation": "max completion tokens reached before generating a valid document"}}
    transport = FakeTransport([(PRIMARY, TransportResponse(400, {}, err))])
    client = make_client(tmp_path, clock, transport)
    for _ in range(2):
        with pytest.raises(OutputTruncated):
            client.complete_json("unit", msgs(), SCHEMA, 50, [PRIMARY])
    assert len(transport.calls) == 1


def test_intent_reasks_skipped_items(tmp_path, clock):
    from audit.intent import classify_intents
    texts = ["Please delete my account.", "Send me a copy of my data, do not delete anything."]

    class Answers:
        def __init__(self):
            self.calls = 0

        def send(self, model, messages, response_format, max_tokens, extra):
            self.calls += 1
            body = messages[1]["content"]
            items = [{"id": f"t{i}", "d": "delete my account" in line}
                     for i, line in enumerate(body.splitlines()[1:])]
            if self.calls == 1:
                items = items[:1]                       # first answer skips an item
            return TransportResponse(200, {}, {"choices": [{"message": {"content": json.dumps({"items": items})}}],
                                               "usage": {"prompt_tokens": 50, "completion_tokens": 10,
                                                         "total_tokens": 60}})

    transport = Answers()
    out = classify_intents(texts, make_client(tmp_path, clock, transport))
    assert transport.calls == 2
    assert all(v["source"].startswith("llm:") for v in out.values())
    assert out["Please delete my account."]["deletion"] and not out[texts[1]]["deletion"]
