"""Direct-file contracts: original bytes, no parser fallback, honest evidence."""
import base64
import json
from io import BytesIO

import httpx
import pytest
from PIL import Image

from backend import extraction, model_document, pipeline
from backend.model_document import DocumentAnswer, extract_model_document, input_parts
from backend.openai_client import OpenAIError, OpenAIResponsesClient
from backend.outcome import FIELD_LABELS


def answer():
    proposal = {key: '' for key in FIELD_LABELS}
    proposal.update(supplier='Test Supplier', documentTotal=20, notes='Test Supplier',
        items=[{'name': 'Test item', 'quantity': 2, 'unit': 'pcs', 'unitPrice': 10,
                'lineTotal': 20, 'components': [], 'additionalFields': []}], additionalFields=[])
    return {'proposal': proposal, 'cells': [{'id': 'supplier', 'text': 'Test Supplier',
        'block': 'metadata', 'row': 1, 'cell': 1, 'page': 1, 'sheet': None, 'kind': 'text'}],
        'evidence': [{'field': 'supplier', 'sourceId': 'supplier', 'excerpt': 'Test Supplier'}],
        'complete': True, 'warnings': []}


class FakeClient:
    def __init__(self, value=None):
        self.value = value or answer()
        self.calls = []

    def generate(self, messages, schema, **kwargs):
        self.calls.append((messages, schema, kwargs))
        return json.dumps(self.value)


@pytest.mark.parametrize('suffix', ['pdf', 'docx', 'xlsx', 'xls', 'csv', 'tsv', 'txt', 'json'])
def test_original_document_bytes_reach_model_without_parsing(monkeypatch, suffix):
    def forbidden(*args, **kwargs):
        pytest.fail('Native parsing must never run')
    monkeypatch.setattr(extraction, 'read_document', forbidden)
    client = FakeClient()
    original = b'original binary document -- not pre-parsed'
    result = extract_model_document(original, f'input.{suffix}', client=client)
    part = client.calls[0][0][1]['content'][0]
    assert part['type'] == 'input_file'
    assert base64.b64decode(part['file_data'].split(',', 1)[1]) == original
    assert client.calls[0][2]['schema_name'] == 'commercial_proposal'
    assert result['proposal']['items'][0]['quantity'] == 2
    assert result['metadata']['fieldEvidence']['supplier']['verifiedInSource'] is False
    assert result['metadata']['verification']['coverageComplete'] is False
    assert result['metadata']['timingsMs']['parsing'] == 0


def test_production_pipeline_routes_to_direct_model(monkeypatch):
    monkeypatch.setattr(pipeline, 'extract_model_document', lambda content, name: (content, name))
    assert pipeline.process_document(b'original', 'input.xlsx') == (b'original', 'input.xlsx')


def test_provider_failure_is_not_replaced_with_rules():
    class Broken:
        def generate(self, *args, **kwargs):
            raise OpenAIError('Нет соединения')
    with pytest.raises(extraction.DocumentError, match='Нет соединения'):
        extract_model_document(b'original', 'input.pdf', client=Broken())


def test_invalid_or_truncated_answer_is_rejected():
    class Invalid:
        def generate(self, *args, **kwargs):
            return '{"proposal":'
    with pytest.raises(extraction.DocumentError, match='некорректные данные'):
        extract_model_document(b'original', 'input.pdf', client=Invalid())


def test_arithmetic_checks_never_fill_or_replace_model_numbers():
    value = answer()
    value['proposal']['items'][0]['lineTotal'] = 99
    value['proposal']['items'][0]['unitPrice'] = None
    value['proposal']['documentTotal'] = 20
    result = extract_model_document(b'original', 'input.pdf', client=FakeClient(value))
    assert result['proposal']['items'][0]['unitPrice'] is None
    assert result['proposal']['items'][0]['lineTotal'] == 99
    assert any('Итог документа' in text for text in result['metadata']['warnings'])
    assert not result['metadata']['verification']['reviewCompleted']


def test_unknown_or_duplicate_source_ids_are_not_certified():
    value = answer()
    value['evidence'][0]['sourceId'] = 'invented'
    result = extract_model_document(b'original', 'input.pdf', client=FakeClient(value))
    assert result['metadata']['fieldEvidence'] == {}
    assert any('ссылок модели' in text for text in result['metadata']['warnings'])
    value['cells'].append(value['cells'][0])
    with pytest.raises(extraction.DocumentError, match='повторяющиеся'):
        extract_model_document(b'original', 'input.pdf', client=FakeClient(value))


