"""Pooled OpenAI Responses transport shared by vision and semantic planning.

Document contents and provider error bodies are never included in public errors.
Pool waits, retries and requests consume the caller's remaining document budget.
"""
from __future__ import annotations

from copy import deepcopy
from email.utils import parsedate_to_datetime
import json
import math
import os
import threading
import time
from datetime import datetime, timezone

import httpx

from .usage import record_usage


class OpenAIError(ValueError):
    """A safe, actionable error without document text or credentials."""


def _number(name: str, default: float, lower: float, upper: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError:
        return default
    return min(upper, max(lower, value)) if math.isfinite(value) else default


def setting_int(name: str, default: int, lower: int, upper: int) -> int:
    return int(_number(name, default, lower, upper))


def configured_model(*, vision: bool = False) -> str:
    default = os.getenv('OPENAI_MODEL', '').strip() or 'gpt-5-mini'
    return (os.getenv('OPENAI_VISION_MODEL', '').strip() or default) if vision else default


def openai_configured() -> bool:
    return bool(os.getenv('OPENAI_API_KEY', '').strip())


def strict_schema(schema: dict) -> dict:
    """Require all object keys, forbid extras, and make optionals nullable."""
    normalized = deepcopy(schema)

    def nullable(node):
        kind = node.get('type')
        if kind == 'null' or isinstance(kind, list) and 'null' in kind:
            return node
        if any(choice.get('type') == 'null' for choice in node.get('anyOf', [])):
            return node
        return {'anyOf': [node, {'type': 'null'}]}

    def visit(node):
        if not isinstance(node, dict):
            return
        node.pop('default', None)
        node.pop('examples', None)
        if node.get('type') == 'object' or 'properties' in node:
            properties = node.setdefault('properties', {})
            original_required = set(node.get('required', []))
            node['additionalProperties'] = False
            node['required'] = list(properties)
            for name, prop in list(properties.items()):
                visit(prop)
                if name not in original_required:
                    properties[name] = nullable(prop)
        for key in ('$defs', 'definitions'):
            for definition in node.get(key, {}).values():
                visit(definition)
        for key in ('anyOf', 'oneOf', 'allOf', 'prefixItems'):
            for branch in node.get(key, []):
                visit(branch)
        if isinstance(node.get('items'), dict):
            visit(node['items'])

    visit(normalized)
    return normalized


def _restore_defaults(value, node: dict, root: dict):
    reference = node.get('$ref', '')
    if reference.startswith('#/'):
        resolved = root
        for part in reference[2:].split('/'):
            resolved = resolved.get(part.replace('~1', '/').replace('~0', '~'), {})
        return _restore_defaults(value, resolved, root)
    if value is None:
        return deepcopy(node.get('default'))
    if isinstance(value, dict):
        required = set(node.get('required', []))
        for name, prop in node.get('properties', {}).items():
            if name not in value:
                continue
            if value[name] is None and name not in required and 'default' in prop:
                value[name] = deepcopy(prop['default'])
            else:
                value[name] = _restore_defaults(value[name], prop, root)
    elif isinstance(value, list) and isinstance(node.get('items'), dict):
        value = [_restore_defaults(item, node['items'], root) for item in value]
    elif node.get('anyOf'):
        branch = next((branch for branch in node['anyOf'] if branch.get('type') != 'null'), {})
        value = _restore_defaults(value, branch, root)
    return value


def _retry_delay(response: httpx.Response, attempt: int) -> float:
    header = response.headers.get('retry-after', '')
    try:
        delay = float(header)
    except ValueError:
        try:
            instant = parsedate_to_datetime(header)
            delay = (instant - datetime.now(timezone.utc)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            delay = .5 * (2 ** attempt)
    return min(8., max(.1, delay)) if math.isfinite(delay) else .5


def _quota_exhausted(response: httpx.Response) -> bool:
    """Inspect only quota code/type; never expose the provider's error body."""
    try:
        data = response.json()
        error = data.get('error') if isinstance(data, dict) else None
        return isinstance(error, dict) and (
            error.get('code') in {'insufficient_quota', 'credit_balance_exhausted', 'billing_hard_limit_reached'}
            or error.get('type') == 'insufficient_quota')
    except (ValueError, TypeError):
        return False


class OpenAIResponsesClient:
    """Thread-safe connection and inference pools shared by all API stages."""

    def __init__(self, client: httpx.Client | None = None):
        self._client = client
        self._lock = threading.Lock()
        self._semaphore = None

    def _resources(self):
        with self._lock:
            if self._semaphore is None:
                self._semaphore = threading.BoundedSemaphore(setting_int('OPENAI_MAX_CONCURRENCY', 4, 1, 16))
            if self._client is None:
                self._client = httpx.Client(
                    trust_env=False, follow_redirects=False,
                    limits=httpx.Limits(max_connections=16, max_keepalive_connections=8,
                                       keepalive_expiry=60))
            return self._client, self._semaphore

    def close(self):
        with self._lock:
            if self._client is not None:
                self._client.close()
                self._client = None

    def generate(self, messages, schema, *, max_tokens: int, remaining: float,
                 model: str | None = None, schema_name: str = 'document_plan') -> str:
        key = os.getenv('OPENAI_API_KEY', '').strip()
        if not key:
            raise OpenAIError('OpenAI API не настроен: укажите OPENAI_API_KEY в .env')
        if not math.isfinite(remaining) or remaining <= 0:
            raise OpenAIError('Истекло время обработки документа перед запросом OpenAI')
        budget = min(remaining, _number('OPENAI_TIMEOUT_SECONDS', 120., 1., 1800.))
        deadline = time.monotonic() + budget
        base_url = (os.getenv('OPENAI_API_URL', '').strip() or 'https://api.openai.com/v1').rstrip('/')
        endpoint = base_url if base_url.endswith('/responses') else base_url + '/responses'
        payload = {
            'model': model or configured_model(), 'input': messages, 'store': False,
            'max_output_tokens': max_tokens,
            'text': {'format': {'type': 'json_schema', 'name': schema_name,
                                'strict': True, 'schema': strict_schema(schema)}},
        }
        if payload['model'].startswith('gpt-5'):
            effort = os.getenv('OPENAI_REASONING_EFFORT', 'minimal').strip()
            if effort in {'minimal', 'low', 'medium', 'high'}:
                payload['reasoning'] = {'effort': effort}
        client, semaphore = self._resources()
        if not semaphore.acquire(timeout=max(0., deadline - time.monotonic())):
            raise OpenAIError('Истекло время ожидания свободного запроса OpenAI')
        try:
            retries = setting_int('OPENAI_MAX_RETRIES', 2, 0, 4)
            for attempt in range(retries + 1):
                remaining_request = deadline - time.monotonic()
                if remaining_request <= 0:
                    raise OpenAIError('Истекло время обработки документа в OpenAI')
                try:
                    response = client.post(endpoint, json=payload,
                        headers={'Authorization': 'Bearer ' + key},
                        timeout=httpx.Timeout(remaining_request, connect=min(10., remaining_request)))
                except httpx.HTTPError as error:
                    record_usage({}, payload['model'], 'vision' if schema_name == 'document_page' else 'semantic')
                    raise OpenAIError('Не удалось связаться с OpenAI API (' + type(error).__name__ + ')') from None
                if time.monotonic() >= deadline:
                    record_usage({}, payload['model'], 'vision' if schema_name == 'document_page' else 'semantic')
                    raise OpenAIError('Истекло время обработки документа в OpenAI')
                if response.status_code == 429 and _quota_exhausted(response):
                    # A depleted balance/project quota cannot recover with
                    # backoff. Avoid repeated paid-call attempts and explain
                    # which configuration the user must correct.
                    raise OpenAIError('OpenAI API: недостаточно квоты проекта (insufficient_quota); '
                                      'проверьте баланс, лимит расходов и проект API-ключа')
                if response.status_code == 429 or 500 <= response.status_code < 600:
                    if response.status_code >= 500:
                        # A server failure can happen after inference; do not
                        # claim the known successful usage is the whole bill.
                        record_usage({}, payload['model'], 'vision' if schema_name == 'document_page' else 'semantic')
                    if attempt < retries:
                        delay = _retry_delay(response, attempt)
                        if delay + .1 < deadline - time.monotonic():
                            time.sleep(delay)
                            continue
                if response.status_code != 200:
                    descriptions = {
                        401: 'проверьте OPENAI_API_KEY', 403: 'нет доступа к модели или проекту',
                        404: 'проверьте OPENAI_MODEL и OPENAI_API_URL',
                        429: 'лимит запросов или средств проекта; повторите позже',
                    }
                    reason = descriptions.get(response.status_code, 'сервис временно недоступен'
                                              if response.status_code >= 500 else 'проверьте настройки запроса')
                    raise OpenAIError(f'OpenAI API: HTTP {response.status_code}; {reason}')
                try:
                    data = response.json()
                    record_usage(data, payload['model'], 'vision' if schema_name == 'document_page' else 'semantic')
                    output = data.get('output', [])
                    if any(part.get('type') == 'refusal' for item in output
                           for part in item.get('content', [])):
                        raise OpenAIError('OpenAI отказался распознавать документ; требуется ручная проверка')
                    if data.get('status') == 'incomplete':
                        raise OpenAIError('Ответ OpenAI обрезан (truncated); сократите документ или увеличьте лимит ответа')
                    if data.get('status') != 'completed':
                        raise OpenAIError('OpenAI не завершил обработку документа')
                    content = ''.join(part.get('text', '') for item in output
                                      if item.get('type') == 'message' and item.get('role') == 'assistant'
                                      for part in item.get('content', []) if part.get('type') == 'output_text')
                    if not content.strip():
                        raise OpenAIError('OpenAI вернул пустой ответ; требуется ручная проверка')
                    parsed = json.loads(content)
                    if not isinstance(parsed, dict):
                        raise OpenAIError('OpenAI вернул некорректную структуру документа')
                    return json.dumps(_restore_defaults(parsed, schema, schema), ensure_ascii=False)
                except (ValueError, TypeError, KeyError, AttributeError) as error:
                    if isinstance(error, OpenAIError):
                        raise
                    raise OpenAIError('OpenAI вернул некорректный JSON; требуется ручная проверка') from None
            raise OpenAIError('OpenAI API временно недоступен')
        finally:
            semaphore.release()


openai_client = OpenAIResponsesClient()
