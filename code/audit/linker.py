"""Resource linker: which records belong to each case's subject, and why.

Two kinds of link:
  strong   - an identifier of the subject (any current or historical email, hash, phone,
             or the user_id) appears in the record, in the lookup that found it (trace joins),
             or in a data-catalog activity that names the subject.
  workflow - the record carries no identifier any more (deleted, anonymized), but the
             deletion worker acted on it inside this case's request window.

Workflow windows of different cases overlap, so workflow candidates are assigned globally:
records strongly owned by any known user are never given to someone else, each record goes
to one case, and a subject gets at most one record per single-record system. Ambiguous
records go to the case whose own strongly linked worker events sit nearest in the event
sequence (closeness in time alone is a decoy pattern in this dataset).
"""
from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field

from .identifiers import system_for_resource
from .identity import Identity, IdentityResolver
from .store import ObservationStore

WORKER = "deletion_worker"
SINGLE_RECORD_SYSTEMS = {"account_service", "analytics_platform", "identity_service"}
FAR = 10_000
REPORT_SLACK_S = 900            # worker results may land just after the workflow reported
MAX_GAP_S = 7_200               # a workflow's jobs run within two hours of its report
# The workflow's reported status is produced by its own primary-profile job, so the job
# result is a matching feature (never proof of state): COMPLETE<-SUCCESS, and so on.
STATUS_RESULT = {"COMPLETE": "SUCCESS", "PARTIALLY_COMPLETE": "PARTIAL", "NOT_STARTED": "FAILED"}
# Measured on strongly linked worker deletions only (no labels): 253 of 255 match STATUS_RESULT,
# for accounts and for search/export/support deletions alike; the other 2 are PARTIALLY_COMPLETE
# workflows whose job FAILED. Other job types always report SUCCESS and carry no signal.
SOFT_RESULTS = {("PARTIALLY_COMPLETE", "FAILED")}
MISMATCH = 2


def result_penalty(event: dict, reported_status: str) -> int:
    """0 = the job's result is what this workflow reported, 1 = the rare observed variant,
    MISMATCH = contradicts it. Only deletion jobs carry a meaningful result."""
    if event["event_type"] != "DELETE_RECORD":
        return 0
    result = event["reported_result"]
    if result == STATUS_RESULT.get(reported_status):
        return 0
    return 1 if (reported_status, result) in SOFT_RESULTS else MISMATCH


def _seconds(ts: str) -> float:
    from datetime import datetime
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()


@dataclass
class Link:
    resource_id: str
    system_id: str | None
    strength: str                       # strong | workflow
    reasons: list[str] = field(default_factory=list)


@dataclass
class CaseLinks:
    case_id: str
    identity: Identity | None
    request: dict
    links: dict[str, Link] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def add(self, rid: str, strength: str, reason: str) -> None:
        link = self.links.get(rid)
        if link is None:
            self.links[rid] = Link(rid, system_for_resource(rid), strength, [reason])
        else:
            if strength == "strong":
                link.strength = "strong"
            if reason not in link.reasons:
                link.reasons.append(reason)


