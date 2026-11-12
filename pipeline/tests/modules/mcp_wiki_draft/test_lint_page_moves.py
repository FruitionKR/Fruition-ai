"""Selected-target lint reuses production moves and the real contribution replay."""
from array import array
from copy import deepcopy

import pytest

from app.core.jev_client import JevUnavailable
from app.core.ai_markdown_sanitizer import external_urls, sanitize_ai_markdown
from app.modules.wiki_ingestion.domain.concept_entries import render_entry_index
from app.modules.mcp_wiki_draft import lint_calculation as lint
from app.modules.wiki_generation.infrastructure import jev_concept_resolver as jev
from app.modules.wiki_ingestion.infrastructure import postgres_wiki_ingestion_repository as production
from app.modules.wiki_ingestion.domain.operation_recovery import PageContribution


class Judge:
    def __init__(self, error=None):
        self.calls = []
        self.error = error

    def judge_part_of(self, pairs):
        self.calls.append(pairs)
        if self.error:
            raise self.error
        return {(head['slug'], detail['slug']): {'part_of': True, 'probability': .99}
                for head, detail in pairs if (head['slug'], detail['slug']) == ('parent', 'child')}


class Embedding:
    model_name = 'fake'


@pytest.fixture
def world(monkeypatch):
    judge = Judge()
    monkeypatch.setattr(jev, 'jev_concept_judge', lambda fallback: judge)
    monkeypatch.setattr(jev, 'embedding_server_model', lambda: Embedding())
    monkeypatch.setattr(production, '_stored_concept_vectors', lambda *_, **__: {})
    monkeypatch.setattr(production, 'cached_embeddings', lambda _, texts: {text: array('f', [1., 0.]) for text in texts})
    rows, pages, loaded = {}, {}, {}
    for name in ('parent', 'child', 'referrer'):
        row = {'id': name, 'slug': name, 'title': name.title(), 'summary': name + ' definition'}
        contribution = {
            'schema_version': 1, 'operation_id': 'seed-' + name, 'document_id': 'doc-' + name,
            'page_id': name,
            'concept': {'slug': name, 'title': name.title(), 'definition': row['summary'],
                        'anchor_reference_ids': ['B1'], 'evidence_claim_ids': ['claim']},
            'evidence_units': [{'evidence_id': 'claim', 'claim': name + ' evidence',
                                'anchor_reference_ids': ['B1']}],
            'links': [],
        }
        if name == 'child':
            contribution['entry_changes'] = [{'op': 'add', 'entry_id': 'detail', 'name': 'Detail',
                'parent_entry_id': None, 'anchor_reference_ids': ['B1'], 'evidence_claim_ids': ['claim']}]
        if name == 'referrer':
            contribution['links'] = [{'source': 'concept:referrer', 'target': 'concept:child',
                'relation': 'entry_reference', 'entry_id': 'detail', 'name': 'Detail', 'parent_entry_id': None}]
        loaded[name] = [PageContribution(name, 'concept', contribution['operation_id'], 1, '', contribution)]
        rebuilt = lint.rebuild_concept_page(loaded[name])
        pages[name] = lint._entry_page(row, loaded[name], rebuilt)
        rows[name] = row
    return judge, rows, pages, loaded


def test_freeze_moves_references_and_replay_without_second_judgment(world):
    judge, rows, pages, loaded = world
    effects = lint._freeze_page_moves(None, 'workspace', pages, rows, {}, jev_allowed=True)
    assert len(judge.calls) == 1
    assert effects['deleted_page_ids'] == ['child']
    assert effects['page_changes']['referrer']['added_links'][0]['target'] == 'concept:parent'
    frozen = deepcopy(effects)
    for name in rows:
        change = lint._change_record(rows[name])
        lint._merge_change(change, effects['page_changes'].get(name, {}))
        first, contribution = lint._replay_page(name, 'draft', loaded[name], change, effects['linked'])
        second, _ = lint._replay_page(name, 'draft', loaded[name], change, effects['linked'])
        assert first == second
        if name == 'child':
            assert first.deleted
        elif name == 'parent':
            assert any(entry['name'] == 'Child' for entry in first.entries)
            assert any(entry['entry_id'] == 'detail' for entry in first.entries)
        else:
            assert first.supported_links[0]['target'] == 'concept:parent'
        # Removing this lint and its linked copies restores the original contribution view.
        assert lint.rebuild_concept_page(loaded[name]).deleted is False
    assert effects == frozen
    assert len(judge.calls) == 1


