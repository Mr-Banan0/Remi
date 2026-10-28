import json
import os
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from scripts import run_david as runner
from scripts.golden_adapter import actions, add_payload


TRACE_DATA = Path(__file__).resolve().parents[2] / 'traceweave' / '.local-data'
LOCAL_DATA = Path(os.getenv('REMI_TEST_RHELM_DIR', str(TRACE_DATA / 'rhelm')))
LOCAL_GOLDEN = Path(os.getenv('REMI_TEST_GOLDEN_DATASET', str(TRACE_DATA / 'whole-message-runtime-20261009-v1/golden/dataset-v1.json')))
TRACE_BASELINE = Path(os.getenv('REMI_TEST_TRACE_BASELINE', str(TRACE_DATA / 'tool-search-history-20261009/david-full-add-stability-20261008/result.json')))


def sample_case(identifier="q1", day="2024-06-03"):
    return {"question": {"id": identifier, "day": day, "kind": "fact", "text": "Which device?",
        "answer": "Dell", "evidence": ["2024-06-02:1"]}}


def sample_plan():
    return [("add", {"request_id": "r1", "session_id": "s1", "turns": [{"ref": "2024-06-02:1",
        "day": "2024-06-02", "timestamp_ms": 1717286400000, "user": "I used a Dell laptop.", "assistant": "Understood."}]}),
        ("search", sample_case())]


def test_atomic_json_keeps_previous_checkpoint_if_replace_fails(tmp_path, monkeypatch):
    path = tmp_path / "result.json"
    runner.atomic_json(path, {"complete": False})
    def failed_replace(*args):
        raise OSError("synthetic_replace_failure")
    monkeypatch.setattr(runner.os, "replace", failed_replace)
    with pytest.raises(OSError):
        runner.atomic_json(path, {"complete": True})
    assert json.loads(path.read_text()) == {"complete": False}
    assert list(tmp_path.glob("*.tmp")) == []


def test_run_lock_blocks_duplicate_and_releases_after_exit(tmp_path):
    with runner.run_lock(tmp_path):
        with pytest.raises(RuntimeError, match="already_locked"):
            with runner.run_lock(tmp_path):
                pass
    with runner.run_lock(tmp_path):
        assert json.loads((tmp_path / ".run.lock").read_text())["pid"] > 0


def test_shared_provider_journal_persists_parallel_calls_and_excludes_secrets(tmp_path):
    journal = runner.DurableJournal(tmp_path / "diagnostics.jsonl")
    journal.set_context(action_index=3, action_kind="search", action_id="q1")
    buffers = [runner.ProviderCalls(journal) for _ in range(3)]
    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(lambda index: buffers[index % 3].append({"kind": "rerank", "seconds": .01,
            "http_status": None, "model": "jev", "api_key": "secret-test-value", "prompt": "private text"}), range(40)))
    records, malformed = runner.read_journal(journal.path)
    assert len(records) == 40 and malformed == []
    assert len({record["journal_id"] for record in records}) == 40
    assert all(record["action_id"] == "q1" and record["http_status"] is None for record in records)
    assert "secret-test-value" not in journal.path.read_text() and "private text" not in journal.path.read_text()


def test_torn_journal_line_is_preserved_and_future_lines_remain_parseable(tmp_path):
    path = tmp_path / "diagnostics.jsonl"
    path.write_text('{"kind":"add"')
    runner.DurableJournal(path).append({"kind": "rerank", "seconds": .1}, provider=True)
    records, malformed = runner.read_journal(path)
    assert [record["kind"] for record in records] == ["rerank"] and malformed == [1]
    assert path.read_text().startswith('{"kind":"add"\n')


def test_durable_step_is_available_independently_of_report_update(tmp_path):
    plan = sample_plan()
    add = runner.commit_step(tmp_path, 0, *plan[0], {"request_id": "r1", "success": True})
    search = runner.commit_step(tmp_path, 1, *plan[1], {"question_id": "q1", "candidates": [{"content": "Dell"}]})
    restored = runner.load_steps(tmp_path, plan)
    assert restored == {0: add, 1: search}
    assert restored[1]["record"]["candidates"][0]["content"] == "Dell"


