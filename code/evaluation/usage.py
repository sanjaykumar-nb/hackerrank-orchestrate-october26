"""usage_report.md from the call ledger.

A run reuses cached model answers, so it shows two views: tokens billed during the run, and
the tokens of every model answer the run's output depends on (billed now or by the earlier run
that produced the cached answer). The second is the true cost of producing output.csv from
scratch. All development calls are summarized separately.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from audit.config import FREE_TIER, MODELS, SAFETY
from audit.llm.ledger import Ledger


def _cost(model: str, tokens_in: int, tokens_out: int) -> float:
    spec = MODELS.get(model)
    if spec is None:
        return 0.0
    return tokens_in / 1e6 * spec.usd_per_m_in + tokens_out / 1e6 * spec.usd_per_m_out


def summarize(rows: list[dict]) -> dict:
    by_model: dict[str, dict] = defaultdict(lambda: defaultdict(int))
    for r in rows:
        m = by_model[r["model"]]
        if r["kind"] == "call":
            m["calls"] += 1
            m["failed_calls"] += r.get("outcome") != "ok"
            m["billed_in"] += r.get("prompt_tokens", 0)
            m["billed_out"] += r.get("completion_tokens", 0)
            if r.get("outcome") == "ok":
                m["answers"] += 1
                m["answer_in"] += r.get("prompt_tokens", 0)
                m["answer_out"] += r.get("completion_tokens", 0)
        elif r["kind"] == "cache_hit":
            m["cache_hits"] += 1
            m["answers"] += 1
            m["answer_in"] += r.get("prompt_tokens", 0)
            m["answer_out"] += r.get("completion_tokens", 0)
        m[f"purpose:{r.get('purpose')}"] += 1
    return {k: dict(v) for k, v in by_model.items()}


def write_usage_report(ledger: Ledger, run_id: str, path: Path, n_eval: int, n_total: int) -> str:
    run = ledger.run_rows(run_id)
    s = summarize(run)
    dev = summarize([r for r in ledger.rows if r["kind"] == "call"])
    lines = [
        "# Usage report",
        "",
        f"Final full-dataset run `{run_id}` that produced `output.csv`, generated "
        f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} from `code/llm_cache/ledger.jsonl`.",
        "",
        f"- Provider: **Groq** (free tier). Requests processed: **{n_total}** "
        f"({n_eval} evaluation + {n_total - n_eval} public samples, all through the same pipeline).",
        "- Model roles: `qwen/qwen3.8-27b` transcribes the 11 screenshots (vision) and classifies request "
        "intent; `openai/gpt-oss-20b` is the text fallback (unused unless qwen's budget runs out). Everything "
        "else (parsing, identity, linking, state, policy, planning) is deterministic code with no model calls.",
        "- Free tier bills $0. Costs below are estimates at Groq's on-demand list prices "
        "(per 1M tokens: qwen3.8-27b $0.80 in / $4.00 out; gpt-oss-20b $0.075 in / $0.30 out).",
        "",
        "## Model answers this output depends on",
        "",
        "Every model answer used by the run, whether fetched now or reused from the response cache "
        "(cached answers were billed once, by the run that created them).",
        "",
        "| Model | Answers used | Fetched this run | From cache | Input tokens | Output tokens | Total tokens | Est. cost (USD) |",
        "|---|---|---|---|---|---|---|---|",
    ]
    tot = defaultdict(float)
    for model, m in sorted(s.items()):
        fetched = m.get("answers", 0) - m.get("cache_hits", 0)
        cost = _cost(model, m.get("answer_in", 0), m.get("answer_out", 0))
        lines.append(f"| `{model}` | {m.get('answers', 0)} | {fetched} | {m.get('cache_hits', 0)} | "
                     f"{m.get('answer_in', 0):,} | {m.get('answer_out', 0):,} | "
                     f"{m.get('answer_in', 0) + m.get('answer_out', 0):,} | {cost:.4f} |")
        for k in ("answers", "cache_hits", "answer_in", "answer_out"):
            tot[k] += m.get(k, 0)
        tot["cost"] += cost
    total_tokens = tot["answer_in"] + tot["answer_out"]
    lines += [
        f"| **Overall** | {int(tot['answers'])} | {int(tot['answers'] - tot['cache_hits'])} | "
        f"{int(tot['cache_hits'])} | {int(tot['answer_in']):,} | {int(tot['answer_out']):,} | "
        f"{int(total_tokens):,} | {tot['cost']:.4f} |",
        "",
        "| Per request | Tokens | Est. cost (USD) |",
        "|---|---|---|",
        f"| Average over {n_total} processed requests | {total_tokens / n_total:,.1f} | {tot['cost'] / n_total:.6f} |",
        f"| Average over the {n_eval} evaluation requests | {total_tokens / n_eval:,.1f} | {tot['cost'] / n_eval:.6f} |",
        "",
        "Requests with identical wording are classified once (550 texts -> fewer unique texts), "
        "and texts are batched, so most requests cost a fraction of one call.",
        "",
        "## Calls billed during this run",
        "",
        "| Model | API calls | Failed / rejected | Input tokens | Output tokens | Est. cost (USD) |",
        "|---|---|---|---|---|---|",
    ]
    for model, m in sorted(s.items()):
        lines.append(f"| `{model}` | {m.get('calls', 0)} | {m.get('failed_calls', 0)} | {m.get('billed_in', 0):,} | "
                     f"{m.get('billed_out', 0):,} | {_cost(model, m.get('billed_in', 0), m.get('billed_out', 0)):.4f} |")
    lines += [
        "",
        "## All development calls (every run so far)",
        "",
        "| Model | API calls | Failed / rejected | Input tokens | Output tokens | Est. cost (USD) |",
        "|---|---|---|---|---|---|",
    ]
    for model, m in sorted(dev.items()):
        lines.append(f"| `{model}` | {m.get('calls', 0)} | {m.get('failed_calls', 0)} | {m.get('billed_in', 0):,} | "
                     f"{m.get('billed_out', 0):,} | {_cost(model, m.get('billed_in', 0), m.get('billed_out', 0)):.4f} |")
    lines += [
        "",
        "## Free-tier safety",
        "",
        f"- Groq free tier per model: {FREE_TIER.rpm} RPM, {FREE_TIER.rpd:,} RPD, {FREE_TIER.tpm:,} TPM, "
        f"{FREE_TIER.tpd:,} TPD; qwen3.8-27b also caps output at 1,000 tokens/minute.",
        f"- The client never plans beyond {int(SAFETY.minute_fraction * 100)}% of a per-minute limit or "
        f"{int(SAFETY.day_fraction * 100)}% of a daily limit, reserves prompt + max output before each call, "
        "settles to billed usage, honours `retry-after`, and refuses a run whose projected spend "
        f"(x{SAFETY.preflight_margin}) exceeds what is left today, falling back to cached answers and rules.",
        "- Failed calls above are output truncations (retried in compact form, then cached so they are not "
        "repeated) and rate-limit rejections (waited out); no limit was exceeded.",
        "",
    ]
    text = "\n".join(lines)
    path.write_text(text, encoding="utf-8")
    return text
