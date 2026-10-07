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


def test_wrong_typed_grounded_metadata_is_rejected():
    source, _ = sample()
    plan = Plan(fields=[
        Quote(field='documentNumber', cell='c3', value='Оборудование'),
        Quote(field='documentDate', cell='c7', value='14,204'),
        Quote(field='supplier', cell='c0', value='Описание'),
    ], tables=[], issues=[])
    proposal, proof, warnings, _, _ = apply_plan(source, plan)
    assert proposal['documentNumber'] == proposal['documentDate'] == proposal['supplier'] == ''
    assert not proof
    assert sum('не соответствует типу' in warning for warning in warnings) == 3


def test_ambiguous_rules_result_survives_unavailable_semantic_model():
    source, _ = sample()
    with patch('backend.universal.model_available', return_value=False), patch(
            'backend.rules.extract_rules') as rules:
        result, report = extract_universal(source, b'', 'unknown.pdf')
    assert result is not None
    assert report['mode'] == 'rules_fallback'
    assert not report['reviewCompleted']
    assert report['llmCalls'] == 0


@pytest.mark.parametrize('text,value', [('14,204', 14.204), ('48 600,0000', 48600),
    ('1 68 117 934,30', 168117934.30), ('1,234.56', 1234.56), ('SKU-12', None), ('NaN', None)])
def test_contextual_estimate_numbers(text, value):
    assert numeric_quote(text) == value
