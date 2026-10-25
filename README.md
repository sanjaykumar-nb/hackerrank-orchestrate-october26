# Ghost Data Still Exists: deletion audit agent

For each request in `dataset/deletion_requests.csv`, the agent decides whether deletion was asked for. If it was, the agent:
- finds the person's current and historical identifiers;
- finds every record still tied to them across 94 evidence files (SQL replicas, JSON exports, six log formats, PDFs, screenshots);
- reconstructs each record's state at `audit_at`;
- applies `deletion_rules.csv`;
- writes the findings, remediation plan and verification checks to `output.csv`.

The challenge brief is in [`problem_statement.md`](problem_statement.md) and the contract in [`AGENTS.md`](AGENTS.md).

## Results at a glance

On the 32 public samples, scored by the full pipeline (`python code/evaluation/main.py`):

| Metric | Score |
|---|---|
| Deletion-status accuracy | **100%** (32/32) |
| Step precision / recall / F1 | 0.979 / 0.959 / **0.969** |
| Field-level accuracy | 95.9% on every field |
| Answers exactly right (whole `steps_json`) | 23/32 (71.9%); edit similarity 0.941 |
| Target state, findings and cited evidence for correctly linked records | 100% |
| Custom evaluation cases (one controlled change each) | **6/6 pass** |
| Tests | 61 pass |

Evaluation set: 518 rows (148 COMPLETE, 360 INCOMPLETE, 10 NOT_APPLICABLE), all passing the contract validator. The samples were visible during development, so their scores are an upper bound. The [evaluation report](code/evaluation/evaluation_report.md) has the full metrics, the custom cases and the failures-and-fixes table.

## Key insights from the data

These findings shaped the design:

- **Identity is split across log lines.** A lookup's key (an email, hash or user_id) is on the `*_started` line and the resource id on `*_completed`. The two are joined on trace id across six log dialects.
- **The request email may be historical.** 100 of 518 requests use an old address. `email_hash` is sha256 of the lower-cased email, so hashes of old addresses are computed rather than only looked up.
- **Ghost copies live in replicas.** A 404 in the primary does not mean deleted. A failover replica can still hold the record (sample case_05), so state is tracked per location.
- **Partial captures are not inventories.** SQL dumps prove absence. Assurance bundles only prove absence for the people they include, otherwise retained audit records look "missing".
- **Workflow statuses and job results are claims.** They are never proof of state. Within one batch, though, a deletion job's result matches its workflow's reported status 253/255 times, so it identifies which job belongs to which case.
- **Decoys are deliberate.** Similar emails (`g.silva76.jr@`), anonymized records logged one second after the subject's account, and catalog links to records with no later state. Only exact identifiers count, and closeness in time is never treated as ownership.
- **"Delete" is not always a deletion request.** "Remove me from marketing lists only" and "correct it without removing any records" are NOT_APPLICABLE.

## Design decisions

| Decision | Why |
|---|---|
| Deterministic core, models only at the edges | Parsing, linking, state, policy and planning are rules over evidence: auditable, reproducible, free. Models handle intent (any language, negation) and the 11 screenshots. |
| Evidence-first state at `audit_at` | Only evidence captured at or before `audit_at`, per location. Explicit absences only count if they come after the last sighting. Activity rows are used only when no evidence speaks. |
| Rules read from `deletion_rules.csv` at runtime | Categories come from the source's own record type where given, so changed rules need no code change. |
| Global one-owner assignment for identifier-less records | Records are tied through deletion-worker events. Overlapping request windows are resolved once across all cases, and undecidable ties are recorded as `contested-with`. |
| Contract order is also the safe order | Data flows are disabled and restores blocked before any record is deleted, redacted or anonymized. Verification covers every relevant record. |
| Cache every model answer and ship it | Reruns cost nothing and are bit-for-bit reproducible. `code.zip` reproduces `output.csv` exactly with no API key. |
| Hard free-tier budget guard | Each call is checked against Groq's per-minute and per-day limits, including an undocumented output-token cap. A run that would exceed the budget falls back to cache and rules instead. |
| No hardcoded answers | The pipeline never reads `sample_output.csv`; a test enforces it. Tie-break signals were measured on strongly linked records, not on the samples. |

## Limitations

- **Contested ownership.** Deleted accounts and anonymized analytics carry no identifier. When several cases in one batch report the same result, no evidence separates them. 228 of 369 such links in the eval set are contested, and each is right about 1/k of the time. Every other signal tested at chance (batch order, file proximity, file co-membership, record content); see section 5 of the evaluation report. This is the main source of error, and the ceiling of what the data supports.
- **Unobservable records.** 3 of 111 resources in the sample answers (`account_004`, `audit_record_5569`, `audit_record_6645`) appear in no evidence file, activity or screenshot. No grounded method can produce them.
- **Vision transcription.** Screens are read by a model and every id is re-validated by regex. A misread email (seen once) fails to match anyone rather than linking a wrong person.

## Cost

Groq free tier, so $0 billed. Estimates below use Groq's on-demand list prices ([usage report](code/evaluation/usage_report.md)):

| | Model answers | Tokens | Est. cost |
|---|---|---|---|
| Final run behind `output.csv` | 26 (11 screenshots + 15 intent batches) | 54,774 | $0.079 total, about $0.00015 per request |
| All development calls | 49 | 81,899 | $0.12 |

Identical request texts are classified once, and texts are batched 30 per call.

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

Without a key, `python code/main.py --no-llm` uses the shipped cached answers, then deterministic rules.

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

| Stage | Code | Notes |
|---|---|---|
| Ingest | `code/audit/ingest/` | SQL (primary vs replicas), JSON (4 layouts), handover text, 6 log dialects with trace joins, PDFs (text layer), screenshots (VLM). |
| Identity | `code/audit/identity.py` | Current and historical emails, phones and hashes for the subject. |
| Linking | `code/audit/linker.py` | Strong links: the subject's identifiers appear in the record, its lookup, or a catalog entry. Workflow links are matched to the case by job result. |
| State | `code/audit/state.py` | State at `audit_at`, per location, with the evidence that proves it. |
| Policy | `code/audit/policy.py` | Category → rule → target state → violation. |
| Planner and output | `code/audit/planner.py`, `output.py` | Contract order, empty evidence for future steps, deterministic CSV, contract validator. |
| LLM client | `code/audit/llm/` | Cache, ledger, budget preflight, per-minute and per-day caps, `retry-after` handling, fallback model. |
| Evaluation | `code/evaluation/` | Sample scoring, custom cases, tie-signal analysis, report generation. |

| Role | Model (Groq) | Notes |
|---|---|---|
| Screenshot transcription (11 images) | `qwen/qwen3.8-27b` (vision) | Strict JSON schema; dense screens retry in compact form. |
| Request intent (550 texts) | `qwen/qwen3.8-27b`, falling back to `openai/gpt-oss-20b` | 550/550 decided; 0 disagreements with the negation-aware rules. |

## Packaging

```powershell
Get-ChildItem code -Recurse -Directory -Force | Where-Object { $_.Name -in '__pycache__','.pytest_cache' } | Remove-Item -Recurse -Force
Compress-Archive -Path README.md, code -DestinationPath code.zip -Force
```

Upload `code.zip`, `output.csv` and `log.txt` at https://www.hackerrank.com/contests/hackerrank-orchestrate-october26/challenges/ghost-data-still-exists
