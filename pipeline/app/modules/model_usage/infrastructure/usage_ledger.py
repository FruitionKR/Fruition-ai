"""AI DB에 호출별 사용량을 보존한다. 작업 롤백과 별도 트랜잭션을 사용한다."""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from time import perf_counter
from uuid import uuid4

from app.modules.wiki_ingestion.infrastructure import postgres_wiki_ingestion_repository as database

_actor = ContextVar('model_usage_actor', default=None)


@contextmanager
def usage_scope(command):
    actor = {key: str(command.get(key) or '') for key in ('run_id', 'workspace_id', 'user_id', 'kind')}
    token = _actor.set(actor if all(actor[key] for key in ('run_id', 'workspace_id', 'user_id')) else None)
    try:
        yield
    finally:
        _actor.reset(token)


def _count(usage, key):
    value = usage.get(key)
    return value if type(value) is int and value >= 0 else None


@contextmanager
def track_call(provider, model):
    actor = _actor.get()
    receipt = {}
    if actor is None:
        yield receipt
        return
    call_id = str(uuid4())
    with database.connect_ai() as conn:
        conn.execute('''INSERT INTO ai_model_usage
            (id, run_id, workspace_id, user_id, kind, provider, requested_model, model, status)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'started')''',
            (call_id, actor['run_id'], actor['workspace_id'], actor['user_id'], actor['kind'], provider, model, model))
    started = perf_counter()
    status = 'failed'
    try:
        yield receipt
        status = 'succeeded'
    finally:
        response = receipt.get('response')
        usage = getattr(response, 'usage_metadata', None)
        usage = usage if isinstance(usage, dict) else {}
        incoming, outgoing = _count(usage, 'input_tokens'), _count(usage, 'output_tokens')
        input_details = usage.get('input_token_details') or {}
        output_details = usage.get('output_token_details') or {}
        cached = _count(input_details, 'cache_read') if isinstance(input_details, dict) else None
        created = _count(input_details, 'cache_creation') if isinstance(input_details, dict) else None
        reasoning = _count(output_details, 'reasoning') if isinstance(output_details, dict) else None
        if incoming is not None and cached is not None and cached > incoming:
            cached = None
        metadata = getattr(response, 'response_metadata', None)
        actual_model = (metadata.get('model_name') or metadata.get('model')) if isinstance(metadata, dict) else None
        actual_model = actual_model if isinstance(actual_model, str) and actual_model.strip() else model
        with database.connect_ai() as conn:
            conn.execute('''UPDATE ai_model_usage SET status=%s, model=%s, input_tokens=%s, output_tokens=%s,
                cached_input_tokens=%s, cache_creation_tokens=%s, reasoning_tokens=%s, duration_ms=%s,
                finished_at=now() WHERE id=%s''',
                (status, actual_model, incoming, outgoing, cached, created, reasoning,
                 (perf_counter()-started)*1000, call_id))


class PostgresUsageRepository:
    def summarize(self, workspace_id: str, user_id: str, start: datetime, end: datetime):
        with database.connect_ai() as conn:
            rows = conn.execute('''SELECT provider, requested_model, model, count(*) AS calls,
                count(*) FILTER (WHERE status='failed') AS failed_calls,
                count(*) FILTER (WHERE status='started') AS unfinished_calls,
                count(*) FILTER (WHERE input_tokens IS NULL OR output_tokens IS NULL) AS unknown_usage_calls,
                COALESCE(sum(input_tokens),0) AS known_input_tokens,
                COALESCE(sum(output_tokens),0) AS known_output_tokens,
                COALESCE(sum(cached_input_tokens),0) AS known_cached_input_tokens,
                COALESCE(sum(cache_creation_tokens),0) AS known_cache_creation_tokens,
                COALESCE(sum(reasoning_tokens),0) AS known_reasoning_tokens,
                count(*) FILTER (WHERE cached_input_tokens IS NULL) AS unknown_cache_usage_calls
                FROM ai_model_usage WHERE workspace_id=%s AND user_id=%s AND started_at>=%s AND started_at<%s
                GROUP BY provider, requested_model, model ORDER BY provider, model, requested_model''',
                (workspace_id, user_id, start, end)).fetchall()
        return [dict(row) for row in rows]
