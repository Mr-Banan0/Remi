from copy import deepcopy
from scripts.audit_golden_runs import measure, summarize


def report():
    return dict(complete=True,dataset_sha256='same',config={'top_k':100},
                questions=[dict(question_id='q',gold_evidence=['e'],evidence_ranks={'e':1},seconds=1,returned_bytes=100)],
                lifecycle_searches=[dict(top1_expected_evidence=True)],errors=[])


def test_incomplete_and_mismatched_runs_are_not_presented_as_successful_metrics():
    baseline=report(); targets=measure(baseline)
    pending=deepcopy(baseline);pending['complete']=False
    assert summarize(pending,baseline,targets)['metrics'] is None
    changed=deepcopy(baseline);changed['config']['top_k']=10
    assert not summarize(changed,baseline,targets)['comparable']
    duplicate=deepcopy(baseline);duplicate['questions']*=2
    assert not summarize(duplicate,baseline,targets)['question_set_complete']


def test_replay_scope_and_lifecycle_failures_remain_visible():
    baseline=report();replay=deepcopy(baseline)
    replay['original_full_run']='bank.json';replay['config']['frozen_planner_report']='plan.json'
    replay['lifecycle_searches'][0]['top1_expected_evidence']=False
    row=summarize(replay,baseline,measure(baseline))
    assert row['scope']=='search_replay_frozen'
    assert row['lifecycle_passed']==0 and row['expected_lifecycle']==1
    assert row['delta_from_historical_best']=={'recall10':0,'mrr10':0,'recall100':0}


def test_algorithm_parameters_are_reported_separately_from_resource_budget():
    baseline=report();changed=deepcopy(baseline)
    changed['config']['settings']={'memory_update_min_similarity':.78}
    row=summarize(changed,baseline,measure(baseline))
    assert row['comparable']
    assert row['method_settings']=={'memory_update_min_similarity':.78}
    changed['config']['settings']['candidate_limit']=100
    assert not summarize(changed,baseline,measure(baseline))['comparable']
