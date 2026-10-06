"""Offline reversible quarantine. Private paths and text never reach stdout."""
import argparse
import collections
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
from pathlib import Path

from backend.annotation_data import source_from_payload
from backend.rules import extract_rules, validate
from .pipeline import ROOT, load
from .privacy import atomic_json, emit, require_isolation, silent_libraries

INPUT = Path('/input')


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def contained(root, relative):
    root = root.resolve()
    path = root / relative
    if Path(relative).is_absolute() or '..' in Path(relative).parts or not path.resolve().is_relative_to(root):
        raise RuntimeError('E_PATH_OUTSIDE_ROOT')
    for part in [path, *path.parents]:
        if part == root:
            break
        if part.is_symlink():
            raise RuntimeError('E_SYMLINK')
    return path


def reasons_for(payload, record):
    if record.get('parse_status') == 'parse_error' or not payload or payload.get('parseError'):
        return ['E_PARSE']
    reasons = set()
    if payload.get('incomplete'):
        reasons.add('E_EXTRACTION_INCOMPLETE')
    source = source_from_payload(payload, 'document')
    if not source.cells or any('\ufffd' in c.text for c in source.cells):
        reasons.add('E_UNREADABLE_TEXT')
    # OCR alone, missing optional fields and a model's failed prediction are
    # deliberately not grounds for quarantine.
    for cell in source.cells:
        if re.search(r'(?i)(?:цена|стоимость|сумма)\b[^\n]{0,60}(?:по запросу|договорн\w*|не определена|уточняется|по согласованию)', cell.text):
            reasons.add('E_PRICE_UNSPECIFIED')
    proposal, proof, warnings, _ = extract_rules(source)
    validate(proposal, proof, warnings)
    for item in proposal['items']:
        if item['unitPrice'] is None and item.get('lineTotal') is None:
            reasons.add('E_PRICE_UNAVAILABLE')
        if item['quantity'] is None:
            reasons.add('E_ITEM_INCOMPLETE')
    if any('не сходится' in w or 'отличается от суммы' in w for w in warnings):
        reasons.add('E_ARITHMETIC')
    return sorted(reasons)


def filter_datasets(excluded, root=ROOT):
    folder = root / 'datasets'
    manifest = load(folder / 'manifest.json', [])
    original = {s: load(folder / ('kp_' + s + '.json'), []) for s in ('train', 'val', 'test')}
    for split, examples in original.items():
        entries = sorted((m for m in manifest if m['split'] == split), key=lambda m: m['start'])
        offset = 0
        for entry in entries:
            if entry['start'] != offset or entry['count'] < 0:
                raise RuntimeError('E_DATASET_MANIFEST')
            offset += entry['count']
        if offset != len(examples):
            raise RuntimeError('E_DATASET_MANIFEST')
    datasets = {s: [] for s in original}
    kept = []
    for entry in manifest:
        if entry['document'] in excluded:
            continue
        split = entry['split']
        kept.append({**entry, 'start': len(datasets[split])})
        datasets[split].extend(original[split][entry['start']:entry['start'] + entry['count']])
    return datasets, kept


def audit():
    candidates, counts = {}, collections.Counter()
    for path in (ROOT / 'records').glob('*.json'):
        record = load(path)
        with silent_libraries():
            reasons = reasons_for(load(ROOT / 'parsed' / path.name), record)
        if reasons:
            candidates[path.stem] = reasons
            counts.update(reasons)
    files, scanned = [], 0
    for path in INPUT.rglob('*'):
        if not path.is_file():
            continue
        relative = path.relative_to(INPUT).as_posix()
        path = contained(INPUT, relative)
        identifier = digest(path)
        scanned += 1
        if identifier in candidates:
            files.append({'relative': relative, 'document': identifier})
    found = {f['document'] for f in files}
    candidates = {k: v for k, v in candidates.items() if k in found}
    plan = {'documents': candidates, 'files': files, 'version': 1}
    atomic_json(ROOT / 'quarantine/plan.json', plan)
    report = {'scanned_files': scanned, 'candidate_documents': len(candidates), 'candidate_files': len(files),
              'reason_codes': dict(counts), 'criteria': 'explicit_uncertainty_not_model_rejection'}
    atomic_json(ROOT / 'reports/quarantine_audit.json', report)
    emit('quarantine_audit', **report)


