"""AI DB에 호출별 사용량을 보존한다. 작업 롤백과 별도 트랜잭션을 사용한다."""
import asyncio
import logging
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from datetime import datetime
from time import perf_counter
from uuid import uuid4

from app.modules.wiki_ingestion.infrastructure import postgres_wiki_ingestion_repository as database

logger = logging.getLogger(__name__)
_actor = ContextVar('model_usage_actor', default=None)
UNATTRIBUTED = 'unattributed'
CALL_COLUMNS = '''id, run_id, workspace_id, user_id, kind, provider, requested_model, model, status,
    input_tokens, output_tokens, cached_input_tokens, cache_creation_tokens, reasoning_tokens,
    audio_seconds, input_characters, started_at, finished_at'''


@contextmanager
def usage_scope(command):
    # 빠진 귀속 값은 건너뛰지 않고 unattributed로 남겨 누락 경로가 원장에 드러나게 한다.
    actor = {key: str(command.get(key) or '') or UNATTRIBUTED for key in ('run_id', 'workspace_id', 'user_id')}
    actor['kind'] = str(command.get('kind') or '')
    token = _actor.set(actor)
    try:
        yield
    finally:
        _actor.reset(token)


def _count(usage, key):
    value = usage.get(key)
    return value if type(value) is int and value >= 0 else None


def _seconds(value):
    return float(value) if type(value) in (int, float) and value >= 0 else None


def start_call(provider, model):
    """호출 전 started 행을 남긴다. scope 밖 호출은 기록할 사용자가 없어 None을 돌려준다."""
    actor = _actor.get()
    if actor is None:
        logger.warning('사용량 원장 scope 밖 모델 호출: provider=%s model=%s', provider, model)
        return None
    if UNATTRIBUTED in actor.values():
        logger.warning('사용자 귀속 없는 과금 호출: run_id=%s workspace_id=%s user_id=%s kind=%s provider=%s model=%s',
                       actor['run_id'], actor['workspace_id'], actor['user_id'], actor['kind'], provider, model)
    call_id = str(uuid4())
    with database.connect_ai() as conn:
        conn.execute('''INSERT INTO ai_model_usage
            (id, run_id, workspace_id, user_id, kind, provider, requested_model, model, status)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'started')''',
            (call_id, actor['run_id'], actor['workspace_id'], actor['user_id'], actor['kind'], provider, model, model))
    return call_id


def finish_call(call_id, status, receipt, model, duration_ms):
    """receipt의 LangChain 응답(response)이나 공급사 usage, 오디오 단위를 행에 채운다."""
    if call_id is None:
        return
    response = receipt.get('response')
    usage = receipt.get('usage', getattr(response, 'usage_metadata', None))
    usage = usage if isinstance(usage, dict) else {}
    incoming, outgoing = _count(usage, 'input_tokens'), _count(usage, 'output_tokens')
    input_details = usage.get('input_token_details') or {}
    output_details = usage.get('output_token_details') or {}
    cached = _count(input_details, 'cache_read') if isinstance(input_details, dict) else None
    created = _count(input_details, 'cache_creation') if isinstance(input_details, dict) else None
    reasoning = _count(output_details, 'reasoning') if isinstance(output_details, dict) else None
    if incoming is not None and cached is not None and cached > incoming:
        cached = None
    audio_seconds = _seconds(receipt.get('audio_seconds'))
    if audio_seconds is None and usage.get('type') == 'duration':
        audio_seconds = _seconds(usage.get('seconds'))
    characters = _count(receipt, 'input_characters')
    metadata = getattr(response, 'response_metadata', None)
    actual_model = (metadata.get('model_name') or metadata.get('model')) if isinstance(metadata, dict) else None
    actual_model = actual_model if isinstance(actual_model, str) and actual_model.strip() else model
    with database.connect_ai() as conn:
        conn.execute('''UPDATE ai_model_usage SET status=%s, model=%s, input_tokens=%s, output_tokens=%s,
            cached_input_tokens=%s, cache_creation_tokens=%s, reasoning_tokens=%s, audio_seconds=%s,
            input_characters=%s, duration_ms=%s, finished_at=now() WHERE id=%s''',
            (status, actual_model, incoming, outgoing, cached, created, reasoning, audio_seconds,
             characters, duration_ms, call_id))


@contextmanager
def track_call(provider, model):
    call_id = start_call(provider, model)
    receipt = {}
    started = perf_counter()
    status = 'failed'
    try:
        yield receipt
        status = 'succeeded'
    finally:
        finish_call(call_id, status, receipt, model, (perf_counter()-started)*1000)


@asynccontextmanager
async def track_call_async(provider, model):
    """이벤트 루프를 막지 않도록 원장 쓰기를 스레드에서 한다."""
    call_id = await asyncio.to_thread(start_call, provider, model)
    receipt = {}
    started = perf_counter()
    status = 'failed'
    try:
        yield receipt
        status = 'succeeded'
    finally:
        await asyncio.to_thread(finish_call, call_id, status, receipt, model, (perf_counter()-started)*1000)


def close_abandoned(conn):
    # ponytail: 조회 직전에 정리한다. 조회 없이 상태를 봐야 하는 소비자가 생기면 주기 작업으로 옮긴다.
    conn.execute('''UPDATE ai_model_usage SET status='abandoned', finished_at=now()
        WHERE status='started' AND started_at < now() - interval '1 hour' ''')


class PostgresUsageRepository:
    def summarize(self, workspace_id: str, user_id: str, start: datetime, end: datetime):
        with database.connect_ai() as conn:
            close_abandoned(conn)
            rows = conn.execute('''SELECT provider, requested_model, model, count(*) AS calls,
                count(*) FILTER (WHERE status='failed') AS failed_calls,
                count(*) FILTER (WHERE status IN ('started', 'abandoned')) AS unfinished_calls,
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

    def list_calls(self, run_id: str | None, start: datetime | None, end: datetime | None):
        with database.connect_ai() as conn:
            close_abandoned(conn)
            if run_id is not None:
                rows = conn.execute(f'SELECT {CALL_COLUMNS} FROM ai_model_usage WHERE run_id=%s ORDER BY started_at, id',
                                    (run_id,)).fetchall()
            else:
                rows = conn.execute(f'''SELECT {CALL_COLUMNS} FROM ai_model_usage
                    WHERE finished_at>=%s AND finished_at<%s ORDER BY finished_at, id''', (start, end)).fetchall()
        return [dict(row) for row in rows]
