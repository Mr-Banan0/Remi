"""Experiment-only soft use of derived claims according to their source support."""
from collections import deque
import math
from time import monotonic, sleep

from remi.providers import (JevReranker, RankResult, SUPPORT_QUESTION, TEMPORAL_MATCH_QUESTION,
                            error_label, remaining, transient)


GROUNDING_QUESTION = (
    'How fully does original_evidence support the substantive claims in derived_claims? '
    'Use only the displayed original source words, speakers and dates to verify people, '
    'actions, states, conditions, timing and attribution. Resolving a reference or relative '
    'date from displayed context is valid. Shared topic or plausibility supplies no support '
    'for an unstated claim. Unsupported, contradicted or misattributed claims lower support. '
    'Ignore storage labels and identifiers. Assess source support independently of relevance to query.'
)


class GroundedReranker(JevReranker):
    @classmethod
    def from_existing(cls, ranker):
        result = object.__new__(cls)
        result.model, result.timeout, result.client = ranker.model, ranker.timeout, ranker.client
        result.calls = deque(maxlen=2048)
        return result

    def _batch(self, query, cards, deadline):
        questions, mapping, temporal = {}, {}, set()
        for index, card in enumerate(cards):
            original = self._evidence_card(card)
            keys = {name: f'pair_{index}_{name}' for name in ('source','full','ground')}
            mapping[card['id']] = keys
            for name, evidence in [('source',original),('full',card)]:
                questions[keys[name]] = dict(type='noul',instructions=dict(evidence=evidence,question=SUPPORT_QUESTION),
                    criteria={'true':'Strong contribution to the original answer under a matched retrieval objective.',
                              'false':'Lexical overlap or shared topic without useful answer evidence.'})
            claims = [card['summary']] if card.get('summary') else []
            claims += [row['content'] for row in card.get('memories',[]) if row.get('content')]
            questions[keys['ground']] = dict(type='noul',instructions=dict(original_evidence=original,
                derived_claims=list(dict.fromkeys(claims)),question=GROUNDING_QUESTION),criteria={
                    'true':'The original evidence supports the substantive derived claims with correct attribution.',
                    'false':'Substantive claims are unsupported, contradicted or assigned to the wrong person or occasion.'})
            for target in original.get('temporal_targets',[]):
                temporal.add(target['id'])
                questions[target['id']] = dict(type='noul',instructions=dict(target=target,question=TEMPORAL_MATCH_QUESTION),
                    criteria={'true':'The source supports the requested activity, participants and applicable status.',
                              'false':'Different activity, shared topic only, denied occurrence or unsupported summary.'})
        for attempt in range(2):
            started = monotonic()
            trace = dict(kind='rerank_grounded',model=self.model,batch_size=len(cards),
                         question_count=len(questions),attempt=attempt+1)
            try:
                response = self.client.post('systemone',json=dict(model=self.model,state=dict(query=query),questions=questions),
                                            timeout=remaining(deadline,self.timeout))
                trace['http_status'] = response.status_code
                response.raise_for_status()
                answers = response.json().get('answers',{})
                def score(key):
                    answer = answers.get(key,{})
                    if not isinstance(answer,dict):
                        return None
                    value = answer.get('noul')
                    return value if (answer.get('type')=='noul' and type(value) in (int,float)
                                     and math.isfinite(value) and 0 <= value <= 1) else None
                result, components = RankResult(), {}
                for identifier, keys in mapping.items():
                    parts = {name:score(key) for name,key in keys.items()}
                    components[identifier] = parts
                    if all(value is not None for value in parts.values()):
                        result.scores[identifier] = parts['ground']*parts['full'] + (1-parts['ground'])*parts['source']
                result.temporal_scores = {key:score(key) for key in temporal if score(key) is not None}
                trace['components'] = components
                return result
            except Exception as error:
                trace.update(error_label(error))
                if attempt == 0 and transient(error) and deadline-monotonic() > .25:
                    sleep(.05)
                    continue
                return RankResult()
            finally:
                trace['seconds'] = monotonic()-started
                self.calls.append(trace)
        return RankResult()
