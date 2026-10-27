"""V54 Search roles, logical-memory RRF, graph seeds and deterministic degradation."""
from remi.models import Document
from remi.providers import ProviderUnavailable, RankResult
from tests.helpers import ScriptedModel, TestEngine, query, request


def document(key):
    return Document(id=key, owner='u1', text=key, source_ids=['src_' + key], memory_id=None,
                    revision_id=None, message_at='2024-06-02T10:00:00+00:00', event_at=None,
                    created_at='2024-06-02T10:00:00+00:00')


def test_rrf_fuses_channel_ranks_instead_of_raw_scores():
    first, repeated = document('first'), document('repeated')
    fused, roles = TestEngine._fuse_channels([
        {'channel': 'base', 'role': 'direct', 'hits': [(first, .99), (repeated, .01)]},
        {'channel': 'constraint', 'role': 'constraint', 'hits': [(repeated, .0001)]},
    ], [first, repeated])
    assert [item.id for item, _ in fused] == ['repeated', 'first']
    assert roles[('source', 'src_repeated')] == {'direct', 'constraint'}


def test_constraint_role_executes_even_when_direct_lexical_coverage_is_sufficient():
    model = ScriptedModel(decide=lambda state: {'query_mode': 'action', 'steps': [{
        'role': 'constraint', 'purpose': 'history', 'query': 'ankle injury walking',
        'time_expression': None, 'anchor_source_id': None, 'anchor_step': None,
        'time_basis': 'message', 'relation': 'independent', 'select': 'all', 'window_days': 0,
    }], 'relevant_memory_ids': [], 'action_time_expression': None})
    engine = TestEngine(model=model)
    engine.add(request('Exercise plan for walking.'))
    engine.add(request('An ankle injury limits walking.', request_id='r2'))
    engine.search(query('Exercise plan for walking.'))
    assert engine.last_search['direct_coverage']['sufficient']
    assert engine.last_search['tool_results'][0]['coverage'].startswith('bounded_candidates')
    assert any(channel['role'] == 'constraint' for channel in engine.last_search['candidate_channels'])
    assert engine.last_search['fusion'] == 'logical_memory_rrf'


def test_constraint_role_expands_candidates_without_overriding_reranker():
    model = ScriptedModel(decide=lambda state: {'query_mode': 'action', 'steps': [{
        'role': 'constraint', 'purpose': 'history', 'query': 'ankle injury walking limitation',
        'time_expression': None, 'anchor_source_id': None, 'anchor_step': None,
        'time_basis': 'message', 'relation': 'independent', 'select': 'all', 'window_days': 0,
    }], 'relevant_memory_ids': [], 'action_time_expression': 'tomorrow'})

    class DirectBiasedRanker:
        def rank(self, query, cards, *, deadline):
            self.cards = cards
            return RankResult(scores={card['id']: (.1 if 'injury' in card['summary'].casefold() else .9)
                                      for card in cards})

    ranker = DirectBiasedRanker()
    engine = TestEngine(model=model, reranker=ranker)
    engine.add(request('Exercise plan for walking.'))
    engine.add(request('An ankle injury limits walking.', request_id='r2'))
    result = engine.search(query('Create an exercise plan for walking.', top_k=1))
    assert 'exercise plan' in result.data[0].content.casefold()
    injury = next(card for card in ranker.cards if 'injury' in card['summary'].casefold())
    assert {'role': 'constraint', 'query': 'ankle injury walking limitation'} in injury['retrieval_objectives']
    assert 'role_selected_ids' not in engine.last_search


def test_planner_failure_runs_bounded_constraint_fallback_without_selecting_all_hot_memory():
    def unavailable(state):
        raise ProviderUnavailable('test')

    engine = TestEngine(model=ScriptedModel(decide=unavailable))
    engine.add(request('A severe ankle injury limits walking.'))
    engine.search(query('Create a morning walking plan.'))
    assert engine.last_search['decision']['steps'][0]['role'] == 'constraint'
    assert engine.last_search['tool_results'][0]['coverage'].startswith('bounded_candidates')
    assert engine.last_search['selected_conditions'] == 0
    assert 'decision_unavailable' in engine.last_search['degradation']


def test_unresolved_temporal_step_keeps_unrestricted_fallback_as_role_evidence():
    model = ScriptedModel()
    engine = TestEngine(model=model)
    engine.add(request('Actual market activity.', when='2024-01-21T10:00:00+00:00'))
    source_id = engine.store.list('sources', 'u1')[0]['id']
    model.decider = lambda state: {'query_mode': 'verify_premise', 'steps': [{
        'role': 'premise_check', 'purpose': 'history', 'query': 'actual market activity',
        'time_expression': '2024-01-20', 'anchor_source_id': source_id,
        'anchor_step': None, 'time_basis': 'event', 'relation': 'same_day', 'select': 'all', 'window_days': 0,
    }], 'relevant_memory_ids': [], 'action_time_expression': None}
    result = engine.search(query('actual market activity', top_k=1), '2024-02-01T00:00:00+00:00')
    assert result.data and 'market' in result.data[0].content.casefold()
    assert engine.last_search['tool_results'][0]['fallback_executed']
    assert any(channel['role'] == 'premise_check' for channel in engine.last_search['candidate_channels'])


def test_latest_state_orders_only_its_bounded_graph_seeds_by_time():
    old = document('old').model_copy(update={'message_at': '2024-01-01T00:00:00+00:00'})
    recent = document('recent').model_copy(update={'message_at': '2024-10-01T00:00:00+00:00'})
    from remi.models import RecallStep
    step = RecallStep(role='latest_state', purpose='history', query='mobility state',
                      time_expression=None, anchor_source_id=None, anchor_step=None,
                      time_basis='message', relation='independent', select='latest', window_days=0)
    assert [item.id for item in TestEngine._role_graph_seeds(step, [(old, .9), (recent, .1)])] == ['recent', 'old']


def test_role_embedding_failure_falls_back_to_lexical_channel():
    class UnavailableEmbedder:
        def embed(self, texts, *, deadline):
            raise ProviderUnavailable('test')

    model = ScriptedModel(decide=lambda state: {'query_mode': 'action', 'steps': [{
        'role': 'constraint', 'purpose': 'history', 'query': 'ankle injury limitation',
        'time_expression': None, 'anchor_source_id': None, 'anchor_step': None,
        'time_basis': 'message', 'relation': 'independent', 'select': 'all', 'window_days': 0,
    }], 'relevant_memory_ids': [], 'action_time_expression': None})
    engine = TestEngine(model=model)
    engine.add(request('An ankle injury creates a walking limitation.'))
    engine.embedder = UnavailableEmbedder()
    result = engine.search(query('Create a walking exercise plan.', top_k=1))
    assert result.data and 'injury' in result.data[0].content.casefold()
    assert 'query_embedding_unavailable' in engine.last_search['degradation']
    assert 'tool_0_embedding_unavailable' in engine.last_search['degradation']
    assert engine.last_search['tool_results'][0]['coverage'].startswith('bounded_candidates')
