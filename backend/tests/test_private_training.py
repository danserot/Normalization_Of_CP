"""Only invented fixtures. No real corpus is opened in tests or failures."""
import copy
from unittest.mock import patch

import pytest

from backend.annotation_data import Annotation, prepare_annotation, training_examples
from training.local.quality import check_annotation, grouped_partition, signatures
from training.local.privacy import require_isolation


def example():
    payload = prepare_annotation(('Поставщик: ООО Синтетика\n'
        'Наименование\tКоличество\tЕд. изм.\tЦена\tСумма\n'
        'Тестовый товар\t2\tшт\t100.00\t200.00').encode(), 'synthetic.txt')
    a = payload['annotation']
    for f in a['fields']:
        f['state'] = 'found' if f['value'] else 'missing'
    a['group'] = 'synthetic'
    a['tables'][0].update(firstRow=3, lastRow=3)
    return payload, Annotation.model_validate(a)


def test_valid_machine_label_does_not_mark_human_review():
    payload, annotation = example()
    result = check_annotation(payload, annotation)
    assert result['passed'], result['reasons']
    assert result['arithmetic_checked'] == 1
    assert not result['human_reviewed']
    assert not any(t.reviewed for t in annotation.tables)
    assert training_examples(annotation, payload, 'synthetic.txt')


def test_hallucination_wrong_cell_arithmetic_and_omitted_table_are_rejected():
    payload, annotation = example()
    annotation.fields[0].state = 'found'
    annotation.fields[0].cell = 'absent'
    annotation.fields[0].value = 'invented'
    assert 'E_GROUNDING' in check_annotation(payload, annotation)['reasons']
    payload, annotation = example()
    payload['cells'][-1]['text'] = '250.00'
    assert 'E_ARITHMETIC' in check_annotation(payload, annotation)['reasons']
    annotation.tables[0].isItems = False
    assert 'E_COVERAGE' in check_annotation(payload, annotation)['reasons']


def test_ocr_and_missing_cached_formula_require_review():
    payload, annotation = example()
    payload['incomplete'] = True
    assert 'E_OCR' in check_annotation(payload, annotation)['reasons']
    payload['incomplete'] = False
    payload['cells'][0]['method'] = 'ocr'
    assert 'E_OCR' in check_annotation(payload, annotation)['reasons']


def test_duplicate_families_do_not_leak_between_splits():
    records = {}
    for i in range(10):
        records[str(i)] = {'signature': {'text_hash': str(i // 2), 'supplier': '', 'shingles': [str(i // 2)]}}
    result = grouped_partition(records, target=2)
    assert {v['split'] for v in result.values()} == {'train', 'val', 'test'}
    for i in range(0, 10, 2):
        assert result[str(i)] == result[str(i + 1)]
    assert result == grouped_partition(dict(reversed(list(records.items()))), target=2)


def test_network_check_fails_closed_on_host():
    with patch('training.local.privacy.sys.platform', 'win32'):
        with pytest.raises(RuntimeError, match='E_NETWORK_NAMESPACE'):
            require_isolation()


def test_explicit_rules_only_recover_literal_unambiguous_labels():
    from backend.extraction import Source
    from training.local.inference import explicit_fields
    source = Source('synthetic')
    source.add('Поставщик: ООО Учебный', 'text', 1)
    source.add('Иван Иванов, менеджер', 'text', 2)
    result = explicit_fields(source.cells)
    assert set(result) == {'supplier'}
    assert result['supplier'].value == 'ООО Учебный'
    source.add('Поставщик: ООО Другой', 'text', 3)
    assert explicit_fields(source.cells) == {}


def test_inference_schema_limits_references_and_column_ranges():
    from backend.universal import inference_schema, Layout, Requisites
    schema = inference_schema(Requisites, [['c42', 'Synthetic']])
    assert schema['$defs']['Quote']['properties']['cell']['enum'] == ['c42']
    assert 'supplier' in schema['$defs']['Quote']['properties']['field']['enum']
    schema = inference_schema(Layout, {'rows': [{'row': 1, 'cells': [['c1', 1, 'Name'], ['c2', 2, 'Price']]}],
                                      'previousLayout': None})
    assert schema['$defs']['ExtraColumn']['properties']['column']['enum'] == [1, 2]
    assert schema['properties']['nameColumn']['enum'] == [0, 1, 2]


def test_fast_labels_require_explicit_headers_and_preserve_quotes():
    from training.local.fast_labels import rule_annotation
    payload, _ = example()
    label = rule_annotation(payload, 'synthetic')
    assert check_annotation(payload, label)['passed']
    assert label.tables[0].firstRow == label.tables[0].lastRow == 3
    assert not any(t.reviewed for t in label.tables)
    payload['cells'][-1]['text'] = '250'
    assert not check_annotation(payload, rule_annotation(payload, 'synthetic'))['passed']


def test_certified_layout_does_not_train_uncertain_requisites():
    from training.local.task_labels import certified_layouts
    payload, _ = example()
    payload['annotation']['fields'][0].update(value='Unconfirmed', cell='absent')
    examples = certified_layouts(payload, 'synthetic')
    assert len(examples) == 1
    assert 'isItems' in examples[0]['output']
    assert 'fields' not in examples[0]['output']
    payload['cells'][-1]['text'] = '250'
    assert not certified_layouts(payload, 'synthetic')


def test_task_metrics_penalize_missing_rows_and_wrong_references():
    import json
    from backend.universal import Layout
    from training.local.task_labels import certified_layouts
    from training.local.evaluate_tasks import score
    payload, _ = example()
    task = certified_layouts(payload, 'synthetic')[0]
    target = Layout.model_validate_json(task['output'])
    predicted = target.model_copy(deep=True)
    predicted.isItems = False
    predicted.components[0].labelCell = 'absent'
    result = score(predicted, target, json.loads(task['instruction']))
    assert result['target_rows'] == 1
    assert result['predicted_rows'] == result['correct_rows'] == 0
    assert result['grounded_references'] == result['exact_references'] == 0


def test_empty_model_result_falls_back_to_explicit_source_table():
    from backend.annotation_data import source_from_payload
    from backend.universal import Plan, extract_universal
    payload, _ = example()
    source = source_from_payload(payload, 'synthetic.txt')
    empty = Plan(fields=[], tables=[], issues=[])
    with patch('backend.universal.model_available', return_value=True), \
         patch('backend.universal.infer_plan', return_value=empty), \
         patch('backend.universal.request_plan', return_value=empty):
        result, report = extract_universal(source, b'', 'synthetic.txt')
    assert result is None
    assert report['mode'] == 'fallback'
    assert not report['coverageComplete']
    assert any('пропустила товарные строки' in issue for issue in report['issues'])
