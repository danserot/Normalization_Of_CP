"""Read private artifacts locally and return ONLY fixed-category aggregates."""
import collections
import json
from pathlib import Path

from .privacy import atomic_json, emit, require_isolation


def main():
    require_isolation()
    root = Path('/private')
    metrics = collections.Counter()
    errors = collections.Counter()
    for path in (root / 'parsed').glob('*.json'):
        data = json.loads(path.read_text())
        cells = data.get('cells', [])
        metrics['documents'] += 1
        metrics['source_cells'] += len(cells)
        metrics['ocr_documents'] += any(c.get('method') == 'ocr' for c in cells)
        metrics['incomplete_documents'] += bool(data.get('incomplete'))
        error = data.get('parseError', '')
        if error:
            code = 'E_LIMIT' if any(s in error for s in ['лимит', 'превыш', 'больше']) else (
                'E_OCR' if 'OCR' in error or 'Tesseract' in error else 'E_EMPTY' if 'текст' in error else 'E_PARSE')
            errors[code] += 1
    labels = collections.Counter()
    reasons = collections.Counter()
    train_reasons = collections.Counter()
    split_counts = collections.defaultdict(collections.Counter)
    assignment_path = root / 'partition.json'
    assignment = json.loads(assignment_path.read_text())['documents'] if assignment_path.exists() else {}
    for path in (root / 'labels').glob('*.json'):
        data = json.loads(path.read_text())
        labels[data['status']] += 1
        reasons.update(data.get('quality', {}).get('reasons', []))
        split = assignment.get(path.stem, {}).get('split', 'unassigned')
        split_counts[split][data['status']] += 1
        if split == 'train':
            train_reasons.update(data.get('quality', {}).get('reasons', []))
    failed_formats = collections.Counter()
    for path in (root / 'records').glob('*.json'):
        record = json.loads(path.read_text())
        if record.get('parse_status') == 'parse_error':
            failed_formats[record['format']] += 1
    report = {'extraction': dict(metrics), 'parse_error_codes': dict(errors),
              'failed_formats': dict(failed_formats), 'label_statuses': dict(labels), 'label_reason_codes': dict(reasons),
              'label_statuses_by_split': {k: dict(v) for k, v in split_counts.items()},
              'train_label_reason_codes': dict(train_reasons)}
    atomic_json(root / 'reports' / 'diagnostics.json', report)
    emit('diagnostics', **report)


if __name__ == '__main__':
    main()
