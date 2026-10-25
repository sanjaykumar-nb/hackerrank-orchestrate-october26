"""Log evidence in six dialects, normalized to key/values and joined per trace.

Who a resource belongs to is often split across lines: the `*_started` line carries the
lookup key (an email, hash or user_id) and the `*_completed` line carries the resource_id,
with the same trace/request id. Joining on that id is what links them.
"""
from __future__ import annotations

import json
import re
from collections import OrderedDict

from ..identifiers import system_for_resource
from ..model import EvidenceFile, Observation, ParseOutput
from .common import coerce, infer_presence, is_record_lookup, link_values, record_lookup_observation

FMT = "log"
TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z)\s+(.*)$")
KV_RE = re.compile(r'([A-Za-z_][\w.-]*)=("(?:[^"\\]|\\.)*"|[^\s"]+)')
SYSLOG_RE = re.compile(r"^<\d+>\d+\s+(\S+)\s+(\S+)\s+(\S+)\s+\S+\s+\S+\s*(.*)$")
BRACKET_RE = re.compile(r"^\[(\w+)\]\s+\[([^\]]+)\]\s+\[([^\]]+)\]\s+([\w.-]+):\s*(.*)$")
PIPE_RE = re.compile(r"^\|?\s*(\w+)\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*(.*)$")
META = {"level", "severity", "service", "logger", "function", "trace_id", "requestId", "event"}
NOISE = {"instance", "pod", "region", "attempt", "duration_ms", "took_ms", "latency_ms", "wait_ms",
         "pool", "retry_in_ms", "shard", "seq"}


def unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return re.sub(r"\\(.)", r"\1", value[1:-1])
    return value


def parse_kv(text: str) -> dict:
    return {k: unquote(v) for k, v in KV_RE.findall(text)}


def parse_line(line: str) -> dict | None:
    """-> {ts, dialect, level, service, trace, event, fields} or None if unrecognized."""
    m = TS_RE.match(line.strip())
    if not m:
        return None
    ts, rest = m.groups()
    if rest.startswith("{"):
        try:
            fields = json.loads(rest)
        except json.JSONDecodeError:
            return None
        dialect, level, service = "json", fields.get("severity"), fields.get("service") or fields.get("logger")
        trace, event = fields.get("trace_id"), fields.get("event")
    elif (s := SYSLOG_RE.match(rest)):
        _, service, trace, kv = s.groups()
        fields = parse_kv(kv)
        dialect, level, event = "syslog", fields.get("level"), fields.get("event")
    elif (b := BRACKET_RE.match(rest)):
        level, service, trace, event, kv = b.groups()
        fields, dialect = parse_kv(kv), "bracket"
    elif (p := PIPE_RE.match(rest)):
        level, service, trace, event, kv = p.groups()
        fields, dialect = parse_kv(kv), "pipe"
    else:
        fields = parse_kv(rest)
        if not fields:
            return None
        message = fields.get("message")
        if isinstance(message, str) and "=" in message:
            fields.pop("message")
            fields.update(parse_kv(message))
            dialect = "cloud"
        else:
            dialect = "logfmt"
        level, service = fields.get("level"), fields.get("service") or fields.get("function")
        trace, event = fields.get("trace_id") or fields.get("requestId"), fields.get("event")
    clean = {k: coerce(v) for k, v in fields.items() if k not in META and k not in NOISE}
    return {"ts": ts, "dialect": dialect, "level": level, "service": service,
            "trace": trace, "event": event or "", "fields": clean, "raw": line.strip()}


def parse(ev: EvidenceFile) -> ParseOutput:
    out = ParseOutput()
    traces: OrderedDict[str, list[dict]] = OrderedDict()
    for n, line in enumerate(ev.path.read_text(encoding="utf-8").splitlines()):
        if not line.strip():
            continue
        entry = parse_line(line)
        if entry is None:
            out.unparsed.append(f"{ev.file_id}:{n + 1}: {line[:120]}")
            continue
        key = entry["trace"] or f"line-{n}"
        traces.setdefault(key, []).append(entry)

    for trace_id, entries in traces.items():
        links = sorted({k for e in entries for k in link_values(e["fields"])})
        resources = list(OrderedDict.fromkeys(
            e["fields"]["resource_id"] for e in entries if e["fields"].get("resource_id")))
        for rid in resources:
            mentions = [e for e in entries if e["fields"].get("resource_id") == rid]
            final = [e for e in mentions if not e["event"].endswith("_started")]
            entry = (final or mentions)[-1]
            out.observations.append(_observation(ev, entry, rid, links, trace_id))
        if not resources and links and any(e["event"].endswith(("_completed", "_failed")) for e in entries):
            entry = entries[-1]
            obs = _observation(ev, entry, None, links, trace_id)
            obs.parser = "log.lookup_only"
            out.observations.append(obs)
    return out


def _observation(ev: EvidenceFile, entry: dict, rid: str | None, links: list[str], trace_id: str) -> Observation:
    fields = dict(entry["fields"])
    if is_record_lookup(fields):
        obs = record_lookup_observation({**fields, "recorded_at": entry["ts"]}, ev, FMT,
                                        f"log.{entry['dialect']}.record_lookup", raw=entry["raw"])
        obs.link_keys = sorted(set(obs.link_keys) | set(links))
        return obs
    for name in list(fields):
        if name in {"lookup_key", "match_key", "scan_key", "subject_key", "subject_ref", "correlation_key"}:
            fields.pop(name)
    fields["_event"] = entry["event"]
    fields["_trace"] = trace_id
    service = entry["service"]
    system = fields.get("system") or system_for_resource(rid) or service
    result = fields.get("result") if isinstance(fields.get("result"), str) else None
    return Observation(
        evidence_id=ev.file_id, source_format=FMT, parser=f"log.{entry['dialect']}",
        observed_at=entry["ts"], captured_at=ev.captured_at, system_id=system, resource_id=rid,
        collection=None, location=service if service and service != system else "log",
        presence=infer_presence(fields, result) if rid else None,
        result=result.upper() if result else None, record_type=fields.get("record_type"),
        fields=fields, link_keys=links, raw=entry["raw"][:2000],
    )
