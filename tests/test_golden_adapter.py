import json
from pathlib import Path

from scripts.golden_adapter import actions, add_payload, input_catalog, metrics, score_case
from scripts.run_golden import summaries


def test_scoring_counts_only_verified_visible_original_blocks():
    payload = {"user_id": "u", "request_id": "r", "messages": [
        {"role": "user", "content": "I used a Dell laptop."}, {"role": "assistant", "content": "It was useful."}]}
    mapping, catalog = input_catalog(payload, [{"ref": "2024-06-02:1"}])
    case = {"question": {"id": "q", "kind": "fact", "day": "2024-06-03", "text": "Device?", "answer": "Dell",
                         "evidence": ["2024-06-02:1"]}, "facets": [{"ref": "2024-06-02:1", "role": "user", "quote": "Dell laptop"}]}
    key = next(iter(mapping))
    only_summary = [{"content": f"Current conditions: [{key}] I used a Dell laptop."}]
    assert score_case(case, only_summary, mapping, catalog)["evidence_recall"] == 0
    source = [{"content": f"Original evidence:\n[{key}] user at 2024-06-02:\nI used a Dell laptop.\nMemory: not scored"}]
    assert score_case(case, source, mapping, catalog)["all_evidence_hit"]
    forged = [{"content": f"Original evidence:\n[{key}] user at 2024-06-02:\nI used a MacBook."}]
    assert score_case(case, forged, mapping, catalog)["evidence_recall"] == 0


def test_two_framework_formats_have_identical_scores():
    sid = "a" * 24
    case = {"question": {"id": "q", "kind": "fact", "day": "2024-06-03", "text": "Device?", "answer": "Dell",
                         "evidence": ["2024-06-02:1"]}}
    ember = [{"content": f"Original evidence:\n[src_{sid}] user at 2024-06-02:\nDell laptop"}]
    trace = [{"content": f"[source:{sid}]\n原始消息（user） time=2024-06-02\nDell laptop"}]
    a = score_case(case, ember, {"src_" + sid: "2024-06-02:1"}, {"src_" + sid: {"role": "user", "content": "Dell laptop"}})
    b = score_case(case, trace, {"source:" + sid: "2024-06-02:1"}, {"source:" + sid: {"role": "user", "content": "Dell laptop"}}, "traceweave")
    assert a["evidence_ranks"] == b["evidence_ranks"] and metrics([a]) == metrics([b])


def test_action_order_matches_the_frozen_reference_builder():
    dataset = {"character": "person", "input_turns": [
        {"ref": "2024-06-02:1", "day": "2024-06-02", "timestamp_ms": 1, "user": "hello", "assistant": "reply"},
        {"ref": "2024-06-03:1", "day": "2024-06-03", "timestamp_ms": 2, "user": "later", "assistant": "reply"}],
        "cases": [{"question": {"day": "2024-06-02"}}, {"question": {"day": "2024-06-03"}}]}
    plan = list(actions(dataset))
    assert [kind for kind, _ in plan] == ["add", "search", "add", "search"]
    assert add_payload(plan[0][1], "u")["messages"] == [
        {"role": "user", "content": "hello", "timestamp": 1}, {"role": "assistant", "content": "reply", "timestamp": 1}]


def test_report_aggregation_accepts_non_http_failures_and_unknown_status():
    report = {"questions": [], "add_calls": [], "lifecycle_add_calls": [], "diagnostic_calls": [
        {"kind": "add", "error_type": "OutputInvalid", "http_status": None},
        {"kind": "rerank", "http_status": 402}, {"kind": "add"}]}
    result = summaries(report)
    assert sum(result["provider_errors"].values()) == 2