def test_step_recovery_requires_contiguous_causal_prefix(tmp_path):
    plan = sample_plan()
    runner.commit_step(tmp_path, 1, *plan[1], {"question_id": "q1"})
    with pytest.raises(ValueError, match="causal_prefix"):
        runner.load_steps(tmp_path, plan)


def test_step_recovery_validates_identity(tmp_path):
    plan = sample_plan()
    runner.commit_step(tmp_path, 0, *plan[0], {"request_id": "r1"})
    changed = [("add", {**plan[0][1], "request_id": "another"}), plan[1]]
    with pytest.raises(ValueError, match="identity_or_status"):
        runner.load_steps(tmp_path, changed)


def test_complete_add_checkpoint_recovers_without_model_generation():
    request = {"status": "complete", "created_at": "2024-06-02T00:00:00+00:00", "completed_at": "2024-06-02T00:00:04+00:00"}
    class Store:
        def get(self, table, owner, request_id):
            assert (table, owner, request_id) == ("requests", "owner", "r1")
            return request
    checkpoint = runner.request_complete(Store(), "owner", "r1")
    record = runner.recovered_add_record(sample_plan()[0][1], checkpoint)
    assert record["seconds"] == 4 and record["latency_basis"] == "recovered_request_checkpoint_wall_clock"


def test_bounded_retries_preserve_exact_add_payload(tmp_path):
    requests = []
    outcomes = [httpx.ConnectError("temporary"), 503, 200]
    payload = add_payload(sample_plan()[0][1], "owner")
    def handler(request):
        requests.append(json.loads(request.content))
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return httpx.Response(outcome, json={"success": outcome == 200})
    journal = runner.DurableJournal(tmp_path / "http.jsonl")
    with httpx.Client(base_url="http://test", transport=httpx.MockTransport(handler)) as client:
        response, seconds, attempts = runner.post_with_retries(client, "/add", payload, journal, retry_seconds=0)
    assert response.status_code == 200 and attempts == 3 and seconds >= 0
    assert requests == [payload, payload, payload]
    records, gaps = runner.read_journal(journal.path)
    assert not gaps and [record["http_status"] for record in records] == [None, 503, 200]


def test_auth_failure_stops_without_service_retry(tmp_path):
    journal = runner.DurableJournal(tmp_path / "http.jsonl")
    with httpx.Client(base_url="http://test", transport=httpx.MockTransport(lambda request: httpx.Response(401))) as client:
        response, _, attempts = runner.post_with_retries(client, "/search", {}, journal, retry_seconds=0)
    assert response.status_code == 401 and attempts == 1


def test_service_retry_exhaustion_is_bounded(tmp_path):
    journal = runner.DurableJournal(tmp_path / "http.jsonl")
    with httpx.Client(base_url="http://test", transport=httpx.MockTransport(lambda request: httpx.Response(503))) as client:
        response, _, attempts = runner.post_with_retries(client, "/add", {}, journal, attempts=2, retry_seconds=0)
    assert response.status_code == 503 and attempts == 2
    assert len(runner.read_journal(journal.path)[0]) == 2


def test_transport_retry_exhaustion_retains_all_attempt_diagnostics(tmp_path):
    journal = runner.DurableJournal(tmp_path / "http.jsonl")
    def handler(request):
        raise httpx.ReadTimeout("temporary")
    with httpx.Client(base_url="http://test", transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RuntimeError, match="retry_exhausted"):
            runner.post_with_retries(client, "/search", {}, journal, attempts=2, retry_seconds=0)
    assert len(runner.read_journal(journal.path)[0]) == 2


