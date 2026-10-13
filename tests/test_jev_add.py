"""Current Add contract, using deterministic providers and zero paid calls."""
from __future__ import annotations

import json
from time import monotonic

import httpx
import pytest

from embergraph.engine import MemoryEngine
from embergraph.jev_choices import JevChoices, choice_answer
from embergraph.models import ContentItem, SplitPlan
from embergraph.providers import OutputInvalid, ProviderUnavailable, strict_schema
from .helpers import HashEmbedder, answer, query, request


def written(key, content, **changes):
    return {"item_id": key, "content": content, "objects": [], "time_expression": None,
            "end_expression": None, "status": "stated", "impact": "unknown", **changes}


class Text:
    def __init__(self, split=None, write=None):
        self.splitter = split or (lambda state: {"items": [{"content": part.strip()}
                                  for part in state["new_user_source"]["content"].split(";")]})
        self.writer = write or (lambda state: {"items": [written(edit["item_id"], edit["new_item"]["content"])
                                                        for edit in state["edits"]]})
        self.splits, self.writes, self.calls = [], [], []

    def split(self, state, *, deadline):
        self.splits.append(state)
        return self.splitter(state)

    def materialize(self, state, *, deadline):
        self.writes.append(state)
        return self.writer(state)

    def decide(self, state, *, deadline):
        return {"steps": [], "relevant_memory_ids": []}


class Judge:
    def __init__(self, kind="fact", selector=None, route="single"):
        self.kind, self.selector, self.route_kind = kind, selector, route
        self.routes, self.classifications, self.selections, self.calls = [], [], [], []

    def route(self, state, *, deadline):
        self.routes.append(state)
        wrapped = {"new_item": {"content": state["new_user_source"]["content"]}}
        kind = self.kind(wrapped) if callable(self.kind) else self.kind
        route = self.route_kind(state) if callable(self.route_kind) else self.route_kind
        return {"route": answer(route), "category": answer(kind),
                **{f"i0_context_{index}": {"noul": float("我选第二个" in state["new_user_source"]["content"] and source["role"] == "assistant")}
                   for index, source in enumerate(state["context_sources"])}}

    def classify(self, states, *, deadline):
        self.classifications.append(states)
        result = {"split_coverage": {"noul": 1.}}
        result.update({key: value for state in states for key, value in (
            (state["item_id"], answer(self.kind(state) if callable(self.kind) else self.kind)),
            (state["item_id"]+"_grounding", {"noul": 1.}))})
        return result

    def select(self, states, *, deadline):
        self.selections.append(states)
        result = {}
        for state in states:
            key = state["item_id"]
            handle, operation = self.selector(state) if self.selector else ("new", "create")
            result[key+"_target"] = answer(handle)
            result[key+"_new"] = answer(operation)
            for candidate in state["candidates"]:
                result[key+"_"+candidate["handle"]] = answer(operation)
        return result


def test_single_target_skips_split_and_uses_speculative_route_category():
    text, judge = Text(), Judge()
    engine = MemoryEngine(model=text, judger=judge)
    original = "会议改到明天，而且改在三楼；这是同一次会议。"
    engine.add(request(original))
    assert not text.splits and not judge.classifications
    assert len(judge.routes) == len(judge.selections) == len(text.writes) == 1
    assert judge.selections[0][0]["new_item"] == {"content": original}
    assert engine.store.memories("u1")[0].current.source_ids == [engine.store.list("sources", "u1")[0]["id"]]


def test_classification_reads_new_source_and_context_without_old_candidates():
    text, judge = Text(), Judge()
    engine = MemoryEngine(model=text, judger=judge)
    engine.add(request("Build access is restricted.", request_id="a"))
    engine.add(request("Build access is restored.", request_id="b"))
    assert all(state["candidates"] == [] for state in judge.routes)
    assert judge.selections[-1][0]["new_user_source"]["id"] in {source["id"] for source in engine.store.list("sources", "u1")}


def test_same_kind_memory_recall_and_bound_original_sources():
    judge = Judge()
    engine = MemoryEngine(model=Text(), judger=judge, embedder=HashEmbedder())
    engine.add(request("Build access is restricted.", request_id="a"))
    judge.kind = "preference"
    engine.add(request("I prefer PDF invoices.", request_id="b"))
    judge.kind = "fact"
    engine.add(request("Build access is restored.", request_id="c"))
    candidates = judge.selections[-1][0]["candidates"]
    assert candidates and {candidate["kind"] for candidate in candidates} == {"fact"}
    assert all(candidate["sources"] and all(source["content"] for source in candidate["sources"]) for candidate in candidates)
    assert len(candidates) <= 4