def verified_move(source_root, target_root, entry):
    source = contained(source_root, entry['relative'])
    target = contained(target_root, entry['relative'])
    expected = entry['document']
    if target.exists() and digest(target) != expected:
        raise RuntimeError('E_TARGET_CONFLICT')
    if not source.exists():
        if target.exists():
            return
        raise RuntimeError('E_SOURCE_MISSING')
    if digest(source) != expected:
        raise RuntimeError('E_SOURCE_CHANGED')
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        temporary = target.with_name(target.name + '.quarantine-copy')
        if temporary.exists():
            raise RuntimeError('E_PENDING_COPY')
        shutil.copyfile(source, temporary)
        if digest(temporary) != expected:
            raise RuntimeError('E_COPY_CHECKSUM')
        os.replace(temporary, target)
    # Both resolved paths were checked; the original is removed only after a
    # verified local copy exists. No recursive deletion and no overwrite.
    original = contained(source_root, entry['relative'])
    if digest(original) != expected:
        raise RuntimeError('E_SOURCE_CHANGED')
    original.unlink()


def mark_database(database, documents, active):
    if not Path(database).exists():
        return 0
    changed = 0
    with sqlite3.connect(database) as db:
        backup_path = ROOT / 'quarantine/database-before.sqlite3'
        if not backup_path.exists():
            with sqlite3.connect(backup_path) as backup:
                db.backup(backup)
        db.execute('CREATE TABLE IF NOT EXISTS training_exclusions (document_sha256 TEXT PRIMARY KEY, reasons TEXT NOT NULL)')
        for key, reasons in documents.items():
            if active:
                db.execute('INSERT OR REPLACE INTO training_exclusions VALUES (?,?)', (key, json.dumps(reasons)))
            else:
                db.execute('DELETE FROM training_exclusions WHERE document_sha256=?', (key,))
        for identifier, payload_text, original in db.execute('SELECT id,payload,original FROM annotations'):
            key = identifier if identifier in documents else hashlib.sha256(original).hexdigest()
            if key not in documents:
                continue
            payload = json.loads(payload_text)
            automation = payload.setdefault('automation', {})
            automation.update(quarantined=active, quarantineReasons=documents[key] if active else [])
            db.execute('UPDATE annotations SET payload=?,revision=revision+1 WHERE id=?',
                       (json.dumps(payload, ensure_ascii=False), identifier))
            changed += 1
    return changed