def test_resume_contract_rejects_config_or_source_changes():
    contract = {"service_code_fingerprint": "a", "configuration": {"model": "gpt-4o-mini"}, "data": {"sha256": "x"}}
    runner.validate_resume_contract(contract, dict(contract))
    for key, value in (("service_code_fingerprint", "b"), ("configuration", {"model": "another"}), ("data", {"sha256": "y"})):
        with pytest.raises(ValueError, match="resume_contract_mismatch"):
            runner.validate_resume_contract(contract, {**contract, key: value})


def test_public_config_ignores_keys_and_rejects_urls_containing_credentials(monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "https://gateway.example/v1")
    monkeypatch.setenv("LLM_API_KEY", "secret-one")
    first = runner.public_configuration()
    monkeypatch.setenv("LLM_API_KEY", "secret-two")
    assert runner.public_configuration() == first
    assert "secret-one" not in json.dumps(first) and "secret-two" not in json.dumps(first)
    for url in ("https://u:pass@gateway.example/v1", "https://gateway.example/v1?key=secret"):
        with pytest.raises(ValueError, match="plain_http_url"):
            runner.public_url(url)


def test_code_fingerprint_changes_for_service_and_runner_separately(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "scripts").mkdir()
    service = tmp_path / "src" / "engine.py"
    service.write_text("version = 1\n")
    for name in ("run_david.py", "golden_adapter.py", "run_golden.py", "analyze_golden.py"):
        (tmp_path / "scripts" / name).write_text("version = 1\n")
    first = runner.code_fingerprints(tmp_path)
    service.write_text("version = 2\n")
    second = runner.code_fingerprints(tmp_path)
    assert first["service_code_fingerprint"] != second["service_code_fingerprint"]
    assert first["runner_code_fingerprint"] == second["runner_code_fingerprint"]


def test_validation_diagnostics_separate_initial_and_repair_causes():
    requests = [{"units": {"0": {"validation": [
        {"stage": "initial", "kind": "fact", "operation": "create", "error": "source_quote"},
        {"stage": "repair", "kind": "event", "operation": "update", "error": "time_unknown"}]}},
        "1": {"commands": []}}]
    summary = runner.validation_summary(requests)
    assert summary["records"] == 2 and summary["by_stage"] == {"initial": 1, "repair": 1}
    assert summary["stage_error_kind"]["initial|source_quote|fact"] == 1


def test_validation_acceptance_has_a_separate_count_from_errors():
    requests = [{"units": {"0": {"validation": [
        {"stage": "initial", "kind": "fact", "operation": "create", "error": None},
        {"stage": "repair", "kind": "event", "operation": "create", "error": "time_unknown"}]}}}]
    summary = runner.validation_summary(requests)
    assert summary["by_error"] == {"time_unknown": 1}
    assert summary["validated_records"] == summary["error_records"] == 1
    assert summary["stage_error_kind"]["initial|accepted|fact"] == 1


def test_complete_resume_finishes_before_provider_creation(tmp_path, monkeypatch):
    dataset = {"character": "David_R._Ellis", "input_turns": [],
               "cases": [sample_case(f"q{i}") for i in range(86)]}
    metadata = {"known_regression_ids": [f"q{i}" for i in range(13)], "source_files": [], "excluded": {},
        "dataset_sha256": "data", "source_manifest_sha256": "sources", "golden_sha256": "known"}
    plan = [("add", {"request_id": f"r{i}", "turns": []}) for i in range(215)] + [("search", case) for case in dataset["cases"]]
    monkeypatch.setattr(runner, "load_dataset", lambda *args: (dataset, metadata))
    monkeypatch.setattr(runner, "actions", lambda dataset: iter(plan))
    monkeypatch.setattr(runner, "load_environment", lambda *args: None)
    monkeypatch.setattr(runner, "public_configuration", lambda: {"model": "test"})
    monkeypatch.setattr(runner, "code_fingerprints", lambda root: {"service_code_fingerprint": "src", "runner_code_fingerprint": "runner"})
    def forbidden():
        raise AssertionError("completed resume created providers")
    monkeypatch.setattr(runner, "environment_engine", forbidden)
    args = SimpleNamespace(data_dir=tmp_path, character="David_R._Ellis", golden_dataset=tmp_path / "golden.json",
        output=tmp_path, env_file=tmp_path / ".env", allow_create_schema=True, resume=True,
        max_http_attempts=3, retry_seconds=0, baseline=None)
    contract = runner.make_contract(tmp_path, metadata, {"model": "test"}, plan, 3)
    contract["retry_seconds"] = 0
    runner.atomic_json(tmp_path / "state.json", {"owner": "test", "started_at": 0, "contract": contract})
    for index, (kind, action) in enumerate(plan):
        runner.commit_step(tmp_path, index, kind, action, {})
    runner.atomic_json(tmp_path / "result.json", {"complete": True})
    assert runner.execute(args) == {"complete": True}


