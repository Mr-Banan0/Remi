"""Request-local failure containment keeps healthy parallel ranking unchanged."""
import json
from threading import Barrier, Lock
from time import monotonic

import httpx
import pytest

from remi.providers import JevReranker


def cards(count=40):
    return [dict(id=f'd{i}', original='Synthetic evidence') for i in range(count)]


def success(request):
    questions = json.loads(request.content)['questions']
    return httpx.Response(200, json={'answers': {
        key: {'type': 'noul', 'noul': .8} for key in questions}})


def test_outage_stops_queued_batches_and_next_request_recovers():
    count = 0
    lock = Lock()
    outage = True
    def handler(request):
        nonlocal count
        with lock:
            count += 1
        if outage:
            raise httpx.ConnectError('secret-token must stay private', request=request) from OSError(61, 'private-host')
        return success(request)
    with_rank = JevReranker('dummy', transport=httpx.MockTransport(handler))
    try:
        assert not with_rank.rank('q', cards(), deadline=monotonic()+10).scores
        assert 3 <= count <= 4  # two in-flight workers can overlap the opening
        assert any(c.get('reason') == 'connection_circuit_open' for c in with_rank.calls)
        logs = json.dumps(list(with_rank.calls))
        assert 'secret-token' not in logs and 'private-host' not in logs
        assert any(c.get('exception_chain', [{}])[-1].get('errno') == 61 for c in with_rank.calls)
        outage = False
        assert len(with_rank.rank('q', cards(), deadline=monotonic()+10).scores) == 40
    finally:
        with_rank.close()


def test_transient_connection_error_retries_with_backoff_and_phase():
    attempts = 0
    def handler(request):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            request.extensions['trace']('connection.start_tls.failed', {})
            raise httpx.ConnectError('tls failed', request=request)
        return success(request)
    ranker = JevReranker('dummy', transport=httpx.MockTransport(handler))
    try:
        assert len(ranker.rank('q', cards(1), deadline=monotonic()+5).scores) == 1
        first = ranker.calls[0]
        assert .5 <= first['retry_delay_seconds'] <= 1
        assert first['seconds'] >= first['retry_delay_seconds']
        assert first['connection_phase'] == 'connection.start_tls.failed'
        assert attempts == 2
    finally:
        ranker.close()


def test_short_budget_skips_retry():
    def handler(request):
        raise httpx.ConnectTimeout('timeout', request=request)
    ranker = JevReranker('dummy', transport=httpx.MockTransport(handler))
    try:
        assert not ranker.rank('q', cards(1), deadline=monotonic()+.2).scores
        assert len(ranker.calls) == 1
        assert ranker.calls[0]['retry_skipped'] == 'insufficient_deadline'
    finally:
        ranker.close()


def test_healthy_requests_keep_two_workers_and_all_scores():
    barrier = Barrier(2)
    def handler(request):
        barrier.wait(timeout=2)
        return success(request)
    ranker = JevReranker('dummy', transport=httpx.MockTransport(handler))
    try:
        assert len(ranker.rank('q', cards(16), deadline=monotonic()+5).scores) == 16
        assert len(ranker.calls) == 2
        assert all(c['http_status'] == 200 for c in ranker.calls)
    finally:
        ranker.close()


@pytest.mark.parametrize('status', [429, 503])
def test_service_errors_retry_without_opening_connection_circuit(status):
    def handler(request):
        return httpx.Response(status)
    ranker = JevReranker('dummy', transport=httpx.MockTransport(handler))
    try:
        ranker.rank('q', cards(24), deadline=monotonic()+10)
        assert len(ranker.calls) == 6
        assert all(not c['circuit_open'] for c in ranker.calls)
    finally:
        ranker.close()


def test_partial_scores_survive_circuit_opening():
    def handler(request):
        questions = json.loads(request.content)['questions']
        if 'd0' in questions:
            return success(request)
        raise httpx.ConnectError('offline', request=request)
    ranker = JevReranker('dummy', transport=httpx.MockTransport(handler))
    try:
        result = ranker.rank('q', cards(48), deadline=monotonic()+10)
        assert result.scores == {f'd{i}': .8 for i in range(8)}
        assert any(c.get('circuit_open') for c in ranker.calls)
    finally:
        ranker.close()


def test_successful_response_resets_consecutive_failures():
    from remi.providers import _RerankConnectionHealth
    health = _RerankConnectionHealth()
    health.record(True)
    health.record(True)
    health.record(False)
    assert health.record(True) == 1
    assert not health.stopped.is_set()
