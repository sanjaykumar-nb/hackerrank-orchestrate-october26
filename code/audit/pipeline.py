"""End-to-end audit over the observation store: intent -> link -> state -> policy -> steps."""
from __future__ import annotations

from dataclasses import dataclass, field

from .assess import Assessment, assess
from .intent import _norm, classify_intents
from .linker import Linker
from .planner import plan
from .policy import PolicyEngine
from .state import StateReconstructor
from .store import ObservationStore


@dataclass
class CaseResult:
    case_id: str
    split: str
    status: str
    steps: list[dict]
    intent: tuple[bool, str]
    assessment: Assessment | None = None
    notes: list[str] = field(default_factory=list)


def run_pipeline(store: ObservationStore, llm=None) -> dict[str, CaseResult]:
    """`llm`: an LLMClient for intent classification; None uses the deterministic rules only."""
    requests = store.rows("SELECT * FROM requests ORDER BY case_id")
    verdicts = classify_intents([r["request_text"] for r in requests], llm)
    intents = {}
    for r in requests:
        v = verdicts[_norm(r["request_text"])]
        intents[r["case_id"]] = (v["deletion"], f"[{v['source']}] {v['cue']}")
    # Non-deletion requests take no part in linking: they authorize no workflow to claim records.
    deletion_requests = [r for r in requests if intents[r["case_id"]][0]]
    cases = Linker(store).link_cases(deletion_requests)
    states, policy = StateReconstructor(store), PolicyEngine(store)
    results = {}
    for req in requests:
        cid = req["case_id"]
        a = assess(cases[cid], states, policy) if cid in cases else None
        status, steps = plan(a, intents[cid][0])
        results[cid] = CaseResult(cid, req["split"], status, steps, intents[cid], a,
                                  list(cases[cid].notes) if cid in cases else [])
    return results
