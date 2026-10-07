"""Full-row execution, bounded planning and source-grounded validation."""
import json
from unittest.mock import patch

import httpx
import pytest

from backend.extraction import Source
from backend.local_model import SemanticModelService
from backend.openai_client import OpenAIResponsesClient
from backend.rules import evidence, extract_rules, validate
from backend.universal import Plan, Quote, apply_plan, extract_universal, plan_payload, request_plan


def table_source(count=3):
    source = Source('synthetic.xlsx')
    headers = ['Наименование', 'Количество', 'Ед. изм.', 'Цена', 'Сумма']
    row = 1
    for column, text in enumerate(headers, 1):
        source.add(text, 'sheet-offer', row, column, kind='table', sheet='offer')
    for index in range(count):
        row += 1
        if index == count // 2:
            for column, text in enumerate(headers, 1):
                source.add(text, 'sheet-offer', row, column, kind='table', sheet='offer')
            row += 1
        for column, text in enumerate([f'Товар {index + 1}', '2', 'шт', '100', '200'], 1):
            source.add(text, 'sheet-offer', row, column, kind='table', sheet='offer')
    row += 1
    for column, text in enumerate(['Итого', '', '', '', str(count * 200)], 1):
        source.add(text, 'sheet-offer', row, column, kind='table', sheet='offer')
    plan = Plan(fields=[], tables=[{
        'block': 'sheet-offer', 'firstRow': 1, 'lastRow': row,
        'nameColumn': 1, 'quantityColumn': 2, 'unitColumn': 3,
        'components': [{'label': 'Цена', 'labelCell': 'c3', 'priceColumn': 4, 'totalColumn': 5}],
        'extras': [],
    }], issues=[])
    return source, plan


def test_five_hundred_rows_need_one_bounded_plan_and_exact_coverage(monkeypatch):
    source, plan = table_source(500)
    monkeypatch.setenv('SEMANTIC_MODE', 'always')
    payload = plan_payload(source)
    assert len(payload['tables'][0]['sampleRows']) < 25
    assert payload['tables'][0]['rowCount'] == 503
    assert len(json.dumps(payload, ensure_ascii=False)) < 12000
    with patch('backend.universal.model_available', return_value=True), patch(
            'backend.universal.infer_plan', return_value=plan) as infer:
        result, report = extract_universal(source, b'', source.file)
    infer.assert_called_once()
    assert report['llmCalls'] == 1
    assert report['coverageComplete']
    assert report['unclaimedRows'] == []
    proposal, proof, warnings, covered = result
    assert len(proposal['items']) == len(covered) == 500
    assert proposal['items'][-1]['name'] == 'Товар 500'
    assert all(item['lineTotal'] == 200 for item in proposal['items'])
    assert proof['items.499.lineTotal']['verifiedInSource']
    assert all(item['name'] not in {'Итого', 'Наименование'} for item in proposal['items'])


def test_zero_quantity_is_preserved_and_negative_numbers_are_rejected():
    source, plan = table_source(2)
    source.cells[6].text = '0'
    source.cells[16].text = '-2'
    proposal, proof, warnings, _, _ = apply_plan(source, plan)
    assert proposal['items'][0]['quantity'] == 0
    assert proposal['items'][1]['quantity'] is None
    assert proof['items.0.quantity']['value'] == 0
    assert any('отрицательное количество' in warning for warning in warnings)


def test_alternatives_are_not_summed_even_if_model_forgets_the_flag():
    source = Source('variants.pdf')
    for row, values in enumerate([
        ['Наименование', 'Количество', 'Вариант A', 'Вариант B'],
        ['Монтаж', '2', '100', '80'],
        ['Всего', '', '200', '160'],
    ], 1):
        for col, value in enumerate(values, 1):
            source.add(value, 'variants', row, col, kind='table', page=1)
    plan = Plan(fields=[Quote(field='documentTotal', cell='c10', value='200')], tables=[{
        'block': 'variants', 'firstRow': 1, 'lastRow': 3, 'nameColumn': 1,
        'quantityColumn': 2, 'unitColumn': 0, 'extras': [],
        'components': [
            {'label': 'Вариант A', 'labelCell': 'c2', 'priceColumn': 3, 'totalColumn': 0},
            {'label': 'Вариант B', 'labelCell': 'c3', 'priceColumn': 4, 'totalColumn': 0}],
    }], issues=[])
    proposal, proof, warnings, covered, unclaimed = apply_plan(source, plan)
    assert len(proposal['items']) == 1
    assert proposal['items'][0]['unitPrice'] is None
    assert proposal['items'][0]['lineTotal'] is None
    assert proposal['documentTotal'] is None
    assert 'documentTotal' not in proof
    assert covered == {('variants', 2)}
    assert unclaimed == []
    assert any('альтернативные' in warning for warning in warnings)


