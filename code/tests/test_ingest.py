"""Ingestion checks against the real dataset (no LLM).

The sample answers are used only as a completeness probe: every (resource, evidence) pair a
sample cites must be visible in the store, otherwise no later stage could ever produce it.
"""
from __future__ import annotations

import csv
import json

import pytest

from audit.config import DATASET_DIR
from audit.ingest.logs import parse_line
from audit.store import ObservationStore

pytestmark = pytest.mark.skipif(not (DATASET_DIR / "evidence_files.csv").exists(), reason="dataset missing")


@pytest.fixture(scope="module")
def store(tmp_path_factory):
    built, report = ObservationStore.build(DATASET_DIR, tmp_path_factory.mktemp("store") / "obs.sqlite", llm=None)
    built.report = report
    return built


def sample_steps():
    with (DATASET_DIR / "sample_output.csv").open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            for step in json.loads(row["steps_json"]):
                yield row["case_id"], step


def test_every_text_evidence_file_parses_cleanly(store):
    assert store.report["unparsed"] == []
    assert not [w for w in store.report["warnings"] if "parser error" in w]
    missing = store.summary()["files_without_observations"]
    formats = {r["file_id"]: r["format"] for r in store.rows("SELECT file_id, format FROM evidence_files")}
    assert all(formats[f] in ("png", "jpg", "jpeg", "webp") for f in missing)


def test_sample_citations_are_grounded(store):
    """Each cited (resource, evidence) is either observed in that evidence, or the evidence
    covers the resource's system (absence proven by a full capture). Screenshots excluded."""
    image_ids = {r["file_id"] for r in store.rows("SELECT file_id FROM evidence_files WHERE format='png'")}
    ungrounded = []
    for case_id, step in sample_steps():
        ev, rid = step["evidence_id"], step["resource_id"]
        if not ev or ev in image_ids:
            continue
        seen = store.rows("SELECT 1 FROM observations WHERE evidence_id=? AND resource_id=?", (ev, rid))
        covered = store.rows("SELECT 1 FROM coverage WHERE evidence_id=? AND system_id=?", (ev, step["system_id"]))
        if not seen and not (step["state"] == "ABSENT" and covered):
            ungrounded.append((case_id, step["operation"], rid, ev))
    assert ungrounded == []


def test_sample_resources_are_discoverable(store):
    """Resources in sample answers that appear somewhere in the store or the activity stream.
    A few sample resources occur in no participant-visible text at all (not even the
    activity stream), so the bar is a rate; the missing list is printed for the report."""
    wanted = {step["resource_id"] for _, step in sample_steps()}
    missing = sorted(r for r in wanted
                     if not store.observations_for_resource(r) and not store.activities_for_resource(r))
    print(f"sample resources discoverable: {len(wanted) - len(missing)}/{len(wanted)}; missing {missing}")
    assert (len(wanted) - len(missing)) / len(wanted) >= 0.95


def test_trace_join_links_lookup_key_to_resource(store):
    rows = store.observations_for_key("MINJUN.KIM@gmail.com")
    assert any(r["resource_id"] == "account_9797" and r["matched_role"] == "link" for r in rows)


def test_replica_copy_keeps_location(store):
    rows = store.observations_for_resource("account_005")
    assert any(r["location"] == "failover_replica" and r["presence"] == "PRESENT" for r in rows)


def test_tombstone_pii_is_content(store):
    rows = [r for r in store.observations_for_resource("tombstone_9178") if r["evidence_id"] == "evidence_51"]
    assert set(json.loads(rows[0]["pii"])) == {"email", "hash", "name", "phone"}


@pytest.mark.parametrize("line,service,trace,key", [
    ('2026-09-15T11:56:49Z level=DEBUG service=account_service trace_id=tr-1 event=profile_lookup_started '
     'lookup_key="a@b.com" attempt=1', "account_service", "tr-1", "lookup_key"),
    ('2026-07-28T05:51:18Z [DEBUG] [account_service] [tr-2] profile_lookup_started: instance="a" '
     'lookup_key="a@b.com"', "account_service", "tr-2", "lookup_key"),
    ('2026-07-03T07:52:21Z | DEBUG | account_service | tr-3 | profile_lookup_started | lookup_key="a@b.com"',
     "account_service", "tr-3", "lookup_key"),
    ('2026-09-30T11:51:03Z requestId=tr-4 level=DEBUG function=profile_service message="resource_id=\\"account_1\\" '
     'event=\\"profile_lookup_completed\\" correlation_key=\\"a@b.com\\""', "profile_service", "tr-4", "correlation_key"),
    ('2026-09-10T08:56:17Z {"severity":"DEBUG","service":"account_service","trace_id":"tr-5",'
     '"event":"profile_lookup_started","lookup_key":"a@b.com"}', "account_service", "tr-5", "lookup_key"),
    ('2026-08-22T10:51:27Z <15>1 node-3 support_platform tr-6 - - level=DEBUG event="attachment_scan_started" '
     'scan_key="a@b.com"', "support_platform", "tr-6", "scan_key"),
])
def test_log_dialects(line, service, trace, key):
    entry = parse_line(line)
    assert (entry["service"], entry["trace"], entry["fields"][key]) == (service, trace, "a@b.com")
