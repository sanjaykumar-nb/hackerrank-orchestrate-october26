# Ghost Data Still Exists: deletion audit agent

For each request in `dataset/deletion_requests.csv`, the agent decides whether deletion was asked for. If it was, the agent:
- finds the person's current and historical identifiers;
- finds every record still tied to them across 94 evidence files (SQL replicas, JSON exports, six log formats, PDFs, screenshots);
- reconstructs each record's state at `audit_at`;
- applies `deletion_rules.csv`;
- writes the findings, remediation plan and verification checks to `output.csv`.

The challenge brief is in [`problem_statement.md`](problem_statement.md) and the contract in [`AGENTS.md`](AGENTS.md).

## Setup

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r code/requirements.txt      # Windows
# .venv/bin/python -m pip install -r code/requirements.txt        # macOS / Linux
cp .env.example .env     # add GROQ_API_KEY (free key: https://console.groq.com/keys); never commit .env
```

## Run

```bash
python code/main.py                 # full audit -> output.csv (518 rows), validated; writes code/evaluation/usage_report.md
python code/evaluation/main.py      # evaluation -> code/evaluation/evaluation_report.md (offline, no API calls)
python -m pytest code               # 61 tests
```

Model answers are cached in `code/llm_cache/`. A rerun therefore makes no API calls, and the pipeline also runs without a key: `python code/main.py --no-llm` uses cached answers, then deterministic rules.

To debug a single case:
- `python code/main.py links --case case_12`: the resolved identity and every linked record, with the reason each was linked.
- `python code/main.py assess --case case_11`: each record's state at `audit_at`, its rule, its target state and the evidence cited.
- `python code/main.py inspect --resource account_9797`: every sighting of a record across all evidence.

## Architecture

```
dataset/ ─► ingest (one parser per format; screenshots via VLM, cached) ─► SQLite observation store
        ─► intent (LLM, rules as fallback/cross-check) ─► identity (current + historical ids, computed hashes)
        ─► linker (strong links + workflow links, global one-owner assignment)
        ─► state at audit_at (per location; explicit or full-capture absence) ─► policy (deletion_rules.csv)
        ─► planner (contract order) ─► output.csv + contract validator
```

Most of the work is deterministic code, with models only where judgement is needed.

| Stage | Code | Notes |
|---|---|---|
| Ingest | `code/audit/ingest/` | Logs are joined on trace id, because the lookup key and the resource id sit on different lines. Every ID the vision model returns is re-checked against the same patterns used for text evidence. |
| Identity | `code/audit/identity.py` | The request email may be a current or historical address. Email hashes are computed from every known email, since `email_hash` = sha256 of the lower-cased email. |
| Linking | `code/audit/linker.py` | Strong links: the person's identifiers appear in the record, its lookup, or a catalog entry. Workflow links: deletion-worker events in the request window. Each record goes to one owner, and ambiguous ties are flagged. |
| State | `code/audit/state.py` | Only evidence captured at or before `audit_at` counts. Primary and replicas are tracked separately, so ghost copies are caught. Activity rows are claims, used only when no evidence speaks about the record. |
| Policy | `code/audit/policy.py` | The data category comes from the source's own record type, or else from record signals. Rule conditions cover retention, attribution and the user_id-only tombstone. |
| Planner | `code/audit/planner.py` | Remediation order is: disable flows and block restores first, then delete, redact and anonymize. Future steps have an empty `evidence_id`. |
| LLM client | `code/audit/llm/` | Groq free tier: cache first, budget preflight, per-minute and per-day caps (including output tokens), `retry-after` handling, fallback model, call ledger. |

## Models (Groq free tier)

| Role | Model | Notes |
|---|---|---|
| Screenshot transcription (11 images) | `qwen/qwen3.8-27b` (vision) | Strict JSON schema; dense screens retry in compact form. |
| Request intent (550 texts, about 15 batches) | `qwen/qwen3.8-27b`, falling back to `openai/gpt-oss-20b` | Cross-checked against negation-aware rules. |

Token use and cost are in [`code/evaluation/usage_report.md`](code/evaluation/usage_report.md). Accuracy, the custom cases and failure analysis are in [`code/evaluation/evaluation_report.md`](code/evaluation/evaluation_report.md).

## Packaging

```powershell
Compress-Archive -Path README.md, code -DestinationPath code.zip -Force
```

Upload `code.zip`, `output.csv` and `log.txt` at https://www.hackerrank.com/contests/hackerrank-orchestrate-october26/challenges/ghost-data-still-exists
