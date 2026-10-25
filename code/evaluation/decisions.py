"""Decision quality on the public samples, per record: target state, violation, cited evidence,
and case status. Labels are read here only, never by the pipeline."""
from __future__ import annotations

import csv
import json
from pathlib import Path

VERIFY_TARGET = {"VERIFY_RECORD_ABSENT": "ABSENT", "VERIFY_RECORD_REDACTED": "REDACTED",
                 "VERIFY_RECORD_ANONYMIZED": "ANONYMIZED", "VERIFY_DATA_FLOW_DISABLED": "DISABLED",
                 "VERIFY_RESTORE_BLOCKED": "BLOCKED", "VERIFY_RECORD_PRESENT": "PRESENT"}


def expected(sample_output: Path) -> dict[str, dict]:
    out = {}
    with sample_output.open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            recs: dict[str, dict] = {}
            for s in json.loads(row["steps_json"]):
                r = recs.setdefault(s["resource_id"], {"finding": None, "target": None, "evidence": ""})
                if s["phase"] == "FINDING":
                    r["finding"], r["evidence"] = s["operation"], s["evidence_id"]
                elif s["phase"] == "VERIFICATION":
                    r["target"] = VERIFY_TARGET[s["operation"]]
                    if s["evidence_id"]:
                        r["evidence"] = s["evidence_id"]
            out[row["case_id"]] = {"status": row["deletion_status"], "records": recs}
    return out


def decision_metrics(assessments: dict, sample_output: Path) -> dict:
    exp = expected(sample_output)
    n = {"records": 0, "target": 0, "violation": 0, "evidence": 0, "evidence_n": 0, "status": 0}
    errors = []
    for case_id, e in sorted(exp.items()):
        a = assessments[case_id]
        n["status"] += a.status == e["status"]
        if a.status != e["status"]:
            errors.append(f"{case_id}: status {a.status} != {e['status']}")
        mine = {d.resource_id: d for d in a.decisions}
        for rid, want in e["records"].items():
            d = mine.get(rid)
            if d is None:
                continue
            n["records"] += 1
            ok_t = d.target == want["target"]
            ok_v = d.finding_op == want["finding"]
            n["target"] += ok_t
            n["violation"] += ok_v
            if want["evidence"]:
                n["evidence_n"] += 1
                n["evidence"] += d.evidence_id == want["evidence"]
            if not ok_t or not ok_v or (want["evidence"] and d.evidence_id != want["evidence"]):
                errors.append(f"{case_id} {rid}: target {d.target}/{want['target']} finding "
                              f"{d.finding_op}/{want['finding']} evidence {d.evidence_id}/{want['evidence']} "
                              f"[{d.category}; {d.reason}]")
    return {"cases": len(exp), "status_accuracy": n["status"] / len(exp),
            "records_compared": n["records"],
            "target_accuracy": n["target"] / max(1, n["records"]),
            "finding_accuracy": n["violation"] / max(1, n["records"]),
            "evidence_accuracy": n["evidence"] / max(1, n["evidence_n"]), "errors": errors}
