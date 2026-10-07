"""Vision coverage, ordered concurrency, evidence locations and honest failures."""
import base64
from io import BytesIO
import json
import re
import threading
import time

import httpx
import pytest
from PIL import Image

from backend.canonical import CanonicalDocument
from backend.fusion import DocumentFusion
from backend.openai_client import OpenAIResponsesClient
from backend.vision import OpenAIDocumentVisionService, RenderedPage, vision_health


def page(number=1):
    output = BytesIO()
    Image.new('RGB', (160, 240), 'white').save(output, format='PNG')
    return RenderedPage(number, output.getvalue(), 160, 240, 80, 120)


def payload(number=1, *, blocks=None, complete=True, blank=False, warnings=None):
    return {'page': number, 'complete': complete, 'blank': blank,
            'blocks': [{'type': 'text', 'text': 'Коммерческое предложение', 'html': ''}]
                      if blocks is None else blocks,
            'warnings': [] if warnings is None else warnings}


def response(data):
    return httpx.Response(200, json={'status': 'completed', 'output': [
        {'type': 'message', 'role': 'assistant',
         'content': [{'type': 'output_text', 'text': json.dumps(data, ensure_ascii=False)}]}]})


def test_actual_responses_request_sends_image_and_returns_complete_page_without_invented_geometry(monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'offline-test-key')
    monkeypatch.setenv('VISION_ENABLED', 'true')
    image = page(7)
    html = '<table><tr><th>Товар</th><th>Количество</th></tr><tr><td>Кабель</td><td></td></tr></table>'
    def handler(request):
        body = json.loads(request.content)
        assert request.url.path.endswith('/responses')
        assert body['store'] is False
        assert body['text']['format']['strict'] is True
        assert body['text']['format']['schema']['properties']['page']['enum'] == [7]
        contents = body['input'][1]['content']
        assert contents[1]['type'] == 'input_image' and contents[1]['detail'] == 'high'
        assert base64.b64decode(contents[1]['image_url'].split(',', 1)[1]) == image.image
        assert 'bbox' not in json.dumps(body['text']['format']['schema'])
        return response(payload(7, blocks=[
            {'type': 'title', 'text': 'КП №17', 'html': ''},
            {'type': 'table', 'text': '', 'html': html},
            {'type': 'footer', 'text': 'Срок поставки: 14 дней', 'html': ''},
        ]))
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        result = OpenAIDocumentVisionService(client=OpenAIResponsesClient(http)).analyze_pages([image], remaining=10)
    assert result.available and not result.warnings
    assert result.pages[0].source_method == 'openai-vision'
    assert [block.type for block in result.pages[0].blocks] == ['title', 'table', 'footer']
    assert all(block.bbox is None and not block.cell_boxes for block in result.pages[0].blocks)
    assert 'Количество' in result.pages[0].blocks[1].html


def test_page_concurrency_is_bounded_and_results_stay_in_source_order(monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'offline-test-key')
    monkeypatch.setenv('VISION_ENABLED', 'true')
    monkeypatch.setenv('OPENAI_VISION_CONCURRENCY', '2')
    active, peak = 0, 0
    guard = threading.Lock()
    def handler(request):
        nonlocal active, peak
        body = json.loads(request.content)
        number = int(re.search(r'page (\d+)', body['input'][1]['content'][0]['text'])[1])
        with guard:
            active += 1
            peak = max(peak, active)
        time.sleep(.015 * (2 if number % 2 else 1))
        with guard:
            active -= 1
        return response(payload(number))
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        service = OpenAIDocumentVisionService(client=OpenAIResponsesClient(http))
        result = service.analyze_pages([page(i) for i in range(1, 6)], remaining=10)
    assert result.available and peak == 2
    assert [page.page for page in result.pages] == [1, 2, 3, 4, 5]


def test_wrong_page_identity_keeps_successful_pages_but_never_claims_all_covered(monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'offline-test-key')
    def handler(request):
        body = json.loads(request.content)
        number = body['text']['format']['schema']['properties']['page']['enum'][0]
        return response(payload(99 if number == 2 else number))
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        result = OpenAIDocumentVisionService(client=OpenAIResponsesClient(http)).analyze_pages([page(1), page(2)])
    assert not result.available and [page.page for page in result.pages] == [1]
    assert 'Страница 2' in result.error and result.warnings


@pytest.mark.parametrize('data', [
    payload(complete=False, warnings=['Не читается правый край']),
    payload(blocks=[{'type': 'text', 'text': 'Цена: [неразборчиво]', 'html': ''}]),
    payload(blocks=[]),
])
def test_unreadable_or_unexplained_empty_response_is_honestly_partial(monkeypatch, data):
    monkeypatch.setenv('OPENAI_API_KEY', 'offline-test-key')
    with httpx.Client(transport=httpx.MockTransport(lambda request: response(data))) as http:
        result = OpenAIDocumentVisionService(client=OpenAIResponsesClient(http)).analyze_pages([page()])
    assert not result.available and not result.pages[0].complete
    assert result.warnings and 'частично' in result.error


