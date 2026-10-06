"""Unknown layouts, grounding, review failures and optional visual routing."""
from unittest.mock import patch

import httpx
import pytest

from backend.extraction import Source
from backend.universal import Plan, Quote, apply_plan, extract_universal, numeric_quote


def sample():
    source = Source('unknown.pdf')
    for block, row, values in [
        ('p1', 1, ['Описание', 'Объём', 'Мера', 'Оборудование', 'Работы', 'Артикул']),
        ('p1', 2, ['Клапан', '14,204', 'м3', '48 600,0000', '2500', 'ZX-19']),
        ('p2', 1, ['Другой клапан', '2', 'шт', '100', '50', 'ZX-20']),
    ]:
        for col, text in enumerate(values, 1):
            source.add(text, block, row, col, page=1 if block == 'p1' else 2)
    tables = [{'block': block, 'firstRow': row, 'lastRow': row, 'nameColumn': 1,
               'quantityColumn': 2, 'unitColumn': 3,
               'components': [{'label': 'Оборудование', 'labelCell': 'c3', 'priceColumn': 4, 'totalColumn': 0},
                              {'label': 'Работы', 'labelCell': 'c4', 'priceColumn': 5, 'totalColumn': 0}],
               'extras': [{'label': 'Артикул', 'labelCell': 'c5', 'column': 6}]} for block, row in [('p1', 2), ('p2', 1)]]
    return source, Plan(fields=[], tables=tables, issues=[])


def test_unseen_headers_components_continuation_and_proof():
    source, plan = sample()
    proposal, proof, warnings, covered, unclaimed = apply_plan(source, plan)
    assert len(proposal['items']) == 2
    item = proposal['items'][0]
    assert item['quantity'] == 14.204
    assert item['unitPrice'] == 51100
    assert item['components'][0]['unitPrice'] == 48600
    assert item['additionalFields'] == [{'label': 'Артикул', 'value': 'ZX-19'}]
    assert proof['items.0.unitPrice']['method'] == 'calculated'
    assert not proof['items.0.unitPrice']['verifiedInSource']
    assert proof['items.1.components.0.unitPrice']['page'] == 2
    assert covered == {('p1', 2), ('p2', 1)}
    assert not unclaimed


def test_hallucinated_header_and_field_are_rejected():
    source, plan = sample()
    plan.tables[0].components[0].label = 'Выдуманная цена'
    plan.fields = [Quote(field='supplier', cell='c0', value='Не существует')]
    plan = Plan.model_validate(plan.model_dump())
    proposal, proof, warnings, _, unclaimed = apply_plan(source, plan)
    assert proposal['supplier'] == ''
    assert len(proposal['items']) == 1
    assert ('p1', 2) in unclaimed
    assert len(warnings) >= 2


def test_review_uses_original_and_repaired_plan():
    source, plan = sample()
    incomplete = plan.model_copy(deep=True)
    incomplete.tables.pop()
    with patch('backend.universal.model_available', return_value=True), patch('backend.universal.infer_plan', return_value=incomplete), patch(
            'backend.universal.request_plan', return_value=plan) as request:
        result, report = extract_universal(source, b'', 'unknown.pdf')
    assert report['reviewCompleted']
    assert len(result[0]['items']) == 2
    assert request.call_args.kwargs['review']
    assert request.call_args.args[1]['source']
    assert request.call_args.args[1]['unclaimedRows'] == [('p2', 1)]


def test_failed_review_retains_extraction_with_visible_warning():
    source, plan = sample()
    with patch('backend.universal.model_available', return_value=True), patch('backend.universal.infer_plan', return_value=plan), patch(
            'backend.universal.request_plan', side_effect=httpx.ReadTimeout('timeout')):
        result, report = extract_universal(source, b'', 'unknown.pdf')
    assert len(result[0]['items']) == 2
    assert not report['reviewCompleted'] and not report['coverageComplete']
    assert report['issues']


def test_vision_routes_problem_pages_and_keeps_grounding(monkeypatch):
    source, plan = sample()
    source.ocr_pages = [1]
    monkeypatch.setenv('LOCAL_VISION_ENABLED', 'true')
    with patch('backend.universal.model_available', return_value=True), patch('backend.universal.infer_plan', return_value=plan), patch(
            'backend.universal.request_plan', return_value=plan) as request, patch(
            'backend.universal.page_images', return_value=['data:image/jpeg;base64,AA==']):
        _, report = extract_universal(source, b'', 'unknown.pdf')
    assert report['visionUsed']
    assert request.call_args.kwargs['images']


def test_model_unavailable_never_runs_rule_fallback():
    source, _ = sample()
    with patch('backend.universal.model_available', return_value=False), patch(
            'backend.rules.extract_rules') as rules:
        result, report = extract_universal(source, b'', 'unknown.pdf')
    assert result is None
    assert report['mode'] == 'model_error'
    assert 'данные не извлечены' in report['issues'][0]
    rules.assert_not_called()


@pytest.mark.parametrize('text,value', [('14,204', 14.204), ('48 600,0000', 48600),
    ('1 68 117 934,30', 168117934.30), ('1,234.56', 1234.56), ('SKU-12', None), ('NaN', None)])
def test_contextual_estimate_numbers(text, value):
    assert numeric_quote(text) == value
