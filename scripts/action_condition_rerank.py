"""Experiment: rank action evidence by concrete feasibility conditions."""
from copy import copy, deepcopy
from remi.providers import JevReranker

QUESTION = "Does the primary original statement provide a concrete personal condition, obligation, limitation, requirement or permission that affects whether, when or how the user can carry out the action requested in query? Evaluate relevance to that action. Use context to resolve the statement's referent, speaker and time. An existing commitment or resource restriction is a condition; wanting an attractive plan, reminder or purchase is a preference. A current permission or resolved restriction can also determine feasibility. Score the condition stated by this source, using the original query."


def configure(ranker):
    original_rank = ranker.rank
    original_score = ranker._score_questions
    def rank(query, cards, *, deadline):
        active = any('constraint' in card.get('retrieval_roles', []) for card in cards)
        if not active:
            return original_rank(query, cards, deadline=deadline)
        local = copy(ranker)
        def score(query, questions, temporal_ids, deadline):
            questions = deepcopy(questions)
            for key, question in questions.items():
                if key not in temporal_ids:
                    question['instructions']['question'] = QUESTION
            return original_score(query, questions, temporal_ids, deadline)
        local._score_questions = score
        return JevReranker.rank(local, query, cards, deadline=deadline)
    ranker.rank = rank
