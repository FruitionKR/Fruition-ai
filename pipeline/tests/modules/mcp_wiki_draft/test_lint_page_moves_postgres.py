"""Real preview/approval transactions, with deterministic unpaid Jev/embeddings."""
from uuid import uuid4

import pytest
import test_mcp_wiki_draft as h
from test_mcp_wiki_draft import isolated_state  # noqa: F401
from test_lint_page_moves import Judge, Embedding
from app.modules.mcp_wiki_draft import lint_calculation as lint
from app.modules.wiki_generation.infrastructure import jev_concept_resolver as jev


@pytest.fixture
def selected(monkeypatch):
    judge = Judge()
    monkeypatch.setattr(jev, 'jev_concept_judge', lambda _: judge)
    monkeypatch.setattr(jev, 'embedding_server_model', lambda: Embedding())
    monkeypatch.setattr(h.wiki_repository, 'cached_embeddings', lambda _, texts: {text: [1., 0.] for text in texts})
    ledger, targets = [], []
    for name in ('parent', 'child', 'referrer'):
        entries = [{'op': 'add', 'entry_id': 'detail', 'name': 'Detail', 'parent_entry_id': None,
                    'anchor_reference_ids': ['doc_seed:B1'], 'evidence_claim_ids': ['evidence']}] if name == 'child' else []
        links = [{'source': 'concept:referrer', 'target': 'concept:child', 'relation': 'entry_reference',
                  'entry_id': 'detail', 'name': 'Detail', 'parent_entry_id': None}] if name == 'referrer' else []
        record, target, statement_id = h._seed_lint_page(name, name, entries=entries, links=links)
        ledger.append(record)
        targets.append(target)
    monkeypatch.setattr(h.calculation, 'read_contributions', lambda *_: ledger)
    with h._connect() as conn:
        conn.execute("INSERT INTO wiki_page_entry_references(page_id,entry_id,workspace_id,target_slug,name) "
                     "VALUES ('referrer','detail','workspace-1','child','Detail')")
    source = h.Source([('B0001', 'Referrer replacement evidence.')])
    grant = str(uuid4())
    payload, _ = h._lint_payload(grant, targets, lambda snapshot: [{
        'operation_id': 'replace', 'kind': 'UPSERT_STATEMENT', 'target_page_id': 'referrer', 'depends_on': [],
        'arguments': {'statement_id': statement_id, 'text': 'Referrer replacement evidence.',
                      'evidence': [{'snapshot_id': snapshot, 'block_id': 'B0001', 'quote': 'Referrer replacement evidence.'}]},
    }], source)
    return judge, source, grant, payload


def test_frozen_moves_apply_and_contributions_replay(selected, monkeypatch):
    judge, source, grant, payload = selected
    preview = h._intake(source).lint(payload)
    assert len(judge.calls) == 1
    with h._connect() as conn:
        draft = dict(conn.execute('SELECT * FROM mcp_wiki_drafts').fetchone())
        recalculated, _ = h.apply_repository_module._recalculate(conn, draft)
        assert recalculated['preview'] == draft['preview']
        assert draft['calculation_inputs']['server_effects']['deleted_page_ids'] == ['child']
        assert conn.execute("SELECT status FROM wiki_pages WHERE id='child'").fetchone()['status'] == 'active'
    def no_judge(_):
        pytest.fail('apply must not construct Jev')
    monkeypatch.setattr(jev, 'jev_concept_judge', no_judge)
    apply_payload = h._apply_payload(grant, preview, operation_id='approved-moves')
    result = h.McpWikiDraftApply(h.PostgresMcpWikiApplyRepository(), h._ValidApprovalAuthority()).apply(
        apply_payload.draft_id, apply_payload)
    assert result['state'] == 'PUBLISHED'
    with h._connect() as conn:
        assert conn.execute("SELECT status FROM wiki_pages WHERE id='child'").fetchone()['status'] == 'deleted'
        assert conn.execute("SELECT target_slug FROM wiki_page_entry_references WHERE page_id='referrer'").fetchone()['target_slug'] == 'parent'
        assert conn.execute("SELECT count(*) AS n FROM wiki_pending_contributions WHERE linked_by_operation_id='approved-moves'").fetchone()['n'] > 0
    assert len(judge.calls) == 1


@pytest.mark.parametrize('drift', ['version', 'identity'])
def test_frozen_move_target_drift_conflicts(selected, drift):
    _, source, grant, payload = selected
    preview = h._intake(source).lint(payload)
    with h._connect() as conn:
        if drift == 'version':
            conn.execute("UPDATE wiki_pages SET updated_at=now()+interval '1 second' WHERE id='parent'")
        else:
            conn.execute("UPDATE wiki_pages SET slug='new-identity' WHERE id='parent'")
    apply_payload = h._apply_payload(grant, preview, operation_id='conflicted-moves')
    result = h.McpWikiDraftApply(h.PostgresMcpWikiApplyRepository(), h._ValidApprovalAuthority()).apply(
        apply_payload.draft_id, apply_payload)
    assert result['state'] == 'CONFLICTED'
    with h._connect() as conn:
        assert conn.execute('SELECT count(*) AS n FROM wiki_publications').fetchone()['n'] == 0


