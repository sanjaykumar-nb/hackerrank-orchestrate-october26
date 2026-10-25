# Usage report

Final full-dataset run `final-20261025T072341Z` that produced `output.csv`, generated 2026-10-25 07:23 UTC from `code/llm_cache/ledger.jsonl`.

- Provider: **Groq** (free tier). Requests processed: **550** (518 evaluation + 32 public samples, all through the same pipeline).
- Model roles: `qwen/qwen3.8-27b` transcribes the 11 screenshots (vision) and classifies request intent; `openai/gpt-oss-20b` is the text fallback (unused unless qwen's budget runs out). Everything else (parsing, identity, linking, state, policy, planning) is deterministic code with no model calls.
- Free tier bills $0. Costs below are estimates at Groq's on-demand list prices (per 1M tokens: qwen3.8-27b $0.80 in / $4.00 out; gpt-oss-20b $0.075 in / $0.30 out).

## Model answers this output depends on

Every model answer used by the run, whether fetched now or reused from the response cache (cached answers were billed once, by the run that created them).

| Model | Answers used | Fetched this run | From cache | Input tokens | Output tokens | Total tokens | Est. cost (USD) |
|---|---|---|---|---|---|---|---|
| `qwen/qwen3.8-27b` | 26 | 0 | 26 | 43,685 | 11,089 | 54,774 | 0.0793 |
| **Overall** | 26 | 0 | 26 | 43,685 | 11,089 | 54,774 | 0.0793 |

| Per request | Tokens | Est. cost (USD) |
|---|---|---|
| Average over 550 processed requests | 99.6 | 0.000144 |
| Average over the 518 evaluation requests | 105.7 | 0.000153 |

Requests with identical wording are classified once (550 texts -> fewer unique texts), and texts are batched, so most requests cost a fraction of one call.

## Calls billed during this run

| Model | API calls | Failed / rejected | Input tokens | Output tokens | Est. cost (USD) |
|---|---|---|---|---|---|
| `qwen/qwen3.8-27b` | 0 | 0 | 0 | 0 | 0.0000 |

## All development calls (every run so far)

| Model | API calls | Failed / rejected | Input tokens | Output tokens | Est. cost (USD) |
|---|---|---|---|---|---|
| `qwen/qwen3.8-27b` | 49 | 7 | 63,610 | 18,289 | 0.1240 |

## Free-tier safety

- Groq free tier per model: 30 RPM, 1,000 RPD, 8,000 TPM, 200,000 TPD; qwen3.8-27b also caps output at 1,000 tokens/minute.
- The client never plans beyond 75% of a per-minute limit or 60% of a daily limit, reserves prompt + max output before each call, settles to billed usage, honours `retry-after`, and refuses a run whose projected spend (x1.2) exceeds what is left today, falling back to cached answers and rules.
- Failed calls above are output truncations (retried in compact form, then cached so they are not repeated) and rate-limit rejections (waited out); no limit was exceeded.
