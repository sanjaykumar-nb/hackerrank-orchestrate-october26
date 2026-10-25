"""CSV inputs: requests, identities, identity history, activity stream, rules, evidence manifest."""
from __future__ import annotations

import csv
import re
from pathlib import Path

from ..identifiers import EMAIL_RE, RESOURCE_RE, USER_REF_RE, norm_email
from ..model import EvidenceFile

ALIAS_RE = re.compile(r"(" + RESOURCE_RE.pattern + r")\s*\(([^)]+)\)")


def read_csv(path: Path) -> list[dict]:
    with path.open(encoding="utf-8", newline="") as fh:
        return [dict(row) for row in csv.DictReader(fh)]


def load_manifest(dataset_dir: Path) -> list[EvidenceFile]:
    root = dataset_dir.resolve()
    files = []
    for row in read_csv(dataset_dir / "evidence_files.csv"):
        path = (dataset_dir / row["file_path"]).resolve()
        if root not in path.parents:
            raise ValueError(f"{row['file_id']}: manifest path escapes the dataset directory")
        files.append(EvidenceFile(row["file_id"], path, row["origin"], row["captured_at"]))
    return files


def load_requests(dataset_dir: Path) -> list[dict]:
    rows = [{**r, "split": "eval"} for r in read_csv(dataset_dir / "deletion_requests.csv")]
    sample = dataset_dir / "sample_deletion_requests.csv"
    if sample.exists():
        rows += [{**r, "split": "sample"} for r in read_csv(sample)]
    return rows


def load_identities(dataset_dir: Path) -> list[dict]:
    return read_csv(dataset_dir / "user_identities.csv")


def load_identity_history(dataset_dir: Path) -> list[dict]:
    return read_csv(dataset_dir / "user_audit_log.csv")


def load_rules(dataset_dir: Path) -> list[dict]:
    return read_csv(dataset_dir / "deletion_rules.csv")


def load_activities(dataset_dir: Path) -> list[dict]:
    """Activity rows plus what their free-text details mention. Results stay claims."""
    rows = []
    for row in read_csv(dataset_dir / "system_activity.csv"):
        details = row.get("details") or ""
        mentioned = [r for r in dict.fromkeys(RESOURCE_RE.findall(details)) if r != row["resource_id"]]
        alias = ALIAS_RE.search(details)
        rows.append({
            **row,
            "mentioned_resources": mentioned,
            "emails": sorted({norm_email(e) for e in EMAIL_RE.findall(details)}),
            "user_refs": sorted(set(USER_REF_RE.findall(details))),
            "alias": alias.group(2).strip() if alias and alias.group(2).strip() != alias.group(1) else None,
        })
    return rows
