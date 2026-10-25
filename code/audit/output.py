"""Write output.csv deterministically and validate it against the submission contract."""
from __future__ import annotations

import csv
import json
import re
from pathlib import Path

from .planner import OPERATIONS, PHASES, STATE_OF

HEADER = ["case_id", "deletion_status", "steps_json"]
STEP_KEYS = {"step_no", "phase", "system_id", "resource_id", "operation", "state", "evidence_id"}
STATUSES = {"COMPLETE", "INCOMPLETE", "NOT_APPLICABLE"}


def case_sort_key(case_id: str):
    m = re.search(r"(\d+)$", case_id)
    return (int(m.group(1)) if m else 0, case_id)


def serialize(steps: list[dict]) -> str:
    return json.dumps(steps, ensure_ascii=False, separators=(",", ":"))


def write_output(path: Path, rows: dict[str, tuple[str, list[dict]]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow(HEADER)
        for case_id in sorted(rows, key=case_sort_key):
            status, steps = rows[case_id]
            writer.writerow([case_id, status, serialize(steps)])


def validate_output(path: Path, requests: list[dict], evidence_times: dict[str, str],
                    activity_times: dict[str, str], audit_times: dict[str, str]) -> list[str]:
    """Returns a list of contract violations (empty when the file is valid)."""
    errors: list[str] = []
    with path.open(encoding="utf-8", newline="") as fh:
        reader = csv.reader(fh)
        header = next(reader, None)
        data = list(reader)
    if header != HEADER:
        errors.append(f"header {header} != {HEADER}")
    by_case = {r["case_id"]: r for r in requests}
    ids = [row[0] for row in data]
    if len(ids) != len(by_case) or set(ids) != set(by_case):
        errors.append(f"{len(ids)} rows for {len(by_case)} requests; missing {sorted(set(by_case) - set(ids))[:5]} "
                      f"extra {sorted(set(ids) - set(by_case))[:5]}")
    if ids != sorted(ids, key=case_sort_key):
        errors.append("rows are not sorted by case_id")
    if len(ids) != len(set(ids)):
        errors.append("duplicate case_id rows")
    cited_time = {**evidence_times, **activity_times, **audit_times}
    for row in data:
        if len(row) != 3:
            errors.append(f"{row[:1]}: expected 3 columns")
            continue
        case_id, status, raw = row
        if status not in STATUSES:
            errors.append(f"{case_id}: bad status {status}")
        try:
            steps = json.loads(raw)
        except json.JSONDecodeError:
            errors.append(f"{case_id}: steps_json is not JSON")
            continue
        if not isinstance(steps, list):
            errors.append(f"{case_id}: steps_json is not an array")
            continue
        if status == "NOT_APPLICABLE" and steps:
            errors.append(f"{case_id}: NOT_APPLICABLE must have []")
        if status != "NOT_APPLICABLE" and not steps:
            errors.append(f"{case_id}: {status} without steps")
        last_phase = 0
        audit_at = by_case.get(case_id, {}).get("audit_at", "")
        for i, s in enumerate(steps, 1):
            where = f"{case_id} step {i}"
            if set(s) != STEP_KEYS:
                errors.append(f"{where}: keys {sorted(s)}")
                continue
            if s["step_no"] != i:
                errors.append(f"{where}: step_no {s['step_no']}")
            if s["phase"] not in PHASES or s["operation"] not in OPERATIONS[s["phase"]]:
                errors.append(f"{where}: operation {s['operation']} not allowed in {s['phase']}")
                continue
            if STATE_OF[s["operation"]] != s["state"]:
                errors.append(f"{where}: state {s['state']} does not match {s['operation']}")
            phase = PHASES.index(s["phase"])
            if phase < last_phase:
                errors.append(f"{where}: phase order")
            last_phase = phase
            if status == "COMPLETE" and s["phase"] != "VERIFICATION":
                errors.append(f"{where}: COMPLETE may only contain verification steps")
            future = status == "INCOMPLETE" and s["phase"] != "FINDING"
            ev = s["evidence_id"]
            if future and ev:
                errors.append(f"{where}: future step must have empty evidence_id")
            if not future and ev:
                if ev not in cited_time:
                    errors.append(f"{where}: unknown evidence {ev}")
                elif cited_time[ev] > audit_at:
                    errors.append(f"{where}: cites {ev} captured after audit_at")
            if not future and not ev:
                errors.append(f"{where}: missing evidence_id")
    return errors
