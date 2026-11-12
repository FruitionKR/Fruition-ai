"""Legacy append publication can be previewed and approved with real PostgreSQL/S3."""
import json
from uuid import uuid4

import pytest

import test_mcp_wiki_draft as h
from test_mcp_wiki_draft import isolated_state  # noqa: F401

from app.core.ai_markdown_sanitizer import sanitize_ai_markdown
from app.modules.wiki_generation.infrastructure.assemble import ConceptPageAssembler, LinkBuilder
from app.modules.wiki_ingestion.application.concept_contribution import build_concept_contributions
from app.modules.wiki_ingestion.infrastructure.concept_evidence import (
    append_concept_evidence,
    concept_evidence_updates,
)


@pytest.mark.parametrize('with_append', [False, True])
@pytest.mark.parametrize('publication_mode', ['skeleton', 'siblings', 'llm'])
@pytest.mark.parametrize('with_entries', [False, True])
def test_append_publication_lint_preview_and_apply_preserve_both_sources(monkeypatch, with_append, publication_mode, with_entries):
    first, _, _ = h._seed_lint_page('alpha-page', 'alpha')
    base = h.read_text_object('baseline/alpha-page.md')
    original = json.loads(h.read_text_object(first['object_key']))
    saved = original
    if publication_mode == 'siblings':
        normalized = {
            'document': {'document_id': 'doc_seed'},
            'concept_ledger': [original['concept'], {
                **original['concept'], 'slug': 'beta', 'title': 'Beta',
            }],
            'evidence_units': [{**original['evidence_units'][0],
                                'related_concept_slugs': ['alpha', 'beta']}],
        }
        base = ConceptPageAssembler().build_top(
            normalized, source_key_points=original['source_key_points'],
        )[0]['markdown']
        saved = build_concept_contributions(
            operation_id=first['ingest_operation_id'], normalized=normalized,
            source_blocks=[], source_key_points=original['source_key_points'],
            links=LinkBuilder().build(normalized),
        )['alpha']
        h.write_text_object(first['object_key'], json.dumps({**saved, 'page_id': 'alpha-page'}))
        assert '[[beta|Beta]]' in base
    if publication_mode == 'llm':
        from app.modules.wiki_generation.infrastructure.generated_concept_page_assembler import GeneratedConceptPageAssembler

        base = GeneratedConceptPageAssembler().build_pages([{
            'slug': 'alpha', 'title': 'Alpha', 'definition': {'text': 'Generated explanation'},
            'evidence': [{'text': 'Seed evidence', 'anchor_reference_ids': ['B1']}],
            'source_document_ids': ['doc_seed'],
        }])[0]['markdown']
    if with_entries:
        saved['entry_changes'] = [{
            'op': 'add', 'entry_id': 'detail', 'name': 'Detail', 'parent_entry_id': None,
            'anchor_reference_ids': ['doc_seed:B1'],
            'evidence_claim_ids': saved['concept']['evidence_claim_ids'],
        }]
    manifest = {'concept_pages': [{'slug': 'alpha', 'markdown': base}],
                'concept_contributions': {'alpha': saved}}
    with h._connect() as conn:
        conn.execute("UPDATE wiki_pages SET status='deleted' WHERE id='alpha-page'")
        h.output_persistence._persist_concept_pages(
            conn, 'doc_seed', manifest, {'concept_ledger': [saved['concept']]},
            'user-1', 'workspace-1', concept_id_by_slug={'alpha': 'alpha-page'},
            operation_id='seed-alpha',
        )
    base = manifest['concept_pages'][0]['markdown']
    assert saved['initial_markdown'] == base
    h.write_text_object(first['object_key'], json.dumps({**saved, 'page_id': 'alpha-page'}))
    base_uri = h.write_text_object('wiki/workspace-1/pages/alpha-page/ops/seed-alpha.md', base)
    second = h._stored_contribution(
        'alpha-page', 'alpha', 'second', 'doc_second', 'doc_second:B1',
    )
    second['sequence_revision'] = 2
    contribution = json.loads(h.read_text_object(second['object_key']))
    contribution['evidence_units'][0]['claim'] = 'Second document evidence.'
    h.write_text_object(second['object_key'], json.dumps(contribution))
    published = sanitize_ai_markdown(append_concept_evidence(base, concept_evidence_updates(contribution))) if with_append else base
    uri = h.write_text_object('wiki/workspace-1/pages/alpha-page/ops/second.md', published) if with_append else base_uri
    with h._connect() as conn:
        conn.execute("UPDATE wiki_pages SET markdown_uri=%s WHERE id='alpha-page'", (uri,))
        snapshot = h.calculation._page_snapshot(dict(conn.execute(
            "SELECT * FROM wiki_pages WHERE id='alpha-page'",
        ).fetchone()))
    monkeypatch.setattr(h.calculation, 'read_contributions', lambda *_: [first, second] if with_append else [first])
    source = h.Source([('B1', 'Third document evidence.')])
    grant = str(uuid4())
    payload, _ = h._lint_payload(grant, [
        {key: snapshot[key] for key in ('page_id', 'version', 'content_hash')},
    ], lambda snapshot_id: [{
        'operation_id': 'third', 'kind': 'UPSERT_STATEMENT',
        'target_page_id': 'alpha-page', 'depends_on': [],
        'arguments': {
            'statement_id': None, 'text': 'Third document evidence.',
            'evidence': [{'snapshot_id': snapshot_id, 'block_id': 'B1', 'quote': 'Third document evidence.'}],
        },
    }], source)
    preview = h._intake(source).lint(payload)
    with h._connect() as conn:
        draft = dict(conn.execute('SELECT * FROM mcp_wiki_drafts').fetchone())
        change = next(item for item in draft['changes'] if item['kind'] == 'PAGE_BODY')
        assert change['before_text'] == published
        expected = change['after_text']
        assert 'doc_seed:B1' in expected
        if with_append:
            assert 'Second document evidence. [doc_second:B1]' in expected
        assert 'Third document evidence.' in expected
        assert h.apply_repository_module._recalculate_lint(conn, draft)['preview'] == draft['preview']
        assert h.calculation._page_snapshot(dict(conn.execute(
            "SELECT * FROM wiki_pages WHERE id='alpha-page'",
        ).fetchone()))['body'] == published
    operation_id = str(uuid4())
    apply_payload = h._apply_payload(grant, preview, operation_id=operation_id)
    result = h.McpWikiDraftApply(h.PostgresMcpWikiApplyRepository(), h._ValidApprovalAuthority()).apply(
        apply_payload.draft_id, apply_payload,
    )
    assert result['state'] == 'PUBLISHED'
    with h._connect() as conn:
        assert h.calculation._page_snapshot(dict(conn.execute(
            "SELECT * FROM wiki_pages WHERE id='alpha-page'",
        ).fetchone()))['body'] == expected
        assert conn.execute(
            "SELECT count(*) AS n FROM wiki_pending_contributions WHERE operation_id=%s", (operation_id,),
        ).fetchone()['n'] == 1
