"""Offline Responses contract, privacy, retry and deadline checks."""
import json

import httpx
import pytest

from backend.openai_client import OpenAIError, OpenAIResponsesClient, strict_schema
from backend.openai_model import SemanticModelService


SCHEMA = {'type': 'object', 'properties': {
    'tables': {'type': 'array', 'items': {'$ref': '#/$defs/Table'}},
}, 'required': ['tables'], '$defs': {'Table': {
    'type': 'object', 'properties': {
        'name': {'type': 'string'}, 'quantityColumn': {'type': 'integer', 'default': 0},
        'comment': {'anyOf': [{'type': 'string'}, {'type': 'null'}], 'default': None},
    }, 'required': ['name'],
}}}


def response_payload(value):
    return {'status': 'completed', 'output': [{'type': 'message', 'role': 'assistant',
             'content': [{'type': 'output_text', 'text': json.dumps(value)}]}]}


def test_strict_schema_normalizes_nested_refs_and_nullable_defaults_without_mutation():
    result = strict_schema(SCHEMA)
    table = result['$defs']['Table']
    assert result['additionalProperties'] is False
    assert table['additionalProperties'] is False
    assert table['required'] == ['name', 'quantityColumn', 'comment']
    assert table['properties']['quantityColumn'] == {'anyOf': [{'type': 'integer'}, {'type': 'null'}]}
    assert table['properties']['comment'] == {'anyOf': [{'type': 'string'}, {'type': 'null'}]}
    assert SCHEMA['$defs']['Table']['properties']['quantityColumn']['default'] == 0
    assert SCHEMA['$defs']['Table']['required'] == ['name']


@pytest.mark.parametrize('url', ['https://api.openai.com/v1', 'https://api.openai.com/v1/responses'])
def test_responses_transport_uses_strict_schema_store_false_and_restores_only_dto_defaults(monkeypatch, url):
    monkeypatch.setenv('OPENAI_API_KEY', 'offline-test-key')
    monkeypatch.setenv('OPENAI_API_URL', url)
    seen = []
    def handler(request):
        seen.append(request)
        assert request.url.path == '/v1/responses'
        body = json.loads(request.content)
        assert body['store'] is False
        assert body['text']['format']['type'] == 'json_schema'
        assert body['text']['format']['strict'] is True
        assert body['model'] == 'gpt-5-mini'
        assert 'temperature' not in body and 'messages' not in body
        assert body['input'] == [{'role': 'user', 'content': 'source cells'}]
        assert request.headers['Authorization'] == 'Bearer offline-test-key'
        return httpx.Response(200, json=response_payload({'tables': [
            {'name': 'Cable', 'quantityColumn': None, 'comment': None}]}))
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = OpenAIResponsesClient(http)
        model = SemanticModelService(client=client)
        result = json.loads(model.generate([{'role': 'user', 'content': 'source cells'}], SCHEMA,
                                          max_tokens=1000, remaining=5))
    assert result == {'tables': [{'name': 'Cable', 'quantityColumn': 0, 'comment': None}]}
    assert len(seen) == 1


def test_missing_key_is_explicit_and_never_initializes_http_pool(monkeypatch):
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    client = OpenAIResponsesClient()
    with pytest.raises(OpenAIError, match='OPENAI_API_KEY'):
        client.generate([], SCHEMA, max_tokens=500, remaining=2)
    assert client._client is None
    assert SemanticModelService(client=client).available() is False


def test_only_explicit_transient_statuses_are_retried_inside_document_budget(monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'offline-test-key')
    monkeypatch.setenv('OPENAI_MAX_RETRIES', '2')
    monkeypatch.setattr('backend.openai_client.time.sleep', lambda value: None)
    statuses = [429, 503, 200]
    seen = []
    def handler(request):
        seen.append(request)
        status = statuses.pop(0)
        return httpx.Response(status, headers={'retry-after': '0'}, json=response_payload({'tables': []}))
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        assert json.loads(OpenAIResponsesClient(http).generate([], SCHEMA,
                          max_tokens=500, remaining=5)) == {'tables': []}
    assert len(seen) == 3


