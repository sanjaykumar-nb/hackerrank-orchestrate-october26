"""Identity resolution and linking against the real dataset (no LLM)."""
from __future__ import annotations

import pytest

from audit.config import CODE_DIR, DATASET_DIR
from audit.identifiers import email_hash
from audit.identity import IdentityResolver
from audit.linker import Linker
from audit.store import ObservationStore
from evaluation.linking import link_metrics

pytestmark = pytest.mark.skipif(not (DATASET_DIR / "evidence_files.csv").exists(), reason="dataset missing")


@pytest.fixture(scope="module")
def store(tmp_path_factory):
    built, _ = ObservationStore.build(DATASET_DIR, tmp_path_factory.mktemp("store") / "obs.sqlite", llm=None)
    return built


@pytest.fixture(scope="module")
def cases(store):
    return Linker(store).link_cases(store.rows("SELECT * FROM requests ORDER BY case_id"))


def test_historical_email_resolves_to_subject(store):
    ident = IdentityResolver(store).resolve_email("minjun.kim90@GMAIL.com")
    assert ident.user_id == "user_12" and ident.matched_on == "historical"
    assert ident.emails["minjun.kim@gmail.com"] == "current"
    assert email_hash("minjun.kim90@gmail.com") in ident.hashes   # old hash, computed and audited


def test_every_request_resolves(cases):
    assert all(c.identity is not None for c in cases.values())


def test_old_hash_links_analytics_record(cases):
    link = cases["case_12"].links["analytics_9914"]
    assert link.strength == "strong" and any("44a65154" in r for r in link.reasons)


def test_similar_email_decoy_not_linked(store, cases):
    # g.silva76.jr@gmail.com is a different person from g.silva76@gmail.com
    decoys = {r["resource_id"] for r in store.observations_for_key("g.silva76.jr@gmail.com") if r["resource_id"]}
    subject = next(c for c in cases.values() if c.request["email"].lower() == "g.silva76@gmail.com")
    assert decoys and not (decoys & set(subject.links))


def test_records_without_identifiers_link_through_workflow(cases):
    assert cases["case_03"].links["analytics_9406"].strength == "workflow"
    assert cases["case_31"].links["account_4857"].strength == "workflow"


def test_each_record_goes_to_one_subject(cases):
    owner: dict[str, str] = {}
    for case in cases.values():
        for rid, link in case.links.items():
            if link.strength == "workflow":
                assert owner.setdefault(rid, case.identity.user_id) == case.identity.user_id


def test_sample_linking_quality(cases):
    m = link_metrics(cases, DATASET_DIR / "sample_output.csv")
    print(f"linking recall={m['recall']:.3f} precision={m['precision']:.3f} exact={m['exact_cases']}/{m['cases']}")
    assert m["recall"] >= 0.90 and m["precision"] >= 0.90


def test_pipeline_never_reads_labels():
    for path in (CODE_DIR / "audit").rglob("*.py"):
        assert "sample_output" not in path.read_text(encoding="utf-8"), path


def test_result_penalty_grades_deletion_jobs():
    from audit.linker import MISMATCH, result_penalty
    job = lambda result, kind="DELETE_RECORD": {"event_type": kind, "reported_result": result}
    assert result_penalty(job("SUCCESS"), "COMPLETE") == 0
    assert result_penalty(job("FAILED"), "PARTIALLY_COMPLETE") == 1
    assert result_penalty(job("FAILED"), "COMPLETE") == MISMATCH
    assert result_penalty(job("SUCCESS", "ANONYMIZE_RECORD"), "NOT_STARTED") == 0   # no result signal


def test_indistinguishable_records_are_flagged_contested(cases):
    # case_28 and case_29 share a report time and both account jobs FAILED: no evidence separates them
    for case_id in ("case_28", "case_29"):
        account = next(l for l in cases[case_id].links.values() if l.system_id == "account_service")
        assert any("contested-with" in r for r in account.reasons)
    # case_31's account job result matches only its own workflow: not contested
    assert not any("contested-with" in r for r in cases["case_31"].links["account_4857"].reasons)