def test_multiple_targets_split_content_only_and_inherit_original_clocks():
    text = Text(lambda state: {"items": [{"content": "The PX-9 test finished."}, {"content": "Build access remains restricted."}]})
    judge = Judge(lambda state: "event" if "PX-9" in state["new_item"]["content"] else "fact", route="multiple")
    engine = MemoryEngine(model=text, judger=judge)
    engine.add(request("The PX-9 test finished. Build access remains restricted."))
    assert {memory.kind for memory in engine.store.memories("u1")} == {"event", "fact"}
    assert len(text.splits) == len(judge.classifications) == len(judge.selections) == len(text.writes) == 1
    assert all(set(state["new_item"]) == {"content"} for state in judge.classifications[0])
    originals = [state["new_user_source"] for state in judge.classifications[0]]
    assert originals[0] == originals[1]
    assert originals[0]["message_at"] != originals[0]["created_at"]
    assert {memory.current.source_ids[0] for memory in engine.store.memories("u1")} == {originals[0]["id"]}


@pytest.mark.parametrize("route", ["raw_only", "uncertain", "outside", None])
def test_route_control_outcomes_skip_generation_and_keep_original_searchable(route):
    text, judge = Text(), Judge(route=route)
    engine = MemoryEngine(model=text, judger=judge)
    assert engine.add(request("Original content survives.")).success
    assert not text.splits and not text.writes and not judge.selections
    assert not engine.store.memories("u1") and engine.search(query("survives")).data


def test_route_failure_is_recorded_and_raw_index_completes():
    class Unavailable(Judge):
        def route(self, state, *, deadline):
            raise ProviderUnavailable("synthetic")
    engine = MemoryEngine(model=Text(), judger=Unavailable())
    assert engine.add(request("An original statement.")).success
    assert engine.store.get("requests", "u1", "r1")["units"]["0"]["rejected"][0]["error"] == "route_unavailable"
    assert not engine.store.memories("u1")


def test_update_appends_history_and_writer_has_fixed_operation_and_target():
    text, judge = Text(), Judge()
    engine = MemoryEngine(model=text, judger=judge)
    engine.add(request("Build access is restricted.", request_id="a"))
    identity = engine.store.memories("u1")[0].id
    judge.selector = lambda state: ("c1", "update")
    engine.add(request("Build access is restored.", request_id="b"))
    memory = engine.store.memories("u1")[0]
    assert memory.id == identity and len(memory.revisions) == 2
    assert memory.revisions[0].content == "Build access is restricted."
    assert memory.current.content == "Build access is restored."
    assert len(text.writes) == 2 and text.writes[-1]["edits"][0]["target"]["id"] == identity
    assert text.writes[-1]["edits"][0]["operation"] == "update"


def test_writer_cannot_override_operation_target_category_or_source():
    def malicious(state):
        return {"items": [written(edit["item_id"], edit["new_item"]["content"], **(
            {"target_id": "invented", "operation": "create", "source_id": "foreign"}
            if edit["operation"] == "update" else {})) for edit in state["edits"]]}
    text, judge = Text(write=malicious), Judge()
    engine = MemoryEngine(model=text, judger=judge)
    engine.add(request("Build access is restricted.", request_id="a"))
    judge.selector = lambda state: ("c1", "update")
    assert engine.add(request("Build access is restored.", request_id="b")).success
    assert len(text.writes) == 3 and len(engine.store.memories("u1")[0].revisions) == 1
    assert engine.search(query("restored")).data


@pytest.mark.parametrize("handle,operation", [("not_in_candidates", "update"), ("c1", "different")])
def test_unresolved_target_or_operation_preserves_old_memory(handle, operation):
    judge = Judge()
    engine = MemoryEngine(model=Text(), judger=judge)
    engine.add(request("Build access is restricted.", request_id="a"))
    judge.selector = lambda state: (handle, operation)
    engine.add(request("A separate account is restored.", request_id="b"))
    assert len(engine.store.memories("u1")[0].revisions) == 1


