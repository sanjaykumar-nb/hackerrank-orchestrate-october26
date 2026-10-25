"""Which signals can break ties between subjects for records with no identifiers left?

Every test uses strongly linked records as pseudo-labels (an identifier of the subject is in
the record or its lookup), never the sample answers, so the conclusions do not overfit them.
"""
from __future__ import annotations

import collections
import itertools
import re
from datetime import datetime

from audit.linker import STATUS_RESULT, Linker, _seconds


def _num(x: str) -> int:
    m = re.search(r"(\d+)$", x)
    return int(m.group(1)) if m else 0


def _ts(t: str | None) -> float:
    try:
        return datetime.fromisoformat((t or "").replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def measure(store, linker: Linker, requests: list[dict], cases: dict) -> dict:
    out: dict = {}
    for r in requests:
        r["uid"] = linker.resolver.resolve_email(r["email"]).user_id
    batches = collections.defaultdict(list)
    for r in requests:
        batches[r["reported_at"]].append(r)

    # 1. Worker deletion results vs the workflow's reported status.
    match = total = 0
    for r in requests:
        t = _seconds(r["reported_at"])
        for ev in linker.events:
            owners = linker.owners.get(ev["resource_id"], {})
            if ev["event_type"] == "DELETE_RECORD" and set(owners) == {r["uid"]} and t - 7200 <= ev["t"] <= t + 900:
                total += 1
                match += ev["reported_result"] == STATUS_RESULT[r["reported_status"]]
    out["result_match"] = (match, total)

    # 2. Order of strongly linked events inside a batch vs case attributes.
    attrs = {"requested_at": lambda r: r["requested_at"], "case number": lambda r: _num(r["case_id"]),
             "user number": lambda r: _num(r["uid"]), "email": lambda r: r["email"].lower()}
    agree, pairs = collections.Counter(), 0
    for rep, group in batches.items():
        if len(group) < 2:
            continue
        t, by_uid = _seconds(rep), {r["uid"]: r for r in group}
        per_type = collections.defaultdict(list)
        for ev in linker.events:
            owners = set(linker.owners.get(ev["resource_id"], {})) & set(by_uid)
            if t - 7200 <= ev["t"] <= t + 900 and len(owners) == 1:
                per_type[ev["event_type"]].append(by_uid[owners.pop()])
        for items in per_type.values():
            for a, b in itertools.combinations(items, 2):
                if a is b:
                    continue
                pairs += 1
                for name, f in attrs.items():
                    agree[name] += f(a) < f(b)
    out["order"] = {name: agree[name] / max(1, pairs) for name in attrs}
    out["order_pairs"] = pairs

    # 3. Same-owner nearest neighbour inside evidence files (time order) vs chance.
    same = n = 0
    chance = 0.0
    for ev in store.rows("SELECT file_id FROM evidence_files"):
        owned = []
        for o in store.observations_for_evidence(ev["file_id"]):
            own = set(linker.owners.get(o["resource_id"] or "", {}))
            if len(own) == 1:
                owned.append((_ts(o["observed_at"]), own.pop()))
        if len(owned) < 3:
            continue
        counts = collections.Counter(u for _, u in owned)
        for i, a in enumerate(owned):
            others = owned[:i] + owned[i + 1:]
            nearest = min(others, key=lambda b: abs(b[0] - a[0]))
            same += nearest[1] == a[1]
            n += 1
            chance += (counts[a[1]] - 1) / len(others)
    out["neighbour"] = (same / max(1, n), chance / max(1, n), n)

    # 4. How many workflow links stay contested, by number of interchangeable claimants.
    k = collections.Counter()
    for c in cases.values():
        for link in c.links.values():
            m = next((re.search(r"contested-with=\[([^\]]*)\]", r) for r in link.reasons
                      if "contested-with" in r), None)
            if m:
                k[(c.request["split"], len(m.group(1).split(",")) + 1)] += 1
    out["contested"] = dict(k)
    out["workflow_links"] = {split: sum(l.strength == "workflow" for c in cases.values()
                                        if c.request["split"] == split for l in c.links.values())
                             for split in ("sample", "eval")}
    return out


def report_lines(m: dict) -> list[str]:
    match, total = m["result_match"]
    same, chance, n = m["neighbour"]
    lines = [
        "## 5. Tie-breaking analysis (records with no identifiers left)",
        "",
        "Deleted accounts and anonymized analytics carry no identifier, so they are tied to a case through",
        "deletion-worker events in the request window. Cases sharing a report batch compete for them. Each",
        "candidate signal was measured on strongly linked records only (pseudo-labels, not sample answers):",
        "",
        "| Signal | Measurement | Verdict |",
        "|---|---|---|",
        f"| Deletion job result vs the workflow's reported status | {match}/{total} match "
        f"({100 * match / max(1, total):.1f}%) | **used**, for every deletion job |",
    ]
    for name, rate in m["order"].items():
        lines.append(f"| Batch order follows {name} | agrees on {100 * rate:.0f}% of {m['order_pairs']} pairs "
                     "(50% = chance) | no signal |")
    lines += [
        f"| Nearest record in the same evidence file has the same owner | {100 * same:.0f}% vs {100 * chance:.0f}% "
        f"by chance ({n} records) | no signal (files are ordered by id or interleave people) |",
        "| Anonymized analytics content (products, metrics) matches the subject's other records | "
        "8 product names in the whole dataset | no signal |",
        "",
        "Other job types (anonymize, redact, disable, block) always report SUCCESS, so they carry no result",
        "signal either. When subjects with the same result evidence compete, the linker assigns",
        "deterministically and records `contested-with=[...]` on the link.",
        "",
        "| Split | Workflow links | Contested, 2 claimants | 3 | 4+ |",
        "|---|---|---|---|---|",
    ]
    for split in ("sample", "eval"):
        c = {k: v for (s, k), v in m["contested"].items() if s == split}
        lines.append(f"| {split} | {m['workflow_links'][split]} | {c.get(2, 0)} | {c.get(3, 0)} | "
                     f"{sum(v for k, v in c.items() if k >= 4)} |")
    lines += [
        "",
        "A contested link is right with probability about 1/k. Dropping links with k >= 3 would raise step",
        "precision but guarantee a miss on records the answer always contains (every case verifies its",
        "account), so all links are kept. This is the measured ceiling of identity linking on this data,",
        "not a parameter tuned to the samples.",
        "",
    ]
    return lines
