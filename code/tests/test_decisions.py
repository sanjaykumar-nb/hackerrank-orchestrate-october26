"""State-at-audit_at and policy decisions against the real dataset (no LLM)."""
from __future__ import annotations

import pytest

from audit.assess import assess
from audit.config import DATASET_DIR
from audit.linker import Linker
from audit.policy import PolicyEngine
from audit.state import StateReconstructor
from audit.store import ObservationStore
from evaluation.decisions import decision_metrics

pytestmark = pytest.mark.skipif(not (DATASET_DIR / "evidence_files.csv").exists(), reason="dataset missing")


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    store, _ = ObservationStore.build(DATASET_DIR, tmp_path_factory.mktemp("store") / "obs.sqlite", llm=None)
    cases = Linker(store).link_cases(store.rows("SELECT * FROM requests ORDER BY case_id"))
    states, policy = StateReconstructor(store), PolicyEngine(store)
    assessments = {cid: assess(c, states, policy) for cid, c in cases.items()}
    return store, states, assessments


def decision(assessments, case_id, rid):
    return next(d for d in assessments[case_id].decisions if d.resource_id == rid)


def test_sample_decision_quality(world):
    m = decision_metrics(world[2], DATASET_DIR / "sample_output.csv")
    print({k: v for k, v in m.items() if k != "errors"})
    assert m["status_accuracy"] >= 0.95
    assert min(m["target_accuracy"], m["finding_accuracy"], m["evidence_accuracy"]) >= 0.95


def test_never_cites_evidence_after_audit_at(world):
    store, states, assessments = world
    for a in assessments.values():
        for d in a.decisions:
            if d.evidence_id.startswith("evidence_"):
                assert states.captured(d.evidence_id) <= a.audit_at, (a.case_id, d.resource_id)


def test_replica_ghost_copy_keeps_record_present(world):
    d = decision(world[2], "case_05", "account_005")
    assert d.violation and d.state.location == "failover_replica" and d.evidence_id == "evidence_54"


def test_partial_bundle_does_not_prove_absence_of_other_subjects(world):
    d = decision(world[2], "case_21", "audit_record_3737")
    assert d.target == "PRESENT" and d.current == "PRESENT"


def test_rules_drive_targets(world):
    a = world[2]
    assert decision(a, "case_32", "tombstone_9178").finding_op == "REPORT_EXPOSED_PII"   # tombstone keeps user_id only
    assert decision(a, "case_19", "support_9553").target == "REDACTED"                  # shared conversation
    assert decision(a, "case_07", "support_001").target == "ABSENT"                     # subject-only attachment
    assert decision(a, "case_12", "analytics_9914").category == "MARKETING_PROFILE"
    assert decision(a, "case_09", "backup_4978").finding_op == "REPORT_RESTORE_RISK"


def test_catalog_link_alone_is_not_state(world):
    assert ("object_19", "no evidence at or before audit_at") in world[2]["case_07"].dropped