def test_unknown_fields_require_both_grounded_label_and_value():
    source = Source('metadata.txt')
    source.add('Срок производства: 45 дней', 'text', 1)
    source.add('Срок установки', 'text', 2, 1)
    source.add('5 дней', 'text', 2, 2)
    plan = Plan(fields=[
        Quote(field='Срок производства', cell='c0', value='45 дней'),
        Quote(field='Срок установки', cell='c2', value='5 дней'),
        Quote(field='Несуществующее поле', cell='c0', value='45 дней'),
        Quote(field='supplier', cell='c0', value='ТОО Выдумано'),
    ], tables=[], issues=[])
    proposal, proof, warnings, _, _ = apply_plan(source, plan)
    assert proposal['additionalFields'] == [
        {'label': 'Срок производства', 'value': '45 дней'},
        {'label': 'Срок установки', 'value': '5 дней'},
    ]
    assert proof['additionalFields.1.label']['sourceId'] == 'c1'
    assert proposal['supplier'] == proposal['warranty'] == ''
    assert len(warnings) >= 2
    deterministic = extract_rules(source)[0]
    assert deterministic['additionalFields'] == proposal['additionalFields']


def test_arithmetic_conflict_warns_without_changing_source_amount():
    source, plan = table_source(1)
    source.cells[14].text = '250'
    proposal, proof, warnings, _, _ = apply_plan(source, plan)
    original_confidence = proof['items.0.lineTotal']['confidence']
    validate(proposal, proof, warnings)
    assert proposal['items'][0]['lineTotal'] == 250
    assert any('отличается от суммы' in warning for warning in warnings)
    assert proof['items.0.lineTotal']['confidence'] < original_confidence
    assert 'arithmetic_conflict' in proof['items.0.lineTotal']['confidenceBasis']


def test_explicit_row_total_overrides_component_sum_and_conflict_is_visible():
    source = Source('components.csv')
    for row, values in enumerate([
        ['Наименование', 'Количество', 'Оборудование', 'Работы', 'Итог позиции'],
        ['Монтаж комплекта', '2', '20', '8', '24'],
    ], 1):
        for column, text in enumerate(values, 1):
            source.add(text, 'table', row, column, kind='table')
    plan = Plan(fields=[], tables=[{
        'block': 'table', 'firstRow': 2, 'lastRow': 2, 'nameColumn': 1,
        'quantityColumn': 2, 'unitColumn': 0, 'lineTotalColumn': 5, 'extras': [],
        'components': [
            {'label': 'Оборудование', 'labelCell': 'c2', 'priceColumn': 0, 'totalColumn': 3},
            {'label': 'Работы', 'labelCell': 'c3', 'priceColumn': 0, 'totalColumn': 4},
        ],
    }], issues=[])
    proposal, proof, warnings, _, _ = apply_plan(source, plan)
    validate(proposal, proof, warnings)
    assert proposal['items'][0]['lineTotal'] == 24
    assert proof['items.0.lineTotal']['sourceId'] == 'c9'
    assert any('сумма компонентов отличается' in warning for warning in warnings)


def test_money_cannot_be_sliced_into_document_number_or_total():
    source = Source('money.pdf')
    source.add('Итого', 'table', 1, 1, kind='table')
    source.add('1 250 000', 'table', 1, 2, kind='table')
    plan = Plan(fields=[
        Quote(field='documentTotal', cell='c1', value='250 000'),
        Quote(field='documentNumber', cell='c1', value='250'),
    ], tables=[], issues=[])
    proposal, proof, warnings, _, _ = apply_plan(source, plan)
    assert proposal['documentTotal'] is None
    assert proposal['documentNumber'] == ''
    assert not proof
    assert len(warnings) == 2


