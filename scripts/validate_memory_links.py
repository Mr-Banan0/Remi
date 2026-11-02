"""Independent association tests; expected decisions stay outside model input."""
import argparse
import json
import subprocess
import time
from pathlib import Path

from remi import memory_linking
from remi.providers import providers_from_environment
from remi.settings import load_environment


CASES = [
    ('shared_project', 'The Linden archive digitization project has 400 boxes.',
     'The Linden archive digitization project must finish in July.', True),
    ('different_projects', 'The Linden archive digitization project has 400 boxes.',
     'The Harbor archive digitization project must finish in July.', False),
    ('current_condition', 'This week Noor has been advised to avoid loud environments.',
     'Noor is planning to attend a loud concert this Friday.', True),
    ('expired_condition', 'Noor fully recovered from noise sensitivity in 2021.',
     'Noor is planning to attend a concert in 2028.', False),
    ('same_mood', 'The pottery class left Noor feeling proud.',
     'Finishing a tax return left Noor feeling proud.', False),
    ('same_person', 'Noor keeps a spare umbrella at work.',
     'Noor prefers pear juice.', False),
    ('explicit_followup', 'The Orion telescope arrived with a cracked lens.',
     'The supplier sent a replacement lens for that damaged Orion telescope.', True),
    ('different_occurrences', 'Noor visited the museum on 3 May.',
     'Noor visited the museum on 20 August.', False),
    ('specific_preference', 'Noor avoids meals containing sesame.',
     'Noor booked a tasting menu that includes sesame paste.', True),
    ('new_state_old_event', 'Noor currently works at Harbor Labs.',
     'Noor attended an induction at Linden Labs five years ago.', False),
    ('chinese_project', '星河修缮项目需要先修屋顶。', '星河修缮项目的预算已经获批。', True),
    ('spanish_topic', 'La clase de piano me hizo feliz.', 'La cena con mis primos me hizo feliz.', False),
]


def packet(left, right):
    handles = {name: dict(handle=name, memory_id=name, revision_id='rev-'+name,
        record_type='fact', content=content) for name,content in [('N1',right),('M1',left)]}
    return dict(owner='test',current_memories=[handles['N1']],recalled_memories=[handles['M1']],
        handles=handles,pairs=[dict(id='L1',left='N1',right='M1',allowed_evidence_ids=['E1'],
            existing=False,current_pair=False)],
        evidence=[dict(id='E1',source_id='source',text=right)],
        background_links=[],preserved_edges=[],truncated=False)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--repeats',type=int,default=2)
    args=parser.parse_args()
    if args.output.exists():raise ValueError('fresh_output_required')
    load_environment(Path('.env'))
    model,_,_=providers_from_environment()
    if model.model!='gpt-4o-mini':raise ValueError('test_requires_gpt_4o_mini')
    old={'__name__':'remi._link_baseline','__package__':'remi'}
    source=subprocess.check_output(['git','show','dce4393:src/remi/memory_linking.py'],text=True)
    exec(compile(source,'baseline_linking.py','exec'),old)
    old['LinkChoice'].model_rebuild(_types_namespace=old)
    old['LinkPlan'].model_rebuild(_types_namespace=old)
    records=[]
    for repeat in range(args.repeats):
        for label,left,right,expected in CASES:
            for version,provider in [('baseline',old['provider_plan']),('candidate',memory_linking.provider_plan)]:
                state=packet(left,right);start=time.monotonic()
                try:
                    output=provider(model,state,deadline=time.monotonic()+60)
                    edges,issues=memory_linking.validate_plan(output,state)
                    actual=bool(edges)
                    record=dict(case=label,repeat=repeat,version=version,expected=expected,
                        actual=actual,passed=actual==expected and not issues,output=output,issues=issues)
                except Exception as error:
                    record=dict(case=label,repeat=repeat,version=version,expected=expected,
                        passed=False,error=type(error).__name__)
                record['seconds']=time.monotonic()-start;records.append(record)
                args.output.write_text(json.dumps(dict(records=records),ensure_ascii=False,indent=2))
                print(json.dumps(record,ensure_ascii=False),flush=True)
    summaries={}
    for version in ('baseline','candidate'):
        rows=[row for row in records if row['version']==version]
        summaries[version]=dict(passed=sum(row['passed'] for row in rows),total=len(rows),
            false_positive=sum(row.get('actual') is True and not row['expected'] for row in rows),
            false_negative=sum(row.get('actual') is False and row['expected'] for row in rows),
            provider_errors=sum('error' in row for row in rows))
    args.output.write_text(json.dumps(dict(summary=summaries,records=records),ensure_ascii=False,indent=2))
    print(json.dumps(summaries),flush=True)


if __name__=='__main__':main()
