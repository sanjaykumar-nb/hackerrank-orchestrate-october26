"""Identifier patterns and normalization shared by ingestion, identity resolution and linking."""
from __future__ import annotations

import hashlib
import re

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
USER_REF_RE = re.compile(r"\buser_\d+\b")
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
PHONE_RE = re.compile(r"\+\d[\d ().-]{6,}\d")
REFERENCE_RE = re.compile(r"\b(?:ORD-\d+|CS-\d+|NST-\d{4}-\d{4}-\d+|CJX-\d{8}-\d{4})\b")

# Resource-id prefix -> owning system, as named throughout system_activity.csv and the evidence.
RESOURCE_SYSTEMS = {
    "account": "account_service",
    "search": "search_service",
    "analytics": "analytics_platform",
    "support": "support_platform",
    "email_delivery": "email_delivery_service",
    "cache": "cache_service",
    "audit_record": "security_service",
    "export": "export_service",
    "data_flow": "integration_service",
    "backup": "backup_service",
    "tombstone": "identity_service",
    "legal_hold": "legal_service",
    "object": "object_storage",
    "marketing": "marketing_platform",
}
RESOURCE_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?:" + "|".join(sorted(RESOURCE_SYSTEMS, key=len, reverse=True)) + r")_\d+\b"
)

# Field names whose non-null values are direct identifiers stored inside a record.
EMAIL_FIELDS = {"email", "owner_email", "customer_email", "requester_email", "uploaded_by", "subject_selector"}
PHONE_FIELDS = {"phone", "owner_phone", "requester_phone"}
NAME_FIELDS = {"full_name", "name", "display_name", "requester_name", "customer_name"}
HASH_FIELDS = {"email_hash"}
USER_FIELDS = {"user_id", "customer_reference", "subject_reference"}
# Field names that describe how a record was looked up, not what the record contains.
LINK_FIELDS = {
    "lookup_key", "match_key", "scan_key", "subject_key", "subject_ref",
    "correlation_key", "lookup_value", "query",
}
NULLISH = {"", "null", "none", "nil", "no direct identifier", "[redacted]"}


def is_null(value) -> bool:
    return value is None or (isinstance(value, str) and value.strip().lower() in NULLISH)


def norm_email(value: str) -> str:
    return value.strip().lower()


def email_hash(email: str) -> str:
    """Dataset hashes are sha256 of the lower-cased email (verified on all 850 identities)."""
    return hashlib.sha256(norm_email(email).encode("utf-8")).hexdigest()


def norm_phone(value: str) -> str:
    return re.sub(r"\D", "", value)


def system_for_resource(resource_id: str | None) -> str | None:
    if not resource_id:
        return None
    return RESOURCE_SYSTEMS.get(resource_id.rsplit("_", 1)[0])


def classify(value) -> tuple[str, str] | None:
    """Return (kind, normalized) for an identifier-like scalar, else None."""
    if is_null(value) or not isinstance(value, str):
        return None
    v = value.strip()
    if EMAIL_RE.fullmatch(v):
        return "email", norm_email(v)
    if USER_REF_RE.fullmatch(v):
        return "user", v
    if HASH_RE.fullmatch(v.lower()):
        return "hash", v.lower()
    if RESOURCE_RE.fullmatch(v):
        return "resource", v
    if PHONE_RE.fullmatch(v):
        return "phone", norm_phone(v)
    return None


def identifiers_in_text(text: str) -> dict[str, set[str]]:
    """Identifiers embedded in free text such as support messages or PDF pages."""
    found: dict[str, set[str]] = {}
    for m in EMAIL_RE.findall(text or ""):
        found.setdefault("email", set()).add(norm_email(m))
    for m in USER_REF_RE.findall(text or ""):
        found.setdefault("user", set()).add(m)
    for m in PHONE_RE.findall(text or ""):
        found.setdefault("phone", set()).add(norm_phone(m))
    return found