def test_heads_and_effects_are_selected_targets_only(world):
    judge, rows, pages, _ = world
    rows.pop('parent')
    pages.pop('parent')
    effects = lint._freeze_page_moves(None, 'workspace', pages, rows, {}, jev_allowed=True)
    assert not effects['deleted_page_ids']
    assert all(head['slug'] != 'parent' for call in judge.calls for head, _ in call)


@pytest.mark.parametrize('unavailable', ['disabled', 'embedding_disabled', 'embedding_failed', 'jev_failed'])
def test_unavailable_has_no_inferred_effects_or_llm(world, monkeypatch, unavailable):
    judge, rows, pages, _ = world
    if unavailable == 'disabled':
        monkeypatch.setattr(jev, 'jev_concept_judge', lambda _: None)
    elif unavailable == 'embedding_disabled':
        monkeypatch.setattr(jev, 'embedding_server_model', lambda: None)
    elif unavailable == 'embedding_failed':
        def fail(*_):
            raise RuntimeError('offline')
        monkeypatch.setattr(production, 'cached_embeddings', fail)
    else:
        judge.error = JevUnavailable('offline')
    effects = lint._freeze_page_moves(None, 'workspace', pages, rows, {}, jev_allowed=True)
    assert not effects.get('page_changes') and not effects.get('linked')


def test_busy_target_is_not_moved_but_its_inbound_reference_is_rewritten(world):
    _, rows, pages, _ = world
    effects = lint._freeze_page_moves(None, 'workspace', pages, rows, {'referrer': {}}, jev_allowed=True)
    assert effects['deleted_page_ids'] == ['child']
    assert effects['page_changes']['referrer']['added_links'][0]['target'] == 'concept:parent'
    effects = lint._freeze_page_moves(None, 'workspace', pages, rows, {'parent': {}}, jev_allowed=True)
    assert not effects['deleted_page_ids']


def test_selected_planner_passes_page_and_entry_bounds(world, monkeypatch):
    _, rows, pages, _ = world
    seen = []
    def vectors(*args, **kwargs):
        seen.append(kwargs)
        return {}
    monkeypatch.setattr(production, '_stored_concept_vectors', vectors)
    lint._freeze_page_moves(None, 'workspace', pages, rows, {}, jev_allowed=True)
    assert set(seen[0]['page_ids']) == set(pages)
    assert set(seen[0]['entry_ids']) <= {entry['entry_id'] for page in pages.values() for entry in page.entries}


@pytest.mark.parametrize('relation', ['source_mentions_concept', 'concept_related_to'])
def test_lint_source_link_does_not_require_source_target(world, monkeypatch, relation):
    from types import SimpleNamespace

    _, rows, _, loaded = world
    row = {**rows['parent'], 'page_type': 'concept', 'status': 'active'}
    snapshot = {
        'page_id': 'parent', 'page_type': 'concept', 'slug': 'parent',
        'status': 'active', 'version': 1, 'content_hash': 'hash',
        'markdown_uri': 'baseline/parent.md',
        'body': lint.rebuild_concept_page(loaded['parent']).markdown,
    }
    link = {'from_page_id': 'source', 'to_page_id': 'parent',
            'link_type': relation, 'label': None, 'confidence': None}
    structures = {'entries': [], 'links': [link], 'references': [],
                  'manual_links': [], 'document_links': []}
    monkeypatch.setattr(lint, '_page_snapshot', lambda _: snapshot)
    monkeypatch.setattr(lint, '_structural_rows', lambda *args: structures)
    monkeypatch.setattr(lint, '_load_reconciled', lambda *args: [{
        'page_id': 'parent', 'active': True, 'sequence_revision': 1,
        'ingest_operation_id': 'seed-parent',
    }])
    monkeypatch.setattr(lint, 'load_concept_contributions', lambda **kwargs: loaded['parent'])
    conn = SimpleNamespace(execute=lambda sql, params: SimpleNamespace(
        fetchall=lambda: [row] if sql.startswith('SELECT * FROM wiki_pages') else []))
    kwargs = dict(
        draft_id='lint-source-link', workspace_id='workspace',
        target_snapshot=[snapshot],
        source_snapshots=[{'snapshot_id': 'source-snapshot', 'document_id': 'doc-source',
                           'source_version': 1, 'source_content_hash': 'source-hash'}],
        operations=[{'operation_id': 'add', 'kind': 'UPSERT_STATEMENT',
                     'target_page_id': 'parent', 'depends_on': [], 'arguments': {
                         'statement_id': None, 'text': 'Additional parent evidence.',
                         'evidence': [{'snapshot_id': 'source-snapshot', 'block_id': 'B1',
                                       'quote': 'Additional parent evidence.'}],
                     }}],
        policy_version='test', max_structural_changes=100, max_deletes=0,
    )
    if relation != 'source_mentions_concept':
        with pytest.raises(lint.PreviewCalculationError, match='omits a linked identity'):
            lint.calculate_lint_preview(conn, **kwargs)
        return
    result = lint.calculate_lint_preview(conn, **kwargs)
    assert any(change['kind'] == 'PAGE_BODY' for change in result['preview']['changes'])
    assert not any(change['kind'] == 'LINK' for change in result['preview']['changes'])
    assert result['calculation_inputs']['public_state']['links'] == [link]
    assert result['publication_plan']['link_changes'] == {'removed_links': [], 'restored_links': []}
    before = result['preview']['public_state_hash']
    structures['links'] = []
    assert lint.calculate_lint_preview(conn, **kwargs)['preview']['public_state_hash'] != before


