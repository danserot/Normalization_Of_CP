"""Resumable private corpus pipeline. stdout is aggregate-only JSON."""
import argparse
import collections
import hashlib
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from .privacy import atomic_json, emit, require_isolation, silent_libraries
from .exclusions import excluded_ids

ROOT = Path('/private')


def load(path, default=None):
    return json.loads(Path(path).read_text(encoding='utf-8')) if Path(path).exists() else default


def parse_one(identifier):
    from backend.annotation_data import prepare_annotation
    from .quality import signatures
    record = load(ROOT / 'records' / (identifier + '.json'))
    path = Path('/input') / record['relative_path']
    if path.suffix.lower() == '.doc':
        converted = ROOT / 'converted' / identifier
        converted.mkdir(parents=True, exist_ok=True)
        subprocess.run(['libreoffice', '-env:UserInstallation=file:///tmp/lo-' + identifier, '--headless',
                        '--convert-to', 'docx', '--outdir', str(converted), str(path)],
                       timeout=90, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        choices = list(converted.glob('*.docx'))
        if len(choices) != 1:
            raise RuntimeError('E_CONVERSION')
        path = choices[0]
    payload = prepare_annotation(path.read_bytes(), 'document' + path.suffix.lower())
    payload['incomplete'] = False
    if path.suffix.lower() == '.xlsx':
        import openpyxl
        from io import BytesIO
        formulas = openpyxl.load_workbook(BytesIO(path.read_bytes()), read_only=True, data_only=False)
        try:
            values = {(c['sheet'], c['row'], c['cell']): c['text'] for c in payload['cells']}
            for sheet in formulas:
                sheet.reset_dimensions()
                for row in sheet:
                    if any(c.data_type == 'f' and not values.get((sheet.title, c.row, c.column)) for c in row):
                        payload['incomplete'] = True
        finally:
            formulas.close()
    payload['original_path'] = str(path)
    atomic_json(ROOT / 'parsed' / (identifier + '.json'), payload)
    record.update(parse_status='parsed' if not payload.get('parseError') else 'parse_error',
                  signature=signatures(payload), cell_count=len(payload['cells']))
    atomic_json(ROOT / 'records' / (identifier + '.json'), record)


def ingest():
    excluded = excluded_ids()
    for folder in ('records', 'parsed', 'labels', 'reports', 'datasets'):
        (ROOT / folder).mkdir(parents=True, exist_ok=True)
    paths = [p for p in Path('/input').rglob('*') if p.is_file()]
    formats = dict(collections.Counter(p.suffix.lower() for p in paths))
    # All names and hashes stay in local files. Never print paths or exceptions.
    completed, failures, skipped = 0, 0, 0
    for path in paths:
        identifier = hashlib.sha256(path.read_bytes()).hexdigest()
        if identifier in excluded:
            skipped += 1
            continue
        destination = ROOT / 'records' / (identifier + '.json')
        old = load(destination, {})
        if old.get('parse_status') == 'parsed':
            skipped += 1
            continue
        if old.get('parse_attempts', 0) >= 2:
            failures += 1
            continue
        atomic_json(destination, {**old, 'relative_path': str(path.relative_to('/input')),
                    'format': path.suffix.lower(), 'parse_status': 'pending',
                    'parse_attempts': old.get('parse_attempts', 0) + 1})
        try:
            process = subprocess.Popen([sys.executable, '-m', 'training.local.pipeline', 'parse-one', '--id', identifier],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            try:
                code = process.wait(timeout=600)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
                raise
            if code:
                raise subprocess.CalledProcessError(code, 'parse-one')
            status = load(destination)['parse_status']
            if status == 'parsed':
                completed += 1
            else:
                failures += 1
        except (subprocess.SubprocessError, OSError):
            record = load(destination)
            record.update(parse_status='parse_error', error='E_PARSE')
            atomic_json(destination, record)
            failures += 1
        emit('ingest', processed=completed + failures, parsed=completed, failed=failures, resumed=skipped)
    records = {p.stem: load(p) for p in (ROOT / 'records').glob('*.json')}
    report = {'files': len(paths), 'formats': formats, 'unique_documents': len(records),
              'parsed': sum(r.get('parse_status') == 'parsed' for r in records.values()),
              'failed': sum(r.get('parse_status') != 'parsed' for r in records.values())}
    atomic_json(ROOT / 'reports' / 'ingest.json', report)
    emit('ingest_complete', **report)


def partition():
    from .quality import grouped_partition
    records = {p.stem: load(p) for p in (ROOT / 'records').glob('*.json')
               if load(p).get('parse_status') == 'parsed'}
    corpus = hashlib.sha256(''.join(sorted(records)).encode()).hexdigest()
    path = ROOT / 'partition.json'
    previous = load(path)
    if previous:
        if previous['corpus'] != corpus:
            raise RuntimeError('E_CORPUS_CHANGED')
        assignment = previous['documents']
    else:
        assignment = grouped_partition(records, min(50, max(1, len(records) // 10)))
        atomic_json(path, {'corpus': corpus, 'documents': assignment, 'frozen_before_labels': True})
    counts = dict(collections.Counter(v['split'] for v in assignment.values()))
    report = {'documents': len(assignment), 'groups': len({v['group'] for v in assignment.values()}), **counts}
    atomic_json(ROOT / 'reports' / 'partition.json', report)
    emit('partition_complete', **report)


def label(args):
    from .inference import LocalGenerator, annotate
    from .quality import check_annotation
    assignment = load(ROOT / 'partition.json')['documents']
    excluded = excluded_ids()
    assignment = {k: v for k, v in assignment.items() if k not in excluded}
    teacher = load('/models/models.lock.json')['teacher']
    code_hash = hashlib.sha256(Path(__file__).with_name('inference.py').read_bytes()).hexdigest()
    atomic_json(ROOT / 'reports' / 'label_configuration.json', {'teacher': teacher, 'inference_code_sha256': code_hash,
                'quantization': 'NF4 double quantization', 'temperature': 0, 'max_context': 8192,
                'tasks': ['Layout', 'Requisites'], 'human_reviewed': False})
    with silent_libraries():
        generator = LocalGenerator('/models/teacher')
    emit('teacher_loaded', gpu_mb=generator.memory_mb())
    # Reject a broken generator before it touches the actual corpus. This fixture
    # is different from the few-shot example embedded in the teacher prompt.
    from backend.annotation_data import prepare_annotation
    with silent_libraries():
        synthetic = prepare_annotation(('Поставщик: ООО Синтетика\n'
            'Наименование\tКоличество\tЕд. изм.\tЦена\tСумма\n'
            'Тестовый товар\t2\tшт\t100.00\t200.00').encode(), 'synthetic.txt')
        probe, _ = annotate(generator, synthetic, 'synthetic')
        checked = check_annotation(synthetic, probe)
    atomic_json(ROOT / 'reports' / 'teacher-preflight.json', checked)
    emit('teacher_preflight', passed=checked['passed'], reason_codes=checked['reasons'])
    if not checked['passed']:
        raise RuntimeError('E_TEACHER_PREFLIGHT')
    processed = 0
    counts = collections.Counter(load(p)['status'] for p in (ROOT / 'labels').glob('*.json'))
    for identifier, group in sorted(assignment.items(), key=lambda item: (
            {'train': 0, 'val': 1, 'test': 2}[item[1]['split']], item[0])):
        path = ROOT / 'labels' / (identifier + '.json')
        old = load(path, {})
        if old.get('status') == 'auto_validated' or old.get('attempts', 0) >= 2 or (
                old.get('status') == 'needs_review' and old.get('inference_code_sha256') == code_hash):
            continue
        if args.limit and processed >= args.limit:
            break
        payload = load(ROOT / 'parsed' / (identifier + '.json'))
        started = time.monotonic()
        record = {'attempts': old.get('attempts', 0) + 1, 'status': 'auto_generated', 'human_reviewed': False,
                  'teacher_revision': teacher['revision'], 'inference_code_sha256': code_hash}
        try:
            with silent_libraries():
                annotation, raw_tasks = annotate(generator, payload, group['group'])
                quality = check_annotation(payload, annotation)
            record.update(annotation=annotation.model_dump(), tasks=raw_tasks, quality=quality,
                          status='auto_validated' if quality['passed'] else 'needs_review')
        except Exception as exc:
            code = str(exc) if str(exc) in ('E_CONTEXT', 'E_TIMEOUT', 'E_SCHEMA') else 'E_MODEL'
            record.update(status='failed', quality={'passed': False, 'reasons': [code], 'human_reviewed': False})
        record['seconds'] = round(time.monotonic() - started, 2)
        if old:
            atomic_json(ROOT / 'label_history' / identifier / (str(old.get('attempts', 0)) + '.json'), old)
        atomic_json(path, record)
        processed += 1
        if old.get('status'):
            counts[old['status']] -= 1
        counts[record['status']] += 1
        atomic_json(ROOT / 'reports' / 'labeling.json', dict(counts))
        emit('labeling', processed=sum(counts.values()), **dict(counts), last_seconds=record['seconds'])


def export(args):
    from backend.annotation_data import Annotation, training_examples, validate_annotation
    assignment = load(ROOT / 'partition.json')['documents']
    excluded = excluded_ids()
    assignment = {k: v for k, v in assignment.items() if k not in excluded}
    datasets = {'train': [], 'val': [], 'test': []}
    manifest = []
    human = {}
    if Path(args.database).exists():
        with sqlite3.connect(args.database) as connection:
            human = {row[0]: row[1] for row in connection.execute(
                "SELECT id,annotation FROM annotations WHERE status='reviewed'")}
    for identifier, group in assignment.items():
        record = load(ROOT / 'labels' / (identifier + '.json'), {})
        if record.get('status') != 'auto_validated' and identifier not in human:
            continue
        payload = load(ROOT / 'parsed' / (identifier + '.json'))
        reviewed = identifier in human
        annotation = Annotation.model_validate_json(human[identifier]) if reviewed else Annotation.model_validate(record['annotation'])
        if reviewed:
            validate_annotation(annotation, payload, 'document', True)
        examples = training_examples(annotation, payload, 'document')
        split = group['split']
        start = len(datasets[split])
        datasets[split].extend(examples)
        manifest.append({'document': identifier, **group, 'start': start, 'count': len(examples),
                         'provenance': 'human_reviewed' if reviewed else 'auto_validated', 'human_reviewed': reviewed})
    for split, examples in datasets.items():
        atomic_json(ROOT / 'datasets' / ('kp_' + split + '.json'), examples)
    atomic_json(ROOT / 'datasets' / 'manifest.json', manifest)
    report = {'documents': len(manifest), 'examples': {k: len(v) for k, v in datasets.items()},
              'human_reviewed': sum(m['human_reviewed'] for m in manifest), 'evaluation_limitation': 'pseudo_labels_are_not_ground_truth'}
    report['train_tasks'] = dict(collections.Counter('Layout' if 'isItems' in json.loads(e['output']) else
            'Requisites_positive' if json.loads(e['output']).get('fields') else 'Requisites_empty' for e in datasets['train']))
    atomic_json(ROOT / 'reports' / 'dataset.json', report)
    emit('export_complete', **report)


def import_review(args):
    from backend.annotations import initialize_annotations
    from .quality import REASONS
    assignment = dict(load(ROOT / 'partition.json')['documents'])
    for path in (ROOT / 'records').glob('*.json'):
        if path.stem not in assignment and load(path).get('parse_status') == 'parse_error':
            assignment[path.stem] = {'group': path.stem, 'split': 'review_only'}
    # Round-robin format/split/reason buckets creates a small diverse queue.
    excluded = excluded_ids()
    assignment = {k: v for k, v in assignment.items() if k not in excluded}
    buckets = collections.defaultdict(list)
    for identifier, group in assignment.items():
        record = load(ROOT / 'records' / (identifier + '.json'))
        label = load(ROOT / 'labels' / (identifier + '.json'), {})
        reasons = label.get('quality', {}).get('reasons', [])
        buckets[(group['split'], record['format'], tuple(reasons))].append(identifier)
    chosen = []
    while buckets and len(chosen) < args.limit:
        for split in ('train', 'val', 'test', 'review_only'):
            keys = [key for key in buckets if key[0] == split]
            if not keys or len(chosen) >= args.limit:
                continue
            key = keys[0]
            candidates = buckets.pop(key)
            chosen.append(candidates.pop(0))
            if candidates:
                buckets[key] = candidates
    database = Path(args.database)
    database.parent.mkdir(parents=True, exist_ok=True)
    inserted = 0
    with sqlite3.connect(database) as connection:
        initialize_annotations(connection)
        if args.rebalance:
            # Rebalance only this pipeline's untouched machine drafts. Keep a
            # local database backup; never remove a user edit or human review.
            with sqlite3.connect(ROOT / 'review-before-rebalance.sqlite3') as backup:
                connection.backup(backup)
            for row in connection.execute('SELECT id,revision,status,payload FROM annotations').fetchall():
                automation = json.loads(row[3]).get('automation', {})
                if row[0] in assignment and row[1] == 1 and row[2] == 'draft' and automation.get('humanReviewed') is False:
                    connection.execute('DELETE FROM annotations WHERE id=? AND revision=1 AND status=?', (row[0], 'draft'))
        for identifier in chosen:
            payload = load(ROOT / 'parsed' / (identifier + '.json'))
            if not payload:
                from backend.annotation_data import draft_annotation
                from backend.extraction import Source
                record = load(ROOT / 'records' / (identifier + '.json'))
                payload = {'cells': [], 'parseError': 'Не удалось прочитать документ.',
                           'warnings': ['Ошибка чтения; необходима проверка оригинала.'],
                           'preview': {'kind': 'text', 'text': ''},
                           'original_path': str(Path('/input') / record['relative_path']),
                           'annotation': draft_annotation(Source('document'), {})}
            label = load(ROOT / 'labels' / (identifier + '.json'), {})
            path = Path(payload['original_path'])
            content = path.read_bytes()
            fingerprint = hashlib.sha256(path.suffix.lower().encode() + b'\0' + content).hexdigest()
            annotation = label.get('annotation') or payload['annotation']
            annotation['group'] = assignment[identifier]['group']
            for field in annotation['fields']:
                field['state'] = 'pending'
            for table in annotation['tables']:
                table['reviewed'] = False
            reasons = label.get('quality', {}).get('reasons', [])
            annotation['notes'] = 'Автоматическая разметка. Требуется проверка человеком.'
            annotation['issues'] = [REASONS.get(code, 'Требуется проверка.') for code in reasons]
            payload['warnings'] = list(dict.fromkeys(payload.get('warnings', []) + annotation['issues']))
            payload['automation'] = {'status': label.get('status', 'unprocessed'),
                                     'humanReviewed': False, 'split': assignment[identifier]['split'],
                                     'reasons': reasons}
            payload.pop('annotation', None)
            now = datetime.now(timezone.utc).isoformat()
            inserted += connection.execute('INSERT OR IGNORE INTO annotations VALUES (?,?,?,?,?,?,?,?,?,?)',
                (identifier, fingerprint, path.name, content, now, now, 1, 'draft',
                 json.dumps(payload, ensure_ascii=False), json.dumps(annotation, ensure_ascii=False))).rowcount
    emit('review_import_complete', selected=len(chosen), inserted=inserted,
         by_split=dict(collections.Counter(assignment[i]['split'] for i in chosen)))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('stage', choices=['ingest', 'parse-one', 'partition', 'label', 'export', 'import-review', 'audit'])
    parser.add_argument('--id')
    parser.add_argument('--limit', type=int, default=0)
    parser.add_argument('--database', default='/private/review.sqlite3')
    parser.add_argument('--rebalance', action='store_true')
    args = parser.parse_args()
    try:
        audit = require_isolation()
        ROOT.mkdir(exist_ok=True)
        atomic_json(ROOT / 'reports' / 'isolation.json', audit)
        if args.stage == 'audit':
            emit('isolation_verified', **audit)
        elif args.stage == 'parse-one':
            with silent_libraries():
                parse_one(args.id)
        elif args.stage == 'ingest':
            ingest()
        elif args.stage == 'partition':
            partition()
        elif args.stage == 'label':
            label(args)
        elif args.stage == 'export':
            export(args)
        elif args.stage == 'import-review':
            import_review(args)
    except Exception as error:
        code = str(error) if str(error).startswith('E_') and len(str(error)) < 40 else 'E_STAGE'
        emit('failed', code=code, error_type=type(error).__name__)
        sys.exit(1)


if __name__ == '__main__':
    main()
