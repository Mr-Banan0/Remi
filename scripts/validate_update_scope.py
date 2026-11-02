"""Isolate memory identity decisions with fixed, independent candidate/target packets."""
import argparse
import json
from pathlib import Path
from time import monotonic

from remi.memory_maintenance import MemoryPlan, provider_plan
from remi.providers import providers_from_environment
from remi.settings import load_environment


CASES=[
    ('different_feelings','fact','I felt refreshed after a long sleep.',
     'I felt encouraged after the ceramics exhibition.','add'),
    ('different_contexts','fact','The successful audition left me feeling confident.',
     'The completed visa paperwork left me feeling relieved.','add'),
    ('different_lessons','experience','Missing the train taught me to arrive at the station early.',
     'Baking bread taught me to measure ingredients before mixing.','add'),
    ('same_residence','fact','I currently live in Durham.',
     'I now live in Lancaster instead of Durham.','update'),
    ('same_preference','preference','I prefer paper notebooks for meeting notes.',
     'I now prefer digital notes instead of paper notebooks for meeting notes.','update'),
    ('separate_occurrence','event','I attended a pottery lesson on April 2.',
     'I attended another pottery lesson on April 17.','add'),
    ('rescheduled_occurrence','event','I plan a pottery lesson on April 20.',
     'That pottery lesson has moved to April 22.','update'),
    ('same_condition','fact','My wrist injury limits typing.',
     'The wrist injury has healed and no longer limits typing.','update'),
]


def packet(kind,old,new):
    old_source=dict(id='previous',role='user',content=old,message_at='2029-04-02T10:00:00Z')
    source=dict(id='current',role='user',content=new,message_at='2029-04-17T10:00:00Z')
    status='planned' if 'plan' in old else 'occurred'
    return dict(new_user_source=source,context_sources=[],resolved_subjects=[],subject_catalog=[],
        history=[dict(id='old',record_type=kind,content=old,origin_content=old,entity_ids=[],
            status=status if kind=='event' else 'stated',event_at=None,planned_at=None,sources=[old_source])],
        candidate_target_ids={'C1':['old']},memory_candidates=[dict(ref='C1',content=new,
            record_type=kind,subject_refs=[],mode='asserted',context=None,
            status=status if kind=='event' else None,time_expression=None,evidence=[dict(source_id='current')])])


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--env-file',type=Path,required=True)
    args=parser.parse_args()
    if args.output.exists():raise ValueError('fresh_output_required')
    load_environment(args.env_file);model,_,_=providers_from_environment()
    if model.model!='gpt-4o-mini':raise ValueError('requires_gpt_4o_mini')
    records=[]
    for repeat in range(2):
        for name,kind,old,new,expected in CASES:
            state=packet(kind,old,new)
            raw=provider_plan(model,state,deadline=monotonic()+90)
            plan=MemoryPlan.model_validate(raw)
            passed=(len(plan.items)==1 and plan.items[0].ref=='C1' and
                plan.items[0].operation==expected and
                plan.items[0].target_id==('old' if expected=='update' else None))
            records.append(dict(case=name,repeat=repeat,expected=expected,passed=passed,output=raw))
            args.output.write_text(json.dumps(records,ensure_ascii=False,indent=2))
            print(json.dumps(records[-1],ensure_ascii=False),flush=True)


if __name__=='__main__':main()
