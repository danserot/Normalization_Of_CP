"""Offline architecture and archived-review compatibility checks."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.annotation_data import (
    Annotation, MarkedTable, plan_training_examples, prepare_annotation,
    source_from_payload, training_examples, validate_annotation,
)
from backend.canonical import Source
from backend.outcome import FIELD_LABELS
from backend.semantic_contracts import Layout, Plan, Requisites


def test_fresh_app_import_has_no_legacy_extraction_dependencies():
    script = """
import sys
import backend.main
import backend.annotation_data
forbidden = (
    'backend.extraction', 'backend.universal', 'backend.rules',
    'backend.local_model', 'backend.vision', 'docx', 'openpyxl', 'xlrd',
)
loaded = [name for name in sys.modules
          if any(name == prefix or name.startswith(prefix + '.')
                 for prefix in forbidden)]
assert not loaded, loaded
"""
    descriptor, database = tempfile.mkstemp(prefix='readdocument-import-', suffix='.sqlite3')
    os.close(descriptor)
    environment = {**os.environ, 'OPENAI_API_KEY': '', 'DATABASE_PATH': database}
    try:
        result = subprocess.run(
            [sys.executable, '-c', script],
            cwd=Path(__file__).resolve().parents[2], env=environment,
            capture_output=True, text=True, timeout=20,
        )
    finally:
        Path(database).unlink(missing_ok=True)
    assert result.returncode == 0, result.stderr


def test_new_annotation_uses_model_cells_and_reviewable_table_blocks(monkeypatch):
    from backend import model_document

    cells = [
        {'id': 'supplier', 'text': 'Synthetic Supplier', 'block': 'metadata',
         'row': 1, 'cell': 1, 'kind': 'text'},
        {'id': 'single', 'text': 'Single column', 'block': 'one-column',
         'row': 1, 'cell': 1, 'kind': 'table'},
        {'id': 'product', 'text': 'Synthetic item', 'block': 'items',
         'row': 2, 'cell': 1, 'kind': 'table'},
        {'id': 'price', 'text': '10', 'block': 'items',
         'row': 2, 'cell': 2, 'kind': 'table'},
    ]
    for cell in cells:
        cell.update(file='synthetic.xlsx', method='model', source_method='model')
    calls = []

    def model_result(content, filename):
        calls.append((content, filename))
        return {'proposal': {'notes': 'Synthetic model transcription'},
                'metadata': {'sourceCells': cells, 'warnings': [],
                             'fieldEvidence': {'supplier': {
                                 'sourceId': 'supplier', 'excerpt': 'Synthetic Supplier'}},
                             'apiUsage': {'complete': False}}}

    monkeypatch.setattr(model_document, 'extract_model_document', model_result)
    payload = prepare_annotation(b'original mock file', 'synthetic.xlsx')
    assert calls == [(b'original mock file', 'synthetic.xlsx')]
    assert [table['block'] for table in payload['annotation']['tables']] == ['items']
    assert payload['sourceIndependentlyVerified'] is False
    annotation = Annotation.model_validate(payload['annotation'])
    for field in annotation.fields:
        field.state = 'found' if field.cell else 'missing'
    annotation.group = 'synthetic'
    annotation.tables[0].reviewed = True
    validate_annotation(annotation, payload, 'synthetic.xlsx', True)


def test_archived_cells_and_training_exports_keep_the_same_contracts():
    source = Source('synthetic.xlsx')
    source.add('Synthetic Supplier', 'metadata', 1)
    for column, text in enumerate(('Product', 'Quantity', 'Price', 'Total'), 1):
        source.add(text, 'table', 1, column, kind='table')
    for column, text in enumerate(('Synthetic item', '2', '10', '20'), 1):
        source.add(text, 'table', 2, column, kind='table')
    annotation = Annotation(
        fields=[{'field': key, 'state': 'missing'} for key in FIELD_LABELS],
        tables=[MarkedTable(block='table', reviewed=True, isItems=True,
                            firstRow=2, lastRow=2, nameColumn=1,
                            quantityColumn=2, unitColumn=0,
                            components=[{'label': 'Price', 'labelCell': 'c3',
                                         'priceColumn': 3, 'totalColumn': 4}],
                            extras=[])], group='synthetic',
    )
    supplier = next(field for field in annotation.fields if field.field == 'supplier')
    supplier.state, supplier.cell, supplier.value = 'found', 'c0', 'Synthetic Supplier'
    payload = {'cells': source.public()}
    validate_annotation(annotation, payload, source.file, True)
    for example in training_examples(annotation, payload, source.file):
        answer = json.loads(example['output'])
        (Requisites if 'fields' in answer else Layout).model_validate(answer)
    plan = Plan.model_validate_json(plan_training_examples(annotation, payload, source.file)[0]['output'])
    assert plan.fields[0].value == 'Synthetic Supplier'
    assert plan.tables[0].firstRow == 2
    from backend import universal
    assert universal.Layout is Layout and universal.Plan is Plan


def test_old_and_canonical_snapshots_keep_provenance():
    payload = {'cells': [{'id': 'c0', 'file': 'old.pdf', 'text': '20',
                         'block': 'table', 'row': 2, 'column': 3, 'page': 1,
                         'kind': 'table', 'sourceMethod': 'pdf-native+vision',
                         'visionAgreement': True, 'rowSpan': 2, 'columnSpan': 1}],
               'routing': {'used': True}}
    source = source_from_payload(payload, 'old.pdf')
    assert source.cells[0].cell == 3
    assert source.cells[0].source_method == 'pdf-native+vision'
    assert source.cells[0].vision_agreement is True
    assert source.cells[0].rowspan == 2
    assert source.routing['used']


def test_annotation_provider_error_keeps_its_status_and_safe_diagnostics():
    from backend.annotations import create_annotation_router, initialize_annotations
    from backend.errors import ProviderError

    class UnavailablePipeline:
        async def run(self, content, filename, mode):
            raise ProviderError('Сервис временно недоступен', code='network_error',
                                http_status=503, stage='connection', retryable=True)

    database = sqlite3.connect(':memory:', check_same_thread=False)
    database.row_factory = sqlite3.Row
    initialize_annotations(database)
    application = FastAPI()
    application.include_router(create_annotation_router(
        lambda: database, lambda: None, UnavailablePipeline()))
    try:
        with TestClient(application) as client:
            response = client.post('/api/annotations',
                                   files={'file': ('synthetic.xls', b'mock document')})
        assert response.status_code == 503
        assert response.json() == {
            'detail': 'Сервис временно недоступен',
            'error': {'code': 'network_error', 'stage': 'connection', 'retryable': True},
        }
        assert database.execute('SELECT COUNT(*) FROM annotations').fetchone()[0] == 0
    finally:
        database.close()
