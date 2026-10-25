"""State of a record at a case's audit_at, with the evidence item that proves it.

Rules:
- Eligibility uses the manifest capture time: nothing captured after audit_at is used.
- A record can live in several locations (primary and replicas). Each location's latest
  verdict counts; the record is present if any location still holds it, which is how a
  replica or failover ghost copy is caught after the primary was cleaned.
- Absence is proven by an explicit sighting (404, NOT_FOUND, empty hits) or by a full
  capture of the system in that location that does not contain the record. SQL dumps are
  full captures. Assurance bundles and JSON collections only cover the subjects they include,
  so their implied absence counts only for a subject that appears in them.
- Activity rows are claims; they are used only when no evidence item speaks about the record.
"""
from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field

from .identifiers import system_for_resource
from .store import ObservationStore

IDENTIFIABLE_RESULTS = {"IDENTIFIABLE", "FOUND_WITH_PII", "EXPOSED"}
CLEAN_RESULTS = {"ANONYMIZED", "REDACTED"}
# Activity types that claim a resulting state. Catalog links, classifications and exports
# say who a record belongs to or that it was touched, not what state it is in.
STATE_EVENTS = {"DELETE_RECORD", "ANONYMIZE_RECORD", "REDACT_RECORD", "DISABLE_DATA_FLOW", "BLOCK_RESTORE",
                "SYNC_RECORD", "RESTORE_BACKUP", "CHECK_DATA_FLOW", "CHECK_RESTORE_ACCESS"}
# Status words used by consoles and logs for "this process can still move/restore data".
RUNNING_WORDS = {"ACTIVE", "HEALTHY", "RUNNING", "ENABLED", "LIVE", "SYNCING", "CONNECTED", "SCHEDULED",
                 "RESTORE ALLOWED", "ALLOWED", "ALLOW", "ELIGIBLE", "RESTORABLE"}
STOPPED_WORDS = {"DISABLED", "PAUSED", "STOPPED", "INACTIVE", "BLOCKED", "DENIED", "DENY", "REVOKED",
                 "RESTORE BLOCKED", "SUSPENDED"}
# Systems whose records are retained by policy: their absence must be shown explicitly, because
# captures that list some security or legal records are not complete inventories of them.
EXPLICIT_ABSENCE_SYSTEMS = {"security_service", "legal_service"}
PII_FIELD_NAMES = {"email", "phone", "full_name", "name", "email_hash", "requester_email", "owner_email",
                   "customer_email", "display_name", "message_text", "message_excerpt", "requester_name",
                   "requester_phone", "owner_phone"}


@dataclass
class RecordState:
    resource_id: str
    system_id: str | None
    presence: str | None = None            # PRESENT | ABSENT | None (no eligible evidence)
    evidence_id: str = ""
    as_of: str | None = None
    location: str | None = None
    pii: set[str] = field(default_factory=set)       # direct identifiers still in the record
    pii_known: bool = False
    user_ref: bool = False                 # stable user reference still in the record
    result: str | None = None
    record_type: str | None = None
    active: bool | None = None             # data flows: can copy data
    restorable: bool | None = None         # backups: can restore data
    retention_until: str | None = None
    signals: dict = field(default_factory=dict)       # fields of the deciding evidence
    traits: dict = field(default_factory=lambda: {"keys": set(), "events": set(), "record_types": set(),
                                                  "collections": set()})   # across all eligible evidence
    history: list[tuple[str, str, str, str]] = field(default_factory=list)  # (time, evidence, location, verdict)
    source: str = ""                       # evidence | coverage | activity | none


def _truthy(value) -> bool | None:
    if value in (True, 1, "1", "true", "True"):
        return True
    if value in (False, 0, "0", "false", "False"):
        return False
    return None


def location_key(location: str | None) -> str:
    """Replica copies are tracked separately; every other view reads the primary."""
    loc = (location or "").lower()
    return loc if "replica" in loc else "primary"


