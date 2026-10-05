"""End-to-end extraction assertions against generated commercial proposals."""

import json
import os
from pathlib import Path

import pytest

from backend.pipeline import process_document


SAMPLES = Path(__file__).resolve().parents[2] / 'examples' / 'mock_kp'
TEXT_FORMATS = ('csv', 'docx', 'json', 'pdf', 'tsv', 'txt', 'xls', 'xlsx')
OCR_FORMATS = ('jpg', 'png', 'webp')


@pytest.mark.parametrize('filename', [*(f'offer.{ext}' for ext in TEXT_FORMATS),
                                      *(f'offer.{ext}' for ext in OCR_FORMATS), 'offer-scan.pdf'])
def test_mock_offer_fields_items_and_evidence(filename, monkeypatch):
    if filename in (*[f'offer.{ext}' for ext in OCR_FORMATS], 'offer-scan.pdf') and not os.getenv('TEST_OCR'):
        pytest.skip('Run with TEST_OCR=1 and Tesseract rus+eng')
    monkeypatch.setenv('LOCAL_MODEL_ENABLED', 'false')
    expected = json.loads((SAMPLES / 'expected.json').read_text(encoding='utf-8'))
    result = process_document((SAMPLES / filename).read_bytes(), filename)
    proposal, evidence = result['proposal'], result['metadata']['fieldEvidence']

    for key, value in expected.items():
        if key == 'items':
            assert proposal['items'] == value
            for index, item in enumerate(value):
                for field in item:
                    assert f'items.{index}.{field}' in evidence
        else:
            assert proposal[key] == value, (filename, key)
            assert key in evidence

    cells = {cell['id']: cell for cell in result['metadata']['sourceCells']}
    for entry in evidence.values():
        for source in entry if isinstance(entry, list) else [entry]:
            assert source['id'] in cells
            assert source['excerpt'] == cells[source['id']]['text']
            assert source['file'] == filename
