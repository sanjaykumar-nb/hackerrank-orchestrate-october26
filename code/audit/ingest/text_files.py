"""Plain-text records such as shift handovers: pipe-delimited record-lookup rows."""
from __future__ import annotations

import re

from ..identifiers import RESOURCE_RE, system_for_resource
from ..model import EvidenceFile, Observation, ParseOutput
from .common import record_lookup_observation

FMT = "text"
ROW_RE = re.compile(r"^\s*[-*]?\s*(\d{4}-\d{2}-\d{2}T[\d:.]+Z)\s*\|(.+)$")


def parse(ev: EvidenceFile) -> ParseOutput:
    out = ParseOutput()
    for line in ev.path.read_text(encoding="utf-8").splitlines():
        m = ROW_RE.match(line)
        if m:
            cells = [c.strip() for c in m.group(2).split("|")]
            cells += [""] * (5 - len(cells))
            system, resource, record_type, result, subject = cells[:5]
            out.observations.append(record_lookup_observation(
                {"recorded_at": m.group(1), "system": system, "resource_id": resource,
                 "record_type": record_type, "result": result, "subject_reference": subject or None},
                ev, FMT, "text.pipe_row", raw=line.strip()))
        elif RESOURCE_RE.search(line):
            for rid in RESOURCE_RE.findall(line):
                out.observations.append(Observation(
                    evidence_id=ev.file_id, source_format=FMT, parser="text.mention",
                    observed_at=ev.captured_at, captured_at=ev.captured_at,
                    system_id=system_for_resource(rid), resource_id=rid, raw=line.strip()))
            out.warnings.append(f"{ev.file_id}: free-text resource mention: {line.strip()[:80]}")
    return out
