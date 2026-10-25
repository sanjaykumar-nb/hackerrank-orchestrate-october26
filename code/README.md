# Ghost Data deletion audit agent

Deterministic evidence reconciliation, with LLM/VLM calls only where judgement is needed. Calls go to the Groq free tier through a client that cannot exceed its limits.

## Setup

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r code/requirements.txt   # Windows
# .venv/bin/python -m pip install -r code/requirements.txt     # macOS / Linux
cp .env.example .env    # then put your GROQ_API_KEY in .env (never commit it)
```

## Commands

| Command | What it does |
|---|---|
| `python code/main.py` (same as `run`) | Full audit: ingest → intent → identity/linking → state at `audit_at` → policy → steps. Writes `output.csv` (518 rows), validates it against the contract, writes `.cache/sample_predictions.csv` and prints sample scores. `--no-llm` uses cached model results only. |
| `python code/main.py ingest` | Parses all 94 evidence files into `.cache/observations.sqlite`. Screenshots are transcribed by `qwen/qwen3.8-27b` (cached in `code/llm_cache/`). |
| `python code/main.py ingest --no-llm` | Same, without screenshot transcription. |
| `python code/main.py llm-plan` | Projects Groq calls/tokens for uncached work against today's remaining budget. Makes no calls. |
| `python code/main.py inspect --resource account_9797` | Every observation and activity for a resource. Also `--key <email/user/hash>` and `--evidence <file_id>`. |
| `python code/main.py links --case case_12` | The case's resolved identity and every linked record with its reasons (strong / workflow). `--eval` scores linking on the public samples. |
| `python code/main.py assess --case case_11` | Each linked record's state at `audit_at` (with the citing evidence), its data category, rule, target state and whether it violates policy. `--eval` scores decisions on the samples. |
| `python -m pytest code` | Offline unit tests plus dataset ingestion checks. |

## Layout

```
code/
  main.py               CLI
  audit/config.py       paths, models, free-tier limits and safety fractions
  audit/identifiers.py  identifier regexes, normalization, resource-id -> system map
  audit/model.py        Observation / Coverage / EvidenceFile
  audit/ingest/         one parser per format: sql, json, text, logs (6 dialects), pdf, images (VLM)
  audit/store.py        SQLite store with identifier index (content vs link roles)
  audit/llm/            Groq client: cache, ledger, rate-limit guard, preflight, fallback
  prompts/              prompts sent to models
  llm_cache/            cached model responses + call ledger (makes reruns free)
  tests/                pytest suite
```

## Groq free-tier safety

Limits are per model, per organisation: 30 RPM, 1K RPD, 8K TPM, 200K TPD. Check your own org at console.groq.com/settings/limits and edit `audit/config.py` if the numbers differ.

- **Self-imposed caps:** at most 75% of RPM/TPM in any rolling minute, and at most 60% of RPD/TPD in any rolling 24 h. The 24 h window comes from `llm_cache/ledger.jsonl`, so it survives restarts.
- **Reservations:** each call reserves (estimated prompt + `max_completion_tokens`) before it is sent, then settles to the billed usage.
- **Groq's headers:** `x-ratelimit-*` headers tighten the caps, which accounts for usage elsewhere in the org. A 429 waits for `retry-after`. The SDK's own retries are disabled.
- **Preflight:** a run refuses LLM work it cannot finish within budget, and falls back to cached results or deterministic rules instead.
- **Fallback:** text tasks fall back to `openai/gpt-oss-20b`, which has a separate budget.