def test_retract_skips_writer_and_preserves_revisions():
    text, judge = Text(), Judge()
    engine = MemoryEngine(model=text, judger=judge)
    engine.add(request("I prefer PDF invoices.", request_id="a"))
    judge.selector = lambda state: ("c1", "retract")
    engine.add(request("I withdraw my PDF preference.", request_id="b"))
    assert len(text.writes) == 1 and len(engine.store.memories("u1")[0].revisions) == 2
    assert engine.store.memories("u1")[0].current.status == "retracted"


def test_long_retraction_uses_existing_content_and_new_original_evidence():
    text, judge = Text(), Judge()
    engine = MemoryEngine(model=text, judger=judge)
    engine.add(request("I prefer PDF invoices.", request_id="a"))
    judge.selector = lambda state: ("c1", "retract")
    original = "I withdraw my PDF preference. " + "Context detail. " * 400
    engine.add(request(original, request_id="b"))
    current = engine.store.memories("u1")[0].current
    assert current.status == "retracted" and current.content == "I prefer PDF invoices."
    assert current.quote == original and len(text.writes) == 1


def test_embedding_retry_reuses_route_split_classify_and_selection():
    class Failing(HashEmbedder):
        def embed(self, texts, *, deadline):
            if any("user:" in text for text in texts):
                raise ProviderUnavailable("synthetic_index_pause")
            return super().embed(texts, deadline=deadline)
    text, judge = Text(), Judge(route="multiple")
    engine = MemoryEngine(model=text, judger=judge, embedder=Failing())
    with pytest.raises(ProviderUnavailable):
        engine.add(request("Build access is restored.;I prefer PDF invoices."))
    counts = len(judge.routes), len(text.splits), len(judge.classifications), len(judge.selections), len(text.writes)
    engine.embedder = HashEmbedder()
    assert engine.add(request("Build access is restored.;I prefer PDF invoices.")).success
    assert (len(judge.routes), len(text.splits), len(judge.classifications), len(judge.selections), len(text.writes)) == counts
    assert len(engine.store.memories("u1")) == 2


def test_query_embedding_retry_reuses_route_and_preserves_single_audit_item():
    class Failing(HashEmbedder):
        def embed(self, texts, *, deadline):
            raise ProviderUnavailable("synthetic_recall_pause")
    text, judge = Text(), Judge()
    engine = MemoryEngine(model=text, judger=judge, embedder=HashEmbedder())
    engine.add(request("Build access is restricted.", request_id="a"))
    engine.embedder = Failing()
    with pytest.raises(ProviderUnavailable):
        engine.add(request("Build access is restored.", request_id="b"))
    count = len(judge.routes)
    engine.embedder = HashEmbedder()
    engine.add(request("Build access is restored.", request_id="b"))
    assert len(judge.routes) == count
    assert len(engine.store.get("requests", "u1", "b")["add_stages"]["0"]["audit"]["items"]) == 1


def test_several_updates_to_same_target_supply_earlier_assertions_to_writer():
    def write(state):
        return {"items": [written(edit["item_id"], " ".join(
            [previous["content"] for previous in edit["preceding_assertions"]] + [edit["new_item"]["content"]]))
            for edit in state["edits"]]}
    text, judge = Text(write=write), Judge()
    engine = MemoryEngine(model=text, judger=judge)
    engine.add(request("Build access is restricted.", request_id="a"))
    judge.selector, judge.route_kind = lambda state: ("c1", "update"), "multiple"
    engine.add(request("Build access is restored.;Build access now includes deployment.", request_id="b"))
    memory = engine.store.memories("u1")[0]
    assert len(memory.revisions) == 3
    assert memory.current.content == "Build access is restored. Build access now includes deployment."
    assert len(text.writes) == 2


def test_shared_repair_is_consumed_by_split_and_not_reused_by_writer():
    def split(state):
        return {"items": [{"content": "Build access is restored."},
                          {"content": "I prefer PDF invoices.", **({} if state.get("repair_errors") else {"source_id": "foreign"})}]}
    text = Text(split=split, write=lambda state: {})
    engine = MemoryEngine(model=text, judger=Judge(route="multiple"))
    assert engine.add(request("Build access is restored.;I prefer PDF invoices.")).success
    assert len(text.splits) == 2 and len(text.writes) == 1
    assert not engine.store.memories("u1")


