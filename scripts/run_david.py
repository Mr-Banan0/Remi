"""Run the complete causal David dialogue with durable, resumable HTTP checkpoints.

Original dialogue enters Add and the question enters Search. Answers and evidence
labels stay in this local evaluator. Existing completed steps are restored before
creating providers, and the database uses the six approved Remi tables.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import socket
import sys
import threading
import time
from collections import Counter, deque
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import httpx
import uvicorn

from remi.api import create_app, environment_engine
from remi.settings import Settings, load_environment
from remi.storage import SQLiteStore, TABLES
from scripts.analyze_golden import analyze
from scripts.golden_adapter import (AML_MESSAGE_LIMIT, AML_METRIC_CUTOFFS, AML_TOP_K, AML_WORD_LIMIT,
    actions, add_payload, input_catalog, metrics, score_case)
from scripts.run_golden import latency, summaries


DIALOGUE_TYPES = {"fact", "temporal", "aggregation", "hallucination", "misleading"}
TURN_REF = re.compile(r"\d{4}-\d{2}-\d{2}:\d+\Z")
SAFE_CALL_FIELDS = {"kind", "model", "attempt", "batch_size", "seconds", "error_type", "http_status"}
RETRY_HTTP = {408, 429, 500, 502, 503, 504}


def digest_json(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def atomic_json(path, value):
    """Commit one complete JSON file and sync its directory entry."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            json.dump(value, file, ensure_ascii=False, indent=2)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary.exists():
            temporary.unlink()


@contextmanager
def run_lock(output):
    """Keep the same run directory exclusive across fresh and resumed processes."""
    path = Path(output) / ".run.lock"
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("run_directory_already_locked") from None
        os.ftruncate(descriptor, 0)
        os.write(descriptor, json.dumps({"pid": os.getpid(), "locked_at": time.time()}).encode())
        os.fsync(descriptor)
        yield
    finally:
        os.close(descriptor)


def read_journal(path):
    records, malformed = [], []
    if not Path(path).exists():
        return records, malformed
    for number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
            if not isinstance(record, dict):
                raise ValueError("object_required")
            records.append(record)
        except (ValueError, json.JSONDecodeError):
            malformed.append(number)
    return records, malformed


class DurableJournal:
    """Use one lock and synced append per physical call, including parallel reranks."""

    def __init__(self, path):
        self.path, self.lock, self.context = Path(path), threading.Lock(), {}
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # A killed process can leave an incomplete final line. Preserve that line
        # as a diagnosable gap and start the next record on its own line.
        if self.path.exists() and self.path.stat().st_size:
            with self.path.open("rb") as file:
                file.seek(-1, os.SEEK_END)
                incomplete = file.read(1) != b"\n"
            if incomplete:
                descriptor = os.open(self.path, os.O_WRONLY | os.O_APPEND)
                try:
                    os.write(descriptor, b"\n")
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)

    def set_context(self, **context):
        with self.lock:
            self.context = context

    def append(self, call, *, provider=False):
        with self.lock:
            safe = {key: value for key, value in call.items() if key in SAFE_CALL_FIELDS} if provider else dict(call)
            record = {**self.context, **safe, "journal_id": uuid4().hex, "recorded_at": time.time()}
            payload = (json.dumps(record, ensure_ascii=False) + "\n").encode()
            descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                offset = 0
                while offset < len(payload):
                    offset += os.write(descriptor, payload[offset:])
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            return record


class ProviderCalls(deque):
    def __init__(self, journal):
        super().__init__(maxlen=256)
        self.journal = journal

    def append(self, call):
        super().append(self.journal.append(call, provider=True))


