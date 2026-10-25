"""SQLite observation store: every input table plus normalized evidence observations.

`obs_keys` indexes each identifier an observation carries, tagged by role:
  content - stored inside the record (what can expose PII)
  link    - how the source looked the record up (links it to a person, exposes nothing)
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .identifiers import (EMAIL_RE, HASH_FIELDS, LINK_FIELDS, NAME_FIELDS, PHONE_FIELDS, USER_FIELDS,
                          USER_REF_RE, classify, is_null, norm_email, norm_phone)
from .ingest import ingest_evidence
from .ingest.tables import (load_activities, load_identities, load_identity_history, load_manifest,
                            load_requests, load_rules)
from .llm import LLMClient
from .model import Observation

SCHEMA = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE evidence_files(file_id TEXT PRIMARY KEY, path TEXT, origin TEXT, captured_at TEXT, format TEXT,
    observations INTEGER, coverage INTEGER, unparsed INTEGER);
CREATE TABLE observations(obs_id INTEGER PRIMARY KEY, evidence_id TEXT, source_format TEXT, parser TEXT,
    observed_at TEXT, captured_at TEXT, system_id TEXT, resource_id TEXT, collection TEXT, location TEXT,
    presence TEXT, result TEXT, record_type TEXT, pii TEXT, fields TEXT, link_keys TEXT, raw TEXT);
CREATE TABLE obs_keys(obs_id INTEGER, kind TEXT, value TEXT, role TEXT);
CREATE TABLE coverage(evidence_id TEXT, captured_at TEXT, system_id TEXT, collection TEXT, location TEXT,
    record_count INTEGER);
CREATE TABLE activities(activity_id TEXT PRIMARY KEY, event_time TEXT, event_type TEXT, source_system TEXT,
    target_system TEXT, resource_id TEXT, reported_result TEXT, details TEXT, mentioned_resources TEXT,
    emails TEXT, user_refs TEXT, alias TEXT);
CREATE TABLE identities(user_id TEXT PRIMARY KEY, name TEXT, email TEXT, phone TEXT, email_hash TEXT,
    updated_at TEXT);
CREATE TABLE identity_history(audit_id TEXT PRIMARY KEY, user_id TEXT, event_time TEXT, field_name TEXT,
    old_value TEXT, new_value TEXT);
CREATE TABLE requests(case_id TEXT PRIMARY KEY, split TEXT, email TEXT, requested_at TEXT,
    reported_status TEXT, reported_at TEXT, audit_at TEXT, request_text TEXT);
CREATE TABLE rules(rule_id TEXT PRIMARY KEY, data_category TEXT, condition TEXT, required_action TEXT);
CREATE INDEX obs_resource ON observations(resource_id);
CREATE INDEX obs_evidence ON observations(evidence_id);
CREATE INDEX keys_value ON obs_keys(value);
CREATE INDEX keys_obs ON obs_keys(obs_id);
CREATE INDEX act_resource ON activities(resource_id);
"""
DIRECT_PII = ("email", "phone", "name", "hash")


def content_identifiers(obs: Observation) -> set[tuple[str, str]]:
    """Identifiers held inside the record. Names only count from name fields; emails are
    found anywhere, including free text such as support messages."""
    found: set[tuple[str, str]] = set()
    for key, value in obs.fields.items():
        leaf = key.split(".")[-1]
        if key.startswith("_") or leaf in LINK_FIELDS or is_null(value):
            continue
        values = value if isinstance(value, list) else [value]
        for v in values:
            if is_null(v) or not isinstance(v, str):
                continue
            if leaf in NAME_FIELDS:
                found.add(("name", v.strip().lower()))
            elif leaf in PHONE_FIELDS or leaf == "phones":
                found.add(("phone", norm_phone(v)))
            elif leaf in HASH_FIELDS:
                found.add(("hash", v.strip().lower()))
            elif leaf in USER_FIELDS or leaf == "user_id":
                found.update(("user", u) for u in USER_REF_RE.findall(v))
            for email in EMAIL_RE.findall(v):
                found.add(("email", norm_email(email)))
            if leaf not in USER_FIELDS:
                found.update(("user", u) for u in USER_REF_RE.findall(v))
    return found


def link_identifiers(obs: Observation) -> set[tuple[str, str]]:
    found = set()
    for key in obs.link_keys:
        hit = classify(key)
        found.add(hit if hit else ("ref", key))
    return found