@pytest.mark.parametrize('publication', ['exact', 'different', 'missing', 'missing_base', 'changed_base', 'missing_history', 'untracked_text', 'changed_related', 'invalidation'])
@pytest.mark.parametrize('related_concepts', [False, True])
@pytest.mark.parametrize('history', ['ingest', 'entries', 'recorded_llm', 'lint', 'invalidation'])
def test_lint_accepts_append_publication_only_with_immutable_evidence(world, monkeypatch, publication, related_concepts, history):
    from types import SimpleNamespace

    from app.modules.wiki_ingestion.infrastructure.concept_evidence import (
        append_concept_evidence, concept_evidence_updates,
    )

    _, rows, _, loaded = world
    row = {**rows['parent'], 'page_type': 'concept', 'status': 'active'}
    first = loaded['parent'][0]
    first = PageContribution('parent', 'concept', first.operation_id, 1, 'first.md', first.contribution)
    baseline = lint.rebuild_concept_page([first]).markdown
    if related_concepts:
        from app.modules.wiki_generation.infrastructure.assemble import ConceptPageAssembler, LinkBuilder
        from app.modules.wiki_ingestion.application.concept_contribution import build_concept_contributions

        normalized = {
            'document': {'document_id': 'doc-parent'},
            'concept_ledger': [first.contribution['concept'], loaded['referrer'][0].contribution['concept']],
            'evidence_units': [{**first.contribution['evidence_units'][0],
                                'related_concept_slugs': ['parent', 'referrer']}],
        }
        baseline = ConceptPageAssembler().build_top(normalized)[0]['markdown']
        contribution = build_concept_contributions(
            operation_id=first.operation_id, normalized=normalized,
            source_blocks=[], links=LinkBuilder().build(normalized),
        )['parent']
        first = PageContribution('parent', 'concept', first.operation_id, 1, 'first.md', contribution)
        assert '[[referrer|Referrer]]' in baseline
        assert baseline != lint.rebuild_concept_page([first]).markdown
    if history == 'entries':
        first.contribution['entry_changes'] = [{
            'op': 'add', 'entry_id': 'detail', 'name': 'Detail', 'parent_entry_id': None,
            'anchor_reference_ids': ['B1'], 'evidence_claim_ids': ['claim'],
        }]
        initial_state = lint.rebuild_concept_page([first])
        baseline = render_entry_index(baseline, list(initial_state.entries), list(initial_state.supported_links))
    if history == 'recorded_llm':
        from app.modules.wiki_generation.infrastructure.generated_concept_page_assembler import GeneratedConceptPageAssembler

        baseline = GeneratedConceptPageAssembler().build_pages([{
            'slug': 'parent', 'title': 'Parent', 'definition': {'text': 'Generated explanation'},
            'evidence': [{'text': 'parent evidence', 'anchor_reference_ids': ['B1']}],
            'source_document_ids': ['doc-parent'],
        }])[0]['markdown']
        first.contribution['initial_markdown'] = baseline
    contributions = [first]
    checkpoint_body = None
    if history in ('lint', 'invalidation'):
        checkpoint = lint.lint_contribution('checkpoint', {
            'page_id': 'parent', 'slug': 'parent', 'title': 'Parent',
            'content_action': 'append_evidence',
            'claims': [{'id': 'checkpoint-claim', 'text': 'Checkpoint evidence', 'refs': ['doc-checkpoint:B1']}],
        })
        if history == 'invalidation':
            # Reingest invalidates the old block while adding replacement evidence.
            checkpoint['artifact_type'] = 'ingest'
            checkpoint['invalidated_reference_ids'] = ['doc-parent:B1']
        contributions.append(PageContribution('parent', 'concept', 'checkpoint', 2, 'checkpoint.md', checkpoint))
        checkpoint_body = lint.rebuild_concept_page(contributions).markdown
    second = deepcopy(first.contribution)
    second.pop('initial_markdown', None)
    second.update(operation_id='second', document_id='doc-second')
    second['evidence_units'][0]['claim'] = 'Additional parent evidence.'
    second['concept']['definition'] = ''
    key = 'wiki/workspace/pages/parent/ops/second.md'
    contributions.append(PageContribution('parent', 'concept', 'second', len(contributions) + 1, key, second))
    published = sanitize_ai_markdown(
        append_concept_evidence(checkpoint_body or baseline, concept_evidence_updates(second)),
        keep_urls=external_urls(checkpoint_body or baseline),
    )
    state = lint.rebuild_concept_page(contributions)
    published = render_entry_index(published, list(state.entries), list(state.supported_links))
    if publication == 'missing_history':
        contributions = contributions[1:]
    if publication == 'untracked_text':
        published += '\nUntracked content.\n'
    if publication == 'changed_related':
        published = published.replace('## Related Concepts\n', '## Related Concepts\n- [[untracked|Untracked]]\n')
    if publication == 'invalidation':
        second['invalidated_reference_ids'] = ['doc-parent:B1']
    snapshot = {
        'page_id': 'parent', 'version': 1, 'content_hash': lint._text_hash(published),
        'page_type': 'concept', 'slug': 'parent', 'status': 'active',
        'body': published, 'markdown_uri': key,
    }
    monkeypatch.setattr(lint, '_page_snapshot', lambda _: snapshot)
    monkeypatch.setattr(lint, '_structural_rows', lambda *args: {
        'entries': [], 'links': [], 'references': [], 'manual_links': [], 'document_links': [],
    })
    monkeypatch.setattr(lint, '_load_reconciled', lambda *args: [
        {'page_id': 'parent', 'active': True, 'sequence_revision': item.sequence,
         'ingest_operation_id': item.operation_id} for item in contributions
    ])
    monkeypatch.setattr(lint, 'load_concept_contributions', lambda **kwargs: contributions)
    reads = []

    def read_markdown(path):
        reads.append(path)
        if path == 'checkpoint.md':
            if publication == 'missing_base':
                raise OSError('unavailable')
            return checkpoint_body + '\nUntracked initial content.\n' if publication == 'changed_base' else checkpoint_body
        if path == 'first.md':
            if publication == 'missing_base':
                raise OSError('unavailable')
            return baseline + '\nUntracked initial content.\n' if publication == 'changed_base' else baseline
        assert path == key
        if publication == 'missing':
            raise OSError('unavailable')
        return 'unrelated publication' if publication == 'different' else published

    monkeypatch.setattr(lint, 'read_text_object', read_markdown)
    conn = SimpleNamespace(execute=lambda sql, params: SimpleNamespace(
        fetchall=lambda: [row] if sql.startswith('SELECT * FROM wiki_pages') else []))
    kwargs = dict(
        draft_id='lint-append', workspace_id='workspace', target_snapshot=[snapshot],
        source_snapshots=[{'snapshot_id': 'snapshot', 'document_id': 'doc-third',
                           'source_version': 1, 'source_content_hash': 'source-hash'}],
        operations=[{'operation_id': 'add', 'kind': 'UPSERT_STATEMENT',
                     'target_page_id': 'parent', 'depends_on': [], 'arguments': {
                         'statement_id': None, 'text': 'Third evidence.',
                         'evidence': [{'snapshot_id': 'snapshot', 'block_id': 'B1',
                                       'quote': 'Third evidence.'}],
                     }}],
        policy_version='test', max_structural_changes=100, max_deletes=0,
    )
    if publication != 'exact':
        with pytest.raises(lint.PreviewCalculationError, match='contribution'):
            lint.calculate_lint_preview(conn, **kwargs)
    else:
        result = lint.calculate_lint_preview(conn, **kwargs)
        change = next(item for item in result['preview']['changes'] if item['kind'] == 'PAGE_BODY')
        assert change['before_text'] == published
        assert 'Additional parent evidence.' in change['after_text']
        assert 'Third evidence.' in change['after_text']
        # Approval replay must reproduce the complete preview, including normalization of the old body.
        assert lint.calculate_lint_preview(conn, **kwargs)['preview'] == result['preview']