def load_dataset(data_dir, character, golden_dataset):
    """Match the established RHELM selection and UTC causal replay policy."""
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", character) or character in {".", ".."}:
        raise ValueError("character_directory_invalid")
    root = Path(data_dir).resolve()
    files = sorted((root / "conversations" / character).glob("conversation_*.json"))
    qa_path = root / "QA_final" / f"low_score_qa_{character}_all_validated.jsonl"
    if not files or not qa_path.is_file():
        raise ValueError("complete_local_rhelm_input_required")
    turns, seen = [], set()
    manifest, raw_sources = [], {}
    for path in [*files, qa_path]:
        raw = path.read_bytes()
        raw_sources[path] = raw
        manifest.append({"path": str(path.relative_to(root)), "sha256": hashlib.sha256(raw).hexdigest()})
        if path == qa_path:
            continue
        sample = json.loads(raw)
        day = date.fromisoformat(sample["date"]).isoformat()
        if sample.get("character") != character:
            raise ValueError("conversation_character_mismatch")
        for item in sample["conversation"]:
            stamp = datetime.fromisoformat(item["timestamp"].replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            stamp = stamp.astimezone(timezone.utc)
            ref = f"{day}:{int(item['turn'])}"
            if ref in seen or stamp.date().isoformat() != day:
                raise ValueError("conversation_turn_identity_or_clock_invalid")
            if not isinstance(item["user"], str) or not isinstance(item["assistant"], str):
                raise ValueError("conversation_content_requires_text")
            seen.add(ref)
            turns.append({"ref": ref, "day": day, "timestamp_ms": int(stamp.timestamp() * 1000),
                          "user": item["user"], "assistant": item["assistant"]})
    turns.sort(key=lambda turn: (turn["timestamp_ms"], int(turn["ref"].rsplit(":", 1)[1])))
    by_ref = {turn["ref"]: turn for turn in turns}
    cases, excluded, question_ids = [], Counter(), set()
    for line in raw_sources[qa_path].decode("utf-8").splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        if item["question_type"] not in DIALOGUE_TYPES:
            excluded["external_or_mixed_question"] += 1
            continue
        raw_evidence = item.get("supporting_evidence")
        evidence = [part.strip() for entry in raw_evidence for part in entry.split(",")] if (
            isinstance(raw_evidence, list) and all(isinstance(entry, str) for entry in raw_evidence)) else None
        if evidence is None or any(not TURN_REF.fullmatch(ref) for ref in evidence):
            excluded["external_or_unparseable_evidence"] += 1
            continue
        evidence = list(dict.fromkeys(evidence))
        if not evidence:
            excluded["no_evidence_label"] += 1
            continue
        day = date.fromisoformat(item["question_date"][:10]).isoformat()
        if any(ref not in by_ref for ref in evidence):
            excluded["evidence_outside_loaded_history"] += 1
            continue
        if any(by_ref[ref]["day"] > day for ref in evidence):
            excluded["future_evidence"] += 1
            continue
        if item["id"] in question_ids:
            raise ValueError("duplicate_question_id")
        question_ids.add(item["id"])
        cases.append({"question": {"id": item["id"], "day": day, "kind": item["question_type"],
            "text": item["question"], "answer": item["answer"], "evidence": evidence,
            "characteristics": list(item.get("characteristics") or [])}})
    cases.sort(key=lambda case: (case["question"]["day"], case["question"]["id"]))
    golden_raw = Path(golden_dataset).read_bytes()
    golden = json.loads(golden_raw)
    known = sorted(case["question"]["id"] for case in golden["cases"])
    if golden["character"] != character or len(known) != len(set(known)) or not set(known) <= question_ids:
        raise ValueError("known_regression_ids_mismatch")
    source_digest = hashlib.sha256()
    for path, raw in sorted(raw_sources.items()):
        source_digest.update(path.name.encode())
        source_digest.update(raw)
    dataset = {"character": character, "input_turns": turns, "cases": cases, "lifecycle": [],
               "rhelm_source_sha256": source_digest.hexdigest()}
    return dataset, {"source_files": manifest, "source_manifest_sha256": digest_json(manifest),
        "dataset_sha256": digest_json(dataset), "golden_sha256": hashlib.sha256(golden_raw).hexdigest(),
        "known_regression_ids": known, "excluded": dict(excluded)}


def code_fingerprints(root):
    root = Path(root)
    def fingerprint(paths):
        digest = hashlib.sha256()
        for path in sorted(paths):
            digest.update(str(path.relative_to(root)).encode())
            digest.update(path.read_bytes())
        return digest.hexdigest()
    service = [*root.joinpath("src").rglob("*.py"), *[root / name for name in ("pyproject.toml", "uv.lock") if (root / name).is_file()]]
    runner = [root / "scripts" / name for name in ("run_david.py", "golden_adapter.py", "run_golden.py", "analyze_golden.py")]
    return {"service_code_fingerprint": fingerprint(service), "runner_code_fingerprint": fingerprint(runner)}


def public_url(value):
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("provider_base_url_requires_plain_http_url")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))