def test_multi_frame_images_are_all_sent_without_ocr():
    output = BytesIO()
    Image.new('RGB', (10, 10), 'white').save(output, format='TIFF', save_all=True,
        append_images=[Image.new('RGB', (10, 10), 'black')])
    parts = input_parts(output.getvalue(), 'scan.tiff')
    assert len(parts) == 2
    assert all(part['type'] == 'input_image' for part in parts)


def test_spreadsheet_provider_limit_and_word_images_are_visible():
    sheet = extract_model_document(b'original', 'input.xlsx', client=FakeClient())
    assert any('1000' in text for text in sheet['metadata']['warnings'])
    word = extract_model_document(b'original', 'input.docx', client=FakeClient())
    assert any('встроенные изображения' in text for text in word['metadata']['warnings'])


def test_direct_file_transport_uses_strict_schema_and_records_usage(monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'offline-test-key')
    def handler(request):
        payload = json.loads(request.content)
        assert payload['store'] is False
        assert payload['input'][1]['content'][0]['type'] == 'input_file'
        assert payload['text']['format']['strict'] is True
        assert payload['text']['format']['name'] == 'commercial_proposal'
        return httpx.Response(200, json={'status': 'completed',
            'usage': {'input_tokens': 100, 'output_tokens': 50},
            'output': [{'type': 'message', 'role': 'assistant',
                'content': [{'type': 'output_text', 'text': json.dumps(answer())}]}]})
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        result = extract_model_document(b'original', 'input.pdf',
            client=OpenAIResponsesClient(http))
    assert result['metadata']['apiUsage']['inputTokens'] == 100
    assert result['metadata']['apiUsage']['outputTokens'] == 50


def test_annotation_upload_uses_model_transcription_not_native_parsers(monkeypatch):
    from backend import annotation_data
    monkeypatch.setattr(model_document, 'openai_client', FakeClient())
    assert not hasattr(annotation_data, 'read_document')
    result = annotation_data.prepare_annotation(b'original', 'input.docx')
    assert result['sourceIndependentlyVerified'] is False
    assert result['cells'][0]['method'] == 'model-document'
    assert result['annotation']['fields'][4]['state'] == 'pending'
    assert result['suggestion']['supplier'] == 'Test Supplier'


def test_http_upload_and_save_preserve_model_values(monkeypatch):
    import sqlite3
    from uuid import uuid4
    from fastapi.testclient import TestClient
    from backend import main

    connection = sqlite3.connect(':memory:', check_same_thread=False)
    connection.row_factory = sqlite3.Row
    monkeypatch.setattr(main, 'connect_db', lambda: connection)
    monkeypatch.setattr(main, 'APP_PASSWORD', '')
    monkeypatch.setattr(main, 'model_available', lambda: True)
    main.initialize_db()
    async def run(content, filename):
        return extract_model_document(content, filename, client=FakeClient())
    monkeypatch.setattr(main.pipeline, 'run', run)
    try:
        with TestClient(main.app) as http:
            result = http.post('/api/extract', files={'file': ('offer.xlsx', b'original')})
            assert result.status_code == 200
            body = result.json()
            assert body['metadata']['verification']['mode'] == 'model_direct'
            payload = {'proposal': body['proposal'], 'sources': [body['metadata']]}
            headers = {'Idempotency-Key': str(uuid4())}
            saved = http.post('/api/proposals', json=payload, headers=headers)
            assert saved.status_code == 200
            assert http.post('/api/proposals', json=payload, headers=headers).json()['duplicate']
            restored = http.get('/api/proposals/' + saved.json()['id']).json()
            assert restored['payload']['proposal'] == body['proposal']
            health = http.get('/api/health').json()
            assert health['localParsers'] is False and health['ruleFallback'] is False
    finally:
        connection.close()


def test_http_model_error_is_explicit_and_returns_no_fallback(monkeypatch):
    from fastapi.testclient import TestClient
    from backend import main
    monkeypatch.setattr(main, 'APP_PASSWORD', '')
    monkeypatch.setattr(main, 'model_available', lambda: True)
    async def run(content, filename):
        raise extraction.DocumentError('Нет соединения с сервисом')
    monkeypatch.setattr(main.pipeline, 'run', run)
    with TestClient(main.app) as http:
        response = http.post('/api/extract', files={'file': ('offer.docx', b'original')})
        assert response.status_code == 422
        assert 'proposal' not in response.json()


def test_direct_cache_does_not_promote_unfinished_results():
    result = extract_model_document(b'original', 'input.pdf', client=FakeClient())
    assert pipeline.Pipeline._cacheable(result) is False
    result['metadata']['cacheEligible'] = True
    assert pipeline.Pipeline._cacheable(result) is True
