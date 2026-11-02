"""Experiment-only separation of a source package's primary statement and context."""
import remi.providers as providers

PRIMARY_SOURCE_INSTRUCTION = (
    ' Evaluate the contribution of original. The context field provides interpretive context '
    'for references, speaker attribution and dates. Credit the primary statement for what '
    'it adds or confirms; context alone does not turn an unrelated primary statement into direct evidence.'
)


def configure(engine):
    """Keep planner and returned cards unchanged; transform only the Jev evidence view."""
    cards = {}
    original_card = engine._card
    original_evidence = engine.reranker._evidence_card
    providers.SUPPORT_QUESTION += PRIMARY_SOURCE_INSTRUCTION

    def card(owner, document, query, budget):
        result = original_card(owner, document, query, budget)
        blocks = engine._source_blocks(owner, document, query, budget)
        cards[(result['id'], result['original'])] = blocks
        return result

    def evidence(card):
        result = original_evidence(card)
        blocks = cards.get((card['id'], card['original']))
        if blocks:
            result['original'] = blocks[0]['rendered']
            result['context'] = '\n\n'.join(row['rendered'] for row in blocks[1:])
        return result

    engine._card = card
    engine.reranker._evidence_card = evidence
