"""Custom evaluation cases: controlled edits to a copy of the dataset, each with an expected effect.

Each case changes one thing (a request's wording, one added evidence file) so a pass or fail
points at one behaviour. Expectations are written as properties of the answer (status, steps
that must or must not appear) derived from the contract, not from the pipeline's own output.
"""
from __future__ import annotations

import csv
import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path

REQ_FIELDS = ["case_id", "email", "requested_at", "reported_status", "reported_at", "audit_at", "request_text"]


@dataclass
class CustomCase:
    name: str
    behaviour: str
    case_id: str
    expect_status: str
    include: list[tuple] = field(default_factory=list)   # (phase, resource_id, operation, evidence_id|None)
    exclude_resources: list[str] = field(default_factory=list)
    exact_steps: list[tuple] | None = None                 # full (phase, resource, operation, evidence) list
    edits: list = field(default_factory=list)              # callables(dataset_dir)


def add_request(row: dict):
    def apply(d: Path):
        path = d / "sample_deletion_requests.csv"
        with path.open("a", encoding="utf-8", newline="") as fh:
            csv.DictWriter(fh, REQ_FIELDS, lineterminator="\n").writerow(row)
    return apply


def copy_request(case_id: str, new_id: str, **changes):
    def apply(d: Path):
        with (d / "sample_deletion_requests.csv").open(encoding="utf-8") as fh:
            row = next(r for r in csv.DictReader(fh) if r["case_id"] == case_id)
        add_request({**row, "case_id": new_id, **changes})(d)
    return apply


def add_bundle(file_id: str, captured_at: str, snapshots: list[dict]):
    """A records-assurance style JSON bundle, registered in the manifest."""
    def apply(d: Path):
        rel = f"evidence/records/custom_{file_id}.json"
        (d / rel).write_text(json.dumps({"exported_at": captured_at, "source": "custom_eval",
                                         "snapshots": [{"captured_at": captured_at, **s} for s in snapshots]}),
                             encoding="utf-8")
        with (d / "evidence_files.csv").open("a", encoding="utf-8", newline="") as fh:
            fh.write(f"{file_id},{rel},custom_eval,{captured_at}\n")
    return apply


def add_sql_failover(file_id: str, captured_at: str, rows: list[tuple]):
    """A failover-replica capture of the account tables (primary and replica)."""
    def apply(d: Path):
        rel = f"evidence/databases/custom_{file_id}.sql"
        cols = "resource_id TEXT PRIMARY KEY, user_id TEXT, full_name TEXT, email TEXT, phone TEXT, account_status TEXT, updated_at TEXT"
        lines = [f"-- Captured at: {captured_at}", "-- Database topology: failover_replica", "BEGIN TRANSACTION;"]
        for table in ("customer_profiles_primary", "customer_profiles_failover_replica"):
            lines.append(f"CREATE TABLE {table} ({cols});")
            for r in rows:
                vals = ",".join("'" + str(v).replace("'", "''") + "'" for v in (*r, captured_at))
                lines.append(f'INSERT INTO "{table}" VALUES({vals});')
        lines.append("COMMIT;")
        (d / rel).write_text("\n".join(lines), encoding="utf-8")
        with (d / "evidence_files.csv").open("a", encoding="utf-8", newline="") as fh:
            fh.write(f"{file_id},{rel},custom_eval,{captured_at}\n")
    return apply


DECOY_ROW = ("account_99003", "user_99003", "Karim El-Masri", "karim.elmasri@gmail.com", "+971 50 731 6284", "ACTIVE")

