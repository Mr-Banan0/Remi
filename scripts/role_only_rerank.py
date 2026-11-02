"""Experiment: navigation queries guide retrieval; roles and original query guide scoring."""

def configure(ranker):
    original = ranker._evidence_card
    def evidence(card):
        result = original(card)
        if 'retrieval_objectives' in result:
            result['retrieval_objectives'] = [{'role': row['role']} for row in result['retrieval_objectives']]
        return result
    ranker._evidence_card = evidence
