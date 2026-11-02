"""Small real-provider checks for source-bound planner dates and date intervals."""
import argparse
import json
from pathlib import Path
from time import monotonic
from types import SimpleNamespace

from remi.engine import MemoryEngine
from remi.providers import providers_from_environment
from remi.settings import load_environment


CASES=[
    ('incident', 'What equipment did I use after the kiln failed?',
     'The kiln failed on 2023-08-09.', '2023-08-10T10:00:00Z'),
    ('location', 'Where did we buy the woven basket?',
     'On 2022-11-12 we bought the woven basket at Willow Market.', '2022-11-12T10:00:00Z'),
    ('interval', 'How many choir rehearsals did I attend from February 4 to February 9, 2025?',
     'I attended choir rehearsal on February 6, 2025.', '2025-02-06T10:00:00Z'),
    ('interval_zh', '2025年8月3日至2025年8月8日，我参加了几次陶艺课？',
     '2025年8月5日我参加了一次陶艺课。', '2025-08-05T10:00:00Z'),
]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if args.output.exists():raise ValueError('fresh_output_required')
    load_environment(args.env_file);model,_,_=providers_from_environment()
    if model.model!='gpt-4o-mini':raise ValueError('requires_gpt_4o_mini')
    results=[]
    for repeat in range(2):
        for name,query,content,stamp in CASES:
            state=dict(query=query,reference_time='2028-01-01T00:00:00Z',current_conditions=[],
                evidence=[dict(id='document-1',source_ids=['source-1'],memory_id=None,
                    original=f'[source-1] user at {stamp}:\n{content}',summary=content,memories=[])])
            raw=model.decide(state,deadline=monotonic()+90)
            decision,errors,_=MemoryEngine._decode_decision(raw)
            _,compile_errors=MemoryEngine._compile_searches(decision,query,state['reference_time'],
                [SimpleNamespace(id='source-1',content=content,message_at=stamp)])
            errors.extend(compile_errors)
            results.append(dict(case=name,repeat=repeat,passed=not errors,output=raw,errors=errors))
            args.output.write_text(json.dumps(results,ensure_ascii=False,indent=2))
            print(json.dumps(results[-1],ensure_ascii=False),flush=True)


if __name__=='__main__':main()