def test_geometry_free_fusion_replaces_native_page_once_preserving_other_pages_and_missing_cells(monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'offline-test-key')
    html = ('<table><tr><th rowspan="2">Товар</th><th colspan="2">Данные</th></tr>'
            '<tr><th>Количество</th><th>Цена</th></tr>'
            '<tr><td>Кабель</td><td></td><td>120</td></tr></table>')
    data = payload(blocks=[{'type': 'table', 'text': '', 'html': html}])
    source = CanonicalDocument('proposal.pdf')
    source.ensure_page(1, 80, 120)
    source.ensure_page(2, 80, 120)
    source.add('Кабель', 'native-page1', 1, 1, page=1, kind='table', bbox=[0, 0, 20, 10])
    source.add('2', 'native-page1', 1, 2, page=1, kind='table', bbox=[20, 0, 40, 10])
    source.add('Другой товар', 'native-page2', 1, 1, page=2, kind='table')
    with httpx.Client(transport=httpx.MockTransport(lambda request: response(data))) as http:
        result = OpenAIDocumentVisionService(client=OpenAIResponsesClient(http)).analyze_pages([page()])
    DocumentFusion().merge(source, result)
    visual = [cell for cell in source.cells if cell.page == 1]
    assert len([cell for cell in visual if cell.text == 'Кабель']) == 1
    assert all(cell.bbox is None and cell.source_method == 'openai-vision' for cell in visual)
    assert next(cell for cell in visual if (cell.row, cell.cell) == (3, 2)).text == ''
    assert next(cell for cell in visual if cell.text == 'Данные').colspan == 2
    assert next(cell for cell in visual if (cell.row, cell.cell) == (2, 1)).merged_into
    assert any(cell.text == 'Другой товар' and cell.block == 'native-page2' for cell in source.cells)
    assert source.routing['processed_pages'] == [1] and source.routing['used']


def test_complete_blank_page_is_counted_and_partial_page_is_not(monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'offline-test-key')
    source = CanonicalDocument('blank.pdf')
    source.ensure_page(1, 80, 120)
    data = payload(blocks=[], blank=True)
    with httpx.Client(transport=httpx.MockTransport(lambda request: response(data))) as http:
        result = OpenAIDocumentVisionService(client=OpenAIResponsesClient(http)).analyze_pages([page()])
    DocumentFusion().merge(source, result)
    assert result.available and source.routing['processed_pages'] == [1]
    assert not source.cells and source.routing['status'] == 'vision'
    data = payload(complete=False, warnings=['Не читается край страницы'])
    with httpx.Client(transport=httpx.MockTransport(lambda request: response(data))) as http:
        result = OpenAIDocumentVisionService(client=OpenAIResponsesClient(http)).analyze_pages([page()])
    DocumentFusion().merge(source, result)
    assert source.cells and source.routing['processed_pages'] == []
    assert 'incomplete_openai_page' in source.routing['issues']


def test_health_and_missing_configuration_never_make_paid_calls(monkeypatch):
    monkeypatch.setenv('VISION_ENABLED', 'true')
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    service = OpenAIDocumentVisionService()
    result = service.analyze_pages([page()])
    assert not result.available and 'OPENAI_API_KEY' in result.error
    health = vision_health()
    assert not health['configured'] and not health['available']
    monkeypatch.setenv('OPENAI_API_KEY', 'offline-test-key')
    health = vision_health()
    assert health['configured'] and health['available'] and health['connectivityVerified'] is False
    assert health['provider'] == 'openai'


def test_contradictory_blank_status_requires_manual_review(monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'offline-test-key')
    data = payload(blocks=[], blank=True, complete=False, warnings=['Не читается текст'])
    with httpx.Client(transport=httpx.MockTransport(lambda request: response(data))) as http:
        result = OpenAIDocumentVisionService(client=OpenAIResponsesClient(http)).analyze_pages([page()])
    assert not result.available and not result.pages
    assert 'противоречивый' in result.error


def test_duplicate_pages_and_expired_budget_are_rejected_before_requests(monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'offline-test-key')
    class Client:
        def generate(self, *args, **kwargs):
            pytest.fail('invalid pages or expired budget must never reach provider')
    service = OpenAIDocumentVisionService(client=Client())
    assert not service.analyze_pages([page(1), page(1)]).available
    assert not service.analyze_pages([page(1)], remaining=0).available
