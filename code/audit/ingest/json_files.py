"""JSON evidence: snapshot bundles, service exports, record-lookup ledgers and flat collections."""
from __future__ import annotations

import json

from ..identifiers import system_for_resource
from ..model import Coverage, EvidenceFile, ParseOutput
from .common import is_record_lookup, record_lookup_observation, record_observation

FMT = "json"


def parse(ev: EvidenceFile) -> ParseOutput:
    data = json.loads(ev.path.read_text(encoding="utf-8"))
    out = ParseOutput()
    if isinstance(data, dict) and "snapshots" in data:
        _bundle(data, ev, out)
    elif isinstance(data, dict) and "service_exports" in data:
        _service_export(data, ev, out)
    elif isinstance(data, dict) and isinstance(data.get("records"), list):
        _flat(data, ev, out)
    else:
        _walk_any(data, ev, out)
    return out


def _bundle(data: dict, ev: EvidenceFile, out: ParseOutput) -> None:
    for snap in data["snapshots"]:
        system, collection = snap.get("data_store"), snap.get("collection")
        captured = snap.get("captured_at") or ev.captured_at
        records = snap.get("records") or []
        if records and all(is_record_lookup(r) for r in records):
            for rec in records:
                out.observations.append(record_lookup_observation(rec, ev, FMT, f"json.bundle.{system}"))
            continue
        out.coverage.append(Coverage(ev.file_id, captured, system, collection, "snapshot", len(records)))
        for rec in records:
            out.observations.append(record_observation(
                rec, ev, source_format=FMT, parser="json.bundle", system_id=system,
                collection=collection, location="snapshot", observed_at=captured))


def _service_export(data: dict, ev: EvidenceFile, out: ParseOutput) -> None:
    for system, collections in data["service_exports"].items():
        for collection, records in collections.items():
            for rec in records:
                observed = rec.get("observed_at") or ev.captured_at
                if collection == "index_queries":
                    _index_query(rec, ev, out, system, observed)
                    continue
                if collection == "profile_lookups":
                    rec = {**{k: v for k, v in rec.items() if k != "profile"}, **(rec.get("profile") or {})}
                out.observations.append(record_observation(
                    rec, ev, source_format=FMT, parser=f"json.service_export.{collection}",
                    system_id=system, collection=collection, location="service_export", observed_at=observed))


def _index_query(rec: dict, ev: EvidenceFile, out: ParseOutput, system: str, observed: str) -> None:
    """A search query and its hits: every hit document is a present index entry; a queried
    resource with no matching hit is reported absent."""
    query = rec.get("query")
    docs = (rec.get("hits") or {}).get("documents") or []
    for doc in docs:
        out.observations.append(record_observation(
            doc, ev, source_format=FMT, parser="json.service_export.index_hit", system_id=system,
            collection="index_queries", location="service_export", observed_at=observed,
            extra_links=[query] if query else None, presence="PRESENT"))
    rid = rec.get("resource_id")
    if rid and rid not in {d.get("resource_id") for d in docs}:
        out.observations.append(record_observation(
            {"resource_id": rid, "query": query, "hits_total": 0}, ev, source_format=FMT,
            parser="json.service_export.index_miss", system_id=system, collection="index_queries",
            location="service_export", observed_at=observed, presence="ABSENT"))


def _flat(data: dict, ev: EvidenceFile, out: ParseOutput) -> None:
    records = data["records"]
    captured = data.get("captured_at") or data.get("exported_at") or ev.captured_at
    if records and all(is_record_lookup(r) for r in records):
        for rec in records:
            out.observations.append(record_lookup_observation(rec, ev, FMT, "json.record_lookup"))
        return
    collection = data.get("collection")
    location = data.get("source") or "snapshot"
    systems = {system_for_resource(r.get("resource_id")) for r in records} - {None}
    for system in sorted(systems):
        out.coverage.append(Coverage(ev.file_id, captured, system, collection, location,
                                     sum(system_for_resource(r.get("resource_id")) == system for r in records)))
    for rec in records:
        out.observations.append(record_observation(
            rec, ev, source_format=FMT, parser="json.collection", system_id=None,
            collection=collection, location=location, observed_at=captured))


def _walk_any(node, ev: EvidenceFile, out: ParseOutput) -> None:
    """Unknown layout: take every object that carries a resource_id."""
    if isinstance(node, dict):
        if "resource_id" in node:
            out.observations.append(record_observation(
                node, ev, source_format=FMT, parser="json.generic", system_id=None, collection=None,
                location=None, observed_at=node.get("observed_at") or node.get("recorded_at")))
            out.warnings.append(f"{ev.file_id}: generic JSON walk used")
        for value in node.values():
            if isinstance(value, (dict, list)):
                _walk_any(value, ev, out)
    elif isinstance(node, list):
        for item in node:
            _walk_any(item, ev, out)
