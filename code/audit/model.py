"""Core records produced by ingestion and consumed by every later stage."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class EvidenceFile:
    file_id: str
    path: Path
    origin: str
    captured_at: str


@dataclass
class Observation:
    """One sighting of a resource (or of a lookup) inside one evidence item.

    `fields` holds what the record itself contains, which is what can expose PII.
    `link_keys` holds the values a source used to look the record up (trace lookups,
    correlation keys, record-lookup subjects); they link a record to a person but are
    not content of the record.
    """
    evidence_id: str
    source_format: str              # sql | json | text | log | pdf | image
    parser: str                     # extractor that produced it, for explain/debugging
    observed_at: str | None         # record or line timestamp
    captured_at: str | None         # capture time of the evidence item (manifest)
    system_id: str | None
    resource_id: str | None
    collection: str | None = None
    location: str | None = None     # primary | replica_* | failover_replica | snapshot | log | ...
    presence: str | None = None     # PRESENT | ABSENT | None when the source does not say
    result: str | None = None       # status word given by the source (FOUND, ANONYMIZED, ...)
    record_type: str | None = None  # data category given by the source (USER_PROFILE, ...)
    fields: dict = field(default_factory=dict)
    link_keys: list[str] = field(default_factory=list)
    raw: str = ""


@dataclass(frozen=True)
class Coverage:
    """An evidence item that captured a whole collection: a resource of `system_id` missing
    from it was absent at `captured_at` (in that location)."""
    evidence_id: str
    captured_at: str
    system_id: str
    collection: str | None
    location: str | None
    record_count: int


@dataclass
class ParseOutput:
    observations: list[Observation] = field(default_factory=list)
    coverage: list[Coverage] = field(default_factory=list)
    unparsed: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def extend(self, other: "ParseOutput") -> None:
        self.observations += other.observations
        self.coverage += other.coverage
        self.unparsed += other.unparsed
        self.warnings += other.warnings
