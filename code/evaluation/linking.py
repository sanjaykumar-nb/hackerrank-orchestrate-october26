"""Linking quality on the public samples: which resources each case's answer mentions vs
which resources the linker attached to the case. Labels are read here only, never by the
pipeline itself."""
from __future__ import annotations

import csv
import json
from pathlib import Path


def expected_resources(sample_output: Path) -> dict[str, set[str]]:
    with sample_output.open(encoding="utf-8") as fh:
        return {row["case_id"]: {s["resource_id"] for s in json.loads(row["steps_json"])}
                for row in csv.DictReader(fh)}


def link_metrics(cases: dict, sample_output: Path) -> dict:
    expected = expected_resources(sample_output)
    hit = want = got = 0
    per_case = {}
    for case_id, exp in sorted(expected.items()):
        pred = set(cases[case_id].links)
        hit, want, got = hit + len(exp & pred), want + len(exp), got + len(pred)
        if exp != pred:
            per_case[case_id] = {"missing": sorted(exp - pred), "extra": sorted(pred - exp)}
    return {"cases": len(expected), "recall": hit / want, "precision": hit / got if got else 0.0,
            "exact_cases": len(expected) - len(per_case), "mismatches": per_case}
