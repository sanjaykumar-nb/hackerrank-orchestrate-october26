"""Deletion audit agent CLI.

  python code/main.py ingest              build the observation store (.cache/observations.sqlite)
  python code/main.py ingest --no-llm     same, skipping screenshot transcription
  python code/main.py llm-plan            project Groq calls/tokens for uncached work vs today's budget
  python code/main.py inspect --resource account_9797 | --key minjun.kim@gmail.com | --evidence evidence_01
  python code/main.py links --case case_12 | --eval   per-case identity + linked records, or sample scores
  python code/main.py assess --case case_11 | --eval  state at audit_at + policy decision per record
  python code/main.py [run] [--no-llm]    full audit -> output.csv (+ sample predictions and scores)
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from audit.config import DATASET_DIR, STORE_PATH  # noqa: E402
from audit.ingest import image_jobs  # noqa: E402
from audit.ingest.tables import load_manifest  # noqa: E402
from audit.llm import LLMClient  # noqa: E402
from audit.store import ObservationStore  # noqa: E402


def cmd_ingest(args) -> int:
    llm = None if args.no_llm else LLMClient()
    if llm is not None and not args.no_llm:
        plan = llm.plan(image_jobs(load_manifest(args.dataset)))
        if not plan["ok"]:
            print("LLM budget too low for screenshot transcription; continuing without it:",
                  json.dumps(plan["models"]))
            llm = LLMClient(offline=True)          # cached transcriptions only
    store, report = ObservationStore.build(args.dataset, STORE_PATH, llm)
    print(json.dumps(store.summary(), indent=2))
    for line in report["warnings"]:
        print("warning:", line)
    for line in report["unparsed"][:20]:
        print("unparsed:", line)
    if llm is not None and llm.events:
        print("llm events:", *llm.events, sep="\n  ")
    return 0


def cmd_llm_plan(args) -> int:
    client = LLMClient()
    plan = client.plan(image_jobs(load_manifest(args.dataset)))
    print(json.dumps({"offline": client.offline, **plan}, indent=2))
    return 0 if plan["ok"] else 1


def cmd_inspect(args) -> int:
    if not STORE_PATH.exists():
        print("No store yet: run `python code/main.py ingest` first.")
        return 1
    store = ObservationStore(STORE_PATH)
    if args.resource:
        rows = store.observations_for_resource(args.resource)
        acts = store.activities_for_resource(args.resource)
    elif args.key:
        rows, acts = store.observations_for_key(args.key), []
    else:
        rows, acts = store.observations_for_evidence(args.evidence), []
    for r in rows:
        fields = {k: v for k, v in json.loads(r["fields"]).items() if v not in (None, "", [])}
        print(f"{r['evidence_id']:<12} {r['observed_at'] or '':<21} {r['system_id'] or '-':<22} "
              f"{r['resource_id'] or '-':<18} {r['presence'] or '-':<8} {r['location'] or '-':<18} "
              f"pii={json.loads(r['pii'])} links={json.loads(r['link_keys'])} "
              f"{('role=' + r['matched_role']) if 'matched_role' in r else ''}")
        print(f"{'':<12} {r['parser']}: {json.dumps(fields, ensure_ascii=False)[:300]}")
    for a in acts:
        print(f"{a['activity_id']:<14} {a['event_time']} {a['event_type']:<22} {a['resource_id']:<18} "
              f"{a['reported_result']:<8} {a['details']}")
    print(f"-- {len(rows)} observations, {len(acts)} activities")
    return 0


def cmd_links(args) -> int:
    from audit.linker import Linker
    store = ObservationStore(STORE_PATH)
    requests = store.rows("SELECT * FROM requests ORDER BY case_id")
    cases = Linker(store).link_cases(requests)
    if args.eval:
        from evaluation.linking import link_metrics
        m = link_metrics(cases, args.dataset / "sample_output.csv")
        print(f"samples: recall={m['recall']:.3f} precision={m['precision']:.3f} "
              f"exact={m['exact_cases']}/{m['cases']}")
        for case_id, diff in m["mismatches"].items():
            print(f"  {case_id}: missing={diff['missing']} extra={diff['extra']}")
        return 0
    for case_id in ([args.case] if args.case else sorted(cases)):
        case = cases[case_id]
        ident = case.identity
        print(f"## {case_id} {case.request['email']} -> {ident.user_id if ident else '?'} "
              f"({ident.matched_on if ident else 'unresolved'})")
        for rid, link in sorted(case.links.items()):
            print(f"   {link.strength:<8} {link.system_id or '-':<22} {rid:<18} {'; '.join(link.reasons[:3])}")
        for note in case.notes:
            print(f"   note: {note}")
    return 0


def cmd_assess(args) -> int:
    from audit.assess import assess
    from audit.linker import Linker
    from audit.policy import PolicyEngine
    from audit.state import StateReconstructor
    store = ObservationStore(STORE_PATH)
    cases = Linker(store).link_cases(store.rows("SELECT * FROM requests ORDER BY case_id"))
    states, policy = StateReconstructor(store), PolicyEngine(store)
    if args.eval:
        from evaluation.decisions import decision_metrics
        assessments = {cid: assess(c, states, policy) for cid, c in cases.items()}
        m = decision_metrics(assessments, args.dataset / "sample_output.csv")
        for err in m.pop("errors"):
            print(" ", err)
        print(json.dumps(m, indent=2))
        return 0
    for case_id in ([args.case] if args.case else sorted(cases)):
        a = assess(cases[case_id], states, policy)
        print(f"## {case_id} audit_at={a.audit_at} -> {a.status}")
        for d in a.decisions:
            flag = "VIOLATION" if d.violation else "ok"
            print(f"   {flag:<9} {d.resource_id:<18} {d.category:<20} {d.rule_id or '-':<8} "
                  f"target={d.target:<10} current={d.current:<9} {d.evidence_id:<13} {d.reason}")
        for rid, why in a.dropped:
            print(f"   dropped   {rid:<18} {why}")
    return 0


def cmd_run(args) -> int:
    from audit.config import BUILD_DIR, OUTPUT_CSV
    from audit.output import validate_output, write_output
    from audit.pipeline import run_pipeline
    from evaluation.steps import load_answers, score

    from audit.ingest.tables import load_requests
    from audit.intent import intent_jobs
    llm = LLMClient(offline=True) if args.no_llm else LLMClient(run_id=args.run_id)
    jobs = image_jobs(load_manifest(args.dataset), llm) + intent_jobs(
        [r["request_text"] for r in load_requests(args.dataset)])
    preflight = llm.plan(jobs)
    print("llm preflight:", json.dumps({"offline": llm.offline, **preflight}))
    if not llm.offline and not preflight["ok"]:
        print("LLM budget too low for this run; using cached model results and rules only")
        llm = LLMClient(offline=True)
    store, report = ObservationStore.build(args.dataset, STORE_PATH, llm)
    for line in report["warnings"]:
        print("warning:", line)
    results = run_pipeline(store, llm)
    disagreements = [(c, r.intent[1]) for c, r in results.items() if "rules disagree" in r.intent[1]]
    print(f"intent: {sum(not r.intent[0] for r in results.values())} non-deletion; "
          f"{sum('[llm' in r.intent[1] for r in results.values())} decided by the model; "
          f"{len(disagreements)} model/rule disagreements")
    for case_id, why in disagreements:
        print("  ", case_id, why)
    if llm.events:
        print("llm events:", *llm.events, sep="\n  ")

    evidence_times = {r["file_id"]: r["captured_at"] for r in store.rows("SELECT * FROM evidence_files")}
    activity_times = {r["activity_id"]: r["event_time"] for r in store.rows("SELECT * FROM activities")}
    audit_times = {r["audit_id"]: r["event_time"] for r in store.rows("SELECT * FROM identity_history")}
    targets = {"eval": OUTPUT_CSV, "sample": BUILD_DIR / "sample_predictions.csv"}
    exit_code = 0
    for split, path in targets.items():
        rows = {cid: (r.status, r.steps) for cid, r in results.items() if r.split == split}
        write_output(path, rows)
        requests = store.rows("SELECT * FROM requests WHERE split=?", (split,))
        errors = validate_output(path, requests, evidence_times, activity_times, audit_times)
        counts = {s: sum(1 for st, _ in rows.values() if st == s)
                  for s in ("COMPLETE", "INCOMPLETE", "NOT_APPLICABLE")}
        print(f"{split}: wrote {len(rows)} rows to {path} {counts} -> "
              f"{'valid' if not errors else f'{len(errors)} contract errors'}")
        for err in errors[:20]:
            print("  ", err)
        exit_code |= bool(errors)

    gold = args.dataset / "sample_output.csv"
    if gold.exists():
        m = score(load_answers(targets["sample"]), load_answers(gold))
        failures = m.pop("failures")
        print("sample scores:", json.dumps(m, indent=2))
        for f in failures[:args.show_failures]:
            print(f"  {f['case_id']} status {f['status'][0]}/{f['status'][1]}")
            for s in f["missing"]:
                print("     missing", s)
            for s in f["extra"]:
                print("     extra  ", s)

    from audit.config import CODE_DIR
    from evaluation.usage import write_usage_report
    n_eval = sum(r.split == "eval" for r in results.values())
    usage_path = CODE_DIR / "evaluation" / "usage_report.md"
    write_usage_report(llm.ledger, llm.run_id, usage_path, n_eval, len(results))
    print(f"usage report for run {llm.run_id} -> {usage_path}")
    return int(exit_code)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dataset", type=Path, default=DATASET_DIR)
    sub = parser.add_subparsers(dest="command")
    p_ingest = sub.add_parser("ingest")
    p_ingest.add_argument("--no-llm", action="store_true", help="skip screenshot transcription")
    sub.add_parser("llm-plan")
    p_inspect = sub.add_parser("inspect")
    group = p_inspect.add_mutually_exclusive_group(required=True)
    group.add_argument("--resource")
    group.add_argument("--key")
    group.add_argument("--evidence")
    p_links = sub.add_parser("links")
    p_links.add_argument("--case")
    p_links.add_argument("--eval", action="store_true", help="score linking on the public samples")
    p_assess = sub.add_parser("assess")
    p_assess.add_argument("--case")
    p_assess.add_argument("--eval", action="store_true", help="score decisions on the public samples")
    p_run = sub.add_parser("run")
    p_run.add_argument("--no-llm", action="store_true", help="use only cached LLM results")
    p_run.add_argument("--show-failures", type=int, default=50)
    p_run.add_argument("--run-id", help="label for this run in the LLM ledger (usage report)")
    args = parser.parse_args(argv)
    if args.command is None:
        args = parser.parse_args(["run", *(argv or sys.argv[1:])])
    handlers = {"ingest": cmd_ingest, "llm-plan": cmd_llm_plan, "inspect": cmd_inspect,
                "links": cmd_links, "assess": cmd_assess, "run": cmd_run}
    return handlers[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
