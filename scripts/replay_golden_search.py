"""Repeat the complete Golden Search set using an immutable approved six-table Add bank."""
from __future__ import annotations

import argparse
from dataclasses import replace
import hashlib
import json
import os
import socket
import sqlite3
import threading
import time
from pathlib import Path
from uuid import uuid4

import httpx
import uvicorn

from remi.api import create_app, environment_engine
from remi.settings import load_environment
from remi.storage import SQLiteStore, TABLES
from scripts.golden_snapshots import copy_query_snapshot
from scripts.frozen_search_plan import FrozenSearchPlanner
from scripts.golden_adapter import AML_METRIC_CUTOFFS, AML_TOP_K, actions, add_payload, input_catalog, metrics, score_case
from scripts.run_golden import compare, diagnostics, fingerprint, latency, verify_baseline


def execute(args):
    original = json.loads(args.original.read_text())
    dataset = json.loads(Path(original["dataset"]).read_text())
    baseline_path=original.get('baseline_result')
    baseline = json.loads(Path(baseline_path).read_text()) if baseline_path else None
    if baseline is not None:
        verify_baseline(dataset, baseline)
    if not original["complete"] or args.output.exists() or not args.allow_copy_bank:
        raise ValueError("complete_original_fresh_output_and_explicit_bank_copy_required")
    query_snapshots = all(row.get('query_snapshot') for row in [
        *original['questions'], *original['lifecycle_searches']])
    if getattr(args, 'require_query_snapshots', False) and not query_snapshots:
        raise ValueError('query_boundary_snapshots_required')
    planner_path = getattr(args, 'frozen_planner', None)
    planner_report = json.loads(planner_path.read_text()) if planner_path else None
    if planner_report is not None and (not query_snapshots or not planner_report['complete']
            or planner_report['dataset_sha256'] != original['dataset_sha256']):
        raise ValueError('complete_matching_planner_report_and_snapshots_required')
    if planner_report is not None and Path(planner_report.get('original_full_run',planner_path)).resolve() != args.original.resolve():
        raise ValueError('planner_report_must_use_the_same_original_bank')
    prefix_path = getattr(args, 'planner_prefix_file', None)
    prefix = prefix_path.read_text() if prefix_path else ''
    if prefix_path and (planner_path or not prefix.strip() or len(prefix.encode()) > 4000):
        raise ValueError('live_planner_and_nonempty_bounded_prefix_required')
    if prefix:
        import remi.providers as providers
        providers.DECISION_PROMPT = prefix + '\n\n' + providers.DECISION_PROMPT
    rerank_question_path = getattr(args, 'rerank_question_file', None)
    rerank_question = rerank_question_path.read_text() if rerank_question_path else ''
    if rerank_question_path:
        if not rerank_question.strip() or len(rerank_question.encode()) > 8000:
            raise ValueError('nonempty_bounded_rerank_question_required')
        import remi.providers as providers
        providers.SUPPORT_QUESTION = rerank_question
    os.umask(0o077)
    args.output.mkdir(parents=True)
    database = args.output / "memory.sqlite3"
    with sqlite3.connect(f"file:{original['database']}?mode=ro", uri=True) as source, sqlite3.connect(database) as target:
        source.backup(target)
    load_environment(args.env_file)
    os.environ["REMI_DB_PATH"] = str(database)
    engine = environment_engine()
    if planner_report is not None:
        engine.model.close()
        engine.model = FrozenSearchPlanner()
    if getattr(args, 'action_condition_rerank', False):
        from scripts.action_condition_rerank import configure
        configure(engine.reranker)
    elif getattr(args, 'role_only_rerank', False):
        from scripts.role_only_rerank import configure
        configure(engine.reranker)
    elif getattr(args, 'grounded_rerank', False):
        from scripts.grounded_reranker import GroundedReranker
        engine.reranker = GroundedReranker.from_existing(engine.reranker)
    elif getattr(args, 'full_rerank_cards', False):
        engine.reranker._evidence_card = lambda card: card
    source_index_only = getattr(args, 'source_index_only', False)
    if source_index_only:
        engine.settings = replace(engine.settings, graph_enabled=False, hot_enabled=False)
        visible_documents = engine._visible_documents
        engine._visible_documents = lambda owner, as_of: [
            document for document in visible_documents(owner, as_of) if document.memory_id is None]
    if {row[0] for row in engine.store.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")} != TABLES:
        raise ValueError("exactly_six_tables_required")
    owner = original["derivation"]["rhelm"]["memories"][0]["owner"]
    life_owner = original["lifecycle_add_calls"][-1]["snapshot"][0]["owner"]
    mapping, catalog = {}, {}
    for kind, action in actions(dataset):
        if kind == "add":
            refs, originals = input_catalog(add_payload(action, owner), action["turns"], action.get("message_refs"))
            mapping.update(refs)
            catalog.update(originals)
    token = uuid4().hex
    server = uvicorn.Server(uvicorn.Config(create_app(engine, api_key=token), log_level="error", access_log=False))
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    port = listener.getsockname()[1]
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(.05)
    if not server.started:
        raise RuntimeError("local_service_start_failed")
    root = Path(__file__).resolve().parents[1]
    report = {"mode": "query_boundary_snapshot_replay" if query_snapshots else "all_search_replay_using_frozen_ingestion", "complete": False,
        "original_full_run": str(args.original), "dataset": original["dataset"], "dataset_sha256": original["dataset_sha256"],
        "baseline_result": original["baseline_result"], "ingestion_code_fingerprint": original["runtime_code_fingerprint"],
        "runtime_directory": str(root), "runtime_code_fingerprint": fingerprint(root), "started_at": time.time(),
        "add_calls": original["add_calls"], "lifecycle_add_calls": original["lifecycle_add_calls"],
        "questions": [], "lifecycle_searches": [], "errors": [], "database": str(database),
        "config": {**original["config"], 'settings':vars(engine.settings),
                   'source_index_only':source_index_only,
                   'frozen_planner_report':str(planner_path) if planner_path else None,
                   'rerank_question':rerank_question,
                   'rerank_question_sha256':hashlib.sha256(rerank_question.encode()).hexdigest() if rerank_question else None,
                   'planner_prefix':prefix,
                   'planner_prefix_sha256':hashlib.sha256(prefix.encode()).hexdigest() if prefix else None,
                   'query_clock':'recorded_planner_clock' if planner_path else 'service_clock',
                   'rerank_input':('action_condition' if getattr(args,'action_condition_rerank',False) else
                                   'role_only' if getattr(args,'role_only_rerank',False) else
                                   'grounded_blend' if getattr(args,'grounded_rerank',False) else
                                   'full_card' if getattr(args,'full_rerank_cards',False) else 'runtime_default')},
        "source_to_ref": mapping}
    def save(status):
        calls = diagnostics(engine)
        report["diagnostic_calls"] = calls
        report["summary"] = {"retrieval": {str(k): metrics(report["questions"], k) for k in AML_METRIC_CUTOFFS},
            "add_latency": original["summary"]["add_latency"], "search_latency": latency(report["questions"]),
            "calls_scope": "Search replay only; Add was reused without new model calls"}
        (args.output / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
        (args.output / "run.json").write_text(json.dumps({"status": status, "updated_at": time.time(),
            "questions_completed": len(report["questions"]), "lifecycle_queries_completed": len(report["lifecycle_searches"]),
            "planned_questions": 13, "planned_lifecycle_queries": 3, "pid": os.getpid()}, indent=2))
    save("running")
    def load_query_bank(record):
        if query_snapshots:
            engine.store.close()
            copy_query_snapshot(record['query_snapshot'], database)
            engine.store = SQLiteStore(database)
    def load_planner(record):
        if planner_report is not None:
            import remi.engine as engine_module
            engine.model.load(record['trace'])
            stamp = record['trace']['as_of']
            engine_module.utcnow = lambda: stamp
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", headers={"Authorization": "Bearer " + token}, timeout=180) as client:
            for case in dataset["cases"]:
                question = case["question"]
                old = next(row for row in original['questions'] if row['question_id'] == question['id'])
                load_query_bank(old)
                if planner_report is not None:
                    load_planner(next(row for row in planner_report['questions'] if row['question_id']==question['id']))
                started = time.monotonic()
                response = client.post("/search", json={
                    "user_id": owner, "query": question["text"], "top_k": AML_TOP_K})
                candidates = response.json()["data"] if response.status_code == 200 else []
                record = score_case(case, candidates, mapping, catalog)
                record.update(seconds=round(time.monotonic() - started, 3), candidates=candidates,
                              http_status=response.status_code, trace=engine.last_search)
                report["questions"].append(record)
                if response.status_code != 200:
                    report["errors"].append({"question_id": question["id"], "http_status": response.status_code})
                print(f"Search replay {len(report['questions'])}: recall={record['evidence_recall']:.3f}", flush=True)
                save("running")
            for old in original["lifecycle_searches"]:
                load_query_bank(old)
                if planner_report is not None:
                    load_planner(next(row for row in planner_report['lifecycle_searches'] if row['query']==old['query']))
                started = time.monotonic()
                response = client.post("/search", json={
                    "user_id": life_owner, "query": old["query"], "top_k": AML_TOP_K})
                candidates = response.json()["data"] if response.status_code == 200 else []
                expected = ("搬家今天已完成", "2026-09-24") if "最终" in old["query"] else (
                    "李明现在住上海", "2026-09-24") if "2026年9月24日" in old["query"] else ("李明现在住杭州", "2026-10-01")
                report["lifecycle_searches"].append({"query": old["query"], "seconds": round(time.monotonic() - started, 3),
                    "http_status": response.status_code, "top1_expected_evidence": bool(candidates) and all(
                        term in candidates[0]["content"] for term in expected), "candidates": candidates, "trace": engine.last_search})
                save("running")
            report["complete"] = True
            save("complete")
            if baseline is not None:
                (args.output / "comparison.json").write_text(json.dumps(compare(report, baseline, dataset), ensure_ascii=False, indent=2))
    except Exception as error:
        report["errors"].append({"exception": type(error).__name__})
        save("interrupted")
        raise SystemExit(2) from None
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()
    print(json.dumps({"result": str(args.output / "result.json"),
                      "metrics": report["summary"]["retrieval"][str(AML_TOP_K)]}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--original", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--allow-copy-bank", action="store_true")
    parser.add_argument("--source-index-only", action="store_true",
                        help="Experiment-only source index ablation with graph and hot memory disabled")
    parser.add_argument('--require-query-snapshots', action='store_true',
                        help='Require the bank captured immediately before each original query')
    planner = parser.add_mutually_exclusive_group()
    planner.add_argument('--planner-prefix-file',type=Path,
                         help='Experiment-only: prepend a bounded task definition and record it in the report')
    planner.add_argument('--frozen-planner',type=Path,
                        help='Replay all planner responses and clocks from this completed matching Search report')
    parser.add_argument('--rerank-question-file', type=Path,
                        help='Experiment-only: override and record the bounded Jev support question')
    rerank = parser.add_mutually_exclusive_group()
    rerank.add_argument('--action-condition-rerank',action='store_true',
                        help='Experiment-only: score constraint-planned requests by feasibility evidence')
    rerank.add_argument('--role-only-rerank',action='store_true',
                        help='Experiment-only: omit generated navigation queries from rerank objectives')
    rerank.add_argument('--grounded-rerank',action='store_true',
                        help='Experiment-only: blend source and full scores using source support')
    rerank.add_argument('--full-rerank-cards',action='store_true',
                        help='Experiment-only: include derived memory claims in Jev cards for paired ablation')
    execute(parser.parse_args())


if __name__ == "__main__":
    main()