def test_split_keeps_good_sibling_when_bad_content_stays_invalid():
    text = Text(split=lambda state: {"items": [{"content": "I prefer PDF invoices."}, {"content": " "}]})
    engine = MemoryEngine(model=text, judger=Judge(route="multiple"))
    engine.add(request("I prefer PDF invoices.;The inspection finished."))
    assert len(engine.store.memories("u1")) == 1
    assert len(text.splits) == 2


def test_split_grounding_rejects_invented_item_and_records_incomplete_coverage():
    class Partial(Judge):
        def classify(self, states, *, deadline):
            result = super().classify(states, deadline=deadline)
            result[states[-1]["item_id"]+"_grounding"] = {"noul": .1}
            result["split_coverage"] = {"noul": .2}
            return result
    text = Text(split=lambda state: {"items": [{"content": "I prefer PDF invoices."}, {"content": "I own an airship."}]})
    engine = MemoryEngine(model=text, judger=Partial(route="multiple"))
    engine.add(request("I prefer PDF invoices.;The inspection finished."))
    assert [memory.current.content for memory in engine.store.memories("u1")] == ["I prefer PDF invoices."]
    rejected = engine.store.get("requests", "u1", "r1")["units"]["0"]["rejected"]
    assert any(item["error"] == "split_coverage_unconfirmed" for item in rejected)


def test_per_item_dates_are_resolved_from_shared_original_with_no_time_copying():
    def write(state):
        return {"items": [written(edit["item_id"], edit["new_item"]["content"], status="occurred" if index == 0 else "planned",
            time_expression="昨天" if index == 0 else "明天") for index, edit in enumerate(state["edits"])]}
    text, judge = Text(write=write), Judge(kind="event", route="multiple")
    engine = MemoryEngine(model=text, judger=judge)
    engine.add(request("昨天检修三号水泵;明天检修五号水泵", when="2026-10-13T10:00:00Z"))
    memories = engine.store.memories("u1")
    assert {memory.current.event_at[:10] for memory in memories} == {"2026-10-12", "2026-10-14"}
    assert len({memory.current.source_ids[0] for memory in memories}) == 1
    assert {memory.current.observed_at[:10] for memory in memories} == {"2026-10-13"}


def test_cancel_action_is_one_memory_with_both_absolute_dates():
    text = Text(write=lambda state: {"items": [written("i0", "昨天取消了明天的茶会。", status="cancelled", time_expression="昨天")]})
    engine = MemoryEngine(model=text, judger=Judge(kind="event"))
    engine.add(request("昨天取消了明天的茶会。", when="2026-10-13T10:00:00Z"))
    assert not text.splits
    memory = engine.store.memories("u1")[0]
    assert memory.current.event_at.startswith("2026-10-12")
    assert "2026-10-14" in memory.current.content


def test_final_date_from_other_item_is_repaired_or_degraded_locally():
    def write(state):
        return {"items": [written(edit["item_id"], edit["new_item"]["content"], status="occurred",
            time_expression="明天" if edit["item_id"] == "i0" else "明天") for edit in state["edits"]]}
    engine = MemoryEngine(model=Text(write=write), judger=Judge(kind="event", route="multiple"))
    engine.add(request("昨天检修水泵;明天检查气阀", when="2026-10-13T10:00:00Z"))
    memories = engine.store.memories("u1")
    assert len(memories) == 2
    assert next(memory.current for memory in memories if "水泵" in memory.current.content).event_at is None
    assert next(memory.current for memory in memories if "气阀" in memory.current.content).event_at.startswith("2026-10-14")


def test_assistant_context_is_bound_by_program_for_explicit_user_confirmation():
    text = Text(write=lambda state: {"items": [written("i0", "用户选择周四的检修时段。", status="planned")]})
    judge = Judge(kind="event", route="raw_only")
    engine = MemoryEngine(model=text, judger=judge)
    engine.add(request("给我两个时段。", assistant="第一个周二，第二个周四。", request_id="a"))
    judge.route_kind = "single"
    engine.add(request("我选第二个。", request_id="b"))
    memory = engine.store.memories("u1")[0]
    assert memory.current.source_ids[0] == text.writes[-1]["edits"][0]["new_user_source"]["id"]
    assert any(source.role == "assistant" for source in engine.store.sources("u1", memory.current.source_ids))
    assert memory.current.quote == "我选第二个。"


