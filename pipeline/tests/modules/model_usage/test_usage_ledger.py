from contextlib import contextmanager
from contextvars import copy_context
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.modules.model_usage.infrastructure import usage_ledger as ledger
from app.modules.model_usage.application.read_usage import ReadUsageUseCase
from app.modules.model_usage.interfaces.http.routes import router
from app.modules.model_usage.interfaces.http.dependencies import get_read_usage


def test_records_retries_failures_and_preserves_context(monkeypatch):
    calls = []
    @contextmanager
    def connect():
        yield SimpleNamespace(execute=lambda sql, params: calls.append((sql, params)))
    monkeypatch.setattr(ledger.database, 'connect_ai', connect)
    command = {'run_id': 'r', 'workspace_id': 'w', 'user_id': 'u', 'kind': 'ingest'}
    def success():
        with ledger.track_call('openai', 'alias') as receipt:
            receipt['response'] = SimpleNamespace(usage_metadata={
                'input_tokens': 100, 'output_tokens': 20,
                'input_token_details': {'cache_read': 40}, 'output_token_details': {'reasoning': 5}},
                response_metadata={'model_name': 'actual-model'})
    with ledger.usage_scope(command):
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(copy_context().run, success).result()
        with pytest.raises(RuntimeError):
            with ledger.track_call('openai', 'alias'):
                raise RuntimeError('timeout')
    success()  # 사용자 실행 컨텍스트가 없는 독립 호출은 다른 사용자에 귀속하지 않는다.
    assert len(calls) == 4
    assert calls[0][1][1:5] == ('r', 'w', 'u', 'ingest')
    assert calls[1][1][:7] == ('succeeded', 'actual-model', 100, 20, 40, None, 5)
    assert calls[3][1][:7] == ('failed', 'alias', None, None, None, None, None)
    assert calls[0][1][0] != calls[2][1][0]


def test_scope_restores_parent_and_unknown_usage_is_not_zero(monkeypatch):
    calls = []
    @contextmanager
    def connect():
        yield SimpleNamespace(execute=lambda sql, params: calls.append(params))
    monkeypatch.setattr(ledger.database, 'connect_ai', connect)
    with ledger.usage_scope({'run_id': 'parent', 'workspace_id': 'w', 'user_id': 'u'}):
        with ledger.usage_scope({'run_id': 'child', 'workspace_id': 'w', 'user_id': 'u'}):
            with ledger.track_call('gemini', 'model'): pass
        with ledger.track_call('gemini', 'model'): pass
    assert calls[0][1] == 'child' and calls[2][1] == 'parent'
    assert calls[1][2] is None and calls[1][3] is None


def test_read_usage_passes_both_actor_filters_and_rejects_invalid_period():
    repository = Mock()
    repository.summarize.return_value = []
    use_case = ReadUsageUseCase(repository)
    start = datetime(2026, 9, 1, tzinfo=timezone.utc)
    result = use_case.execute('w', 'u', start, start + timedelta(days=1))
    repository.summarize.assert_called_once_with('w', 'u', start, start + timedelta(days=1))
    assert 'cost_status' not in result
    with pytest.raises(ValueError): use_case.execute('w', 'u', start, start)
    with pytest.raises(ValueError): use_case.execute('w', 'u', start.replace(tzinfo=None), start)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_read_usage] = lambda: use_case
    with TestClient(app) as client:
        assert client.get('/usage/models?workspace_id=w&user_id=u').status_code == 200
        assert client.get('/usage/models?workspace_id=w&user_id=u&from_at=2026-09-01T00:00:00').status_code == 400
        assert client.get('/usage/models?workspace_id=&user_id=u').status_code == 422


def test_internal_token_checked_before_usage_query(monkeypatch):
    import api
    monkeypatch.setenv('INTERNAL_CALLBACK_TOKEN', 'test-token')
    use_case = Mock()
    use_case.execute.return_value = {'workspace_id': 'w', 'user_id': 'u', 'models': [],
                                   'from_at': datetime.now(timezone.utc), 'to_at': datetime.now(timezone.utc)}
    api.app.dependency_overrides[get_read_usage] = lambda: use_case
    try:
        client = TestClient(api.app)
        url = '/usage/models?workspace_id=w&user_id=u'
        assert client.get(url).status_code == 401
        use_case.execute.assert_not_called()
        assert client.get(url, headers={'X-Internal-Token': 'test-token'}).status_code == 200
        assert use_case.execute.call_args.args[:2] == ('w', 'u')
    finally:
        api.app.dependency_overrides.pop(get_read_usage, None)


def test_ledger_failure_prevents_unrecorded_model_call(monkeypatch):
    monkeypatch.setattr(ledger.database, 'connect_ai', Mock(side_effect=RuntimeError('database unavailable')))
    model = Mock()
    with ledger.usage_scope({'run_id': 'r', 'workspace_id': 'w', 'user_id': 'u'}):
        with pytest.raises(RuntimeError):
            with ledger.track_call('openai', 'model'):
                model()
    model.assert_not_called()
