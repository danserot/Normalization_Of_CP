"""Frozen test evaluation: gold, pseudo-label agreement and safety are distinct."""
import argparse
import collections
import json
import sqlite3
import sys
import time
from pathlib import Path

from backend.annotation_data import Annotation
from .pipeline import ROOT, load
from .privacy import atomic_json, emit, require_isolation, silent_libraries


def compare(predicted, target):
    p = {f.field: f for f in predicted.fields if f.state == 'found'}
    t = {f.field: f for f in target.fields if f.state == 'found'}
    columns = lambda a: {x.block: (x.isItems, x.nameColumn, x.quantityColumn, x.unitColumn,
                                  tuple((c.priceColumn, c.totalColumn) for c in x.components)) for x in a.tables}
    rowset = lambda a: {(x.block, r) for x in a.tables if x.isItems for r in range(x.firstRow, x.lastRow + 1)}
    pc, tc = columns(predicted), columns(target)
    pr, tr = rowset(predicted), rowset(target)
    return {'documents': 1, 'predicted_fields': len(p), 'target_fields': len(t),
            'exact_values': sum(k in t and v.value == t[k].value for k, v in p.items()),
            'exact_cells': sum(k in t and v.cell == t[k].cell and v.value == t[k].value for k, v in p.items()),
            'target_tables': len(tc), 'exact_columns': sum(k in pc and pc[k] == v for k, v in tc.items()),
            'predicted_rows': len(pr), 'target_rows': len(tr), 'exact_rows': len(pr & tr)}


def ratios(counts):
    def ratio(n, d):
        return round(counts[n] / counts[d], 4) if counts[d] else None
    return {**dict(counts), 'field_precision': ratio('exact_values', 'predicted_fields'),
            'field_recall': ratio('exact_values', 'target_fields'),
            'cell_precision': ratio('exact_cells', 'predicted_fields'),
            'column_accuracy': ratio('exact_columns', 'target_tables'),
            'row_precision': ratio('exact_rows', 'predicted_rows'), 'row_recall': ratio('exact_rows', 'target_rows')}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--variant', choices=['baseline', 'trained', 'gguf'], required=True)
    parser.add_argument('--split', choices=['test', 'val'], default='test')
    parser.add_argument('--database', default='/private/review.sqlite3')
    args = parser.parse_args()
    require_isolation()
    from .inference import LocalGenerator, annotate
    from .quality import check_annotation
    assignment = load(ROOT / 'partition.json')['documents']
    destination = ROOT / 'evaluation' / (args.variant + '-' + args.split)
    destination.mkdir(parents=True, exist_ok=True)
    with silent_libraries():
        if args.variant == 'gguf':
            from .serve import LlamaGenerator
            generator = LlamaGenerator()
        else:
            generator = LocalGenerator('/models/student', str(ROOT / 'training' / 'adapter') if args.variant == 'trained' else None)
    emit('evaluation_loaded', variant=args.variant)
    for identifier, group in assignment.items():
        if group['split'] != args.split or (destination / (identifier + '.json')).exists():
            continue
        payload = load(ROOT / 'parsed' / (identifier + '.json'))
        started = time.monotonic()
        record = {'valid': False}
        try:
            with silent_libraries():
                annotation, tasks = annotate(generator, payload, group['group'])
                quality = check_annotation(payload, annotation)
            record.update(valid=True, annotation=annotation.model_dump(), quality=quality)
        except Exception:
            record['error'] = 'E_INFERENCE'
        record['seconds'] = time.monotonic() - started
        atomic_json(destination / (identifier + '.json'), record)
        emit('evaluating', variant=args.variant, completed=len(list(destination.glob('*.json'))))
    safety, pseudo, gold = collections.Counter(), collections.Counter(), collections.Counter()
    seconds = []
    human = {}
    if Path(args.database).exists():
        with sqlite3.connect(args.database) as db:
            human = {row[0]: Annotation.model_validate_json(row[1]) for row in db.execute(
                "SELECT id,annotation FROM annotations WHERE status='reviewed'")}
    for path in destination.glob('*.json'):
        result = load(path)
        safety['documents'] += 1
        seconds.append(result['seconds'])
        label = load(ROOT / 'labels' / path.name, {})
        if not result['valid']:
            safety['invalid_documents'] += 1
            empty = Annotation(fields=[], tables=[])
            if label.get('status') == 'auto_validated':
                pseudo.update(compare(empty, Annotation.model_validate(label['annotation'])))
            if path.stem in human:
                gold.update(compare(empty, human[path.stem]))
            continue
        predicted = Annotation.model_validate(result['annotation'])
        quality = result['quality']
        safety['auto_checks_passed'] += int(quality['passed'])
        safety['predicted_fields'] += sum(f.state == 'found' for f in predicted.fields)
        safety['grounded_fields'] += quality['grounded_fields']
        safety['item_rows'] += quality['item_rows']
        if label.get('status') == 'auto_validated':
            pseudo.update(compare(predicted, Annotation.model_validate(label['annotation'])))
        if path.stem in human:
            gold.update(compare(predicted, human[path.stem]))
    count = safety['documents']
    report = {'variant': args.variant, 'split': args.split, 'automatic_checks': dict(safety),
              'schema_valid_document_rate': (count - safety['invalid_documents']) / count if count else None,
              'unsupported_field_rate': 1 - safety['grounded_fields'] / safety['predicted_fields'] if safety['predicted_fields'] else None,
              'pseudo_label_agreement': ratios(pseudo), 'human_gold': ratios(gold) if gold else None,
              'human_gold_available': len(human), 'mean_document_seconds': sum(seconds) / len(seconds) if seconds else None,
              'limitation': 'No human gold: agreement is not accuracy' if not gold else ''}
    report['cpu_server_rss_mb' if args.variant == 'gguf' else 'gpu_peak_mb'] = generator.memory_mb()
    atomic_json(ROOT / 'reports' / (args.variant + '-' + args.split + '.json'), report)
    emit('evaluation_complete', variant=args.variant, documents=count, invalid=safety['invalid_documents'],
         human_gold_documents=gold['documents'])


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        emit('failed', code='E_EVALUATION', error_type=type(error).__name__)
        sys.exit(1)
