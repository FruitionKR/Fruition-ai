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


def _record(monkeypatch):
    calls = []
    @contextmanager
    def connect():
        yield SimpleNamespace(execute=lambda sql, params=(): calls.append((sql, params)))
    monkeypatch.setattr(ledger.database, 'connect_ai', connect)
    return calls


def test_missing_actor_is_recorded_as_unattributed_with_warning(monkeypatch, caplog):
    calls = _record(monkeypatch)
    with ledger.usage_scope({'run_id': None, 'workspace_id': 'w', 'user_id': '', 'kind': 'query'}):
        with ledger.track_call('openai', 'model'): pass
    assert calls[0][1][1:5] == ('unattributed', 'w', 'unattributed', 'query')
    assert '사용자 귀속 없는 과금 호출' in caplog.text


def test_receipt_records_provider_usage_and_audio_units(monkeypatch):
    calls = _record(monkeypatch)
    with ledger.usage_scope({'run_id': 'r', 'workspace_id': 'w', 'user_id': 'u'}):
        with ledger.track_call('openai', 'transcribe') as receipt:
            receipt['usage'] = {'type': 'duration', 'seconds': 3.5}
        with ledger.track_call('openai', 'tts') as receipt:
            receipt.update(usage={'input_tokens': 7, 'output_tokens': 2}, input_characters=12, audio_seconds=1.0)
    # UPDATE 인자: status, model, in, out, cached, created, reasoning, audio_seconds, input_characters
    assert calls[1][1][:9] == ('succeeded', 'transcribe', None, None, None, None, None, 3.5, None)
    assert calls[3][1][:9] == ('succeeded', 'tts', 7, 2, None, None, None, 1.0, 12)


def test_queries_close_calls_started_over_an_hour_ago(monkeypatch):
    calls = []
    @contextmanager
    def connect():
        def execute(sql, params=()):
            calls.append(sql)
            return SimpleNamespace(fetchall=lambda: [])
        yield SimpleNamespace(execute=execute)
    monkeypatch.setattr(ledger.database, 'connect_ai', connect)
    repository = ledger.PostgresUsageRepository()
    start = datetime(2026, 9, 1, tzinfo=timezone.utc)
    repository.list_calls('r', None, None)
    repository.summarize('w', 'u', start, start + timedelta(days=1))
    for index in (0, 2):
        assert "SET status='abandoned'" in calls[index] and "interval '1 hour'" in calls[index]


def test_list_calls_by_run_or_finished_period(monkeypatch):
    import api
    from app.modules.model_usage.application.read_usage import ListUsageCallsUseCase
    from app.modules.model_usage.interfaces.http.dependencies import get_list_usage_calls
    monkeypatch.setenv('INTERNAL_CALLBACK_TOKEN', 'test-token')
    repository = Mock()
    repository.list_calls.return_value = [{
        'id': '00000000-0000-0000-0000-000000000001', 'run_id': 'r', 'workspace_id': 'w', 'user_id': 'u',
        'kind': 'speech_synthesis', 'provider': 'openai', 'requested_model': 'm', 'model': 'm', 'status': 'abandoned',
        'input_tokens': None, 'output_tokens': None, 'cached_input_tokens': None, 'cache_creation_tokens': None,
        'reasoning_tokens': None, 'audio_seconds': None, 'input_characters': 12,
        'started_at': datetime(2026, 9, 1, tzinfo=timezone.utc), 'finished_at': None}]
    api.app.dependency_overrides[get_list_usage_calls] = lambda: ListUsageCallsUseCase(repository)
    try:
        client = TestClient(api.app)
        url = '/internal/model-usage/calls'
        headers = {'X-Internal-Token': 'test-token'}
        assert client.get(f'{url}?run_id=r').status_code == 401
        response = client.get(f'{url}?run_id=r', headers=headers)
        assert response.status_code == 200
        assert response.json()['calls'][0]['input_characters'] == 12
        repository.list_calls.assert_called_once_with('r', None, None)
        period = 'finished_from=2026-09-01T00:00:00Z&finished_to=2026-09-02T00:00:00Z'
        assert client.get(f'{url}?{period}', headers=headers).status_code == 200
        assert repository.list_calls.call_args.args[0] is None
        for query in ('', f'run_id=r&{period}', 'finished_from=2026-09-01T00:00:00Z',
                      'finished_from=2026-09-01T00:00:00&finished_to=2026-09-02T00:00:00'):
            assert client.get(f'{url}?{query}', headers=headers).status_code == 400, query
    finally:
        api.app.dependency_overrides.pop(get_list_usage_calls, None)


