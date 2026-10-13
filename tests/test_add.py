from __future__ import annotations

import pytest

from embergraph.engine import DuplicateRequestConflict
from .helpers import TestEngine as MemoryEngine
from embergraph.providers import OutputInvalid, ProviderUnavailable
from .helpers import HashEmbedder, ScriptedModel, command, query, request


def test_raw_assistant_and_memory_are_indexed_before_success():
    engine = MemoryEngine(model=ScriptedModel(), embedder=HashEmbedder())
    result = engine.add(request("I used a Dell for teaching.", assistant="Try updating your driver."))
    assert result.success
    assert len(engine.store.list("sources", "u1")) == 2
    assert len(engine.store.memories("u1")) == 1
    assert all(document.vector is not None for document in engine.store.documents("u1"))
    assert "driver" in engine.search(query("driver")).data[0].content


def test_assistant_only_stays_raw():
    from embergraph.models import AddMessage
    message = request("temporary")
    message.messages = [AddMessage(role="assistant", content="You should try running.")]
    engine = MemoryEngine(model=ScriptedModel())
    engine.add(message)
    assert engine.store.memories("u1") == []
    assert engine.search(query("running")).data


def test_idempotence_and_conflict():
    model = ScriptedModel()
    engine = MemoryEngine(model=model)
    engine.add(request("An event."))
    engine.add(request("An event."))
    assert len(model.extractions) == 1
    assert len(engine.store.memories("u1")) == 1
    with pytest.raises(DuplicateRequestConflict):
        engine.add(request("Another payload."))


def test_independent_operations_survive_one_bad_command():
    def extract(state, errors):
        if errors:
            return {"replacements": [{"position": 1, "command": command(state, "meeting cancelled.",
                quote="meeting cancelled.", objects=[{"kind": "person", "name": "Unmentioned"}])}]}
        return {"commands": [command(state, "Move completed", quote="Move completed"),
            command(state, "meeting cancelled.", quote="meeting cancelled.", objects=[{"kind": "person", "name": "Unmentioned"}])]}
    model = ScriptedModel(extract)
    engine = MemoryEngine(model=model)
    engine.add(request("Move completed; meeting cancelled."))
    assert len(model.extractions) == 2
    assert len(engine.store.memories("u1")) == 1
    assert engine.store.get("requests", "u1", "r1")["status"] == "complete"


def test_internal_invalid_json_has_one_repair_and_raw_success():
    def extract(state, errors):
        raise OutputInvalid("invalid")
    model = ScriptedModel(extract)
    engine = MemoryEngine(model=model)
    assert engine.add(request("My original evidence survives.")).success
    assert len(model.extractions) == 2
    assert not engine.store.memories("u1")
    assert engine.search(query("evidence")).data


def test_provider_failure_preserves_raw_and_retry_checkpoint():
    def unavailable(state, errors):
        raise ProviderUnavailable("test")
    model = ScriptedModel(unavailable)
    engine = MemoryEngine(model=model)
    with pytest.raises(ProviderUnavailable):
        engine.add(request("My original event."))
    assert len(engine.store.list("sources", "u1")) == 1
    model.extractor = lambda state, errors: {"commands": [command(state)]}
    engine.add(request("My original event."))
    assert len(engine.store.memories("u1")) == 1


def test_embedding_retry_keeps_plan_and_writes_once():
    class FailingEmbedder(HashEmbedder):
        def embed(self, texts, *, deadline):
            if any("user:" in text for text in texts):
                raise ProviderUnavailable("index unavailable")
            return super().embed(texts, deadline=deadline)
    model = ScriptedModel()
    engine = MemoryEngine(model=model, embedder=FailingEmbedder())
    with pytest.raises(ProviderUnavailable):
        engine.add(request("Move happened."))
    assert len(engine.store.memories("u1")) == 1
    engine.embedder = HashEmbedder()
    engine.add(request("Move happened."))
    assert len(model.extractions) == 1
    assert len(engine.store.memories("u1")[0].revisions) == 1
    assert all(document.vector for document in engine.store.documents("u1"))


def test_updates_append_and_distinct_occurrences_stay_distinct():
    model = ScriptedModel()
    engine = MemoryEngine(model=model)
    engine.add(request("Project launch planned."))
    old = engine.store.memories("u1")[0]
    model.extractor = lambda state, errors: {"commands": [command(state, operation="update", target=old.id)]}
    engine.add(request("Project launch completed.", request_id="r2"))
    updated = engine.store.memories("u1", [old.id])[0]
    assert len(updated.revisions) == 2
    assert updated.revisions[0].content == "Project launch planned."
    model.extractor = lambda state, errors: {"commands": [command(state)]}
    engine.add(request("Another project launch happened.", request_id="r3"))
    assert len(engine.store.memories("u1")) == 2
    assert any(edge.relation == "revises" for edge in engine.store.edges_for("u1"))


def test_kind_conflict_rejected_without_altering_target():
    model = ScriptedModel(lambda state, errors: {"commands": [command(state, kind="preference")]})
    engine = MemoryEngine(model=model)
    engine.add(request("I like watermelon."))
    old = engine.store.memories("u1")[0]
    model.extractor = lambda state, errors: {"commands": [command(state, kind="event", operation="update", target=old.id)]}
    engine.add(request("Watermelon shipment arrived.", request_id="r2"))
    assert len(engine.store.memories("u1")[0].revisions) == 1


def test_foreign_owner_target_and_sources_are_rejected():
    model = ScriptedModel()
    engine = MemoryEngine(model=model)
    engine.add(request("Watermelon arrived."))
    target = engine.store.memories("u1")[0]
    model.extractor = lambda state, errors: {"commands": [command(state, operation="update", target=target.id)]}
    engine.add(request("Watermelon arrived.", owner="u2"))
    assert not engine.store.memories("u2")
    assert not engine.search(query("Watermelon", owner="u3")).data


def test_final_time_failure_preserves_content_with_unknown_time():
    model = ScriptedModel(lambda state, errors: {"commands": [command(state, time="next solstice")]})
    engine = MemoryEngine(model=model)
    assert engine.add(request("I completed the move.")).success
    memory = engine.store.memories("u1")[0]
    assert memory.current.event_at is None
    assert memory.current.content == "I completed the move."
    assert len(model.extractions) == 2


def test_separate_events_get_their_own_dates_and_absolute_references():
    def extract(state, errors):
        return {"commands": [command(state, "昨天取消了明天的聚会", quote="昨天取消了明天的聚会", time="昨天", status="cancelled"),
                             command(state, "今天搬家", quote="今天搬家", time="今天")]}
    engine = MemoryEngine(model=ScriptedModel(extract))
    engine.add(request("昨天取消了明天的聚会，今天搬家。", when="2026-10-12T10:00:00+00:00"))
    memories = sorted(engine.store.memories("u1"), key=lambda memory: memory.current.event_at)
    assert len(memories) == 2
    assert memories[0].current.event_at.startswith("2026-10-11")
    assert "2026-10-13" in memories[0].current.content
    assert memories[1].current.event_at.startswith("2026-10-12")


@pytest.mark.parametrize("kind", ["preference", "fact", "event", "experience"])
def test_all_kinds_use_the_same_path(kind):
    engine = MemoryEngine(model=ScriptedModel(lambda state, errors: {"commands": [command(state, kind=kind)]}))
    engine.add(request("Concrete independently sourced memory."))
    assert engine.store.memories("u1")[0].kind == kind
