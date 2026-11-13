import pymupdf
import pytest
from pydantic import ValidationError
from app.modules.document_restoration.interfaces.cost_preflight import PdfSource, pdf_features


def test_pdf_features_counts_all_pages_and_utf8_text(tmp_path):
    path = tmp_path / 'test.pdf'
    with pymupdf.open() as pdf:
        page = pdf.new_page()
        page.insert_text((72, 72), 'A document with enough readable text for cost estimation.')
        pdf.new_page()
        pdf.save(path)
    result = pdf_features(str(path))
    assert result['pages'] == 2
    assert result['scanned_pages'] == 1
    assert result['text_bytes'] > 40
    assert result['images'] == 0


def test_pdf_source_rejects_arbitrary_storage_keys():
    with pytest.raises(ValidationError):
        PdfSource(source_key='../private/file')
    assert PdfSource(source_key='sources/documents/doc_123/original')


def test_invalid_pdf_is_rejected(tmp_path):
    path = tmp_path / 'bad.pdf'
    path.write_bytes(b'not a pdf')
    with pytest.raises(pymupdf.FileDataError):
        pdf_features(str(path))


def test_internal_endpoint_authenticates_before_reading_storage(monkeypatch):
    import api
    from fastapi.testclient import TestClient
    from unittest.mock import Mock
    from app.modules.document_restoration.interfaces import cost_preflight
    monkeypatch.setenv('INTERNAL_CALLBACK_TOKEN', 'cost-test-token')
    storage = Mock()
    monkeypatch.setattr(cost_preflight, 'client', storage)
    http = TestClient(api.app)
    body = {'source_key': 'sources/documents/doc_123/original'}
    assert http.post('/internal/documents/pdf-cost-features', json=body).status_code == 401
    storage.assert_not_called()
    monkeypatch.setattr(cost_preflight, 'pdf_features', lambda path: dict(pages=1, text_bytes=100, images=0, scanned_pages=0))
    response = http.post('/internal/documents/pdf-cost-features', json=body, headers={'X-Internal-Token': 'cost-test-token'})
    assert response.status_code == 200
    assert response.json()['pages'] == 1
    storage.return_value.fget_object.assert_called_once()
