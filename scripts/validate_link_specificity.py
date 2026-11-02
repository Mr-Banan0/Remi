"""Check pair-specific links for generic insights and contextualized experiences."""
import argparse
import json
from pathlib import Path
from time import monotonic

from remi.memory_linking import provider_plan, validate_plan
from remi.providers import providers_from_environment
from remi.settings import load_environment
from scripts.validate_memory_links_holdout import packet


CASES = [
    ('generic_insight', 'experience', 'Cooperating on small tasks makes me feel connected.', None, [
        ('event', 'I sorted donated clothes with colleagues at the Juniper shelter.', False),
        ('event', 'My cousin and I arranged seats for a concert last month.', False),
        ('experience', 'Finishing a report with colleagues felt rewarding.', False)]),
    ('unspecified_episode', 'experience', 'Doing those little jobs together made the afternoon easier.', None, [
        ('event', 'We sorted donations at the Juniper shelter in April.', False),
        ('event', 'We painted the rehearsal room in August.', False),
        ('experience', 'Working together made the concert preparation easier.', False)]),
    ('grounded_episode', 'experience', 'Doing those little jobs together made the afternoon easier.',
     'After sorting donated clothes at the Juniper shelter on April 8, I realized that doing those little jobs together made the afternoon easier.', [
        ('event', 'We sorted donated clothes at the Juniper shelter on April 8.', True),
        ('event', 'We painted the rehearsal room in August.', False),
        ('event', 'We arranged seats for a concert in September.', False)]),
    ('bounded_lesson', 'experience', 'For the Juniper shelter donation drive, assigning sorting stations prevented duplicate work.', None, [
        ('event', 'The Juniper shelter donation drive collected 200 coats.', True),
        ('fact', 'The Juniper shelter donation drive has four sorting stations.', True),
        ('event', 'We used assigned stations for a different warehouse audit last year.', False)]),
    ('general_preference', 'preference', 'I prefer quiet surroundings for focused work.', None, [
        ('event', 'I am considering renting a desk beside the rehearsal studio for daily focused work.', True),
        ('event', 'I joined a noisy street parade while on holiday.', False),
        ('event', 'I bought a quiet electric kettle for my aunt.', False)]),
    ('specific_constraint', 'fact', 'My current medication makes me drowsy, and I was told to avoid driving.', None, [
        ('event', 'I plan to drive to the airport tomorrow.', True),
        ('event', 'I drove to a museum three years ago.', False),
        ('event', 'My colleague drives an electric car.', False)]),
]

VALIDATION = [
    ('progress_lesson', 'experience', 'Making tangible progress motivates me.', None, [
        ('event', 'I completed the first chapter of a language course yesterday.', False),
        ('event', 'I painted one wall of the studio last weekend.', False),
        ('experience', 'Finishing a difficult puzzle made me feel accomplished.', False)]),
    ('chinese_unresolved', 'experience', '那段经历让我更敢表达自己。', None, [
        ('event', '我上个月在社区会议上发言。', False),
        ('event', '我去年参加过松林剧团的首次排练。', False),
        ('experience', '参加读书会后，我更愿意分享看法。', False)]),
    ('chinese_grounded', 'experience', '那段经历让我更敢表达自己。',
     '我去年参加过松林剧团的首次排练。那段经历让我更敢表达自己。', [
        ('event', '我去年参加过松林剧团的首次排练。', True),
        ('event', '我上个月在社区会议上发言。', False),
        ('event', '我本周开始练习游泳。', False)]),
    ('spanish_scope', 'fact', 'Mi presupuesto actual para el alquiler es de 600 euros al mes.', None, [
        ('event', 'Estoy considerando un piso que cuesta 800 euros al mes.', True),
        ('event', 'Mi hermana alquila un piso de 800 euros para vivir sola.', False),
        ('event', 'Hace diez años alquilé un piso de 800 euros al mes.', False)]),
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--validation', action='store_true')
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError('fresh_output_required')
    load_environment(Path('.env'))
    model, _, _ = providers_from_environment()
    if model.model != 'gpt-4o-mini':
        raise ValueError('test_requires_gpt_4o_mini')
    records = []
    for reverse in (False, True):
        for name, kind, current, evidence, rows in (VALIDATION if args.validation else CASES):
            rows = list(reversed(rows)) if reverse else rows
            state = packet(current, [(text, yes) for _, text, yes in rows])
            state['current_memories'][0]['record_type'] = kind
            for row, (target_kind, _, _) in zip(state['recalled_memories'], rows):
                row['record_type'] = target_kind
            state['evidence'][0]['text'] = evidence or current
            raw = provider_plan(model, state, deadline=monotonic()+90)
            edges, issues = validate_plan(raw, state)
            actual = {edge.target if edge.origin == 'N1' else edge.origin for edge in edges}
            expected = {f'M{i}' for i, (_, _, yes) in enumerate(rows, 1) if yes}
            record = dict(case=name, reverse=reverse, output=raw, issues=issues,
                          passed=actual == expected and not issues,
                          false_positive=len(actual-expected), false_negative=len(expected-actual))
            records.append(record)
            args.output.write_text(json.dumps(records, ensure_ascii=False, indent=2))
            print(json.dumps(record, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
