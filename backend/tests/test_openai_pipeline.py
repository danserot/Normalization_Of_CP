"""Runtime contracts for the API-only workflow, using synthetic provider output."""
import asyncio
from io import BytesIO
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from backend import main
from backend.pipeline import PipelinePool, process_document
from backend.vision import OpenAIDocumentVisionService


def picture(format='PNG', frames=1):
    stream = BytesIO()
    image = Image.new('RGB', (80, 120), 'white')
    image.save(stream, format=format, save_all=frames > 1,
               append_images=[image.copy() for _ in range(frames - 1)])
    return stream.getvalue()


def test_missing_key_is_explicit_without_paid_health_requests(monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', '')
    monkeypatch.setattr(main, 'APP_PASSWORD', '')
    with TestClient(main.app) as client:
        assert client.get('/api/health').json()['configured'] is False
        assert client.get('/api/models').json()['models'][0]['id'] == 'openai'
        result = client.post('/api/extract', files={'file': ('scan.png', picture())})
        assert result.status_code == 503
        assert 'OPENAI_API_KEY' in result.json()['detail']


def test_transcribed_image_goes_through_semantic_api_with_evidence(monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'synthetic-key')
    calls = []

    class Provider:
        def generate(self, messages, schema, **options):
            calls.append(options['schema_name'])
            image = messages[1]['content'][1]
            assert image['detail'] == 'high'
            assert image['image_url'].startswith('data:image/png;base64,')
            page = schema['properties']['page']['enum'][0]
            return json.dumps({'page': page, 'complete': True, 'blank': False,
                'warnings': [], 'blocks': [{'type': 'text', 'text': 'Клиент: Альфа', 'html': ''}]})

    monkeypatch.setattr('backend.vision.create_vision_service',
                        lambda: OpenAIDocumentVisionService(client=Provider()))

    def semantic(messages, schema, **options):
        calls.append('document_plan')
        payload = json.loads(messages[1]['content'])
        cell_id = payload['metadataBlocks'][0]['rows'][0]['cells'][0][0]
        return json.dumps({'fields': [{'field': 'client', 'cell': cell_id, 'value': 'Альфа'}],
                           'tables': [], 'issues': []})

    monkeypatch.setattr('backend.universal.semantic_model.generate', semantic)
    result = process_document(picture(), 'proposal.png')
    assert calls == ['document_page', 'document_plan']
    assert result['proposal']['client'] == 'Альфа'
    assert result['proposal']['supplier'] == ''
    assert result['proposal']['documentTotal'] is None
    assert result['metadata']['verification']['visionComplete']
    absent = {entry['field']: entry['reason'] for entry in result['metadata']['outcome']['unavailable']}
    assert absent['supplier'] == 'absent'
    evidence = result['metadata']['fieldEvidence']['client']
    assert evidence['excerpt'] == 'Клиент: Альфа'
    assert evidence['source_method'] == 'openai-vision'
    assert evidence['bbox'] is None


def test_multiframe_tiff_preserves_all_pages(monkeypatch):
    from backend.extraction import read_document
    monkeypatch.setenv('OPENAI_API_KEY', 'synthetic-key')

    class Provider:
        def generate(self, messages, schema, **options):
            page = schema['properties']['page']['enum'][0]
            return json.dumps({'page': page, 'complete': True, 'blank': False, 'warnings': [],
                               'blocks': [{'type': 'text', 'text': f'Page {page}', 'html': ''}]})

    monkeypatch.setattr('backend.vision.create_vision_service',
                        lambda: OpenAIDocumentVisionService(client=Provider()))
    source = read_document(picture('TIFF', 2), 'scan.tiff')
    assert source.pages == 2
    assert source.routing['processed_pages'] == [1, 2]
    assert [cell.text for cell in source.cells] == ['Page 1', 'Page 2']


def test_unreadable_source_is_not_reported_absent(monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', '')
    result = process_document(picture(), 'scan.png')
    outcome = result['metadata']['outcome']
    assert outcome['state'] == 'unavailable'
    assert outcome['unavailable'][0]['reason'] == 'unreadable'
    assert not PipelinePool._cacheable(result)


def test_failed_semantic_review_cannot_be_complete_even_with_all_values():
    from backend.outcome import FIELD_LABELS, extraction_outcome
    proposal = {key: 'source value' for key in FIELD_LABELS}
    proposal['items'] = [{'name': 'Service', 'components': [{'lineTotal': 100}]}]
    result = extraction_outcome(proposal, {'reviewCompleted': False, 'coverageComplete': True})
    assert result['state'] == 'partial'
    assert result['unavailable'][0]['reason'] == 'unverified'


def test_unreadable_marker_is_not_a_business_value():
    from backend.canonical import Source
    from backend.rules import extract_rules
    from backend.universal import Plan, Quote, apply_plan
    source = Source('scan.png')
    cell = source.add('Поставщик: [неразборчиво]', 'p1', 1, method='openai-vision')
    assert extract_rules(source)[0]['supplier'] == ''
    plan = Plan(fields=[Quote(field='supplier', cell=cell.id, value='[неразборчиво]')],
                tables=[], issues=[])
    assert apply_plan(source, plan)[0]['supplier'] == ''


def test_http_upload_uses_worker_and_both_openai_stages(monkeypatch, tmp_path):
    """The HTTP route, subprocess, transport and evidence execute together."""
    calls = []

    class ProviderHandler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            assert self.path == '/v1/responses'
            assert data['store'] is False
            kind = data['text']['format']['name']
            calls.append(kind)
            if kind == 'document_page':
                value = {'page': 1, 'complete': True, 'blank': False, 'warnings': [],
                         'blocks': [{'type': 'text', 'text': 'Клиент: Альфа', 'html': ''}]}
            else:
                source = json.loads(data['input'][1]['content'])
                cell = source['metadataBlocks'][0]['rows'][0]['cells'][0][0]
                value = {'fields': [{'field': 'client', 'cell': cell, 'value': 'Альфа'}],
                         'tables': [], 'issues': []}
            payload = json.dumps({'status': 'completed', 'output': [{'type': 'message',
                'role': 'assistant', 'content': [{'type': 'output_text',
                                               'text': json.dumps(value)}]}]}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(('127.0.0.1', 0), ProviderHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv('OPENAI_API_KEY', 'local-fixture-key')
    monkeypatch.setenv('OPENAI_API_URL', f'http://127.0.0.1:{server.server_port}/v1/responses')
    monkeypatch.setattr(main, 'APP_PASSWORD', '')
    monkeypatch.setattr(main, 'DATABASE_PATH', tmp_path / 'http.sqlite3')
    main.initialize_db()
    try:
        with TestClient(main.app) as client:
            response = client.post('/api/extract', data={'models': '["openai"]'},
                                   files={'file': ('scan.png', picture(), 'image/png')})
            assert response.status_code == 200, response.text
            result = response.json()
            assert result['proposal']['client'] == 'Альфа'
            assert calls == ['document_page', 'document_plan']
            assert result['metadata']['verification']['visionComplete']
            assert result['metadata']['fieldEvidence']['client']['excerpt'] == 'Клиент: Альфа'
            repeat = client.post('/api/extract', files={'file': ('scan.png', picture())})
            assert repeat.json()['metadata']['cacheHit']
            assert len(calls) == 2
            saved = client.post('/api/proposals', json={'proposal': result['proposal'],
                                                       'sources': [result['metadata']]})
            assert saved.status_code == 200
            assert client.get('/api/proposals/' + saved.json()['id']).json()['payload']['proposal']['client'] == 'Альфа'
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_pool_parallelism_duplicate_coalescing_and_shared_cache(monkeypatch):
    monkeypatch.setenv('EXTRACTION_WORKERS', '2')
    instance = PipelinePool()
    active, maximum, calls = 0, 0, 0
    async def run(content, filename, mode='extract'):
        nonlocal active, maximum, calls
        active += 1
        calls += 1
        maximum = max(maximum, active)
        await asyncio.sleep(.03)
        active -= 1
        return {'proposal': {'client': filename}, 'metadata': {
            'outcome': {'state': 'partial'}, 'verification': {
                'coverageComplete': True, 'reviewCompleted': True, 'visionRequired': False}}}
    for worker in instance.workers:
        monkeypatch.setattr(worker, 'run', run)

    async def scenario():
        results = await asyncio.gather(instance.run(b'A', 'a.txt'), instance.run(b'B', 'b.txt'),
                                       instance.run(b'A', 'a.txt'))
        assert maximum == calls == 2
        assert results[0] == results[2]
        results[0]['proposal']['client'] = 'edited'
        cached = await instance.run(b'A', 'a.txt')
        assert cached['metadata']['cacheHit']
        assert cached['proposal']['client'] == 'a.txt'
        assert instance.pending == instance.active == 0
    asyncio.run(scenario())