@pytest.mark.skipif(not LOCAL_DATA.exists() or not LOCAL_GOLDEN.is_file(), reason="local full David input unavailable")
def test_complete_david_plan_matches_aml_full_chunking_and_question_scope():
    dataset, metadata = runner.load_dataset(LOCAL_DATA, "David_R._Ellis", LOCAL_GOLDEN)
    plan = list(actions(dataset))
    preview = runner.plan_preview(dataset, metadata, plan)
    assert preview["add_calls"] == 278 and preview["questions"] == 86
    assert preview["turns"] == preview["processing_units"] == 1486
    assert preview["max_add_messages"] <= 20 and preview["max_add_words"] <= 2000
    assert preview["known_regression_questions"] == 13 and preview["remaining_same_person_questions"] == 73
    assert preview["excluded"] == {"external_or_mixed_question": 72}
    assert preview["paid_calls"] == 0
    for kind, action in plan:
        if kind == "add":
            payload = add_payload(action, "test")
            assert set(payload) == {"request_id", "session_id", "user_id", "messages"}
            assert all(set(message) == {"role", "content", "timestamp"} for message in payload["messages"])


@pytest.mark.skipif(not LOCAL_DATA.exists() or not TRACE_BASELINE.is_file(), reason="historical baseline unavailable")
def test_historical_david_baseline_with_old_adapter_is_rejected():
    dataset, _ = runner.load_dataset(LOCAL_DATA, "David_R._Ellis", LOCAL_GOLDEN)
    with pytest.raises(ValueError, match="historical_baseline_aml_full_contract_mismatch"):
        runner.compare_baseline({"questions": []}, dataset, TRACE_BASELINE)


