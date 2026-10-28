"""V53 search activates a logical Memory from any visible revision or source."""
from remi.models import SearchRequest
from tests.helpers import ScriptedModel, query, request
from tests.test_event_maintenance import OWNER, lifecycle, req, update


def test_old_version_hit_activates_current_version_and_revision_path():
    engine,model=lifecycle();memory_id=engine.store.memories(OWNER)[0].id
    old_revision=engine.store.memories(OWNER)[0].current.id
    model.writer=lambda state:[update(state,content='Aster plans the workshop on 2030-01-05.')]
    engine.add(req('Aster moved that workshop to January 5.','move',1))
    current_revision=engine.store.memories(OWNER)[0].current.id
    engine.model=None
    result=engine.search(SearchRequest(user_id=OWNER,query='workshop January 3',top_k=10),
                         '2030-01-10T00:00:00+00:00')
    assert len(result.data)==1
    assert '2030-01-03' in result.data[0].content and '2030-01-05' in result.data[0].content
    assert any(path['memory_id']==memory_id and path['revision_ids']==[old_revision,current_revision]
               for path in engine.last_search['revision_paths'])
    assert engine.last_search['memory_bundles']==1


def test_as_of_search_does_not_expose_a_later_revision():
    engine,model=lifecycle()
    model.writer=lambda state:[update(state,content='Aster plans the workshop on 2030-01-05.')]
    engine.add(req('Aster moved that workshop to January 5.','move',1))
    engine.model=None
    result=engine.search(SearchRequest(user_id=OWNER,query='workshop',top_k=10),
                         '2030-01-01T23:59:59+00:00')
    content='\n'.join(item.content for item in result.data)
    assert '2030-01-03' in content and '2030-01-05' not in content


def test_strong_direct_evidence_skips_unrestricted_supplement_execution():
    model=ScriptedModel(decide=lambda state:{'query_mode':'direct','action_time_expression':None,
        'searches':[{'role':'direct','query':'Dell evidence','time_expression':None,
        'anchor_source_id':None,'anchor_step':None,'relation':'independent'}],
        'relevant_memory_ids':[]})
    from tests.helpers import TestEngine
    engine=TestEngine(model=model)
    engine.add(request('Dell evidence supports remote work.'))
    result=engine.search(query('Dell evidence'))
    assert result.data
    assert engine.last_search['direct_coverage']['sufficient']
    assert engine.last_search['tool_results'][0]['coverage']=='direct_evidence_sufficient'