def apply(database):
    plan = load(ROOT / 'quarantine/plan.json')
    if not plan:
        raise RuntimeError('E_AUDIT_REQUIRED')
    old = load(ROOT / 'quarantine/exclusions.json', {})
    documents = {**old.get('documents', {}), **plan['documents']}
    datasets, manifest = filter_datasets(set(documents), ROOT)
    destination = ROOT / 'quarantine/originals'
    # Preflight every path/checksum before the first move.
    for entry in plan['files']:
        source = contained(INPUT, entry['relative'])
        target = contained(destination, entry['relative'])
        if source.exists() and digest(source) != entry['document']:
            raise RuntimeError('E_SOURCE_CHANGED')
        if target.exists() and digest(target) != entry['document']:
            raise RuntimeError('E_TARGET_CONFLICT')
        if not source.exists() and not target.exists():
            raise RuntimeError('E_SOURCE_MISSING')
    backup = ROOT / 'quarantine/datasets-before'
    if not backup.exists():
        shutil.copytree(ROOT / 'datasets', backup)
    files = {f['relative']: f for f in old.get('files', []) + plan['files']}
    registry = {'documents': documents, 'files': list(files.values()), 'state': 'applying'}
    atomic_json(ROOT / 'quarantine/exclusions.json', registry)
    for split, examples in datasets.items():
        atomic_json(ROOT / 'datasets' / ('kp_' + split + '.json'), examples)
    atomic_json(ROOT / 'datasets/manifest.json', manifest)
    dataset_report = load(ROOT / 'reports/dataset.json', {})
    previous_report = ROOT / 'quarantine/dataset-report-before.json'
    if not previous_report.exists():
        atomic_json(previous_report, dataset_report)
    dataset_report.update(documents=len(manifest), examples={s: len(v) for s, v in datasets.items()},
                          documents_by_split=dict(collections.Counter(m['split'] for m in manifest)),
                          excluded_quarantine_documents=len(documents))
    dataset_report['train_tasks'] = dict(collections.Counter('Layout' if 'isItems' in json.loads(e['output']) else 'Requisites'
                                                            for e in datasets['train']))
    atomic_json(ROOT / 'reports/dataset.json', dataset_report)
    changed = mark_database(database, documents, True)
    for number, entry in enumerate(plan['files'], 1):
        verified_move(INPUT, destination, entry)
        if number % 10 == 0:
            emit('quarantine_moving', completed=number, total=len(plan['files']))
    registry['state'] = 'active'
    atomic_json(ROOT / 'quarantine/exclusions.json', registry)
    report = {'quarantined_documents': len(documents), 'quarantined_files': len(files),
              'remaining_files': sum(p.is_file() for p in INPUT.rglob('*')), 'database_marked': changed,
              'active_dataset_examples': {s: len(v) for s, v in datasets.items()}, 'reversible': True}
    atomic_json(ROOT / 'reports/quarantine.json', report)
    emit('quarantine_complete', **report)


def restore(database):
    path = ROOT / 'quarantine/exclusions.json'
    registry = load(path, {})
    # Preflight destinations before restoring anything; never overwrite edits.
    for entry in registry.get('files', []):
        target = contained(INPUT, entry['relative'])
        if target.exists() and digest(target) != entry['document']:
            raise RuntimeError('E_TARGET_CONFLICT')
    for entry in registry.get('files', []):
        verified_move(ROOT / 'quarantine/originals', INPUT, entry)
    mark_database(database, registry.get('documents', {}), False)
    atomic_json(ROOT / 'quarantine/restored.json', registry)
    atomic_json(path, {'documents': {}, 'files': [], 'state': 'restored'})
    emit('quarantine_restored', files=len(registry.get('files', [])), dataset_reexport_required=True)


def verify(database):
    registry = load(ROOT / 'quarantine/exclusions.json', {})
    copied, still_active = 0, 0
    for entry in registry.get('files', []):
        target = contained(ROOT / 'quarantine/originals', entry['relative'])
        if not target.exists() or digest(target) != entry['document']:
            raise RuntimeError('E_COPY_CHECKSUM')
        copied += 1
        still_active += contained(INPUT, entry['relative']).exists()
    from .exclusions import assert_training_allowed
    assert_training_allowed(ROOT)
    if still_active:
        raise RuntimeError('E_QUARANTINE_INCOMPLETE')
    with sqlite3.connect(f'file:{database}?mode=ro', uri=True) as db:
        registered = {row[0] for row in db.execute('SELECT document_sha256 FROM training_exclusions')}
    if not set(registry.get('documents', {})).issubset(registered):
        raise RuntimeError('E_DATABASE_EXCLUSIONS')
    report = {'verified_copies': copied, 'quarantined_files_in_input': still_active,
              'excluded_examples_in_active_dataset': 0, 'database_exclusions_verified': True}
    atomic_json(ROOT / 'reports/quarantine_verification.json', report)
    emit('quarantine_verified', **report)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['audit', 'apply', 'restore', 'verify'])
    parser.add_argument('--database', default='/data/readdocument.sqlite3')
    args = parser.parse_args()
    try:
        require_isolation()
        if args.action == 'audit':
            audit()
        elif args.action == 'apply':
            apply(args.database)
        elif args.action == 'verify':
            verify(args.database)
        else:
            restore(args.database)
    except Exception as error:
        code = str(error) if re.fullmatch(r'E_[A-Z_]+', str(error)) else 'E_QUARANTINE'
        emit('failed', code=code)
        sys.exit(1)
