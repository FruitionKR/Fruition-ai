import sys

import pymupdf
import pytest
from pydantic import ValidationError
from unittest.mock import Mock

from app.modules.document_restoration.infrastructure.pdf_cost_features import pdf_features
from app.modules.document_restoration.interfaces import cost_preflight
from app.modules.document_restoration.interfaces.cost_preflight import PdfSource, UnreadablePdf, inspect_file


def make_pdf(path, texts):
    with pymupdf.open() as pdf:
        for text in texts:
            page = pdf.new_page()
            if text:
                page.insert_text((72, 72), text)
        pdf.save(path)
    return str(path)


TEXT = 'A document with enough readable text for cost estimation.'


def test_isolated_parser_counts_all_pages_and_utf8_text(tmp_path):
    result = inspect_file(make_pdf(tmp_path / 'test.pdf', [TEXT, '']))
    assert result['pages'] == 2
    assert result['scanned_pages'] == 1
    assert result['text_bytes'] > 40
    assert result['images'] == 0


def test_parser_reads_bounded_pages_and_extrapolates(tmp_path):
    path = make_pdf(tmp_path / 'long.pdf', [TEXT, TEXT, '', TEXT])
    full = pdf_features(path, 10)
    sampled = pdf_features(path, 2)
    assert sampled['pages'] == 4
    assert sampled['text_bytes'] == full['text_bytes'] // 3 * 4  # 앞 2쪽(텍스트 2쪽)을 4쪽으로 외삽
    assert sampled['scanned_pages'] == 0


def test_pdf_source_rejects_arbitrary_storage_keys():
    with pytest.raises(ValidationError):
        PdfSource(source_key='../private/file')
    assert PdfSource(source_key='sources/documents/doc_123/original')


def test_invalid_and_encrypted_pdfs_are_unreadable(tmp_path):
    bad = tmp_path / 'bad.pdf'
    bad.write_bytes(b'not a pdf')
    with pytest.raises(UnreadablePdf):
        inspect_file(str(bad))
    encrypted = tmp_path / 'encrypted.pdf'
    with pymupdf.open() as pdf:
        pdf.new_page().insert_text((72, 72), TEXT)
        pdf.save(encrypted, encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw='secret', owner_pw='owner')
    with pytest.raises(UnreadablePdf):
        inspect_file(str(encrypted))


@pytest.mark.parametrize('script', ['import os; os.abort()', 'import time; time.sleep(5)'])
def test_parser_crash_or_timeout_does_not_take_down_the_api(monkeypatch, script):
    monkeypatch.setattr(cost_preflight, 'TIMEOUT_SECONDS', 1)
    monkeypatch.setattr(cost_preflight, 'parser_command', lambda path: [sys.executable, '-c', script])
    with pytest.raises(UnreadablePdf):
        inspect_file('unused.pdf')


def endpoint(monkeypatch, size=1000):
    import api
    from fastapi.testclient import TestClient
    monkeypatch.setenv('INTERNAL_CALLBACK_TOKEN', 'cost-test-token')
    storage = Mock()
    storage.return_value.stat_object.return_value.size = size
    monkeypatch.setattr(cost_preflight, 'client', storage)
    return TestClient(api.app), storage


BODY = {'source_key': 'sources/documents/doc_123/original'}
HEADERS = {'X-Internal-Token': 'cost-test-token'}


def test_internal_endpoint_authenticates_before_reading_storage(monkeypatch):
    http, storage = endpoint(monkeypatch)
    assert http.post('/internal/documents/pdf-cost-features', json=BODY).status_code == 401
    storage.assert_not_called()
    monkeypatch.setattr(cost_preflight, 'inspect_file', lambda path: dict(pages=1, text_bytes=100, images=0, scanned_pages=0))
    response = http.post('/internal/documents/pdf-cost-features', json=BODY, headers=HEADERS)
    assert response.status_code == 200
    assert response.json()['pages'] == 1
    storage.return_value.fget_object.assert_called_once()


def test_unreadable_pdf_is_422_and_oversized_pdf_is_never_downloaded(monkeypatch):
    http, storage = endpoint(monkeypatch)

    def unreadable(path):
        raise UnreadablePdf
    monkeypatch.setattr(cost_preflight, 'inspect_file', unreadable)
    assert http.post('/internal/documents/pdf-cost-features', json=BODY, headers=HEADERS).status_code == 422
    http, storage = endpoint(monkeypatch, size=cost_preflight.MAX_BYTES + 1)
    assert http.post('/internal/documents/pdf-cost-features', json=BODY, headers=HEADERS).status_code == 413
    storage.return_value.fget_object.assert_not_called()


def test_busy_parser_slots_return_503_without_downloading(monkeypatch):
    http, storage = endpoint(monkeypatch)
    slots = Mock()
    slots.acquire.return_value = False
    monkeypatch.setattr(cost_preflight, '_slots', slots)
    assert http.post('/internal/documents/pdf-cost-features', json=BODY, headers=HEADERS).status_code == 503
    storage.assert_not_called()
    slots.release.assert_not_called()
