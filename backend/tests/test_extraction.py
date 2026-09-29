import os
from unittest.mock import patch

import pytest

from backend.extraction import DocumentError, Source, read_document
from backend.local_model import apply_candidates
from backend.pipeline import process_document
from backend.rules import extract_rules, parse_number
from backend.tests.fixtures import make_fixtures


@pytest.fixture(scope='module')
def fixtures(tmp_path_factory):
    path = tmp_path_factory.mktemp('documents')
    make_fixtures(path)
    return path


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setenv('LOCAL_MODEL_ENABLED', 'false')


@pytest.mark.parametrize('value,expected', [('1 234,56', 1234.56), ('1,234.56', 1234.56),
    ('1.234,56', 1234.56), ('1234.56', 1234.56), ('0', 0), ('1,234', None),
    ('29.09.2026', None), ('SKU-12', None), ('', None), ('NaN', None), ('1 234,56 KZT', 1234.56), ('12 34', None)])
def test_numbers(value, expected):
    assert parse_number(value) == expected


@pytest.mark.parametrize('name,count', [('offer.xlsx', 2), ('offer.xls', 1), ('offer.csv', 1),
    ('offer.tsv', 1), ('offer.json', 1), ('offer.docx', 1), ('offer.txt', 1), ('offer-utf16.txt', 1), ('text.pdf', 1)])
def test_formats_and_every_value_evidence(fixtures, name, count):
    result = process_document((fixtures / name).read_bytes(), name)
    p, m = result['proposal'], result['metadata']
    assert len(p['items']) == count
    assert p['items'][0] == {'name': 'Кабель', 'quantity': 2, 'unit': 'шт.', 'unitPrice': 1234.56, 'lineTotal': 2469.12}
    source = {c['id']: c for c in m['sourceCells']}
    for key, value in p.items():
        if key in ('notes', 'items') or value in ('', None):
            continue
        assert key in m['fieldEvidence'], key
    for index, item in enumerate(p['items']):
        for key, value in item.items():
            if value not in ('', None):
                assert m['fieldEvidence'][f'items.{index}.{key}']['value'] == value
    for e in m['fieldEvidence'].values():
        for entry in e if isinstance(e, list) else [e]:
            assert entry['excerpt'] == source[entry['id']]['text']
            assert entry['file'] == name
            assert entry['row'] >= 1 and entry['cell'] >= 1
    if name in ('offer.docx', 'offer.txt', 'offer.xlsx', 'text.pdf'):
        assert p['client'] == 'ТОО Альфа'
        assert p['validUntil'] == '31.10.2026'
        assert p['paymentTerms'] == '50% предоплата'
        assert p['documentTotal'] == 2469.12


def test_missing_and_ambiguous_never_default(fixtures):
    result = process_document((fixtures / 'missing.csv').read_bytes(), 'missing.csv')
    first, second = result['proposal']['items']
    assert first['quantity'] is None and first['unitPrice'] is None
    assert second['quantity'] == 2 and second['unitPrice'] is None
    assert len(result['metadata']['warnings']) >= 3


def test_arbitrary_numeric_columns_are_not_prices(fixtures):
    result = process_document((fixtures / 'unknown.csv').read_bytes(), 'unknown.csv')
    assert result['proposal']['items'] == []
    assert result['metadata']['status'] == 'empty'


def test_conflicting_values_preserved(fixtures):
    result = process_document((fixtures / 'conflict.txt').read_bytes(), 'conflict.txt')
    assert [e['value'] for e in result['metadata']['fieldEvidence']['client']] == ['ТОО Гамма', 'ТОО Дельта']


def test_model_cannot_invent_or_cross_rows():
    source = Source('test.txt')
    source.add('Кабель', 'table', 1)
    source.add('100', 'table', 2, 2)
    proposal, proof, warnings, used = extract_rules(source)
    apply_candidates({'fields': [{'field': 'client', 'cell': 'c0', 'value': 'Придуманное ООО'}],
        'items': [{'name': 'c0', 'quantity': '', 'unitPrice': 'c1'}]}, source.cells, proposal, proof)
    assert not proposal['client'] and not proposal['items']


def test_invalid_signature_and_limits():
    with pytest.raises(DocumentError):
        read_document(b'not a pdf', 'bad.pdf')
    with pytest.raises(DocumentError):
        read_document(b'a' * 200001, 'huge.txt')


def test_text_pdf_never_runs_ocr(fixtures):
    with patch('backend.extraction.ocr', side_effect=AssertionError('Unexpected OCR')):
        result = process_document((fixtures / 'text.pdf').read_bytes(), 'text.pdf')
    assert result['metadata']['ocrPages'] == 0


@pytest.mark.skipif(not os.getenv('TEST_OCR'), reason='Enable TEST_OCR=1 with local Tesseract installed')
@pytest.mark.parametrize('name', ['scan.png', 'scan.jpg', 'scan.webp', 'scan.pdf', 'rotated.png'])
def test_real_ocr(fixtures, name):
    result = process_document((fixtures / name).read_bytes(), name)
    assert result['metadata']['ocrPages'] == 1
    assert result['proposal']['client'] == 'ТОО Альфа'
    assert result['metadata']['fieldEvidence']['client']['page'] == 1
    assert 'ocr' in result['metadata']['fieldEvidence']['client']['method']


@pytest.mark.skipif(not os.getenv('TEST_OCR'), reason='Requires Tesseract')
def test_ocr_table_and_mixed_pdf(fixtures):
    result = process_document((fixtures / 'table-scan.png').read_bytes(), 'table-scan.png')
    assert result['proposal']['items'][0]['name'] == 'Кабель'
    assert result['proposal']['items'][0]['quantity'] == 2
    assert result['proposal']['items'][0]['unitPrice'] == 1234.56
    mixed = process_document((fixtures / 'mixed.pdf').read_bytes(), 'mixed.pdf')
    assert mixed['metadata']['ocrPageNumbers'] == [2]
    assert len(mixed['proposal']['items']) == 2


def test_footer_is_not_item():
    result = process_document('Наименование\tКоличество\tЦена\nКабель\t2\t100\nГарантия: 12 месяцев'.encode(), 'test.txt')
    assert len(result['proposal']['items']) == 1
    assert result['proposal']['warranty'] == '12 месяцев'


@pytest.mark.skipif(not os.getenv('TEST_OCR'), reason='Requires Tesseract kaz')
def test_kazakh_characters(fixtures, monkeypatch):
    monkeypatch.setenv('TESSERACT_LANG', 'rus+eng+kaz')
    result = process_document((fixtures / 'kazakh.png').read_bytes(), 'kazakh.png')
    assert result['proposal']['client'] == 'Әділ Ұйым'
    assert result['proposal']['supplier'] == 'Қазақ Өнім'


@pytest.mark.parametrize('columns', [('name','quantity','unitPrice'), ('unitPrice','name','quantity'), ('quantity','unitPrice','name')])
def test_column_permutations(columns):
    values = {'name': 'Кабель', 'quantity': '2', 'unitPrice': '100'}
    text = ';'.join(columns) + '\n' + ';'.join(values[c] for c in columns)
    assert process_document(text.encode(), 'test.csv')['proposal']['items'][0]['unitPrice'] == 100
