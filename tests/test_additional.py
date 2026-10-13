from datetime import timedelta

from remi.clock import parse_time
from .helpers import TestEngine as MemoryEngine
from remi.heat import hot_view, visible_memory
from remi.models import Revision
from tests.helpers import ScriptedModel, command, request


def test_same_content_distinct_dated_occurrences_in_one_source():
    model = ScriptedModel(lambda state, errors: {"commands": [
        command(state, "用户参加培训", quote="6月2日参加培训", time="6月2日"),
        command(state, "用户参加培训", quote="6月9日参加培训", time="6月9日")]})
    engine = MemoryEngine(model=model)
    engine.add(request("6月2日参加培训，6月9日参加培训。"))
    memories = engine.store.memories("u1")
    assert len(memories) == 2
    assert {memory.current.event_at[:10] for memory in memories} == {"2024-06-02", "2024-06-09"}


def test_expiry_changes_hot_view_while_history_stays_available():
    def extract(state, errors):
        draft = command(state, kind="fact", quote="2024-06-02至2024-06-09暂停工作", time="2024-06-02")
        draft["end_expression"] = "2024-06-09"
        return {"commands": [draft]}
    engine = MemoryEngine(model=ScriptedModel(extract))
    engine.add(request("2024-06-02至2024-06-09暂停工作。"))
    assert hot_view(engine.store.memories("u1"), "2024-06-03T00:00:00Z")
    assert not hot_view(engine.store.memories("u1"), "2024-06-10T00:00:00Z")
    assert len(engine.store.memories("u1")) == 1
    assert engine.store.documents("u1")


def test_retract_retains_history_and_removes_current_condition():
    model = ScriptedModel(lambda state, errors: {"commands": [command(state, kind="preference")]})
    engine = MemoryEngine(model=model)
    engine.add(request("I prefer watermelon."))
    memory = engine.store.memories("u1")[0]
    model.extractor = lambda state, errors: {"commands": [command(state, kind="preference", operation="retract", target=memory.id)]}
    engine.add(request("I withdraw my preference for watermelon.", request_id="r2", when="2024-06-03T10:00:00Z"))
    assert not hot_view(engine.store.memories("u1"), "2024-06-04T00:00:00Z")
    assert len(engine.store.memories("u1")[0].revisions) == 2
    assert hot_view(engine.store.memories("u1"), "2024-06-02T12:00:00Z")


def test_current_revision_uses_source_clock_for_out_of_order_archive():
    engine = MemoryEngine(model=ScriptedModel())
    engine.add(request("Original state."))
    memory = engine.store.memories("u1")[0]
    old = memory.current.model_copy(update={"id": "old", "content": "Older observation", "observed_at": "2024-05-01T10:00:00Z"})
    memory.revisions.append(old)
    assert memory.current.content == "Original state."
    assert visible_memory(memory, "2024-05-02T00:00:00Z").current.content == "Older observation"


def test_user_confirmation_can_select_previous_request_assistant_context():
    model = ScriptedModel(lambda state, errors: {"commands": []})
    engine = MemoryEngine(model=model)
    engine.add(request("Suggest a slot.", assistant="Option one: Tuesday; option two: Thursday."))
    def accept(state, errors):
        draft = command(state, "The user accepted Thursday", kind="event", status="planned")
        draft["context_source_ids"] = [state["context_sources"][0]["id"]]
        return {"commands": [draft]}
    model.extractor = accept
    engine.add(request("I choose option two.", request_id="r2", when="2024-06-02T11:00:00Z"))
    memory = engine.store.memories("u1")[0]
    assert len(memory.current.source_ids) == 2
    assert "Thursday" in engine.store.sources("u1", memory.current.source_ids)[1].content


def test_mixed_language_tokens_preserve_chinese_and_device_name():
    from remi.retrieval import tokenize
    tokens = tokenize("我用Dell电脑，GPU training и обучение")
    assert "dell" in tokens and "我用" in tokens and "电脑" in tokens
    assert "training" in tokens and "обучение" in tokens
