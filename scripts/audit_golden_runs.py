"""Read-only comparison of Golden runs, separating replay scope from full validation."""
from collections import Counter
import argparse
import hashlib
import json
from pathlib import Path
from scripts.golden_adapter import metrics
from scripts.run_golden import fingerprint, latency

CONFIG_KEYS = ('top_k', 'add_chunking', 'search_reference_time', 'scoring', 'model',
               'reranker', 'embedding', 'embedding_dimensions')
RESOURCE_KEYS = ('hot_limit','candidate_limit','graph_limit','decision_bytes','evidence_bytes',
                 'context_bytes','role_recall_min_score','add_timeout','search_timeout',
                 'graph_enabled','hot_enabled')


def contract(report):
    config = report.get('config', {})
    result = {key: config.get(key) for key in CONFIG_KEYS}
    result['settings'] = {key:config.get('settings',{}).get(key) for key in RESOURCE_KEYS}
    return result


def method_settings(report):
    return {key:value for key,value in report.get('config',{}).get('settings',{}).items()
            if key not in RESOURCE_KEYS}


def measure(report):
    rows = report.get('questions', [])
    return {'recall10': metrics(rows, 10)['mean_evidence_recall'],
            'mrr10': metrics(rows, 10)['mrr'],
            'recall100': metrics(rows, 100)['mean_evidence_recall']}


def summarize(report, reference, targets):
    questions = report.get('questions', [])
    ids = [row['question_id'] for row in questions]
    expected = [row['question_id'] for row in reference['questions']]
    complete = bool(report.get('complete'))
    comparable = (report.get('dataset_sha256') == reference.get('dataset_sha256')
                  and contract(report) == contract(reference))
    scope = ('search_replay_frozen' if report.get('config', {}).get('frozen_planner_report')
             else 'search_replay_live') if report.get('original_full_run') else 'full'
    measured = measure(report) if complete else None
    lifecycle = report.get('lifecycle_searches', [])
    return dict(scope=scope, complete=complete, comparable=comparable, method_settings=method_settings(report),
        questions=len(questions), expected_questions=len(expected),
        question_set_complete=len(ids) == len(set(ids)) and set(ids) == set(expected),
        lifecycle_passed=sum(bool(row.get('top1_expected_evidence')) for row in lifecycle),
        lifecycle_total=len(lifecycle), expected_lifecycle=len(reference.get('lifecycle_searches', [])),
        request_errors=report.get('errors', []), metrics=measured,
        delta_from_historical_best={key: measured[key]-targets[key] for key in targets} if measured else None,
        search_latency=latency(questions),
        mean_returned_bytes=(sum(row.get('returned_bytes',0) for row in questions)/len(questions)) if questions else None,
        runtime_fingerprint=report.get('runtime_code_fingerprint'),
        provider_errors=dict(Counter(str(call.get('error_type','http_error'))+'|'+str(call.get('http_status'))
            for call in report['diagnostic_calls'] if call.get('error_type') or (call.get('http_status') or 0)>=400))
            if 'diagnostic_calls' in report else None)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline',type=Path,nargs='+',required=True)
    parser.add_argument('--run',type=Path,nargs='+',required=True)
    parser.add_argument('--output',type=Path,required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError('fresh_output_required')
    baselines = [json.loads(path.read_text()) for path in args.baseline]
    reference = baselines[0]
    if not all(row.get('complete') and row.get('dataset_sha256') == reference.get('dataset_sha256')
               and contract(row) == contract(reference) for row in baselines):
        raise ValueError('complete_matching_baselines_required')
    targets = {key:max(measure(row)[key] for row in baselines) for key in measure(reference)}
    output = dict(baselines=[dict(path=str(path),method_settings=method_settings(row))
                             for path,row in zip(args.baseline,baselines)],
                  comparison_contract=contract(reference), historical_best=targets, runs=[])
    for path in args.run:
        report = json.loads(path.read_text())
        row = summarize(report, reference, targets)
        row['path'] = str(path)
        runtime = Path(report.get('active_runtime_directory') or report.get('runtime_directory',''))
        expected = report.get('active_runtime_code_fingerprint') or report.get('runtime_code_fingerprint')
        row['runtime_matches_record'] = fingerprint(runtime) == expected if (runtime/'src').is_dir() else None
        dataset = Path(report.get('dataset',''))
        row['dataset_matches_record'] = hashlib.sha256(dataset.read_bytes()).hexdigest() == report.get('dataset_sha256') if dataset.is_file() else None
        output['runs'].append(row)
    args.output.write_text(json.dumps(output,ensure_ascii=False,indent=2))
    print(json.dumps(output,ensure_ascii=False))


if __name__ == '__main__':
    main()
