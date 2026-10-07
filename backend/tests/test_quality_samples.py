"""End-to-end extraction assertions against generated commercial proposals."""

import json
import os
from pathlib import Path

import pytest

from backend.pipeline import process_document
from backend.extraction import DocumentError, read_document
from backend.rules import extract_rules


SAMPLES = Path(__file__).resolve().parents[2] / 'examples' / 'mock_kp'
TEXT_FORMATS = ('csv', 'docx', 'json', 'pdf', 'tsv', 'txt', 'xls', 'xlsx')
IMAGE_FORMATS = ('jpg', 'png', 'webp')


@pytest.mark.parametrize('filename', [*(f'offer.{ext}' for ext in TEXT_FORMATS), 'offer-scan.pdf'])
def test_mock_offer_fields_items_and_evidence(filename, monkeypatch):
    if filename == 'offer-scan.pdf' and not os.getenv('TEST_VISION'):
        pytest.skip('Run with TEST_VISION=1 and an OpenAI API key for live vision')
    if filename == 'offer-scan.pdf':
        monkeypatch.setenv('VISION_ENABLED', 'true')
    monkeypatch.setenv('LOCAL_MODEL_ENABLED', 'false')
    def fixture_model(source, _content, _filename):
        proposal, proof, warnings, used = extract_rules(source)
        return (proposal, proof, warnings, used), {
            'mode': 'model', 'reviewCompleted': True, 'visionUsed': False,
            'coverageComplete': True, 'unclaimedRows': [], 'issues': [],
        }
    monkeypatch.setattr('backend.pipeline.extract_universal', fixture_model)
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


@pytest.mark.parametrize('extension', IMAGE_FORMATS)
def test_standalone_images_need_openai_configuration(extension):
    source = read_document((SAMPLES / f'offer.{extension}').read_bytes(), f'offer.{extension}')
    assert source.routing['mandatory']
    assert source.routing['available'] is False
    assert not source.text().strip()