def test_missing_table_rows_are_reported_and_required_vision_cannot_claim_complete(monkeypatch):
    source, plan = table_source(3)
    source.routing = {'mandatory': True, 'status': 'unavailable', 'used': False}
    plan.tables[0].lastRow = 2
    # Disable rule supplement to expose the executor's own omitted-row diagnostics.
    empty_source = Source('empty')
    monkeypatch.setenv('SEMANTIC_MODE', 'always')
    with patch('backend.universal.model_available', return_value=True), patch(
            'backend.universal.infer_plan', return_value=plan), patch(
            'backend.universal.extract_rules', return_value=extract_rules(empty_source)):
        result, report = extract_universal(source, b'', source.file)
    assert len(result[0]['items']) == 1
    assert len(report['unclaimedRows']) == 2
    assert not report['coverageComplete']
    assert not report['reviewCompleted']
    assert any('visual document understanding' in issue for issue in report['issues'])


def test_confidence_is_backend_heuristic_with_native_vision_agreement():
    source = Source('native.pdf')
    source.add('1 250 000 ₸', 'table', 1, method='pdf-native+vision', kind='table')
    cell = source.cells[0]
    cell.vision_agreement = True
    agreed = evidence(cell, 1250000, 'model')
    cell.vision_agreement = False
    conflict = evidence(cell, 1250000, 'model')
    assert agreed['confidence'] > conflict['confidence']
    assert agreed['confidenceKind'] == 'heuristic'
    assert 'native_text' in agreed['confidenceBasis']
    assert 'vision_native_conflict' in conflict['confidenceBasis']


def test_production_request_and_training_share_exact_prompt():
    source, _ = table_source(1)
    from backend.universal import PLAN_SYSTEM
    with patch('backend.universal.request_object', return_value=Plan(fields=[], tables=[], issues=[])) as request:
        request_plan(source, plan_payload(source))
    assert request.call_args.args[3] == PLAN_SYSTEM


def test_visual_metadata_roles_and_locations_reach_semantic_model():
    source = Source('visual.pdf')
    source.add('Коммерческое предложение', 'visual-title', 1, page=2,
               bbox=[20, 30, 300, 70], reading_order=4)
    source.blocks[0].type = 'doc_title'
    source.add('Условия оплаты', 'visual-section', 1, page=2,
               bbox=[20, 400, 300, 430], reading_order=8)
    source.blocks[1].type = 'section_header'
    source.add('Наименование', 'visual-table', 1, 1, kind='table', page=2,
               bbox=[20, 90, 180, 110], reading_order=5)
    source.add('Цена', 'visual-table', 1, 2, kind='table', page=2,
               bbox=[180, 90, 300, 110], reading_order=5)
    payload = plan_payload(source)
    title = next(block for block in payload['metadataBlocks'] if block['id'] == 'visual-title')
    assert title['type'] == 'doc_title'
    assert title['page'] == 2
    assert title['sheet'] is None
    assert title['bbox'] == [20, 30, 300, 70]
    assert title['readingOrder'] == 4
    section = next(block for block in payload['metadataBlocks'] if block['id'] == 'visual-section')
    assert section['type'] == 'section_header'
    assert section['readingOrder'] == 8
    assert payload['tables'][0]['bbox'] == [20, 90, 300, 110]
    assert payload['tables'][0]['readingOrder'] == 5


def test_model_issues_are_marked_as_unverified_claims():
    source, plan = table_source(1)
    plan.issues = ['Все итоги исправлены и назначены.', 'Срок действия составляет 30 дней.']
    proposal, _, warnings, _, _ = apply_plan(source, plan)
    assert proposal['validUntil'] == ''
    assert all(f'Непроверенное замечание semantic model: {issue}' in warnings for issue in plan.issues)
    assert all(issue not in warnings for issue in plan.issues)


def run_with_plan(source, plan, monkeypatch):
    monkeypatch.setenv('SEMANTIC_MODEL_MODE', 'always')
    with patch('backend.universal.model_available', return_value=True), patch(
            'backend.universal.infer_plan', return_value=plan):
        return extract_universal(source, b'', source.file)


