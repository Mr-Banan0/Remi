"""Raw source writer scope, source evidence and owner/time bounded recall."""
import pytest
from pydantic import ValidationError
from remi.models import SubjectDraft,GraphObject,AliasEvidence
from remi.identity import raw_subject_candidates
from remi.engine import MemoryEngine
from remi.storage import MemoryStore
from tests.helpers import request,query,HashEmbedder
from tests.test_subject_memory import SubjectModel

@pytest.mark.parametrize('kind',['entity','device','place','project','work','concept','team','other'])
def test_only_person_animal_organization_can_be_stored(kind):
    with pytest.raises(ValidationError):GraphObject(kind=kind,id='o',name='Thing',owner='u1',observed_at='2027-01-01T00:00:00Z')

@pytest.mark.parametrize('kind',['person','animal','organization'])
def test_direct_model_assigns_supported_subject_kind(kind):
    e=MemoryEngine(model=SubjectModel([('Sol',False,[])],kinds={'Sol':kind}));e.add(request('Sol has a particular identity.'))
    assert e.store.list('objects','u1')[0]['kind']==kind

@pytest.mark.parametrize('kind',['animal','organization'])
def test_user_identity_requires_person(kind):
    e=MemoryEngine(model=SubjectModel([('user',True,[])],kinds={'@user':kind}));e.add(request('I am speaking.'))
    assert not e.store.list('objects','u1')

@pytest.mark.parametrize('text',['The camera serial number is ZX-48.','Project Cedar uses two cameras.','The street is called Azure Lane.'])
def test_subjectless_fact_keeps_outside_scope_names_in_memory(text):
    e=MemoryEngine(model=SubjectModel([],[(text,[],[],None)]),embedder=HashEmbedder());e.add(request(text))
    assert not e.store.list('objects','u1') and e.store.memories('u1')[0].current.content==text
    assert e.search(query(text)).data


def test_recall_uses_raw_alias_matches_and_owner_knowledge_time():
    store=MemoryStore();clock='2024-06-01T00:00:00Z'
    def add(key,owner,time,name,alias):
        o=GraphObject(id=key,owner=owner,name=name,kind='person',observed_at=time,
            aliases=[AliasEvidence(name=alias,source_id='old',quote=name+' is called '+alias,observed_at=time)])
        store.put('objects',owner,key,o.model_dump())
    add('known','u1',clock,'Alexandra','Alex');add('foreign','u2',clock,'Alexander','Alex');add('future','u1','2025-01-01T00:00:00Z','Alexis','Alex')
    hits=raw_subject_candidates(store,'u1',[dict(content='Alex visited.')],clock)
    assert [(o.id,names) for o,names in hits]==[('known',['Alex'])]
    assert not raw_subject_candidates(store,'u1',[dict(content='Alexandria visited.')],clock)
