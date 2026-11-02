"""Check update identity with mixed candidates and shared-person distractors."""
import argparse
from copy import deepcopy
import json
from pathlib import Path
from time import monotonic

from remi.memory_maintenance import MemoryPlan, provider_plan
from remi.providers import providers_from_environment
from remi.settings import load_environment


CASES = [
    ('separate_projects', 'fact', [
        'I want a checklist of supplies for the community mural with Lina.',
        'Lina and I feel proud of the completed community mural.',
        'I currently live in Bergen.',
    ], [
        ('I want a checklist of equipment for the radio interview with Lina.', None),
        ('The radio interview makes me nervous about speaking clearly.', None),
        ('I now live in Oslo instead of Bergen.', 'M3'),
    ]),
    ('separate_occurrences', 'event', [
        'Nadia and I planted vegetables in the shared garden on May 2.',
        'Nadia and I bought concert tickets on May 3.',
        'I plan a dentist appointment on May 9.',
    ], [
        ('Nadia and I repaired the greenhouse door on May 7.', None),
        ('Nadia and I attended the concert on May 7.', None),
        ('My May 9 dentist appointment has been moved to May 12.', 'M3'),
    ]),
    ('scope_and_lessons', 'experience', [
        'Organizing the library fundraiser taught me to delegate work early.',
        'Learning to swim taught me to practice steadily.',
        'Missing my train taught me to leave extra travel time.',
    ], [
        ('Organizing a mountain trek taught me to inspect equipment in advance.', None),
        ('Preparing for a language exam taught me to practice steadily.', None),
        ('That missed train also taught me to check the platform before buying coffee.', 'M3'),
    ]),
]

HOLDOUT = [
    ('requests_and_preferences', 'fact', [
        'Please help Omar and me organize the photographs from our wedding.',
        'My home internet connection is provided by CedarNet.',
        'I feel relieved after resolving the dispute with my landlord.',
    ], [
        ('Please help Omar and me organize the documents for our mortgage.', None),
        ('My home internet is now supplied by CloudWave instead of CedarNet.', 'M2'),
        ('I feel relieved after submitting my thesis.', None),
    ]),
    ('distinct_actions', 'event', [
        'I submitted an application to the orchestra on September 2.',
        'I plan to rehearse with Ben on September 11.',
        'Ben and I painted the fence on September 3.',
    ], [
        ('I auditioned for the orchestra on September 8.', None),
        ('The rehearsal with Ben has moved from September 11 to September 13.', 'M2'),
        ('Ben and I painted the shed on September 8.', None),
    ]),
    ('reflections_zh', 'experience', [
        '上次滑雪摔倒的经历让我学会了先检查装备。',
        '照顾住院的父亲让我学会了耐心倾听。',
        '第一次独自做饭让我学会了提前准备材料。',
    ], [
        ('上周露营淋雨的经历让我学会了先检查装备。', None),
        ('那次照顾住院父亲的经历还让我明白了及时向家人求助的重要性。', 'M2'),
        ('主持社区活动让我学会了耐心倾听。', None),
    ]),
]


def packet(kind, old, new, reverse):
    history = [dict(id=f'M{i}', record_type=kind, content=text, origin_content=text,
        entity_ids=[], status=('planned' if i == 3 else 'occurred') if kind == 'event' else None,
        event_at=None, planned_at=None, sources=[dict(id=f'S{i}', role='user', content=text,
            message_at='2029-05-03T10:00:00Z')]) for i, text in enumerate(old, 1)]
    candidates = [dict(ref=f'C{i}', content=text, record_type=kind, subject_refs=[],
        mode='asserted', context=None, status=('planned' if target else 'occurred') if kind == 'event' else None,
        time_expression=None, evidence=[dict(source_id='current')]) for i, (text, target) in enumerate(new, 1)]
    if reverse:
        history.reverse()
        candidates.reverse()
    return dict(new_user_source=dict(id='current', role='user', content=' '.join(x[0] for x in new),
        message_at='2029-05-07T10:00:00Z'), context_sources=[], resolved_subjects=[], subject_catalog=[],
        history=history, memory_candidates=candidates,
        candidate_target_ids={x['ref']: [h['id'] for h in history] for x in candidates})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--holdout', action='store_true')
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError('fresh_output_required')
    load_environment(args.env_file)
    model, _, _ = providers_from_environment()
    if model.model != 'gpt-4o-mini':
        raise ValueError('requires_gpt_4o_mini')
    results = []
    for repeat in range(2):
        for reverse in (False, True):
            for name, kind, old, new in HOLDOUT if args.holdout else CASES:
                state = packet(kind, old, new, reverse)
                raw = provider_plan(model, deepcopy(state), deadline=monotonic()+120)
                plan = MemoryPlan.model_validate(raw)
                actual = {item.ref: (item.operation, item.target_id) for item in plan.items}
                expected = {f'C{i}': ('update' if target else 'add', target)
                            for i, (_, target) in enumerate(new, 1)}
                results.append(dict(case=name, repeat=repeat, reverse=reverse,
                    passed=actual == expected, correct=sum(actual.get(k) == v for k,v in expected.items()),
                    expected=expected, output=raw))
                args.output.write_text(json.dumps(results, ensure_ascii=False, indent=2))
                print(json.dumps({k:v for k,v in results[-1].items() if k != 'output'}), flush=True)


if __name__ == '__main__':
    main()
