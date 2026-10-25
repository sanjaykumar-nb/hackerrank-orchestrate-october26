"""Intent gate, planner ordering, output contract and end-to-end sample scores."""
from __future__ import annotations

import csv
import json
from types import SimpleNamespace

import pytest

from audit.config import DATASET_DIR
from audit.intent import rule_intent
from audit.output import validate_output, write_output
from audit.pipeline import run_pipeline
from audit.planner import plan
from audit.store import ObservationStore
from evaluation.steps import load_answers, score


@pytest.mark.parametrize("text,deletion", [
    ("Please permanently delete the account associated with this email address.", True),
    ("Je demande la suppression définitive du compte associé à cette adresse e-mail.", True),
    ("이 이메일 주소에 연결된 계정의 영구 삭제를 요청합니다.", True),
    ("I deleted this account but the email is still recognized. Please remove any remaining copy of my profile.", True),
    ("Please remove me from marketing lists only. I still want receipts.", False),
    ("Please stop the promotional emails. I still need the account, so I am not asking for deletion.", False),
    ("My surname is misspelled. Can you correct it without removing any of my records?", False),
    ("Could you send me a copy of the information held on my account? Please don't delete anything.", False),
])
def test_intent_rules(text, deletion):
    assert rule_intent(text)[0] is deletion


def fake_decision(rid, system, violation, target, evidence="evidence_01"):
    from audit.policy import FINDING, REMEDIATION, VERIFY
    return SimpleNamespace(resource_id=rid, system_id=system, violation=violation, evidence_id=evidence,
                           finding_op=FINDING[target] if violation else None,
                           remediation_op=REMEDIATION[target] if violation else None, verify_op=VERIFY[target])


def test_planner_orders_and_blanks_future_evidence():
    decisions = [fake_decision("support_9", "support_platform", True, "REDACTED"),
                 fake_decision("search_2", "search_service", True, "ABSENT"),
                 fake_decision("data_flow_1", "integration_service", True, "DISABLED"),
                 fake_decision("account_1", "account_service", False, "ABSENT"),
                 fake_decision("analytics_3", "analytics_platform", True, "ANONYMIZED")]
    a = SimpleNamespace(status="INCOMPLETE", decisions=decisions)
    status, steps = plan(a, True)
    ops = [s["operation"] for s in steps]
    assert ops[:4] == ["REPORT_REMAINING_RECORD", "REPORT_EXPOSED_PII", "REPORT_EXPOSED_PII", "REPORT_ACTIVE_DATA_FLOW"]
    assert [s["resource_id"] for s in steps[1:3]] == ["analytics_3", "support_9"]   # then by system_id
    assert ops[4] == "DISABLE_DATA_FLOW"            # flows stop before records are changed
    assert all(s["evidence_id"] == "" for s in steps if s["phase"] != "FINDING")
    assert [s["step_no"] for s in steps] == list(range(1, len(steps) + 1))
    assert plan(a, False) == ("NOT_APPLICABLE", [])


def test_validator_catches_contract_breaks(tmp_path):
    path = tmp_path / "out.csv"
    bad_step = {"step_no": 2, "phase": "VERIFICATION", "system_id": "account_service", "resource_id": "account_1",
                "operation": "VERIFY_RECORD_ABSENT", "state": "PRESENT", "evidence_id": "evidence_99"}
    write_output(path, {"case_2": ("COMPLETE", [bad_step]), "case_1": ("NOT_APPLICABLE", [])})
    reqs = [{"case_id": "case_1", "audit_at": "2026-09-01T00:00:00Z"},
            {"case_id": "case_2", "audit_at": "2026-09-01T00:00:00Z"}]
    errors = " | ".join(validate_output(path, reqs, {"evidence_01": "2026-08-01T00:00:00Z"}, {}, {}))
    assert "step_no" in errors and "does not match" in errors and "unknown evidence" in errors


pytestmark_data = pytest.mark.skipif(not (DATASET_DIR / "evidence_files.csv").exists(), reason="dataset missing")


@pytest.fixture(scope="module")
def results(tmp_path_factory):
    store, _ = ObservationStore.build(DATASET_DIR, tmp_path_factory.mktemp("store") / "obs.sqlite", llm=None)
    return store, run_pipeline(store)


@pytestmark_data
def test_full_output_is_valid(results, tmp_path):
    store, res = results
    path = tmp_path / "output.csv"
    write_output(path, {c: (r.status, r.steps) for c, r in res.items() if r.split == "eval"})
    ev = {r["file_id"]: r["captured_at"] for r in store.rows("SELECT * FROM evidence_files")}
    act = {r["activity_id"]: r["event_time"] for r in store.rows("SELECT * FROM activities")}
    aud = {r["audit_id"]: r["event_time"] for r in store.rows("SELECT * FROM identity_history")}
    assert validate_output(path, store.rows("SELECT * FROM requests WHERE split='eval'"), ev, act, aud) == []
    with path.open(encoding="utf-8") as fh:
        assert sum(1 for _ in csv.DictReader(fh)) == 518


@pytestmark_data
def test_sample_full_answers(results, tmp_path):
    _, res = results
    predicted = {c: (r.status, r.steps) for c, r in res.items() if r.split == "sample"}
    m = score(predicted, load_answers(DATASET_DIR / "sample_output.csv"))
    print(json.dumps({k: v for k, v in m.items() if k != "failures"}))
    assert m["status_accuracy"] >= 0.95 and m["step_exact"]["f1"] >= 0.95