def public_configuration():
    return {"model": os.getenv("LLM_MODEL", "gpt-4o-mini"),
        "llm_base_url": public_url(os.getenv("LLM_BASE_URL", "https://api.quickrouter.us/v1")),
        "llm_timeout": float(os.getenv("REMI_LLM_TIMEOUT", "45")),
        "embedding": "text-embedding-v4", "embedding_dimensions": int(os.getenv("REMI_EMBEDDING_DIMENSIONS", "1024")),
        "embedding_base_url": public_url(os.getenv("DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")),
        "reranker": os.getenv("TYPESAFE_MODEL", "jev-1.13.0"),
        "reranker_base_url": public_url(os.getenv("TYPESAFE_BASE_URL", "https://api.quickrouter.ai/v1")),
        "reranker_timeout": float(os.getenv("REMI_RERANK_TIMEOUT", "8")),
        "settings": vars(Settings.from_environment())}


def make_contract(root, metadata, config, plan, max_http_attempts):
    return {**code_fingerprints(root), "data": metadata,
        "configuration": config, "top_k": AML_TOP_K,
        "add_chunking": {"message_limit": AML_MESSAGE_LIMIT, "word_limit": AML_WORD_LIMIT,
                         "word_counter": "unicode_word_units_v1"},
        "search_reference_time": "not_sent",
        "scoring": "visible_raw_source_v2", "clock": "UTC; same-day dialogue precedes date-only question",
        "max_http_attempts": max_http_attempts,
        "planned_actions_sha256": digest_json([(kind, action) for kind, action in plan])}


def validate_resume_contract(old, new):
    if old != new:
        changed = sorted(key for key in set(old) | set(new) if old.get(key) != new.get(key))
        raise ValueError("resume_contract_mismatch:" + ",".join(changed))


def step_identity(index, kind, action):
    return f"{index:06d}", kind, action["request_id"] if kind == "add" else action["question"]["id"]


def load_steps(output, plan):
    steps = {}
    for index, (kind, action) in enumerate(plan):
        name, _, identity = step_identity(index, kind, action)
        path = Path(output) / "steps" / f"{name}.json"
        if not path.exists():
            continue
        step = json.loads(path.read_text())
        if step.get("index") != index or step.get("kind") != kind or step.get("identity") != identity or not step.get("complete"):
            raise ValueError("step_checkpoint_identity_or_status_invalid")
        steps[index] = step
    completed = sorted(steps)
    if completed != list(range(len(completed))):
        raise ValueError("step_checkpoints_require_causal_prefix")
    return steps


def commit_step(output, index, kind, action, record):
    name, _, identity = step_identity(index, kind, action)
    step = {"index": index, "kind": kind, "identity": identity, "complete": True, "record": record}
    atomic_json(Path(output) / "steps" / f"{name}.json", step)
    return step


def request_complete(store, owner, request_id):
    request = store.get("requests", owner, request_id)
    return request if request and request.get("status") == "complete" else None


def recovered_add_record(action, request):
    seconds = (datetime.fromisoformat(request["completed_at"]) - datetime.fromisoformat(request["created_at"])).total_seconds()
    return {"request_id": action["request_id"], "turn_refs": [turn["ref"] for turn in action["turns"]],
        "units": len(action["turns"]), "seconds": seconds, "http_status": 200, "success": True,
        "processing_complete": True, "latency_basis": "recovered_request_checkpoint_wall_clock"}


