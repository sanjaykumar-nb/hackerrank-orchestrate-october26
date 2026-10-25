"""Helpers shared by all evidence parsers: flattening, presence inference, record-lookups."""
from __future__ import annotations

import json

from ..identifiers import LINK_FIELDS, classify, is_null, system_for_resource
from ..model import EvidenceFile, Observation

ABSENT_RESULTS = {"NOT_FOUND", "ABSENT", "MISSING", "DELETED_RECORD", "MISS"}
RECORD_LOOKUP_KEYS = {"system", "resource_id", "record_type", "result"}


def flatten(record: dict, prefix: str = "") -> dict:
    """Nested dicts become dotted keys; scalar lists are kept, lists of dicts become JSON text."""
    out: dict = {}
    for key, value in record.items():
        name = f"{prefix}{key}"
        if isinstance(value, dict):
            out.update(flatten(value, name + "."))
        elif isinstance(value, list) and any(isinstance(v, dict) for v in value):
            out[name] = json.dumps(value, ensure_ascii=False, sort_keys=True)
        else:
            out[name] = value
    return out


def coerce(value):
    """Log and SQL values arrive as strings or 0/1; normalize the obvious scalars."""
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("true", "false"):
            return low == "true"
        if low in ("null", "none"):
            return None
        if low.lstrip("-").isdigit() and len(low) < 12:
            return int(low)
    return value


def infer_presence(fields: dict, result: str | None = None) -> str:
    status = fields.get("http_status", fields.get("status_code"))
    if status in (404, "404", 410, "410"):
        return "ABSENT"
    if fields.get("rows") in (0, "0") and status not in (200, "200"):
        return "ABSENT"
    if result and result.upper() in ABSENT_RESULTS:
        return "ABSENT"
    return "PRESENT"


def link_values(fields: dict) -> list[str]:
    """Identifier-like values held in lookup/correlation fields."""
    keys = []
    for name, value in fields.items():
        if name.split(".")[-1] in LINK_FIELDS and not is_null(value):
            hit = classify(str(value))
            keys.append(hit[1] if hit else str(value).strip())
    return keys


def is_record_lookup(record: dict) -> bool:
    return RECORD_LOOKUP_KEYS <= set(record)


def record_lookup_observation(record: dict, ev: EvidenceFile, source_format: str, parser: str,
                              raw: str = "") -> Observation:
    """Rows of the form (time, system, resource, record_type, result, subject) used by
    handovers, ledgers, manifests and diagnostic logs. The subject names who the row is about,
    so it is a link key, not record content."""
    subject = record.get("subject_reference", record.get("lookup_value"))
    result = str(record.get("result") or "").upper() or None
    hit = classify(str(subject)) if not is_null(subject) else None
    return Observation(
        evidence_id=ev.file_id, source_format=source_format, parser=parser,
        observed_at=record.get("recorded_at") or ev.captured_at, captured_at=ev.captured_at,
        system_id=record.get("system") or system_for_resource(record.get("resource_id")),
        resource_id=record.get("resource_id"), collection="record_lookup", location=None,
        presence=infer_presence({}, result), result=result, record_type=record.get("record_type"),
        fields={k: v for k, v in record.items() if k not in ("subject_reference", "lookup_value")},
        link_keys=[hit[1] if hit else str(subject).strip()] if subject is not None and not is_null(subject) else [],
        raw=raw or json.dumps(record, ensure_ascii=False, sort_keys=True),
    )


def record_observation(record: dict, ev: EvidenceFile, *, source_format: str, parser: str,
                       system_id: str | None, collection: str | None, location: str | None,
                       observed_at: str | None, extra_links: list[str] | None = None,
                       presence: str | None = None) -> Observation:
    fields = {k: coerce(v) for k, v in flatten(record).items()}
    links = link_values(fields) + list(extra_links or [])
    for name in list(fields):
        if name.split(".")[-1] in LINK_FIELDS:
            fields.pop(name)                     # how it was found, not what it contains
    rid = fields.get("resource_id")
    result = fields.get("result") if isinstance(fields.get("result"), str) else None
    return Observation(
        evidence_id=ev.file_id, source_format=source_format, parser=parser,
        observed_at=observed_at or ev.captured_at, captured_at=ev.captured_at,
        system_id=system_id or system_for_resource(rid), resource_id=rid,
        collection=collection, location=location,
        presence=presence or infer_presence(fields, result), result=result.upper() if result else None,
        record_type=fields.get("record_type"), fields=fields, link_keys=sorted(set(links)),
        raw=json.dumps(record, ensure_ascii=False, sort_keys=True, default=str)[:2000],
    )