def test_unselected_inbound_reference_rejected_before_jev(selected):
    judge, source, _, payload = selected
    with h._connect() as conn:
        conn.execute("INSERT INTO wiki_pages(id,page_type,title,slug,user_id,workspace_id,status,created_at,updated_at) "
                     "VALUES ('outside','concept','Outside','outside','user-1','workspace-1','active',now(),now())")
        conn.execute("INSERT INTO wiki_page_entry_references(page_id,entry_id,workspace_id,target_slug,name) "
                     "VALUES ('outside','detail','workspace-1','child','Detail')")
    with pytest.raises(h.McpWikiDraftError) as exc:
        h._intake(source).lint(payload)
    assert exc.value.code == 'INVALID_LINT_OPERATION'
    assert not judge.calls


@pytest.mark.parametrize('limit', ['max_structural_changes', 'max_deletes'])
def test_inferred_effects_obey_existing_limits(selected, limit):
    _, source, _, payload = selected
    h._intake(source).lint(payload)
    with h._connect() as conn:
        draft = dict(conn.execute('SELECT * FROM mcp_wiki_drafts').fetchone())
        kwargs = dict(
            draft_id=str(draft['draft_id']), workspace_id='workspace-1',
            target_snapshot=draft['target_pages'], source_snapshots=draft['source_snapshots'],
            operations=draft['normalized_submissions'], policy_version=draft['calculation_inputs']['policy_version'],
            server_effects=draft['calculation_inputs']['server_effects'],
            max_structural_changes=100, max_deletes=20,
        )
        kwargs[limit] = 0
        with pytest.raises(lint.PreviewCalculationError, match='exceeds structural bounds'):
            lint.calculate_lint_preview(conn, **kwargs)


def test_unavailable_keeps_client_operation(selected):
    from app.core.jev_client import JevUnavailable
    judge, source, _, payload = selected
    judge.error = JevUnavailable('offline')
    h._intake(source).lint(payload)
    with h._connect() as conn:
        draft = dict(conn.execute('SELECT * FROM mcp_wiki_drafts').fetchone())
        assert not draft['calculation_inputs']['server_effects']
        assert any(change['kind'] == 'CONCEPT_STATEMENT' for change in draft['changes'])
        assert not any(change['kind'] == 'PAGE_DELETE' for change in draft['changes'])


def test_stale_target_without_candidate_definition_still_conflicts(selected):
    judge, source, _, payload = selected
    with h._connect() as conn:
        conn.execute("UPDATE wiki_pages SET summary='',updated_at=now()+interval '1 second' WHERE id='parent'")
    with pytest.raises(h.McpWikiDraftError) as exc:
        h._intake(source).lint(payload)
    assert exc.value.code == 'TARGET_CHANGED'
    assert not judge.calls


def test_selected_vector_sql_excludes_unrelated_pages_and_entries_but_web_keeps_them(selected, monkeypatch):
    _, source, _, payload = selected
    h._seed_lint_page('outside', 'outside')
    loader = h.wiki_repository._stored_concept_vectors
    with h._connect() as conn:
        for page_id in ('parent', 'child', 'referrer', 'outside'):
            conn.execute(
                "INSERT INTO wiki_page_embeddings(page_id,embedding_model,representation_hash,embedding_vector,embedding_dimension) "
                "VALUES (%s,'fake',%s,ARRAY[1.,0.],2)", (page_id, page_id),
            )
        for page_id, entry_id in (('child', 'detail'), ('child', 'stale-entry'), ('outside', 'outside-entry')):
            conn.execute(
                "INSERT INTO wiki_embedding_vectors(id,embedding_model,representation_hash,representation_text,embedding_vector,status) "
                "VALUES (%s,'fake',%s,'entry',ARRAY[1.,0.],'completed')", (entry_id, entry_id),
            )
            conn.execute(
                "INSERT INTO wiki_embedding_units(id,embedding_vector_id,page_id,source_document_id,unit_type,text,entry_id) "
                "VALUES (%s,%s,%s,'doc_seed','sub_entry','entry',%s)", (entry_id, entry_id, page_id, entry_id),
            )
        assert set(loader(conn, 'workspace-1', 'fake', page_ids=['child'], entry_ids=['detail'])) == {'child', 'detail'}
        assert loader(conn, 'workspace-1', 'fake', page_ids=[], entry_ids=[]) == {}
        assert set(loader(conn, 'workspace-1', 'fake')) == {
            'parent', 'child', 'referrer', 'outside', 'detail', 'stale-entry', 'outside-entry',
        }
    materialized = []
    def capture(*args, **kwargs):
        result = loader(*args, **kwargs)
        materialized.append(set(result))
        return result
    monkeypatch.setattr(h.wiki_repository, '_stored_concept_vectors', capture)
    h._intake(source).lint(payload)
    # The entry has no authoritative definition, so it isn't a selected head either.
    assert materialized == [{'parent', 'child', 'referrer'}]
