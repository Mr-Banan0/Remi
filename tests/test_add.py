from __future__ import annotations

import pytest

from remi.engine import DuplicateRequestConflict
from .helpers import TestEngine as MemoryEngine
from remi.providers import OutputInvalid, ProviderUnavailable
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
    from remi.models import AddMessage
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


def test_invalid_identity_keeps_good_sibling_and_indexes_pending_evidence():
    model = ScriptedModel(lambda state, errors: {'commands': [command(state, 'Move completed'),
        command(state, 'meeting cancelled.', objects=[{'kind': 'person', 'name': 'Unmentioned'}])]})
    engine = MemoryEngine(model=model)
    engine.add(request('Move completed; meeting cancelled.'))
    assert [m.current.content for m in engine.store.memories('u1')] == ['Move completed']
    progress = engine.store.get('requests', 'u1', 'r1')
    assert progress['status'] == 'complete'
    assert [d['status'] for d in progress['units']['0']['candidate_dispositions']] == ['accepted','pending']
    assert any(d.id.startswith('doc_pending_') for d in engine.store.documents('u1'))
    assert engine.store.list('sources', 'u1')


def test_invalid_output_keeps_sources_and_a_retryable_checkpoint():
    model=ScriptedModel(lambda state,errors: (_ for _ in ()).throw(OutputInvalid('invalid')))
    engine=MemoryEngine(model=model)
    with pytest.raises(ProviderUnavailable): engine.add(request('My original evidence survives.'))
    assert len(model.extractions)==0 and len(model.subject_calls)==2
    assert not engine.store.memories('u1')
    assert engine.store.get('requests','u1','r1')['status']=='pending'
    assert engine.store.list('sources','u1')[0]['content']=='My original evidence survives.'


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
    assert len(engine.store.memories("u1")) == 0
    engine.embedder = HashEmbedder()
    engine.add(request("Move happened."))
    assert len(model.extractions) == 1
    assert len(engine.store.memories("u1")[0].revisions) == 1
    assert all(document.vector for document in engine.store.documents("u1"))


def test_updates_append_and_distinct_occurrences_stay_distinct():
    model = ScriptedModel()
    engine = MemoryEngine(model=model)
    model.extractor = lambda state, errors: {"commands": [command(state, status="planned")]}
    engine.add(request("Project launch planned."))
    old = engine.store.memories("u1")[0]
    model.extractor = lambda state, errors: {"commands": [command(state, )]}
    engine.add(request("Project launch completed.", request_id="r2"))
    updated = engine.store.memories("u1", [old.id])[0]
    assert updated == old
    assert updated.current.content == "Project launch planned."
    assert len(engine.store.memories("u1")) == 2
    model.extractor = lambda state, errors: {"commands": [command(state)]}
    engine.add(request("Another project launch happened.", request_id="r3"))
    assert len(engine.store.memories("u1")) == 3
    assert not any(edge.relation == "revises" for edge in engine.store.edges_for("u1"))


def test_preference_and_new_event_remain_independent():
    model = ScriptedModel(lambda state, errors: {"commands": [command(state, kind="preference")]})
    engine = MemoryEngine(model=model)
    engine.add(request("I like watermelon."))
    old = engine.store.memories("u1")[0]
    model.extractor = lambda state, errors: {"commands": [command(state, kind="event", )]}
    engine.add(request("Watermelon shipment arrived.", request_id="r2"))
    assert engine.store.get("requests","u1","r2")["units"]["0"]["candidate_dispositions"][0]["status"] == "accepted"
    assert len(engine.store.memories("u1")) == 2
    assert len(engine.store.memories("u1")[0].revisions) == 1
    assert engine.store.memories("u1")[0].record_type == "preference"


def test_foreign_owner_sources_are_rejected():
    model=ScriptedModel();engine=MemoryEngine(model=model)
    engine.add(request('Private source.'));source=engine.store.list('sources','u1')[0]['id']
    model.extractor=lambda state,errors:{'commands':[{**command(state),'source_id':source}]}
    with pytest.raises(ProviderUnavailable):engine.add(request('New source.',owner='u2'))
    assert not engine.store.memories('u2')
    assert not engine.search(query('Private',owner='u3')).data


def test_final_time_failure_preserves_content_with_unknown_time():
    model = ScriptedModel(lambda state, errors: {"commands": [command(state, time="next solstice")]})
    engine = MemoryEngine(model=model)
    assert engine.add(request("I completed the move.")).success
    assert not engine.store.memories("u1")
    assert "completed the move" in engine.search(query("move")).data[0].content
    assert len(model.extractions) == 1 and len(model.subject_calls) == 1


def test_separate_events_get_their_own_dates_and_absolute_references():
    def extract(state, errors):
        return {"commands": [command(state, "昨天取消了明天的聚会", quote="昨天取消了明天的聚会", time="昨天", status="cancelled"),
                             command(state, "今天搬家", quote="今天搬家", time="今天")]}
    engine = MemoryEngine(model=ScriptedModel(extract))
    engine.add(request("昨天取消了明天的聚会，今天搬家。", when="2026-10-12T10:00:00+00:00"))
    memories = sorted(engine.store.memories("u1"), key=lambda memory: memory.current.event_at or memory.current.planned_at)
    assert len(memories) == 2
    assert memories[0].current.planned_at.startswith("2026-10-11")
    assert "2026-10-13" in memories[0].current.content
    assert memories[1].current.event_at.startswith("2026-10-12")


@pytest.mark.parametrize("kind", ["preference", "fact", "event", "experience"])
def test_all_kinds_use_the_same_path(kind):
    engine = MemoryEngine(model=ScriptedModel(lambda state, errors: {"commands": [command(state, kind=kind)]}))
    engine.add(request("Concrete independently sourced memory."))
    assert engine.store.memories("u1")[0].record_type == kind