def post_with_retries(client, path, payload, journal, *, attempts=3, retry_seconds=2, timeout=None):
    """Reuse exactly the same Add identity on bounded service/network retries."""
    started = time.monotonic()
    for attempt in range(1, attempts + 1):
        call_started = time.monotonic()
        try:
            response = client.post(path, json=payload, timeout=timeout)
            journal.append({"path": path, "attempt": attempt, "http_status": response.status_code,
                            "seconds": round(time.monotonic() - call_started, 6)})
            if response.status_code not in RETRY_HTTP or attempt == attempts:
                return response, round(time.monotonic() - started, 3), attempt
        except httpx.TransportError as error:
            journal.append({"path": path, "attempt": attempt, "http_status": None,
                            "error_type": type(error).__name__, "seconds": round(time.monotonic() - call_started, 6)})
            if attempt == attempts:
                raise RuntimeError("local_http_transport_retry_exhausted") from None
        if retry_seconds:
            time.sleep(min(retry_seconds * attempt, 10))
    raise RuntimeError("local_http_retry_exhausted")


def report_summary(report, known_ids):
    summary = summaries(report)
    summary["provider_errors"] = {"|".join(key): value for key, value in summary["provider_errors"].items()}
    groups = {"all": report["questions"], "known_regression": [record for record in report["questions"] if record["question_id"] in known_ids],
              "remaining_same_person": [record for record in report["questions"] if record["question_id"] not in known_ids]}
    summary["groups"] = {group: {str(k): metrics(records, k) for k in AML_METRIC_CUTOFFS}
                         for group, records in groups.items()}
    summary["http_errors"] = dict(Counter(str(call.get("http_status")) if call.get("http_status") else call.get("error_type", "unknown")
        for call in report["http_attempts"] if call.get("error_type") or (call.get("http_status") or 0) >= 400))
    summary["http_attempts"] = len(report["http_attempts"])
    summary["processing_units_completed"] = sum(record["units"] for record in report["add_calls"])
    return summary


def validation_summary(requests):
    records = [record for request in requests for unit in request.get("units", {}).values()
               for record in unit.get("validation", [])]
    return {"records": len(records), "by_stage": dict(Counter(record.get("stage", "unknown") for record in records)),
        "validated_records": sum(not record.get("error") for record in records),
        "error_records": sum(bool(record.get("error")) for record in records),
        "by_error": dict(Counter(record["error"] for record in records if record.get("error"))),
        "by_kind": dict(Counter(record.get("kind", "unknown") for record in records)),
        "by_operation": dict(Counter(record.get("operation", "unknown") for record in records)),
        "stage_error_kind": dict(Counter("|".join(str(record.get(key) or ("accepted" if key == "error" else "unknown")) for key in ("stage", "error", "kind"))
                                       for record in records))}


def compare_baseline(report, dataset, baseline_path):
    """Rescore historical TraceWeave raw originals on the exact causal input."""
    baseline = json.loads(Path(baseline_path).read_text())
    expected_chunking = {"message_limit": AML_MESSAGE_LIMIT, "word_limit": AML_WORD_LIMIT,
                         "word_counter": "unicode_word_units_v1"}
    baseline_contract = baseline.get("config", {})
    if (baseline_contract.get("top_k") != AML_TOP_K
            or baseline_contract.get("add_chunking") != expected_chunking
            or baseline_contract.get("search_reference_time") != "not_sent"):
        raise ValueError("historical_baseline_aml_full_contract_mismatch")
    if not baseline.get("complete") or baseline.get("character") != dataset["character"]:
        raise ValueError("historical_baseline_completion_or_character_mismatch")
    if baseline.get("config", {}).get("dataset_sha256") != dataset["rhelm_source_sha256"]:
        raise ValueError("historical_baseline_source_file_hash_mismatch")
    lookup = {record["question_id"]: record.get("variants", {}).get("tool_search", record) for record in baseline["questions"]}
    if set(lookup) != {case["question"]["id"] for case in dataset["cases"]}:
        raise ValueError("historical_baseline_question_set_mismatch")
    mapping, catalog = {}, {}
    for kind, action in actions(dataset):
        if kind != "add":
            continue
        payload = add_payload(action, baseline["user_id"])
        for index, message in enumerate(payload["messages"]):
            key = "source:" + hashlib.sha256(f"{baseline['user_id']}\0{payload['request_id']}\0{index}".encode()).hexdigest()[:24]
            refs = action.get("message_refs", [turn["ref"] for turn in action["turns"] for _ in range(2)])
            mapping[key], catalog[key] = refs[index], {"role": message["role"], "content": message["content"]}
    scored = [score_case(case, lookup[case["question"]["id"]]["candidates"], mapping, catalog, "traceweave") for case in dataset["cases"]]
    for record in scored:
        old = lookup[record["question_id"]]
        if old.get("evidence_ranks") != record["evidence_ranks"]:
            raise ValueError("historical_baseline_strict_scoring_parity_failed:" + record["question_id"])
    old = {record["question_id"]: record for record in scored}
    cases = []
    for record in report["questions"]:
        previous = old[record["question_id"]]
        missing = {ref for ref, rank in record["evidence_ranks"].items() if rank is None}
        old_missing = {ref for ref, rank in previous["evidence_ranks"].items() if rank is None}
        cases.append({"question_id": record["question_id"], "question": record["question"],
            "old_recall": previous["evidence_recall"], "recall": record["evidence_recall"],
            "persistent_missing": sorted(missing & old_missing), "resolved_missing": sorted(old_missing - missing),
            "new_missing": sorted(missing - old_missing), "missing": sorted(missing)})
    return {"scope": "same-person development; historical baseline, no final-answer judge or paired causal attribution",
        "baseline": str(baseline_path), "scoring": "visible_raw_source_v2", "baseline_scoring_parity": True,
        "traceweave": metrics(scored), "remi": metrics(report["questions"]), "cases": cases,
        "all_bad_cases": [case for case in cases if case["missing"]]}


