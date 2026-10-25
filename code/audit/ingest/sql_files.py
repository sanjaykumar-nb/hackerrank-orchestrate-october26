"""SQLite-compatible snapshot exports: load into memory, then read every table.

Table names carry the topology (primary, regional/read/reporting/failover replica), so a
copy that survives only in a replica keeps its location for replication-drift findings.
"""
from __future__ import annotations

import json
import re
import sqlite3

from ..identifiers import system_for_resource
from ..model import Coverage, EvidenceFile, ParseOutput
from .common import record_observation

FMT = "sql"
LOCATION_RE = re.compile(r"(primary|(?:\w+_)?replica(?:_\w+)?)$")


def location_of(table: str) -> str:
    """customer_data_replica_eu_west -> replica_eu_west; customer_profiles_primary -> primary."""
    for prefix in ("customer_data_", "customer_profiles_", "search_documents_"):
        if table.startswith(prefix):
            rest = table[len(prefix):]
            if LOCATION_RE.fullmatch(rest):
                return rest
    if table.endswith("_primary"):
        return "primary"
    return "inventory"


def header_value(text: str, label: str) -> str | None:
    m = re.search(rf"^--\s*{label}:\s*(.+)$", text, re.M)
    return m.group(1).strip() if m else None


def parse(ev: EvidenceFile) -> ParseOutput:
    script = ev.path.read_text(encoding="utf-8")
    captured = header_value(script, "Captured at") or ev.captured_at
    topology = header_value(script, "Database topology")
    con = sqlite3.connect(":memory:")
    con.executescript(script)
    out = ParseOutput()
    tables = [r[0] for r in con.execute("select name from sqlite_master where type='table' order by name")]
    scope: list[tuple[str, str]] = []                     # (system, captured_at) from snapshot_scope
    per_table: dict[str, list[dict]] = {}
    for table in tables:
        cols = [r[1] for r in con.execute(f'pragma table_info("{table}")')]
        rows = [dict(zip(cols, row)) for row in con.execute(f'select * from "{table}"')]
        if table == "snapshot_scope":
            scope = [(r["origin_system"], r.get("captured_at") or captured) for r in rows]
            continue
        per_table[table] = rows

    collections: set[tuple[str, str | None]] = set()     # (system, collection) seen anywhere in the file
    for table, rows in per_table.items():
        location = location_of(table)
        for row in rows:
            if "record_json" in row:
                system, collection = row.get("origin_system"), row.get("collection_name")
                record = json.loads(row["record_json"])
                observed = row.get("updated_at") or captured
            else:
                record = dict(row)
                system = system_for_resource(record.get("resource_id"))
                collection = table
                observed = record.pop("updated_at", None) or record.get("indexed_at") or captured
            if system:
                collections.add((system, collection))
            obs = record_observation(record, ev, source_format=FMT, parser=f"sql.{location}",
                                     system_id=system, collection=collection, location=location,
                                     observed_at=observed)
            obs.fields["_table"] = table
            obs.fields["_topology"] = topology
            out.observations.append(obs)

    if scope:
        # Inventory captures declare what they scanned; an empty table still proves absence.
        for system, when in scope:
            out.coverage.append(Coverage(ev.file_id, when, system, None, "inventory",
                                         sum(o.system_id == system for o in out.observations)))
    else:
        for table, rows in per_table.items():
            location = location_of(table)
            systems = {s for s, _ in collections} or {
                system_for_resource(r.get("resource_id")) for r in rows} - {None}
            for system in sorted(s for s in systems if s):
                count = sum(o.system_id == system and o.location == location for o in out.observations)
                out.coverage.append(Coverage(ev.file_id, captured, system, None, location, count))
    con.close()
    return out
