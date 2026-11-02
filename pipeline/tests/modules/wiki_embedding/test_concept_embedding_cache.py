import os
from pathlib import Path
from uuid import uuid4
import psycopg
import pytest
from psycopg import sql
from psycopg.rows import dict_row
from app.core.pipeline_control import task_run_id
from app.modules.wiki_embedding.infrastructure import concept_embedding_cache as cache
from app.modules.wiki_generation.infrastructure import jev_concept_resolver as resolver


class Model:
    model_name = "test-model"
    def __init__(self):
        self.calls = []
    def embed(self, texts):
        self.calls.append(texts)
        return [[float(len(text)), 0.125] for text in texts]


@pytest.fixture
def db(monkeypatch):
    dsn = os.environ.get("TEST_AGENT_DATABASE_URL")
    if not dsn:
        pytest.skip("PostgreSQL test URL not set")
    schema = "test_embedding_cache_" + uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    def connect():
        return psycopg.connect(dsn, row_factory=dict_row, options=f"-csearch_path={schema}")
    with connect() as conn:
        conn.execute((Path(__file__).parents[3] / 'db/ai_schema.sql').read_text())
        for run, workspace in [('run1','ws1'),('run2','ws1'),('run3','ws2')]:
            conn.execute("INSERT INTO ai_task_runs(id,workspace_id,user_id,kind) VALUES (%s,%s,'user','ingest')",(run,workspace))
    monkeypatch.setattr(cache, 'connect_ai', connect)
    token = task_run_id.set('run1')
    resolver._embedding_cache.clear()
    try:
        yield connect
    finally:
        task_run_id.reset(token)
        resolver._embedding_cache.clear()
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def test_restart_reuses_database_vectors_and_preserves_order(db):
    model=Model()
    first=resolver.cached_embeddings(model,['abc','a'])
    resolver._embedding_cache.clear()
    task_run_id.set('run2')
    second=resolver.cached_embeddings(model,['a','abc'])
    assert second == first
    assert model.calls == [['abc','a']]
    assert cache.embed_cached(model,['abc','changed','abc']) == [[3.,.125],[7.,.125],[3.,.125]]
    assert model.calls[-1] == ['changed']


def test_model_and_workspace_are_isolated(db):
    model=Model()
    cache.embed_cached(model,['a'])
    model.model_name='new-model'
    cache.embed_cached(model,['a'])
    task_run_id.set('run3')
    cache.embed_cached(model,['a'])
    assert model.calls == [['a'],['a'],['a']]


def test_owner_deletion_cascades_and_deleted_scope_cannot_refill(db):
    model=Model()
    cache.embed_cached(model,['a'])
    with db() as conn:
        conn.execute("DELETE FROM ai_task_runs WHERE id='run1'")
        assert conn.execute('SELECT count(*) AS n FROM concept_embedding_cache').fetchone()['n']==0
        conn.execute("INSERT INTO ai_purged_scopes(scope_type,scope_id) VALUES ('workspace','ws1')")
    task_run_id.set('run2')
    cache.embed_cached(model,['a'])
    with db() as conn:
        assert conn.execute('SELECT count(*) AS n FROM concept_embedding_cache').fetchone()['n']==0


def test_cache_is_bounded_and_failed_model_does_not_write(db,monkeypatch):
    monkeypatch.setattr(cache,'CACHE_LIMIT',2)
    model=Model()
    cache.embed_cached(model,['a','b','c'])
    with db() as conn:
        assert conn.execute('SELECT count(*) AS n FROM concept_embedding_cache').fetchone()['n']==2
    monkeypatch.setattr(model,'embed',lambda texts: [])
    with pytest.raises(ValueError):
        cache.embed_cached(model,['missing'])
    with db() as conn:
        assert conn.execute('SELECT count(*) AS n FROM concept_embedding_cache').fetchone()['n']==2


def test_database_cache_outage_falls_back_to_model(monkeypatch):
    def unavailable():
        raise psycopg.OperationalError('unavailable')
    monkeypatch.setattr(cache,'connect_ai',unavailable)
    token=task_run_id.set('run')
    try:
        assert cache.embed_cached(Model(),['ab']) == [[2.,.125]]
    finally:
        task_run_id.reset(token)


def test_keys_reuse_vector_when_model_input_is_unchanged(db):
    # 긴 페이지에서 잘린 뒤쪽만 바뀌면 모델 입력이 같으므로 다시 계산하지 않는다.
    model=Model()
    first=cache.embed_cached(model,['head tail-1'],keys=['head'])
    task_run_id.set('run2')
    assert cache.embed_cached(model,['head tail-2','other'],keys=['head','other']) == [first[0],[5.,.125]]
    assert model.calls == [['head tail-1'],['other']]
