"""Evidence ingestion: dispatch each manifest entry to the parser for its format."""
from __future__ import annotations

from ..llm import LLMClient
from ..model import EvidenceFile, ParseOutput
from . import images, json_files, logs, pdfs, sql_files, text_files

PARSERS = {
    ".sql": sql_files.parse,
    ".json": json_files.parse,
    ".txt": text_files.parse,
    ".log": logs.parse,
    ".pdf": pdfs.parse,
}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}


def ingest_evidence(files: list[EvidenceFile], llm: LLMClient | None = None) -> tuple[ParseOutput, dict]:
    total = ParseOutput()
    stats: dict[str, dict] = {}
    for ev in files:
        ext = ev.path.suffix.lower()
        try:
            if ext in IMAGE_EXTENSIONS:
                part = images.parse(ev, llm)
            elif ext in PARSERS:
                part = PARSERS[ext](ev)
            else:
                part = ParseOutput(warnings=[f"{ev.file_id}: unsupported format {ext}"])
        except Exception as exc:          # one bad file must not sink the run; it is reported
            part = ParseOutput(warnings=[f"{ev.file_id}: parser error {exc!r}"])
        stats[ev.file_id] = {"format": ext.lstrip("."), "observations": len(part.observations),
                             "coverage": len(part.coverage), "unparsed": len(part.unparsed)}
        total.extend(part)
    return total, stats


def image_jobs(files: list[EvidenceFile], llm: LLMClient | None = None) -> list[dict]:
    """Requests a run will make for screenshots. A screen already transcribed in compact form
    is planned as that cached request, not as the full one that is known not to fit."""
    jobs = []
    for ev in files:
        if ev.path.suffix.lower() not in IMAGE_EXTENSIONS:
            continue
        full = images.job(ev)
        if llm is not None:
            compact = images.job(ev, compact=True)
            if llm.cache.get(compact["purpose"], llm.cache.key(compact["purpose"], compact["messages"],
                                                                compact["schema"])) is not None:
                full = compact
        jobs.append(full)
    return jobs
