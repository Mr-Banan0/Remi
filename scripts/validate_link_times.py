"""Evaluate structured-time association boundaries on independent synthetic cases."""
import argparse
import json
import subprocess
import time
from pathlib import Path

from remi import memory_linking
from remi.providers import providers_from_environment
from remi.settings import load_environment
from scripts.validate_memory_links import packet


CASES = [
    ('separate_inspections', 'Tessa inspected the studio ventilation.',
     'Tessa inspected the studio ventilation.',
     dict(event_at='2022-04-03',observed_at='2026-05-09',record_type='event'),
     dict(event_at='2026-05-09',observed_at='2026-05-09',record_type='event'), False),
    ('separate_practices', 'Hugo practiced violin in his room.',
     'Hugo practiced violin in his room.',
     dict(event_at='2026-03-02',record_type='event'),
     dict(event_at='2026-03-19',record_type='event'), False),
    ('same_exhibition_details', 'The Acacia exhibition will display wood carvings.',
     'The Acacia exhibition opens to visitors.',
     dict(planned_at='2026-06-07',observed_at='2026-01-02'),
     dict(event_at='2026-06-07',observed_at='2026-06-08',record_type='event'), True),
    ('reported_preference', 'Tessa prefers quiet places to work.',
     'Tessa is considering working in a loud shared studio.',
     dict(observed_at='2023-01-02',record_type='preference'),
     dict(planned_at='2026-05-09',observed_at='2026-05-01'), True),
]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if args.output.exists():raise ValueError('fresh_output_required')
    load_environment(Path('.env'));model,_,_=providers_from_environment()
    if model.model!='gpt-4o-mini':raise ValueError('requires_gpt_4o_mini')
    old={'__name__':'remi._link_baseline','__package__':'remi'}
    exec(compile(subprocess.check_output(['git','show','c3353d2:src/remi/memory_linking.py'],text=True),
                 'baseline_linking.py','exec'),old)
    old['LinkChoice'].model_rebuild(_types_namespace=old)
    old['LinkPlan'].model_rebuild(_types_namespace=old)
    records=[]
    for repeat in range(2):
        for label,left,right,left_meta,right_meta,expected in CASES:
            for version,provider,validator in (
                ('baseline',old['provider_plan'],old['validate_plan']),
                ('candidate',memory_linking.provider_plan,memory_linking.validate_plan)):
                state=packet(left,right)
                state['handles']['M1'].update(left_meta)
                state['handles']['N1'].update(right_meta)
                raw=provider(model,state,deadline=time.monotonic()+90)
                edges,issues=validator(raw,state)
                records.append(dict(case=label,repeat=repeat,version=version,expected=expected,
                    actual=bool(edges),passed=bool(edges)==expected and not issues,output=raw,issues=issues))
                args.output.write_text(json.dumps(records,ensure_ascii=False,indent=2))
                print(json.dumps(records[-1],ensure_ascii=False),flush=True)


if __name__=='__main__':main()
