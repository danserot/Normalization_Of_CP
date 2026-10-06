"""Synthetic cases only; never load real documents into a test failure."""
import hashlib
import json
from pathlib import Path

import pytest

from backend.annotation_data import prepare_annotation
from backend.outcome import extraction_outcome
from training.local.exclusions import assert_training_allowed
from training.local.privacy import atomic_json
from training.local.quarantine import contained, filter_datasets, reasons_for, verified_move


def payload(text):
    return prepare_annotation(text.encode(), 'synthetic.txt')


def test_explicit_unknown_cost_is_a_candidate_but_ocr_and_optional_fields_are_not():
    record = {'parse_status': 'parsed'}
    assert 'E_PRICE_UNSPECIFIED' in reasons_for(payload('Стоимость: по запросу'), record)
    clean = payload('Наименование\tКоличество\tЦена\tСумма\nТестовая гайка\t2\t100\t200')
    assert reasons_for(clean, record) == []
    for cell in clean['cells']:
        cell['method'] = 'ocr'
    assert reasons_for(clean, record) == []
    assert reasons_for(payload('Поставщик: ООО Синтетика'), record) == []
    assert reasons_for(None, {'parse_status': 'parse_error'}) == ['E_PARSE']


def test_missing_cost_and_arithmetic_have_distinct_reasons():
    record = {'parse_status': 'parsed'}
    missing = payload('Наименование\tКоличество\tЦена\nТестовая гайка\t2\tуточняется')
    assert 'E_PRICE_UNAVAILABLE' in reasons_for(missing, record)
    wrong = payload('Наименование\tКоличество\tЦена\tСумма\nТестовая гайка\t2\t100\t250')
    assert 'E_ARITHMETIC' in reasons_for(wrong, record)


def test_outcome_preserves_zero_prices_and_reports_unknown_values():
    proposal = {'supplier': 'Синтетика', 'documentTotal': 0, 'items': [
        {'name': 'Образец', 'quantity': 1, 'unit': 'шт', 'unitPrice': 0, 'lineTotal': 0}]}
    result = extraction_outcome(proposal, {})
    missing = {entry['field'] for entry in result['unavailable']}
    assert result['state'] == 'partial'
    assert 'documentTotal' not in missing and 'items.0.unitPrice' not in missing
    assert 'currency' in missing
    assert extraction_outcome({'notes': 'Синтетический текст'}, {})['state'] == 'unavailable'
    proposal['items'][0]['unitPrice'] = None
    assert 'items.0.unitPrice' in {e['field'] for e in extraction_outcome(proposal, {})['unavailable']}


def test_moves_are_reversible_and_do_not_overwrite_changes(tmp_path):
    source, target = tmp_path / 'files', tmp_path / 'quarantine'
    source.mkdir()
    content = b'synthetic fixture'
    (source / 'fixture.txt').write_bytes(content)
    entry = {'relative': 'fixture.txt', 'document': hashlib.sha256(content).hexdigest()}
    verified_move(source, target, entry)
    assert not (source / 'fixture.txt').exists()
    assert (target / 'fixture.txt').read_bytes() == content
    verified_move(source, target, entry)  # Interrupted run can safely resume.
    (source / 'fixture.txt').write_bytes(b'user edit')
    with pytest.raises(RuntimeError, match='E_TARGET_CONFLICT'):
        verified_move(target, source, entry)
    assert (source / 'fixture.txt').read_bytes() == b'user edit'
    (source / 'fixture.txt').unlink()
    verified_move(target, source, entry)
    assert (source / 'fixture.txt').read_bytes() == content
    with pytest.raises(RuntimeError, match='E_PATH_OUTSIDE_ROOT'):
        contained(source, '../outside.txt')


def test_dataset_filter_and_training_gate_keep_provenance(tmp_path):
    for split, data in [('train', [{'output': '{}'}, {'output': '{}'}, {'output': '{}'}]), ('val', []), ('test', [])]:
        atomic_json(tmp_path / 'datasets' / f'kp_{split}.json', data)
    manifest = [{'document': 'excluded', 'split': 'train', 'start': 0, 'count': 2},
                {'document': 'keep', 'split': 'train', 'start': 2, 'count': 1}]
    atomic_json(tmp_path / 'datasets/manifest.json', manifest)
    atomic_json(tmp_path / 'quarantine/exclusions.json', {'documents': {'excluded': ['E_PARSE']}})
    with pytest.raises(RuntimeError, match='E_QUARANTINED_TRAINING_DATA'):
        assert_training_allowed(tmp_path)
    datasets, filtered = filter_datasets({'excluded'}, tmp_path)
    assert len(datasets['train']) == 1
    assert filtered == [{'document': 'keep', 'split': 'train', 'start': 0, 'count': 1}]
    atomic_json(tmp_path / 'datasets/manifest.json', filtered)
    assert_training_allowed(tmp_path)


def test_quarantine_apply_filters_existing_data_and_restore_returns_originals(tmp_path, monkeypatch):
    from training.local import quarantine
    private, originals = tmp_path / 'private', tmp_path / 'files'
    originals.mkdir()
    monkeypatch.setattr(quarantine, 'ROOT', private)
    monkeypatch.setattr(quarantine, 'INPUT', originals)
    content = b'Only synthetic data'
    identifier = hashlib.sha256(content).hexdigest()
    (originals / 'source.txt').write_bytes(content)
    atomic_json(private / 'quarantine/plan.json', {'documents': {identifier: ['E_PARSE']},
        'files': [{'document': identifier, 'relative': 'source.txt'}]})
    for split in ('train', 'val', 'test'):
        atomic_json(private / 'datasets' / f'kp_{split}.json', [{'output': '{}'}] if split == 'train' else [])
    atomic_json(private / 'datasets/manifest.json', [{'document': identifier, 'split': 'train', 'start': 0, 'count': 1}])
    quarantine.apply(str(private / 'missing.sqlite3'))
    assert not (originals / 'source.txt').exists()
    assert json.loads((private / 'datasets/kp_train.json').read_text()) == []
    assert (private / 'quarantine/datasets-before/kp_train.json').exists()
    quarantine.restore(str(private / 'missing.sqlite3'))
    assert (originals / 'source.txt').read_bytes() == content
    assert json.loads((private / 'quarantine/exclusions.json').read_text())['documents'] == {}
