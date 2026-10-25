"""Policy engine: data category -> rule in deletion_rules.csv -> target state -> violation.

Rules are read from the CSV at runtime; this module only maps evidence to categories and
evaluates each rule's condition against the record's state at audit_at.
"""
from __future__ import annotations

from dataclasses import dataclass

from .state import RecordState
from .store import ObservationStore

ACTION_TARGET = {"DELETE": "ABSENT", "REDACT": "REDACTED", "ANONYMIZE": "ANONYMIZED", "RETAIN": "PRESENT"}
DATA_FLOW = "DATA_FLOW"           # not a rule row: no active sync path may recreate data
SYSTEM_CATEGORY = {
    "account_service": "USER_PROFILE",
    "search_service": "SEARCH_INDEX_ENTRY",
    "cache_service": "CACHE_ENTRY",
    "export_service": "USER_EXPORT",
    "marketing_platform": "MARKETING_PROFILE",
    "object_storage": "SUPPORT_ATTACHMENT",
    "analytics_platform": "ANALYTICS_PROFILE",
    "email_delivery_service": "EMAIL_DELIVERY_LOG",
    "security_service": "SECURITY_AUDIT_LOG",
    "legal_service": "LEGAL_HOLD_RECORD",
    "backup_service": "BACKUP_ARCHIVE",
    "identity_service": "ACCOUNT_TOMBSTONE",
    "integration_service": DATA_FLOW,
}
MESSAGE_SIGNALS = {"message_text", "message_excerpt", "channel", "detector", "matched_value"}
MARKETING_SIGNALS = {"email_opt_in", "marketing_opt_in", "campaign", "subscription_status"}

# operation names by target state
FINDING = {"ABSENT": "REPORT_REMAINING_RECORD", "REDACTED": "REPORT_EXPOSED_PII",
           "ANONYMIZED": "REPORT_EXPOSED_PII", "DISABLED": "REPORT_ACTIVE_DATA_FLOW",
           "BLOCKED": "REPORT_RESTORE_RISK", "PRESENT": "REPORT_EXPOSED_PII"}
REMEDIATION = {"ABSENT": "DELETE_RECORD", "REDACTED": "REDACT_RECORD", "ANONYMIZED": "ANONYMIZE_RECORD",
               "DISABLED": "DISABLE_DATA_FLOW", "BLOCKED": "BLOCK_RESTORE", "PRESENT": "REDACT_RECORD"}
VERIFY = {"ABSENT": "VERIFY_RECORD_ABSENT", "REDACTED": "VERIFY_RECORD_REDACTED",
          "ANONYMIZED": "VERIFY_RECORD_ANONYMIZED", "DISABLED": "VERIFY_DATA_FLOW_DISABLED",
          "BLOCKED": "VERIFY_RESTORE_BLOCKED", "PRESENT": "VERIFY_RECORD_PRESENT"}


@dataclass
class Decision:
    resource_id: str
    system_id: str | None
    category: str
    rule_id: str | None
    target: str                   # ABSENT | REDACTED | ANONYMIZED | PRESENT | DISABLED | BLOCKED
    current: str                  # same vocabulary plus ACTIVE / EXPOSED / UNKNOWN
    violation: bool
    evidence_id: str
    reason: str
    state: RecordState

    @property
    def finding_op(self) -> str | None:
        return FINDING[self.target] if self.violation else None

    @property
    def remediation_op(self) -> str | None:
        return REMEDIATION[self.target] if self.violation else None

    @property
    def verify_op(self) -> str:
        return VERIFY[self.target]


class PolicyEngine:
    def __init__(self, store: ObservationStore):
        self.rules = {r["data_category"]: r for r in store.rows("SELECT * FROM rules")}

    def categorize(self, st: RecordState) -> str:
        known = [t for t in sorted(st.traits["record_types"] | {st.record_type} - {None})
                 if t in self.rules or t == DATA_FLOW]
        if known:
            return st.record_type if st.record_type in known else known[0]
        keys = st.traits["keys"] | {k.split(".")[-1] for k in st.signals}
        events = " ".join(st.traits["events"])
        collections = " ".join(st.traits["collections"])
        if st.system_id == "support_platform":
            if "attachment" in collections or "attachment" in events or "document" in collections:
                return "SUPPORT_ATTACHMENT"
            if keys & MESSAGE_SIGNALS or "message" in events or "ticket_read" in events \
                    or "conversation" in collections or "ticket" in collections:
                return "SUPPORT_MESSAGE"
            return "SUPPORT_ATTACHMENT"
        if st.system_id == "analytics_platform" and (keys & MARKETING_SIGNALS or "contact_lookup" in events):
            return "MARKETING_PROFILE"
        return SYSTEM_CATEGORY.get(st.system_id or "", "USER_PROFILE")

    def decide(self, st: RecordState, audit_at: str) -> Decision:
        category = self.categorize(st)
        rule = self.rules.get(category)
        rule_id = rule["rule_id"] if rule else None
        if category == DATA_FLOW:
            target = "DISABLED"
        elif category == "BACKUP_ARCHIVE":
            target = "BLOCKED"            # retained, with restore access blocked
        else:
            target = ACTION_TARGET[rule["required_action"]]
            if target == "PRESENT" and st.retention_until and st.retention_until < audit_at:
                target, rule_id = "ABSENT", f"{rule_id}:retention-expired"
        current, violation, reason = self._evaluate(st, target)
        return Decision(st.resource_id, st.system_id, category, rule_id, target, current, violation,
                        st.evidence_id, reason, st)

    @staticmethod
    def _evaluate(st: RecordState, target: str) -> tuple[str, bool, str]:
        if st.presence is None:
            return "UNKNOWN", False, "no eligible evidence about this record"
        present = st.presence == "PRESENT"
        exposed = bool(st.pii)
        if target == "ABSENT":
            return ("PRESENT", True, "record still exists") if present else ("ABSENT", False, "absent")
        if target == "DISABLED":
            if not present or st.active is False:
                return "DISABLED", False, "flow inactive"
            return ("ACTIVE", True, "flow active") if st.active else ("UNKNOWN", False, "flow state not shown")
        if target == "BLOCKED":
            if st.restorable:
                return "ACTIVE", True, "restore allowed"
            return ("BLOCKED", False, "restore blocked") if st.restorable is False else ("UNKNOWN", False, "restore state not shown")
        if target == "PRESENT":
            if not present:
                return "ABSENT", False, "retained record missing (cannot be restored by remediation)"
            return ("EXPOSED", True, "retained record holds direct identifiers") if exposed else ("PRESENT", False, "retained")
        if not present:
            return "ABSENT", False, "record no longer exists"
        if target == "REDACTED":
            return ("EXPOSED", True, f"direct identifiers present: {sorted(st.pii)}") if exposed else ("REDACTED", False, "no direct identifiers")
        # ANONYMIZED: no direct identifiers and no stable user reference either
        if exposed or st.user_ref:
            return "EXPOSED", True, f"still attributable: pii={sorted(st.pii)} user_ref={st.user_ref}"
        return "ANONYMIZED", False, "not attributable"
