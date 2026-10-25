"""Does the request authorize deletion? Deterministic first pass.

Non-deletion cues are checked before deletion words because the hard cases mention deletion
only to refuse it ("I am not asking for deletion", "without removing any of my records",
"remove me from marketing lists only"). Phase 6 adds an LLM classifier; this rule set stays
as its fallback and as a cross-check.
"""
from __future__ import annotations

import re

NON_DELETION = [
    r"\bnot asking for (?:deletion|erasure)",
    r"\bnot a request to (?:erase|delete)",
    r"\bdo(?:n'?t| not) (?:close|delete|remove|erase|change)",
    r"\bwithout (?:removing|deleting|closing)",
    r"\b(?:marketing|promotional|newsletter)[\w\s]{0,20}\bonly\b",
    r"\bstop (?:the )?(?:promotional|marketing) (?:emails|messages)",
    r"\b(?:leave|keep) (?:the|my) (?:account|profile)[\w\s]{0,30}(?:active|as it is|in place)",
    r"\bleave my (?:profile|account|records)[\w\s]{0,30}in place",
    r"\bcontinue using the service",
    r"\bI still (?:need|use|want) (?:the|my|it)\b",
    r"\b(?:send|give) me a copy\b|\bcopy of (?:the information|last year)",
    r"\bdownload my (?:account )?(?:information|data)",
    r"\bwhat personal information you hold",
    r"\b(?:correct|replace|update|change) (?:it|the|my)\b(?![\w\s]{0,20}\bdelet)",
    r"\breset(?:ting)? the login",
]
DELETION = [
    r"delet", r"\berase", r"\berasure", r"\bremove", r"\bclose (?:my|this|the) account",
    r"permanently", r"supprim", r"suppression", r"\bis gone\b", r"cannot identify me", r"anonymi",r"elimin", r"exclus", r"l[öo]sch", r"削除", r"삭제", r"\bborr",
]
NON_RE = [re.compile(p, re.I) for p in NON_DELETION]
DEL_RE = [re.compile(p, re.I) for p in DELETION]


KINDS = ["deletion", "verify_deletion", "access", "export", "correction", "marketing_opt_out",
         "account_help", "other"]
SCHEMA = {
    "type": "object",
    "properties": {"items": {"type": "array", "items": {
        "type": "object",
        "properties": {"id": {"type": "string"}, "d": {"type": "boolean"}},
        "required": ["id", "d"], "additionalProperties": False}}},
    "required": ["items"], "additionalProperties": False,
}
BATCH = 30                # ~11 output tokens per item, so 30 items fit in MAX_TOKENS with room
MAX_TOKENS = 450          # stays under the free tier's output-tokens-per-minute cap


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def intent_jobs(texts: list[str], unique_sorted: bool = True) -> list[dict]:
    """Unique texts in stable order, batched. Identical wording is classified once."""
    from .config import PROMPTS_DIR, TEXT_MODELS
    prompt = (PROMPTS_DIR / "intent.md").read_text(encoding="utf-8")
    unique = sorted({_norm(t) for t in texts}) if unique_sorted else list(texts)
    jobs = []
    for start in range(0, len(unique), BATCH):
        chunk = unique[start:start + BATCH]
        body = "\n".join(f"[t{i}] {t}" for i, t in enumerate(chunk))
        jobs.append({"purpose": "intent", "texts": chunk, "schema": SCHEMA, "max_tokens": MAX_TOKENS,
                     "models": TEXT_MODELS,
                     "messages": [{"role": "system", "content": prompt},
                                  {"role": "user", "content": f"Requests:\n{body}"}]})
    return jobs


def classify_intents(texts: list[str], llm) -> dict[str, dict]:
    """text -> {deletion, cue, source, rule, llm}. The model decides when it answers; the rules
    decide when it cannot, and every disagreement is kept for review."""
    out: dict[str, dict] = {}
    for t in texts:
        ok, cue = rule_intent(t)
        out[_norm(t)] = {"deletion": ok, "cue": cue, "source": "rules", "rule": ok, "llm": None, "kind": None}
    if llm is None:
        return out
    from .llm import LLMUnavailable
    jobs = intent_jobs(texts)
    for round_no in range(2):              # second round re-asks anything the model skipped
        missing = []
        for job in jobs:
            try:
                res = llm.complete_json(job["purpose"], job["messages"], job["schema"],
                                        job["max_tokens"], job["models"])
            except LLMUnavailable as exc:
                for t in job["texts"]:
                    out[t]["cue"] += f" (LLM unavailable: {exc})"
                continue
            answers = {item["id"]: item for item in res.data.get("items", [])}
            for i, t in enumerate(job["texts"]):
                item = answers.get(f"t{i}")
                if item is None:
                    missing.append(t)
                    continue
                rec = out[t]
                rec.update(llm=item["d"], source=f"llm:{res.model}", deletion=item["d"])
                rec["cue"] = f"{'deletion' if item['d'] else 'non-deletion'} ({res.model})" + (
                    "" if item["d"] == rec["rule"] else f"; rules disagree: {rec['cue']}")
        if not missing:
            break
        jobs = intent_jobs(missing, unique_sorted=False)
    return out


def rule_intent(text: str) -> tuple[bool, str]:
    """-> (authorizes_deletion, cue that decided it)."""
    for pattern in NON_RE:
        m = pattern.search(text or "")
        if m:
            return False, f"non-deletion cue: {m.group(0)!r}"
    for pattern in DEL_RE:
        m = pattern.search(text or "")
        if m:
            return True, f"deletion cue: {m.group(0)!r}"
    return False, "no deletion request found"
