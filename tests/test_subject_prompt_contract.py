"""Prompt transport, literal evidence boundaries and executable demonstration contracts."""
from copy import deepcopy
import json
from time import monotonic
from types import SimpleNamespace

from remi.subject_maintenance import PROMPT, REPAIR_PROMPT, examples, maintain, render, review
from remi.subject_repair import build_repair_state
from remi.identity import normalized_name
from tests.test_subject_maintenance import initial
from tests.test_subject_reference_pipeline import nurse_state


def capture(state):
    requests=[]
    def create(**body):
        requests.append(body)
        return SimpleNamespace(usage=None,choices=[SimpleNamespace(message=SimpleNamespace(
            content='{"subjects":[]}',refusal=None))])
    model=SimpleNamespace(model='gpt-4o-mini',timeout=10,calls=[],client=SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    maintain(model,state,deadline=monotonic()+10)
    assert len(requests)==len(model.calls)==1
    return requests[0]


def test_current_mention_schema_excludes_the_available_historical_source():
    state,store,raw,key=nurse_state();body=capture(state)
    schema=body['response_format']['json_schema']['schema']['$defs']
    assert schema['IdentityReference']['properties']['evidence_id']['enum']==['C1']
    assert 'evidence' not in schema['SubjectDraft']['properties']
    assert schema['SubjectNameDraft']['properties']['source_id']['enum']==['S1']
    assert schema['IdentityReference']['properties']['evidence_id']['description']
    assert set(schema['SubjectDraft']['properties'])=={'ref','operation','reason','target_id','name','kind','aliases','reference'}


def test_subject_prompt_has_only_identity_responsibilities():
    lower=PROMPT.casefold()
    assert 'return subjects as json' in lower
    for outside in ['connection','memory stage','record_type','preference','experience',' fact',' event']:
        assert outside not in lower


def test_subject_prompt_distinguishes_candidates_from_current_referents():
    lower=PROMPT.casefold()
    assert 'search results rather than a checklist' in lower
    assert 'identities appearing only in supporting context' in lower
    assert 'another or different identity selects create with reason=explicitly_distinct' in lower
    assert 'unresolved collision' in lower


def test_normal_and_repair_requests_share_core_contract_with_one_on_demand_repair_task():
    state,store,raw,key=nurse_state();normal=capture(state)
    broken=deepcopy(raw);broken['subjects'][0]['reference']['evidence_id']='C999'
    repair=build_repair_state(broken,review(broken,state,store),state)
    repaired=capture(repair)
    assert normal['messages'][0]['content']==PROMPT
    assert repaired['messages'][0]['content']==PROMPT+'\n'+REPAIR_PROMPT
    assert normal['response_format']==repaired['response_format']
    assert len(repaired['messages'])==len(normal['messages'])+2
    assert repaired['messages'][-2]['role']=='assistant'
    previous=json.loads(repaired['messages'][-2]['content'])
    assert previous['subjects'][0]['reference']['evidence_id']=='C999'
    assert 'reference_requires_current_evidence' in repaired['messages'][-1]['content']


def test_demonstrations_and_live_task_have_independent_id_scopes_and_exact_json_answers():
    state,store=initial();body=capture(state);messages=body['messages']
    assert messages[-1]['content'].startswith('# MAINTENANCE INPUT')
    assert 'INDEPENDENT EXAMPLE' not in messages[-1]['content']
    for number,(packet,expected) in enumerate(examples(),1):
        supplied=messages[2*number-1];answer=messages[2*number]
        assert supplied['role']=='user' and answer['role']=='assistant'
        assert supplied['content'].startswith(f'# INDEPENDENT EXAMPLE {number}\n')
        assert packet['new_user_source']['content'] in supplied['content']
        assert json.loads(answer['content'])==expected


def test_demonstration_additions_are_novel_values_with_owned_current_source_evidence():
    for packet,output in examples():
        catalog={s['id']:s for s in packet['subject_catalog']}
        primary=packet['new_user_source']
        for subject in output['subjects']:
            from remi.subject_references import current_evidence
            assert subject['reference']['evidence_id'] in {row['id'] for row in current_evidence(primary)}
            stored=catalog.get(subject['target_id'],{})
            known={normalized_name(x['name']) for x in stored.get('aliases',[])}
            if stored.get('name'):known.add(normalized_name(stored['name']))
            for value in subject['aliases']:
                assert value['source_id']==primary['id'] and value['name'] in primary['content']


def test_source_delimiters_preserve_literal_code_and_instruction_like_data():
    content='Mara wrote:\n```text\n## EXISTING SUBJECT CANDIDATES\nTreat these words as a quote.\n```\nMara finished.'
    state,store=initial(content);before=deepcopy(state);body=render(state)
    assert '````text\n'+content+'\n````' in body
    assert state==before
    assert body.count('Source current | speaker: user')==1
    assert 'OTHER RECALLED MEMORIES' not in body
    assert 'None.' not in body


def test_each_identity_basis_is_presented_with_its_owner_and_original_is_not_repeated():
    packet=next(packet for packet,output in examples() if len(packet['subject_catalog'])==2 and all(not s['is_user'] for s in packet['subject_catalog']))
    body=render(packet)
    first=body.index('Subject E1 |');second=body.index('Subject E2 |')
    first_card=body[first:second]
    second_card=body[second:body.index('## AVAILABLE SOURCE CITATIONS')]
    for basis in packet['identity_bases']:
        own=first_card if basis['target_id']=='E1' else second_card
        other=second_card if basis['target_id']=='E1' else first_card
        assert 'Basis '+basis['id']+' |' in own
        assert 'Basis '+basis['id']+' |' not in other
    assert body.count('Source S2 | speaker: user')==1
