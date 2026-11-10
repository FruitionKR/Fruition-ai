"""Selected-target lint reuses production moves and the real contribution replay."""
from array import array
from copy import deepcopy

import pytest

from app.core.jev_client import JevUnavailable
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