class StateReconstructor:
    def __init__(self, store: ObservationStore):
        self.store = store
        self.manifest = {r["file_id"]: r for r in store.rows("SELECT * FROM evidence_files")}
        self.by_resource: dict[str, list[dict]] = defaultdict(list)
        for row in store.rows("SELECT * FROM observations WHERE resource_id IS NOT NULL ORDER BY obs_id"):
            row["fields"] = json.loads(row["fields"])
            row["pii"] = json.loads(row["pii"])
            self.by_resource[row["resource_id"]].append(row)
        self.coverage: dict[str, list[dict]] = defaultdict(list)
        for row in store.rows("SELECT * FROM coverage"):
            row["full"] = self.manifest[row["evidence_id"]]["format"] == "sql"
            self.coverage[row["system_id"]].append(row)
        self.user_content = {r["obs_id"] for r in store.rows(
            "SELECT DISTINCT obs_id FROM obs_keys WHERE kind='user' AND role='content'")}
        self.activities: dict[str, list[dict]] = defaultdict(list)
        for act in store.rows("SELECT * FROM activities ORDER BY event_time, activity_id"):
            self.activities[act["resource_id"]].append(act)
            for rid in json.loads(act["mentioned_resources"]):
                self.activities[rid].append(act)

    def captured(self, evidence_id: str) -> str:
        return self.manifest[evidence_id]["captured_at"]

    def subject_evidence(self, resource_ids, keys) -> set[str]:
        """Evidence items that mention the subject (any linked record or identifier)."""
        found = {o["evidence_id"] for rid in resource_ids for o in self.by_resource.get(rid, [])}
        keys = list(keys)
        for i in range(0, len(keys), 500):
            chunk = keys[i:i + 500]
            found |= {r["evidence_id"] for r in self.store.rows(
                f"SELECT DISTINCT o.evidence_id FROM obs_keys k JOIN observations o USING(obs_id)"
                f" WHERE k.value IN ({','.join('?' * len(chunk))})", chunk)}
        return found

    def state_at(self, resource_id: str, audit_at: str, subject_evidence: set[str] | None = None) -> RecordState:
        system = system_for_resource(resource_id)
        st = RecordState(resource_id, system)
        rows = [o for o in self.by_resource.get(resource_id, []) if self.captured(o["evidence_id"]) <= audit_at]
        # (time, explicit, evidence_id, location, presence, rows)
        verdicts = []
        per_file: dict[tuple[str, str], list[dict]] = defaultdict(list)
        for o in rows:
            per_file[(o["evidence_id"], location_key(o["location"]))].append(o)
            self._traits(st, o)
        for (ev, loc), obs in per_file.items():
            presence = "PRESENT" if any(o["presence"] == "PRESENT" for o in obs) else "ABSENT"
            verdicts.append((self.captured(ev), 1, ev, loc, presence, obs))
        seen_files = {ev for ev, _ in per_file}
        for cov in ([] if system in EXPLICIT_ABSENCE_SYSTEMS else self.coverage.get(system, [])):
            ev = cov["evidence_id"]
            if ev in seen_files or self.captured(ev) > audit_at:
                continue
            if not cov["full"] and (subject_evidence is None or ev not in subject_evidence):
                continue
            verdicts.append((self.captured(ev), 0, ev, location_key(cov["location"]), "ABSENT", []))
        for t, explicit, ev, loc, presence, _ in sorted(verdicts):
            st.history.append((t, ev, loc, presence if explicit else "ABSENT(coverage)"))
        if not verdicts:
            acts = [a for a in self.activities.get(resource_id, [])
                    if a["event_time"] <= audit_at and a["event_type"] in STATE_EVENTS]
            if acts:
                self._apply_activity(st, acts[-1])
            else:
                st.source = "none"
            return st

        latest: dict[str, tuple] = {}
        for v in sorted(verdicts, key=lambda v: (v[0], v[1], v[2])):
            latest[v[3]] = v
        present = [v for v in latest.values() if v[4] == "PRESENT"]
        if present:
            chosen = max(present, key=lambda v: (v[0], v[1], v[2]))
        else:
            # Absent everywhere: cite the latest explicit sighting if nothing contradicts it,
            # otherwise the latest full capture.
            last_seen = max((v[0] for v in verdicts if v[4] == "PRESENT"), default="")
            explicit = [v for v in verdicts if v[1] and v[4] == "ABSENT" and v[0] >= last_seen]
            chosen = max(explicit or list(latest.values()), key=lambda v: (v[0], v[1], v[2]))
        self._apply(st, chosen)
        return st

    @staticmethod
    def _traits(st: RecordState, o: dict) -> None:
        st.traits["keys"] |= {k.split(".")[-1] for k, v in o["fields"].items() if not k.startswith("_")}
        if o["fields"].get("_event"):
            st.traits["events"].add(o["fields"]["_event"])
        if o["record_type"]:
            st.traits["record_types"].add(o["record_type"])
        if o["collection"]:
            st.traits["collections"].add(o["collection"])

    def _apply(self, st: RecordState, verdict: tuple) -> None:
        t, explicit, ev, loc, presence, rows = verdict
        st.evidence_id, st.as_of, st.location, st.presence = ev, t, loc, presence
        st.source = "evidence" if explicit else "coverage"
        for r in [r for r in rows if r["presence"] == presence] or rows:
            f = r["fields"]
            st.signals.update({k: val for k, val in f.items() if val is not None})
            st.pii |= set(r["pii"])
            st.pii_known |= any(k.split(".")[-1] in PII_FIELD_NAMES for k in f)
            st.user_ref |= r["obs_id"] in self.user_content
            st.result = r["result"] or st.result
            st.record_type = r["record_type"] or st.record_type
        if st.result in IDENTIFIABLE_RESULTS:
            st.pii.add("unspecified")
            st.pii_known = True
        elif st.result in CLEAN_RESULTS:
            st.pii_known = True
        s = st.signals
        word = (st.result or str(s.get("status") or "")).upper().strip()
        word_state = True if word in RUNNING_WORDS else False if word in STOPPED_WORDS else None
        if st.system_id == "integration_service":
            if _truthy(s.get("enabled")) is not None:
                st.active = _truthy(s.get("enabled"))
            elif word_state is not None:
                st.active = word_state
        if st.system_id == "backup_service":
            decision = str(s.get("decision") or "").upper()
            if _truthy(s.get("restore_allowed")) is not None:
                st.restorable = _truthy(s.get("restore_allowed"))
            elif word in ("ACTIVE", "BLOCKED"):
                st.restorable = word == "ACTIVE"
            elif decision in ("ALLOW", "ALLOWED", "DENY", "DENIED"):
                st.restorable = decision.startswith("ALLOW")
            elif _truthy(s.get("deletion_block")) is not None:
                st.restorable = not _truthy(s.get("deletion_block"))
            elif _truthy(s.get("eligible")) is not None:
                st.restorable = _truthy(s.get("eligible"))
            elif word_state is not None:
                st.restorable = word_state
        st.retention_until = s.get("retention_until") or s.get("retained_until")

    def _apply_activity(self, st: RecordState, act: dict) -> None:
        """Fallback when no evidence item mentions the record: the latest activity claim."""
        st.evidence_id, st.as_of, st.source = act["activity_id"], act["event_time"], "activity"
        ok = act["reported_result"] == "SUCCESS"
        st.result = f"{act['event_type']}:{act['reported_result']}"
        kind = act["event_type"]
        if kind == "DELETE_RECORD":
            st.presence = "ABSENT" if ok else "PRESENT"
        elif kind == "DISABLE_DATA_FLOW":
            st.presence, st.active = "PRESENT", not ok
        elif kind == "BLOCK_RESTORE":
            st.presence, st.restorable = "PRESENT", not ok
        elif kind in ("SYNC_RECORD", "CHECK_DATA_FLOW") and st.system_id == "integration_service":
            st.presence, st.active = "PRESENT", kind == "SYNC_RECORD"
        else:
            st.presence = "PRESENT"
        st.history.append((act["event_time"], act["activity_id"], "activity", st.result))