def test_legacy_unplanned_stage_cache_requires_its_frozen_runtime():
    from embergraph.add_pipeline import AddPipeline
    with pytest.raises(ProviderUnavailable, match="add_checkpoint_version_mismatch"):
        AddPipeline(MemoryEngine(model=Text(), judger=Judge())).run({}, monotonic()+5,
            {"audit": {"pipeline": "G05-jev-choice"}})


def test_repaired_writer_result_survives_interruption_before_unit_plan_commit():
    from embergraph.add_pipeline import AddPipeline
    text = Text(write=lambda state: {"items": [written("i0", "Access is paused.")]} if state.get("repair_errors") else {})
    engine = MemoryEngine(model=text, judger=Judge())
    state = {"owner": "u1", "new_user_source": {"id": "s1", "content": "Access is paused.", "role": "user",
             "message_at": "2026-10-13T10:00:00Z", "created_at": "2026-10-13T11:00:00Z"},
             "context_sources": [], "candidates": []}
    cache, crash = {}, [True]
    def save():
        if "write_repair_result" in cache and crash[0]:
            crash[0] = False
            raise RuntimeError("synthetic checkpoint interruption")
    pipeline = AddPipeline(engine)
    with pytest.raises(RuntimeError):
        pipeline.run(state, monotonic()+5, cache, save)
    commands, errors, _ = pipeline.run(state, monotonic()+5, cache, save)
    assert len(commands) == 1 and not errors and len(text.writes) == 2
    assert len(engine.judger.routes) == len(engine.judger.selections) == 1


def test_repaired_split_result_survives_interruption_without_second_repair():
    from embergraph.add_pipeline import AddPipeline
    text = Text(split=lambda state: {"items": [{"content": "Access is paused."}, {"content": "Inspection is planned."}]}
                if state.get("repair_errors") else {})
    engine = MemoryEngine(model=text, judger=Judge(route="multiple"))
    state = {"owner": "u1", "new_user_source": {"id": "s1", "content": "Access is paused.;Inspection is planned.", "role": "user",
             "message_at": "2026-10-13T10:00:00Z", "created_at": "2026-10-13T11:00:00Z"},
             "context_sources": [], "candidates": []}
    cache, crash = {}, [True]
    def save():
        if "split_repair_result" in cache and crash[0]:
            crash[0] = False
            raise RuntimeError("synthetic checkpoint interruption")
    pipeline = AddPipeline(engine)
    with pytest.raises(RuntimeError):
        pipeline.run(state, monotonic()+5, cache, save)
    commands, errors, _ = pipeline.run(state, monotonic()+5, cache, save)
    assert len(commands) == 2 and not errors and len(text.splits) == 2 and len(text.writes) == 1


def test_expiry_date_failure_retains_supported_effective_start():
    text = Text(write=lambda state: {"items": [written("i0", "访问暂停从今天开始。", time_expression="今天",
        end_expression="next solstice")]})
    engine = MemoryEngine(model=text, judger=Judge())
    engine.add(request("访问暂停从今天开始，解除时间待定。", when="2026-10-13T10:00:00Z"))
    current = engine.store.memories("u1")[0].current
    assert current.valid_from.startswith("2026-10-13") and current.valid_to is None
    assert len(text.writes) == 2


def test_unneeded_context_cannot_ground_named_object_in_final_writer():
    text = Text(write=lambda state: {"items": [written("i0", edit["new_item"]["content"],
        objects=[{"kind": "person", "name": "Rina"}] if "My access" in edit["new_item"]["content"] else [])
        for edit in state["edits"]]})
    judge = Judge(route="raw_only")
    engine = MemoryEngine(model=text, judger=judge)
    engine.add(request("Rina is arranging a separate review.", request_id="a"))
    judge.route_kind = "single"
    engine.add(request("My access is restricted.", request_id="b"))
    assert not engine.store.memories("u1")
    assert all(not edit["context_sources"] for payload in text.writes for edit in payload["edits"])


@pytest.mark.parametrize("field,value", [("confidence", float("nan")), ("confidence", True),
                                       ("probabilities", {"a": .4}), ("choice", "outside")])
