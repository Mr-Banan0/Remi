"""Independent source-ranking validation for references, dates and attribution."""
import argparse
import json
from pathlib import Path
from time import monotonic

from remi.providers import providers_from_environment
from remi.settings import load_environment


CASES = [
    ('relative_schedule', 'When is my ceramics class now?',
     [('user, 2025-07-02: My ceramics class was scheduled for Thursday.\nassistant: Is that still the plan?\nuser, 2025-07-02: It has moved to Friday.',
       'The ceramics class is now on July 4, 2025.'),
      ('user, 2025-06-04: My ceramics class meets on Thursdays.', 'The class meets on Thursdays.'),
      ('assistant: Classes often meet in the evening.', 'Classes may meet in the evening.')]),
    ('speaker', 'What food does Maya need to avoid?',
     [('user: Maya wrote to me: "I cannot eat peanuts because of my allergy."', 'Maya has a peanut allergy.'),
      ('user: I avoid milk because it upsets my stomach.', 'The user avoids milk.'),
      ('user: I bought peanuts for another guest.', 'Another guest received peanuts.')]),
    ('changed_preference', 'What drink do I currently prefer in the afternoon?',
     [('user, 2025-08-12: I used to choose coffee in the afternoon, but I now prefer mint tea.', 'The user currently prefers mint tea in the afternoon.'),
      ('user, 2025-01-05: Coffee is my preferred afternoon drink.', 'The user preferred afternoon coffee in January.'),
      ('assistant: Water can be refreshing in the afternoon.', 'Water is a possible afternoon drink.')]),
    ('option_reference', 'Which connector did I choose for my replacement charger?',
     [('assistant: The options are a USB-C connector first and a round barrel connector second.\nuser: I chose the second option for my replacement charger.', 'The user selected a round barrel connector.'),
      ('user: My previous charger used USB-C.', 'The previous charger used USB-C.'),
      ('assistant: Choose a charger with compatible voltage.', 'Compatible voltage matters.')]),
    ('separate_occurrences', 'Was my October 8 appointment cancelled?',
     [('user, 2025-10-07: The appointment on October 8 was cancelled. The October 15 appointment is still confirmed.', 'The October 8 appointment was cancelled.'),
      ('user, 2025-10-15: I attended my appointment today.', 'The October 15 appointment occurred.'),
      ('assistant: Remember to bring your appointment confirmation.', 'Bring confirmation to appointments.')]),
    ('state_after_event', 'Where do I live now?',
     [('user, 2025-05-01: I moved out of Lyon last month. I now live in Porto.', 'The user now lives in Porto.'),
      ('user, 2024-08-01: My home is in Lyon.', 'The user lived in Lyon in August 2024.'),
      ('user, 2025-06-01: I visited Lyon for a weekend and returned home.', 'The user visited Lyon in June.')]),
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--grounded',action='store_true')
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError('fresh_output_required')
    load_environment(Path('.env'))
    _, _, ranker = providers_from_environment()
    if args.grounded:
        from scripts.grounded_reranker import GroundedReranker
        ranker=GroundedReranker.from_existing(ranker)
    records = []
    for reverse in (False, True):
        for name, query, evidence in CASES:
            cards = [dict(id=f'D{i}', original=source, summary=summary,
                          memories=[dict(content=summary,record_type='fact',mode='asserted')],
                          retrieval_objectives=[dict(role='direct',query=query)])
                     for i,(source,summary) in enumerate(evidence)]
            if reverse:
                cards.reverse()
            started = monotonic()
            result = ranker.rank(query,cards,deadline=started+60)
            complete = len(result.scores) == len(cards)
            record = dict(case=name,reverse=reverse,scores=result.scores,complete=complete,
                          seconds=monotonic()-started,components=ranker.calls[-1].get('components'),
                          passed=complete and result.scores['D0'] > max(result.scores['D1'],result.scores['D2']))
            records.append(record)
            args.output.write_text(json.dumps(records,ensure_ascii=False,indent=2))
            print(json.dumps(record),flush=True)


if __name__ == '__main__':
    main()
