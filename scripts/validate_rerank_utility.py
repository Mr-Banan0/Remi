"""Compare reranker evidence utility on independent action and factual queries."""
import argparse
import json
from pathlib import Path
from time import monotonic

import remi.providers as providers
from remi.engine import MemoryEngine
from remi.models import Document, Source
from remi.settings import load_environment


PROPOSED = (
    'How essential is `evidence` for a correct, feasible, personalized answer to `query`? '
    'Judge original source facts with their speakers, dates and conditions. Strong evidence supplies '
    'the requested fact, corrects an assumed premise, or establishes a condition that changes whether '
    'or how the requested action can be done. Generic encouragement, repeated task wording and advice '
    "without the user's circumstances are weak evidence. Use evidence.retrieval_objectives to understand "
    'the retrieval purpose, then check its support in the original source. A later statement changes '
    'an earlier condition only when it addresses that same condition.'
)

CASES = [
    ('availability', 'Create a weekend morning study schedule for me.', 'constraint',
     'Existing commitments that affect study availability',
     ('user','I work every Saturday and Sunday from 6 AM until noon.'),
     ('assistant','A weekend morning study routine can help you build momentum. Set reminders and celebrate small wins.'),
     ('user','I enjoy learning new skills.')),
    ('compatibility', 'Help me choose a charging dock for my laptop.', 'constraint',
     'Requirements that affect which docks work with the laptop',
     ('user','My laptop charges only through its proprietary round connector; its USB ports do not accept power.'),
     ('assistant','Choose a convenient charging dock with a clean design and place it near your desk.'),
     ('user','My desk is made of oak.')),
    ('budget', 'Help me plan a large purchase this month.', 'constraint',
     'Financial resources and existing commitments affecting the purchase',
     ('user','My remaining savings are committed to next month’s rent and tuition.'),
     ('assistant','Making a purchase can feel rewarding. Compare styles and create a motivating savings chart.'),
     ('user','I like browsing shops on weekends.')),
    ('location', 'Where did I put my passport?', 'direct', 'Where the passport is stored',
     ('user','My passport is in the blue drawer beside my desk.'),
     ('user','My passport expires next year.'),
     ('assistant','Keep important documents in a safe location.')),
    ('premise', 'How did my speech at the museum on May 6 go?', 'premise_check',
     'What actually happened on May 6 and whether the speech occurred',
     ('user','On May 6 I stayed home and the museum speech was cancelled.'),
     ('assistant','Public speaking can be rewarding. Rehearse your speech and ask the audience for feedback.'),
     ('user','I attended a museum lecture in February.')),
    ('preference', 'Which kind of tea do I prefer?', 'direct', 'The user’s tea preference',
     ('user','I prefer unsweetened green tea.'),
     ('user','I bought black tea for visiting colleagues.'),
     ('assistant','There are many teas to explore, including black and green tea.')),
]


def context_card(key, speaker, text, context, query, budget):
    engine = MemoryEngine()
    stamp = '2025-05-01T09:00:00+00:00'
    for index, (role, content) in enumerate([(speaker,text), ('assistant',context)]):
        source = Source(id=f'{key}-s{index}',owner='u',session_id='s',request_id='r',ordinal=index,
                        role=role,content=content,message_at=stamp,created_at=stamp)
        engine.store.put('sources','u',source.id,source.model_dump())
    document = Document(id=key,owner='u',text=text,source_ids=[f'{key}-s0',f'{key}-s1'],
                        memory_id=None,revision_id=None,message_at=stamp,event_at=None,created_at=stamp)
    return engine._card('u',document,query,budget)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--proposed',action='store_true')
    parser.add_argument('--evidence-budget',type=int,
                        help='Exercise production evidence cards with long conversational context.')
    parser.add_argument('--corrupt-summaries',action='store_true',
                        help='Stress source verification with misleading derived summaries.')
    parser.add_argument('--source-only',action='store_true',
                        help='Experiment-only: omit derived summary and memory fields from reranking.')
    args=parser.parse_args()
    if args.output.exists(): raise ValueError('fresh_output_required')
    load_environment(args.env_file)
    if args.proposed: providers.SUPPORT_QUESTION=PROPOSED
    _,_,ranker=providers.providers_from_environment()
    records=[]
    for repeat in range(2):
        for name,query,role,objective,*evidence in CASES:
            cards=[dict(id=f'D{i}',original=f'{speaker}: {text}',summary=text,memories=[],
                retrieval_objectives=[dict(role=role,query=objective)])
                for i,(speaker,text) in enumerate(evidence)]
            if args.evidence_budget:
                context = (query + ' ' + evidence[1][1] + ' ') * 30
                cards = [dict(context_card(f'D{i}',speaker,text,context,query,args.evidence_budget),
                              retrieval_objectives=[dict(role=role,query=objective)])
                         for i,(speaker,text) in enumerate(evidence)]
            if args.corrupt_summaries:
                for i,card in enumerate(cards):
                    claim = 'No particular answer fact was recorded.' if i == 0 else evidence[0][1]
                    card['summary'] = claim
                    card['memories'] = [dict(content=claim,record_type='fact',mode='asserted')]
            if args.source_only:
                cards = [{key:value for key,value in card.items() if key not in {'summary','memories'}}
                         for card in cards]
            if repeat: cards.reverse()
            result=ranker.rank(query,cards,deadline=monotonic()+60)
            complete=len(result.scores)==len(cards)
            passed=complete and result.scores['D0']>max(result.scores['D1'],result.scores['D2'])
            records.append(dict(case=name,repeat=repeat,complete=complete,passed=passed,scores=result.scores))
            args.output.write_text(json.dumps(records,ensure_ascii=False,indent=2))
            print(json.dumps(records[-1]),flush=True)


if __name__=='__main__': main()