def test_alternative_totals_cannot_return_through_rules_supplement(monkeypatch):
    source = Source('variants.xlsx')
    for row, values in enumerate([
        ['Наименование', 'Количество', 'Вариант A', 'Вариант B'],
        ['Монтаж', '2', '100', '80'],
        ['Всего', '', '200', '160'],
    ], 1):
        for col, value in enumerate(values, 1):
            source.add(value, 'variants', row, col, kind='table')
    plan = Plan(fields=[], tables=[{
        'block': 'variants', 'firstRow': 2, 'lastRow': 2, 'nameColumn': 1,
        'quantityColumn': 2, 'unitColumn': 0, 'extras': [], 'componentMode': 'alternative',
        'components': [
            {'label': 'Вариант A', 'labelCell': 'c2', 'priceColumn': 3, 'totalColumn': 0},
            {'label': 'Вариант B', 'labelCell': 'c3', 'priceColumn': 4, 'totalColumn': 0}],
    }], issues=[])
    result, report = run_with_plan(source, plan, monkeypatch)
    assert result[0]['documentTotal'] is None
    assert result[0]['items'][0]['unitPrice'] is None
    assert result[0]['items'][0]['componentMode'] == 'alternative'
    assert result[0]['additionalFields'] == [
        {'label': 'Всего — Вариант A', 'value': '200'},
        {'label': 'Всего — Вариант B', 'value': '160'},
    ]
    assert 'documentTotal' not in result[1]


def test_incomplete_plan_preserves_known_native_scalars_for_same_row(monkeypatch):
    source, plan = table_source(2)
    plan.tables[0].quantityColumn = 0
    plan.tables[0].unitColumn = 0
    plan.tables[0].components[0].totalColumn = 0
    result, report = run_with_plan(source, plan, monkeypatch)
    assert result[0]['items'][0]['quantity'] == 2
    assert result[0]['items'][0]['unit'] == 'шт'
    assert result[0]['items'][0]['lineTotal'] == 200
    assert result[1]['items.0.quantity']['sourceId'] == 'c6'
    assert result[1]['items.0.lineTotal']['sourceId'] == 'c9'
    assert report['coverageComplete']


def test_two_column_service_rows_and_headers_do_not_become_metadata(monkeypatch):
    source = Source('services.csv')
    for row, values in enumerate([['Упаковка', 'Цена'], ['Упаковка оборудования', '100']], 1):
        for column, value in enumerate(values, 1):
            source.add(value, 'services', row, column, kind='table')
    plan = Plan(fields=[], tables=[{
        'block': 'services', 'firstRow': 2, 'lastRow': 2, 'nameColumn': 1,
        'quantityColumn': 0, 'unitColumn': 0, 'extras': [],
        'components': [{'label': 'Цена', 'labelCell': 'c1', 'priceColumn': 2, 'totalColumn': 0}],
    }], issues=[])
    result, _ = run_with_plan(source, plan, monkeypatch)
    assert result[0]['additionalFields'] == []
    assert result[0]['items'][0]['name'] == 'Упаковка оборудования'


def test_two_column_five_hundred_rows_remain_bounded():
    source = Source('two-columns.csv')
    for row, values in enumerate([['Наименование', 'Цена'], *[
            [f'Упаковка комплекта {index}', '100'] for index in range(500)]], 1):
        for column, value in enumerate(values, 1):
            source.add(value, 'items', row, column, kind='table')
    payload = plan_payload(source)
    assert len(json.dumps(payload, ensure_ascii=False)) < 12000
    assert len(payload['metadataBlocks']) < 5


