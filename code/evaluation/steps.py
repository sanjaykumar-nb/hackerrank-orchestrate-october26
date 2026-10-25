"""Full-answer scoring against labelled cases (the public samples or custom cases).

Metrics:
  status_accuracy   deletion_status exact match
  step P/R/F1       steps as tuples (phase, system, resource, operation, state, evidence);
                    also a relaxed variant without evidence_id
  field_accuracy    gold steps aligned to predicted steps by (phase, resource, operation),
                    falling back to (phase, resource); each field scored, a missing step scores 0
  sequence          exact steps_json array match, and mean normalized edit similarity
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

FIELDS = ["phase", "system_id", "resource_id", "operation", "state", "evidence_id"]


def load_answers(path: Path) -> dict[str, tuple[str, list[dict]]]:
    with path.open(encoding="utf-8") as fh:
        return {r["case_id"]: (r["deletion_status"], json.loads(r["steps_json"])) for r in csv.DictReader(fh)}


def _tuple(step: dict, with_evidence: bool = True) -> tuple:
    return tuple(step[f] for f in FIELDS if with_evidence or f != "evidence_id")


def _edit_similarity(a: list, b: list) -> float:
    if not a and not b:
        return 1.0
    prev = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        cur = [i]
        for j, y in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (x != y)))
        prev = cur
    return 1 - prev[-1] / max(len(a), len(b))


def _prf(tp: int, n_pred: int, n_gold: int) -> dict:
    p = tp / n_pred if n_pred else 1.0
    r = tp / n_gold if n_gold else 1.0
    return {"precision": p, "recall": r, "f1": 2 * p * r / (p + r) if p + r else 0.0}


def score(predicted: dict[str, tuple[str, list[dict]]], gold: dict[str, tuple[str, list[dict]]]) -> dict:
    status_ok = exact = 0
    tp = tp_relaxed = n_pred = n_gold = 0
    field_hits = {f: 0 for f in FIELDS}
    similarity = 0.0
    failures = []
    for case_id, (g_status, g_steps) in sorted(gold.items()):
        p_status, p_steps = predicted.get(case_id, ("MISSING", []))
        status_ok += p_status == g_status
        g_t = [_tuple(s) for s in g_steps]
        p_t = [_tuple(s) for s in p_steps]
        exact += p_status == g_status and g_t == p_t
        similarity += _edit_similarity(p_t, g_t)
        n_pred, n_gold = n_pred + len(p_t), n_gold + len(g_t)
        tp += len(set(g_t) & set(p_t))
        tp_relaxed += len({_tuple(s, False) for s in g_steps} & {_tuple(s, False) for s in p_steps})
        by_op = {(s["phase"], s["resource_id"], s["operation"]): s for s in p_steps}
        by_res = {(s["phase"], s["resource_id"]): s for s in p_steps}
        for g in g_steps:
            p = by_op.get((g["phase"], g["resource_id"], g["operation"])) or by_res.get((g["phase"], g["resource_id"]))
            for f in FIELDS:
                field_hits[f] += bool(p) and p[f] == g[f]
        if p_status != g_status or g_t != p_t:
            failures.append({"case_id": case_id, "status": [p_status, g_status],
                             "missing": sorted(set(g_t) - set(p_t)), "extra": sorted(set(p_t) - set(g_t))})
    n = len(gold)
    return {
        "cases": n,
        "status_accuracy": status_ok / n,
        "step_exact": _prf(tp, n_pred, n_gold),
        "step_relaxed_no_evidence": _prf(tp_relaxed, n_pred, n_gold),
        "field_accuracy": {f: field_hits[f] / max(1, n_gold) for f in FIELDS},
        "sequence_exact_match": exact / n,
        "sequence_edit_similarity": similarity / n,
        "failures": failures,
    }
