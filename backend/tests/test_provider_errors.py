"""Provider failures remain typed and sanitized across transport/HTTP boundaries."""
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from backend import main
from backend.errors import ProviderError
from backend.openai_client import OpenAIError, OpenAIResponsesClient, connection_error


@pytest.mark.parametrize('failure,code,status', [
    (httpx.ConnectError('[SSL: UNEXPECTED_EOF_WHILE_READING] secret'), 'api_tls_reset', 503),
    (httpx.ConnectError('certificate_verify_failed secret'), 'api_certificate_error', 503),
    (httpx.ConnectError('connection refused secret'), 'api_connection_error', 503),
    (httpx.ReadTimeout('secret'), 'api_timeout', 504),
])
def test_network_failure_classification_has_no_private_exception_text(failure, code, status):
    error = connection_error(failure)
    assert error.code == code and error.http_status == status
    assert 'secret' not in str(error)
    assert 'secret' not in json.dumps(error.public_info())
    rebuilt = ProviderError(error.worker_payload()['message'],
        **{key: value for key, value in error.worker_payload().items() if key != 'message'})
    assert rebuilt.public_info() == error.public_info()


def test_http_reports_tls_failure_as_service_unavailable_not_invalid_document(monkeypatch):
    monkeypatch.setattr(main, 'APP_PASSWORD', '')
    monkeypatch.setattr(main, 'model_available', lambda: True)
    async def run(content, filename):
        raise connection_error(httpx.ConnectError('[SSL: UNEXPECTED_EOF_WHILE_READING]'))
    monkeypatch.setattr(main.pipeline, 'run', run)
    with TestClient(main.app) as client:
        response = client.post('/api/extract', files={'file': ('synthetic.xls', b'original')})
    assert response.status_code == 503
    assert response.json()['error'] == {'code': 'api_tls_reset', 'stage': 'connection', 'retryable': True}
    assert 'proposal' not in response.json()


def test_rejected_request_has_different_stage_than_connection_error(monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'offline-test-key')
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(
            400, json={'error': {'message': 'private source content', 'param': 'input'}}))) as http:
        with pytest.raises(OpenAIError) as failure:
            OpenAIResponsesClient(http).generate([], {'type': 'object', 'properties': {}},
                max_tokens=50, remaining=5)
    assert failure.value.code == 'api_request_rejected'
    assert failure.value.stage == 'request'
    assert 'private' not in str(failure.value)


def test_explicit_proxy_is_used_and_ambient_proxy_remains_disabled(monkeypatch):
    seen = {}
    fake = object()
    def client_factory(**kwargs):
        seen.update(kwargs)
        return fake
    monkeypatch.setattr(httpx, 'Client', client_factory)
    monkeypatch.setenv('OPENAI_PROXY_URL', 'http://proxy.example:3128')
    client, _ = OpenAIResponsesClient()._resources()
    assert client is fake
    assert seen['proxy'] == 'http://proxy.example:3128'
    assert seen['trust_env'] is False