CASES = [
    CustomCase(
        "negated-deletion",
        "A request that mentions deletion only to refuse it must be NOT_APPLICABLE with no steps.",
        "custom_01", "NOT_APPLICABLE", exact_steps=[],
        edits=[copy_request("case_01", "custom_01", request_text=(
            "Please remove me from marketing lists only. I still use this account, so do not delete "
            "or close it."))]),
    CustomCase(
        "later-evidence-ignored",
        "A capture taken after audit_at that shows the ghost replica copy cleaned up must not change "
        "the answer: the copy is still reported as remaining (case_05 at its audit time).",
        "custom_02", "INCOMPLETE",
        include=[("FINDING", "account_005", "REPORT_REMAINING_RECORD", "evidence_54"),
                 ("REMEDIATION", "account_005", "DELETE_RECORD", "")],
        edits=[copy_request("case_05", "custom_02"),
               add_sql_failover("evidence_95", "2026-09-10T00:00:00Z", [DECOY_ROW])]),
    CustomCase(
        "earlier-cleanup-counts",
        "The same clean capture taken before audit_at proves the ghost copy is gone: the case becomes "
        "COMPLETE and cites the new capture.",
        "custom_03", "COMPLETE",
        exact_steps=[("VERIFICATION", "account_005", "VERIFY_RECORD_ABSENT", "evidence_96")],
        edits=[copy_request("case_05", "custom_03"),
               add_sql_failover("evidence_96", "2026-09-07T00:00:00Z", [DECOY_ROW])]),
    CustomCase(
        "similar-email-decoy",
        "A live search entry for a near-identical email (one extra token) belongs to someone else and "
        "must not be attributed to the subject; the answer stays COMPLETE.",
        "custom_04", "COMPLETE", exclude_resources=["search_99004"],
        exact_steps=[("VERIFICATION", "account_001", "VERIFY_RECORD_ABSENT", "evidence_54")],
        edits=[copy_request("case_01", "custom_04"),
               add_bundle("evidence_97", "2026-09-01T00:00:00Z", [{
                   "data_store": "search_service", "collection": "customer_search_documents",
                   "records": [{"resource_id": "search_99004", "index_name": "customer_directory_v3",
                                "document": {"display_name": "Amina El-Masri",
                                             "email": "amina.elmasri.jr@gmail.com"}}]}])]),
    CustomCase(
        "active-connector",
        "An enabled connector still selecting the subject's email can recreate deleted data: the case "
        "is INCOMPLETE, the flow is reported with its evidence, disabled first, then verified disabled.",
        "custom_05", "INCOMPLETE",
        exact_steps=[("FINDING", "data_flow_99005", "REPORT_ACTIVE_DATA_FLOW", "evidence_98"),
                     ("REMEDIATION", "data_flow_99005", "DISABLE_DATA_FLOW", ""),
                     ("VERIFICATION", "account_001", "VERIFY_RECORD_ABSENT", ""),
                     ("VERIFICATION", "data_flow_99005", "VERIFY_DATA_FLOW_DISABLED", "")],
        edits=[copy_request("case_01", "custom_05"),
               add_bundle("evidence_98", "2026-09-02T00:00:00Z", [{
                   "data_store": "integration_service", "collection": "connector_configurations",
                   "records": [{"resource_id": "data_flow_99005", "connector_name": "crm-sync",
                                "destination": "crm", "subject_selector": "amina.elmasri@gmail.com",
                                "enabled": True, "mode": "continuous_sync"}]}])]),
    CustomCase(
        "tombstone-redacted",
        "A tombstone that keeps only the stable user_id is compliant: once a capture shows the PII "
        "removed, the tombstone check passes (case_32's exposed tombstone, later redacted).",
        "custom_06", "COMPLETE",
        include=[("VERIFICATION", "tombstone_9178", "VERIFY_RECORD_REDACTED", "evidence_99"),
                 ("VERIFICATION", "audit_record_7068", "VERIFY_RECORD_PRESENT", None)],
        edits=[copy_request("case_32", "custom_06"),
               add_bundle("evidence_99", "2026-09-15T12:00:00Z", [{
                   "data_store": "identity_service", "collection": "deleted_customer_references",
                   "records": [{"resource_id": "tombstone_9178", "subject_reference": "user_542",
                                "name": None, "email": None, "phone": None, "email_hash": None,
                                "account_status": "DELETED"}]}])]),
]


def build_dataset(source: Path, target: Path, case: CustomCase) -> Path:
    """One isolated copy per case, so cases about the same person cannot see each other's edits."""
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(source, target, ignore=shutil.ignore_patterns("*.png", "*.jpg"))
    for edit in case.edits:
        edit(target)
    return target


def check(case: CustomCase, status: str, steps: list[dict]) -> list[str]:
    problems = []
    if status != case.expect_status:
        problems.append(f"status {status} != {case.expect_status}")
    got = [(s["phase"], s["resource_id"], s["operation"], s["evidence_id"]) for s in steps]
    if case.exact_steps is not None and got != case.exact_steps:
        problems.append(f"steps {got} != {case.exact_steps}")
    for phase, rid, op, ev in case.include:
        if not any(g[:3] == (phase, rid, op) and (ev is None or g[3] == ev) for g in got):
            problems.append(f"missing {phase} {op} {rid} {ev}")
    for rid in case.exclude_resources:
        if any(g[1] == rid for g in got):
            problems.append(f"{rid} must not appear")
    return problems
