"""Application screenshots, transcribed once by a vision model and cached.

The model only transcribes; every identifier it returns is re-validated with the same
regexes used for text evidence, so a hallucinated id cannot enter the observation store.
"""
from __future__ import annotations

from ..config import PROMPTS_DIR, VISION_MODELS
from ..identifiers import EMAIL_RE, RESOURCE_RE, USER_REF_RE, norm_email, norm_phone, system_for_resource
from ..llm import LLMClient, LLMUnavailable, OutputTruncated, image_part, text_part
from ..model import EvidenceFile, Observation, ParseOutput

FMT = "image"
PURPOSE = "image_extract"
MAX_TOKENS = 900                  # stays under the free tier's output-tokens-per-minute cap
COMPACT_NOTE = ("Compact mode: the full transcription did not fit. For table rows give only "
                "resource_id, user_id, email, name and status, with empty references and attributes; "
                "give references and attributes only for the record in the detail panel. "
                "Keep activity lines short.")
NULLABLE = {"type": ["string", "null"]}
SCHEMA = {
    "type": "object",
    "properties": {
        "application": NULLABLE,
        "page_title": NULLABLE,
        "records": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "resource_id": NULLABLE, "system_hint": NULLABLE, "user_id": NULLABLE,
                "email": NULLABLE, "phone": NULLABLE, "name": NULLABLE, "status": NULLABLE,
                "references": {"type": "array", "items": {"type": "string"}},
                "attributes": {"type": "array", "items": {
                    "type": "object",
                    "properties": {"key": {"type": "string"}, "value": {"type": "string"}},
                    "required": ["key", "value"], "additionalProperties": False}},
            },
            "required": ["resource_id", "system_hint", "user_id", "email", "phone", "name",
                         "status", "references", "attributes"],
            "additionalProperties": False}},
        "activity": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["application", "page_title", "records", "activity"],
    "additionalProperties": False,
}


def job(ev: EvidenceFile, compact: bool = False) -> dict:
    """The request for one screenshot, also used by the LLM preflight plan."""
    prompt = (PROMPTS_DIR / "image_extract.md").read_text(encoding="utf-8")
    ask = "Transcribe this screenshot." + (" " + COMPACT_NOTE if compact else "")
    messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": [text_part(ask), image_part(ev.path)]},
    ]
    return {"purpose": PURPOSE, "messages": messages, "schema": SCHEMA,
            "max_tokens": MAX_TOKENS, "models": VISION_MODELS}


def _valid(pattern, value: str | None) -> str | None:
    return value.strip() if value and pattern.fullmatch(value.strip()) else None


def parse(ev: EvidenceFile, llm: LLMClient | None) -> ParseOutput:
    out = ParseOutput()
    if llm is None:
        out.warnings.append(f"{ev.file_id}: screenshot skipped (no LLM client)")
        return out
    result, last_error = None, ""
    for compact in (False, True):
        spec = job(ev, compact)
        try:
            result = llm.complete_json(spec["purpose"], spec["messages"], spec["schema"],
                                       spec["max_tokens"], spec["models"])
            break
        except OutputTruncated as exc:
            last_error = str(exc)          # dense screen: retry asking for a compact transcription
        except LLMUnavailable as exc:
            last_error = str(exc)          # offline: the compact transcription may be the cached one
            if not llm.offline:
                break
    if result is None:
        out.warnings.append(f"{ev.file_id}: screenshot not transcribed ({last_error})")
        return out
    data = result.data
    screen = {"application": data.get("application"), "page_title": data.get("page_title"),
              "extracted_by": result.model}
    for rec in data.get("records", []):
        # Models sometimes copy a combined header ("user_11 · account_5762") into one field,
        # so ids are pulled out of the text with the same regexes used for text evidence.
        id_text = f"{rec.get('resource_id') or ''} {rec.get('user_id') or ''}"
        rids = RESOURCE_RE.findall(id_text)
        rid = rids[0] if rids else None
        users = USER_REF_RE.findall(id_text)
        user = users[0] if users else None
        email = _valid(EMAIL_RE, rec.get("email"))
        if not (rid or user or email):
            continue
        attr_text = " ".join([*(rec.get("references") or []), *(a["value"] for a in rec.get("attributes") or [])])
        mentioned = [r for r in dict.fromkeys(RESOURCE_RE.findall(attr_text)) if r not in rids]
        fields = {**screen, "user_id": user, "email": email,
                  "phone": rec.get("phone"), "name": rec.get("name"),
                  "system_hint": rec.get("system_hint"), "status": rec.get("status"),
                  "references": rec.get("references") or []}
        for attr in rec.get("attributes") or []:
            fields[f"attr.{attr['key']}"] = attr["value"]
        links = [k for k in (norm_email(email) if email else None, user) if k]
        links += [r for r in (rec.get("references") or []) if r]
        if rec.get("phone"):
            links.append(norm_phone(rec["phone"]))
        out.observations.append(Observation(
            evidence_id=ev.file_id, source_format=FMT, parser="image.record",
            observed_at=ev.captured_at, captured_at=ev.captured_at,
            system_id=system_for_resource(rid) or rec.get("system_hint"), resource_id=rid,
            collection="screen", location=data.get("application"), presence="PRESENT" if rid else None,
            result=(rec.get("status") or "").upper() or None, fields=fields,
            link_keys=sorted(set(links)), raw=str(rec)[:2000]))
        # Other records shown as belonging to this one (e.g. its support ticket) exist and are
        # linked to the same person, but none of this record's fields are theirs.
        for other in mentioned:
            out.observations.append(Observation(
                evidence_id=ev.file_id, source_format=FMT, parser="image.reference",
                observed_at=ev.captured_at, captured_at=ev.captured_at,
                system_id=system_for_resource(other), resource_id=other, collection="screen_reference",
                location=data.get("application"), presence="PRESENT", fields={**screen, "referenced_by": rid},
                link_keys=sorted(set(links)), raw=attr_text[:2000]))
    for line in data.get("activity") or []:
        for rid in RESOURCE_RE.findall(line):
            out.observations.append(Observation(
                evidence_id=ev.file_id, source_format=FMT, parser="image.activity",
                observed_at=ev.captured_at, captured_at=ev.captured_at,
                system_id=system_for_resource(rid), resource_id=rid, collection="screen_activity",
                location=data.get("application"), presence=None, fields={**screen, "activity": line},
                raw=line))
    return out