def test_merged_header_relationships_reach_model_and_component_labels():
    source = Source('merged.pdf')
    for row, values in enumerate([
        ['Наименование', 'Вариант A', '', 'Вариант B', ''],
        ['', 'СМР', 'ТМЦ', 'СМР', 'ТМЦ'],
        ['Монтаж', '100', '200', '80', '160'],
    ], 1):
        for column, value in enumerate(values, 1):
            source.add(value, 'table', row, column, kind='table',
                       colspan=2 if row == 1 and column in {2, 4} else 1)
    plan = Plan(fields=[], tables=[{
        'block': 'table', 'firstRow': 3, 'lastRow': 3, 'nameColumn': 1,
        'quantityColumn': 0, 'unitColumn': 0, 'extras': [], 'componentMode': 'alternative',
        'components': [
            {'label': 'СМР', 'labelCell': 'c6', 'priceColumn': 0, 'totalColumn': 2},
            {'label': 'ТМЦ', 'labelCell': 'c7', 'priceColumn': 0, 'totalColumn': 3},
            {'label': 'СМР', 'labelCell': 'c8', 'priceColumn': 0, 'totalColumn': 4},
            {'label': 'ТМЦ', 'labelCell': 'c9', 'priceColumn': 0, 'totalColumn': 5}],
    }], issues=[])
    payload = plan_payload(source)
    assert next(cell for cell in payload['tables'][0]['headerCells'] if cell['id'] == 'c1')['colspan'] == 2
    assert next(header for header in payload['tables'][0]['columnHeaders'] if header['column'] == 3)['sourceIds'] == ['c1', 'c7']
    proposal, proof, _, _, _ = apply_plan(source, plan)
    assert [component['label'] for component in proposal['items'][0]['components']] == [
        'Вариант A / СМР', 'Вариант A / ТМЦ', 'Вариант B / СМР', 'Вариант B / ТМЦ']
    label_proof = proof['items.0.components.0.label']
    assert not label_proof['verifiedInSource']
    assert [ref['sourceId'] for ref in label_proof['sources']] == ['c1', 'c6']


def test_rejected_model_table_cannot_assign_partial_total_or_supplier_as_client(monkeypatch):
    source = Source('matrix.pdf')
    source.add('ТОО Синтетика', 'header', 1)
    for row, values in enumerate([
        ['Наименование', 'Вариант A', '', 'Вариант B', ''],
        ['', 'Стоимость СМР', 'Стоимость ТМЦ', 'Стоимость СМР', 'Стоимость ТМЦ'],
        ['Монтаж', '100', '200', '80', '160'],
        ['Итого с НДС', '116', '232', '92.8', '185.6'],
        ['Всего', '348', '', '278.4', ''],
    ], 1):
        for column, value in enumerate(values, 1):
            source.add(value, 'table', row, column, kind='table',
                       colspan=2 if row == 1 and column in {2, 4} else 1)
    plan = Plan(fields=[
        Quote(field='supplier', cell='c0', value='ТОО Синтетика'),
        Quote(field='client', cell='c0', value='ТОО Синтетика'),
        Quote(field='documentTotal', cell='c17', value='116'),
    ], tables=[{
        'block': 'table', 'firstRow': 3, 'lastRow': 3, 'nameColumn': 1,
        'quantityColumn': 0, 'unitColumn': 0, 'extras': [], 'componentMode': 'alternative',
        'components': [{'label': 'Выдумано', 'labelCell': 'c7', 'priceColumn': 0, 'totalColumn': 2}],
    }], issues=[])
    result, _ = run_with_plan(source, plan, monkeypatch)
    assert result[0]['supplier'] == 'ТОО Синтетика'
    assert result[0]['client'] == ''
    assert result[0]['documentTotal'] is None
    assert len(result[0]['items']) == 1
    assert [field['value'] for field in result[0]['additionalFields']] == ['348', '278.4']


def test_model_transport_rejects_truncated_plan(monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'offline-test-key')
    def handler(request):
        body = json.loads(request.content)
        assert request.url.path.endswith('/responses')
        assert body['text']['format']['type'] == 'json_schema'
        assert body['text']['format']['strict'] is True
        assert body['store'] is False and 'temperature' not in body
        return httpx.Response(200, json={'status': 'incomplete',
                              'incomplete_details': {'reason': 'max_output_tokens'}, 'output': []})
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        service = SemanticModelService(client=OpenAIResponsesClient(http))
        with pytest.raises(ValueError, match='truncated'):
            service.generate([{'role': 'user', 'content': '{}'}], Plan.model_json_schema(),
                             max_tokens=500, remaining=30)