class ObservationStore:
    def __init__(self, path: Path):
        self.path = path
        self.con = sqlite3.connect(str(path))
        self.con.row_factory = sqlite3.Row

    # -- build ------------------------------------------------------------------------
    @classmethod
    def build(cls, dataset_dir: Path, path: Path, llm: LLMClient | None = None) -> tuple["ObservationStore", dict]:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            path.unlink()
        store = cls(path)
        store.con.executescript(SCHEMA)
        files = load_manifest(dataset_dir)
        parsed, stats = ingest_evidence(files, llm)
        store._load_tables(dataset_dir)
        store._load_evidence(files, parsed, stats)
        store.con.execute("INSERT INTO meta VALUES('dataset_dir', ?)", (str(dataset_dir),))
        store.con.commit()
        report = {"files": stats, "warnings": parsed.warnings, "unparsed": parsed.unparsed}
        return store, report

    def _load_tables(self, dataset_dir: Path) -> None:
        c = self.con
        c.executemany("INSERT INTO requests VALUES(?,?,?,?,?,?,?,?)", [
            (r["case_id"], r["split"], r["email"], r["requested_at"], r["reported_status"],
             r["reported_at"], r["audit_at"], r["request_text"]) for r in load_requests(dataset_dir)])
        c.executemany("INSERT INTO identities VALUES(?,?,?,?,?,?)", [
            (r["user_id"], r["name"], r["email"], r["phone"], r["email_hash"], r["updated_at"])
            for r in load_identities(dataset_dir)])
        c.executemany("INSERT INTO identity_history VALUES(?,?,?,?,?,?)", [
            (r["audit_id"], r["user_id"], r["event_time"], r["field_name"], r["old_value"], r["new_value"])
            for r in load_identity_history(dataset_dir)])
        c.executemany("INSERT INTO rules VALUES(?,?,?,?)", [
            (r["rule_id"], r["data_category"], r["condition"], r["required_action"])
            for r in load_rules(dataset_dir)])
        c.executemany("INSERT INTO activities VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", [
            (a["activity_id"], a["event_time"], a["event_type"], a["source_system"], a["target_system"],
             a["resource_id"], a["reported_result"], a["details"], json.dumps(a["mentioned_resources"]),
             json.dumps(a["emails"]), json.dumps(a["user_refs"]), a["alias"])
            for a in load_activities(dataset_dir)])

    def _load_evidence(self, files, parsed, stats) -> None:
        c = self.con
        c.executemany("INSERT INTO evidence_files VALUES(?,?,?,?,?,?,?,?)", [
            (ev.file_id, str(ev.path), ev.origin, ev.captured_at, stats[ev.file_id]["format"],
             stats[ev.file_id]["observations"], stats[ev.file_id]["coverage"], stats[ev.file_id]["unparsed"])
            for ev in files])
        for obs in parsed.observations:
            content = content_identifiers(obs)
            pii = sorted({kind for kind, _ in content if kind in DIRECT_PII})
            cur = c.execute(
                "INSERT INTO observations(evidence_id, source_format, parser, observed_at, captured_at, system_id,"
                " resource_id, collection, location, presence, result, record_type, pii, fields, link_keys, raw)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (obs.evidence_id, obs.source_format, obs.parser, obs.observed_at, obs.captured_at, obs.system_id,
                 obs.resource_id, obs.collection, obs.location, obs.presence, obs.result, obs.record_type,
                 json.dumps(pii), json.dumps(obs.fields, ensure_ascii=False, default=str),
                 json.dumps(obs.link_keys, ensure_ascii=False), obs.raw))
            keys = [(k, v, "content") for k, v in content] + [(k, v, "link") for k, v in link_identifiers(obs)]
            if obs.resource_id:
                keys.append(("resource", obs.resource_id, "self"))
            c.executemany("INSERT INTO obs_keys VALUES(?,?,?,?)", [(cur.lastrowid, *k) for k in keys])
        c.executemany("INSERT INTO coverage VALUES(?,?,?,?,?,?)", [
            (cv.evidence_id, cv.captured_at, cv.system_id, cv.collection, cv.location, cv.record_count)
            for cv in parsed.coverage])

    # -- queries ----------------------------------------------------------------------
    def rows(self, sql: str, params=()) -> list[dict]:
        return [dict(r) for r in self.con.execute(sql, params)]

    def observations_for_resource(self, resource_id: str) -> list[dict]:
        return self.rows("SELECT * FROM observations WHERE resource_id=? ORDER BY observed_at, obs_id",
                         (resource_id,))

    def observations_for_key(self, value: str) -> list[dict]:
        hit = classify(value)
        needle = hit[1] if hit else value.strip()
        return self.rows(
            "SELECT o.*, k.role AS matched_role FROM obs_keys k JOIN observations o USING(obs_id)"
            " WHERE k.value=? ORDER BY o.observed_at, o.obs_id", (needle,))

    def observations_for_evidence(self, evidence_id: str) -> list[dict]:
        return self.rows("SELECT * FROM observations WHERE evidence_id=? ORDER BY obs_id", (evidence_id,))

    def activities_for_resource(self, resource_id: str) -> list[dict]:
        return self.rows("SELECT * FROM activities WHERE resource_id=? OR mentioned_resources LIKE ?"
                         " ORDER BY event_time", (resource_id, f'%"{resource_id}"%'))

    def summary(self) -> dict:
        one = lambda sql: self.con.execute(sql).fetchone()[0]
        return {
            "evidence_files": one("SELECT count(*) FROM evidence_files"),
            "observations": one("SELECT count(*) FROM observations"),
            "distinct_resources": one("SELECT count(DISTINCT resource_id) FROM observations"),
            "coverage_rows": one("SELECT count(*) FROM coverage"),
            "activities": one("SELECT count(*) FROM activities"),
            "requests": one("SELECT count(*) FROM requests"),
            "by_format": {r["source_format"]: r["n"] for r in self.rows(
                "SELECT source_format, count(*) n FROM observations GROUP BY 1 ORDER BY 1")},
            "files_without_observations": [r["file_id"] for r in self.rows(
                "SELECT file_id FROM evidence_files WHERE observations=0 AND coverage=0 ORDER BY file_id")],
        }