class Linker:
    def __init__(self, store: ObservationStore, resolver: IdentityResolver | None = None):
        self.store = store
        self.resolver = resolver or IdentityResolver(store)
        self.identities = {uid: self.resolver.for_user(uid) for uid in self.resolver.current}
        self.key_owner: dict[str, str] = {}
        for uid, ident in self.identities.items():
            for key in ident.keys():
                self.key_owner.setdefault(key, uid)
        self.owners = self._strong_owners()
        self.events = store.rows(
            "SELECT * FROM activities WHERE source_system=? ORDER BY event_time, activity_id", (WORKER,))
        for i, ev in enumerate(self.events):
            ev["seq"] = i
            ev["t"] = _seconds(ev["event_time"])

    def _strong_owners(self) -> dict[str, dict[str, list[str]]]:
        """resource_id -> {user_id: [reasons]} from evidence keys and catalog activities."""
        owners: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
        for row in self.store.rows(
                "SELECT o.resource_id, o.evidence_id, k.value, k.role FROM obs_keys k JOIN observations o"
                " USING(obs_id) WHERE o.resource_id IS NOT NULL AND k.role IN ('content','link')"):
            uid = self.key_owner.get(row["value"])
            if uid:
                reason = f"{row['role']}:{row['value']}@{row['evidence_id']}"
                if reason not in owners[row["resource_id"]][uid]:
                    owners[row["resource_id"]][uid].append(reason)
        for act in self.store.rows("SELECT * FROM activities"):
            for value in json.loads(act["emails"]) + json.loads(act["user_refs"]):
                uid = self.key_owner.get(value)
                if uid:
                    owners[act["resource_id"]][uid].append(f"catalog:{value}@{act['activity_id']}")
        return owners

    # -- per case -----------------------------------------------------------------------
    def strong_links(self, case: CaseLinks) -> None:
        if case.identity is None:
            return
        uid = case.identity.user_id
        for rid, by_user in self.owners.items():
            if uid in by_user:
                for reason in by_user[uid]:
                    case.add(rid, "strong", reason)
                others = sorted(set(by_user) - {uid})
                if others:
                    case.notes.append(f"{rid} also strongly linked to {others}")

    def window_events(self, case: CaseLinks) -> list[dict]:
        lo = _seconds(case.request["requested_at"])
        hi = _seconds(case.request["reported_at"]) + REPORT_SLACK_S
        return [ev for ev in self.events if lo <= ev["t"] <= hi]

    def scope_accounts(self, case: CaseLinks) -> None:
        """A subject who re-registered has an older, already-deleted account. Keep the account
        the deletion worker handled in this request's window; drop the others."""
        accounts = [l for l in case.links.values() if l.system_id == "account_service"]
        if len(accounts) < 2:
            return
        in_window = {ev["resource_id"] for ev in self.window_events(case)}
        keep = [l for l in accounts if l.resource_id in in_window]
        if len(keep) == 1:
            for link in accounts:
                if link is not keep[0]:
                    del case.links[link.resource_id]
                    case.notes.append(f"dropped {link.resource_id}: other account handled in this window")

    # -- all cases ----------------------------------------------------------------------
    def link_cases(self, requests: list[dict]) -> dict[str, CaseLinks]:
        cases: dict[str, CaseLinks] = {}
        for req in requests:
            case = CaseLinks(req["case_id"], self.resolver.resolve_email(req["email"]), req)
            if case.identity is None:
                case.notes.append("request email resolves to no known user")
            self.strong_links(case)
            cases[req["case_id"]] = case
        for case in cases.values():
            self.scope_accounts(case)
        self._assign_workflow(cases)
        return cases

    def _assign_workflow(self, cases: dict[str, CaseLinks]) -> None:
        candidates = []          # (score, case_id, rid, activity_id, explanation)
        for case in cases.values():
            if case.identity is None:
                continue
            uid = case.identity.user_id
            window = self.window_events(case)
            own_seq = [ev["seq"] for ev in window if uid in self.owners.get(ev["resource_id"], {})]
            reported = _seconds(case.request["reported_at"])
            expected_result = STATUS_RESULT.get(case.request["reported_status"])
            for ev in window:
                rid = ev["resource_id"]
                owners = self.owners.get(rid, {})
                if uid in owners:
                    case.add(rid, "strong", f"worker:{ev['activity_id']}")
                    continue
                if owners:
                    continue                      # belongs to someone else
                gap = abs(reported - ev["t"])
                if gap > MAX_GAP_S:
                    continue                      # workflow jobs run in the hours before the report
                mismatch = result_penalty(ev, case.request["reported_status"])
                if mismatch == MISMATCH and system_for_resource(rid) == "account_service" and gap > REPORT_SLACK_S:
                    continue                      # a different job's primary-profile result
                distance = min((abs(ev["seq"] - s) for s in own_seq), default=FAR)
                candidates.append(((mismatch, distance, gap), case.case_id, rid, ev["activity_id"],
                                   f"result-penalty={mismatch} seq-distance={distance} gap={gap:.0f}s"))

        taken: dict[str, str] = {}
        slots: set[tuple[str, str]] = set()
        for case in cases.values():
            for link in case.links.values():
                if link.system_id in SINGLE_RECORD_SYSTEMS:
                    slots.add((case.case_id, link.system_id))
        # Same subject may file several requests; they share records, so slots are per user too.
        rivals: dict[str, list[tuple[int, str]]] = defaultdict(list)   # rid -> [(result penalty, case)]
        for score, case_id, rid, _, _ in candidates:
            rivals[rid].append((score[0], case_id))
        assigned: list[tuple[str, str, str, str, int]] = []
        for score, case_id, rid, activity_id, why in sorted(candidates):
            case = cases[case_id]
            system = system_for_resource(rid)
            owner_case = taken.get(rid)
            if owner_case and cases[owner_case].identity.user_id != case.identity.user_id:
                continue
            if (case_id, system) in slots and rid not in case.links:
                continue
            taken.setdefault(rid, case_id)
            if system in SINGLE_RECORD_SYSTEMS:
                slots.add((case_id, system))
            assigned.append((case_id, rid, activity_id, why, score[0]))

        # A link is contested when another subject had the same result evidence and ended without
        # a record of that kind: the evidence cannot tell them apart, so the choice is recorded.
        workflow_slot = {(c, system_for_resource(r)) for c, r, _, _, _ in assigned}
        for case_id, rid, activity_id, why, penalty in assigned:
            system = system_for_resource(rid)
            uid = cases[case_id].identity.user_id
            # a rival is interchangeable if it has no record of this kind, or only one that was
            # itself assigned from the workflow (the two could be swapped)
            contested = sorted({c for p, c in rivals[rid]
                                if p == penalty and cases[c].identity.user_id != uid
                                and (system not in SINGLE_RECORD_SYSTEMS or (c, system) not in slots
                                     or (c, system) in workflow_slot)})
            if contested:
                why += f" contested-with={contested}"
            cases[case_id].add(rid, "workflow", f"worker:{activity_id} {why}")
