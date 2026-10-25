"""Append-only JSONL ledger of every model call and cache hit.

It is the source of truth for rolling 24 h spend (survives restarts) and for usage_report.md.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

DAY_S = 86_400


class Ledger:
    def __init__(self, path: Path, clock=time.time):
        self.path = path
        self.clock = clock
        self.rows: list[dict] = []
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    self.rows.append(json.loads(line))

    def record(self, **row) -> dict:
        row.setdefault("ts", self.clock())
        self.rows.append(row)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, sort_keys=True) + "\n")
        return row

    def day_usage(self, model: str, now: float | None = None) -> tuple[int, int]:
        """(requests, tokens) sent to `model` in the last 24 h. Every attempt that reached
        the API counts as a request, including rejected ones; tokens come from billed usage."""
        cutoff = (now if now is not None else self.clock()) - DAY_S
        requests = tokens = 0
        for row in self.rows:
            if row.get("kind") == "call" and row.get("model") == model and row["ts"] >= cutoff:
                requests += 1
                tokens += int(row.get("total_tokens") or 0)
        return requests, tokens

    def run_rows(self, run_id: str) -> list[dict]:
        return [r for r in self.rows if r.get("run_id") == run_id]
