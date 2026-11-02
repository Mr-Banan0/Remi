"""Independent real-model checks for premise decomposition and other query modes."""
import argparse
import json
from pathlib import Path
from time import monotonic

from remi.models import Decision
from remi.providers import providers_from_environment
from remi.settings import load_environment

CASES=[('repair','I remember collecting my repaired camera on April 9. What did the technician replace?',[]),('exam','我记得3月8日参加了驾驶考试，考官给了什么建议？',[]),('concert','Recuerdo que canté en el teatro el 6 de mayo. ¿Qué dijo el público?',[]),('trip','I remember visiting the aquarium on August 14. Which exhibit impressed me?',[]),('verified','Which room did I book for the seminar?', [dict(id='D1',source_ids=['S1'],original='user: I booked Room 204 for the seminar.',summary='The user booked Room 204.')]),('contradicted','How did my June 2 sailing lesson go?', [dict(id='D1',source_ids=['S1'],original='user: On June 2 I stayed at home; my sailing lesson was postponed to June 9.',summary='Sailing lesson moved from June 2 to June 9.')])]

HOLDOUT=[('zh_assumption','我记得5月1日完成了搬家，当时谁来帮忙？',[]),('en_assumption','I remember submitting the grant on February 7. What feedback did I receive?',[]),('single_fact','When did I finish the pottery course?',[]),('temporal_join','Which generator did I rent after the workshop lost power?',[]),('current','Where do I live now?',[]),('action','Can I buy a new piano this month?',[])]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--holdout',action='store_true')
    args=parser.parse_args()
    if args.output.exists():
        raise ValueError('fresh_output_required')
    load_environment(Path('.env'))
    model,embedder,ranker=providers_from_environment()
    records=[]
    expected_modes=['verify_premise','verify_premise','direct','temporal_join','current_state','action'] if args.holdout else ['verify_premise']*4+['direct','verify_premise']
    try:
        for (name,query,evidence),expected in zip(HOLDOUT if args.holdout else CASES,expected_modes):
            state=dict(query=query,reference_time='2026-11-02T09:00:00Z',evidence=evidence,current_conditions=[])
            started=monotonic()
            result=model.decide(state,deadline=started+60)
            decision=Decision.model_validate(result)
            split=(len(decision.searches)==2 and decision.searches[0].time_expression is not None
                   and decision.searches[1].time_expression is None
                   and decision.searches[1].anchor_source_id is None
                   and decision.searches[1].anchor_step is None
                   and decision.searches[1].relation=='independent') if expected=='verify_premise' else None
            row=dict(case=name,query=query,result=result,expected_mode=expected,
                     mode_matches=decision.query_mode==expected,independent_date_activity_scopes=split,
                     seconds=monotonic()-started)
            records.append(row)
            args.output.write_text(json.dumps(records,ensure_ascii=False,indent=2))
            print(json.dumps(row,ensure_ascii=False),flush=True)
    finally:
        model.close();embedder.close();ranker.close()


if __name__=='__main__':
    main()
