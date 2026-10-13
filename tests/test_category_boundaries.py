"""Offline prompt wiring and budgets; semantic quality is evaluated by the paid Golden run."""
import json
from collections import Counter
from pathlib import Path
from time import monotonic

import httpx

from remi.add_judgments import (CATEGORY_CRITERIA, CATEGORY_EXAMPLES, CATEGORY_INSTRUCTIONS,
                                ROUTE, ROUTE_CRITERIA, ROUTE_EXAMPLES, category_instructions)
from remi.jev_choices import JevChoices


def test_reviewed_category_examples_have_one_fixed_label_and_cover_the_boundary():
    assert len(CATEGORY_EXAMPLES) == 10
    assert Counter(example['category'] for example in CATEGORY_EXAMPLES) == {
        'fact': 5, 'event': 2, 'preference': 1, 'experience': 1, 'raw_only': 1}
    assert set(CATEGORY_CRITERIA) == {'fact', 'event', 'preference', 'experience', 'raw_only', 'uncertain'}
    assert all(example['content'] and example['basis'] for example in CATEGORY_EXAMPLES)
    assert all(example['category'] in CATEGORY_CRITERIA for example in CATEGORY_EXAMPLES)
    assert all(example['route'] in ROUTE_CRITERIA for example in ROUTE_EXAMPLES)
    mixed = next(example for example in ROUTE_EXAMPLES if example['route'] == 'multiple')
    assert {target['category'] for target in mixed['targets']} == {'event', 'fact'}


def test_prompt_content_is_english_and_bounded_with_scoped_fact_and_same_occurrence_rules():
    prompt = category_instructions('new_user_source.content', 'new_user_source', 'context_sources', single_target=True)
    assert prompt.isascii() and ROUTE.isascii()
    assert 'project or phase' in CATEGORY_CRITERIA['fact']
    assert 'duration may be unknown' in CATEGORY_CRITERIA['fact']
    assert 'same activity remains event' in prompt
    assert 'background explicitly supplied within a user request' in ROUTE
    assert len(prompt.encode()) < 5000 and len(ROUTE.encode()) < 3000


def test_original_and_focused_branches_share_the_reviewed_rules_and_examples():
    original = category_instructions('new_user_source.content', 'new_user_source', 'context_sources', single_target=True)
    focused = category_instructions('items[1].new_item.content', 'items[1].new_user_source', 'items[1].context_sources')
    for prompt in (original, focused):
        assert CATEGORY_INSTRUCTIONS in prompt
        assert json.dumps(CATEGORY_EXAMPLES, ensure_ascii=False) in prompt
    assert original.startswith('Assuming the source establishes exactly one independent memory target')
    assert '`items[1].new_item.content`' in focused
    assert '`items[1].new_user_source`' in focused


def test_real_transport_receives_shared_prompts_without_added_request_stages():
    calls = []
    def handler(request):
        payload = json.loads(request.content)
        calls.append(payload)
        answers = {}
        for key, question in payload['questions'].items():
            if question['type'] == 'noul':
                answers[key] = {'type': 'noul', 'noul': 1.0}
            else:
                selected = next(iter(question['criteria']))
                answers[key] = {'type': 'choice', 'choice': selected, 'confidence': 1.0,
                    'probabilities': {option: float(option == selected) for option in question['criteria']}}
        return httpx.Response(200, json={'answers': answers})
    judge = JevChoices('offline-dummy', 'https://test.invalid/v1', transport=httpx.MockTransport(handler))
    source = {'id': 's0', 'role': 'user', 'content': 'A synthetic assertion.'}
    try:
        judge.route({'new_user_source': source, 'context_sources': []}, deadline=monotonic()+5)
        judge.classify([{'item_id': 'i0', 'new_item': {'content': source['content']},
                         'new_user_source': source, 'context_sources': []}], deadline=monotonic()+5)
    finally:
        judge.close()
    assert len(calls) == 2
    first = calls[0]['questions']['category']
    second = calls[1]['questions']['i0']
    assert first['criteria'] == second['criteria'] == CATEGORY_CRITERIA
    assert CATEGORY_INSTRUCTIONS in first['instructions'] and CATEGORY_INSTRUCTIONS in second['instructions']
    assert calls[0]['questions']['route']['instructions'] == ROUTE
    assert all(call['model'] == 'jev-1.13.0' for call in calls)


def test_learning_example_texts_are_independent_of_the_local_golden_inputs():
    dataset = Path('/Users/bananabig/Dev/agent-memory-leaderboard-eval/solutions/traceweave/.local-data/whole-message-runtime-20261009-v1/golden/dataset-v1.json')
    if dataset.is_file():
        public_inputs = json.loads(dataset.read_text())['input_turns']
        originals = '\n'.join(turn[role] for turn in public_inputs for role in ('user', 'assistant'))
        for example in [*CATEGORY_EXAMPLES, *ROUTE_EXAMPLES]:
            assert example['content'] not in originals
