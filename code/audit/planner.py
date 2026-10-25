"""Assessment -> deletion_status + ordered steps.

COMPLETE:       verification steps only, each citing the evidence that proves the state.
INCOMPLETE:     findings (cited), then the remediation plan and the checks that must pass
                afterwards (both future work, so evidence_id is empty). Verification covers the
                required final state of every relevant record, not only the remediated ones.
NOT_APPLICABLE: no steps.
Order: phase, then the operation order of the contract table, then system_id, resource_id.
That operation order also makes remediation safe: data flows are disabled and restores
blocked before any record they could recreate is deleted, redacted or anonymized.
"""
from __future__ import annotations

from .assess import Assessment

PHASES = ["FINDING", "REMEDIATION", "VERIFICATION"]
OPERATIONS = {
    "FINDING": ["REPORT_REMAINING_RECORD", "REPORT_EXPOSED_PII", "REPORT_ACTIVE_DATA_FLOW", "REPORT_RESTORE_RISK"],
    "REMEDIATION": ["DISABLE_DATA_FLOW", "BLOCK_RESTORE", "DELETE_RECORD", "REDACT_RECORD", "ANONYMIZE_RECORD"],
    "VERIFICATION": ["VERIFY_RECORD_ABSENT", "VERIFY_RECORD_PRESENT", "VERIFY_RECORD_REDACTED",
                     "VERIFY_RECORD_ANONYMIZED", "VERIFY_DATA_FLOW_DISABLED", "VERIFY_RESTORE_BLOCKED"],
}
STATE_OF = {
    "REPORT_REMAINING_RECORD": "PRESENT", "REPORT_EXPOSED_PII": "PRESENT", "REPORT_ACTIVE_DATA_FLOW": "ACTIVE",
    "REPORT_RESTORE_RISK": "ACTIVE", "DISABLE_DATA_FLOW": "DISABLED", "BLOCK_RESTORE": "BLOCKED",
    "DELETE_RECORD": "ABSENT", "REDACT_RECORD": "REDACTED", "ANONYMIZE_RECORD": "ANONYMIZED",
    "VERIFY_RECORD_ABSENT": "ABSENT", "VERIFY_RECORD_PRESENT": "PRESENT", "VERIFY_RECORD_REDACTED": "REDACTED",
    "VERIFY_RECORD_ANONYMIZED": "ANONYMIZED", "VERIFY_DATA_FLOW_DISABLED": "DISABLED",
    "VERIFY_RESTORE_BLOCKED": "BLOCKED",
}
RANK = {(phase, op): (i, j) for i, phase in enumerate(PHASES) for j, op in enumerate(OPERATIONS[phase])}


def step(phase: str, decision, operation: str, evidence_id: str) -> dict:
    return {"phase": phase, "system_id": decision.system_id, "resource_id": decision.resource_id,
            "operation": operation, "state": STATE_OF[operation], "evidence_id": evidence_id}


def plan(assessment: Assessment | None, authorizes_deletion: bool) -> tuple[str, list[dict]]:
    if not authorizes_deletion or assessment is None:
        return "NOT_APPLICABLE", []
    status = assessment.status
    steps = []
    for d in assessment.decisions:
        if status == "INCOMPLETE" and d.violation:
            steps.append(step("FINDING", d, d.finding_op, d.evidence_id))
            steps.append(step("REMEDIATION", d, d.remediation_op, ""))
        steps.append(step("VERIFICATION", d, d.verify_op, d.evidence_id if status == "COMPLETE" else ""))
    steps.sort(key=lambda s: (RANK[(s["phase"], s["operation"])], s["system_id"] or "", s["resource_id"]))
    return status, [{"step_no": i, **s} for i, s in enumerate(steps, 1)]
