"""PDF business documents (statements, attachments, registers, journal extracts).

pypdf recovers the text layer cleanly, so extraction is deterministic: identifiers,
business references and register rows. No model call is needed for these files.
"""
from __future__ import annotations

import re

from pypdf import PdfReader

from ..identifiers import REFERENCE_RE, RESOURCE_RE, identifiers_in_text, system_for_resource
from ..model import EvidenceFile, Observation, ParseOutput

FMT = "pdf"
REGISTER_ROW_RE = re.compile(
    r"(ORD-\d+)\s*\|\s*(user_\d+)\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*"
    r"Retain until\s+([^|]+?)\s*\|")
RETAIN_RE = re.compile(r"retain until\s+(\d{1,2} \w{3} \d{4})", re.I)
FILE_RE = re.compile(r"\b[\w-]+\.(?:pdf|zip|png|jpg|csv)\b")


def document_text(path) -> str:
    return "\n".join((page.extract_text() or "") for page in PdfReader(str(path)).pages)


def parse(ev: EvidenceFile) -> ParseOutput:
    out = ParseOutput()
    text = document_text(ev.path)
    flat = re.sub(r"\s+", " ", text)
    title = next((line.strip() for line in text.splitlines() if line.strip()), "")
    ids = identifiers_in_text(text)
    references = sorted(set(REFERENCE_RE.findall(flat)))
    common = {
        "document_title": title,
        "emails": sorted(ids.get("email", [])),
        "phones": sorted(ids.get("phone", [])),
        "references": references,
        "file_names": sorted(set(FILE_RE.findall(flat))),
        "retain_until": RETAIN_RE.findall(flat),
    }
    links = sorted(ids.get("email", set()) | ids.get("user", set()) | set(references))

    for row in REGISTER_ROW_RE.finditer(flat):
        order, user, reason, date, amount, until = row.groups()
        out.observations.append(Observation(
            evidence_id=ev.file_id, source_format=FMT, parser="pdf.register_row",
            observed_at=ev.captured_at, captured_at=ev.captured_at, system_id=None, resource_id=None,
            collection="register", presence="PRESENT",
            fields={"order_reference": order, "subject_reference": user, "reason": reason,
                    "recorded": date, "amount": amount, "retain_until": until,
                    "document_title": title},
            link_keys=[user, order], raw=row.group(0)))

    resources = sorted(set(RESOURCE_RE.findall(flat)))
    for rid in resources:
        out.observations.append(Observation(
            evidence_id=ev.file_id, source_format=FMT, parser="pdf.document",
            observed_at=ev.captured_at, captured_at=ev.captured_at, system_id=system_for_resource(rid),
            resource_id=rid, collection="document", presence="PRESENT",
            fields={**common, "subject_reference": sorted(ids.get("user", []))},
            link_keys=links, raw=flat[:2000]))
    if not resources:
        # No resource id printed (e.g. a statement): keep the document so references such as
        # statement numbers can be joined to catalog aliases later.
        out.observations.append(Observation(
            evidence_id=ev.file_id, source_format=FMT, parser="pdf.document",
            observed_at=ev.captured_at, captured_at=ev.captured_at, system_id=None, resource_id=None,
            collection="document", presence="PRESENT",
            fields={**common, "subject_reference": sorted(ids.get("user", []))},
            link_keys=links, raw=flat[:2000]))
    return out
