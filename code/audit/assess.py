"""Per-case assessment: linked records -> state at audit_at -> policy decisions."""
from __future__ import annotations

from dataclasses import dataclass, field

from .linker import CaseLinks
from .policy import Decision, PolicyEngine
from .state import EXPLICIT_ABSENCE_SYSTEMS, StateReconstructor

# The primary account must always be verified absent, and records policy requires to keep
# must always be verified present, even when no evidence speaks about them by audit_at.
ALWAYS_PLANNED = {"account_service"} | EXPLICIT_ABSENCE_SYSTEMS


@dataclass
class Assessment:
    case_id: str
    audit_at: str
    decisions: list[Decision] = field(default_factory=list)
    dropped: list[tuple[str, str]] = field(default_factory=list)

    @property
    def violations(self) -> list[Decision]:
        return [d for d in self.decisions if d.violation]

    @property
    def unverified(self) -> list[Decision]:
        return [d for d in self.decisions if d.current == "UNKNOWN"]

    @property
    def status(self) -> str:
        # COMPLETE must be proven: every required state needs evidence at audit_at.
        return "INCOMPLETE" if self.violations or self.unverified else "COMPLETE"


def assess(case: CaseLinks, states: StateReconstructor, policy: PolicyEngine) -> Assessment:
    audit_at = case.request["audit_at"]
    out = Assessment(case.case_id, audit_at)
    keys = case.identity.keys() if case.identity else {}
    scope = states.subject_evidence(case.links, keys)
    for rid, link in sorted(case.links.items()):
        st = states.state_at(rid, audit_at, scope)
        # A record nothing speaks about by audit_at (only a catalog link, or evidence captured
        # later) cannot be reported or verified for this case. The primary account is the
        # exception: its absence is always the first thing a deletion audit must verify.
        if st.presence is None and link.system_id not in ALWAYS_PLANNED:
            out.dropped.append((rid, "no evidence at or before audit_at"))
            continue
        out.decisions.append(policy.decide(st, audit_at))
    if not out.decisions and out.dropped:
        # Nothing verifiable is linked: keep the linked records as unverified requirements
        # rather than claim a completion that no evidence supports.
        for rid, _ in out.dropped:
            out.decisions.append(policy.decide(states.state_at(rid, audit_at, scope), audit_at))
        out.dropped = []
    return out