def execute(args):
    root = Path(__file__).resolve().parents[1]
    dataset, metadata = load_dataset(args.data_dir, args.character, args.golden_dataset)
    plan = list(actions(dataset))
    preview = plan_preview(dataset, metadata, plan)
    if (preview["questions"] != len(dataset["cases"])
            or preview["known_regression_questions"] != len(metadata["known_regression_ids"])
            or preview["processing_units"] != preview["turns"]
            or preview["max_add_messages"] > AML_MESSAGE_LIMIT
            or preview["max_add_words"] > AML_WORD_LIMIT):
        raise ValueError("david_plan_must_cover_all_turns_and_obey_aml_chunk_limits")
    if not args.allow_create_schema:
        raise PermissionError("explicit_approved_six_table_scope_required")
    load_environment(args.env_file)
    config = public_configuration()
    contract = make_contract(root, metadata, config, plan, args.max_http_attempts)
    contract["retry_seconds"] = args.retry_seconds
    if args.baseline:
        compare_baseline({"questions": []}, dataset, args.baseline)
        contract["baseline_sha256"] = hashlib.sha256(args.baseline.read_bytes()).hexdigest()
    existed = args.output.exists()
    if existed and not args.resume:
        raise FileExistsError("fresh_output_directory_required")
    if args.resume and not existed:
        raise FileNotFoundError("existing_run_directory_required_for_resume")
    os.umask(0o077)
    args.output.mkdir(parents=True, mode=0o700, exist_ok=args.resume)
    with run_lock(args.output):
        state_path, result_path = args.output / "state.json", args.output / "result.json"
        if args.resume:
            state = json.loads(state_path.read_text())
            validate_resume_contract(state["contract"], contract)
        else:
            state = {"owner": "david:" + uuid4().hex, "started_at": time.time(), "contract": contract,
                     "runtime_directory": str(root), "character": args.character}
            atomic_json(state_path, state)
        steps = load_steps(args.output, plan)
        if result_path.exists():
            previous = json.loads(result_path.read_text())
            if previous.get("complete"):
                if len(steps) != len(plan):
                    raise ValueError("complete_report_requires_all_step_checkpoints")
                print(json.dumps({"status": "already_complete", "result": str(result_path), "paid_calls": 0}), flush=True)
                return previous
        else:
            previous = None
        previous_run_path = args.output / "run.json"
        previous_run = json.loads(previous_run_path.read_text()) if previous_run_path.exists() else {}
        unclean_resume = args.resume and previous_run.get("status") in {"starting", "running", "reporting"}
        import remi
        if Path(remi.__file__).resolve().parent.parent != (root / "src").resolve():
            raise ValueError("runtime_import_requires_frozen_src_on_pythonpath")
        database = args.output / "memory.sqlite3"
        if not database.exists():
            if steps:
                raise ValueError("saved_steps_require_existing_database")
            SQLiteStore.initialize(database, allow_create_schema=True)
        os.environ["REMI_BACKEND"] = "sqlite"
        os.environ["REMI_DB_PATH"] = str(database)
        engine = environment_engine()
        actual_tables = {row[0] for row in engine.store.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if actual_tables != TABLES or engine.store.connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            engine.close()
            raise ValueError("database_integrity_or_approved_table_scope_invalid")
        owner = state["owner"]
        provider_journal = DurableJournal(args.output / "diagnostics.jsonl")
        http_journal = DurableJournal(args.output / "http-attempts.jsonl")
        for resource in (engine.model, engine.embedder, engine.reranker):
            if resource is not None:
                resource.calls = ProviderCalls(provider_journal)
        report = {"architecture": "Remi-David", "complete": False, "character": args.character,
            "dataset": str(args.data_dir), "dataset_sha256": metadata["dataset_sha256"],
            "source_manifest_sha256": metadata["source_manifest_sha256"], "source_files": metadata["source_files"],
            "rhelm_source_sha256": dataset.get("rhelm_source_sha256"),
            "runtime_directory": str(root), "runtime_code_fingerprint": contract["service_code_fingerprint"],
            "runner_code_fingerprint": contract["runner_code_fingerprint"], "started_at": state["started_at"],
            "user_id": owner, "database": str(database), "config": config,
            "known_regression_ids": metadata["known_regression_ids"], "excluded": metadata["excluded"],
            "planned_add_calls": preview["add_calls"], "planned_questions": preview["questions"],
            "loaded_turns": preview["turns"], "planned_processing_units": preview["processing_units"],
            "add_calls": [], "questions": [], "lifecycle_add_calls": [], "lifecycle_searches": [],
            "diagnostic_calls": [], "http_attempts": [], "protocol_checks": [], "errors": list(previous.get("errors", [])) if previous else [], "source_to_ref": {},
            "scope": "same-person development diagnostic; strict visible-original retrieval evidence; no final-answer judge"}
        report["unclean_resume_count"] = (previous.get("unclean_resume_count", 0) if previous else 0) + int(unclean_resume)
        mapping, catalog = {}, {}
        token = uuid4().hex
        server = uvicorn.Server(uvicorn.Config(create_app(engine, api_key=token), log_level="error", access_log=False))
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        port = listener.getsockname()[1]
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        active_action = {}
        def save(status):
            records = [step for _, step in sorted(steps.items())]
            report["add_calls"] = [step["record"] for step in records if step["kind"] == "add"]
            report["questions"] = [step["record"] for step in records if step["kind"] == "search"]
            report["source_to_ref"] = mapping
            report["diagnostic_calls"], provider_gaps = read_journal(provider_journal.path)
            report["http_attempts"], http_gaps = read_journal(http_journal.path)
            report["physical_call_statistics_complete"] = not provider_gaps and report["unclean_resume_count"] == 0
            report["journal_malformed_lines"] = {"provider": provider_gaps, "http": http_gaps}
            report["summary"] = report_summary(report, set(metadata["known_regression_ids"]))
            atomic_json(result_path, report)
            atomic_json(args.output / "run.json", {"status": status, "pid": os.getpid(), "local_api": f"http://127.0.0.1:{port}",
                "runtime_directory": str(root), "started_at": state["started_at"], "updated_at": time.time(),
                "add_completed": len(report["add_calls"]), "questions_completed": len(report["questions"]),
                "processing_units_completed": report["summary"]["processing_units_completed"],
                "planned_add_calls": preview["add_calls"], "planned_questions": preview["questions"],
                "current_action": active_action, "result": str(result_path), "resume_command": resume_command(args, root)})
        try:
            save("starting")
            thread.start()
            deadline = time.monotonic() + 10
            while not server.started and thread.is_alive() and time.monotonic() < deadline:
                time.sleep(.05)
            if not server.started:
                raise RuntimeError("local_service_start_failed")
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", headers={"Authorization": "Bearer " + token}) as client:
                client.get("/ready", timeout=15).raise_for_status()
                for index, (kind, action) in enumerate(plan):
                    _, _, identity = step_identity(index, kind, action)
                    active_action = {"index": index, "kind": kind, "identity": identity, "started_at": time.time()}
                    provider_journal.set_context(action_index=index, action_kind=kind, action_id=identity)
                    http_journal.set_context(action_index=index, action_kind=kind, action_id=identity)
                    if kind == "add":
                        payload = add_payload(action, owner)
                        refs, originals = input_catalog(payload, action["turns"], action.get("message_refs"))
                        mapping.update(refs)
                        catalog.update(originals)
                        completed = request_complete(engine.store, owner, action["request_id"])
                        if index in steps:
                            if not completed:
                                raise ValueError("add_file_checkpoint_requires_complete_database_request")
                            continue
                        if completed:
                            steps[index] = commit_step(args.output, index, kind, action, recovered_add_record(action, completed))
                            save("running")
                            continue
                        save("running")
                        response, seconds, attempts = post_with_retries(client, "/add", payload, http_journal,
                            attempts=args.max_http_attempts, retry_seconds=args.retry_seconds,
                            timeout=engine.settings.add_timeout + 30)
                        completed = request_complete(engine.store, owner, action["request_id"])
                        if response.status_code != 200 or response.json().get("success") is not True or not completed:
                            raise RuntimeError("david_add_paused_with_checkpoint")
                        record = {"request_id": action["request_id"], "turn_refs": [turn["ref"] for turn in action["turns"]],
                            "units": len(action["turns"]), "seconds": seconds, "http_status": 200, "success": True,
                            "processing_complete": True, "http_attempts": attempts, "latency_basis": "observed_http_attempts"}
                    else:
                        if index in steps:
                            continue
                        save("running")
                        question = action["question"]
                        response, seconds, attempts = post_with_retries(client, "/search", {"user_id": owner,
                            "query": question["text"], "top_k": AML_TOP_K}, http_journal,
                            attempts=args.max_http_attempts, retry_seconds=args.retry_seconds,
                            timeout=engine.settings.search_timeout + 30)
                        if response.status_code != 200:
                            raise RuntimeError("david_search_paused_with_checkpoint")
                        candidates = response.json()["data"]
                        record = score_case(action, candidates, mapping, catalog)
                        record.update(seconds=seconds, http_status=200, http_attempts=attempts, candidates=candidates,
                            trace=engine.last_search, known_regression=question["id"] in metadata["known_regression_ids"],
                            input_complete=all(ref in mapping.values() for ref in question["evidence"]))
                    steps[index] = commit_step(args.output, index, kind, action, record)
                    save("running")
                    print(json.dumps({"action": index + 1, "kind": kind, "identity": identity,
                        "seconds": record["seconds"], "add_completed": len(report["add_calls"]),
                        "questions_completed": len(report["questions"]), "recall": record.get("evidence_recall")}), flush=True)
                report["derivation"] = {"rhelm": {"memories": [memory.model_dump() for memory in engine.store.memories(owner)],
                    "requests": engine.store.list("requests", owner)}}
                report["validation_summary"] = validation_summary(report["derivation"]["rhelm"]["requests"])
                tables = {row[0] for row in engine.store.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                report["tables"] = sorted(tables)
                report["protocol_checks"] = [{"name": "exactly_six_tables", "passed": tables == TABLES},
                    {"name": "complete_causal_plan", "passed": len(steps) == len(plan)}]
                if tables != TABLES:
                    raise RuntimeError("approved_table_scope_mismatch")
                report["ended_at"] = time.time()
                # Derived reports can be rebuilt locally from durable steps.
                # Completion is published after all diagnostic artifacts commit.
                save("reporting")
                atomic_json(args.output / "analysis.json", {**analyze(report), "validation": report["validation_summary"]})
                if args.baseline:
                    atomic_json(args.output / "comparison.json", compare_baseline(report, dataset, args.baseline))
                report["complete"] = True
                atomic_json(args.output / "analysis.json", {**analyze(report), "validation": report["validation_summary"]})
                active_action = {}
                save("complete")
        except BaseException as error:
            report["errors"].append({"operation": "runner", "error_type": type(error).__name__,
                "reason": str(error)[:160] if isinstance(error, (RuntimeError, ValueError)) else "interrupted",
                "action": active_action, "recorded_at": time.time()})
            save("interrupted")
            raise
        finally:
            server.should_exit = True
            if thread.ident is not None:
                thread.join(timeout=15)
            listener.close()
            if not thread.is_alive():
                engine.close()
            else:
                report["physical_call_statistics_complete"] = False
                report["errors"].append({"operation": "runner_shutdown", "error_type": "InFlightProviderCall",
                    "reason": "The service is still settling an in-flight call; preserved journal counts are a lower bound."})
                atomic_json(result_path, report)
        print(json.dumps({"result": str(result_path), "complete": report["complete"],
            "metrics": report["summary"]["retrieval"][str(AML_TOP_K)]}), flush=True)
        return report


def plan_preview(dataset, metadata, plan):
    add_actions = [action for kind, action in plan if kind == "add"]
    covered_refs = {turn["ref"] for action in add_actions for turn in action["turns"]}
    return {"character": dataset["character"], "add_calls": len(add_actions),
        "questions": len(dataset["cases"]), "turns": len(dataset["input_turns"]),
        "processing_units": len(covered_refs),
        "max_add_messages": max((action.get("message_count", len(action["turns"]) * 2)
                                 for action in add_actions), default=0),
        "max_add_words": max((action.get("word_count", 0) for action in add_actions), default=0),
        "add_chunk_contract": {"message_limit": AML_MESSAGE_LIMIT, "word_limit": AML_WORD_LIMIT,
                               "word_counter": "unicode_word_units_v1"},
        "known_regression_questions": len(metadata["known_regression_ids"]),
        "remaining_same_person_questions": len(dataset["cases"]) - len(metadata["known_regression_ids"]),
        "dataset_sha256": metadata["dataset_sha256"], "source_manifest_sha256": metadata["source_manifest_sha256"],
        "rhelm_source_sha256": dataset.get("rhelm_source_sha256"),
        "excluded": metadata["excluded"], "paid_calls": 0}


def resume_command(args, root):
    import shlex
    parts = ["env", "PYTHONPATH=" + str(root / "src") + ":" + str(root), sys.executable, "-m", "scripts.run_david", "--data-dir", str(args.data_dir),
        "--character", args.character, "--golden-dataset", str(args.golden_dataset), "--output", str(args.output),
        "--env-file", str(args.env_file), "--max-http-attempts", str(args.max_http_attempts),
        "--retry-seconds", str(args.retry_seconds), "--execute", "--allow-create-schema", "--resume"]
    if args.baseline:
        parts.extend(["--baseline", str(args.baseline)])
    return " ".join(shlex.quote(part) for part in parts)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--character", default="David_R._Ellis")
    parser.add_argument("--golden-dataset", type=Path, required=True)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--allow-create-schema", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--max-http-attempts", type=int, default=3)
    parser.add_argument("--retry-seconds", type=float, default=2)
    args = parser.parse_args()
    if not 1 <= args.max_http_attempts <= 3 or not 0 <= args.retry_seconds <= 10:
        parser.error("retry budget requires 1..3 attempts and 0..10 seconds")
    if args.execute:
        if args.output is None or args.env_file is None:
            parser.error("execution requires --output and --env-file")
        try:
            execute(args)
        except (RuntimeError, ValueError, OSError, httpx.HTTPError) as error:
            print(json.dumps({"status": "interrupted", "error_type": type(error).__name__,
                              "message": "Inspect run.json and retained checkpoints; use the recorded resume command."}), flush=True)
            raise SystemExit(2) from None
    else:
        dataset, metadata = load_dataset(args.data_dir, args.character, args.golden_dataset)
        print(json.dumps(plan_preview(dataset, metadata, list(actions(dataset)))), flush=True)


if __name__ == "__main__":
    main()