def test_sync_http_routes_attribute_calls_to_request_id(monkeypatch):
    from app.modules.agent.interfaces.http import routes as agent_routes
    from app.modules.meeting_notes.interfaces.http import routes as meeting_routes
    from app.modules.query.interfaces.http import routes as query_routes
    from app.modules.skill.interfaces.http import routes as skill_routes
    from app.modules.wiki_schema.interfaces.http import routes as schema_routes
    actors = []
    class Model:
        def __getattr__(self, name):
            def call(*args, **kwargs):
                actors.append(ledger._actor.get())
                raise ValueError('stop')
            return call
    app = FastAPI()
    for router in (agent_routes.router, meeting_routes.router, query_routes.router,
                   skill_routes.agent_router, schema_routes.router):
        app.include_router(router)
    app.dependency_overrides.update({
        agent_routes.get_handle_agent_turn_use_case: Model,
        query_routes.get_query_answer_use_case: Model,
    })
    monkeypatch.setattr(meeting_routes, 'build_meeting_notes', lambda provider, model: Model())
    monkeypatch.setattr(schema_routes, 'build_organize_schema_use_case', lambda provider, model: Model())
    monkeypatch.setattr(schema_routes, 'build_create_schema_draft_use_case', lambda provider, model: Model())
    monkeypatch.setattr(meeting_routes, 'authorize_speech', lambda *args: None)
    monkeypatch.setattr(skill_routes, 'get_propose_skill_draft_use_case', lambda **kwargs: Model())
    monkeypatch.setattr(skill_routes, 'get_author_skill_use_case', lambda **kwargs: Model())
    actor = {'workspace_id': 'w', 'user_id': 'u'}
    llm = {'provider': 'openai', 'model': 'gpt-5-nano'}
    requests = {
        '/agent/turn': ('agent_turn', {'message': 'hi', **llm, **actor}),
        '/meeting-notes/preview': ('meeting_notes', {'segments': [{'id': 's1', 'text': '안건'}], **llm, **actor}),
        '/query': ('query', {'question': '질문', 'allow_web_search': False, **llm, **actor}),
        '/skills/draft-from-runs/preview': ('skill_draft_preview', {
            **llm, **actor, 'scope_type': 'personal', 'source_runs': [{
                'run_id': 'source', 'status': 'completed', 'request_summary': '정리', 'plan_summary': '이동',
                'successful_operations': [{'tool_name': 'create_folder', 'reason': '생성'}]}]}),
        '/wiki-schema/preview': ('wiki_schema_preview', {'raw_markdown': '# 스키마', **llm, **actor}),
        '/wiki-schema/drafts': ('wiki_schema_draft', {'raw_markdown': '# 스키마', **llm, **actor}),
    }
    with TestClient(app, raise_server_exceptions=False) as client:
        for path, (kind, body) in requests.items():
            response = client.post(path, json=body, headers={'X-Request-Id': f'req-{kind}'})
            assert actors, (path, response.status_code, response.text)
            assert actors.pop() == {'run_id': f'req-{kind}', 'workspace_id': 'w', 'user_id': 'u', 'kind': kind}, path


def test_openai_realtime_cached_tokens_key_is_normalized(monkeypatch):
    calls = []
    @contextmanager
    def connect():
        yield SimpleNamespace(execute=lambda sql, params: calls.append(params))
    monkeypatch.setattr(ledger.database, 'connect_ai', connect)
    with ledger.usage_scope({'run_id': 'r', 'workspace_id': 'w', 'user_id': 'u'}):
        with ledger.track_call('openai', 'gpt-realtime-2.1-mini') as receipt:
            receipt['usage'] = {'input_tokens': 100, 'output_tokens': 20,
                                'input_token_details': {'cached_tokens': 30, 'audio_tokens': 0}}
    assert calls[1][:5] == ('succeeded', 'gpt-realtime-2.1-mini', 100, 20, 30)


def test_anthropic_ttl_cache_creation_is_summed_without_double_count(monkeypatch):
    calls = _record(monkeypatch)
    # langchain_anthropic은 TTL별 값이 있으면 cache_creation을 0으로 둔다.
    ttl = {'cache_read': 0, 'cache_creation': 0, 'ephemeral_5m_input_tokens': 30, 'ephemeral_1h_input_tokens': 5}
    both = {**ttl, 'cache_creation': 40}
    with ledger.usage_scope({'run_id': 'r', 'workspace_id': 'w', 'user_id': 'u'}):
        for details in (ttl, both):
            with ledger.track_call('anthropic', 'claude') as receipt:
                receipt['usage'] = {'input_tokens': 100, 'output_tokens': 2, 'input_token_details': details}
    assert calls[1][1][5] == 35 and calls[3][1][5] == 40
