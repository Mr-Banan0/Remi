"""Replay the complete frozen Golden through real local HTTP using configured providers."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import threading
import time
from collections import Counter, deque
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import httpx
import uvicorn

from remi.api import create_app, environment_engine
from remi.settings import load_environment
from remi.storage import SQLiteStore, TABLES
from remi.add_pipeline import PIPELINE
from scripts.golden_adapter import actions, add_payload, input_catalog, metrics, score_case


def fingerprint(root):
    digest = hashlib.sha256()
    for path in sorted(path for folder in ("src", "scripts") for path in (root / folder).rglob("*.py")):
        digest.update(str(path.relative_to(root)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def percentile(values, p):
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int((len(ordered) - 1) * p))] if ordered else None


def latency(records):
    values = [record["seconds"] for record in records if isinstance(record.get("seconds"), (int, float))]
    return {"mean": sum(values) / len(values) if values else None,
            "p50": percentile(values, .5), "p95": percentile(values, .95)}


def diagnostics(engine):
    return [dict(call) for resource in (engine.model, engine.embedder, engine.reranker)
            if resource is not None for call in resource.calls]


def summaries(report):
    questions, additions = report["questions"], report["add_calls"] + report["lifecycle_add_calls"]
    calls = report["diagnostic_calls"]
    types = sorted({result["question_type"] for result in questions})
    return {"retrieval": {str(k): metrics(questions, k) for k in (1, 3, 5, 10)},
        "by_type": {kind: metrics([item for item in questions if item["question_type"] == kind]) for kind in types},
        "mean_facet_quote_coverage": sum(item["facet_quote_coverage"] for item in questions) / len(questions) if questions else 0,
        "add_success_rate": sum(record["success"] for record in additions) / len(additions) if additions else 0,
        "add_latency": latency(additions), "search_latency": latency(questions),
        "calls_by_kind": dict(Counter(call["kind"] for call in calls)),
        "provider_errors": dict(Counter((call.get("error_type", "http_error"), str(call.get("http_status", "")))
            for call in calls if call.get("error_type") or (call.get("http_status") or 0) >= 400))}


def completed_lifecycle(records, step):
    return any(record["step"] == step and record.get("success") and record.get("processing_complete")
               for record in records)


def progress_counts(report):
    rhelm = {record["request_id"] for record in report["add_calls"]
             if record.get("success") and record.get("processing_complete")}
    lifecycle = {record["step"] for record in report["lifecycle_add_calls"]
                 if record.get("success") and record.get("processing_complete")}
    return {"add_completed": len(rhelm) + len(lifecycle),
            "add_attempts": len(report["add_calls"]) + len(report["lifecycle_add_calls"]),
            "rhelm_add_completed": len(rhelm), "lifecycle_add_completed": len(lifecycle),
            "questions_completed": len(report["questions"]),
            "lifecycle_queries_completed": len(report["lifecycle_searches"])}


class CallJournal(deque):
    """Persist safe physical-call diagnostics independently of report aggregation."""
    def __init__(self, path):
        super().__init__(maxlen=2048)
        self.path, self.lock = path, threading.Lock()
    def append(self, call):
        super().append(call)
        with self.lock, self.path.open("a") as file:
            file.write(json.dumps(call, ensure_ascii=False) + "\n")


def compare(report, baseline, dataset):
    lookup = {item["question_id"]: item for item in baseline["questions"]}
    results = []
    for item in report["questions"]:
        old = lookup[item["question_id"]]
        missing, old_missing = [ref for ref, rank in item["evidence_ranks"].items() if rank is None], [
            ref for ref, rank in old["evidence_ranks"].items() if rank is None]
        results.append({"question_id": item["question_id"], "question": item["question"],
            "traceweave_recall": old["evidence_recall"], "remi_recall": item["evidence_recall"],
            "old_missing": old_missing, "missing": missing,
            "persistent_missing": sorted(set(missing) & set(old_missing)),
            "resolved_missing": sorted(set(old_missing) - set(missing)),
            "new_missing": sorted(set(missing) - set(old_missing)),
            "classification": "regressed" if item["evidence_recall"] < old["evidence_recall"] else
                "improved" if item["evidence_recall"] > old["evidence_recall"] else "unchanged"})
    return {"comparison_type": "same-dataset historical baseline; not a repeated paired causal experiment",
        "scoring": "visible_raw_source_v2", "dataset_sha256": report["dataset_sha256"],
        "same_dataset": baseline["dataset_sha256"] == report["dataset_sha256"],
        "traceweave": {"result": report["baseline_result"], "code": baseline["runtime_code_fingerprint"],
            "metrics": metrics(baseline["questions"]), "add_latency": baseline["summary"]["add_latency"],
            "search_latency": baseline["summary"]["search_latency"]},
        "remi": {"code": report["runtime_code_fingerprint"], "metrics": metrics(report["questions"]),
            "add_latency": report["summary"]["add_latency"], "search_latency": report["summary"]["search_latency"]},
        "cases": results, "all_bad_cases": [item for item in results if item["missing"]]}


def verify_baseline(dataset, baseline):
    mapping, catalog = {}, {}
    owner = baseline["user_id"]
    for kind, action in actions(dataset):
        if kind != "add":
            continue
        payload = add_payload(action, owner)
        for index, message in enumerate(payload["messages"]):
            key = "source:" + hashlib.sha256(f"{owner}\0{payload['request_id']}\0{index}".encode()).hexdigest()[:24]
            mapping[key] = action["turns"][index // 2]["ref"]
            catalog[key] = {"role": message["role"], "content": message["content"]}
    lookup = {item["question_id"]: item for item in baseline["questions"]}
    for case in dataset["cases"]:
        old = lookup[case["question"]["id"]]
        result = score_case(case, old["candidates"], mapping, catalog, "traceweave")
        if result["evidence_ranks"] != old["evidence_ranks"]:
            raise ValueError("baseline_scoring_parity_failed:" + old["question_id"])
    return True


def execute(args):
    root = Path(__file__).resolve().parents[1]
    dataset_bytes = args.dataset.read_bytes()
    dataset, baseline = json.loads(dataset_bytes), json.loads(args.baseline.read_text())
    dataset_hash = hashlib.sha256(dataset_bytes).hexdigest()
    if baseline["dataset_sha256"] != dataset_hash or not baseline.get("complete"):
        raise ValueError("baseline_dataset_or_completion_mismatch")
    verify_baseline(dataset, baseline)
    if args.output.exists() and not args.resume:
        raise FileExistsError("fresh_output_directory_required")
    if not args.allow_create_schema:
        raise PermissionError("explicit_six_table_creation_required")
    load_environment(args.env_file)
    args.output.mkdir(parents=True, mode=0o700, exist_ok=args.resume)
    os.umask(0o077)
    database = args.output / "memory.sqlite3"
    previous_report = json.loads((args.output / "result.json").read_text()) if args.resume else None
    if not args.resume:
        SQLiteStore.initialize(database, allow_create_schema=True)
    os.environ["REMI_DB_PATH"] = str(database)
    engine = environment_engine()
    token = uuid4().hex
    owners = [row[0] for row in engine.store.connection.execute("SELECT DISTINCT owner FROM requests")] if args.resume else []
    owner = next((value for value in owners if value.startswith("golden:")), "golden:" + token)
    life_owner = "lifecycle:" + owner.split(":", 1)[1]
    for resource in (engine.model, engine.embedder, engine.reranker):
        resource.calls = CallJournal(args.output / "diagnostics.jsonl")
    app = create_app(engine, api_key=token)
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    port = listener.getsockname()[1]
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(.05)
    if not server.started:
        raise RuntimeError("local_service_start_failed")
    plan = list(actions(dataset))
    report = {"architecture": PIPELINE, "complete": False, "dataset": str(args.dataset),
        "dataset_sha256": dataset_hash, "baseline_result": str(args.baseline), "runtime_directory": str(root),
        "runtime_code_fingerprint": fingerprint(root), "active_runtime_code_fingerprint": fingerprint(root),
        "active_runtime_directory": str(root), "started_at": time.time(), "planned_add_calls": sum(
            kind == "add" for kind, action in plan) + len(dataset["lifecycle"]), "planned_questions": len(dataset["cases"]),
        "add_calls": [], "questions": [], "lifecycle_add_calls": [], "lifecycle_searches": [],
        "protocol_checks": [], "errors": [], "diagnostic_calls": [], "source_to_ref": {},
        "config": {"top_k": 10, "batch_turns": 8, "scoring": "visible_raw_source_v2", "model": engine.model.model,
            "reranker": engine.reranker.model, "embedding": "text-embedding-v4", "embedding_dimensions": engine.embedder.dimensions,
            "settings": vars(engine.settings)}, "database": str(database)}
    prior_calls = []
    if previous_report:
        if previous_report['active_runtime_code_fingerprint'] != fingerprint(root):
            raise ValueError('resume_requires_frozen_runtime')
        if previous_report['dataset_sha256'] != dataset_hash:
            raise ValueError('resume_requires_frozen_dataset')
        report = previous_report
        prior_calls = list(report["diagnostic_calls"])
        report["resumed_at"] = time.time()
        report["runner_resume_code_fingerprint"] = fingerprint(root)
        report["active_runtime_directory"] = str(root)
        report["physical_call_statistics_complete"] = False
    mapping, catalog, last_payload = {}, {}, None
    def save(status):
        report["diagnostic_calls"] = [*prior_calls, *diagnostics(engine)]
        # JSON keys remain strings; physical call error classes are safe diagnostics.
        report["summary"] = summaries(report)
        report["summary"]["provider_errors"] = {"|".join(key): value for key, value in report["summary"]["provider_errors"].items()}
        report["source_to_ref"] = mapping
        (args.output / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
        (args.output / "run.json").write_text(json.dumps({"status": status, "pid": os.getpid(),
            "runtime_directory": str(root), "started_at": report["started_at"], "updated_at": time.time(),
            **progress_counts(report), "planned_add_calls": report["planned_add_calls"],
            "planned_questions": report["planned_questions"], "result": str(args.output / "result.json")}, indent=2))
    def post(client, path, payload, clock=None):
        seconds = 0.0
        for attempt in range(1, 4):
            started = time.monotonic()
            response = client.post(path, json=payload, headers={"x-memory-reference-time": clock} if clock else None)
            elapsed = time.monotonic() - started
            seconds += elapsed
            with (args.output / 'http-attempts.jsonl').open('a') as journal:
                journal.write(json.dumps({'path': path, 'request_id': payload.get('request_id'),
                    'query': payload.get('query'), 'attempt': attempt, 'http_status': response.status_code,
                    'seconds': round(elapsed, 3)}) + '\n')
            if response.status_code not in {429, 502, 503, 504}:
                break
        return response, round(seconds, 3)
    save("running")
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", headers={"Authorization": "Bearer " + token}, timeout=1800) as client:
            client.get("/ready").raise_for_status()
            for kind, action in plan:
                if kind == "add":
                    payload = add_payload(action, owner)
                    completed = engine.store.get("requests", owner, payload["request_id"])
                    if completed and completed["status"] == "complete":
                        refs, originals = input_catalog(payload, action["turns"])
                        mapping.update(refs)
                        catalog.update(originals)
                        last_payload = payload
                        if not any(record["request_id"] == payload["request_id"] for record in report["add_calls"]):
                            seconds = (datetime.fromisoformat(completed["completed_at"]) - datetime.fromisoformat(completed["created_at"])).total_seconds()
                            report["add_calls"].append({"request_id": payload["request_id"], "turn_refs": [turn["ref"] for turn in action["turns"]],
                                "units": len(action["turns"]), "seconds": seconds, "http_status": 200, "success": True,
                                "processing_complete": True, "latency_basis": "recovered_request_checkpoint_wall_clock"})
                            report["call_statistics_gap_request_ids"] = [payload["request_id"]]
                        save("running")
                        continue
                    response, seconds = post(client, "/add", payload)
                    progress = engine.store.get("requests", owner, payload["request_id"])
                    record = {"request_id": payload["request_id"], "turn_refs": [turn["ref"] for turn in action["turns"]],
                        "units": len(action["turns"]), "seconds": seconds, "http_status": response.status_code,
                        "success": response.status_code == 200 and response.json().get("success") is True,
                        "processing_complete": bool(progress and progress["status"] == "complete")}
                    report["add_calls"].append(record)
                    if not record["success"]:
                        report["errors"].append({"operation": "add", "http_status": response.status_code})
                        raise RuntimeError("golden_add_interrupted")
                    refs, originals = input_catalog(payload, action["turns"])
                    mapping.update(refs)
                    catalog.update(originals)
                    last_payload = payload
                    print(f"Add {len(report['add_calls'])}: {record['units']} units, {seconds}s", flush=True)
                else:
                    question = action["question"]
                    if any(record["question_id"] == question["id"] and record.get('http_status') == 200 for record in report["questions"]):
                        continue
                    response, seconds = post(client, "/search", {"user_id": owner, "query": question["text"], "top_k": 10},
                                             question["day"] + "T23:59:59Z")
                    candidates = response.json()["data"] if response.status_code == 200 else []
                    record = score_case(action, candidates, mapping, catalog)
                    record.update(seconds=seconds, http_status=response.status_code, candidates=candidates,
                                  trace=engine.last_search, input_complete=all(ref in mapping.values() for ref in question["evidence"]))
                    if response.status_code != 200:
                        report["errors"].append({"operation": "search", "question_id": question["id"], "http_status": response.status_code})
                        raise RuntimeError('golden_search_interrupted')
                    report["questions"].append(record)
                    print(f"Search {len(report['questions'])}: recall={record['evidence_recall']:.3f}, {seconds}s", flush=True)
                save("running")
            if last_payload:
                before = len(engine.store.memories(owner))
                response, _ = post(client, "/add", last_payload)
                report["protocol_checks"].append({"name": "idempotent_add", "passed": response.status_code == 200
                    and len(engine.store.memories(owner)) == before})
                conflict = {**last_payload, "messages": [{"role": "user", "content": "changed payload"}]}
                response, _ = post(client, "/add", conflict)
                report["protocol_checks"].append({"name": "request_id_conflict", "passed": response.status_code == 409})
            for index, case in enumerate(dataset["lifecycle"]):
                if completed_lifecycle(report["lifecycle_add_calls"], index + 1):
                    continue
                payload = {"user_id": life_owner, "request_id": f"lifecycle-{index}", "session_id": "lifecycle", "messages": [
                    {"role": "user", "content": case["text"], "timestamp": int(datetime.fromisoformat(case["day"]).replace(
                        tzinfo=timezone.utc).timestamp() * 1000)}]}
                response, seconds = post(client, "/add", payload)
                progress = engine.store.get("requests", life_owner, payload["request_id"])
                report["lifecycle_add_calls"].append({"step": index + 1, "seconds": seconds, "http_status": response.status_code,
                    "success": response.status_code == 200, "processing_complete": bool(progress and progress["status"] == "complete"),
                    "snapshot": [memory.model_dump() for memory in engine.store.memories(life_owner)]})
                if response.status_code != 200:
                    report["errors"].append({"operation": "lifecycle_add", "http_status": response.status_code})
                    raise RuntimeError("golden_lifecycle_interrupted")
                save("running")
            for question, expected in (("李明那次从北京搬到上海的搬家最终是哪天完成的？", ("搬家今天已完成", "2026-09-24")),
                ("李明在2026年9月24日住在哪里？", ("李明现在住上海", "2026-09-24")),
                ("李明现在住在哪里？", ("李明现在住杭州", "2026-10-01"))):
                if any(record["query"] == question for record in report["lifecycle_searches"]):
                    continue
                response, seconds = post(client, "/search", {"user_id": life_owner, "query": question, "top_k": 5})
                if response.status_code != 200:
                    raise RuntimeError('golden_lifecycle_search_interrupted')
                candidates = response.json()["data"]
                report["lifecycle_searches"].append({"query": question, "seconds": seconds, "http_status": response.status_code,
                    "top1_expected_evidence": bool(candidates) and all(term in candidates[0]["content"] for term in expected),
                    "candidates": candidates, "trace": engine.last_search})
                save("running")
            report["derivation"] = {name: {"memories": [memory.model_dump() for memory in engine.store.memories(user)],
                "requests": engine.store.list("requests", user)} for name, user in (("rhelm", owner), ("lifecycle", life_owner))}
            actual = {row[0] for row in engine.store.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            report["tables"] = sorted(actual)
            report["protocol_checks"].append({"name": "exactly_six_tables", "passed": actual == TABLES})
            report["complete"], report["ended_at"] = True, time.time()
            save("complete")
            comparison = compare(report, baseline, dataset)
            (args.output / "comparison.json").write_text(json.dumps(comparison, ensure_ascii=False, indent=2))
    except Exception as error:
        report["errors"].append({"operation": "runner", "exception": type(error).__name__})
        save("interrupted")
        raise SystemExit(2) from None
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()
    print(json.dumps({"result": str(args.output / "result.json"), "complete": report["complete"],
                      "metrics": report["summary"]["retrieval"]["10"]}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--allow-create-schema", action="store_true")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    if args.execute:
        execute(args)
    else:
        dataset = json.loads(args.dataset.read_text())
        plan = list(actions(dataset))
        print(json.dumps({"add_calls": sum(kind == "add" for kind, _ in plan) + len(dataset["lifecycle"]),
            "questions": len(dataset["cases"]), "turns": len(dataset["input_turns"]), "paid_calls": 0}))


if __name__ == "__main__":
    main()