@pytest.mark.skipif(os.getenv("REMI_TEST_SCHEMA_APPROVED") != "1", reason="explicit six-table offline approval flag required")
def test_http_runner_resumes_after_committed_search_and_reuses_add_checkpoint(tmp_path, monkeypatch):
    """Exercise real local HTTP with deterministic offline providers and six tables."""
    from remi.engine import MemoryEngine
    from remi.providers import ProviderUnavailable, RankResult
    from remi.settings import Settings
    from remi.storage import SQLiteStore
    units = [{"ref": f"2024-06-02:{index + 1}", "day": "2024-06-02", "timestamp_ms": 1717286400000,
              "user": f"I used Dell laptop number {index}.", "assistant": "Understood."} for index in range(215)]
    dataset = {"character": "David_R._Ellis", "input_turns": units, "cases": [sample_case(f"q{i}") for i in range(86)]}
    metadata = {"known_regression_ids": [f"q{i}" for i in range(13)], "source_files": [], "excluded": {},
        "dataset_sha256": "synthetic-data", "source_manifest_sha256": "synthetic-sources", "golden_sha256": "known"}
    plan = [("add", {"request_id": f"r{i}", "session_id": "synthetic", "turns": [turn]}) for i, turn in enumerate(units)]
    plan.extend(("search", case) for case in dataset["cases"])
    counters = Counter()
    failed_index = [False]
    class Model:
        def __init__(self):
            self.calls = deque()
        def maintain_subjects(self, state, *, deadline):
            return {'subjects': []}
        def extract_memories(self, state, *, deadline):
            counters["extract"] += 1
            self.calls.append({"kind": "add", "model": "offline", "seconds": 0})
            return {"items": [], "reason": "no memory"}
        def propose(self, state, *, deadline):
            return {"items": [], "reason": "no memory"}
        def decide(self, state, *, deadline):
            counters["decide"] += 1
            self.calls.append({"kind": "search_decision", "model": "offline", "seconds": 0})
            return {"query_mode": "direct", "action_time_expression": None,
                    "searches": [], "relevant_memory_ids": []}
        def close(self):
            pass
    class Embedder:
        def __init__(self):
            self.calls = deque()
        def embed(self, texts, *, deadline):
            counters["embed"] += 1
            self.calls.append({"kind": "embedding", "model": "offline", "seconds": 0})
            if any("assistant" in text for text in texts) and not failed_index[0]:
                failed_index[0] = True
                raise ProviderUnavailable("synthetic_index_pause")
            return [[1., 0.] for _ in texts]
        def close(self):
            pass
    class Reranker:
        def __init__(self):
            self.calls = deque()
        def rank(self, query, cards, *, deadline):
            counters["rerank"] += 1
            self.calls.append({"kind": "rerank", "model": "offline", "seconds": 0})
            return RankResult(scores={card["id"]: .5 for card in cards})
        def close(self):
            pass
    def offline_engine():
        counters["engine_created"] += 1
        model = Model()
        return MemoryEngine(SQLiteStore(os.environ["REMI_DB_PATH"]), model, Embedder(), Reranker(),
                            Settings(graph_enabled=False, hot_enabled=False))
    monkeypatch.setattr(runner, "__file__", str(Path(runner.__file__).parent.parent / "main" / "scripts" / "run_david.py"))
    runtime = Path(runner.__file__).parents[1]
    import remi
    monkeypatch.setattr(remi, "__file__", str(runtime / "src" / "remi" / "__init__.py"))
    monkeypatch.setattr(runner, "load_dataset", lambda *args: (dataset, metadata))
    monkeypatch.setattr(runner, "actions", lambda dataset: iter(plan))
    monkeypatch.setattr(runner, "load_environment", lambda *args: None)
    monkeypatch.setattr(runner, "public_configuration", lambda: {"model": "offline"})
    monkeypatch.setattr(runner, "code_fingerprints", lambda root: {"service_code_fingerprint": "synthetic-src", "runner_code_fingerprint": "synthetic-runner"})
    monkeypatch.setattr(runner, "environment_engine", offline_engine)
    original_atomic = runner.atomic_json
    crash = [True]
    def interrupted_report(path, value):
        if Path(path).name == "result.json" and len(value.get("questions", [])) == 1 and crash[0]:
            crash[0] = False
            raise KeyboardInterrupt()
        original_atomic(path, value)
    monkeypatch.setattr(runner, "atomic_json", interrupted_report)
    args = SimpleNamespace(data_dir=tmp_path, character="David_R._Ellis", golden_dataset=tmp_path / "known.json",
        output=tmp_path / "run", env_file=tmp_path / ".env", allow_create_schema=True, resume=False,
        max_http_attempts=3, retry_seconds=0, baseline=None)
    with pytest.raises(KeyboardInterrupt):
        runner.execute(args)
    assert counters["extract"] == 215 and counters["decide"] == 1
    assert len(runner.load_steps(args.output, plan)) == 216
    args.resume = True
    report = runner.execute(args)
    assert report["complete"] and len(report["questions"]) == 86 and len(report["add_calls"]) == 215
    assert counters["extract"] == 215 and counters["decide"] == 86 and counters["rerank"] == 86
    assert counters["engine_created"] == 2
    assert report["summary"]["groups"]["known_regression"]["10"]["question_count"] == 13
    assert report["summary"]["groups"]["remaining_same_person"]["10"]["question_count"] == 73
    assert report["tables"] == sorted(runner.TABLES)
    runner.execute(args)
    assert counters["engine_created"] == 2
