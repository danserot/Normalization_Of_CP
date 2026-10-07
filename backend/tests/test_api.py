import asyncio
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from backend import main
from backend.extraction import DocumentError
from backend.pipeline import Pipeline


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, 'DATABASE_PATH', tmp_path / 'test.sqlite3')
    monkeypatch.setattr(main, 'APP_PASSWORD', '')
    monkeypatch.setenv('LOCAL_MODEL_ENABLED', 'false')
    monkeypatch.setattr(main, 'model_available', lambda: True)
    main.initialize_db()
    with TestClient(main.app) as client:
        yield client


def test_api_bad_file_and_recovery(client):
    assert client.post('/api/extract', files={'file': ('test.exe', b'x')}).status_code == 415
    assert client.post('/api/extract', files={'file': ('test.pdf', b'bad')}).status_code == 422
    response = client.post('/api/extract', files={'file': ('test.txt', 'Клиент: Альфа'.encode())})
    assert response.status_code == 200
    assert response.json()['proposal']['client'] == 'Альфа'
    assert response.json()['metadata']['verification']['mode'] == 'rules_fallback'
    assert response.json()['metadata']['outcome']['state'] == 'partial'


def test_busy_rejected_before_starting_second_worker(client):
    main.pipeline.pending = main.pipeline.max_pending
    try:
        response = client.post('/api/extract', files={'file': ('test.txt', b'Client: Alpha')})
        assert response.status_code == 429
    finally:
        main.pipeline.pending = 0


def test_save_nullable_total_and_idempotency(client):
    result = client.post('/api/extract', files={'file': ('test.txt', 'Клиент: Альфа'.encode())}).json()
    proposal = result['proposal']
    proposal['items'] = [{'name': 'Item', 'quantity': 2, 'unit': '', 'unitPrice': 0, 'lineTotal': 0}]
    payload = {'proposal': proposal, 'sources': [result['metadata']]}
    headers = {'Idempotency-Key': str(uuid4())}
    first = client.post('/api/proposals', json=payload, headers=headers)
    assert first.status_code == 200
    again = client.post('/api/proposals', json=payload, headers=headers)
    assert again.json()['duplicate']
    assert first.json()['id'] == again.json()['id']
    assert client.get('/api/proposals/' + first.json()['id']).json()['payload'] == payload
    payload['proposal']['items'][0]['quantity'] = None
    assert client.post('/api/proposals', json=payload).status_code == 200
    payload['proposal']['items'][0]['quantity'] = -1
    assert client.post('/api/proposals', json=payload).status_code == 422


def test_component_only_service_can_be_saved_without_invented_quantity(client):
    result = client.post('/api/extract', files={'file': ('test.txt', 'Название: КП'.encode())}).json()
    proposal = result['proposal']
    proposal['items'] = [{'name': 'Монтаж вентиляции', 'quantity': None, 'unit': '',
                          'unitPrice': None, 'lineTotal': None,
                          'componentMode': 'alternative',
                          'components': [{'label': 'СМР с МБОР', 'lineTotal': 100},
                                         {'label': 'СМР без МБОР', 'lineTotal': 90}]}]
    saved = client.post('/api/proposals', json={'proposal': proposal, 'sources': [result['metadata']]})
    assert saved.status_code == 200
    restored = client.get('/api/proposals/' + saved.json()['id']).json()['payload']['proposal']
    assert restored['items'][0]['quantity'] is None
    assert restored['items'][0]['unitPrice'] is None
    assert restored['items'][0]['componentMode'] == 'alternative'
    proposal['items'][0]['quantity'] = 0
    assert client.post('/api/proposals', json={'proposal': proposal, 'sources': []}).status_code == 200


def test_worker_hard_deadline_and_recovery(monkeypatch):
    from backend import pipeline as module
    monkeypatch.setenv('LOCAL_MODEL_ENABLED', 'false')
    instance = Pipeline()
    try:
        monkeypatch.setattr(module, 'DEADLINE', -.1)
        with pytest.raises(DocumentError, match='Обработка остановлена'):
            asyncio.run(instance.run(b'Client: Alpha', 'text.txt'))
        assert instance.process is None
        monkeypatch.setattr(module, 'DEADLINE', 180)
        result = asyncio.run(instance.run(b'Client: Alpha', 'text.txt'))
        assert result['proposal']['client'] == 'Alpha'
        assert result['metadata']['verification']['mode'] == 'rules_fallback'
    finally:
        instance.stop()
