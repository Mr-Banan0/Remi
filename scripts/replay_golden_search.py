"""Repeat the complete Golden Search set using an immutable approved six-table Add bank."""
from __future__ import annotations

import argparse
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
from remi.storage import TABLES
from scripts.golden_adapter import actions, add_payload, input_catalog, metrics, score_case
from scripts.run_golden import compare, diagnostics, fingerprint, latency, verify_baseline


def execute(args):
    original = json.loads(args.original.read_text())
    dataset = json.loads(Path(original["dataset"]).read_text())
    baseline = json.loads(Path(original["baseline_result"]).read_text())
    verify_baseline(dataset, baseline)
    if not original["complete"] or args.output.exists() or not args.allow_copy_bank:
        raise ValueError("complete_original_fresh_output_and_explicit_bank_copy_required")
    os.umask(0o077)
    args.output.mkdir(parents=True)
    database = args.output / "memory.sqlite3"
    with sqlite3.connect(f"file:{original['database']}?mode=ro", uri=True) as source, sqlite3.connect(database) as target:
        source.backup(target)
    load_environment(args.env_file)
    os.environ["REMI_DB_PATH"] = str(database)
    engine = environment_engine()
    if {row[0] for row in engine.store.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")} != TABLES:
        raise ValueError("exactly_six_tables_required")
    owner = original["derivation"]["rhelm"]["memories"][0]["owner"]
    life_owner = original["lifecycle_add_calls"][-1]["snapshot"][0]["owner"]
    mapping, catalog = {}, {}
    for kind, action in actions(dataset):
        if kind == "add":
            refs, originals = input_catalog(add_payload(action, owner), action["turns"])
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
    report = {"mode": "all_search_replay_using_frozen_ingestion", "complete": False,
        "original_full_run": str(args.original), "dataset": original["dataset"], "dataset_sha256": original["dataset_sha256"],
        "baseline_result": original["baseline_result"], "ingestion_code_fingerprint": original["runtime_code_fingerprint"],
        "runtime_directory": str(root), "runtime_code_fingerprint": fingerprint(root), "started_at": time.time(),
        "add_calls": original["add_calls"], "lifecycle_add_calls": original["lifecycle_add_calls"],
        "questions": [], "lifecycle_searches": [], "errors": [], "database": str(database),
        "config": original["config"], "source_to_ref": mapping}
    def save(status):
        calls = diagnostics(engine)
        report["diagnostic_calls"] = calls
        report["summary"] = {"retrieval": {str(k): metrics(report["questions"], k) for k in (1, 3, 5, 10)},
            "add_latency": original["summary"]["add_latency"], "search_latency": latency(report["questions"]),
            "calls_scope": "Search replay only; Add was reused without new model calls"}
        (args.output / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
        (args.output / "run.json").write_text(json.dumps({"status": status, "updated_at": time.time(),
            "questions_completed": len(report["questions"]), "lifecycle_queries_completed": len(report["lifecycle_searches"]),
            "planned_questions": 13, "planned_lifecycle_queries": 3, "pid": os.getpid()}, indent=2))
    save("running")
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", headers={"Authorization": "Bearer " + token}, timeout=180) as client:
            for case in dataset["cases"]:
                question = case["question"]
                started = time.monotonic()
                response = client.post("/search", json={"user_id": owner, "query": question["text"], "top_k": 10},
                    headers={"x-memory-reference-time": question["day"] + "T23:59:59Z"})
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
                started = time.monotonic()
                response = client.post("/search", json={"user_id": life_owner, "query": old["query"], "top_k": 5})
                candidates = response.json()["data"] if response.status_code == 200 else []
                expected = ("搬家今天已完成", "2026-09-24") if "最终" in old["query"] else (
                    "李明现在住上海", "2026-09-24") if "2026年9月24日" in old["query"] else ("李明现在住杭州", "2026-10-01")
                report["lifecycle_searches"].append({"query": old["query"], "seconds": round(time.monotonic() - started, 3),
                    "http_status": response.status_code, "top1_expected_evidence": bool(candidates) and all(
                        term in candidates[0]["content"] for term in expected), "candidates": candidates, "trace": engine.last_search})
                save("running")
            report["complete"] = True
            save("complete")
            (args.output / "comparison.json").write_text(json.dumps(compare(report, baseline, dataset), ensure_ascii=False, indent=2))
    except Exception as error:
        report["errors"].append({"exception": type(error).__name__})
        save("interrupted")
        raise SystemExit(2) from None
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()
    print(json.dumps({"result": str(args.output / "result.json"), "metrics": report["summary"]["retrieval"]["10"]}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--original", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--allow-copy-bank", action="store_true")
    execute(parser.parse_args())


if __name__ == "__main__":
    main()
