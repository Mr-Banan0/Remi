"""Mixed-candidate association evaluation, with reversed candidate order."""
import argparse
import json
import time
from pathlib import Path

from remi.memory_linking import provider_plan, validate_plan
from remi.providers import providers_from_environment
from remi.settings import load_environment


CASES = [
    ('exhibition', 'The Marigold exhibition opens on June 8.', [
        ('The Marigold exhibition will display twelve sculptures.', True),
        ('The Marigold exhibition has a venue budget of 900 euros.', True),
        ('The Seabird exhibition opens in July.', False)]),
    ('access', 'Leila booked a hotel room accessible only by stairs for next week.', [
        ('Leila currently uses a wheelchair and needs step-free access.', True),
        ('Leila prefers orange curtains.', False),
        ('Leila enjoyed a ferry trip last summer.', False)]),
    ('repair', 'The replacement motor for the Willow pump arrived today.', [
        ('The Willow pump stopped because its motor failed.', True),
        ('The Birch pump needs a new hose.', False),
        ('The garden fountain is painted green.', False)]),
    ('chinese', '海风读书会本周六在二楼举行。', [
        ('海风读书会本期讨论《小王子》。', True),
        ('山谷读书会计划下周招募成员。', False),
        ('我上周六买了一本小说。', False)]),
    ('spanish', 'El proyecto Aurora tiene una fecha límite en diciembre.', [
        ('El proyecto Aurora dispone de un presupuesto de 500 euros.', True),
        ('El proyecto Brisa necesita tres personas.', False),
        ('Me gusta pasear en diciembre.', False)]),
    ('independent', 'Evan felt relieved after repairing the kitchen tap.', [
        ('Evan felt relieved after submitting a thesis.', False),
        ('Evan bought curtains last year.', False),
        ('Evan repaired a tap at a different house ten years ago.', False)]),
]


def packet(current, candidates):
    rows=[dict(handle='N1',memory_id='N1',revision_id='rN1',record_type='fact',content=current)]
    rows.extend(dict(handle=f'M{i}',memory_id=f'M{i}',revision_id=f'rM{i}',
        record_type='fact',content=text) for i,(text,_) in enumerate(candidates,1))
    return dict(owner='holdout',current_memories=rows[:1],recalled_memories=rows[1:],
        handles={row['handle']:row for row in rows},
        pairs=[dict(id=f'L{i}',left='N1',right=f'M{i}',allowed_evidence_ids=['E1'])
               for i in range(1,len(rows))],
        evidence=[dict(id='E1',source_id='source',text=current)],preserved_edges=[],background_links=[])


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if args.output.exists():raise ValueError('fresh_output_required')
    load_environment(Path('.env'))
    model,_,_=providers_from_environment()
    if model.model!='gpt-4o-mini':raise ValueError('test_requires_gpt_4o_mini')
    records=[]
    for reverse in (False,True):
        for name,current,candidates in CASES:
            candidates=list(reversed(candidates)) if reverse else candidates
            state=packet(current,candidates)
            raw=provider_plan(model,state,deadline=time.monotonic()+90)
            edges,issues=validate_plan(raw,state)
            actual={edge.origin if edge.target=='N1' else edge.target for edge in edges}
            expected={f'M{i}' for i,(_,yes) in enumerate(candidates,1) if yes}
            records.append(dict(case=name,reversed=reverse,passed=actual==expected and not issues,
                false_positive=len(actual-expected),false_negative=len(expected-actual),
                expected=len(expected),output=raw,issues=issues))
            args.output.write_text(json.dumps(records,ensure_ascii=False,indent=2))
            print(json.dumps(records[-1],ensure_ascii=False),flush=True)
    print(json.dumps(dict(passed=sum(row['passed'] for row in records),total=len(records),
        false_positive=sum(row['false_positive'] for row in records),
        false_negative=sum(row['false_negative'] for row in records))),flush=True)


if __name__=='__main__':main()