@pytest.mark.parametrize('code, error_type', [
    ('insufficient_quota', None),
    ('credit_balance_exhausted', 'insufficient_quota'),
    ('billing_hard_limit_reached', None),
    ('provider_quota_limit', 'insufficient_quota'),
])
def test_insufficient_quota_is_actionable_sanitized_and_never_retried(monkeypatch, code, error_type):
    monkeypatch.setenv('OPENAI_API_KEY', 'offline-test-key')
    monkeypatch.setenv('OPENAI_MAX_RETRIES', '4')
    monkeypatch.setattr('backend.openai_client.time.sleep', lambda value: pytest.fail('Quota exhaustion must not retry'))
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(429, headers={'retry-after': '1'}, json={'error': {
            'code': code, 'type': error_type, 'message': 'private document text offline-test-key',
        }})
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(OpenAIError, match='insufficient_quota') as failure:
            OpenAIResponsesClient(http).generate([], SCHEMA, max_tokens=500, remaining=5)
    assert len(seen) == 1
    assert 'баланс' in str(failure.value) and 'проект' in str(failure.value)
    assert 'private document' not in str(failure.value) and 'offline-test-key' not in str(failure.value)


def test_rate_limit_429_is_still_retried(monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'offline-test-key')
    monkeypatch.setenv('OPENAI_MAX_RETRIES', '2')
    monkeypatch.setattr('backend.openai_client.time.sleep', lambda value: None)
    seen = []
    def handler(request):
        seen.append(request)
        if len(seen) == 1:
            return httpx.Response(429, headers={'retry-after': '0'}, json={'error': {
                'code': 'rate_limit_exceeded', 'message': 'private response details',
            }})
        return httpx.Response(200, json=response_payload({'tables': []}))
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        result = OpenAIResponsesClient(http).generate([], SCHEMA, max_tokens=500, remaining=5)
    assert json.loads(result) == {'tables': []} and len(seen) == 2


@pytest.mark.parametrize('status', [400, 401, 403, 404])
def test_nontransient_errors_are_never_retried_or_leaked(monkeypatch, status):
    monkeypatch.setenv('OPENAI_API_KEY', 'offline-test-key')
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(status, json={'error': {'message': 'private quote data offline-test-key'}})
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(OpenAIError) as failure:
            OpenAIResponsesClient(http).generate([], SCHEMA, max_tokens=500, remaining=5)
    assert str(status) in str(failure.value)
    assert 'private' not in str(failure.value) and 'offline-test-key' not in str(failure.value)
    assert len(seen) == 1


def test_network_failure_is_not_replayed_and_is_sanitized(monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'offline-test-key')
    seen = []
    def handler(request):
        seen.append(request)
        raise httpx.ReadTimeout('private request body secret', request=request)
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(OpenAIError, match='ReadTimeout') as failure:
            OpenAIResponsesClient(http).generate([], SCHEMA, max_tokens=500, remaining=5)
    assert 'private' not in str(failure.value) and len(seen) == 1


def test_expired_budget_prevents_provider_call(monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'offline-test-key')
    client = OpenAIResponsesClient()
    with pytest.raises(OpenAIError, match='время'):
        client.generate([], SCHEMA, max_tokens=500, remaining=0)
    assert client._client is None


def test_response_arriving_after_budget_cannot_claim_success(monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'offline-test-key')
    instant = [100.]
    monkeypatch.setattr('backend.openai_client.time.monotonic', lambda: instant[0])
    def handler(request):
        instant[0] += 2
        return httpx.Response(200, json=response_payload({'tables': []}))
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(OpenAIError, match='время'):
            OpenAIResponsesClient(http).generate([], SCHEMA, max_tokens=500, remaining=1)


@pytest.mark.parametrize('data, expected', [
    ({'status': 'incomplete', 'output': []}, 'truncated'),
    ({'status': 'completed', 'output': [{'type': 'message', 'role': 'assistant',
       'content': [{'type': 'refusal', 'refusal': 'private document data'}]}]}, 'отказался'),
    ({'status': 'completed', 'output': []}, 'пустой'),
])
def test_incomplete_refused_or_empty_responses_cannot_claim_success(monkeypatch, data, expected):
    monkeypatch.setenv('OPENAI_API_KEY', 'offline-test-key')
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=data))) as http:
        with pytest.raises(OpenAIError, match=expected):
            OpenAIResponsesClient(http).generate([], SCHEMA, max_tokens=500, remaining=5)
