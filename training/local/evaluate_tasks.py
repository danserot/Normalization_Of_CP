"""Evaluate the frozen certified-task subset; not whole-document accuracy."""
import argparse
import collections
import hashlib
import json
import sys
import time

from backend.universal import Layout
from .pipeline import ROOT, load
from .privacy import atomic_json, emit, require_isolation, silent_libraries


def score(predicted, target, payload):
    p, t = predicted, target
    cells = {c[0]: c[2] for row in payload['rows'] for c in row['cells']}
    previous = payload.get('previousLayout') or {}
    cells.update({x['labelCell']: x['label'] for x in previous.get('components', []) + previous.get('extras', [])})
    labels = [*p.components, *p.extras]
    columns = lambda x: (x.isItems, x.nameColumn, x.quantityColumn, x.unitColumn,
                         [(c.priceColumn, c.totalColumn) for c in x.components], [e.column for e in x.extras])
    rows = lambda x: set(range(x.firstRow, x.lastRow + 1)) if x.isItems else set()
    pr, tr = rows(p), rows(t)
    return {'valid': 1, 'exact_layout': int(p == t), 'exact_columns': int(columns(p) == columns(t)),
            'target_rows': len(tr), 'predicted_rows': len(pr), 'correct_rows': len(pr & tr),
            'predicted_references': len(labels),
            'grounded_references': sum(x.labelCell in cells and x.label in cells[x.labelCell] for x in labels),
            'exact_references': int([(x.labelCell, x.label) for x in labels] ==
                                    [(x.labelCell, x.label) for x in [*t.components, *t.extras]])}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--variant', choices=['baseline', 'trained', 'gguf'], required=True)
    args = parser.parse_args()
    require_isolation()
    weight_tag = 'base'
    if args.variant != 'baseline':
        weights = ROOT / ('training/adapter/adapter_model.safetensors' if args.variant == 'trained' else 'gguf/model-q4_k_m.gguf')
        with weights.open('rb') as stream:
            weight_tag = hashlib.file_digest(stream, 'sha256').hexdigest()[:16]
    report_path = ROOT / 'reports' / ('tasks-' + args.variant + '.json')
    if report_path.exists():
        atomic_json(ROOT / 'reports/history' / ('tasks-' + args.variant + '-' + str(time.time_ns()) + '.json'), load(report_path))
    with silent_libraries():
        if args.variant == 'gguf':
            from .serve import LlamaGenerator
            generator = LlamaGenerator()
        else:
            from .inference import LocalGenerator
            generator = LocalGenerator('/models/student', str(ROOT / 'training/adapter') if args.variant == 'trained' else None)
    emit('task_evaluation_loaded', variant=args.variant)
    report = {'variant': args.variant, 'scope': 'certified_layout_subset_only', 'human_gold': None,
              'requisites': None, 'limitation': 'Agreement with automatic labels is not accuracy; excludes uncertain documents'}
    for split in ('val', 'test'):
        path = ROOT / 'datasets' / ('kp_' + split + '.json')
        fingerprint = hashlib.sha256(path.read_bytes()).hexdigest()
        samples = load(path)
        results = ROOT / 'evaluation' / ('tasks-' + args.variant + '-' + split + '-' + weight_tag)
        counts, seconds = collections.Counter(), []
        for index, sample in enumerate(samples):
            destination = results / (str(index) + '.json')
            record = load(destination, {})
            if record and record.get('dataset_sha256') != fingerprint:
                raise RuntimeError('E_DATASET_CHANGED')
            target = Layout.model_validate_json(sample['output'])
            payload = json.loads(sample['instruction'])
            if not record:
                start = time.monotonic()
                record = {'valid': False, 'dataset_sha256': fingerprint}
                try:
                    with silent_libraries():
                        predicted = generator(sample['system'], payload, Layout, max_tokens=500)
                    record.update(valid=True, answer=predicted.model_dump())
                except Exception:
                    record['error'] = 'E_INFERENCE'
                record['seconds'] = time.monotonic() - start
                atomic_json(destination, record)
            counts['tasks'] += 1
            seconds.append(record['seconds'])
            if record['valid']:
                counts.update(score(Layout.model_validate(record['answer']), target, payload))
            else:
                counts['target_rows'] += target.lastRow - target.firstRow + 1 if target.isItems else 0
            emit('task_evaluating', variant=args.variant, split=split, completed=index + 1, total=len(samples))
        ratio = lambda n, d: round(counts[n] / counts[d], 4) if counts[d] else None
        report[split] = {**dict(counts), 'schema_valid_rate': ratio('valid', 'tasks'),
                         'column_agreement': ratio('exact_columns', 'tasks'),
                         'reference_agreement': ratio('exact_references', 'tasks'),
                         'row_precision': ratio('correct_rows', 'predicted_rows'),
                         'row_recall': ratio('correct_rows', 'target_rows'),
                         'grounded_reference_rate': ratio('grounded_references', 'predicted_references'),
                         'mean_task_seconds': round(sum(seconds) / len(seconds), 2) if seconds else None}
    report['cpu_server_rss_mb' if args.variant == 'gguf' else 'gpu_peak_mb'] = generator.memory_mb()
    atomic_json(report_path, report)
    emit('task_evaluation_complete', **report)


if __name__ == '__main__':
    try:
        main()
    except Exception:
        emit('failed', code='E_TASK_EVALUATION')
        sys.exit(1)