def test_choice_contract_rejects_malformed_answers(field, value):
    raw = {"type": "choice", "choice": "a", "confidence": 1., "probabilities": {"a": 1., "b": 0.}}
    raw[field] = value
    with pytest.raises(OutputInvalid):
        choice_answer(raw, {"a": "A", "b": "B"})


def native_answers(body):
    return {key: {"type": "noul", "noul": 1.} if question["type"] == "noul" else
            {"type": "choice", "choice": "single" if key == "route" else "fact", "confidence": 1.,
             "probabilities": {option: float(option == ("single" if key == "route" else "fact"))
                               for option in question["criteria"]}}
            for key, question in body["questions"].items()}


def test_route_gateway_uses_bounded_choice_and_conditional_category():
    payloads = []
    def handler(request):
        assert request.url.path == "/v1/systemone"
        body = json.loads(request.content)
        payloads.append(body)
        return httpx.Response(200, json={"answers": native_answers(body)})
    judge = JevChoices("dummy-test", "https://test.invalid/v1", transport=httpx.MockTransport(handler))
    result = judge.route({"new_user_source": {"content": "访问权限暂停。"}, "context_sources": [], "candidates": []}, deadline=monotonic()+5)
    assert result["route"]["choice"] == "single" and result["category"]["choice"] == "fact"
    assert set(payloads[0]["questions"]["route"]["criteria"]) == {"single", "multiple", "raw_only", "uncertain"}
    assert "exactly one" in payloads[0]["questions"]["category"]["instructions"]
    assert "candidates" not in payloads[0]["state"] and "dummy-test" not in repr(judge.calls)
    judge.close()


def test_classification_and_coverage_share_request_with_explicit_focused_questions():
    payloads = []
    def handler(request):
        body = json.loads(request.content)
        payloads.append(body)
        return httpx.Response(200, json={"answers": native_answers(body)})
    judge = JevChoices("dummy-test", "https://test.invalid/v1", transport=httpx.MockTransport(handler))
    result = judge.classify([{"item_id": "i1", "new_item": {"content": "Access is locked."},
        "new_user_source": {"role": "user", "content": "Access is locked.;Inspection finished."},
        "split_contents": ["Access is locked.", "Inspection finished."]}], deadline=monotonic()+5)
    assert result["i1"]["choice"] == "fact" and result["i1_grounding"]["noul"] == 1.
    assert result["split_coverage"]["noul"] == 1. and len(payloads) == 1
    assert "items[0]" in payloads[0]["questions"]["i1"]["instructions"]
    judge.close()


def test_partial_jev_batch_failure_preserves_completed_independent_answers():
    count = [0]
    def handler(request):
        count[0] += 1
        if count[0] == 2:
            return httpx.Response(500)
        return httpx.Response(200, json={"answers": native_answers(json.loads(request.content))})
    judge = JevChoices("dummy-test", "https://test.invalid/v1", transport=httpx.MockTransport(handler))
    result = judge.classify([{"item_id": f"i{index}", "new_item": {"content": "Access is locked."}}
                             for index in range(5)], deadline=monotonic()+5)
    assert "i0" in result and "i4" not in result and len(judge.calls) == 2
    judge.close()


def test_oversized_state_is_traced_and_remains_local_raw_fallback():
    judge = JevChoices("dummy-test", "https://test.invalid/v1", transport=httpx.MockTransport(
        lambda request: pytest.fail("An oversized state remains local")))
    text = Text()
    engine = MemoryEngine(model=text, judger=judge)
    original = "x" * 25000
    engine.add(request(original))
    assert not text.splits and not text.writes and not engine.store.memories("u1")
    assert engine.store.list("sources", "u1")[0]["content"] == original
    assert judge.calls[-1]["error_type"] == "state_budget_exceeded"
    judge.close()


def test_splitting_schema_and_synthetic_examples_contain_content_only():
    from embergraph.add_judgments import SPLIT_EXAMPLES
    schema = strict_schema(SplitPlan)
    assert set(schema["$defs"]["ContentItem"]["properties"]) == {"content"}
    assert len(json.dumps(schema)) < 2000
    for example in SPLIT_EXAMPLES:
        SplitPlan.model_validate({"items": example["items"]})
        assert all(set(raw) == {"content"} for raw in example["items"])
        for raw in example["items"]:
            ContentItem.model_validate(raw)
